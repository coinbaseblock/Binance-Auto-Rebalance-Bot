"""
Tests for distribution order placement mode:
  - Strategy.calculate_child_orders()
  - Strategy.calculate_all_child_orders()
  - Backtester distribution-mode simulation
  - OrderManager NOTIONAL safety guard
"""
import json
import os
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from src.order_manager import OrderManager
from src.portfolio import Portfolio
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

    def test_min_notional_caps_child_count(self, distribution_config):
        """Regression: with Fibonacci-weighted sizing, the smallest top child
        is total_usdt / sum(fib[:n]). For a $167 ladder at n=7 that's ~$5.06,
        which falls below Binance's $5 min_notional after qty step-rounding
        and triggers "Filter failure: NOTIONAL" loops. Passing min_notional
        to calculate_child_orders should reduce n until the smallest child
        clears the buffered floor."""
        # ZEC-Distribution-5K-style: small unit_size relative to ladder count
        distribution_config['ladder_config']['unit_size_btc'] = 0.0033
        distribution_config['order_placement']['child_order_usdt'] = 25.0
        distribution_config['order_placement']['min_children_per_ladder'] = 4
        distribution_config['order_placement']['max_children_per_ladder'] = 12
        path = _write_config(distribution_config)
        try:
            s = Strategy(path)
            s.update_prices(50000.0)
            ladder = s.ladders[0]  # The shallowest ladder (smallest usdt_cost)
            next_buy = s.ladders[1]['buy_price']
            # Without min_notional: small top children (the bug)
            no_floor = s.calculate_child_orders(ladder, next_buy, min_notional=0.0)
            # With min_notional=$5 (Binance default): every child must clear
            # $5 × 1.10 = $5.50
            with_floor = s.calculate_child_orders(ladder, next_buy, min_notional=5.0)
            min_with_floor = min(c['usdt_cost'] for c in with_floor)
            assert min_with_floor >= 5.0 * 1.10 - 1e-9
            # And the floored variant should never have MORE children than
            # the unfloored one (the cap can only shrink n).
            assert len(with_floor) <= len(no_floor)
        finally:
            os.unlink(path)

    def test_min_notional_keeps_total_usdt(self, distribution_config):
        """The floor cap only changes child count; total USDT cost across
        children must still equal the parent ladder's usdt_cost (no leakage)."""
        distribution_config['order_placement']['child_order_usdt'] = 25.0
        distribution_config['order_placement']['min_children_per_ladder'] = 4
        distribution_config['order_placement']['max_children_per_ladder'] = 12
        path = _write_config(distribution_config)
        try:
            s = Strategy(path)
            s.update_prices(50000.0)
            for ladder, next_buy in zip(
                s.ladders,
                [l['buy_price'] for l in s.ladders[1:]] + [None],
            ):
                children = s.calculate_child_orders(ladder, next_buy, min_notional=10.0)
                if not children:
                    continue
                total = sum(c['usdt_cost'] for c in children)
                assert total == pytest.approx(ladder['usdt_cost'], rel=1e-9)
        finally:
            os.unlink(path)

    def test_min_notional_buffer_is_configurable(self, distribution_config):
        """notional_safety_buffer scales the floor — a higher buffer means
        fewer children for the same min_notional."""
        distribution_config['order_placement']['child_order_usdt'] = 25.0
        distribution_config['order_placement']['min_children_per_ladder'] = 2
        distribution_config['order_placement']['max_children_per_ladder'] = 12

        # Default buffer (1.10)
        path1 = _write_config(distribution_config)
        try:
            s1 = Strategy(path1)
            s1.update_prices(50000.0)
            ladder1 = s1.ladders[0]
            next_buy1 = s1.ladders[1]['buy_price']
            children_default = s1.calculate_child_orders(ladder1, next_buy1, min_notional=5.0)
        finally:
            os.unlink(path1)

        # Higher buffer (2.0) — should give fewer-or-equal children
        distribution_config['order_placement']['notional_safety_buffer'] = 2.0
        path2 = _write_config(distribution_config)
        try:
            s2 = Strategy(path2)
            s2.update_prices(50000.0)
            ladder2 = s2.ladders[0]
            next_buy2 = s2.ladders[1]['buy_price']
            children_strict = s2.calculate_child_orders(ladder2, next_buy2, min_notional=5.0)
        finally:
            os.unlink(path2)

        assert len(children_strict) <= len(children_default)


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


# ---------------------------------------------------------------------------
# OrderManager NOTIONAL guard
# ---------------------------------------------------------------------------

