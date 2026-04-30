"""
Main Entry Point for Binance Auto Rebalance Bot
"""
import argparse
import atexit
import logging
import os
import signal
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json
import requests.exceptions

from src.binance_client import BinanceClient
from src.strategy import Strategy
from src.portfolio import Portfolio
from src.order_manager import OrderManager
from src.martingale import MartingaleCalculator
from src.state_store import StateStore
from backtest.backtester import Backtester
from backtest.data_loader import DataLoader
from src.web_dashboard import TradingDashboard

# Force UTF-8 on stdout/stderr so log messages containing Unicode (e.g. the
# "→" arrow in date-range messages) don't blow up on Windows consoles whose
# default codepage is cp1252. errors='replace' is a safety net in case the
# stream can't be reconfigured (e.g. when stdout is not a real TTY).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

# Setup logging
Path('logs').mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/bot.log', encoding='utf-8'),
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
        end_dt = datetime.now(timezone.utc)
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

    # Session persistence: state file is per-mode so live and paper don't mix
    state_path = args.state_file or f"state/bot_state_{args.mode}.json"
    state_store = StateStore(state_path)

    if args.reset_state:
        backup = state_store.reset()
        if backup:
            logger.info(f"--reset-state: previous state backed up to {backup}")
        else:
            logger.info("--reset-state: no existing state file to reset")

    resuming = state_store.exists()

    # Get total capital
    balance = client.get_account_balance('USDT')
    total_capital = balance['free']
    logger.info(f"Available capital: ${total_capital:.2f} USDT")

    portfolio = Portfolio(total_capital)
    order_manager = OrderManager(client, portfolio)

    # Load strategies
    strategies = load_strategies(args.strategies)

    if resuming:
        logger.info(f"=== RESUMING SESSION from {state_path} ===")
        try:
            saved = state_store.load()
            state_store.apply(saved, portfolio, order_manager, strategies)
            logger.info(f"Restored: {len(portfolio.trades_history)} trades, "
                        f"{len(order_manager.active_orders)} active orders, "
                        f"realized P&L: ${portfolio.get_realized_pnl():.2f}")
        except Exception as e:
            logger.error(f"Failed to load state file: {e}. "
                         f"Use --reset-state to start fresh, or fix the file.")
            raise

        # Reconcile with the exchange to catch any fills that happened while offline
        # AND to detect orphan orders (placed but never recorded due to a mid-flight crash).
        order_manager.reconcile_with_exchange(strategies=strategies)
    else:
        # Fresh session: initialize each strategy with current prices.
        for strategy in strategies:
            current_price = client.get_current_price(strategy.config['pair'])
            strategy.update_prices(current_price)
            logger.info(f"Initialized {strategy.config['name']} at ${current_price:.2f}")

            # Log all planned ladder levels so user can see the plan
            order_manager.log_planned_ladders(strategy)
            if order_manager.is_distribution_mode(strategy):
                order_manager.log_planned_distribution(strategy)
            if order_manager.is_accumulation_enabled(strategy):
                order_manager.log_planned_sell_ladders(strategy)

        # Persist marker state BEFORE placing any orders. If we crash between an
        # API call and recording the response, on next start `resuming` will be
        # True and reconcile_with_exchange() will adopt orphan orders from the
        # exchange rather than re-placing duplicates.
        try:
            state_store.save(portfolio, order_manager, strategies)
        except Exception as e:
            logger.error(f"Failed to save initial state (continuing): {e}")

        # Now place initial order(s) based on order_placement.mode
        for strategy in strategies:
            current_price = client.get_current_price(strategy.config['pair'])
            if order_manager.is_distribution_mode(strategy):
                logger.info(f"{strategy.config['name']}: Distribution mode - children will be "
                           f"placed as price approaches each child price (respecting open-order cap)")
                order_manager.place_distribution_orders(strategy, current_price)
            elif order_manager.is_sequential_mode(strategy):
                logger.info(f"{strategy.config['name']}: Sequential mode - placing first order only, "
                           f"next orders will be placed as price approaches each level")
                order_manager.place_next_sequential_order(strategy, current_price)
            else:
                order_manager.place_ladder_buy_orders(strategy, current_price)

            # SELL-side accumulation runs in parallel with whichever BUY mode
            # is active. Place initial accumulation SELLs immediately.
            if order_manager.is_accumulation_enabled(strategy):
                order_manager.place_accumulation_orders(strategy, current_price)

        # Persist again now that orders have been placed
        try:
            state_store.save(portfolio, order_manager, strategies)
        except Exception as e:
            logger.error(f"Failed to save initial state (continuing): {e}")

    # Optional embedded dashboard: shares portfolio/order_manager/strategies/client
    # with the trading loop so the UI reflects live state instead of a fresh empty
    # session. Runs in a background thread (Flask/SocketIO).
    dashboard = None
    if getattr(args, 'with_dashboard', False):
        dashboard = TradingDashboard(host='0.0.0.0', port=args.port)
        dashboard.set_trading_components(portfolio, strategies, order_manager, client)
        for strategy in strategies:
            symbol = strategy.config['pair']
            try:
                dashboard.current_prices[symbol] = client.get_current_price(symbol)
            except Exception as e:
                logger.warning(f"Dashboard: could not seed price for {symbol}: {e}")
        dashboard.start_async(debug=False)
        print(f"\n{'='*60}")
        print("DASHBOARD ENABLED")
        print(f"Open http://localhost:{args.port} in your browser")
        print(f"{'='*60}\n")

    # Main trading loop
    logger.info("Starting trading loop...")
    check_interval = 30  # Check more frequently for sequential mode responsiveness
    max_network_backoff = 300  # Cap backoff at 5 minutes
    # If get_current_price falls back to cache during a network outage and the
    # cached value is older than this, skip placing new orders this iteration to
    # avoid trading on a ghost price.
    stale_price_threshold = 60
    consecutive_errors = 0
    current_prices = {}

    def _save_state(label):
        try:
            state_store.save(portfolio, order_manager, strategies)
        except Exception as e:
            logger.error(f"State save failed ({label}): {e}")

    # --- Crash-safe shutdown wiring ---------------------------------------
    # Save on any path the OS gives us a chance to handle: SIGTERM (taskkill,
    # docker stop, systemd), SIGBREAK (Windows console close / Ctrl+Break),
    # and atexit (any normal interpreter shutdown). Hard power loss can't be
    # caught here — that case relies on the per-iteration save plus
    # reconcile_with_exchange() on the next start.
    _shutdown_saved = {"done": False}

    def _atexit_save():
        if _shutdown_saved["done"]:
            return
        _shutdown_saved["done"] = True
        logger.info("atexit: saving session state...")
        _save_state("atexit")

    def _signal_save_and_exit(signum, frame):
        # Re-raise as KeyboardInterrupt so the existing try/except path runs
        # the same shutdown logic (including a final save + log).
        signame = getattr(signal, "Signals", None)
        try:
            label = signal.Signals(signum).name
        except Exception:
            label = str(signum)
        logger.info(f"Received signal {label}; initiating graceful shutdown")
        _save_state(f"signal-{label}")
        _shutdown_saved["done"] = True
        # Convert to KeyboardInterrupt to flow into the unified shutdown path.
        raise KeyboardInterrupt()

    atexit.register(_atexit_save)
    # SIGTERM is sent by `taskkill <pid>` (no /F), `docker stop`, systemd, etc.
    try:
        signal.signal(signal.SIGTERM, _signal_save_and_exit)
    except (AttributeError, ValueError):
        pass
    # SIGBREAK fires on Windows console Ctrl+Break and (best-effort) on
    # console-window close. Available only on Windows.
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, _signal_save_and_exit)
        except (ValueError, OSError):
            pass

    try:
        last_heartbeat_save = time.time()
        heartbeat_interval = 60  # Force a checkpoint at least once per minute

        while True:
            state_dirty = False
            try:
                # Check filled orders
                filled = order_manager.check_filled_orders()

                # Refresh prices first so any recovery-routed SELLs use a live
                # market price for their drawdown decision. get_current_price
                # falls back to a cached value during a network outage; the age
                # check below decides whether the cached value is fresh enough
                # to base trading decisions on.
                current_prices = {}
                max_price_age = 0.0
                for strategy in strategies:
                    symbol = strategy.config['pair']
                    current_prices[symbol] = client.get_current_price(symbol)
                    age = client.get_price_age(symbol)
                    if age is not None and age > max_price_age:
                        max_price_age = age

                prices_stale = max_price_age > stale_price_threshold

                # Mirror the loop's freshly fetched prices into the embedded
                # dashboard so the UI doesn't have to re-hit the API itself.
                if dashboard is not None:
                    dashboard.current_prices.update(current_prices)

                if prices_stale:
                    logger.warning(
                        f"Prices stale ({max_price_age:.0f}s > {stale_price_threshold}s "
                        f"threshold) — skipping new order placement this iteration"
                    )

                if filled:
                    state_dirty = True
                    logger.info(f"Processed {len(filled)} filled orders")

                    # Place corresponding sell orders for filled buys. When
                    # prices are stale we pass current_price=None so the
                    # recovery-eligibility check is skipped (it would otherwise
                    # use a ghost price for drawdown comparison); the SELL
                    # itself uses the child's precomputed sell_price.
                    for order_data in filled:
                        if order_data['type'] == 'BUY':
                            # Find the strategy
                            for strategy in strategies:
                                if strategy.config['name'] == order_data['strategy']:
                                    child = order_data.get('child')
                                    if child is not None:
                                        cp = (None if prices_stale
                                              else current_prices.get(strategy.config['pair']))
                                        order_manager._place_child_sell(
                                            strategy, child, order_data['ladder'],
                                            order_data.get('filled_qty', child['qty']),
                                            current_price=cp,
                                        )
                                    else:
                                        order_manager.place_sell_order(strategy, order_data['ladder'])
                        elif order_data['type'] == 'SELL_ACCUM' and order_data.get('needs_buyback'):
                            # Accumulation SELL just filled: place the BUY_BACK.
                            for strategy in strategies:
                                if strategy.config['name'] == order_data['strategy']:
                                    sell_ladder = order_data.get('sell_ladder') or {}
                                    order_manager._place_buyback_buy(
                                        strategy, sell_ladder,
                                        filled_qty=sell_ladder.get('filled_qty', 0),
                                        filled_sell_price=sell_ladder.get('filled_price', 0),
                                    )
                                    break

                stats = portfolio.get_statistics(current_prices)

                if len(filled) > 0:  # Only log when there's activity
                    logger.info(f"Portfolio: ${stats['total_value']:.2f} | "
                               f"P&L: ${stats['total_pnl']:.2f} ({stats['roi_percent']:.2f}%) | "
                               f"Open: {stats['num_open_positions']} | "
                               f"Trades: {stats['num_trades']}")

                if not prices_stale:
                    # Sequential mode: check if price is approaching next levels
                    for strategy in strategies:
                        cp = current_prices[strategy.config['pair']]
                        open_before = len(order_manager.active_orders)
                        if order_manager.is_distribution_mode(strategy):
                            order_manager.place_distribution_orders(strategy, cp)
                        elif order_manager.is_sequential_mode(strategy):
                            order_manager.place_next_sequential_order(strategy, cp)
                        # Accumulation runs in parallel: promote pending SELL
                        # ladders whose price is in proximity. Re-place any
                        # buybacks that were deferred (e.g. price-filter retry).
                        if order_manager.is_accumulation_enabled(strategy):
                            try:
                                order_manager.place_accumulation_orders(strategy, cp)
                            except RuntimeError as e:
                                # Insufficient coin balance: surfaced loudly,
                                # don't kill the loop — let user fix wallet.
                                logger.error(f"Accumulation halted: {e}")
                        # Recovery scan: roll any far-from-market SELLs into the
                        # merged recovery lot so they don't sit unfillable, and
                        # ensure a merged SELL exists whenever the lot has
                        # pooled children. Throttled internally.
                        rolled = order_manager.check_stale_sells(strategy, cp)
                        if rolled or len(order_manager.active_orders) != open_before:
                            state_dirty = True

                    # Auto-restart: when all positions are closed and no active orders for a strategy
                    for strategy in strategies:
                        strategy_has_orders = any(
                            od['strategy'] == strategy.config['name']
                            for od in order_manager.active_orders.values()
                        )
                        buy_done = strategy.all_ladders_closed()
                        sell_done = (not strategy.is_accumulation_enabled()
                                     or strategy.all_sell_ladders_closed())
                        if not strategy_has_orders and buy_done and sell_done:
                            current_price = current_prices[strategy.config['pair']]
                            logger.info(f"=== AUTO-RESTART: {strategy.config['name']} cycle complete, "
                                        f"starting new cycle at ${current_price:.2f} ===")
                            strategy.reset_ladders()
                            strategy.reset_sell_ladders()
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
                            if order_manager.is_accumulation_enabled(strategy):
                                order_manager.log_planned_sell_ladders(strategy)
                                try:
                                    order_manager.place_accumulation_orders(strategy, current_price)
                                except RuntimeError as e:
                                    logger.error(f"Accumulation halt on restart: {e}")
                            state_dirty = True

                # Persist on any state change OR at least once per heartbeat
                # interval, so an unexpected shutdown (power loss, force-kill)
                # never loses more than ~heartbeat_interval seconds of progress.
                now_ts = time.time()
                if state_dirty:
                    _save_state("iteration")
                    last_heartbeat_save = now_ts
                elif now_ts - last_heartbeat_save >= heartbeat_interval:
                    _save_state("heartbeat")
                    last_heartbeat_save = now_ts

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
        # Save state BEFORE cancelling so a resume can decide what to do.
        # We do not auto-cancel here anymore because the user typically wants
        # to resume the same session next launch. Use --reset-state plus a
        # manual cancel on Binance if you really want a clean slate.
        _save_state("shutdown")
        logger.info(f"State saved to {state_path}. Resume with the same command "
                    f"(or pass --reset-state to start fresh).")

        # Print final statistics
        if current_prices:
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
    parser.add_argument('--with-dashboard', action='store_true',
                       help='When used with --mode live or --mode paper, also run the '
                            'web dashboard in a background thread sharing the same '
                            'portfolio/orders. Open http://localhost:<--port> to view.')
    parser.add_argument('--state-file',
                       help='Path to session state file (default: state/bot_state_<mode>.json). '
                            'Used by live and paper modes to persist trades, positions, and the '
                            'distribution / sequential queue across restarts.')
    parser.add_argument('--reset-state', action='store_true',
                       help='Discard saved session state before starting (forget previous '
                            'orders, positions, and P&L). The old state file is renamed to '
                            '*.reset.<timestamp> as a backup.')

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
