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

        if self.strategy.is_distribution_mode():
            return self._run_distribution()

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

    def _run_distribution(self):
        """Backtest variant for distribution mode: each ladder is split into
        Fibonacci-weighted children (see Strategy.calculate_child_orders). Each
        child goes through pending → placed (buy) → active → closed (sell),
        mirroring the live OrderManager. Children are promoted only when the
        market price is within proximity, and only when the simulated open-order
        count is below the configured cap — so the simulation matches the
        exchange's real limitations.
        """
        dist_cfg = self.strategy.get_distribution_config()
        proximity = dist_cfg['proximity_percent']
        cap = dist_cfg['max_open_orders_cap']

        # Build pending children for every ladder
        all_children = self.strategy.calculate_all_child_orders()
        pending = []  # queue of children waiting to be placed (sorted top-first)
        for ladder in self.strategy.ladders:
            children = all_children.get(ladder['level'], [])
            ladder['children_total'] = len(children)
            ladder['children_closed'] = 0
            for c in children:
                c['parent_ladder'] = ladder
                c['_buy_time'] = None
                c['_buy_cost'] = None
            pending.extend(children)
        pending.sort(key=lambda c: c['buy_price'], reverse=True)

        # Simulated books
        active_buys = []   # children with open BUY order
        active_sells = []  # children with open SELL order

        total_children = len(pending)
        logger.info(f"Distribution backtest: {total_children} children across "
                    f"{len(self.strategy.ladders)} ladders, cap={cap}, "
                    f"proximity={proximity:.1%}")

        for timestamp, row in self.data.iterrows():
            open_price = row['open']
            close_price = row['close']
            low_price = row['low']
            high_price = row['high']

            # 1. Promote pending children that are in proximity and fit the cap
            def open_count():
                return len(active_buys) + len(active_sells)

            for child in list(pending):
                if open_count() >= cap:
                    break
                if open_price <= child['buy_price']:
                    should_promote = True
                else:
                    distance = (open_price - child['buy_price']) / open_price
                    should_promote = distance <= proximity
                if not should_promote:
                    break  # sorted top-first; rest are farther away
                pending.remove(child)
                active_buys.append(child)
                child['status'] = 'placed'

            # 2. Check child BUY fills (limit buy fills when candle low crosses)
            for child in list(active_buys):
                if low_price <= child['buy_price']:
                    cost = child['qty'] * child['buy_price']
                    fee = cost * 0.001
                    total_cost = cost + fee
                    if self.capital < total_cost:
                        continue  # insufficient capital — skip this candle
                    self.capital -= total_cost
                    child['_buy_time'] = timestamp
                    child['_buy_cost'] = total_cost
                    child['status'] = 'active'
                    active_buys.remove(child)
                    active_sells.append(child)
                    child['parent_ladder']['status'] = 'active'

            # 3. Check child SELL fills (limit sell fills when candle high crosses)
            for child in list(active_sells):
                sell_price = child['parent_ladder']['sell_price']
                if high_price >= sell_price:
                    revenue = child['qty'] * sell_price
                    fee = revenue * 0.001
                    net_revenue = revenue - fee
                    profit = net_revenue - child['_buy_cost']
                    self.capital += net_revenue

                    self.trades.append({
                        'buy_time': child['_buy_time'],
                        'sell_time': timestamp,
                        'level': child['parent_level'],
                        'child_idx': child['idx'],
                        'buy_price': child['buy_price'],
                        'sell_price': sell_price,
                        'quantity': child['qty'],
                        'cost': child['_buy_cost'],
                        'revenue': net_revenue,
                        'profit': profit,
                        'roi': (profit / child['_buy_cost']) * 100 if child['_buy_cost'] else 0,
                    })
                    active_sells.remove(child)
                    child['status'] = 'closed'
                    parent = child['parent_ladder']
                    parent['children_closed'] = parent.get('children_closed', 0) + 1
                    if parent['children_closed'] >= parent['children_total']:
                        parent['status'] = 'closed'

            # 4. Mark-to-market portfolio value
            positions_value = sum(c['qty'] * close_price for c in active_sells)
            # active_buys have not filled yet — their USDT still counts as cash
            self.portfolio_value.append({
                'timestamp': timestamp,
                'cash': self.capital,
                'positions_value': positions_value,
                'total_value': self.capital + positions_value,
                'pending_children': len(pending),
                'placed_children': len(active_buys) + len(active_sells),
            })

        logger.info(f"Distribution backtest complete: {len(self.trades)}/{total_children} children closed, "
                    f"{len(pending)} never promoted, {len(active_buys)} never filled, "
                    f"{len(active_sells)} never sold")
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
