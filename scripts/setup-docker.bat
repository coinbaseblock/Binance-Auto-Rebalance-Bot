@echo off
REM ----------------------------------------------------------------------------
REM setup-docker.bat - one-shot preparation for running the bot via Docker.
REM
REM What this does:
REM   1. Verifies Docker + Docker Compose v2 are installed.
REM   2. Creates logs, data\historical, charts_output (mounted volumes).
REM   3. Copies .env.example to .env if .env is missing.
REM   4. Builds the Docker image (skip with --no-build).
REM   5. Lists the available compose profiles.
REM
REM Usage:
REM   scripts\setup-docker.bat
REM   scripts\setup-docker.bat --no-build
REM ----------------------------------------------------------------------------
setlocal enableextensions

REM Move to repo root (parent of this script's folder)
pushd "%~dp0\.."

set "NO_BUILD=0"
if /I "%1"=="--no-build" set "NO_BUILD=1"
if /I "%1"=="-h"        goto :help
if /I "%1"=="--help"    goto :help

echo.
echo ==^> Checking Docker installation
where docker >nul 2>&1
if errorlevel 1 (
    echo [err ] docker is not on PATH. Install Docker Desktop first.
    popd & exit /b 1
)
docker --version

docker compose version >nul 2>&1
if errorlevel 1 (
    echo [err ] 'docker compose' v2 plugin not found. Install Compose v2.
    popd & exit /b 1
)
docker compose version

echo.
echo ==^> Creating host directories for volume mounts
if not exist logs            mkdir logs
if not exist data\historical mkdir data\historical
if not exist charts_output   mkdir charts_output
echo   logs\, data\historical\, charts_output\ ready

echo.
echo ==^> Checking .env
if exist .env (
    echo   .env already exists - leaving it alone
) else (
    if not exist .env.example (
        echo [err ] .env.example is missing - cannot bootstrap .env
        popd & exit /b 1
    )
    copy /Y .env.example .env >nul
    echo [warn] .env created from .env.example - edit it now and add your API keys.
    echo [warn]   BINANCE_API_KEY / BINANCE_API_SECRET are required.
)

if "%NO_BUILD%"=="1" (
    echo.
    echo [warn] Skipping docker build ^(--no-build^)
) else (
    echo.
    echo ==^> Building Docker image ^(this can take a few minutes^)
    REM Disable BuildKit for stability on Windows/WSL2.
    set "DOCKER_BUILDKIT=0"
    docker compose build
    if errorlevel 1 (
        echo [err ] docker compose build failed. See build-docker.bat for recovery tips.
        popd & exit /b 1
    )
)

echo.
echo ==^> Available compose profiles
echo.
echo   Dashboard:
echo     docker compose --profile dashboard up -d
echo     docker compose --profile demo      up -d
echo.
echo   Single-coin ^(paper / testnet^):
echo     docker compose --profile paper-btc up -d
echo     docker compose --profile paper-eth up -d
echo     docker compose --profile paper-bnb up -d
echo     docker compose --profile paper-dcr up -d
echo     docker compose --profile paper-zec up -d
echo.
echo   Single-coin ^(LIVE^):
echo     docker compose --profile live-btc  up -d
echo     docker compose --profile live-eth  up -d
echo     docker compose --profile live-bnb  up -d
echo     docker compose --profile live-dcr  up -d
echo     docker compose --profile live-zec  up -d
echo.
echo   Multi-coin baskets:
echo     docker compose --profile paper-basket up -d
echo     docker compose --profile live-basket  up -d
echo.
echo   Multi-coin custom basket:
echo     set STRATEGIES=btc_conservative eth_balanced dcr_balanced
echo     docker compose --profile paper-custom up -d
echo.
echo   Backtest:
echo     docker compose --profile backtest run --rm backtest --strategies btc_conservative --days 30
echo.
echo   Tail logs / stop:
echo     docker compose logs -f ^<service^>
echo     docker compose down
echo.
echo ==^> Setup complete.
popd & exit /b 0

:help
type "%~f0" | more
popd & exit /b 0
