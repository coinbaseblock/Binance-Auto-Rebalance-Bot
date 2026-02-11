"""
Backtesting Engine
"""
import pandas as pd
import logging
from datetime import datetime, timedelta
import json

logger = logging.getLogger(__name__)


class Backtester:
    def __init__(self, strategy, historical_data):
        self.strategy = strategy
        self.data = historical_data
        self.trades = []
        self.portfolio_value = []
        self.initial_capital = 10000  # USDT
        self.capital = self.initial_capital
        self.positions = []

    def run(self):
        """Run backtest on historical data"""
        logger.info(f"Starting backtest for {self.strategy.config['name']}")
        logger.info(f"Data range: {self.data.index[0]} to {self.data.index[-1]}")

        # Initialize ladders with starting price
        starting_price = self.data['close'].iloc[0]
        self.strategy.update_prices(starting_price)

        # Track which ladders are active
        active_ladders = []

        # Iterate through each candle
        for timestamp, row in self.data.iterrows():
            current_price = row['close']
            low_price = row['low']
            high_price = row['high']

            # Check if any buy orders would trigger
            for ladder in self.strategy.get_pending_ladders():
                if low_price <= ladder['buy_price']:
                    # Buy triggered
                    cost = ladder['btc_amount'] * ladder['buy_price']
                    fee = cost * 0.001
                    total_cost = cost + fee

                    if self.capital >= total_cost:
                        self.capital -= total_cost
                        active_ladders.append({
                            'ladder': ladder,
                            'buy_price': ladder['buy_price'],
                            'buy_time': timestamp,
                            'quantity': ladder['btc_amount'],
                            'cost': total_cost
                        })
                        ladder['status'] = 'active'

                        logger.debug(f"{timestamp}: BUY Level {ladder['level']} @ ${ladder['buy_price']:.2f}")

            # Check if any sell orders would trigger
            for position in active_ladders[:]:
                ladder = position['ladder']
                if high_price >= ladder['sell_price']:
                    # Sell triggered
                    revenue = position['quantity'] * ladder['sell_price']
                    fee = revenue * 0.001
                    net_revenue = revenue - fee

                    profit = net_revenue - position['cost']

                    self.capital += net_revenue

                    trade = {
                        'buy_time': position['buy_time'],
                        'sell_time': timestamp,
                        'level': ladder['level'],
                        'buy_price': position['buy_price'],
                        'sell_price': ladder['sell_price'],
                        'quantity': position['quantity'],
                        'cost': position['cost'],
                        'revenue': net_revenue,
                        'profit': profit,
                        'roi': (profit / position['cost']) * 100
                    }

                    self.trades.append(trade)
                    active_ladders.remove(position)
                    ladder['status'] = 'closed'

                    logger.debug(f"{timestamp}: SELL Level {ladder['level']} @ ${ladder['sell_price']:.2f} - Profit: ${profit:.2f}")

            # Calculate portfolio value
            positions_value = sum(p['quantity'] * current_price for p in active_ladders)
            total_value = self.capital + positions_value

            self.portfolio_value.append({
                'timestamp': timestamp,
                'cash': self.capital,
                'positions_value': positions_value,
                'total_value': total_value
            })

        return self.generate_report()

    def generate_report(self):
        """Generate backtest report"""
        if not self.trades:
            return {
                'error': 'No trades executed',
                'initial_capital': self.initial_capital,
                'final_capital': self.capital
            }

        trades_df = pd.DataFrame(self.trades)
        portfolio_df = pd.DataFrame(self.portfolio_value)

        total_profit = trades_df['profit'].sum()
        num_trades = len(trades_df)
        winning_trades = len(trades_df[trades_df['profit'] > 0])
        losing_trades = len(trades_df[trades_df['profit'] < 0])

        report = {
            'strategy': self.strategy.config['name'],
            'period': {
                'start': self.data.index[0].strftime('%Y-%m-%d'),
                'end': self.data.index[-1].strftime('%Y-%m-%d'),
                'days': (self.data.index[-1] - self.data.index[0]).days
            },
            'capital': {
                'initial': self.initial_capital,
                'final': portfolio_df['total_value'].iloc[-1],
                'profit': total_profit,
                'roi_percent': (total_profit / self.initial_capital) * 100
            },
            'trades': {
                'total': num_trades,
                'winning': winning_trades,
                'losing': losing_trades,
                'win_rate': (winning_trades / num_trades) * 100 if num_trades > 0 else 0,
                'avg_profit': trades_df['profit'].mean(),
                'max_profit': trades_df['profit'].max(),
                'max_loss': trades_df['profit'].min()
            },
            'performance': {
                'total_return_pct': ((portfolio_df['total_value'].iloc[-1] - self.initial_capital) / self.initial_capital) * 100,
                'max_drawdown_pct': self._calculate_max_drawdown(portfolio_df),
                'sharpe_ratio': self._calculate_sharpe_ratio(portfolio_df)
            }
        }

        return report

    def _calculate_max_drawdown(self, portfolio_df):
        """Calculate maximum drawdown"""
        peak = portfolio_df['total_value'].expanding(min_periods=1).max()
        drawdown = (portfolio_df['total_value'] - peak) / peak
        return drawdown.min() * 100

    def _calculate_sharpe_ratio(self, portfolio_df):
        """Calculate Sharpe ratio"""
        returns = portfolio_df['total_value'].pct_change().dropna()
        if len(returns) == 0:
            return 0
        return (returns.mean() / returns.std()) * (252 ** 0.5) if returns.std() > 0 else 0

    def save_report(self, filepath):
        """Save backtest report to file"""
        report = self.generate_report()
        with open(filepath, 'w') as f:
            json.dump(report, f, indent=2)
        logger.info(f"Backtest report saved to {filepath}")