class _NotionalFakeInner:
    def __init__(self):
        self._next_id = 11000
        self.orders = {}

    def _new_id(self):
        self._next_id += 1
        return self._next_id


class _NotionalFakeClient:
    """Minimal client stub that exposes the symbol-filter surface
    OrderManager needs for the NOTIONAL guard."""
    def __init__(self, min_notional, tick_size='0.01', step_size='0.001'):
        self.client = _NotionalFakeInner()
        self._min_notional = float(min_notional)
        self._tick = tick_size
        self._step = step_size
        self.balance = 1_000_000.0
        self.placed = []

    def get_symbol_filters(self, symbol):
        return {
            'tick_size': self._tick,
            'step_size': self._step,
            'min_notional': str(self._min_notional),
        }

    @staticmethod
    def _floor(value, step):
        s = Decimal(str(step)).normalize()
        return Decimal(str(value)).quantize(s, rounding='ROUND_DOWN')

    def round_price(self, symbol, price):
        return self._floor(price, self._tick)

    def round_quantity(self, symbol, qty):
        return self._floor(qty, self._step)

    def get_account_balance(self, asset):
        return {'free': self.balance, 'locked': 0.0}

    def check_percent_price_filter(self, symbol, side, price):
        return True, "ok"

    def create_limit_order(self, symbol, side, quantity, price):
        oid = self.client._new_id()
        order = {
            'orderId': oid, 'symbol': symbol, 'side': side,
            'price': str(price), 'origQty': str(quantity),
            'executedQty': '0', 'cummulativeQuoteQty': '0', 'status': 'NEW',
        }
        self.client.orders[oid] = order
        self.placed.append((side, float(quantity), float(price)))
        return order

    def cancel_order(self, symbol, order_id):
        order = self.client.orders.get(order_id)
        if order is None:
            raise RuntimeError(f"unknown order {order_id}")
        order['status'] = 'CANCELED'
        return order


def _zec_distribution_strategy_path():
    """Build a config that, before the fix, produced sub-$5 top children for
    the shallowest ladder (Level -1, ~$167 of capital across 7 children)."""
    cfg = {
        "enabled": True,
        "name": "ZEC NOTIONAL Repro",
        "pair": "ZECUSDT",
        "description": "Reproduce the NOTIONAL filter loop",
        "ladder_config": {
            "base_gap": 0.04,
            "gap_max": 0.60,
            "ladders": 8,
            "fibonacci": [1, 1, 2, 3, 5, 8, 13, 21],
            "unit_size_zec": 0.5,
        },
        "capital_allocation": {"max_allocation_percent": 1.0, "reserve_percent": 0.10},
        "risk_management": {"safety_multiplier": 1.5, "stop_loss_percent": -0.50,
                            "take_profit_percent": 0.25},
        "execution": {"auto_rebalance": True, "rebalance_interval_hours": 12,
                      "min_profit_to_close": 0.005},
        "order_placement": {
            "mode": "distribution",
            "child_order_usdt": 25.0,
            "proximity_percent": 0.02,
            "max_open_orders_cap": 180,
            "min_children_per_ladder": 4,
            "max_children_per_ladder": 12,
            "spread_mode": "geometric",
            "child_profit_percent": 0.012,
            "child_sell_mode": "min_of_both",
        },
    }
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    json.dump(cfg, f)
    f.close()
    return f.name


