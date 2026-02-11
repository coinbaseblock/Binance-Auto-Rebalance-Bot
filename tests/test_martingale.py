"""
Tests for Martingale module
"""
import pytest

from src.martingale import MartingaleCalculator


@pytest.fixture
def calculator():
    """Create a MartingaleCalculator instance"""
    return MartingaleCalculator(fee_rate=0.001)


class TestMartingaleCalculator:
    def test_position_size_level_1(self, calculator):
        """Test position size calculation for level 1"""
        result = calculator.calculate_position_size(
            level=1,
            previous_cost=0,
            gap_amount=100,
            target_profit=10
        )

        assert result['level'] == 1
        assert result['units'] == 1  # 2^0 = 1
        assert result['previous_cost'] == 0

    def test_position_size_level_3(self, calculator):
        """Test position size calculation for level 3"""
        result = calculator.calculate_position_size(
            level=3,
            previous_cost=1000,
            gap_amount=100,
            target_profit=10
        )

        assert result['level'] == 3
        assert result['units'] == 4  # 2^2 = 4
        assert result['previous_cost'] == 1000

    def test_martingale_doubling(self, calculator):
        """Test that units double with each level"""
        levels = [1, 2, 3, 4, 5]
        expected_units = [1, 2, 4, 8, 16]

        for level, expected in zip(levels, expected_units):
            result = calculator.calculate_position_size(
                level=level,
                previous_cost=0,
                gap_amount=100
            )
            assert result['units'] == expected

    def test_calculate_profit_positive(self, calculator):
        """Test profit calculation for a winning trade"""
        result = calculator.calculate_profit(
            buy_price=50000,
            sell_price=51000,
            quantity=0.1
        )

        # Buy cost: 50000 * 0.1 = 5000
        # Buy fee: 5000 * 0.001 = 5
        # Total buy: 5005
        # Sell revenue: 51000 * 0.1 = 5100
        # Sell fee: 5100 * 0.001 = 5.1
        # Net sell: 5094.9
        # Profit: 5094.9 - 5005 = 89.9

        assert result['buy_cost'] == 5000
        assert result['buy_fee'] == 5
        assert result['total_buy_cost'] == 5005
        assert result['sell_revenue'] == 5100
        assert abs(result['sell_fee'] - 5.1) < 0.001
        assert abs(result['net_sell_revenue'] - 5094.9) < 0.001
        assert abs(result['profit'] - 89.9) < 0.001
        assert result['roi_percent'] > 0

    def test_calculate_profit_negative(self, calculator):
        """Test profit calculation for a losing trade"""
        result = calculator.calculate_profit(
            buy_price=50000,
            sell_price=49000,
            quantity=0.1
        )

        assert result['profit'] < 0
        assert result['roi_percent'] < 0

    def test_calculate_profit_breakeven(self, calculator):
        """Test profit calculation at approximate breakeven"""
        result = calculator.calculate_profit(
            buy_price=50000,
            sell_price=50100,  # Small gain to cover fees
            quantity=0.1
        )

        # Should be close to breakeven (fees eat into the small gain)
        assert abs(result['profit']) < 5  # Small profit or loss

    def test_fee_rate_impact(self):
        """Test that different fee rates affect profit"""
        calc_low_fee = MartingaleCalculator(fee_rate=0.0005)
        calc_high_fee = MartingaleCalculator(fee_rate=0.002)

        result_low = calc_low_fee.calculate_profit(50000, 51000, 0.1)
        result_high = calc_high_fee.calculate_profit(50000, 51000, 0.1)

        assert result_low['profit'] > result_high['profit']
