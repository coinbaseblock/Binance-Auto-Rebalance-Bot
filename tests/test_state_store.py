"""
Tests for src/state_store.StateStore: round-trip save+load of Portfolio,
Strategy ladders, and OrderManager (active_orders, sequential_state,
pending_children).
"""
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from src.portfolio import Portfolio
from src.order_manager import OrderManager
from src.state_store import StateStore
from src.strategy import Strategy


def _write_config(config):
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    json.dump(config, f)
    f.close()
    return f.name


@pytest.fixture
def distribution_config():
    return {
        "enabled": True,
        "name": "ZEC Distribution Test",
        "pair": "ZECUSDT",
        "description": "Test",
        "ladder_config": {
            "base_gap": 0.025,
            "ladders": 4,
            "fibonacci": [1, 1, 2, 3],
            "unit_size_zec": 0.5,
        },
        "capital_allocation": {"max_allocation_percent": 1.0, "reserve_percent": 0.0},
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
            "max_children_per_ladder": 8,
        },
    }


@pytest.fixture
def strategy(distribution_config):
    path = _write_config(distribution_config)
    try:
        s = Strategy(path)
        s.update_prices(354.23)
        yield s
    finally:
        os.unlink(path)


class _FakeBinanceOrderClient:
    """Stub for binance_client.client (the inner SDK client)."""
    def __init__(self, fills_by_id=None):
        self._fills = fills_by_id or {}

    def get_order(self, symbol, orderId):
        if orderId in self._fills:
            return self._fills[orderId]
        return {"orderId": orderId, "symbol": symbol, "status": "NEW"}


class FakeBinanceClient:
    """Minimal stand-in for src.binance_client.BinanceClient."""
    def __init__(self, fills_by_id=None):
        self.client = _FakeBinanceOrderClient(fills_by_id)


def _populate_distribution_state(strategy, om, portfolio):
    """Simulate a session that has placed some children and filled one BUY."""
    om.place_distribution_orders(strategy, 354.23) if False else None  # not called here

    # Manually build pending queue + place a couple of "active" orders
    all_children = strategy.calculate_all_child_orders()
    queue = []
    for ladder in strategy.get_pending_ladders():
        ladder_children = all_children.get(ladder['level'], [])
        ladder['children_total'] = len(ladder_children)
        ladder['children_placed'] = 0
        ladder['children_closed'] = 0
        for c in ladder_children:
            c['parent_ladder'] = ladder
        queue.extend(ladder_children)
    queue.sort(key=lambda c: c['buy_price'], reverse=True)
    om._pending_children[strategy.config['name']] = queue

    # Pretend two children were placed
    placed_a = queue.pop(0)
    placed_b = queue.pop(0)
    placed_a['status'] = 'placed'
    placed_b['status'] = 'placed'

    om.active_orders[1001] = {
        'strategy': strategy.config['name'],
        'level': placed_a['parent_level'],
        'type': 'BUY',
        'order': {'orderId': 1001, 'symbol': 'ZECUSDT', 'price': '345.10', 'origQty': '0.015'},
        'ladder': placed_a['parent_ladder'],
        'child': placed_a,
    }
    om.active_orders[1002] = {
        'strategy': strategy.config['name'],
        'level': placed_b['parent_level'],
        'type': 'BUY',
        'order': {'orderId': 1002, 'symbol': 'ZECUSDT', 'price': '344.84', 'origQty': '0.015'},
        'ladder': placed_b['parent_ladder'],
        'child': placed_b,
    }
    placed_a['parent_ladder']['children_placed'] += 1
    placed_b['parent_ladder']['children_placed'] += 1

    # Pretend a third child fully cycled (BUY filled, SELL filled, profit recorded)
    cycled = queue.pop(0)
    cycled['status'] = 'closed'
    cycled['parent_ladder']['children_placed'] += 1
    cycled['parent_ladder']['children_closed'] += 1
    portfolio.add_position(
        strategy_name=strategy.config['name'],
        ladder_level=(cycled['parent_level'], cycled['idx']),
        buy_price=cycled['buy_price'],
        quantity=cycled['qty'],
        cost=cycled['usdt_cost'],
    )
    portfolio.close_position(
        strategy_name=strategy.config['name'],
        ladder_level=(cycled['parent_level'], cycled['idx']),
        sell_price=cycled['sell_price'],
        quantity=cycled['qty'],
    )


