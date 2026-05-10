@echo off
REM ----------------------------------------------------------------------------
REM build-binaries.bat - Windows host driver for Docker-based binary builds.
REM Mirrors scripts/build-binaries.sh.
REM
REM Usage:
REM   scripts\build-binaries.bat                 :: build all targets
REM   scripts\build-binaries.bat linux/amd64
REM   scripts\build-binaries.bat linux/arm64 windows/amd64 windows/arm64
REM
REM Requirements:
REM   - Docker Desktop (with buildx)
REM   - For cross-arch (linux/arm64), enable QEMU once:
REM       docker run --privileged --rm tonistiigi/binfmt --install all
REM ----------------------------------------------------------------------------
setlocal enabledelayedexpansion

cd /d "%~dp0\.."

set "TARGETS=%*"
if "%TARGETS%"=="" set "TARGETS=windows/amd64 windows/arm64 linux/amd64 linux/arm64"

where docker >nul 2>&1 || (echo [err] docker not on PATH & exit /b 1)
docker buildx version >nul 2>&1 || (echo [err] docker buildx missing & exit /b 1)

docker buildx inspect bot-binary-builder >nul 2>&1
if errorlevel 1 (
    echo ==^> Creating buildx builder 'bot-binary-builder'
    docker buildx create --name bot-binary-builder --use >nul
) else (
    docker buildx use bot-binary-builder >nul
)

if not exist dist mkdir dist

for %%T in (%TARGETS%) do (
    if /i "%%T"=="linux/amd64"   call :build_linux amd64
    if /i "%%T"=="linux/arm64"   call :build_linux arm64
    if /i "%%T"=="windows/amd64" call :build_windows amd64
    if /i "%%T"=="windows/arm64" call :build_windows arm64
)

echo.
echo ==^> Done. Artifacts in .\dist:
dir /b dist\binance-bot-*
exit /b 0

:build_linux
set "ARCH=%~1"
set "OUT=dist\_tmp-linux-%ARCH%"
echo.
echo ==^> Building linux/%ARCH% -^> dist\binance-bot-linux-%ARCH%
if exist "%OUT%" rmdir /s /q "%OUT%"
docker buildx build --platform linux/%ARCH% -f Dockerfile.binary -o "type=local,dest=%OUT%" . || exit /b 1
move /y "%OUT%\binance-bot" "dist\binance-bot-linux-%ARCH%" >nul
rmdir /s /q "%OUT%"
goto :eof

:build_windows
set "ARCH=%~1"
set "OUT=dist\_tmp-windows-%ARCH%"
echo.
echo ==^> Building windows/%ARCH% -^> dist\binance-bot-windows-%ARCH%.exe
if exist "%OUT%" rmdir /s /q "%OUT%"
docker buildx build --platform linux/%ARCH% -f Dockerfile.binary.windows -o "type=local,dest=%OUT%" . || exit /b 1
move /y "%OUT%\binance-bot.exe" "dist\binance-bot-windows-%ARCH%.exe" >nul
rmdir /s /q "%OUT%"
goto :eof
