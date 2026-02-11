"""
Martingale Position Sizing Calculator
"""
import logging

logger = logging.getLogger(__name__)


class MartingaleCalculator:
    def __init__(self, fee_rate=0.001):
        self.fee_rate = fee_rate

    def calculate_position_size(self, level, previous_cost, gap_amount, target_profit=10):
        """
        Calculate position size for a given level

        Args:
            level: Ladder level (1, 2, 3, ...)
            previous_cost: Total cost from previous levels
            gap_amount: Price difference to next level
            target_profit: Minimum profit target in USDT

        Returns:
            dict with position size details
        """
        # Simple Martingale: double each time
        units = 2 ** (level - 1)

        # Calculate required amount to cover previous losses + target profit
        total_recovery = previous_cost + target_profit

        # Account for fees (buy + sell)
        fee_multiplier = 1 + (2 * self.fee_rate)

        # Minimum position size needed
        min_position_value = total_recovery / (gap_amount / gap_amount * fee_multiplier)

        return {
            'level': level,
            'units': units,
            'previous_cost': previous_cost,
            'target_profit': target_profit,
            'recommended_units': max(units, int(min_position_value / gap_amount) + 1)
        }

    def calculate_profit(self, buy_price, sell_price, quantity):
        """
        Calculate profit after fees

        Args:
            buy_price: Entry price
            sell_price: Exit price
            quantity: Amount traded

        Returns:
            dict with profit details
        """
        buy_cost = buy_price * quantity
        buy_fee = buy_cost * self.fee_rate
        total_buy_cost = buy_cost + buy_fee

        sell_revenue = sell_price * quantity
        sell_fee = sell_revenue * self.fee_rate
        net_sell_revenue = sell_revenue - sell_fee

        profit = net_sell_revenue - total_buy_cost
        roi = (profit / total_buy_cost) * 100 if total_buy_cost > 0 else 0

        return {
            'buy_cost': buy_cost,
            'buy_fee': buy_fee,
            'total_buy_cost': total_buy_cost,
            'sell_revenue': sell_revenue,
            'sell_fee': sell_fee,
            'net_sell_revenue': net_sell_revenue,
            'profit': profit,
            'roi_percent': roi
        }
