# Binance Auto Rebalance Bot

Advanced multi-strategy cryptocurrency rebalancing bot using Fibonacci-Martingale approach.

## Features

- Multi-coin support (BTC, ETH, BNB, etc.)
- Multi-strategy execution (Conservative, Balanced, Aggressive)
- Fibonacci-based ladder spacing
- Martingale position sizing
- Comprehensive backtesting
- Fee calculation (0.1% Binance)
- Stop-loss protection
- Real-time portfolio tracking
- Detailed logging

## Installation

```bash
# Clone repository
git clone https://github.com/yourusername/binance-auto-rebalance.git
cd binance-auto-rebalance

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Setup environment
cp .env.example .env
# Edit .env with your Binance API keys
```

## Environment Variables (.env)

The `.env` file contains all configuration for the bot. Copy `.env.example` to `.env` and configure the following variables:

### Binance API Credentials

| Variable | Required | Description |
|----------|----------|-------------|
| `BINANCE_API_KEY` | Yes | Your Binance API key (get from Binance > API Management) |
| `BINANCE_API_SECRET` | Yes | Your Binance API secret |
| `BINANCE_TESTNET` | No | Use testnet (`true`) or mainnet (`false`). Default: `true` |

### Trading Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TOTAL_CAPITAL_USDT` | 10000 | Total capital in USDT for trading |
| `MAX_ALLOCATION_PER_COIN` | 0.20 | Maximum allocation per coin (20% = 0.20) |

### Risk Management

| Variable | Default | Description |
|----------|---------|-------------|
| `STOP_LOSS_PERCENT` | 25 | Stop-loss percentage (25 = -25%) |
| `MAX_CONCURRENT_STRATEGIES` | 5 | Maximum number of concurrent strategies |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | INFO | Log level: DEBUG, INFO, WARNING, ERROR |
| `LOG_FILE` | logs/bot.log | Path to log file |

### Backtest

| Variable | Default | Description |
|----------|---------|-------------|
| `BACKTEST_START_DATE` | 2024-01-01 | Backtest start date (YYYY-MM-DD) |
| `BACKTEST_END_DATE` | 2024-12-31 | Backtest end date (YYYY-MM-DD) |

### Docker Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_PORT` | 5000 | Dashboard web UI port |
| `DEMO_PORT` | 5001 | Demo dashboard port |

### Example .env file

```bash
# Binance API Credentials
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here

# Trading Configuration
BINANCE_TESTNET=true
TOTAL_CAPITAL_USDT=10000
MAX_ALLOCATION_PER_COIN=0.20

# Risk Management
STOP_LOSS_PERCENT=25
MAX_CONCURRENT_STRATEGIES=5

# Logging
LOG_LEVEL=INFO
LOG_FILE=logs/bot.log

# Backtest
BACKTEST_START_DATE=2024-01-01
BACKTEST_END_DATE=2024-12-31

# Docker Configuration
DASHBOARD_PORT=5000
DEMO_PORT=5001
```

> **⚠️ Important:** Never commit your `.env` file to version control. The `.gitignore` already excludes it.

## Docker Installation

You can also run the bot using Docker Compose.

### Prerequisites

- Docker Engine 20.10+
- Docker Compose v2.0+

### Quick Start with Docker

```bash
# Clone repository
git clone https://github.com/yourusername/binance-auto-rebalance.git
cd binance-auto-rebalance

# Setup environment
cp .env.example .env
# Edit .env with your Binance API keys

# Start the dashboard (default)
docker compose up -d dashboard

# Access the dashboard at http://localhost:5000
```

### Available Services

| Service | Description | Port | Command |
|---------|-------------|------|---------|
| `dashboard` | Web dashboard for monitoring | 5000 | `docker compose up dashboard` |
| `dashboard-demo` | Demo dashboard with sample data | 5001 | `docker compose --profile demo up dashboard-demo` |
| `paper` | Paper trading (testnet) | - | `docker compose --profile paper up paper` |
| `live` | Live trading (real money) | - | `docker compose --profile live up live` |

### Docker Commands

