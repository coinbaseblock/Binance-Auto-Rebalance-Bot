#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# build-binaries.sh — build standalone PyInstaller binaries for every
# supported target inside Docker, so the host only needs Docker + buildx.
#
# Targets:
#   linux/amd64    -> dist/binance-bot-linux-amd64
#   linux/arm64    -> dist/binance-bot-linux-arm64
#   windows/amd64  -> dist/binance-bot-windows-amd64.exe
#
# Usage:
#   bash scripts/build-binaries.sh                     # build all targets
#   bash scripts/build-binaries.sh linux/amd64         # build one
#   bash scripts/build-binaries.sh linux/arm64 windows/amd64
#
# Requirements:
#   - Docker Engine 20.10+ with the buildx plugin
#   - QEMU registered for cross-arch builds (one-off):
#       docker run --privileged --rm tonistiigi/binfmt --install all
# ----------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")/.."

DEFAULT_TARGETS=(windows/amd64 linux/amd64 linux/arm64)
TARGETS=("$@")
if [ ${#TARGETS[@]} -eq 0 ]; then
    TARGETS=("${DEFAULT_TARGETS[@]}")
fi

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[err ]\033[0m %s\n' "$*" >&2; }

# 1. Sanity checks
command -v docker >/dev/null 2>&1 || { err "docker not on PATH"; exit 1; }
docker buildx version >/dev/null 2>&1 || { err "docker buildx not installed"; exit 1; }

# 2. Make sure a usable buildx builder exists.
if ! docker buildx inspect bot-binary-builder >/dev/null 2>&1; then
    say "Creating buildx builder 'bot-binary-builder'"
    docker buildx create --name bot-binary-builder --use >/dev/null
else
    docker buildx use bot-binary-builder >/dev/null
fi

mkdir -p dist

build_linux() {
    local arch="$1"
    local platform="linux/${arch}"
    local out="dist/_tmp-linux-${arch}"
    say "Building ${platform} -> dist/binance-bot-linux-${arch}"
    rm -rf "$out"
    docker buildx build \
        --platform "$platform" \
        -f Dockerfile.binary \
        -o "type=local,dest=${out}" \
        .
    mv "${out}/binance-bot" "dist/binance-bot-linux-${arch}"
    chmod +x "dist/binance-bot-linux-${arch}"
    rm -rf "$out"
    echo "  -> dist/binance-bot-linux-${arch} ($(du -h "dist/binance-bot-linux-${arch}" | cut -f1))"
}

build_windows() {
    local arch="$1"
    local out="dist/_tmp-windows-${arch}"
    say "Building windows/${arch} -> dist/binance-bot-windows-${arch}.exe"
    rm -rf "$out"
    docker buildx build \
        --platform "linux/amd64" \
        -f Dockerfile.binary.windows \
        -o "type=local,dest=${out}" \
        .
    mv "${out}/binance-bot.exe" "dist/binance-bot-windows-${arch}.exe"
    rm -rf "$out"
    echo "  -> dist/binance-bot-windows-${arch}.exe ($(du -h "dist/binance-bot-windows-${arch}.exe" | cut -f1))"
}

for target in "${TARGETS[@]}"; do
    case "$target" in
        linux/amd64)   build_linux amd64 ;;
        linux/arm64)   build_linux arm64 ;;
        windows/amd64) build_windows amd64 ;;
        *)
            err "Unsupported target: $target"
            err "Supported: linux/amd64, linux/arm64, windows/amd64"
            exit 1
            ;;
    esac
done

say "Done. Artifacts in ./dist:"
ls -lh dist/binance-bot-* 2>/dev/null || true

cat <<'EOF'

Run on the target host:
    # Linux
    chmod +x ./binance-bot-linux-amd64
    ./binance-bot-linux-amd64 --mode dashboard --port 5000

    # Windows
    binance-bot-windows-amd64.exe --mode dashboard --port 5000

The binary expects `config/`, `.env`, `logs/`, and `data/` to be in the
current working directory (same layout as the source tree).
EOF
