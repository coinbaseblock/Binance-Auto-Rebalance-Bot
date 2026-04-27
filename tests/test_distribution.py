"""
Tests for distribution order placement mode:
  - Strategy.calculate_child_orders()
  - Strategy.calculate_all_child_orders()
  - Backtester distribution-mode simulation
"""
import json
import os
import tempfile
from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.strategy import Strategy
from backtest.backtester import Backtester


def _write_config(config):
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    json.dump(config, f)
    f.close()
    return f.name


@pytest.fixture
def distribution_config():
    return {
        "enabled": True,
        "name": "BTC Distribution",
        "pair": "BTCUSDT",
        "description": "Test strategy using distribution mode",
        "ladder_config": {
            "base_gap": 0.02,
            "ladders": 5,
            "fibonacci": [1, 1, 2, 3, 5],
            "unit_size_btc": 0.001
        },
        "capital_allocation": {"max_allocation_percent": 0.5, "reserve_percent": 0.1},
        "risk_management": {"safety_multiplier": 1.0, "stop_loss_percent": -0.3,
                             "take_profit_percent": 0.2},
        "execution": {"auto_rebalance": True, "rebalance_interval_hours": 24,
                      "min_profit_to_close": 0.005},
        "order_placement": {
            "mode": "distribution",
            "child_order_usdt": 20.0,
            "proximity_percent": 0.02,
            "max_open_orders_cap": 100,
            "min_children_per_ladder": 2,
            "max_children_per_ladder": 12,
        },
    }


@pytest.fixture
def strategy(distribution_config):
    path = _write_config(distribution_config)
    try:
        s = Strategy(path)
        s.update_prices(50000.0)
        yield s
    finally:
        os.unlink(path)


