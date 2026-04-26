"""
Main Entry Point for Binance Auto Rebalance Bot
"""
import argparse
import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
import json
import requests.exceptions

from src.binance_client import BinanceClient
from src.strategy import Strategy
from src.portfolio import Portfolio
from src.order_manager import OrderManager
from src.martingale import MartingaleCalculator
from backtest.backtester import Backtester
from backtest.data_loader import DataLoader
from src.web_dashboard import TradingDashboard

# Setup logging
Path('logs').mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


def load_strategies(strategy_names):
    """Load strategy configurations"""
    strategies = []
    config_dir = Path('config/strategies')

    if 'all' in strategy_names:
        # Load all enabled strategies
        for config_file in config_dir.glob('*.json'):
            strategy = Strategy(config_file)
            if strategy.config.get('enabled', True):
                strategies.append(strategy)
    else:
        # Load specific strategies
        for name in strategy_names:
            config_file = config_dir / f"{name}.json"
            if config_file.exists():
                strategies.append(Strategy(config_file))
            else:
                logger.warning(f"Strategy config not found: {config_file}")

    logger.info(f"Loaded {len(strategies)} strategies")
    return strategies


def run_backtest(args):
    """Run backtest mode"""
    logger.info("=== BACKTEST MODE ===")

    # Initialize Binance client (for historical data only)
    client = BinanceClient(testnet=True)
    data_loader = DataLoader(client)

    # Resolve the date range: --days N takes precedence and runs N days back
    # from today; otherwise fall back to --start / --end.
    if args.days is not None:
        end_dt = datetime.utcnow()
        start_dt = end_dt - timedelta(days=int(args.days))
        start_date = start_dt.strftime('%Y-%m-%d')
        end_date = end_dt.strftime('%Y-%m-%d')
        logger.info(f"Backtest window: last {args.days} days ({start_date} → {end_date})")
    else:
        start_date = args.start
        end_date = args.end
        logger.info(f"Backtest window: {start_date} → {end_date}")

    # Load strategies
    strategies = load_strategies(args.strategies)

    for strategy in strategies:
        logger.info(f"\n{'='*60}")
        logger.info(f"Backtesting: {strategy.config['name']}")
        logger.info(f"{'='*60}")

        # Distribution mode needs finer granularity so each child's narrow
        # price band can actually be resolved inside the candle.
        interval = args.interval or ('5m' if strategy.is_distribution_mode() else '1h')

        # Load historical data
        symbol = strategy.config['pair']
        data = data_loader.load_historical_data(
            symbol=symbol,
            interval=interval,
            start_date=start_date,
            end_date=end_date
        )

        # Run backtest
        backtester = Backtester(strategy, data)
        report = backtester.run()

        # Print report
        print(json.dumps(report, indent=2, default=str))

        # Save report
        report_file = f"logs/backtest_{strategy.config['name'].replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        backtester.save_report(report_file)


