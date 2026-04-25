#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# setup-docker.sh — one-shot preparation for running the bot via Docker.
#
# What this does:
#   1. Verifies Docker + Docker Compose v2 are installed.
#   2. Creates ./logs, ./data/historical, ./charts_output (mounted volumes).
#   3. Copies .env.example to .env if .env is missing.
#   4. Builds the Docker image.
#   5. Lists the available compose profiles.
#
# Usage:
#   bash scripts/setup-docker.sh
#   bash scripts/setup-docker.sh --no-build   # skip docker build
# ----------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")/.."

NO_BUILD=0
for arg in "$@"; do
  case "$arg" in
    --no-build) NO_BUILD=1 ;;
    -h|--help)
      sed -n '2,16p' "$0"
      exit 0
      ;;
  esac
done

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[err ]\033[0m %s\n' "$*" >&2; }

# 1. Check Docker
say "Checking Docker installation"
if ! command -v docker >/dev/null 2>&1; then
  err "docker is not on PATH. Install Docker Desktop or Docker Engine first."
  exit 1
fi
docker --version

if ! docker compose version >/dev/null 2>&1; then
  err "'docker compose' (v2) plugin not found. Install Compose v2."
  exit 1
fi
docker compose version

# 2. Ensure host directories exist (volumes will mount these)
say "Creating host directories for volume mounts"
mkdir -p logs data/historical charts_output
echo "  logs/, data/historical/, charts_output/ ready"

# 3. .env bootstrap
say "Checking .env"
if [ -f .env ]; then
  echo "  .env already exists — leaving it alone"
else
  if [ ! -f .env.example ]; then
    err ".env.example is missing — cannot bootstrap .env"
    exit 1
  fi
  cp .env.example .env
  warn ".env created from .env.example — edit it now and add your API keys."
  warn "  BINANCE_API_KEY / BINANCE_API_SECRET are required."
fi

# 4. Build image (unless --no-build)
if [ "$NO_BUILD" -eq 0 ]; then
  say "Building Docker image (this can take a few minutes the first time)"
  docker compose build
else
  warn "Skipping docker build (--no-build)"
fi

# 5. Show available profiles
say "Available compose profiles"
cat <<'EOF'
  Dashboard:
    docker compose --profile dashboard up -d         # web UI on :5000
    docker compose --profile demo      up -d         # demo UI on :5001

  Single-coin (paper / testnet — safe to test):
    docker compose --profile paper-btc up -d
    docker compose --profile paper-eth up -d
    docker compose --profile paper-bnb up -d
    docker compose --profile paper-dcr up -d
    docker compose --profile paper-zec up -d

  Single-coin (LIVE — real money, use with care):
    docker compose --profile live-btc  up -d
    docker compose --profile live-eth  up -d
    docker compose --profile live-bnb  up -d
    docker compose --profile live-dcr  up -d
    docker compose --profile live-zec  up -d

  Multi-coin baskets (all enabled strategies in one container):
    docker compose --profile paper-basket up -d
    docker compose --profile live-basket  up -d

  Multi-coin custom basket (pick your own list):
    STRATEGIES="btc_conservative eth_balanced dcr_balanced" \
        docker compose --profile paper-custom up -d

  Backtest (one-off):
    docker compose --profile backtest run --rm backtest \
        --strategies btc_conservative --days 30

  Tail logs / stop:
    docker compose logs -f <service>
    docker compose down
EOF

say "Setup complete."