class TestCalculateChildOrders:
    def test_is_distribution_mode(self, strategy):
        assert strategy.is_distribution_mode() is True

    def test_children_sum_to_parent_usdt(self, strategy):
        ladder = strategy.ladders[2]  # pick a mid ladder
        next_buy = strategy.ladders[3]['buy_price']
        children = strategy.calculate_child_orders(ladder, next_buy)
        assert len(children) >= 2
        total = sum(c['usdt_cost'] for c in children)
        assert total == pytest.approx(ladder['usdt_cost'], rel=1e-9)

    def test_child_prices_sorted_descending(self, strategy):
        ladder = strategy.ladders[2]
        next_buy = strategy.ladders[3]['buy_price']
        children = strategy.calculate_child_orders(ladder, next_buy)
        prices = [c['buy_price'] for c in children]
        assert prices == sorted(prices, reverse=True)

    def test_child_prices_within_range(self, strategy):
        ladder = strategy.ladders[2]
        next_buy = strategy.ladders[3]['buy_price']
        children = strategy.calculate_child_orders(ladder, next_buy)
        top = ladder['buy_price']
        # Top child should be at or very near the ladder's buy_price, and the
        # deepest child should approach (but not cross below) the next ladder.
        assert children[0]['buy_price'] <= top
        assert children[-1]['buy_price'] >= next_buy - 1e-6

    def test_default_sell_price_min_of_both(self, strategy):
        """Default child_sell_mode is 'min_of_both': each child's sell is at
        most the parent ladder's planned exit, and at most child_buy *
        (1 + child_profit_percent). Deep children should sell BELOW the
        parent ladder's sell_price so small bounces close them quickly."""
        cfg = strategy.get_distribution_config()
        profit = cfg['child_profit_percent']
        ladder = strategy.ladders[2]
        next_buy = strategy.ladders[3]['buy_price']
        children = strategy.calculate_child_orders(ladder, next_buy)
        assert len(children) >= 2
        for c in children:
            expected = min(c['buy_price'] * (1 + profit), ladder['sell_price'])
            assert c['sell_price'] == pytest.approx(expected)
            assert c['sell_price'] <= ladder['sell_price']
        # Deepest child should benefit from the per-child cap (its buy is
        # well below ladder.sell_price, so it sells earlier on a bounce).
        assert children[-1]['sell_price'] < ladder['sell_price']

    def test_legacy_ladder_sell_mode(self, distribution_config):
        """Opting into child_sell_mode='ladder' restores the legacy hybrid
        pairing where every child shares the parent ladder's sell_price."""
        distribution_config['order_placement']['child_sell_mode'] = 'ladder'
        path = _write_config(distribution_config)
        try:
            s = Strategy(path)
            s.update_prices(50000.0)
            ladder = s.ladders[2]
            next_buy = s.ladders[3]['buy_price']
            children = s.calculate_child_orders(ladder, next_buy)
            for c in children:
                assert c['sell_price'] == ladder['sell_price']
        finally:
            os.unlink(path)

    def test_geometric_spread_is_evenly_spaced(self, distribution_config):
        """Default 'geometric' spread should give roughly constant ratio
        between adjacent child prices — no top-cluster like the legacy
        Fibonacci weighting."""
        # Use a tight ladder range and many children to make the contrast clear
        distribution_config['order_placement']['child_order_usdt'] = 5.0
        distribution_config['order_placement']['min_children_per_ladder'] = 6
        distribution_config['order_placement']['max_children_per_ladder'] = 6
        path = _write_config(distribution_config)
        try:
            s = Strategy(path)
            s.update_prices(50000.0)
            ladder = s.ladders[1]
            next_buy = s.ladders[2]['buy_price']
            children = s.calculate_child_orders(ladder, next_buy)
            assert len(children) == 6
            ratios = [
                children[i + 1]['buy_price'] / children[i]['buy_price']
                for i in range(len(children) - 1)
            ]
            # All ratios should be equal under geometric spacing
            for r in ratios:
                assert r == pytest.approx(ratios[0], rel=1e-6)
        finally:
            os.unlink(path)

    def test_fibonacci_spread_mode_legacy(self, distribution_config):
        """spread_mode='fibonacci' restores the legacy log-Fibonacci weighting
        (children clustered near the top)."""
        distribution_config['order_placement']['spread_mode'] = 'fibonacci'
        distribution_config['order_placement']['child_order_usdt'] = 5.0
        distribution_config['order_placement']['min_children_per_ladder'] = 6
        distribution_config['order_placement']['max_children_per_ladder'] = 6
        path = _write_config(distribution_config)
        try:
            s = Strategy(path)
            s.update_prices(50000.0)
            ladder = s.ladders[1]
            next_buy = s.ladders[2]['buy_price']
            children = s.calculate_child_orders(ladder, next_buy)
            # First gap (top) should be much smaller than last gap (bottom)
            first_gap = children[0]['buy_price'] - children[1]['buy_price']
            last_gap = children[-2]['buy_price'] - children[-1]['buy_price']
            assert last_gap > first_gap * 3  # heavy weighting at the bottom
        finally:
            os.unlink(path)

    def test_fibonacci_weighted_sizing(self, strategy):
        """Deeper children should carry larger USDT (martingale-like)."""
        ladder = strategy.ladders[2]
        next_buy = strategy.ladders[3]['buy_price']
        children = strategy.calculate_child_orders(ladder, next_buy)
        if len(children) >= 3:
            assert children[-1]['usdt_cost'] >= children[0]['usdt_cost']

    def test_deepest_ladder_without_next(self, strategy):
        ladder = strategy.ladders[-1]
        children = strategy.calculate_child_orders(ladder, None)
        assert len(children) >= 1
        assert children[-1]['buy_price'] < ladder['buy_price']

    def test_child_count_scales_with_target_size(self, distribution_config):
        # Smaller target size → more children
        distribution_config['order_placement']['child_order_usdt'] = 5.0
        path = _write_config(distribution_config)
        try:
            s = Strategy(path)
            s.update_prices(50000.0)
            ladder = s.ladders[3]
            next_buy = s.ladders[4]['buy_price']
            small_children = s.calculate_child_orders(ladder, next_buy)
        finally:
            os.unlink(path)

        distribution_config['order_placement']['child_order_usdt'] = 100.0
        path = _write_config(distribution_config)
        try:
            s2 = Strategy(path)
            s2.update_prices(50000.0)
            ladder = s2.ladders[3]
            next_buy = s2.ladders[4]['buy_price']
            large_children = s2.calculate_child_orders(ladder, next_buy)
        finally:
            os.unlink(path)

        assert len(small_children) >= len(large_children)

    def test_max_children_cap(self, distribution_config):
        distribution_config['order_placement']['child_order_usdt'] = 0.01
        distribution_config['order_placement']['max_children_per_ladder'] = 8
        path = _write_config(distribution_config)
        try:
            s = Strategy(path)
            s.update_prices(50000.0)
            children = s.calculate_child_orders(
                s.ladders[3], s.ladders[4]['buy_price']
            )
            assert len(children) <= 8
        finally:
            os.unlink(path)

    def test_calculate_all_child_orders(self, strategy):
        all_children = strategy.calculate_all_child_orders()
        assert set(all_children.keys()) == {l['level'] for l in strategy.ladders}
        for level, children in all_children.items():
            assert len(children) >= 1


