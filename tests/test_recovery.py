"""
Tests for the recovery (merge-and-wait-for-bounce) flow in OrderManager.

The feature has two coexisting behaviours:
  1. Quick small profits (legacy): a normal child SELL goes up at
     child_profit_percent over its own buy.
  2. Recovery: when price has already dropped well past a child's buy
     (drawdown_threshold), or an existing SELL has drifted far above the
     market (stale_sell_threshold), the child joins a per-strategy "recovery
     lot" and gets sold as part of one merged SELL at avg_cost * (1 + target).

These tests use a fake Binance client so we can assert the order_manager's
decisions without touching the real exchange.
"""
import json
import os
import tempfile

import pytest

from src.order_manager import OrderManager
from src.portfolio import Portfolio
from src.state_store import StateStore
from src.strategy import Strategy


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeInner:
    """Stub for binance_client.client (the python-binance SDK object)."""
    def __init__(self):
        self._next_id = 9000
        self.orders = {}            # order_id -> status dict (NEW / FILLED / etc.)
        self.placed = []            # log of (side, qty, price, order_id)
        self.cancelled = []         # log of cancelled order ids

    def _new_id(self):
        self._next_id += 1
        return self._next_id

    def get_order(self, symbol, orderId):
        if orderId in self.orders:
            return self.orders[orderId]
        return {"orderId": orderId, "symbol": symbol, "status": "NEW"}

    def cancel_order(self, symbol, orderId):
        self.cancelled.append(orderId)
        if orderId in self.orders:
            self.orders[orderId]["status"] = "CANCELED"
        return {"orderId": orderId, "status": "CANCELED"}


class FakeClient:
    """Stub for src.binance_client.BinanceClient with the surface
    OrderManager actually calls."""
    def __init__(self):
        self.client = _FakeInner()
        self.balance = 1_000_000.0  # plenty so balance checks always pass

    def get_account_balance(self, asset):
        return {'free': self.balance, 'locked': 0.0}

    def check_percent_price_filter(self, symbol, side, price):
        return True, "ok"

    def create_limit_order(self, symbol, side, quantity, price):
        oid = self.client._new_id()
        order = {
            "orderId": oid,
            "symbol": symbol,
            "side": side,
            "price": str(price),
            "origQty": str(quantity),
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
            "status": "NEW",
        }
        self.client.orders[oid] = order
        self.client.placed.append((side, float(quantity), float(price), oid))
        return order

    def cancel_order(self, symbol, order_id):
        return self.client.cancel_order(symbol=symbol, orderId=order_id)

    def get_open_orders(self, symbol=None):
        return [o for o in self.client.orders.values()
                if o.get("status") in ("NEW", "PARTIALLY_FILLED")]

    def get_current_price(self, symbol):
        return 100.0  # tests override with explicit current_price kwargs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _strategy_with_recovery(recovery_overrides=None):
    cfg = {
        "enabled": True,
        "name": "TEST Recovery",
        "pair": "ZECUSDT",
        "description": "Recovery test",
        "ladder_config": {
            "base_gap": 0.04,
            "ladders": 3,
            "fibonacci": [1, 1, 2],
            "unit_size_zec": 0.5,
        },
        "capital_allocation": {"max_allocation_percent": 1.0, "reserve_percent": 0.0},
        "risk_management": {"safety_multiplier": 1.0, "stop_loss_percent": -0.3,
                            "take_profit_percent": 0.2},
        "execution": {"auto_rebalance": True, "rebalance_interval_hours": 24,
                      "min_profit_to_close": 0.005},
        "order_placement": {
            "mode": "distribution",
            "child_order_usdt": 25.0,
            "proximity_percent": 0.02,
            "max_open_orders_cap": 100,
            "min_children_per_ladder": 2,
            "max_children_per_ladder": 6,
            "spread_mode": "geometric",
            "child_profit_percent": 0.012,
            "child_sell_mode": "min_of_both",
        },
        "recovery": {
            "enabled": True,
            "drawdown_threshold": 0.03,
            "stale_sell_threshold": 0.05,
            "stale_check_interval_seconds": 0,  # no throttle in tests
            "profit_target": 0.005,
            "min_merge_count": 2,
        },
    }
    if recovery_overrides:
        cfg["recovery"].update(recovery_overrides)
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    json.dump(cfg, f)
    f.close()
    s = Strategy(f.name)
    s.update_prices(100.0)
    return s, f.name


