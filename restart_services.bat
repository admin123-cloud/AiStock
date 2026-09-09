@echo off
setlocal

REM AiStock-core service restart script
REM ==================================

set "ROOT_DIR=%~dp0"
if "%ROOT_DIR:~-1%"=="\" set "ROOT_DIR=%ROOT_DIR:~0,-1%"
set "FRONTEND_DIR=%ROOT_DIR%\frontend"
set "LOG_DIR=%ROOT_DIR%\runtime\logs"
set "BACKEND_PORT=8000"
set "FRONTEND_PORT=3000"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>nul

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
powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command "Start-Process -FilePath 'python' -ArgumentList '-m','uvicorn','api.main:app','--host','0.0.0.0','--port','%BACKEND_PORT%' -WorkingDirectory '%ROOT_DIR%' -RedirectStandardOutput '%LOG_DIR%\backend-uvicorn.log' -RedirectStandardError '%LOG_DIR%\backend-uvicorn.err.log' -WindowStyle Hidden"

echo Waiting for backend to start...
timeout /t 3 /nobreak > nul

echo Starting frontend service...
powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','npm run dev -- --host 127.0.0.1 --port %FRONTEND_PORT%' -WorkingDirectory '%FRONTEND_DIR%' -RedirectStandardOutput '%LOG_DIR%\frontend-vite.log' -RedirectStandardError '%LOG_DIR%\frontend-vite.err.log' -WindowStyle Hidden"

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