def run_live_trading(args):
    """Run live trading mode"""
    logger.info("=== LIVE TRADING MODE ===")
    logger.warning("WARNING: LIVE TRADING IS ACTIVE - REAL MONEY AT RISK")

    # Initialize components
    client = BinanceClient(testnet=False)

    # Get total capital
    balance = client.get_account_balance('USDT')
    total_capital = balance['free']
    logger.info(f"Available capital: ${total_capital:.2f} USDT")

    portfolio = Portfolio(total_capital)
    order_manager = OrderManager(client, portfolio)

    # Load strategies
    strategies = load_strategies(args.strategies)

    # Initialize each strategy with current prices
    for strategy in strategies:
        current_price = client.get_current_price(strategy.config['pair'])
        strategy.update_prices(current_price)
        logger.info(f"Initialized {strategy.config['name']} at ${current_price:.2f}")

        # Log all planned ladder levels so user can see the plan
        order_manager.log_planned_ladders(strategy)

        # Place initial order(s) based on order_placement.mode
        if order_manager.is_distribution_mode(strategy):
            order_manager.log_planned_distribution(strategy)
            logger.info(f"{strategy.config['name']}: Distribution mode - children will be "
                       f"placed as price approaches each child price (respecting open-order cap)")
            order_manager.place_distribution_orders(strategy, current_price)
        elif order_manager.is_sequential_mode(strategy):
            logger.info(f"{strategy.config['name']}: Sequential mode - placing first order only, "
                       f"next orders will be placed as price approaches each level")
            order_manager.place_next_sequential_order(strategy, current_price)
        else:
            order_manager.place_ladder_buy_orders(strategy, current_price)

    # Main trading loop
    logger.info("Starting trading loop...")
    check_interval = 30  # Check more frequently for sequential mode responsiveness
    max_network_backoff = 300  # Cap backoff at 5 minutes
    consecutive_errors = 0
    try:
        while True:
            try:
                # Check filled orders
                filled = order_manager.check_filled_orders()

                if filled:
                    logger.info(f"Processed {len(filled)} filled orders")

                    # Place corresponding sell orders for filled buys
                    for order_data in filled:
                        if order_data['type'] == 'BUY':
                            # Find the strategy
                            for strategy in strategies:
                                if strategy.config['name'] == order_data['strategy']:
                                    child = order_data.get('child')
                                    if child is not None:
                                        order_manager._place_child_sell(
                                            strategy, child, order_data['ladder'],
                                            order_data.get('filled_qty', child['qty'])
                                        )
                                    else:
                                        order_manager.place_sell_order(strategy, order_data['ladder'])

                # Update portfolio statistics
                current_prices = {}
                for strategy in strategies:
                    symbol = strategy.config['pair']
                    current_prices[symbol] = client.get_current_price(symbol)

                stats = portfolio.get_statistics(current_prices)

                if len(filled) > 0:  # Only log when there's activity
                    logger.info(f"Portfolio: ${stats['total_value']:.2f} | "
                               f"P&L: ${stats['total_pnl']:.2f} ({stats['roi_percent']:.2f}%) | "
                               f"Open: {stats['num_open_positions']} | "
                               f"Trades: {stats['num_trades']}")

                # Sequential mode: check if price is approaching next levels
                for strategy in strategies:
                    cp = current_prices[strategy.config['pair']]
                    if order_manager.is_distribution_mode(strategy):
                        order_manager.place_distribution_orders(strategy, cp)
                    elif order_manager.is_sequential_mode(strategy):
                        order_manager.place_next_sequential_order(strategy, cp)

                # Auto-restart: when all positions are closed and no active orders for a strategy
                for strategy in strategies:
                    strategy_has_orders = any(
                        od['strategy'] == strategy.config['name']
                        for od in order_manager.active_orders.values()
                    )
                    if not strategy_has_orders and strategy.all_ladders_closed():
                        current_price = current_prices[strategy.config['pair']]
                        logger.info(f"=== AUTO-RESTART: {strategy.config['name']} cycle complete, "
                                    f"starting new cycle at ${current_price:.2f} ===")
                        strategy.reset_ladders()
                        strategy.update_prices(current_price)
                        order_manager.log_planned_ladders(strategy)
                        if order_manager.is_distribution_mode(strategy):
                            order_manager.reset_distribution_state(strategy.config['name'])
                            order_manager.log_planned_distribution(strategy)
                            order_manager.place_distribution_orders(strategy, current_price)
                        elif order_manager.is_sequential_mode(strategy):
                            order_manager.reset_sequential_state(strategy.config['name'])
                            order_manager.place_next_sequential_order(strategy, current_price)
                        else:
                            order_manager.place_ladder_buy_orders(strategy, current_price)

                # Reset error counter on successful iteration
                consecutive_errors = 0

            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ReadTimeout,
                    ConnectionError,
                    OSError) as e:
                consecutive_errors += 1
                backoff = min(check_interval * (2 ** consecutive_errors), max_network_backoff)
                logger.warning(f"Network error in trading loop (attempt {consecutive_errors}), "
                               f"retrying in {backoff}s: {e}")
                time.sleep(backoff)
                continue

            # Sleep before next iteration
            time.sleep(check_interval)

    except KeyboardInterrupt:
        logger.info("\nShutting down gracefully...")
        order_manager.cancel_all_orders()
        logger.info("All orders cancelled")

        # Print final statistics
        stats = portfolio.get_statistics(current_prices)
        print(f"\n{'='*60}")
        print("FINAL STATISTICS")
        print(f"{'='*60}")
        print(json.dumps(stats, indent=2))


def run_paper_trading(args):
    """Run paper trading mode (simulated with live data)"""
    logger.info("=== PAPER TRADING MODE ===")

    # Similar to live trading but with simulated orders
    client = BinanceClient(testnet=True)

    logger.info("Paper trading uses testnet - no real money at risk")
    run_live_trading(args)


