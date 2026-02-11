#!/usr/bin/env python3
"""
Trading Summary Demo - Shows tables and charts for trading performance

This script demonstrates the reporting and visualization features
with sample trading data.

Usage:
    python examples/trading_summary_demo.py
"""

import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
from datetime import datetime, timedelta
from src.reporting import TradingReporter
from src.charts import TradingCharts, create_dashboard


def generate_realistic_trades(num_trades=20, base_price=42000):
    """
    Generate realistic sample trades for demonstration

    Args:
        num_trades: Number of trades to generate
        base_price: Starting BTC price

    Returns:
        List of trade dictionaries
    """
    trades = []
    price = base_price

    print(f"\n🔄 Generating {num_trades} sample trades...")

    for i in range(num_trades):
        # Random ladder level (-1 to -5)
        level = random.randint(-5, -1)

        # Calculate prices based on level
        buy_price = price * (1 + level * 0.008)  # Lower buy for deeper levels
        sell_price = buy_price * random.uniform(1.01, 1.02)  # 1-2% profit target

        # Quantity increases at deeper levels (martingale style)
        quantity = 0.001 * (2 ** abs(level + 1))

        # Calculate costs and profit
        cost = buy_price * quantity * 1.001  # 0.1% fee
        revenue = sell_price * quantity * 0.999  # 0.1% fee

        # 75% chance of profitable trade
        if random.random() < 0.75:
            profit = revenue - cost
        else:
            # Simulate stop-loss or unfavorable close
            sell_price = buy_price * random.uniform(0.985, 0.995)
            revenue = sell_price * quantity * 0.999
            profit = revenue - cost

        trade = {
            'level': level,
            'buy_price': buy_price,
            'sell_price': sell_price,
            'quantity': quantity,
            'cost': cost,
            'revenue': revenue,
            'profit': profit,
            'roi': (profit / cost) * 100,
            'buy_time': datetime.now() - timedelta(hours=random.randint(1, 100)),
            'sell_time': datetime.now() - timedelta(hours=random.randint(0, 50))
        }

        trades.append(trade)

        # Price fluctuation for next trade
        price *= random.uniform(0.995, 1.005)

    return trades


def generate_portfolio_history(initial_capital=10000, days=30):
    """
    Generate sample portfolio value history

    Args:
        initial_capital: Starting capital
        days: Number of days of history

    Returns:
        List of portfolio value dictionaries
    """
    print(f"\n📈 Generating {days} days of portfolio history...")

    portfolio = []
    value = initial_capital

    for day in range(days * 24):  # Hourly data
        # Simulate realistic market movement
        trend = 0.0003  # Slight upward bias
        volatility = random.uniform(-0.015, 0.018)
        change = trend + volatility

        value *= (1 + change)

        # Ensure value doesn't go negative
        value = max(value, initial_capital * 0.5)

        portfolio.append({
            'timestamp': datetime.now() - timedelta(hours=days * 24 - day),
            'cash': value * random.uniform(0.2, 0.4),
            'positions_value': value * random.uniform(0.6, 0.8),
            'total_value': value
        })

    return portfolio


def main():
    """Main demo function"""
    print("=" * 60)
    print("🚀 TRADING SUMMARY DEMO")
    print("=" * 60)
    print("\nThis demo shows the reporting and visualization features")
    print("using simulated trading data.\n")

    # Generate sample data
    trades = generate_realistic_trades(num_trades=25)
    portfolio_history = generate_portfolio_history(initial_capital=10000, days=14)

    # ==========================================
    # PART 1: TABLE REPORTS
    # ==========================================
    print("\n" + "=" * 60)
    print("📋 PART 1: TABLE REPORTS")
    print("=" * 60)

    reporter = TradingReporter(trades=trades, portfolio_value=portfolio_history)

    # Print full report with all tables
    reporter.print_full_report(initial_capital=10000)

    # ==========================================
    # PART 2: INDIVIDUAL CHARTS
    # ==========================================
    print("\n" + "=" * 60)
    print("📊 PART 2: GENERATING CHARTS")
    print("=" * 60)

    charts = TradingCharts(trades=trades, portfolio_value=portfolio_history)

    # Create output directory
    output_dir = 'examples/charts_output'
    os.makedirs(output_dir, exist_ok=True)

    print("\nGenerating individual charts...")

    # Generate all charts (set show=False for non-interactive environments)
    charts.generate_all_charts(output_dir=output_dir, show=False)

    # ==========================================
    # PART 3: COMPREHENSIVE DASHBOARD
    # ==========================================
    print("\n" + "=" * 60)
    print("🎯 PART 3: CREATING DASHBOARD")
    print("=" * 60)

    dashboard_path = f'{output_dir}/trading_dashboard.png'
    create_dashboard(
        trades=trades,
        portfolio_value=portfolio_history,
        save_path=dashboard_path,
        show=False
    )

    # ==========================================
    # SUMMARY
    # ==========================================
    print("\n" + "=" * 60)
    print("✅ DEMO COMPLETE!")
    print("=" * 60)

    print(f"\n📁 Generated files in '{output_dir}/':")
    for f in os.listdir(output_dir):
        if f.endswith('.png'):
            print(f"   - {f}")

    print("\n💡 Tips:")
    print("   - Use TradingReporter for table summaries")
    print("   - Use TradingCharts for individual charts")
    print("   - Use create_dashboard() for a comprehensive view")
    print("\n📝 Example usage in your code:")
    print("""
    from src.reporting import TradingReporter
    from src.charts import TradingCharts, create_dashboard

    # After running backtest or live trading...
    reporter = TradingReporter(trades=backtester.trades,
                                portfolio_value=backtester.portfolio_value)
    reporter.print_full_report()

    charts = TradingCharts(trades=backtester.trades,
                           portfolio_value=backtester.portfolio_value)
    charts.generate_all_charts(output_dir='my_charts')
    """)


if __name__ == '__main__':
    main()
