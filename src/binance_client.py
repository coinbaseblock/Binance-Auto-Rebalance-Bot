"""
Binance API Client Wrapper
"""
import os
import time
from decimal import Decimal, ROUND_DOWN
from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv
import logging
import requests.exceptions

logger = logging.getLogger(__name__)


class BinanceClient:
    def __init__(self, testnet=False):
        load_dotenv()

        api_key = os.getenv('BINANCE_API_KEY')
        api_secret = os.getenv('BINANCE_API_SECRET')

        if not api_key or not api_secret:
            raise ValueError("Binance API credentials not found in .env file")

        self.client = Client(api_key, api_secret, testnet=testnet)
        self.testnet = testnet
        self._symbol_filters = {}  # Cache for symbol filter info

        logger.info(f"Binance client initialized (testnet={testnet})")

    def get_symbol_filters(self, symbol):
        """Fetch and cache symbol exchange filters (tick size, lot size, min notional, percent price)"""
        if symbol in self._symbol_filters:
            return self._symbol_filters[symbol]

        try:
            info = self.client.get_symbol_info(symbol)
            if not info:
                raise ValueError(f"Symbol {symbol} not found on exchange")

            filters = {}
            for f in info['filters']:
                if f['filterType'] == 'PRICE_FILTER':
                    filters['tick_size'] = f['tickSize']
                    filters['min_price'] = f['minPrice']
                    filters['max_price'] = f['maxPrice']
                elif f['filterType'] == 'LOT_SIZE':
                    filters['step_size'] = f['stepSize']
                    filters['min_qty'] = f['minQty']
                    filters['max_qty'] = f['maxQty']
                elif f['filterType'] == 'NOTIONAL':
                    filters['min_notional'] = f.get('minNotional', '0')
                elif f['filterType'] == 'PERCENT_PRICE_BY_SIDE':
                    filters['bid_multiplier_down'] = float(f['bidMultiplierDown'])
                    filters['bid_multiplier_up'] = float(f['bidMultiplierUp'])
                    filters['ask_multiplier_down'] = float(f['askMultiplierDown'])
                    filters['ask_multiplier_up'] = float(f['askMultiplierUp'])

            self._symbol_filters[symbol] = filters
            logger.info(f"Symbol filters for {symbol}: tick_size={filters.get('tick_size')}, step_size={filters.get('step_size')}")
            return filters
        except BinanceAPIException as e:
            logger.error(f"Error fetching symbol info for {symbol}: {e}")
            raise

    @staticmethod
    def _round_to_step(value, step_size):
        """Round a value down to the nearest step size.
        Returns Decimal to preserve exact precision for Binance API string formatting.

        Note: Binance returns tick_size/step_size with trailing zeros (e.g. '0.01000000').
        We must normalize() to strip trailing zeros before quantize(), otherwise
        quantize() matches decimal places (8) instead of rounding to the step (0.01).
        """
        step = Decimal(str(step_size)).normalize()
        val = Decimal(str(value))
        return val.quantize(step, rounding=ROUND_DOWN)

    def round_price(self, symbol, price):
        """Round price to comply with PRICE_FILTER tick size"""
        filters = self.get_symbol_filters(symbol)
        tick_size = filters.get('tick_size', '0.01')
        return self._round_to_step(price, tick_size)

    def round_quantity(self, symbol, quantity):
        """Round quantity to comply with LOT_SIZE step size"""
        filters = self.get_symbol_filters(symbol)
        step_size = filters.get('step_size', '0.001')
        return self._round_to_step(quantity, step_size)

    def check_percent_price_filter(self, symbol, side, price):
        """Check if price passes PERCENT_PRICE_BY_SIDE filter.
        Returns (ok, reason) tuple."""
        filters = self.get_symbol_filters(symbol)
        bid_down = filters.get('bid_multiplier_down')
        if bid_down is None:
            return True, ""  # Filter not present for this symbol

        # Get weighted average price used by Binance for this filter
        try:
            avg_price = float(self.client.get_avg_price(symbol=symbol)['price'])
        except Exception:
            return True, ""  # Can't validate, let exchange decide

        if side == 'BUY':
            min_price = avg_price * filters['bid_multiplier_down']
            max_price = avg_price * filters['bid_multiplier_up']
        else:
            min_price = avg_price * filters['ask_multiplier_down']
            max_price = avg_price * filters['ask_multiplier_up']

        if price < min_price or price > max_price:
            return False, (f"price ${price:.2f} outside PERCENT_PRICE_BY_SIDE range "
                          f"[${min_price:.2f}, ${max_price:.2f}] (avg=${avg_price:.2f})")
        return True, ""

    def get_current_price(self, symbol, retries=3, backoff=2):
        """Get current market price for a symbol, with retry on network errors"""
        for attempt in range(retries + 1):
            try:
                ticker = self.client.get_symbol_ticker(symbol=symbol)
                return float(ticker['price'])
            except BinanceAPIException as e:
                logger.error(f"Error getting price for {symbol}: {e}")
                raise
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ReadTimeout) as e:
                if attempt < retries:
                    wait = backoff ** (attempt + 1)
                    logger.warning(f"Network error getting price for {symbol} "
                                   f"(attempt {attempt + 1}/{retries + 1}), retrying in {wait}s: {e}")
                    time.sleep(wait)
                else:
                    logger.error(f"Network error getting price for {symbol} after {retries + 1} attempts: {e}")
                    raise

    def get_account_balance(self, asset='USDT'):
        """Get account balance for specific asset"""
        try:
            balance = self.client.get_asset_balance(asset=asset)
            return {
                'free': float(balance['free']),
                'locked': float(balance['locked']),
                'total': float(balance['free']) + float(balance['locked'])
            }
        except BinanceAPIException as e:
            logger.error(f"Error getting balance for {asset}: {e}")
            raise

    def create_limit_order(self, symbol, side, quantity, price):
        """Create a limit order with automatic precision rounding"""
        try:
            # Round price and quantity to comply with exchange filters
            rounded_price = self.round_price(symbol, price)
            rounded_qty = self.round_quantity(symbol, quantity)

            if rounded_qty <= 0:
                raise ValueError(f"Quantity {quantity} rounds to 0 for {symbol} (step_size too large)")
            if rounded_price <= 0:
                raise ValueError(f"Price {price} rounds to 0 for {symbol} (tick_size too large)")

            logger.info(f"Order precision: price {price} -> '{rounded_price}', qty {quantity} -> '{rounded_qty}'")

            order = self.client.create_order(
                symbol=symbol,
                side=side,  # 'BUY' or 'SELL'
                type='LIMIT',
                timeInForce='GTC',
                quantity=str(rounded_qty),
                price=str(rounded_price)
            )
            logger.info(f"Order created: {side} {rounded_qty} {symbol} @ {rounded_price}")
            return order
        except BinanceAPIException as e:
            logger.error(f"Error creating order: {e}")
            raise

    def create_market_order(self, symbol, side, quantity):
        """Create a market order with automatic precision rounding"""
        try:
            rounded_qty = self.round_quantity(symbol, quantity)

            if rounded_qty <= 0:
                raise ValueError(f"Quantity {quantity} rounds to 0 for {symbol} (step_size too large)")

            order = self.client.create_order(
                symbol=symbol,
                side=side,
                type='MARKET',
                quantity=str(rounded_qty)
            )
            logger.info(f"Market order created: {side} {rounded_qty} {symbol}")
            return order
        except BinanceAPIException as e:
            logger.error(f"Error creating market order: {e}")
            raise

    def get_open_orders(self, symbol=None):
        """Get all open orders"""
        try:
            return self.client.get_open_orders(symbol=symbol)
        except BinanceAPIException as e:
            logger.error(f"Error getting open orders: {e}")
            raise

    def cancel_order(self, symbol, order_id):
        """Cancel an order"""
        try:
            result = self.client.cancel_order(symbol=symbol, orderId=order_id)
            logger.info(f"Order cancelled: {order_id}")
            return result
        except BinanceAPIException as e:
            logger.error(f"Error cancelling order: {e}")
            raise

    def get_historical_klines(self, symbol, interval, start_str, end_str=None):
        """Get historical klines/candlestick data"""
        try:
            klines = self.client.get_historical_klines(
                symbol, interval, start_str, end_str
            )
            return klines
        except BinanceAPIException as e:
            logger.error(f"Error getting historical data: {e}")
            raise