def run_dashboard(args):
    """Run realtime web dashboard for monitoring"""
    logger.info("=== DASHBOARD MODE ===")

    # Initialize dashboard
    dashboard = TradingDashboard(host='0.0.0.0', port=args.port)

    # If demo mode, use mock data
    if args.demo:
        logger.info("Running dashboard in DEMO mode with sample data")
        from src.reporting import generate_sample_trades

        # Create mock components
        class MockPortfolio:
            def __init__(self):
                self.initial_capital = 10000
                self.capital_free = 8500
                self.capital_allocated = 1500
                self.positions = {
                    'BTC Conservative': {
                        'ladders': [
                            {'level': -1, 'buy_price': 42000, 'quantity': 0.01, 'cost': 420, 'status': 'open', 'timestamp': datetime.now()},
                            {'level': -2, 'buy_price': 41500, 'quantity': 0.02, 'cost': 830, 'status': 'open', 'timestamp': datetime.now()}
                        ]
                    }
                }
                self.trades_history = generate_sample_trades()

            def get_statistics(self, prices):
                import random
                base_pnl = 250 + random.uniform(-50, 50)
                return {
                    'initial_capital': self.initial_capital,
                    'capital_free': self.capital_free,
                    'capital_allocated': self.capital_allocated,
                    'total_value': 10000 + base_pnl,
                    'realized_pnl': 180,
                    'unrealized_pnl': base_pnl - 180,
                    'total_pnl': base_pnl,
                    'roi_percent': base_pnl / 100,
                    'num_trades': len(self.trades_history),
                    'num_open_positions': 2
                }

        class MockOrderManager:
            def __init__(self):
                self.active_orders = {
                    'order1': {'type': 'BUY', 'symbol': 'BTCUSDT', 'price': 41000, 'quantity': 0.04, 'ladder': -3},
                    'order2': {'type': 'SELL', 'symbol': 'BTCUSDT', 'price': 42500, 'quantity': 0.01, 'ladder': -1},
                    'order3': {'type': 'BUY', 'symbol': 'ETHUSDT', 'price': 2200, 'quantity': 0.5, 'ladder': -2}
                }

        class MockStrategy:
            def __init__(self, name, pair):
                self.config = {'name': name, 'pair': pair, 'num_ladders': 6, 'base_gap_percent': 1.0}

        # Set mock components
        dashboard.portfolio = MockPortfolio()
        dashboard.order_manager = MockOrderManager()
        dashboard.strategies = [
            MockStrategy('BTC Conservative', 'BTCUSDT'),
            MockStrategy('ETH Balanced', 'ETHUSDT'),
            MockStrategy('BNB Aggressive', 'BNBUSDT')
        ]
        dashboard.current_prices = {'BTCUSDT': 42150, 'ETHUSDT': 2250, 'BNBUSDT': 315}

        print(f"\n{'='*60}")
        print("BINANCE AUTO REBALANCE BOT - LIVE DASHBOARD")
        print(f"{'='*60}")
        print(f"Dashboard URL: http://localhost:{args.port}")
        print(f"Mode: DEMO (sample data)")
        print(f"{'='*60}\n")

        dashboard.start(debug=False)

    else:
        # Connect to real trading bot
        logger.info("Connecting to live trading system...")

        # Initialize Binance client
        use_testnet = args.mode == 'paper'
        client = BinanceClient(testnet=use_testnet)

        # Get initial balance
        balance = client.get_account_balance('USDT')
        total_capital = balance['free']
        logger.info(f"Available capital: ${total_capital:.2f} USDT")

        portfolio = Portfolio(total_capital)
        order_manager = OrderManager(client, portfolio)

        # Load strategies
        strategies = load_strategies(args.strategies)

        # Initialize strategies with current prices
        for strategy in strategies:
            current_price = client.get_current_price(strategy.config['pair'])
            strategy.update_prices(current_price)
            dashboard.current_prices[strategy.config['pair']] = current_price

        # Set dashboard components
        dashboard.set_trading_components(portfolio, strategies, order_manager, client)

        print(f"\n{'='*60}")
        print("BINANCE AUTO REBALANCE BOT - LIVE DASHBOARD")
        print(f"{'='*60}")
        print(f"Dashboard URL: http://localhost:{args.port}")
        print(f"Mode: {'PAPER TRADING' if use_testnet else 'LIVE TRADING'}")
        print(f"Strategies: {', '.join(s.config['name'] for s in strategies)}")
        print(f"{'='*60}\n")

        dashboard.start(debug=False)


def main():
    parser = argparse.ArgumentParser(description='Binance Auto Rebalance Bot')
    parser.add_argument('--mode', choices=['live', 'paper', 'backtest', 'dashboard'], required=True,
                       help='Trading mode')
    parser.add_argument('--strategies', nargs='+', default=['all'],
                       help='Strategy names or "all"')
    parser.add_argument('--start', help='Backtest start date (YYYY-MM-DD)')
    parser.add_argument('--end', help='Backtest end date (YYYY-MM-DD)')
    parser.add_argument('--days', type=int,
                       help='Backtest: run the last N days (overrides --start/--end)')
    parser.add_argument('--interval',
                       help='Backtest candle interval (e.g. 1m, 5m, 15m, 1h, 4h, 1d). '
                            'Default: 1h for normal/sequential, 5m for distribution.')
    parser.add_argument('--port', type=int, default=5000,
                       help='Dashboard web server port (default: 5000)')
    parser.add_argument('--demo', action='store_true',
                       help='Run dashboard in demo mode with sample data')

    args = parser.parse_args()

    # Create necessary directories
    Path('data/historical').mkdir(parents=True, exist_ok=True)

    # Run appropriate mode
    if args.mode == 'backtest':
        if args.days is None and (not args.start or not args.end):
            parser.error("Backtest mode requires either --days N or both --start and --end")
        run_backtest(args)
    elif args.mode == 'live':
        run_live_trading(args)
    elif args.mode == 'paper':
        run_paper_trading(args)
    elif args.mode == 'dashboard':
        run_dashboard(args)


if __name__ == '__main__':
    main()