```bash
# 1. Build (Windows - use build-docker.bat or disable BuildKit)
build-docker.bat
# OR
set DOCKER_BUILDKIT=0
docker build -t binance-dcr-bot .

# 2. Run DCR Live
docker-compose up dcr-live

# หรือรันแบบ background
docker-compose up -d dcr-live

# 3. View logs
docker logs -f binance-dcr-live
```

### Docker Operations Reference

| Operation | Command |
|-----------|---------|
| Run DCR Live | `docker-compose up dcr-live` |
| Run DCR Live (background) | `docker-compose up -d dcr-live` |
| Run Simulation | `docker-compose --profile simulation up dcr-simulation` |
| Run Dashboard | `docker-compose --profile dashboard up dashboard` |
| Stop Bot | `docker-compose down` |
| View Status | `docker ps` |
| Restart Bot | `docker restart binance-dcr-live` |
| View Logs | `docker logs -f binance-dcr-live` |

### Start/Stop Specific Trading Pair

หยุด Bot เฉพาะคู่เหรียญ:

```bash
# Stop และ Remove container เฉพาะคู่ DCR/USDT
docker stop binance-dcr-live && docker rm binance-dcr-live

# Start ใหม่ด้วย docker run
docker run -d --name binance-dcr-live \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/data:/app/data \
  binance-dcr-bot \
  python main.py --mode live --symbol DCR/USDT

# หรือใช้ docker-compose (แนะนำ)
docker-compose --profile dcr up -d dcr-live
```

รันคู่เหรียญอื่น (ตัวอย่าง BTC/USDT):

```bash
# รัน BTC/USDT
docker run -d --name binance-btc-live \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/data:/app/data \
  binance-dcr-bot \
  python main.py --mode live --symbol BTC/USDT

# รัน ETH/USDT
docker run -d --name binance-eth-live \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/data:/app/data \
  binance-dcr-bot \
  python main.py --mode live --symbol ETH/USDT

# Stop เฉพาะ BTC
docker stop binance-btc-live && docker rm binance-btc-live

# Stop เฉพาะ ETH
docker stop binance-eth-live && docker rm binance-eth-live
```

### Start/Stop All Bots

หยุด Bot ทั้งหมด:

```bash
# Stop ทุก container ที่เกี่ยวกับ binance-bot
docker stop $(docker ps -q --filter "name=binance-")
docker rm $(docker ps -aq --filter "name=binance-")

# หรือใช้ docker-compose (แนะนำ)
docker-compose down
```

Start Bot ทั้งหมด:

```bash
# Start ทุก service ด้วย docker-compose
docker-compose up -d

# Start พร้อม DCR Live
docker-compose --profile dcr up -d

# Start หลายคู่พร้อมกัน (ใช้ docker run)
docker run -d --name binance-btc-live --env-file .env -v $(pwd)/logs:/app/logs -v $(pwd)/data:/app/data binance-dcr-bot python main.py --mode live --symbol BTC/USDT
docker run -d --name binance-eth-live --env-file .env -v $(pwd)/logs:/app/logs -v $(pwd)/data:/app/data binance-dcr-bot python main.py --mode live --symbol ETH/USDT
docker run -d --name binance-dcr-live --env-file .env -v $(pwd)/logs:/app/logs -v $(pwd)/data:/app/data binance-dcr-bot python main.py --mode live --symbol DCR/USDT

docker run --name binance-bot -p 5000:5000 --env-file .env binance-dcr-bot python main.py --mode dashboard --port 5000 --strategies dcr_balanced

```

### Quick Reference Table

| Action | Command |
|--------|---------|
| Stop เฉพาะ DCR | `docker stop binance-dcr-live && docker rm binance-dcr-live` |
| Stop เฉพาะ BTC | `docker stop binance-btc-live && docker rm binance-btc-live` |
| Stop ทั้งหมด | `docker stop $(docker ps -q --filter "name=binance-")` |
| Remove ทั้งหมด | `docker rm $(docker ps -aq --filter "name=binance-")` |
| Start DCR | `docker-compose --profile dcr up -d dcr-live` |
| Start All | `docker-compose up -d` |
| View All Bots | `docker ps --filter "name=binance-"` |
| View Logs | `docker logs -f binance-dcr-live` |

