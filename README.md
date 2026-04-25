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

You can run the bot via Docker Compose for both single-coin and multi-coin
setups. Every trading service shares the same image and `.env`, and is
gated behind a Compose **profile** so containers only start when you ask
for them.

### Prerequisites

- Docker Engine 20.10+
- Docker Compose v2.0+ (the `docker compose` plugin, not legacy `docker-compose`)

### One-shot preparation

The `scripts/setup-docker.sh` (Linux/macOS/WSL) and
`scripts\setup-docker.bat` (Windows) helpers do the boring parts once:

1. Verify Docker + Compose v2 are installed.
2. Create `logs/`, `data/historical/`, `charts_output/` for volume mounts.
3. Copy `.env.example` → `.env` if it does not exist yet.
4. Build the image (`docker compose build`).
5. Print the list of available profiles.

```bash
# Linux / macOS / WSL
bash scripts/setup-docker.sh

# Windows (cmd or PowerShell)
scripts\setup-docker.bat

# Skip the image build (useful in CI or after edits to .env only)
bash scripts/setup-docker.sh --no-build
```

After it finishes, **edit `.env`** and fill in `BINANCE_API_KEY` /
`BINANCE_API_SECRET` before starting any live or paper service.

### Profile cheatsheet

All services live in `compose.yml`. None start by default — pick a profile.

| Goal | Profile | Command |
|---|---|---|
| Web dashboard (real data) | `dashboard` | `docker compose --profile dashboard up -d` |
| Web dashboard (demo data) | `demo` | `docker compose --profile demo up -d` |
| Single coin — paper / testnet | `paper-btc`, `paper-eth`, `paper-bnb`, `paper-dcr`, `paper-zec` | `docker compose --profile paper-btc up -d` |
| Single coin — LIVE (real money) | `live-btc`, `live-eth`, `live-bnb`, `live-dcr`, `live-zec` | `docker compose --profile live-dcr up -d` |
| Multi-coin basket — paper | `paper-basket` | `docker compose --profile paper-basket up -d` |
| Multi-coin basket — LIVE | `live-basket` | `docker compose --profile live-basket up -d` |
| Custom basket — paper | `paper-custom` | `STRATEGIES="btc_conservative eth_balanced" docker compose --profile paper-custom up -d` |
| Custom basket — LIVE | `live-custom` | `STRATEGIES="dcr_balanced zec_balanced" docker compose --profile live-custom up -d` |
| Backtest (one-off) | `backtest` | `docker compose --profile backtest run --rm backtest --strategies btc_conservative --days 30` |

> **Strategy names** match the JSON file names under `config/strategies/`
> (without the `.json` extension): `btc_conservative`, `eth_balanced`,
> `bnb_aggressive`, `dcr_balanced`, `zec_balanced`,
> `btc_distribution_example`, `zec_distribution_5k`. Add a new preset by
> dropping a JSON file into that folder — `paper-basket` / `live-basket`
> auto-pick it up via `--strategies all`.

### Single-coin examples

```bash
# Paper-trade DCR on testnet (safe — no real funds)
docker compose --profile paper-dcr up -d
docker compose logs -f binance-paper-dcr

# Live-trade BTC with the conservative preset (REAL money)
docker compose --profile live-btc up -d
docker compose logs -f binance-live-btc

# Stop just that one coin
docker compose --profile live-btc down
```

You can run **several single-coin profiles at the same time** because each
service has its own container name — they share the image and `.env` but
their state is independent:

```bash
docker compose --profile live-dcr up -d
docker compose --profile live-zec up -d
docker compose ps               # both containers running
```

### Multi-coin examples

Two ways to run multiple coins:

**A) One container, all enabled strategies** (lighter on resources, one
log stream):

```bash
# Paper basket
docker compose --profile paper-basket up -d
docker compose logs -f binance-paper-basket

# Live basket
docker compose --profile live-basket up -d
```

**B) Custom basket — pick exactly which strategies run together:**

```bash
# Linux / macOS
STRATEGIES="btc_conservative eth_balanced dcr_balanced" \
    docker compose --profile paper-custom up -d

# Windows (cmd)
set STRATEGIES=btc_conservative eth_balanced dcr_balanced
docker compose --profile paper-custom up -d

# Windows (PowerShell)
$env:STRATEGIES = "btc_conservative eth_balanced dcr_balanced"
docker compose --profile paper-custom up -d
```

The space-separated list is forwarded to `main.py --strategies`. Each
strategy keeps its own ladders and per-symbol open-order cap, so running
several inside one container is functionally the same as running each in
its own profile.

