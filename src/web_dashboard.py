"""
Realtime Web Dashboard - Flask + SocketIO for live trading display
"""
import logging
import threading
import time
from datetime import datetime
from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO, emit

logger = logging.getLogger(__name__)


class TradingDashboard:
    """Realtime web dashboard for trading bot"""

    def __init__(self, host='0.0.0.0', port=5000):
        self.host = host
        self.port = port
        self.app = Flask(__name__, template_folder='templates')
        self.app.config['SECRET_KEY'] = 'trading-bot-secret'
        self.socketio = SocketIO(self.app, cors_allowed_origins="*")

        # Trading data
        self.portfolio = None
        self.strategies = []
        self.order_manager = None
        self.client = None
        self.current_prices = {}

        # Setup routes
        self._setup_routes()
        self._setup_socketio()

        # Background update thread
        self.update_thread = None
        self.running = False

    def _setup_routes(self):
        """Setup Flask routes"""

        @self.app.route('/')
        def index():
            return render_template('dashboard.html')

        @self.app.route('/api/status')
        def api_status():
            return jsonify(self._get_status_data())

        @self.app.route('/api/trades')
        def api_trades():
            if self.portfolio:
                return jsonify(self.portfolio.trades_history)
            return jsonify([])

        @self.app.route('/api/positions')
        def api_positions():
            if self.portfolio:
                positions = []
                for strategy_name, data in self.portfolio.positions.items():
                    for pos in data['ladders']:
                        if pos['status'] == 'open':
                            positions.append({
                                'strategy': strategy_name,
                                'level': pos['level'],
                                'buy_price': pos['buy_price'],
                                'quantity': pos['quantity'],
                                'cost': pos['cost'],
                                'timestamp': pos['timestamp'].isoformat() if isinstance(pos['timestamp'], datetime) else str(pos['timestamp'])
                            })
                return jsonify(positions)
            return jsonify([])

    def _setup_socketio(self):
        """Setup SocketIO events"""

        @self.socketio.on('connect')
        def handle_connect():
            logger.info("Client connected to dashboard")
            emit('status_update', self._get_status_data())

        @self.socketio.on('disconnect')
        def handle_disconnect():
            logger.info("Client disconnected from dashboard")

        @self.socketio.on('request_update')
        def handle_request_update():
            emit('status_update', self._get_status_data())

    def _get_status_data(self):
        """Get current trading status data"""
        data = {
            'timestamp': datetime.now().isoformat(),
            'connected': self.client is not None,
            'strategies_count': len(self.strategies),
            'prices': self.current_prices,
            'portfolio': None,
            'strategies': [],
            'active_orders': [],
            'recent_trades': []
        }

        # Portfolio statistics
        if self.portfolio and self.current_prices:
            try:
                stats = self.portfolio.get_statistics(self.current_prices)
                data['portfolio'] = {
                    'initial_capital': stats['initial_capital'],
                    'total_value': round(stats['total_value'], 2),
                    'capital_free': round(stats['capital_free'], 2),
                    'capital_allocated': round(stats['capital_allocated'], 2),
                    'realized_pnl': round(stats['realized_pnl'], 2),
                    'unrealized_pnl': round(stats['unrealized_pnl'], 2),
                    'total_pnl': round(stats['total_pnl'], 2),
                    'roi_percent': round(stats['roi_percent'], 2),
                    'num_trades': stats['num_trades'],
                    'num_open_positions': stats['num_open_positions']
                }
            except Exception as e:
                logger.error(f"Error getting portfolio stats: {e}")

        # Strategies info
        for strategy in self.strategies:
            config = strategy.config
            data['strategies'].append({
                'name': config.get('name', 'Unknown'),
                'pair': config.get('pair', 'Unknown'),
                'num_ladders': config.get('num_ladders', 0),
                'base_gap_percent': config.get('base_gap_percent', 0)
            })

        # Active orders
        if self.order_manager:
            for order_id, order_data in self.order_manager.active_orders.items():
                data['active_orders'].append({
                    'order_id': order_id,
                    'type': order_data.get('type', 'Unknown'),
                    'symbol': order_data.get('symbol', 'Unknown'),
                    'price': order_data.get('price', 0),
                    'quantity': order_data.get('quantity', 0),
                    'level': order_data.get('ladder', 0)
                })

        # Recent trades (last 10)
        if self.portfolio:
            recent = self.portfolio.trades_history[-10:]
            for trade in recent:
                data['recent_trades'].append({
                    'strategy': trade.get('strategy', 'Unknown'),
                    'level': trade.get('level', 0),
                    'buy_price': trade.get('buy_price', 0),
                    'sell_price': trade.get('sell_price', 0),
                    'quantity': trade.get('quantity', 0),
                    'profit': round(trade.get('profit', 0), 2),
                    'timestamp': trade['timestamp'].isoformat() if isinstance(trade.get('timestamp'), datetime) else str(trade.get('timestamp', ''))
                })

        return data

    def set_trading_components(self, portfolio, strategies, order_manager, client):
        """Set trading components for dashboard to monitor"""
        self.portfolio = portfolio
        self.strategies = strategies
        self.order_manager = order_manager
        self.client = client

    def update_prices(self, prices):
        """Update current prices and broadcast to clients"""
        self.current_prices = prices
        self.socketio.emit('price_update', {
            'timestamp': datetime.now().isoformat(),
            'prices': prices
        })

    def broadcast_trade(self, trade):
        """Broadcast new trade to all connected clients"""
        self.socketio.emit('new_trade', {
            'timestamp': datetime.now().isoformat(),
            'trade': trade
        })

    def broadcast_order(self, order, event_type='order_placed'):
        """Broadcast order event to all connected clients"""
        self.socketio.emit(event_type, {
            'timestamp': datetime.now().isoformat(),
            'order': order
        })

    def _background_update(self):
        """Background thread for periodic updates"""
        while self.running:
            try:
                # Fetch current prices
                if self.client and self.strategies:
                    for strategy in self.strategies:
                        symbol = strategy.config['pair']
                        try:
                            price = self.client.get_current_price(symbol)
                            self.current_prices[symbol] = price
                        except Exception as e:
                            logger.error(f"Error fetching price for {symbol}: {e}")

                # Broadcast status update
                self.socketio.emit('status_update', self._get_status_data())

            except Exception as e:
                logger.error(f"Background update error: {e}")

            time.sleep(5)  # Update every 5 seconds

    def start(self, debug=False):
        """Start the dashboard server"""
        self.running = True

        # Start background update thread
        self.update_thread = threading.Thread(target=self._background_update, daemon=True)
        self.update_thread.start()

        logger.info(f"Dashboard starting at http://{self.host}:{self.port}")
        self.socketio.run(self.app, host=self.host, port=self.port, debug=debug, allow_unsafe_werkzeug=True)

    def start_async(self, debug=False):
        """Start dashboard in a background thread"""
        self.running = True

        def run_server():
            self.socketio.run(self.app, host=self.host, port=self.port, debug=debug, allow_unsafe_werkzeug=True)

        # Start server thread
        server_thread = threading.Thread(target=run_server, daemon=True)
        server_thread.start()

        # Start background update thread
        self.update_thread = threading.Thread(target=self._background_update, daemon=True)
        self.update_thread.start()

        logger.info(f"Dashboard running at http://{self.host}:{self.port}")
        return server_thread

    def stop(self):
        """Stop the dashboard server"""
        self.running = False
        logger.info("Dashboard stopped")


