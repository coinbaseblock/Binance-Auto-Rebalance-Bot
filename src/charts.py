"""
Trading Charts - Generate visualizations for trading performance
"""
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import pandas as pd
import numpy as np
from datetime import datetime


class TradingCharts:
    """Generate trading visualizations"""

    def __init__(self, trades=None, portfolio_value=None):
        self.trades = trades or []
        self.portfolio_value = portfolio_value or []
        plt.style.use('seaborn-v0_8-darkgrid')

    def equity_curve(self, save_path=None, show=True):
        """
        Plot portfolio equity curve over time

        Args:
            save_path: Path to save the chart
            show: Whether to display the chart
        """
        if not self.portfolio_value:
            print("No portfolio data for equity curve")
            return

        df = pd.DataFrame(self.portfolio_value)

        fig, ax = plt.subplots(figsize=(12, 6))

        ax.plot(df.index, df['total_value'], color='#2E86AB', linewidth=2, label='Portfolio Value')
        ax.fill_between(df.index, df['total_value'], alpha=0.3, color='#2E86AB')

        # Mark high points
        peak_idx = df['total_value'].idxmax()
        ax.scatter([peak_idx], [df['total_value'].iloc[peak_idx]],
                   color='green', s=100, zorder=5, marker='^', label='Peak')

        ax.set_title('📈 Portfolio Equity Curve', fontsize=14, fontweight='bold')
        ax.set_xlabel('Time', fontsize=12)
        ax.set_ylabel('Value (USDT)', fontsize=12)
        ax.legend(loc='upper left')
        ax.grid(True, alpha=0.3)

        plt.tight_layout()

        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            print(f"Chart saved to: {save_path}")

        if show:
            plt.show()

        return fig

    def profit_loss_chart(self, save_path=None, show=True):
        """
        Plot profit/loss for each trade

        Args:
            save_path: Path to save the chart
            show: Whether to display the chart
        """
        if not self.trades:
            print("No trades for P/L chart")
            return

        df = pd.DataFrame(self.trades)

        fig, ax = plt.subplots(figsize=(12, 6))

        colors = ['#27AE60' if p > 0 else '#E74C3C' for p in df['profit']]
        bars = ax.bar(range(len(df)), df['profit'], color=colors, edgecolor='white', linewidth=0.5)

        # Add zero line
        ax.axhline(y=0, color='black', linestyle='-', linewidth=1)

        # Add cumulative profit line
        ax2 = ax.twinx()
        cumulative = df['profit'].cumsum()
        ax2.plot(range(len(df)), cumulative, color='#8E44AD', linewidth=2,
                 marker='o', markersize=4, label='Cumulative P/L')
        ax2.set_ylabel('Cumulative Profit (USDT)', fontsize=12, color='#8E44AD')

        ax.set_title('💰 Profit/Loss Per Trade', fontsize=14, fontweight='bold')
        ax.set_xlabel('Trade #', fontsize=12)
        ax.set_ylabel('Profit (USDT)', fontsize=12)

        # Legend
        win_patch = mpatches.Patch(color='#27AE60', label='Winning Trade')
        loss_patch = mpatches.Patch(color='#E74C3C', label='Losing Trade')
        ax.legend(handles=[win_patch, loss_patch], loc='upper left')

        plt.tight_layout()

        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            print(f"Chart saved to: {save_path}")

        if show:
            plt.show()

        return fig

    def profit_by_level_chart(self, save_path=None, show=True):
        """
        Plot profit breakdown by ladder level

        Args:
            save_path: Path to save the chart
            show: Whether to display the chart
        """
        if not self.trades:
            print("No trades for level analysis")
            return

        df = pd.DataFrame(self.trades)
        level_profit = df.groupby('level')['profit'].sum().sort_index()

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

        # Bar chart of profit by level
        colors = ['#27AE60' if p > 0 else '#E74C3C' for p in level_profit.values]
        ax1.barh(level_profit.index.astype(str), level_profit.values, color=colors, edgecolor='white')
        ax1.axvline(x=0, color='black', linestyle='-', linewidth=1)
        ax1.set_title('💹 Profit by Ladder Level', fontsize=14, fontweight='bold')
        ax1.set_xlabel('Total Profit (USDT)', fontsize=12)
        ax1.set_ylabel('Ladder Level', fontsize=12)

        # Trade count by level
        level_counts = df.groupby('level').size().sort_index()
        ax2.barh(level_counts.index.astype(str), level_counts.values, color='#3498DB', edgecolor='white')
        ax2.set_title('📊 Trade Count by Level', fontsize=14, fontweight='bold')
        ax2.set_xlabel('Number of Trades', fontsize=12)
        ax2.set_ylabel('Ladder Level', fontsize=12)

        plt.tight_layout()

        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            print(f"Chart saved to: {save_path}")

        if show:
            plt.show()

        return fig

    def win_loss_pie(self, save_path=None, show=True):
        """
        Plot win/loss ratio pie chart

        Args:
            save_path: Path to save the chart
            show: Whether to display the chart
        """
        if not self.trades:
            print("No trades for pie chart")
            return

        df = pd.DataFrame(self.trades)
        wins = len(df[df['profit'] > 0])
        losses = len(df[df['profit'] <= 0])

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

        # Win/Loss count pie
        sizes = [wins, losses]
        labels = [f'Wins\n({wins})', f'Losses\n({losses})']
        colors = ['#27AE60', '#E74C3C']
        explode = (0.05, 0)

        ax1.pie(sizes, explode=explode, labels=labels, colors=colors,
                autopct='%1.1f%%', shadow=True, startangle=90,
                textprops={'fontsize': 12, 'fontweight': 'bold'})
        ax1.set_title('📊 Win Rate', fontsize=14, fontweight='bold')

        # Profit distribution pie
        win_profit = df[df['profit'] > 0]['profit'].sum()
        loss_amount = abs(df[df['profit'] <= 0]['profit'].sum())

        if win_profit > 0 or loss_amount > 0:
            profit_sizes = [max(0, win_profit), max(0, loss_amount)]
            profit_labels = [f'Profits\n(${win_profit:.0f})', f'Losses\n(${loss_amount:.0f})']

            ax2.pie(profit_sizes, explode=explode, labels=profit_labels, colors=colors,
                    autopct='%1.1f%%', shadow=True, startangle=90,
                    textprops={'fontsize': 12, 'fontweight': 'bold'})
            ax2.set_title('💰 Profit Distribution', fontsize=14, fontweight='bold')

        plt.tight_layout()

        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            print(f"Chart saved to: {save_path}")

        if show:
            plt.show()

        return fig

    def drawdown_chart(self, save_path=None, show=True):
        """
        Plot portfolio drawdown over time

        Args:
            save_path: Path to save the chart
            show: Whether to display the chart
        """
        if not self.portfolio_value:
            print("No portfolio data for drawdown chart")
            return

        df = pd.DataFrame(self.portfolio_value)

        # Calculate drawdown
        peak = df['total_value'].expanding(min_periods=1).max()
        drawdown = ((df['total_value'] - peak) / peak) * 100

        fig, ax = plt.subplots(figsize=(12, 6))

        ax.fill_between(df.index, 0, drawdown, color='#E74C3C', alpha=0.7)
        ax.plot(df.index, drawdown, color='#C0392B', linewidth=1)

        # Mark maximum drawdown
        min_dd_idx = drawdown.idxmin()
        ax.scatter([min_dd_idx], [drawdown.iloc[min_dd_idx]],
                   color='#8B0000', s=100, zorder=5, marker='v')
        ax.annotate(f'Max DD: {drawdown.iloc[min_dd_idx]:.1f}%',
                    xy=(min_dd_idx, drawdown.iloc[min_dd_idx]),
                    xytext=(10, -20), textcoords='offset points',
                    fontsize=10, fontweight='bold',
                    arrowprops=dict(arrowstyle='->', color='black'))

        ax.set_title('📉 Portfolio Drawdown', fontsize=14, fontweight='bold')
        ax.set_xlabel('Time', fontsize=12)
        ax.set_ylabel('Drawdown (%)', fontsize=12)
        ax.grid(True, alpha=0.3)

        plt.tight_layout()

        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            print(f"Chart saved to: {save_path}")

        if show:
            plt.show()

        return fig

    def generate_all_charts(self, output_dir='charts', show=True):
        """
        Generate all charts and save to directory

        Args:
            output_dir: Directory to save charts
            show: Whether to display charts
        """
        import os
        os.makedirs(output_dir, exist_ok=True)

        print("\n📊 Generating Trading Charts...")
        print("="*50)

        charts = []

        if self.portfolio_value:
            print("📈 Creating Equity Curve...")
            self.equity_curve(save_path=f"{output_dir}/equity_curve.png", show=show)
            charts.append('equity_curve.png')

            print("📉 Creating Drawdown Chart...")
            self.drawdown_chart(save_path=f"{output_dir}/drawdown.png", show=show)
            charts.append('drawdown.png')

        if self.trades:
            print("💰 Creating P/L Chart...")
            self.profit_loss_chart(save_path=f"{output_dir}/profit_loss.png", show=show)
            charts.append('profit_loss.png')

            print("📊 Creating Level Analysis...")
            self.profit_by_level_chart(save_path=f"{output_dir}/profit_by_level.png", show=show)
            charts.append('profit_by_level.png')

            print("🥧 Creating Win/Loss Pie...")
            self.win_loss_pie(save_path=f"{output_dir}/win_loss_pie.png", show=show)
            charts.append('win_loss_pie.png')

        print("="*50)
        print(f"✅ Generated {len(charts)} charts in '{output_dir}/'")

        return charts


