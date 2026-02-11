@echo off
REM Docker build script for Windows
REM Handles common BuildKit/WSL2 resource issues

echo Building Binance DCR Bot Docker image...
echo.

REM Try building with BuildKit disabled first (most reliable on Windows)
echo Method 1: Building with BuildKit disabled...
set DOCKER_BUILDKIT=0
docker build -t binance-dcr-bot . 2>nul
if %ERRORLEVEL% EQU 0 (
    echo.
    echo Build successful!
    echo Run with: docker run -d -p 5000:5000 --env-file .env binance-dcr-bot
    goto :end
)

echo Method 1 failed, trying alternative...
echo.

REM Try with BuildKit but limited parallelism
echo Method 2: Building with limited parallelism...
set DOCKER_BUILDKIT=1
docker build --build-arg BUILDKIT_INLINE_CACHE=1 -t binance-dcr-bot . 2>nul
if %ERRORLEVEL% EQU 0 (
    echo.
    echo Build successful!
    echo Run with: docker run -d -p 5000:5000 --env-file .env binance-dcr-bot
    goto :end
)

echo.
echo Build failed. Try these troubleshooting steps:
echo 1. Restart Docker Desktop
echo 2. Increase WSL2 memory in %%USERPROFILE%%\.wslconfig:
echo    [wsl2]
echo    memory=4GB
echo    processors=2
echo 3. Run: wsl --shutdown (then restart Docker Desktop)
echo.

:end