class TestNotionalGuard:
    def test_prime_queue_clamps_children_under_min_notional(self):
        """Regression: priming the queue with a $5 min_notional should
        produce no child smaller than $5 × buffer ($5.50 by default).
        Before the fix, level -1 had a top child of ~$5.06 which the
        exchange rejected with NOTIONAL filter failure."""
        path = _zec_distribution_strategy_path()
        try:
            s = Strategy(path)
            s.update_prices(347.99)  # match the live-mode log price

            client = _NotionalFakeClient(min_notional=5.0)
            portfolio = Portfolio(5000.0)
            om = OrderManager(client, portfolio)
            om.prime_distribution_queue(s)

            queue = om._pending_children[s.config['name']]
            # Every child should be at least min_notional (× safety buffer 1.10)
            for child in queue:
                assert child['usdt_cost'] >= 5.0 * 1.10 - 1e-9, (
                    f"child L{child['parent_level']}.{child['idx']} usdt "
                    f"${child['usdt_cost']:.2f} < floor"
                )
        finally:
            os.unlink(path)

    def test_promote_drops_child_below_min_notional(self):
        """Defense-in-depth: even if a saved-state child slips through with
        sub-min_notional sizing, _promote_pending_children should drop it
        from the queue rather than retry every loop forever."""
        path = _zec_distribution_strategy_path()
        try:
            s = Strategy(path)
            s.update_prices(347.99)

            client = _NotionalFakeClient(min_notional=5.0)
            portfolio = Portfolio(5000.0)
            om = OrderManager(client, portfolio)

            # Manually inject a sub-notional child (mimics resumed state from
            # before the fix). buy_price × qty after rounding ≈ $4.98.
            ladder = s.ladders[0]
            ladder['children_total'] = 1
            ladder['children_closed'] = 0
            ladder['children_placed'] = 0
            bad_child = {
                'idx': 0,
                'parent_level': ladder['level'],
                'buy_price': 332.13,
                'sell_price': 336.11,
                'usdt_cost': 5.06,
                'qty': 0.01524,
                'status': 'pending',
                'parent_ladder': ladder,
            }
            om._pending_children[s.config['name']] = [bad_child]

            # Price right at the child price → should attempt to promote.
            placed = om._promote_pending_children(s, current_price=332.13)

            assert placed == [], "should not have placed a sub-notional order"
            queue = om._pending_children[s.config['name']]
            assert bad_child not in queue, "bad child should be dropped from queue"
            assert client.placed == [], "no order should have been sent to exchange"
        finally:
            os.unlink(path)

    def test_guard_allows_valid_child_through(self):
        """Sanity: a properly-sized child (well above min_notional) places
        normally. Verifies the guard isn't over-eager."""
        path = _zec_distribution_strategy_path()
        try:
            s = Strategy(path)
            s.update_prices(347.99)

            client = _NotionalFakeClient(min_notional=5.0)
            portfolio = Portfolio(5000.0)
            om = OrderManager(client, portfolio)
            om.prime_distribution_queue(s)

            placed = om._promote_pending_children(s, current_price=332.13)
            # At least the top child(ren) should have been placed.
            assert len(placed) >= 1
            assert all(p['side'] == 'BUY' for p in placed)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Stale-BUY auto-cancel
# ---------------------------------------------------------------------------

def _stale_buy_strategy_path(extra_placement=None):
    """Distribution strategy with stale-BUY auto-cancel enabled by default."""
    placement = {
        "mode": "distribution",
        "child_order_usdt": 25.0,
        "proximity_percent": 0.02,
        "max_open_orders_cap": 180,
        "min_children_per_ladder": 1,
        "max_children_per_ladder": 12,
        "spread_mode": "geometric",
        "child_profit_percent": 0.012,
        "child_sell_mode": "min_of_both",
        "stale_buy_max_age_seconds": 3600,
        "stale_buy_distance_threshold": 0.04,
        "stale_buy_check_interval_seconds": 0,  # disable throttle for tests
    }
    if extra_placement:
        placement.update(extra_placement)
    cfg = {
        "enabled": True,
        "name": "ZEC Stale-BUY Test",
        "pair": "ZECUSDT",
        "description": "stale-BUY recycle test",
        "ladder_config": {
            "base_gap": 0.04,
            "gap_max": 0.60,
            "ladders": 8,
            "fibonacci": [1, 1, 2, 3, 5, 8, 13, 21],
            "size_mode": "fibonacci",
            "unit_size_zec": 0.5,
        },
        "capital_allocation": {"max_allocation_percent": 1.0, "reserve_percent": 0.10},
        "risk_management": {"safety_multiplier": 1.5, "stop_loss_percent": -0.50,
                            "take_profit_percent": 0.25},
        "execution": {"auto_rebalance": True, "rebalance_interval_hours": 12,
                      "min_profit_to_close": 0.005},
        "order_placement": placement,
    }
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    json.dump(cfg, f)
    f.close()
    return f.name


def _seed_stale_buy(om, strategy, *, buy_price, current_price, age_seconds):
    """Inject a fake-but-valid BUY into active_orders with the given age.

    Returns (order_id, child_dict) so tests can assert on either.
    """
    import time as _time
    ladder = strategy.ladders[0]
    ladder.setdefault('children_total', 1)
    ladder.setdefault('children_closed', 0)
    ladder['children_placed'] = ladder.get('children_placed', 0) + 1
    child = {
        'idx': 0,
        'parent_level': ladder['level'],
        'buy_price': buy_price,
        'sell_price': buy_price * 1.01,
        'usdt_cost': buy_price * 0.5,
        'qty': 0.5,
        'status': 'placed',
        'parent_ladder': ladder,
    }
    order = om.client.create_limit_order(
        strategy.config['pair'], 'BUY', child['qty'], buy_price
    )
    om.active_orders[order['orderId']] = {
        'strategy': strategy.config['name'],
        'level': child['parent_level'],
        'type': 'BUY',
        'order': order,
        'ladder': ladder,
        'child': child,
        'placed_at': _time.time() - age_seconds,
    }
    om._pending_children.setdefault(strategy.config['name'], [])
    return order['orderId'], child


