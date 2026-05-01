"""
Tests for Strategy module
"""
import pytest
import json
import tempfile
import os

from src.strategy import Strategy


@pytest.fixture
def sample_config():
    """Create a sample strategy config - realistic 10 ladders with Fibonacci

    Uses real production values:
    - base_gap: 0.8% (0.008) - realistic gap between ladders
    - fibonacci: [1,1,2,3,5,8,13,21,34,55] - standard Fibonacci sequence
    - Compound formula: buy_multiplier = ∏(1 - gap_i)
    """
    return {
        "enabled": True,
        "name": "BTC Conservative",
        "pair": "BTCUSDT",
        "description": "Conservative BTC strategy with 10 Fibonacci ladders",
        "ladder_config": {
            "base_gap": 0.008,  # 0.8% base gap (realistic)
            "ladders": 10,
            "fibonacci": [1, 1, 2, 3, 5, 8, 13, 21, 34, 55],
            "unit_size_btc": 0.001
        },
        "capital_allocation": {
            "max_allocation_percent": 0.25,
            "reserve_percent": 0.10
        },
        "risk_management": {
            "safety_multiplier": 1.5,
            "stop_loss_percent": -0.30,
            "take_profit_percent": 0.25
        },
        "execution": {
            "auto_rebalance": True,
            "rebalance_interval_hours": 24,
            "min_profit_to_close": 0.005
        }
    }