@pytest.fixture
def strategy_and_om():
    s, path = _strategy_with_recovery()
    portfolio = Portfolio(5000.0)
    client = FakeClient()
    om = OrderManager(client, portfolio)
    # Prime the distribution queue so child snapshots have parent_ladder back-refs
    om.prime_distribution_queue(s)
    yield s, om, portfolio, client
    os.unlink(path)


def _grab_child(strategy, om, level=-1, idx=0):
    """Return a child dict for the given parent level, with parent_ladder set."""
    queue = om._pending_children[strategy.config['name']]
    for c in queue:
        if c['parent_level'] == level and c['idx'] == idx:
            return c
    raise AssertionError(f"child L{level}.{idx} not found in pending queue")


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

class TestEligibility:
    def test_at_buy_price_not_eligible(self, strategy_and_om):
        strategy, om, _, _ = strategy_and_om
        rec_cfg = strategy.get_recovery_config()
        child = {'buy_price': 100.0}
        # Price exactly at buy → drawdown 0 → not eligible.
        assert om._eligible_for_recovery(child, 100.0, rec_cfg) is False

    def test_below_threshold_not_eligible(self, strategy_and_om):
        strategy, om, _, _ = strategy_and_om
        rec_cfg = strategy.get_recovery_config()
        # 1% drawdown vs 3% threshold.
        assert om._eligible_for_recovery({'buy_price': 100.0}, 99.0, rec_cfg) is False

    def test_above_threshold_eligible(self, strategy_and_om):
        strategy, om, _, _ = strategy_and_om
        rec_cfg = strategy.get_recovery_config()
        # 5% drawdown vs 3% threshold.
        assert om._eligible_for_recovery({'buy_price': 100.0}, 95.0, rec_cfg) is True


# ---------------------------------------------------------------------------
# Routing (normal vs recovery)
# ---------------------------------------------------------------------------

class TestRouting:
    def test_small_drawdown_uses_normal_child_sell(self, strategy_and_om):
        strategy, om, _, client = strategy_and_om
        child = _grab_child(strategy, om, level=-1, idx=0)
        ladder = child['parent_ladder']
        # current_price right at the buy → drawdown 0 → normal SELL path.
        order = om._place_child_sell(
            strategy, child, ladder, child['qty'],
            current_price=child['buy_price']
        )
        assert order is not None
        # Exactly one SELL on the book, of type 'SELL' (legacy quick-profit).
        sells = [od for od in om.active_orders.values() if od['type'] == 'SELL']
        assert len(sells) == 1
        # And no recovery state created.
        assert strategy.config['name'] not in om._recovery_lots \
            or not om._recovery_lots[strategy.config['name']]['children']

    def test_large_drawdown_pools_into_recovery_no_merge_yet(self, strategy_and_om):
        strategy, om, _, client = strategy_and_om
        child = _grab_child(strategy, om, level=-1, idx=0)
        ladder = child['parent_ladder']
        # Force a big drawdown — current_price 10% below child buy.
        deep_price = child['buy_price'] * 0.90
        om._place_child_sell(strategy, child, ladder, child['qty'],
                             current_price=deep_price)
        lot = om._recovery_lots[strategy.config['name']]
        assert len(lot['children']) == 1
        # min_merge_count is 2 — single-child lot uses an individual recovery SELL.
        assert lot.get('merged_sell_order_id') is None
        recovery_sells = [od for od in om.active_orders.values() if od['type'] == 'SELL_RECOVERY']
        assert len(recovery_sells) == 1
        # Target ≈ buy * (1 + 0.005)
        rs = recovery_sells[0]
        target = float(rs['order']['price'])
        assert abs(target - child['buy_price'] * 1.005) < 0.05

    def test_two_drawdown_fills_create_merged_sell(self, strategy_and_om):
        strategy, om, _, client = strategy_and_om
        child_a = _grab_child(strategy, om, level=-1, idx=0)
        child_b = _grab_child(strategy, om, level=-1, idx=1)
        ladder = child_a['parent_ladder']

        # Both fills happen with price well below their buys → both eligible.
        deep_price = min(child_a['buy_price'], child_b['buy_price']) * 0.85
        om._place_child_sell(strategy, child_a, ladder, child_a['qty'],
                             current_price=deep_price)
        om._place_child_sell(strategy, child_b, ladder, child_b['qty'],
                             current_price=deep_price)

        lot = om._recovery_lots[strategy.config['name']]
        assert len(lot['children']) == 2
        # Merged SELL placed; the prior individual recovery SELL was cancelled.
        assert lot['merged_sell_order_id'] is not None
        merged = om.active_orders[lot['merged_sell_order_id']]
        assert merged['type'] == 'SELL_MERGED'
        # No leftover individual recovery SELLs for these children.
        leftover = [od for od in om.active_orders.values() if od['type'] == 'SELL_RECOVERY']
        assert leftover == []
        # Merged qty = sum of pooled child qty.
        expected_qty = child_a['qty'] + child_b['qty']
        assert abs(float(merged['order']['origQty']) - expected_qty) < 1e-9
        # Merged price ≈ avg cost * (1 + 0.005)
        total_cost = (child_a['qty'] * child_a['buy_price']
                      + child_b['qty'] * child_b['buy_price'])
        avg = total_cost / expected_qty
        target = float(merged['order']['price'])
        assert abs(target - avg * 1.005) < 0.05


