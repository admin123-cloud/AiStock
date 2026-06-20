@echo off
setlocal

REM AiStock-core service restart script
REM ==================================

set "ROOT_DIR=%~dp0"
if "%ROOT_DIR:~-1%"=="\" set "ROOT_DIR=%ROOT_DIR:~0,-1%"
set "FRONTEND_DIR=%ROOT_DIR%\frontend"
set "BACKEND_PORT=8000"
set "FRONTEND_PORT=3000"

echo ================================
echo AiStock-core Service Restart
echo Root: %ROOT_DIR%
echo ================================

call :stop_port %BACKEND_PORT% "Backend"
call :stop_port %FRONTEND_PORT% "Frontend"

if not exist "%FRONTEND_DIR%\node_modules" (
    echo Frontend dependencies missing, installing...
    pushd "%FRONTEND_DIR%"
    call npm install
    if errorlevel 1 (
        popd
        echo Frontend dependency install failed
        exit /b 1
    )
    popd
)

echo Starting backend service...
start "AiStock-core Backend" cmd /k "cd /d %ROOT_DIR% && python -m uvicorn api.main:app --host 0.0.0.0 --port %BACKEND_PORT%"

echo Waiting for backend to start...
timeout /t 3 /nobreak > nul

echo Starting frontend service...
start "AiStock-core Frontend" cmd /k "cd /d %FRONTEND_DIR% && npm run dev"

echo ================================
echo Services started successfully
echo Backend: http://localhost:%BACKEND_PORT%
echo Frontend: http://localhost:%FRONTEND_PORT%
echo API Docs: http://localhost:%BACKEND_PORT%/docs
echo ================================
exit /b 0

:stop_port
set "TARGET_PORT=%~1"
set "TARGET_NAME=%~2"
echo Checking %TARGET_NAME% port %TARGET_PORT%...
netstat -ano | findstr :%TARGET_PORT% > nul
if errorlevel 1 (
    echo %TARGET_NAME% is not running
    goto :eof
)

for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%TARGET_PORT%') do (
    taskkill /pid %%a /f > nul 2>nul
    echo %TARGET_NAME% process %%a stopped
)
goto :eof
