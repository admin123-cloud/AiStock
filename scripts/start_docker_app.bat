@echo off
setlocal

set "ROOT_DIR=%~dp0.."
for %%I in ("%ROOT_DIR%") do set "ROOT_DIR=%%~fI"

echo ================================
echo AiStock Docker App
echo Root: %ROOT_DIR%
echo ================================

cd /d "%ROOT_DIR%"
docker compose -f docker-compose.app.yml up -d --no-build

echo.
echo Frontend: http://localhost:3000
echo Backend:  http://localhost:8000
echo Docs:     http://localhost:8000/docs
echo TDX GW:   http://127.0.0.1:8765/health