### Troubleshooting Docker on Windows

If you encounter `pthread_create failed: Resource temporarily unavailable` errors:

1. **Use the build script (recommended)**:
   ```bash
   build-docker.bat
   ```

2. **Or manually disable BuildKit**:
   ```bash
   set DOCKER_BUILDKIT=0
   docker build -t binance-dcr-bot .
   ```

3. **If issues persist, increase WSL2 memory**:
   - Create/edit `%USERPROFILE%\.wslconfig`:
     ```ini
     [wsl2]
     memory=4GB
     processors=2
     ```
   - Run `wsl --shutdown` and restart Docker Desktop

### Docker Cleanup

To clean up Docker resources related to this project:

```bash
# 1. Stop and remove containers/volumes/networks for this project
docker-compose down --volumes --remove-orphans

# 2. Remove the built image
docker image rm binance-dcr-bot

# 3. (Optional) Full system cleanup - removes ALL unused Docker resources
# ⚠️ Warning: This affects all Docker projects, not just this one
docker system prune -a --volumes
```

### Legacy Docker Commands

```bash
# Start dashboard (default service)
docker compose up -d dashboard

# Start demo dashboard
docker compose --profile demo up -d dashboard-demo

# Start paper trading bot
docker compose --profile paper up -d paper

# Start live trading bot (USE WITH CAUTION!)
docker compose --profile live up -d live

# View logs
docker compose logs -f dashboard

# Stop all services
docker compose down

# Rebuild after code changes
docker compose build --no-cache
docker compose up -d dashboard
```

### Volume Mounts

The following directories are mounted as volumes for data persistence:

- `./logs` - Application logs
- `./data` - Historical data and cache
- `./charts_output` - Generated charts
- `./config` - Strategy configurations (read-only)

## Configuration

### Strategy Configuration

Edit `config/strategies/*.json` files:

```json
{
  "name": "BTC Conservative",
  "pair": "BTC/USDT",
  "base_gap": 0.01,
  "ladders": 6,
  "unit_size": 0.01,
  "fibonacci": [1, 1, 2, 3, 5, 8],
  "safety_multiplier": 1.5,
  "stop_loss": -0.25
}
```

## Usage

### Live Trading

```bash
python main.py --mode live --strategies btc_conservative eth_balanced
```

### Backtesting

```bash
python main.py --mode backtest --strategies btc_conservative --start 2024-01-01 --end 2024-12-31
```

### Paper Trading

```bash
python main.py --mode paper --strategies all
```

## Strategy Examples

### Conservative (Low Risk)

- Base Gap: 1.0%
- Ladders: 6
- Total Swing: 20%
- Expected ROI: 5-10% per cycle

### Balanced (Medium Risk)

- Base Gap: 0.75%
- Ladders: 8
- Total Swing: 25%
- Expected ROI: 10-15% per cycle

### Aggressive (High Risk)

- Base Gap: 0.6%
- Ladders: 10
- Total Swing: 85.8%
- Expected ROI: 15-25% per cycle

## Complete Ladder Table (10 Ladders with 0.6% Base Gap)

Reference Price: $100,000 BTC

### BUY SIDE (Price Drops)

| Ladder | Fib | Gap % | Cumulative % | Buy Price | Units | BTC | USDT Cost |
|--------|-----|-------|--------------|-----------|-------|-----|-----------|
| -1 | 1 | 0.60% | 0.60% | $99,400 | 1 | 0.01 | $994 |
| -2 | 1 | 0.60% | 1.20% | $98,800 | 2 | 0.02 | $1,988 |
| -3 | 2 | 1.20% | 2.40% | $97,600 | 4 | 0.04 | $3,976 |
| -4 | 3 | 1.80% | 4.20% | $95,800 | 8 | 0.08 | $7,952 |
| -5 | 5 | 3.00% | 7.20% | $92,800 | 16 | 0.17 | $15,904 |
| -6 | 8 | 4.80% | 12.00% | $88,000 | 32 | 0.36 | $31,808 |
| -7 | 13 | 7.80% | 19.80% | $80,200 | 64 | 0.79 | $63,616 |
| -8 | 21 | 12.60% | 32.40% | $67,600 | 128 | 1.88 | $127,232 |
| -9 | 34 | 20.40% | 52.80% | $47,200 | 256 | 5.39 | $254,464 |
| -10 | 55 | 33.00% | 85.80% | $14,200 | 512 | 35.84 | $508,928 |