### Manual Docker build (no Compose)

```bash
# Linux / macOS
docker build -t binance-dcr-bot .

# Windows — disable BuildKit if you hit OCI runtime errors
set DOCKER_BUILDKIT=0
docker build -t binance-dcr-bot .
# or use the helper:
build-docker.bat
```

### Docker Commands Reference

| Action | Command |
|--------|---------|
| List active profiles | `docker compose config --profiles` |
| Start a profile | `docker compose --profile <name> up -d` |
| Stop a profile | `docker compose --profile <name> down` |
| Stop everything | `docker compose down` |
| View logs | `docker compose logs -f <service>` |
| List containers | `docker ps --filter "name=binance-"` |
| Rebuild after code change | `docker compose build` |

### Troubleshooting: Container Name Already in Use

If you get this error:

```text
docker: Error response from daemon: Conflict. The container name "/binance-bot" is already in use by container "8f3e5a509a5e0b2ed25e77909fd890828fef1879c3d7150fd2ac6810dc4433c3". You have to remove (or rename) that container to be able to reuse that name.
```

This means a container with that name already exists (even if it's stopped). Choose one of these solutions:

**Solution 1: Reuse the stopped container**
```bash
docker start binance-bot
```

**Solution 2: Remove the old container and create a new one**
```bash
docker stop binance-bot
docker rm binance-bot
docker run -d --name binance-bot \
  -p 5000:5000 \
  --env-file .env \
  binance-dcr-bot \
  python main.py --mode live --port 5000 --strategies dcr_balanced zec_balanced
```

**Solution 3: Use a different container name**
```bash
docker run -d --name binance-bot-live \
  -p 5000:5000 \
  --env-file .env \
  binance-dcr-bot \
  python main.py --mode live --port 5000 --strategies dcr_balanced zec_balanced
```

**Check container status:**
```bash
docker ps -a --filter "name=binance-bot"
```

> **Tip:** Use descriptive container names for different purposes, e.g., `binance-live-dcr-zec`, `binance-dashboard`, `binance-paper` to avoid name conflicts.

### Running Multiple Trading Instances

To run the bot with different trading pairs simultaneously:

**Example: Run DCR and ZEC strategies**
```bash
docker run -d --name binance-dcr-live \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/data:/app/data \
  binance-dcr-bot \
  python main.py --mode live --strategies dcr_balanced

docker run -d --name binance-zec-live \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/data:/app/data \
  binance-dcr-bot \
  python main.py --mode live --strategies zec_balanced
```

**Stop specific instances:**
```bash
# Stop DCR instance
docker stop binance-dcr-live && docker rm binance-dcr-live

# Stop ZEC instance
docker stop binance-zec-live && docker rm binance-zec-live
```

**Stop all Binance bot containers:**
```bash
docker stop $(docker ps -q --filter "name=binance-")
docker rm $(docker ps -aq --filter "name=binance-")

# Or use Docker Compose
docker compose down
```

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
docker compose down --volumes --remove-orphans

# 2. Remove the built image
docker image rm binance-dcr-bot

# 3. (Optional) Full system cleanup - removes ALL unused Docker resources
# ⚠️ Warning: This affects all Docker projects, not just this one
docker system prune -a --volumes
```

### Docker Compose Services

```bash
# View available services
docker compose config --services

# Start specific service
docker compose up -d <service-name>

# View logs for a service
docker compose logs -f <service-name>

# Rebuild after code changes
docker compose build --no-cache

# Stop all services
docker compose down

# Stop and remove volumes
docker compose down --volumes
```

### Volume Mounts

The following directories are mounted as volumes for data persistence:

- `./logs` - Application logs
- `./data` - Historical data and cache
- `./charts_output` - Generated charts
- `./config` - Strategy configurations (read-only)

## Build Standalone Binaries (Docker)

Build single-file PyInstaller binaries for **Linux amd64**, **Linux arm64**,
and **Windows amd64** without installing Python, PyInstaller, or Wine on your
host — only Docker is required. Internally `Dockerfile.binary` (Linux) and
`Dockerfile.binary.windows` (Windows-via-Wine) are driven by `docker buildx`.

### Prerequisites

- Docker Engine 20.10+ with the `buildx` plugin
- For cross-architecture builds (e.g. building `linux/arm64` on an `amd64`
  host), register QEMU once:

  ```bash
  docker run --privileged --rm tonistiigi/binfmt --install all
  ```

### Build all targets

```bash
# Linux / macOS / WSL
bash scripts/build-binaries.sh

# Windows (cmd / PowerShell)
scripts\build-binaries.bat
```

Output lands in `./dist/`:

```
dist/
├── binance-bot-linux-amd64        # ELF, runs on x86_64 Linux
├── binance-bot-linux-arm64        # ELF, runs on ARM64 Linux (Raspberry Pi 4/5, AWS Graviton, Apple Silicon under Linux)
└── binance-bot-windows-amd64.exe  # PE, runs on 64-bit Windows
```

### Build a single target

```bash
bash scripts/build-binaries.sh linux/amd64
bash scripts/build-binaries.sh linux/arm64
bash scripts/build-binaries.sh windows/amd64

# Or several at once
bash scripts/build-binaries.sh linux/arm64 windows/amd64
```

### Run the binary

The binary is self-contained (Python interpreter + all deps embedded). It
still needs the **`config/`** directory and a **`.env`** file in the working
directory, plus writable `logs/` and `data/` folders. The simplest layout:

```
my-bot/
├── binance-bot-linux-amd64        # the binary
├── .env                            # your API keys (copy from .env.example)
├── config/
│   ├── global_config.json
│   └── strategies/*.json
├── logs/                           # auto-created
└── data/                           # auto-created
```

#### Linux / macOS

```bash
# One-time: make it executable
chmod +x ./binance-bot-linux-amd64

# Web dashboard (real data)
./binance-bot-linux-amd64 --mode dashboard --port 5000

# Web dashboard (demo data, no API keys needed)
./binance-bot-linux-amd64 --mode dashboard --port 5000 --demo

# Paper-trade on Binance testnet
./binance-bot-linux-amd64 --mode paper --strategies btc_conservative

# Live-trade (REAL money)
./binance-bot-linux-amd64 --mode live --strategies dcr_balanced zec_balanced

# Backtest the last 30 days
./binance-bot-linux-amd64 --mode backtest --strategies btc_conservative --days 30

# Backtest a fixed window
./binance-bot-linux-amd64 --mode backtest --strategies all \
    --start 2024-01-01 --end 2024-06-30
```

ARM64 (Raspberry Pi, AWS Graviton, etc.) — same commands, just swap the
binary name:

```bash
chmod +x ./binance-bot-linux-arm64
./binance-bot-linux-arm64 --mode dashboard --port 5000
```

#### Windows

```cmd
:: Web dashboard
binance-bot-windows-amd64.exe --mode dashboard --port 5000

:: Paper trading
binance-bot-windows-amd64.exe --mode paper --strategies btc_conservative

:: Live trading
binance-bot-windows-amd64.exe --mode live --strategies dcr_balanced

:: Backtest last 30 days
binance-bot-windows-amd64.exe --mode backtest --strategies btc_conservative --days 30
```

PowerShell uses the same commands; prefix with `.\` if the binary is in the
current folder:

```powershell
.\binance-bot-windows-amd64.exe --mode dashboard --port 5000
```

### Run the binary as a service

**Linux (systemd)** — drop this into `/etc/systemd/system/binance-bot.service`:

```ini
[Unit]
Description=Binance Auto Rebalance Bot
After=network-online.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/opt/binance-bot
ExecStart=/opt/binance-bot/binance-bot-linux-amd64 --mode live --strategies dcr_balanced
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now binance-bot
sudo journalctl -u binance-bot -f
```

**Windows (Task Scheduler)** — `Create Basic Task` → trigger at logon →
action `Start a program` → program: full path to
`binance-bot-windows-amd64.exe` → arguments:
`--mode live --strategies dcr_balanced` → start in: folder containing the
binary + `config/` + `.env`.

### Notes & limits

- First build downloads base images and compiles wheels under QEMU for
  arm64 — expect 5–15 minutes. Subsequent builds are layer-cached and fast.
- The Windows build uses `batonogov/pyinstaller-windows` (Wine + Python on
  Linux). If it fails on your network, you can build natively on a Windows
  host with `pip install pyinstaller && pyinstaller --onefile main.py`.
- Each binary is large (~80–150 MB) because it bundles `numpy`, `pandas`,
  `matplotlib`, `eventlet`, and `ccxt`. This is expected for PyInstaller
  one-file builds.
- The binary reads `config/strategies/*.json` from the **current working
  directory**, not from a path baked into the executable — so you can
  swap presets without rebuilding.

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

### Backtest with `--days` shortcut

```bash
# Last N days (auto end = today)
python main.py --mode backtest --strategies btc_conservative --days 30

# Explicit range (takes precedence of --days)
python main.py --mode backtest --strategies all --start 2024-01-01 --end 2024-03-01

# Force a specific candle interval
python main.py --mode backtest --strategies btc_conservative --days 7 --interval 1h
```

Distribution-mode strategies auto-select `5m` candles so narrow child price
bands resolve cleanly; pass `--interval 1h` to override.

## Distribution Order Mode

`order_placement.mode = "distribution"` splits each ladder into several
Fibonacci-weighted **child orders** that mirror the main ladder's shape
(compound price spacing + size growing toward the bottom). Children stay in
an in-memory pending queue and are promoted to Binance only when price is
near them — so the bot stays within the exchange's hard limits on open-order
count and price distance (`PERCENT_PRICE_BY_SIDE`).

### How it works (one ladder at a time)

1. **Split** — `calculate_child_orders()` turns a ladder into N children
   (N scaled by `child_order_usdt`, clamped to
   `[min_children_per_ladder, max_children_per_ladder]`). Prices use
   compound multiplicative gaps weighted by Fibonacci, so top children sit
   dense near the ladder's buy price and deeper ones spread toward the next
   ladder's buy price. Sizes are Fibonacci-weighted too (deeper = bigger)
   and sum exactly to the parent's USDT.
2. **Queue** — All children across all ladders land in a single in-memory
   pending queue, sorted top-price-first.
3. **Promote** — On every price tick, `_promote_pending_children()` sends a
   child to Binance only when **both** hold:
   - `current_price` is within `proximity_percent` of the child's buy price
     (or already at/below it), **and**
   - the symbol's open-order count is below `max_open_orders_cap`.
4. **Hybrid SELL on fill** — When a child BUY fills, the bot immediately
   places a SELL at the parent ladder's `sell_price`. Profit stays exactly
   as planned; deeper children that buy cheaper just earn extra.

### Configuration

Add an `order_placement` block to your strategy JSON:

```json
{
  "name": "BTC Distribution Example",
  "pair": "BTCUSDT",
  "ladder_config": { "base_gap": 0.01, "ladders": 6, "fibonacci": [1, 1, 2, 3, 5, 8], "unit_size_btc": 0.01 },
  "order_placement": {
    "mode": "distribution",
    "child_order_usdt": 20.0,
    "proximity_percent": 0.02,
    "max_open_orders_cap": 180,
    "min_children_per_ladder": 2,
    "max_children_per_ladder": 15
  }
}
```

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"normal"` | Set to `"distribution"` to enable. |
| `child_order_usdt` | 20.0 | Target USDT per child; actual count scales to fit the parent. |
| `proximity_percent` | 0.02 | Max distance (fraction) from market before a child is promoted. |
| `max_open_orders_cap` | 180 | Hard ceiling on promoted orders per symbol (Binance limit ≈ 200). |
| `min_children_per_ladder` | 2 | Floor on splits per ladder. |
| `max_children_per_ladder` | 15 | Ceiling on splits per ladder. |

### Ready-to-run presets

| Preset file | Pair | Target capital | Notes |
|---|---|---|---|
| `config/strategies/btc_distribution_example.json` | BTCUSDT | flexible | Reference example for the docs above. |
| `config/strategies/zec_distribution_5k.json` | ZECUSDT | ~5,000 USDT | 8 ladders, `child_order_usdt = 25`, cap 180. |

Drop new presets into `config/strategies/` — every `*.json` in that folder is
auto-discovered. The strategy *name* you pass to `--strategies` is the file
name without the `.json` extension.

### Usage

```bash
# --- Single coin -------------------------------------------------------
# Live (real funds) — one strategy
python main.py --mode live  --strategies zec_distribution_5k

# Paper (testnet) — one strategy
python main.py --mode paper --strategies btc_distribution_example

# Backtest (auto-picks 5m interval for distribution strategies)
python main.py --mode backtest --strategies zec_distribution_5k --days 30

# --- Multiple coins ----------------------------------------------------
# Run several presets in the same process (space-separated)
python main.py --mode live  --strategies btc_distribution_example zec_distribution_5k

# Run every preset in config/strategies/ at once
python main.py --mode paper --strategies all

# Backtest a basket over a fixed window
python main.py --mode backtest --strategies btc_distribution_example zec_distribution_5k --days 60
```

Each strategy keeps its own ladders, pending queue, and per-symbol open-order
cap, so multiple coins run independently in the same process.

### Testing recommendations

Before enabling on a funded account, we recommend the following order:

**1. Unit / math invariants** (already covered by `tests/test_distribution.py`):

```bash
python -m pytest tests/ -v                      # all 33 tests
python -m pytest tests/test_distribution.py -v  # 12 distribution tests
```

The existing suite asserts: child USDT sums to parent, prices are sorted
descending and bounded by the next ladder, sell price is hybrid-paired,
sizing is Fibonacci-weighted, and caps clamp correctly.

**2. Gaps the suite does not yet cover** — worth adding before production:

- **Proximity boundary** — promote at `buy × (1 + proximity)` exactly, skip
  one tick above.
- **Cap exhaustion + recovery** — set `max_open_orders_cap` low (e.g. 3),
  run an oscillating price series, verify pending queue holds and resumes
  after SELL fills free slots.
- **Multi-symbol cap accounting** — if you run two strategies on the same
  pair, confirm cap is counted per-symbol, not per-strategy.
- **Partial BUY fills** — Binance may fill only part of a child; check that
  the hybrid SELL qty matches `filled_qty`, and that any remainder is not
  silently lost.
- **Restart mid-cycle** — the pending queue lives in memory. Kill the bot,
  restart, and verify it neither duplicates orders already on Binance nor
  drops queued children.

**3. Behavioural / integration**:

- Run **paper trading for at least a week** with the same strategy in
  `normal` and `distribution` mode and compare fill count, avg slippage,
  drawdown, and orders-on-exchange.
- Backtest 30–90 days across a volatile and a ranging window; inspect
  `portfolio_value[i]['pending_children']` — how often did the cap pin the
  queue? If it's pinned most of the time, lower `child_order_usdt` or raise
  the cap.
- Dry-run one cycle on live with **tiny capital** (e.g. 50 USDT) before
  scaling up.

### Extending the feature

If you want to build on top of distribution mode, the highest-leverage
changes are, in order:

1. **Persist the pending queue** *(high urgency).* Today
   `_pending_children` lives only in memory. A bot restart loses the queue
   and can duplicate orders already on Binance. Start at
   `src/order_manager.py:21` — add JSON/SQLite serialise/deserialise around
   `place_distribution_orders()` and the constructor.
2. **Per-symbol cap accounting** *(high urgency if you run multiple
   strategies on one pair).* Review the open-order count inside
   `_promote_pending_children()` (`src/order_manager.py:336`) to make sure
   it counts every order on the symbol, not only those owned by the current
   strategy.
3. **Stop-loss / cycle-close semantics for children** *(medium-high).* When
   a stop-loss triggers, decide what happens to promoted BUYs awaiting
   SELL, to promoted SELLs, and to the pending queue. Today the flow is
   implicit — make it explicit and tested.
4. **Child-level analytics** *(medium).* The backtester already records
   `child_idx`, `buy_price`, per-child profit/ROI. Mirror that in live:
   log each SELL fill with `{parent_level, child_idx, actual_buy,
   planned_sell, actual_profit}` from `_place_child_sell()`.
5. **Dynamic child sizing** *(medium).* `child_order_usdt` is static. Scale
   it from ATR or remaining balance — entry point is the child-count math
   at `src/strategy.py:172-207`.
6. **Partial-fill handling** *(medium).* Track remainder on the child,
   re-promote only the unfilled portion, and size the hybrid SELL off
   `filled_qty` rather than `child.qty`.
7. **Sequential + distribution combo** *(exploratory).* Verify whether the
   current queue ordering matches the intent of the "Sequential" mode —
   global top-price-first vs. strictly per-ladder ordering.

Key code anchors when you dig in:

- `src/strategy.py:147, 232, 234, 248` — child price/size math.
- `src/order_manager.py:21, 290, 321, 336, 352, 427` — pending queue,
  proximity/cap gating, hybrid SELL pairing.
- `backtest/backtester.py:109` — distribution simulation branch.
- `main.py:68-91, 143-147, 201-222` — CLI flags and live loop routing.
- `tests/test_distribution.py` — 12 tests (10 strategy math, 2 backtest).

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
├── Dockerfile.binary           # Linux PyInstaller binary build
├── Dockerfile.binary.windows   # Windows .exe build (Wine inside Docker)
├── compose.yml
├── scripts/
│   ├── setup-docker.sh
│   ├── setup-docker.bat
│   ├── build-binaries.sh       # Driver: produces dist/binance-bot-*
│   └── build-binaries.bat
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