class TestStateRoundTrip:
    def test_save_then_load_restores_portfolio_pnl(self, strategy, tmp_path):
        portfolio = Portfolio(5000.0)
        client = FakeBinanceClient()
        om = OrderManager(client, portfolio)
        _populate_distribution_state(strategy, om, portfolio)

        original_realized = portfolio.get_realized_pnl()
        original_trades = len(portfolio.trades_history)
        assert original_trades == 1
        assert original_realized != 0

        store = StateStore(tmp_path / "state.json")
        store.save(portfolio, om, [strategy])

        # Fresh objects, then apply
        portfolio2 = Portfolio(0.0)
        om2 = OrderManager(client, portfolio2)
        strat2 = Strategy(_write_config(strategy.config))
        strat2.update_prices(354.23)

        data = store.load()
        store.apply(data, portfolio2, om2, [strat2])

        assert portfolio2.get_realized_pnl() == pytest.approx(original_realized, rel=1e-9)
        assert len(portfolio2.trades_history) == original_trades
        assert portfolio2.initial_capital == 5000.0

    def test_active_orders_relink_to_ladder_and_child(self, strategy, tmp_path):
        portfolio = Portfolio(5000.0)
        client = FakeBinanceClient()
        om = OrderManager(client, portfolio)
        _populate_distribution_state(strategy, om, portfolio)

        store = StateStore(tmp_path / "state.json")
        store.save(portfolio, om, [strategy])

        portfolio2 = Portfolio(0.0)
        om2 = OrderManager(client, portfolio2)
        strat2 = Strategy(_write_config(strategy.config))
        strat2.update_prices(354.23)

        store.apply(store.load(), portfolio2, om2, [strat2])

        assert set(om2.active_orders.keys()) == {1001, 1002}
        for od in om2.active_orders.values():
            # Ladder reference must point to a ladder in the new strategy
            assert od["ladder"] in strat2.ladders
            # Child must carry a parent_ladder back-reference to the same ladder
            assert od["child"] is not None
            assert od["child"]["parent_ladder"] is od["ladder"]
            assert od["child"]["parent_level"] == od["ladder"]["level"]

    def test_pending_children_relink_parent_ladder(self, strategy, tmp_path):
        portfolio = Portfolio(5000.0)
        client = FakeBinanceClient()
        om = OrderManager(client, portfolio)
        _populate_distribution_state(strategy, om, portfolio)

        original_pending = len(om._pending_children[strategy.config['name']])
        assert original_pending > 0

        store = StateStore(tmp_path / "state.json")
        store.save(portfolio, om, [strategy])

        portfolio2 = Portfolio(0.0)
        om2 = OrderManager(client, portfolio2)
        strat2 = Strategy(_write_config(strategy.config))
        strat2.update_prices(354.23)
        store.apply(store.load(), portfolio2, om2, [strat2])

        restored = om2._pending_children.get(strategy.config['name'], [])
        assert len(restored) == original_pending
        for c in restored:
            assert c["parent_ladder"] in strat2.ladders
            assert c["parent_ladder"]["level"] == c["parent_level"]

    def test_ladder_children_counts_preserved(self, strategy, tmp_path):
        portfolio = Portfolio(5000.0)
        client = FakeBinanceClient()
        om = OrderManager(client, portfolio)
        _populate_distribution_state(strategy, om, portfolio)

        snapshot = [
            (l['level'], l.get('children_placed', 0), l.get('children_closed', 0),
             l.get('children_total', 0), l['status'])
            for l in strategy.ladders
        ]

        store = StateStore(tmp_path / "state.json")
        store.save(portfolio, om, [strategy])

        portfolio2 = Portfolio(0.0)
        om2 = OrderManager(client, portfolio2)
        strat2 = Strategy(_write_config(strategy.config))
        strat2.update_prices(354.23)
        store.apply(store.load(), portfolio2, om2, [strat2])

        restored = [
            (l['level'], l.get('children_placed', 0), l.get('children_closed', 0),
             l.get('children_total', 0), l['status'])
            for l in strat2.ladders
        ]
        assert restored == snapshot

    def test_atomic_save_creates_backup(self, strategy, tmp_path):
        portfolio = Portfolio(5000.0)
        client = FakeBinanceClient()
        om = OrderManager(client, portfolio)
        _populate_distribution_state(strategy, om, portfolio)

        store = StateStore(tmp_path / "state.json")
        store.save(portfolio, om, [strategy])
        store.save(portfolio, om, [strategy])  # second save should produce .bak

        assert (tmp_path / "state.json").exists()
        assert (tmp_path / "state.json.bak").exists()

    def test_reset_moves_existing_file_aside(self, strategy, tmp_path):
        portfolio = Portfolio(5000.0)
        client = FakeBinanceClient()
        om = OrderManager(client, portfolio)
        _populate_distribution_state(strategy, om, portfolio)

        store = StateStore(tmp_path / "state.json")
        store.save(portfolio, om, [strategy])
        backup = store.reset()
        assert backup is not None
        assert backup.exists()
        assert not (tmp_path / "state.json").exists()


class TestReconcileWithExchange:
    def test_filled_order_kept_for_next_check(self, strategy):
        portfolio = Portfolio(5000.0)
        # Seed exchange to report orderId 1001 as FILLED
        client = FakeBinanceClient(fills_by_id={
            1001: {
                "orderId": 1001, "symbol": "ZECUSDT", "status": "FILLED",
                "price": "345.10", "executedQty": "0.015",
                "cummulativeQuoteQty": "5.18",
            },
        })
        om = OrderManager(client, portfolio)
        _populate_distribution_state(strategy, om, portfolio)

        kept, missed, dropped = om.reconcile_with_exchange()
        assert missed == 1
        assert 1001 in om.active_orders  # left for check_filled_orders to pick up

    def test_canceled_order_dropped_and_child_requeued(self, strategy):
        portfolio = Portfolio(5000.0)
        client = FakeBinanceClient(fills_by_id={
            1001: {"orderId": 1001, "symbol": "ZECUSDT", "status": "CANCELED"},
        })
        om = OrderManager(client, portfolio)
        _populate_distribution_state(strategy, om, portfolio)

        before_pending = len(om._pending_children[strategy.config['name']])
        kept, missed, dropped = om.reconcile_with_exchange()

        assert dropped == 1
        assert 1001 not in om.active_orders
        # The dropped child should have been pushed back onto the pending queue
        assert len(om._pending_children[strategy.config['name']]) == before_pending + 1
