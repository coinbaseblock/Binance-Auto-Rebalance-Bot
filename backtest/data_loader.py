"""
Historical Data Loader
"""
import pandas as pd
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class DataLoader:
    def __init__(self, binance_client):
        self.client = binance_client

    def load_historical_data(self, symbol, interval, start_date, end_date):
        """
        Load historical klines data from Binance

        Args:
            symbol: Trading pair (e.g., 'BTCUSDT')
            interval: Timeframe (e.g., '1h', '4h', '1d')
            start_date: Start date string (e.g., '2024-01-01')
            end_date: End date string (e.g., '2024-12-31')

        Returns:
            pandas DataFrame with OHLCV data
        """
        logger.info(f"Loading historical data for {symbol} from {start_date} to {end_date}")

        klines = self.client.get_historical_klines(
            symbol=symbol,
            interval=interval,
            start_str=start_date,
            end_str=end_date
        )

        # Convert to DataFrame
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])

        # Convert timestamp to datetime
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)

        # Convert price columns to float
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)

        logger.info(f"Loaded {len(df)} candles")
        return df

    def save_to_csv(self, df, filepath):
        """Save DataFrame to CSV"""
        df.to_csv(filepath)
        logger.info(f"Data saved to {filepath}")

    def load_from_csv(self, filepath):
        """Load DataFrame from CSV"""
        df = pd.read_csv(filepath, index_col='timestamp', parse_dates=True)
        logger.info(f"Data loaded from {filepath}")
        return df