# Standalone demo mode
if __name__ == '__main__':
    from reporting import generate_sample_trades, generate_sample_portfolio_value

    # Create dashboard
    dashboard = TradingDashboard(port=5000)

    # Generate sample data
    sample_trades = generate_sample_trades()

    # Create a mock portfolio for demo
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
            self.trades_history = sample_trades

        def get_statistics(self, prices):
            return {
                'initial_capital': self.initial_capital,
                'capital_free': self.capital_free,
                'capital_allocated': self.capital_allocated,
                'total_value': 10250,
                'realized_pnl': 180,
                'unrealized_pnl': 70,
                'total_pnl': 250,
                'roi_percent': 2.5,
                'num_trades': len(self.trades_history),
                'num_open_positions': 2
            }

    class MockOrderManager:
        def __init__(self):
            self.active_orders = {
                'order1': {'type': 'BUY', 'symbol': 'BTCUSDT', 'price': 41000, 'quantity': 0.04, 'ladder': -3},
                'order2': {'type': 'SELL', 'symbol': 'BTCUSDT', 'price': 42500, 'quantity': 0.01, 'ladder': -1}
            }

    class MockStrategy:
        def __init__(self, name, pair):
            self.config = {'name': name, 'pair': pair, 'num_ladders': 6, 'base_gap_percent': 1.0}

    # Set mock components
    dashboard.portfolio = MockPortfolio()
    dashboard.order_manager = MockOrderManager()
    dashboard.strategies = [MockStrategy('BTC Conservative', 'BTCUSDT'), MockStrategy('ETH Balanced', 'ETHUSDT')]
    dashboard.current_prices = {'BTCUSDT': 42150, 'ETHUSDT': 2250}

    print("Starting demo dashboard...")
    print("Open http://localhost:5000 in your browser")
    dashboard.start(debug=True)
