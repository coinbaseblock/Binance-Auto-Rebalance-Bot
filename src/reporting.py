"""
Trading Summary Reporter - Generate tables and statistics
"""
import pandas as pd
from tabulate import tabulate
from datetime import datetime


class TradingReporter:
    """Generate trading summary reports with tables"""

    def __init__(self, trades=None, portfolio_value=None):
        self.trades = trades or []
        self.portfolio_value = portfolio_value or []

    def trades_table(self, format='grid'):
        """
        Generate a formatted table of all trades

        Args:
            format: Table format ('grid', 'simple', 'github', 'html')

        Returns:
            Formatted table string
        """
        if not self.trades:
            return "No trades to display"

        df = pd.DataFrame(self.trades)

        # Format columns
        table_data = []
        for _, trade in df.iterrows():
            row = {
                '#': len(table_data) + 1,
                'Level': trade.get('level', 'N/A'),
                'Buy Price': f"${trade.get('buy_price', 0):,.2f}",
                'Sell Price': f"${trade.get('sell_price', 0):,.2f}",
                'Quantity': f"{trade.get('quantity', 0):.6f}",
                'Profit': f"${trade.get('profit', 0):,.2f}",
                'ROI': f"{trade.get('roi', (trade.get('profit', 0) / trade.get('cost', 1)) * 100):.2f}%",
                'Status': '✅ WIN' if trade.get('profit', 0) > 0 else '❌ LOSS'
            }
            table_data.append(row)

        return tabulate(table_data, headers='keys', tablefmt=format)

    def summary_stats(self, format='grid'):
        """
        Generate summary statistics table

        Args:
            format: Table format

        Returns:
            Formatted statistics table
        """
        if not self.trades:
            return "No trades for statistics"

        df = pd.DataFrame(self.trades)

        total_trades = len(df)
        winning_trades = len(df[df['profit'] > 0])
        losing_trades = len(df[df['profit'] < 0])
        total_profit = df['profit'].sum()
        avg_profit = df['profit'].mean()
        max_profit = df['profit'].max()
        max_loss = df['profit'].min()
        win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0

        stats = [
            ['📊 Total Trades', total_trades],
            ['✅ Winning Trades', winning_trades],
            ['❌ Losing Trades', losing_trades],
            ['📈 Win Rate', f"{win_rate:.1f}%"],
            ['💰 Total Profit/Loss', f"${total_profit:,.2f}"],
            ['📊 Average P/L per Trade', f"${avg_profit:,.2f}"],
            ['🏆 Best Trade', f"${max_profit:,.2f}"],
            ['📉 Worst Trade', f"${max_loss:,.2f}"],
        ]

        return tabulate(stats, headers=['Metric', 'Value'], tablefmt=format)

    def profit_by_level(self, format='grid'):
        """
        Generate profit breakdown by ladder level

        Args:
            format: Table format

        Returns:
            Formatted table by level
        """
        if not self.trades:
            return "No trades for level analysis"

        df = pd.DataFrame(self.trades)

        level_stats = df.groupby('level').agg({
            'profit': ['count', 'sum', 'mean'],
            'buy_price': 'mean',
            'sell_price': 'mean'
        }).round(2)

        level_stats.columns = ['Trades', 'Total Profit', 'Avg Profit', 'Avg Buy', 'Avg Sell']
        level_stats = level_stats.reset_index()
        level_stats.columns = ['Level', 'Trades', 'Total Profit', 'Avg Profit', 'Avg Buy', 'Avg Sell']

        # Format values
        table_data = []
        for _, row in level_stats.iterrows():
            table_data.append({
                'Level': int(row['Level']),
                'Trades': int(row['Trades']),
                'Total Profit': f"${row['Total Profit']:,.2f}",
                'Avg Profit': f"${row['Avg Profit']:,.2f}",
                'Avg Buy': f"${row['Avg Buy']:,.2f}",
                'Avg Sell': f"${row['Avg Sell']:,.2f}"
            })

        return tabulate(table_data, headers='keys', tablefmt=format)

    def portfolio_summary(self, initial_capital=10000, format='grid'):
        """
        Generate portfolio performance summary

        Args:
            initial_capital: Starting capital
            format: Table format

        Returns:
            Formatted portfolio summary
        """
        if not self.portfolio_value:
            return "No portfolio data available"

        df = pd.DataFrame(self.portfolio_value)

        final_value = df['total_value'].iloc[-1]
        total_return = final_value - initial_capital
        total_return_pct = (total_return / initial_capital) * 100

        # Calculate max drawdown
        peak = df['total_value'].expanding(min_periods=1).max()
        drawdown = (df['total_value'] - peak) / peak
        max_drawdown = drawdown.min() * 100

        # Calculate Sharpe ratio
        returns = df['total_value'].pct_change().dropna()
        sharpe = (returns.mean() / returns.std()) * (252 ** 0.5) if returns.std() > 0 else 0

        summary = [
            ['💵 Initial Capital', f"${initial_capital:,.2f}"],
            ['💰 Final Value', f"${final_value:,.2f}"],
            ['📈 Total Return', f"${total_return:,.2f}"],
            ['📊 ROI', f"{total_return_pct:.2f}%"],
            ['📉 Max Drawdown', f"{max_drawdown:.2f}%"],
            ['⚡ Sharpe Ratio', f"{sharpe:.2f}"],
        ]

        return tabulate(summary, headers=['Metric', 'Value'], tablefmt=format)

    def print_full_report(self, initial_capital=10000):
        """Print a complete trading report"""
        print("\n" + "="*60)
        print("📊 TRADING SUMMARY REPORT")
        print("="*60)

        print("\n📋 TRADE HISTORY")
        print("-"*60)
        print(self.trades_table())

        print("\n📈 SUMMARY STATISTICS")
        print("-"*60)
        print(self.summary_stats())

        print("\n📊 PROFIT BY LADDER LEVEL")
        print("-"*60)
        print(self.profit_by_level())

        print("\n💼 PORTFOLIO PERFORMANCE")
        print("-"*60)
        print(self.portfolio_summary(initial_capital))

        print("\n" + "="*60)


def generate_sample_trades():
    """Generate sample trade data for demonstration"""
    import random

    trades = []
    base_price = 42000  # BTC starting price

    for i in range(15):
        level = random.randint(-5, -1)
        buy_price = base_price * (1 + level * 0.01)  # Lower price for deeper levels
        sell_price = buy_price * 1.015  # 1.5% profit target
        quantity = 0.001 * abs(level)  # More quantity at deeper levels
        cost = buy_price * quantity * 1.001  # Including 0.1% fee
        revenue = sell_price * quantity * 0.999  # After 0.1% fee
        profit = revenue - cost

        trades.append({
            'level': level,
            'buy_price': buy_price,
            'sell_price': sell_price,
            'quantity': quantity,
            'cost': cost,
            'revenue': revenue,
            'profit': profit,
            'roi': (profit / cost) * 100,
            'buy_time': datetime.now(),
            'sell_time': datetime.now()
        })

        base_price *= random.uniform(0.995, 1.005)  # Small price fluctuation

    return trades


def generate_sample_portfolio_value(trades, initial_capital=10000):
    """Generate sample portfolio value data"""
    import random

    portfolio = []
    value = initial_capital

    for i in range(100):
        # Simulate value fluctuation
        change = random.uniform(-0.02, 0.025)
        value *= (1 + change)

        portfolio.append({
            'timestamp': datetime.now(),
            'cash': value * 0.3,
            'positions_value': value * 0.7,
            'total_value': value
        })

    return portfolio
