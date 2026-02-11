# PowerShell build script for Docker on Windows
# Handles OCI runtime errors with automatic retry after WSL reset

param(
    [switch]$Force,      # Force rebuild with --no-cache
    [switch]$ResetWSL    # Reset WSL before building
)

$ErrorActionPreference = "Continue"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Docker Build Script for Windows (PowerShell)" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Disable BuildKit for legacy builder (more stable on Windows)
$env:DOCKER_BUILDKIT = "0"

# Reset WSL if requested
if ($ResetWSL) {
    Write-Host "Resetting WSL..." -ForegroundColor Yellow
    wsl --shutdown
    Start-Sleep -Seconds 5
    Write-Host "WSL reset complete. Waiting for Docker..." -ForegroundColor Yellow
    Start-Sleep -Seconds 10
}

# Clean up failed layers
Write-Host "Cleaning up Docker build cache..." -ForegroundColor Gray
docker builder prune -f 2>$null | Out-Null

# Build arguments
$buildArgs = @("build", "--platform", "linux/amd64", "-t", "binance-dcr-bot")
if ($Force) {
    $buildArgs += "--no-cache"
}
$buildArgs += "."

Write-Host "Building Docker image..." -ForegroundColor White
Write-Host "(This may take a few minutes on first build)" -ForegroundColor Gray
Write-Host ""

# First attempt
$result = & docker @buildArgs
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host " BUILD SUCCESSFUL!" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "Run your container with:" -ForegroundColor White
    Write-Host "  docker run -d --name binance-bot binance-dcr-bot" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Or run interactively:" -ForegroundColor White
    Write-Host "  docker run -it --rm binance-dcr-bot" -ForegroundColor Yellow
    Write-Host ""
    exit 0
}

# Build failed - check if it's an OCI runtime error
Write-Host ""
Write-Host "============================================================" -ForegroundColor Red
Write-Host " BUILD FAILED - Attempting Recovery" -ForegroundColor Red
Write-Host "============================================================" -ForegroundColor Red
Write-Host ""
Write-Host "Detected possible OCI runtime error. Attempting WSL reset..." -ForegroundColor Yellow
Write-Host ""

# Reset WSL and retry
Write-Host "Step 1: Shutting down WSL..." -ForegroundColor Cyan
wsl --shutdown
Write-Host "Step 2: Waiting 10 seconds for cleanup..." -ForegroundColor Cyan
Start-Sleep -Seconds 10
Write-Host "Step 3: Pruning Docker system..." -ForegroundColor Cyan
docker system prune -f 2>$null | Out-Null
Write-Host "Step 4: Retrying build..." -ForegroundColor Cyan
Write-Host ""

# Second attempt with --no-cache
$buildArgs = @("build", "--platform", "linux/amd64", "--no-cache", "-t", "binance-dcr-bot", ".")
$result = & docker @buildArgs
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host " BUILD SUCCESSFUL (after retry)!" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "Run your container with:" -ForegroundColor White
    Write-Host "  docker run -d --name binance-bot binance-dcr-bot" -ForegroundColor Yellow
    Write-Host ""
    exit 0
}

# Still failing
Write-Host ""
Write-Host "============================================================" -ForegroundColor Red
Write-Host " BUILD STILL FAILING" -ForegroundColor Red
Write-Host "============================================================" -ForegroundColor Red
Write-Host ""
Write-Host "The OCI runtime error persists. Manual steps required:" -ForegroundColor Yellow
Write-Host ""
Write-Host "1. Close Docker Desktop completely" -ForegroundColor White
Write-Host "2. Create/edit file: $env:USERPROFILE\.wslconfig" -ForegroundColor White
Write-Host "   Add these lines:" -ForegroundColor Gray
Write-Host "     [wsl2]" -ForegroundColor Cyan
Write-Host "     memory=4GB" -ForegroundColor Cyan
Write-Host "     processors=2" -ForegroundColor Cyan
Write-Host "     swap=2GB" -ForegroundColor Cyan
Write-Host ""
Write-Host "3. Restart your computer" -ForegroundColor White
Write-Host "4. Start Docker Desktop and wait for it to fully load" -ForegroundColor White
Write-Host "5. Run this script again" -ForegroundColor White
Write-Host ""

exit 1
