@echo off
setlocal

set "ROOT_DIR=%~dp0.."
for %%I in ("%ROOT_DIR%") do set "ROOT_DIR=%%~fI"

set "PORT=8765"
set "AISTOCK_ALLOW_LEGACY_TDX=1"
set "AISTOCK_TDXQ_EAGER_INIT=0"
set "AISTOCK_STRICT_TDXQ_STARTUP=0"
set "AISTOCK_TDXQ_INIT_PATH=D:\TDX\PYPlugins\user\aistock_gateway.py"
set "PYTHON_BIN=python"
if exist "F:\python3.10\python.exe" set "PYTHON_BIN=F:\python3.10\python.exe"
set "LOG_DIR=%ROOT_DIR%\runtime\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>nul
set "LOG_FILE=%LOG_DIR%\tdx_gateway.log"

powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command "try { $r = Invoke-RestMethod -TimeoutSec 3 -Uri 'http://127.0.0.1:%PORT%/health'; if ($r.ready -eq $true -or $r.status -eq 'available') { exit 0 } } catch { exit 1 }; exit 1"
if "%ERRORLEVEL%"=="0" (
  echo Existing AiStock TDX Gateway is healthy on port %PORT%.
  exit /b 0
)

echo ================================
echo AiStock TDX Gateway
echo Root: %ROOT_DIR%
echo URL:  http://127.0.0.1:%PORT%
echo Bind: 0.0.0.0:%PORT%
echo ================================

cd /d "%ROOT_DIR%"
echo [%date% %time%] Starting AiStock TDX Gateway on port %PORT% >> "%LOG_FILE%"
"%PYTHON_BIN%" -m uvicorn services.tdx_gateway:app --host 0.0.0.0 --port %PORT% >> "%LOG_FILE%" 2>&1
echo [%date% %time%] AiStock TDX Gateway exited with code %ERRORLEVEL% >> "%LOG_FILE%"