class TestStaleBuyRecycle:
    def test_recycles_old_far_buy_back_to_pending(self):
        """A BUY that is both old AND far below market is cancelled and the
        child returns to the pending queue for later re-promotion."""
        path = _stale_buy_strategy_path()
        try:
            s = Strategy(path)
            s.update_prices(380.0)
            client = _NotionalFakeClient(min_notional=5.0)
            om = OrderManager(client, Portfolio(5000.0))
            oid, child = _seed_stale_buy(
                om, s, buy_price=320.0, current_price=380.0, age_seconds=7200
            )

            cancelled = om._scan_stale_buys(s, current_price=380.0)

            assert cancelled == 1
            assert oid not in om.active_orders, "stale BUY should be removed"
            assert client.client.orders[oid]['status'] == 'CANCELED'
            queue = om._pending_children[s.config['name']]
            assert child in queue, "child should re-enter pending queue"
            assert child['status'] == 'pending'
        finally:
            os.unlink(path)

    def test_close_buy_is_left_alone_even_if_old(self):
        """A BUY within the distance threshold is preserved — the bot still
        wants it to fill on the next dip."""
        path = _stale_buy_strategy_path()
        try:
            s = Strategy(path)
            s.update_prices(380.0)
            client = _NotionalFakeClient(min_notional=5.0)
            om = OrderManager(client, Portfolio(5000.0))
            # 2% below market (< 4% threshold), but very old.
            oid, _ = _seed_stale_buy(
                om, s, buy_price=372.4, current_price=380.0, age_seconds=86400
            )

            cancelled = om._scan_stale_buys(s, current_price=380.0)

            assert cancelled == 0
            assert oid in om.active_orders, "near-market BUY must not be cancelled"

        finally:
            os.unlink(path)

    def test_fresh_far_buy_is_left_alone(self):
        """A deep BUY that's still young keeps its slot — it might fill soon."""
        path = _stale_buy_strategy_path()
        try:
            s = Strategy(path)
            s.update_prices(380.0)
            client = _NotionalFakeClient(min_notional=5.0)
            om = OrderManager(client, Portfolio(5000.0))
            oid, _ = _seed_stale_buy(
                om, s, buy_price=300.0, current_price=380.0, age_seconds=60
            )

            cancelled = om._scan_stale_buys(s, current_price=380.0)

            assert cancelled == 0
            assert oid in om.active_orders
        finally:
            os.unlink(path)

    def test_disabled_when_max_age_zero(self):
        """stale_buy_max_age_seconds=0 leaves the legacy behaviour intact."""
        path = _stale_buy_strategy_path(extra_placement={
            'stale_buy_max_age_seconds': 0,
        })
        try:
            s = Strategy(path)
            s.update_prices(380.0)
            client = _NotionalFakeClient(min_notional=5.0)
            om = OrderManager(client, Portfolio(5000.0))
            oid, _ = _seed_stale_buy(
                om, s, buy_price=200.0, current_price=380.0, age_seconds=86400 * 30
            )

            cancelled = om._scan_stale_buys(s, current_price=380.0)

            assert cancelled == 0
            assert oid in om.active_orders, "disabled scan must not cancel anything"
        finally:
            os.unlink(path)

    def test_throttle_blocks_repeat_scans(self):
        """Once the scan runs, the throttle prevents a second scan within
        the interval (so we don't hammer the exchange every loop tick)."""
        path = _stale_buy_strategy_path(extra_placement={
            'stale_buy_check_interval_seconds': 600,
        })
        try:
            s = Strategy(path)
            s.update_prices(380.0)
            client = _NotionalFakeClient(min_notional=5.0)
            om = OrderManager(client, Portfolio(5000.0))
            _seed_stale_buy(
                om, s, buy_price=300.0, current_price=380.0, age_seconds=7200
            )

            first = om._scan_stale_buys(s, current_price=380.0)
            # Add another stale BUY and immediately scan again — throttle
            # should keep the second scan from running.
            _seed_stale_buy(
                om, s, buy_price=290.0, current_price=380.0, age_seconds=7200
            )
            second = om._scan_stale_buys(s, current_price=380.0)

            assert first == 1
            assert second == 0, "second scan within throttle window must be a no-op"
        finally:
            os.unlink(path)