**Total BUY Side: 44.58 BTC | $1,016,862 USDT**

### SELL SIDE (Price Rises)

| Ladder | Fib | Gap % | Cumulative % | Sell Price | Units | BTC | USDT Revenue |
|--------|-----|-------|--------------|------------|-------|-----|--------------|
| +1 | 1 | 0.60% | 0.60% | $100,600 | 1 | 0.01 | $1,006 |
| +2 | 1 | 0.60% | 1.20% | $101,200 | 2 | 0.02 | $2,012 |
| +3 | 2 | 1.20% | 2.40% | $102,400 | 4 | 0.04 | $4,024 |
| +4 | 3 | 1.80% | 4.20% | $104,200 | 8 | 0.08 | $8,048 |
| +5 | 5 | 3.00% | 7.20% | $107,200 | 16 | 0.15 | $16,096 |
| +6 | 8 | 4.80% | 12.00% | $112,000 | 32 | 0.29 | $32,192 |
| +7 | 13 | 7.80% | 19.80% | $119,800 | 64 | 0.54 | $64,384 |
| +8 | 21 | 12.60% | 32.40% | $132,400 | 128 | 0.97 | $128,768 |
| +9 | 34 | 20.40% | 52.80% | $152,800 | 256 | 1.69 | $257,536 |
| +10 | 55 | 33.00% | 85.80% | $185,800 | 512 | 2.77 | $515,072 |

**Total SELL Side: 6.56 BTC | $1,029,138 USDT**

### Combined BUY + SELL Grid

| Ladder | Buy Price | Reference | Sell Price | Profit/Unit |
|--------|-----------|-----------|------------|-------------|
| 10 | $14,200 | ← | $185,800 | +$1,716 |
| 9 | $47,200 | ← | $152,800 | +$1,056 |
| 8 | $67,600 | ← | $132,400 | +$648 |
| 7 | $80,200 | ← | $119,800 | +$396 |
| 6 | $88,000 | ← | $112,000 | +$240 |
| 5 | $92,800 | ← | $107,200 | +$144 |
| 4 | $95,800 | ← | $104,200 | +$84 |
| 3 | $97,600 | ← | $102,400 | +$48 |
| 2 | $98,800 | ← | $101,200 | +$24 |
| 1 | $99,400 | $100,000 | $100,600 | +$12 |

## Auto Rebalance Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                    AUTO REBALANCE FLOW                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. INITIALIZATION                                              │
│     └─► Load Strategy Config                                    │
│     └─► Calculate all ladder prices                             │
│     └─► Place BUY limit orders at all -1 to -10 levels          │
│                                                                 │
│  2. PRICE DROP                                                  │
│     ┌──────────────────────────────────────────────────────┐    │
│     │ Price drops to $99,400 → Ladder -1 BUY order FILLED  │    │
│     │ System automatically:                                │    │
│     │   • Records position in Portfolio                    │    │
│     │   • Places SELL order at $100,600 (+1)               │    │
│     │   • Updates ladder status to "active"                │    │
│     └──────────────────────────────────────────────────────┘    │
│                                                                 │
│  3. PRICE RECOVERY                                              │
│     ┌──────────────────────────────────────────────────────┐    │
│     │ Price rises to $100,600 → SELL order FILLED          │    │
│     │ System automatically:                                │    │
│     │   • Closes position                                  │    │
│     │   • Records profit: $12/unit                         │    │
│     │   • Resets ladder status to "pending"                │    │
│     │   • Places new BUY order at $99,400                  │    │
│     └──────────────────────────────────────────────────────┘    │
│                                                                 │
│  4. CONTINUOUS CYCLE                                            │
│     └─► rebalance_interval_hours: Check every 24 hours          │
│     └─► check_interval_seconds: Monitor orders every 60 sec     │
│     └─► price_update_interval: Update prices every 5 sec        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Auto Rebalance Example Scenario