def _make_oscillating_data(start_price, low_frac, high_frac, num_candles=500):
    """Build a synthetic 5m OHLCV DataFrame.

    The first candle closes near start_price (so Strategy.update_prices() anchors
    the ladders at start_price). Subsequent candles oscillate: even candles sweep
    down to low_frac*start_price, odd candles sweep up to high_frac*start_price.
    This gives every ladder child repeated opportunities to fill on both sides.
    """
    idx = pd.date_range('2024-01-01', periods=num_candles, freq='5min')
    rows = []
    # Anchor candle: open/close == start_price, narrow range
    rows.append({'open': start_price, 'high': start_price * 1.001,
                 'low': start_price * 0.999, 'close': start_price, 'volume': 1.0})
    # Then oscillate
    for i in range(1, num_candles):
        if i % 2 == 1:
            # Down-sweep: open at start, low at low_frac, close at low_frac
            rows.append({'open': start_price, 'high': start_price,
                         'low': start_price * low_frac,
                         'close': start_price * low_frac, 'volume': 1.0})
        else:
            # Up-sweep: open at low, high at high_frac, close at high_frac
            rows.append({'open': start_price * low_frac,
                         'high': start_price * high_frac,
                         'low': start_price * low_frac,
                         'close': start_price * high_frac, 'volume': 1.0})
    return pd.DataFrame(rows, index=idx)


class TestBacktestDistribution:
    def test_run_distribution_produces_trades(self, strategy):
        # Oscillate wide enough to trigger all ladders and their sells
        data = _make_oscillating_data(
            start_price=50000.0, low_frac=0.30, high_frac=1.20, num_candles=200
        )
        bt = Backtester(strategy, data)
        bt.initial_capital = 50000
        bt.capital = 50000
        report = bt.run()

        assert 'error' not in report
        assert report['trades']['total'] > 0
        # Each trade is a child fill — should be many more than 5 ladders
        total_children = sum(
            len(cs) for cs in strategy.calculate_all_child_orders().values()
        )
        # At least some children should have completed full cycles
        assert report['trades']['total'] <= total_children
        # Distribution should be net-profitable in an oscillating market
        assert report['capital']['profit'] > 0

    def test_cap_limits_open_orders(self, distribution_config):
        # Set a very low cap; some children should remain pending at end
        distribution_config['order_placement']['max_open_orders_cap'] = 3
        path = _write_config(distribution_config)
        try:
            s = Strategy(path)
            s.update_prices(50000.0)
            # Drop sharply so many children want to promote at once
            data = _make_oscillating_data(
                start_price=50000.0, low_frac=0.30, high_frac=1.20, num_candles=20
            )
            bt = Backtester(s, data)
            bt.initial_capital = 50000
            bt.capital = 50000
            bt.run()
            last = bt.portfolio_value[-1]
            # With cap=3 and 5 ladders × multiple children, at some point
            # pending_children must have been > 0
            peaks = max(pv['pending_children'] for pv in bt.portfolio_value)
            assert peaks > 0
        finally:
            os.unlink(path)
