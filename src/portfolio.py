"""
Portfolio Manager - Track positions across multiple strategies
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class Portfolio:
    def __init__(self, initial_capital):
        self.initial_capital = initial_capital
        self.positions = {}  # {strategy_name: {ladders: [], total_cost: 0}}
        self.trades_history = []
        self.capital_allocated = 0
        self.capital_free = initial_capital

    def add_position(self, strategy_name, ladder_level, buy_price, quantity, cost):
        """Add a new position to portfolio"""
        if strategy_name not in self.positions:
            self.positions[strategy_name] = {
                'ladders': [],
                'total_cost': 0,
                'total_quantity': 0
            }

        position = {
            'level': ladder_level,
            'buy_price': buy_price,
            'quantity': quantity,
            'cost': cost,
            'timestamp': datetime.now(),
            'status': 'open'
        }

        self.positions[strategy_name]['ladders'].append(position)
        self.positions[strategy_name]['total_cost'] += cost
        self.positions[strategy_name]['total_quantity'] += quantity

        self.capital_allocated += cost
        self.capital_free -= cost

        logger.info(f"Position added: {strategy_name} Level {ladder_level} - {quantity} @ {buy_price}")

    def close_position(self, strategy_name, ladder_level, sell_price, quantity):
        """Close a position and record profit"""
        if strategy_name not in self.positions:
            logger.error(f"Strategy {strategy_name} not found in portfolio")
            return None

        # Find matching position
        for position in self.positions[strategy_name]['ladders']:
            if position['level'] == ladder_level and position['status'] == 'open':
                # Calculate profit
                revenue = sell_price * quantity
                profit = revenue - position['cost']

                # Update position
                position['status'] = 'closed'
                position['sell_price'] = sell_price
                position['profit'] = profit
                position['close_timestamp'] = datetime.now()

                # Update portfolio
                self.capital_free += revenue
                self.capital_allocated -= position['cost']

                # Record trade
                trade = {
                    'strategy': strategy_name,
                    'level': ladder_level,
                    'buy_price': position['buy_price'],
                    'sell_price': sell_price,
                    'quantity': quantity,
                    'profit': profit,
                    'timestamp': datetime.now()
                }
                self.trades_history.append(trade)

                logger.info(f"Position closed: {strategy_name} Level {ladder_level} - Profit: ${profit:.2f}")
                return trade

        logger.warning(f"No open position found for {strategy_name} Level {ladder_level}")
        return None

    def get_total_value(self, current_prices):
        """Calculate total portfolio value"""
        total_value = self.capital_free

        for strategy_name, data in self.positions.items():
            # Get current price for this strategy's pair
            pair = strategy_name.split()[0]  # Extract coin name
            current_price = current_prices.get(f"{pair}USDT", 0)

            # Add value of open positions
            for position in data['ladders']:
                if position['status'] == 'open':
                    total_value += position['quantity'] * current_price

        return total_value

    def get_unrealized_pnl(self, current_prices):
        """Calculate unrealized profit/loss"""
        unrealized_pnl = 0

        for strategy_name, data in self.positions.items():
            pair = strategy_name.split()[0]
            current_price = current_prices.get(f"{pair}USDT", 0)

            for position in data['ladders']:
                if position['status'] == 'open':
                    current_value = position['quantity'] * current_price
                    unrealized_pnl += current_value - position['cost']

        return unrealized_pnl

    def get_realized_pnl(self):
        """Calculate realized profit/loss from closed trades"""
        return sum(trade['profit'] for trade in self.trades_history)

    def get_statistics(self, current_prices):
        """Get portfolio statistics"""
        total_value = self.get_total_value(current_prices)
        realized_pnl = self.get_realized_pnl()
        unrealized_pnl = self.get_unrealized_pnl(current_prices)
        total_pnl = realized_pnl + unrealized_pnl

        return {
            'initial_capital': self.initial_capital,
            'capital_free': self.capital_free,
            'capital_allocated': self.capital_allocated,
            'total_value': total_value,
            'realized_pnl': realized_pnl,
            'unrealized_pnl': unrealized_pnl,
            'total_pnl': total_pnl,
            'roi_percent': (total_pnl / self.initial_capital) * 100,
            'num_trades': len(self.trades_history),
            'num_open_positions': sum(
                len([p for p in data['ladders'] if p['status'] == 'open'])
                for data in self.positions.values()
            )
        }