**BTC price drops from $100K → $80K → recovers to $100K**

```
Time     Price      Event                        Action
─────────────────────────────────────────────────────────────
T+0      $100,000   Start                        Place BUY orders (10 ladders)
T+1      $99,400    Ladder -1 triggered          Buy 0.01 BTC @ $99,400
                                                 → Place SELL @ $100,600
T+2      $98,800    Ladder -2 triggered          Buy 0.02 BTC @ $98,800
                                                 → Place SELL @ $99,400
T+3      $97,600    Ladder -3 triggered          Buy 0.04 BTC @ $97,600
                                                 → Place SELL @ $98,800
...
T+7      $80,200    Ladder -7 triggered          Buy 0.64 BTC @ $80,200
                                                 → Place SELL @ $88,000

─────────────────────────────────────────────────────────────
                  *** Price Recovery ***
─────────────────────────────────────────────────────────────

T+10     $88,000    Ladder -7 SELL filled        Sell 0.64 BTC @ $88,000
                                                 Profit: $4,992
                                                 → Place new BUY @ $80,200
T+11     $97,600    Ladder -3 SELL filled        Sell 0.04 BTC @ $98,800
                                                 Profit: $48
...
T+15     $100,600   Ladder -1 SELL filled        Sell 0.01 BTC @ $100,600
                                                 Profit: $12
                                                 → Cycle restarts
```

### Configuration Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| auto_rebalance | true | Enable auto rebalance |
| rebalance_interval_hours | 24 | Rebalance check interval |
| min_profit_to_close | 0.005 | Minimum 0.5% profit to close |
| check_interval_seconds | 60 | Order check interval |
| price_update_interval | 5 | Price update interval |

### Base Gap Comparison

| Base Gap | Total Swing | Buy Price (Ladder 10) | Suitable For |
|----------|-------------|----------------------|--------------|
| 0.5% | 71.5% | $28,500 | Conservative |
| 0.6% | 85.8% | $14,200 | Balanced |
| 0.7% | 100.1% | ~$0 (invalid) | - |
| 0.8% | 114.4% | Negative (invalid) | - |

**Recommendation:** Use base_gap = 0.6% for 10 ladders to cover crashes up to -85.8%

## Project Structure

```
binance-auto-rebalance/
├── .env.example
├── .gitignore
├── .dockerignore
├── README.md
├── requirements.txt
├── Dockerfile
├── compose.yml
├── config/
│   ├── strategies/
│   │   ├── btc_conservative.json
│   │   ├── eth_balanced.json
│   │   └── bnb_aggressive.json
│   └── global_config.json
├── src/
│   ├── __init__.py
│   ├── binance_client.py
│   ├── strategy.py
│   ├── order_manager.py
│   ├── martingale.py
│   └── portfolio.py
├── backtest/
│   ├── __init__.py
│   ├── backtester.py
│   └── data_loader.py
├── tests/
│   ├── test_strategy.py
│   └── test_martingale.py
├── data/
│   └── historical/
├── logs/
└── main.py
```

## Example Output

```
=== BACKTEST REPORT ===
{
  "strategy": "BTC Conservative",
  "period": {
    "start": "2024-01-01",
    "end": "2024-12-31",
    "days": 365
  },
  "capital": {
    "initial": 10000.0,
    "final": 11245.67,
    "profit": 1245.67,
    "roi_percent": 12.46
  },
  "trades": {
    "total": 48,
    "winning": 46,
    "losing": 2,
    "win_rate": 95.83,
    "avg_profit": 25.95
  }
}
```

## Risk Warning

Cryptocurrency trading involves substantial risk. This bot is for educational purposes. Always:

- Start with small capital
- Use testnet first
- Monitor positions regularly
- Set appropriate stop-losses
- Never invest more than you can afford to lose

## License

MIT