def create_dashboard(trades, portfolio_value, save_path='trading_dashboard.png', show=True):
    """
    Create a comprehensive trading dashboard with multiple charts

    Args:
        trades: List of trade dictionaries
        portfolio_value: List of portfolio value dictionaries
        save_path: Path to save the dashboard
        show: Whether to display the dashboard
    """
    if not trades and not portfolio_value:
        print("No data for dashboard")
        return

    fig = plt.figure(figsize=(16, 12))
    fig.suptitle('🚀 TRADING PERFORMANCE DASHBOARD', fontsize=18, fontweight='bold', y=0.98)

    # Create grid layout
    gs = fig.add_gridspec(3, 3, hspace=0.3, wspace=0.3)

    trades_df = pd.DataFrame(trades) if trades else pd.DataFrame()
    portfolio_df = pd.DataFrame(portfolio_value) if portfolio_value else pd.DataFrame()

    # 1. Equity Curve (top row, spans 2 columns)
    if not portfolio_df.empty:
        ax1 = fig.add_subplot(gs[0, :2])
        ax1.plot(portfolio_df.index, portfolio_df['total_value'], color='#2E86AB', linewidth=2)
        ax1.fill_between(portfolio_df.index, portfolio_df['total_value'], alpha=0.3, color='#2E86AB')
        ax1.set_title('📈 Equity Curve', fontsize=12, fontweight='bold')
        ax1.set_ylabel('Value (USDT)')
        ax1.grid(True, alpha=0.3)

    # 2. Win/Loss Pie (top right)
    if not trades_df.empty:
        ax2 = fig.add_subplot(gs[0, 2])
        wins = len(trades_df[trades_df['profit'] > 0])
        losses = len(trades_df[trades_df['profit'] <= 0])
        ax2.pie([wins, losses], labels=[f'Win ({wins})', f'Loss ({losses})'],
                colors=['#27AE60', '#E74C3C'], autopct='%1.0f%%',
                textprops={'fontsize': 10})
        ax2.set_title('🎯 Win Rate', fontsize=12, fontweight='bold')

    # 3. Profit per Trade (middle row)
    if not trades_df.empty:
        ax3 = fig.add_subplot(gs[1, :])
        colors = ['#27AE60' if p > 0 else '#E74C3C' for p in trades_df['profit']]
        ax3.bar(range(len(trades_df)), trades_df['profit'], color=colors, edgecolor='white', linewidth=0.5)
        ax3.axhline(y=0, color='black', linestyle='-', linewidth=1)

        # Cumulative line
        ax3_twin = ax3.twinx()
        cumulative = trades_df['profit'].cumsum()
        ax3_twin.plot(range(len(trades_df)), cumulative, color='#8E44AD',
                      linewidth=2, marker='o', markersize=3)
        ax3_twin.set_ylabel('Cumulative', color='#8E44AD')

        ax3.set_title('💰 Profit/Loss per Trade', fontsize=12, fontweight='bold')
        ax3.set_xlabel('Trade #')
        ax3.set_ylabel('Profit (USDT)')

    # 4. Profit by Level (bottom left)
    if not trades_df.empty:
        ax4 = fig.add_subplot(gs[2, 0])
        level_profit = trades_df.groupby('level')['profit'].sum().sort_index()
        colors = ['#27AE60' if p > 0 else '#E74C3C' for p in level_profit.values]
        ax4.barh(level_profit.index.astype(str), level_profit.values, color=colors)
        ax4.axvline(x=0, color='black', linestyle='-', linewidth=1)
        ax4.set_title('📊 Profit by Level', fontsize=12, fontweight='bold')
        ax4.set_xlabel('Profit (USDT)')

    # 5. Drawdown (bottom middle)
    if not portfolio_df.empty:
        ax5 = fig.add_subplot(gs[2, 1])
        peak = portfolio_df['total_value'].expanding(min_periods=1).max()
        drawdown = ((portfolio_df['total_value'] - peak) / peak) * 100
        ax5.fill_between(portfolio_df.index, 0, drawdown, color='#E74C3C', alpha=0.7)
        ax5.set_title('📉 Drawdown', fontsize=12, fontweight='bold')
        ax5.set_ylabel('Drawdown (%)')

    # 6. Summary Stats (bottom right)
    ax6 = fig.add_subplot(gs[2, 2])
    ax6.axis('off')

    if not trades_df.empty:
        total_profit = trades_df['profit'].sum()
        win_rate = (len(trades_df[trades_df['profit'] > 0]) / len(trades_df)) * 100
        avg_profit = trades_df['profit'].mean()

        stats_text = f"""
        📊 SUMMARY
        ──────────────
        Total Trades: {len(trades_df)}
        Win Rate: {win_rate:.1f}%

        💰 Total P/L: ${total_profit:,.2f}
        📈 Avg Trade: ${avg_profit:,.2f}
        🏆 Best: ${trades_df['profit'].max():,.2f}
        📉 Worst: ${trades_df['profit'].min():,.2f}
        """

        if not portfolio_df.empty:
            initial = portfolio_df['total_value'].iloc[0]
            final = portfolio_df['total_value'].iloc[-1]
            roi = ((final - initial) / initial) * 100
            stats_text += f"""
        💵 ROI: {roi:.2f}%
        """

        ax6.text(0.1, 0.5, stats_text, transform=ax6.transAxes,
                 fontsize=11, verticalalignment='center',
                 fontfamily='monospace',
                 bbox=dict(boxstyle='round', facecolor='#f8f9fa', edgecolor='#dee2e6'))

    plt.savefig(save_path, dpi=150, bbox_inches='tight', facecolor='white')
    print(f"✅ Dashboard saved to: {save_path}")

    if show:
        plt.show()

    return fig
