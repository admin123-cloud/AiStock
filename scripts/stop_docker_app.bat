@echo off
setlocal

set "ROOT_DIR=%~dp0.."
for %%I in ("%ROOT_DIR%") do set "ROOT_DIR=%%~fI"

echo ================================
echo Stop AiStock Docker App
echo Root: %ROOT_DIR%
echo ================================

cd /d "%ROOT_DIR%"
docker compose -f docker-compose.app.yml stop