# ---------------------------------------------------------------------------
# Stale SELL roll-up
# ---------------------------------------------------------------------------

class TestStaleSellRollup:
    def test_stale_sell_rolled_into_recovery(self, strategy_and_om):
        strategy, om, _, client = strategy_and_om
        # Place two normal child SELLs (simulating earlier sideway fills).
        child_a = _grab_child(strategy, om, level=-1, idx=0)
        child_b = _grab_child(strategy, om, level=-1, idx=1)
        ladder = child_a['parent_ladder']
        # Use the normal path explicitly (drawdown=0).
        om._place_normal_child_sell(strategy, child_a, ladder, child_a['qty'])
        om._place_normal_child_sell(strategy, child_b, ladder, child_b['qty'])

        # Market crashes ~10% below the SELLs — they're now stale.
        far_below = min(float(child_a['sell_price']),
                        float(child_b['sell_price'])) * 0.90
        rolled = om.check_stale_sells(strategy, far_below)
        assert rolled == 2
        # The two normal SELLs should be cancelled and the recovery lot
        # should now hold both children with a merged SELL placed.
        normal_sells = [od for od in om.active_orders.values() if od['type'] == 'SELL']
        assert normal_sells == []
        lot = om._recovery_lots[strategy.config['name']]
        assert len(lot['children']) == 2
        assert lot['merged_sell_order_id'] is not None

    def test_stale_check_throttled(self, strategy_and_om):
        strategy, _, _, _ = _setup_throttled_strategy()
        s, om, _, _ = strategy
        child = _grab_child(s, om, level=-1, idx=0)
        ladder = child['parent_ladder']
        om._place_normal_child_sell(s, child, ladder, child['qty'])
        far_below = float(child['sell_price']) * 0.90
        # First call rolls.
        first = om.check_stale_sells(s, far_below)
        # Second call — throttled, should do nothing.
        second = om.check_stale_sells(s, far_below)
        assert first == 1
        assert second == 0


def _setup_throttled_strategy():
    """Helper: like the fixture but with stale_check_interval_seconds=10000
    so the throttle is observable. We can't reuse the parametrized fixture
    here because we need a different recovery config."""
    s, path = _strategy_with_recovery({"stale_check_interval_seconds": 10_000})
    portfolio = Portfolio(5000.0)
    client = FakeClient()
    om = OrderManager(client, portfolio)
    om.prime_distribution_queue(s)
    return (s, om, portfolio, client), path, None, None


# ---------------------------------------------------------------------------
# Merged fill distribution
# ---------------------------------------------------------------------------

