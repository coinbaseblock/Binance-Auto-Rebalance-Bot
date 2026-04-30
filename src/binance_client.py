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
    # Beyond this age, cached prices are too stale to use even as a network-error
    # fallback. The caller will receive an exception instead.
    MAX_CACHE_FALLBACK_AGE_SEC = 600

    # recvWindow protects signed requests from being delayed/replayed. Binance
    # rejects requests where (server_time - timestamp) > recvWindow with code
    # -1021. We push it above the default 5000ms because a) Windows clocks
    # drift, and b) network jitter can add hundreds of ms.
    DEFAULT_RECV_WINDOW_MS = 10000

    # Re-sync the clock offset every N seconds even without an error, so steady
    # drift on long-running sessions doesn't accumulate past recvWindow.
    TIME_SYNC_INTERVAL_SEC = 1800  # 30 min

    def __init__(self, testnet=False):
        load_dotenv()

        api_key = os.getenv('BINANCE_API_KEY')
        api_secret = os.getenv('BINANCE_API_SECRET')

        if not api_key or not api_secret:
            raise ValueError("Binance API credentials not found in .env file")

        self.client = Client(api_key, api_secret, testnet=testnet)
        self.testnet = testnet
        self._symbol_filters = {}  # Cache for symbol filter info
        self._price_cache = {}  # symbol -> (price, fetch_timestamp)
        self.recv_window = self.DEFAULT_RECV_WINDOW_MS
        self._last_time_sync = 0.0

        # Best-effort initial sync; if it fails (e.g. cold network) we don't
        # block startup — the next signed call will retry on -1021.
        try:
            self._sync_time_offset()
        except Exception as e:
            logger.warning(f"Initial time sync failed (will retry on demand): {e}")

        logger.info(f"Binance client initialized (testnet={testnet})")

    def _sync_time_offset(self):
        """Fetch Binance server time and store the offset on the underlying
        Client so all subsequent signed requests use a server-aligned timestamp.

        python-binance reads `client.timestamp_offset` and adds it to
        `int(time.time()*1000)` when building signed requests.
        """
        server_ms = self.client.get_server_time()['serverTime']
        local_ms = int(time.time() * 1000)
        offset = server_ms - local_ms
        self.client.timestamp_offset = offset
        self._last_time_sync = time.time()
        log = logger.warning if abs(offset) > 1000 else logger.info
        log(f"Time offset synced with Binance server: {offset:+d} ms")

    def _ensure_time_synced(self, force=False):
        """Re-sync the clock offset if it's stale or forced."""
        if force or (time.time() - self._last_time_sync) > self.TIME_SYNC_INTERVAL_SEC:
            try:
                self._sync_time_offset()
            except Exception as e:
                logger.warning(f"Time re-sync failed: {e}")

    def _signed(self, fn, *args, **kwargs):
        """Run a signed Binance API call with clock-drift resilience.

        - Ensures time offset is fresh before sending
        - Injects recvWindow if the caller didn't set one
        - On -1021 (timestamp outside recvWindow), force-resyncs and retries
          exactly once
        """
        self._ensure_time_synced()
        kwargs.setdefault('recvWindow', self.recv_window)
        try:
            return fn(*args, **kwargs)
        except BinanceAPIException as e:
            if e.code == -1021:
                logger.warning(
                    f"Got -1021 (timestamp outside recvWindow) on {fn.__name__}; "
                    f"re-syncing time offset and retrying once"
                )
                self._ensure_time_synced(force=True)
                return fn(*args, **kwargs)
            raise

    def get_order(self, symbol, order_id):
        """Query a single order's status (signed). Wraps the underlying
        client.get_order so callers get clock-drift resilience for free.
        """
        return self._signed(self.client.get_order, symbol=symbol, orderId=order_id)

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
        """Get current market price for a symbol, with retry on network errors.

        On success the price is cached together with a fetch timestamp. If all
        retries fail with network errors and a recent enough cached price exists
        (age <= MAX_CACHE_FALLBACK_AGE_SEC), the cached price is returned as a
        soft fallback so the caller can keep operating. Use get_price_age() to
        detect when a returned price is stale and gate trading decisions
        accordingly.
        """
        for attempt in range(retries + 1):
            try:
                ticker = self.client.get_symbol_ticker(symbol=symbol)
                price = float(ticker['price'])
                self._price_cache[symbol] = (price, time.time())
                return price
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
                    cached = self._price_cache.get(symbol)
                    if cached is not None:
                        cached_price, cached_ts = cached
                        age = time.time() - cached_ts
                        if age <= self.MAX_CACHE_FALLBACK_AGE_SEC:
                            logger.warning(
                                f"Network error getting price for {symbol} after "
                                f"{retries + 1} attempts; using cached price "
                                f"${cached_price} (age {age:.0f}s): {e}"
                            )
                            return cached_price
                    logger.error(f"Network error getting price for {symbol} after {retries + 1} attempts: {e}")
                    raise

    def get_price_age(self, symbol):
        """Return age in seconds of the last cached price for symbol, or None
        if no successful fetch has been recorded yet."""
        cached = self._price_cache.get(symbol)
        if cached is None:
            return None
        return time.time() - cached[1]

    def get_account_balance(self, asset='USDT'):
        """Get account balance for specific asset"""
        try:
            balance = self._signed(self.client.get_asset_balance, asset=asset)
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

            order = self._signed(
                self.client.create_order,
                symbol=symbol,
                side=side,  # 'BUY' or 'SELL'
                type='LIMIT',
                timeInForce='GTC',
                quantity=str(rounded_qty),
                price=str(rounded_price),
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

            order = self._signed(
                self.client.create_order,
                symbol=symbol,
                side=side,
                type='MARKET',
                quantity=str(rounded_qty),
            )
            logger.info(f"Market order created: {side} {rounded_qty} {symbol}")
            return order
        except BinanceAPIException as e:
            logger.error(f"Error creating market order: {e}")
            raise

    def get_open_orders(self, symbol=None):
        """Get all open orders"""
        try:
            kwargs = {'symbol': symbol} if symbol is not None else {}
            return self._signed(self.client.get_open_orders, **kwargs)
        except BinanceAPIException as e:
            logger.error(f"Error getting open orders: {e}")
            raise

    def cancel_order(self, symbol, order_id):
        """Cancel an order"""
        try:
            result = self._signed(self.client.cancel_order, symbol=symbol, orderId=order_id)
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