@pytest.fixture
def strategy(sample_config):
    """Create a Strategy instance with temp config file"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(sample_config, f)
        temp_path = f.name

    try:
        strategy = Strategy(temp_path)
        yield strategy
    finally:
        os.unlink(temp_path)


class TestStrategy:
    def test_load_config(self, strategy, sample_config):
        """Test that config is loaded correctly"""
        assert strategy.config['name'] == sample_config['name']
        assert strategy.config['pair'] == sample_config['pair']

    def test_ladder_calculation(self, strategy):
        """Test that 10 ladders are calculated correctly with compound Fibonacci gaps

        With base_gap=0.8%, Fibonacci [1,1,2,3,5,8,13,21,34,55]:
        Compound formula: buy_multiplier = ∏(1 - base_gap * fib_i)

        Level  Fib  Gap      Multiplier    Cumulative Drop
        -1     1    0.8%     0.992000      0.8%
        -2     1    0.8%     0.984064      1.6%
        -3     2    1.6%     0.968319      3.2%
        -4     3    2.4%     0.945079      5.5%
        -5     5    4.0%     0.907276      9.3%
        -6     8    6.4%     0.849210      15.1%
        -7     13   10.4%    0.760892      23.9%
        -8     21   16.8%    0.633062      36.7%
        -9     34   27.2%    0.460869      53.9%
        -10    55   44.0%    0.258087      74.2%
        """
        assert len(strategy.ladders) == 10

        # Check first ladder
        assert strategy.ladders[0]['level'] == -1
        assert strategy.ladders[0]['fibonacci'] == 1
        assert strategy.ladders[0]['gap_percent'] == 0.008  # 0.8%

        # Compute expected compound multipliers
        base_gap = 0.008
        fibonacci = [1, 1, 2, 3, 5, 8, 13, 21, 34, 55]
        expected_multiplier = 1.0
        for i, fib in enumerate(fibonacci):
            gap = base_gap * fib
            expected_multiplier *= (1 - gap)
            expected_cumulative = 1 - expected_multiplier
            assert abs(strategy.ladders[i]['cumulative_gap_percent'] - expected_cumulative) < 0.0001
            assert strategy.ladders[i]['buy_price_multiplier'] > 0  # Never negative

        # Check all ladder levels are negative (buy below market)
        for i, ladder in enumerate(strategy.ladders):
            assert ladder['level'] == -(i + 1)

    def test_martingale_units(self, strategy):
        """Test Martingale unit doubling for 10 ladders"""
        expected_units = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]
        for i, ladder in enumerate(strategy.ladders):
            assert ladder['units'] == expected_units[i]

        # Total units should be 2^10 - 1 = 1023
        total_units = sum(ladder['units'] for ladder in strategy.ladders)
        assert total_units == 1023

    def test_update_prices(self, strategy):
        """Test price updates"""
        current_price = 50000

        strategy.update_prices(current_price)

        # Check that buy prices are below current price
        for ladder in strategy.ladders:
            assert ladder['buy_price'] < current_price
            assert ladder['usdt_cost'] > 0

    def test_get_pending_ladders(self, strategy):
        """Test pending ladders filter for 10 ladders"""
        pending = strategy.get_pending_ladders()
        assert len(pending) == 10  # All should be pending initially

        # Mark one as active
        strategy.ladders[0]['status'] = 'active'
        pending = strategy.get_pending_ladders()
        assert len(pending) == 9

    def test_get_active_ladders(self, strategy):
        """Test active ladders filter"""
        active = strategy.get_active_ladders()
        assert len(active) == 0  # None should be active initially

        # Mark one as active
        strategy.ladders[0]['status'] = 'active'
        active = strategy.get_active_ladders()
        assert len(active) == 1

    def test_to_dict(self, strategy):
        """Test dictionary export"""
        strategy.update_prices(50000)
        result = strategy.to_dict()

        assert 'name' in result
        assert 'pair' in result
        assert 'ladders' in result
        assert 'total_swing' in result
        assert 'required_capital' in result


class TestGapClamp:
    """Test gap_max clamp prevents negative prices for high base_gap coins"""

    @pytest.fixture
    def zec_config(self):
        """ZEC-like config where fib×base_gap exceeds 100% at level -9"""
        return {
            "enabled": True,
            "name": "ZEC Balanced",
            "pair": "ZECUSDT",
            "description": "ZEC with gap_max clamp",
            "ladder_config": {
                "base_gap": 0.038,
                "gap_max": 0.95,
                "ladders": 13,
                "fibonacci": [1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233],
                "unit_size_zec": 3.0
            },
            "capital_allocation": {
                "max_allocation_percent": 0.35,
                "reserve_percent": 0.10
            },
            "risk_management": {
                "safety_multiplier": 1.5,
                "stop_loss_percent": -0.50,
                "take_profit_percent": 0.25
            },
            "execution": {
                "auto_rebalance": True,
                "rebalance_interval_hours": 12,
                "min_profit_to_close": 0.005
            }
        }

    @pytest.fixture
    def zec_strategy(self, zec_config):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(zec_config, f)
            temp_path = f.name
        try:
            strategy = Strategy(temp_path)
            yield strategy
        finally:
            os.unlink(temp_path)

    def test_all_13_ladders_created(self, zec_strategy):
        """All 13 ladders should be created (no truncation)"""
        assert len(zec_strategy.ladders) == 13

    def test_all_buy_multipliers_positive(self, zec_strategy):
        """Every buy_price_multiplier must be > 0 (no negative prices)"""
        for ladder in zec_strategy.ladders:
            assert ladder['buy_price_multiplier'] > 0, \
                f"Level {ladder['level']}: multiplier={ladder['buy_price_multiplier']}"

    def test_gap_clamped_at_95(self, zec_strategy):
        """Levels where raw gap > 95% should be clamped to 95%"""
        for ladder in zec_strategy.ladders:
            assert ladder['gap_percent'] <= 0.95, \
                f"Level {ladder['level']}: gap={ladder['gap_percent']}"

        # Level -9 (fib=34): raw_gap = 0.038 * 34 = 1.292, should clamp to 0.95
        level_9 = zec_strategy.ladders[8]
        assert level_9['raw_gap_percent'] == pytest.approx(0.038 * 34, abs=1e-10)
        assert level_9['gap_percent'] == 0.95

    def test_raw_gap_preserved(self, zec_strategy):
        """raw_gap_percent stores the unclamped value"""
        level_1 = zec_strategy.ladders[0]
        assert level_1['raw_gap_percent'] == pytest.approx(0.038, abs=1e-10)
        assert level_1['gap_percent'] == pytest.approx(0.038, abs=1e-10)

        # Level -9 raw gap should exceed 1.0
        level_9 = zec_strategy.ladders[8]
        assert level_9['raw_gap_percent'] > 1.0

    def test_zec_buy_prices_match(self, zec_strategy):
        """Verify ZEC buy prices at entry=$35.00 match expected values"""
        entry = 35.00
        zec_strategy.update_prices(entry)

        expected_buy = [33.67, 32.39, 29.93, 26.52, 21.48, 14.95, 7.56, 1.53]
        for i, exp in enumerate(expected_buy):
            actual = zec_strategy.ladders[i]['buy_price']
            assert abs(actual - exp) < 0.02, \
                f"Level {-(i+1)}: expected ~${exp}, got ${actual:.4f}"

    def test_sell_equals_buy_divided_by_one_minus_gap(self, zec_strategy):
        """Verify: sell = buy / (1 - gap) for reverse calculation"""
        entry = 35.00
        zec_strategy.update_prices(entry)

        for ladder in zec_strategy.ladders:
            gap = ladder['gap_percent']
            reconstructed_sell = ladder['buy_price'] / (1 - gap)
            assert abs(reconstructed_sell - ladder['sell_price']) < 0.001, \
                f"Level {ladder['level']}: sell=${ladder['sell_price']:.4f} vs reconstructed=${reconstructed_sell:.4f}"

    def test_default_gap_max(self):
        """When gap_max is not in config, default to 0.95"""
        config = {
            "enabled": True,
            "name": "No Gap Max",
            "pair": "TESTUSDT",
            "description": "Test default gap_max",
            "ladder_config": {
                "base_gap": 0.038,
                "ladders": 10,
                "fibonacci": [1, 1, 2, 3, 5, 8, 13, 21, 34, 55],
                "unit_size_tes": 1.0
            },
            "capital_allocation": {"max_allocation_percent": 0.25, "reserve_percent": 0.10},
            "risk_management": {"safety_multiplier": 1.5, "stop_loss_percent": -0.50, "take_profit_percent": 0.25},
            "execution": {"auto_rebalance": True, "rebalance_interval_hours": 12, "min_profit_to_close": 0.005}
        }
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(config, f)
            temp_path = f.name
        try:
            s = Strategy(temp_path)
            # Level -9 (fib=34): 0.038*34=1.292, should clamp to default 0.95
            assert s.ladders[8]['gap_percent'] == 0.95
            assert s.ladders[8]['buy_price_multiplier'] > 0
        finally:
            os.unlink(temp_path)


def _make_size_mode_strategy(size_mode, micro_enabled=False, accum_size_mode=None):
    """Build a Strategy with the given fib-tier size_mode (and optional
    matching SELL accumulation)."""
    config = {
        "enabled": True,
        "name": "Size Mode Test",
        "pair": "ZECUSDT",
        "description": "size_mode unit test",
        "ladder_config": {
            "base_gap": 0.04,
            "gap_max": 0.95,
            "ladders": 8,
            "fibonacci": [1, 1, 2, 3, 5, 8, 13, 21],
            "unit_size_zec": 0.5,
        },
        "capital_allocation": {"max_allocation_percent": 1.0, "reserve_percent": 0.10},
        "risk_management": {"safety_multiplier": 1.5, "stop_loss_percent": -0.5,
                             "take_profit_percent": 0.25},
        "execution": {"auto_rebalance": True, "rebalance_interval_hours": 12,
                       "min_profit_to_close": 0.005},
    }
    if size_mode is not None:
        config["ladder_config"]["size_mode"] = size_mode
    if micro_enabled:
        config["ladder_config"]["micro_layer"] = {
            "enabled": True,
            "count": 4,
            "gap": 0.004,
            "fibonacci": [1, 1, 2, 3],
            "unit_size_zec": 0.04,
        }
    if accum_size_mode is not None:
        config["accumulation"] = {
            "enabled": True,
            "base_gap": 0.04,
            "ladders": 8,
            "fibonacci": [1, 1, 2, 3, 5, 8, 13, 21],
            "size_mode": accum_size_mode,
            "unit_size_zec": 0.05,
            "coin_profit_percent": 0.005,
        }
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(config, f)
        temp_path = f.name
    try:
        return Strategy(temp_path)
    finally:
        os.unlink(temp_path)


class TestSizeMode:
    """size_mode controls the units multiplier used by the fib tier."""

    def test_default_is_legacy_martingale(self):
        """No size_mode → existing 2^i behaviour (preserves old strategies)."""
        s = _make_size_mode_strategy(size_mode=None)
        assert [l['units'] for l in s.ladders] == [1, 2, 4, 8, 16, 32, 64, 128]

    def test_explicit_martingale(self):
        s = _make_size_mode_strategy(size_mode='martingale')
        assert [l['units'] for l in s.ladders] == [1, 2, 4, 8, 16, 32, 64, 128]

    def test_fibonacci_uses_configured_sequence(self):
        """size_mode='fibonacci' → units literally match the fib config."""
        s = _make_size_mode_strategy(size_mode='fibonacci')
        assert [l['units'] for l in s.ladders] == [1, 1, 2, 3, 5, 8, 13, 21]
        # All ladders annotated with their size_mode for log clarity
        for ladder in s.ladders:
            assert ladder['size_mode'] == 'fibonacci'

    def test_fib_martingale_is_fib_times_two_pow_i(self):
        s = _make_size_mode_strategy(size_mode='fib_martingale')
        expected = [1 * 1, 1 * 2, 2 * 4, 3 * 8, 5 * 16, 8 * 32, 13 * 64, 21 * 128]
        assert [l['units'] for l in s.ladders] == expected

    def test_unknown_mode_falls_back_to_martingale(self):
        s = _make_size_mode_strategy(size_mode='nonsense')
        assert [l['units'] for l in s.ladders] == [1, 2, 4, 8, 16, 32, 64, 128]

    def test_micro_stays_flat_regardless_of_size_mode(self):
        """User explicitly wants the micro 'always trading' tier kept flat."""
        s = _make_size_mode_strategy(size_mode='fib_martingale', micro_enabled=True)
        micro = [l for l in s.ladders if l.get('tier') == 'micro']
        assert len(micro) == 4
        for l in micro:
            assert l['units'] == 1
            assert l['size_mode'] == 'flat'

    def test_sell_accumulation_honours_size_mode(self):
        """SELL-side accumulation reads its own size_mode (independent of BUY)."""
        s = _make_size_mode_strategy(
            size_mode='martingale', accum_size_mode='fibonacci'
        )
        sell_units = [l['units'] for l in s.sell_ladders if l.get('tier') == 'fib']
        assert sell_units == [1, 1, 2, 3, 5, 8, 13, 21]

    def test_fibonacci_sizing_dramatically_smaller_than_martingale(self):
        """Capital required for fib mode is a fraction of legacy 2^i."""
        s_mart = _make_size_mode_strategy(size_mode='martingale')
        s_fib = _make_size_mode_strategy(size_mode='fibonacci')
        s_mart.update_prices(380.0)
        s_fib.update_prices(380.0)
        cap_mart = s_mart.calculate_required_capital()
        cap_fib = s_fib.calculate_required_capital()
        # Pure fib should need < 20% of pure martingale (54 vs 255 unit-sum).
        assert cap_fib < cap_mart * 0.25, (
            f"fib capital ${cap_fib:.0f} not <25% of martingale ${cap_mart:.0f}"
        )