class TestMergedFillDistribution:
    def test_merged_fill_closes_all_children_at_merged_price(self, strategy_and_om):
        strategy, om, portfolio, client = strategy_and_om
        child_a = _grab_child(strategy, om, level=-1, idx=0)
        child_b = _grab_child(strategy, om, level=-1, idx=1)
        ladder = child_a['parent_ladder']
        ladder['children_total'] = 2
        ladder['children_closed'] = 0

        # Pretend both children's BUYs already filled — record positions in portfolio
        # so close_position has something to close.
        portfolio.add_position(strategy.config['name'],
                               (child_a['parent_level'], child_a['idx']),
                               child_a['buy_price'], child_a['qty'],
                               child_a['qty'] * child_a['buy_price'])
        portfolio.add_position(strategy.config['name'],
                               (child_b['parent_level'], child_b['idx']),
                               child_b['buy_price'], child_b['qty'],
                               child_b['qty'] * child_b['buy_price'])

        deep_price = min(child_a['buy_price'], child_b['buy_price']) * 0.85
        om._place_child_sell(strategy, child_a, ladder, child_a['qty'],
                             current_price=deep_price)
        om._place_child_sell(strategy, child_b, ladder, child_b['qty'],
                             current_price=deep_price)
        lot = om._recovery_lots[strategy.config['name']]
        merged_id = lot['merged_sell_order_id']

        # Simulate Binance reporting the merged SELL filled.
        merged_price = lot['merged_sell_price']
        merged_qty = lot['merged_sell_qty']
        client.client.orders[merged_id].update({
            "status": "FILLED",
            "price": str(merged_price),
            "executedQty": str(merged_qty),
            "cummulativeQuoteQty": str(merged_price * merged_qty),
        })

        filled = om.check_filled_orders()
        # One filled order detected and the lot is cleared.
        assert any(od['type'] == 'SELL_MERGED' for od in filled)
        assert strategy.config['name'] not in om._recovery_lots
        # Both per-child positions are closed in portfolio.
        positions = portfolio.positions[strategy.config['name']]['ladders']
        assert all(p['status'] == 'closed' for p in positions)
        # Realized P&L is positive (we sold above weighted average cost).
        assert portfolio.get_realized_pnl() > 0
        # And the parent ladder is closed because both its tracked children closed.
        assert ladder['status'] == 'closed'


# ---------------------------------------------------------------------------
# State persistence round-trip
# ---------------------------------------------------------------------------

class TestRecoveryPersistence:
    def test_recovery_lot_round_trips_through_state_store(self, strategy_and_om, tmp_path):
        strategy, om, portfolio, client = strategy_and_om
        child_a = _grab_child(strategy, om, level=-1, idx=0)
        child_b = _grab_child(strategy, om, level=-1, idx=1)
        ladder = child_a['parent_ladder']
        deep_price = min(child_a['buy_price'], child_b['buy_price']) * 0.85
        om._place_child_sell(strategy, child_a, ladder, child_a['qty'],
                             current_price=deep_price)
        om._place_child_sell(strategy, child_b, ladder, child_b['qty'],
                             current_price=deep_price)
        lot_before = om._recovery_lots[strategy.config['name']]
        merged_id_before = lot_before['merged_sell_order_id']
        target_before = lot_before['merged_sell_price']

        store = StateStore(tmp_path / "state.json")
        store.save(portfolio, om, [strategy])

        # Build a fresh manager and load.
        portfolio2 = Portfolio(5000.0)
        client2 = FakeClient()
        # Mirror the merged order on the new client so reconcile-style checks work.
        om2 = OrderManager(client2, portfolio2)

        data = store.load()
        store.apply(data, portfolio2, om2, [strategy])

        lot_after = om2._recovery_lots[strategy.config['name']]
        assert len(lot_after['children']) == 2
        assert lot_after['merged_sell_order_id'] == merged_id_before
        assert abs(lot_after['merged_sell_price'] - target_before) < 1e-9
        # The active SELL_MERGED order is also restored with its child snapshots.
        merged_order = om2.active_orders[merged_id_before]
        assert merged_order['type'] == 'SELL_MERGED'
        assert len(merged_order['recovery_children']) == 2
        # Each restored child snapshot has a parent_ladder back-link.
        for c in merged_order['recovery_children']:
            assert c['parent_ladder'] is not None
