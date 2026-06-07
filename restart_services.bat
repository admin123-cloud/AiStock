@echo off

REM AiStock Service Restart Script
REM ================================

echo ================================
echo AiStock Service Restart Script
echo ================================

REM Check backend service port (8000)
echo Checking backend service status...
netstat -ano | findstr :8000 > nul
if %errorlevel% equ 0 (
    echo Backend service is running, stopping...
    REM Find and kill process occupying port 8000
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000') do (
        taskkill /pid %%a /f > nul
        echo Backend process %%a stopped
    )
) else (
    echo Backend service is not running
)

REM Check frontend service port (5173, Vite default port)
echo Checking frontend service status...
netstat -ano | findstr :5173 > nul
if %errorlevel% equ 0 (
    echo Frontend service is running, stopping...
    REM Find and kill process occupying port 5173
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr :5173') do (
        taskkill /pid %%a /f > nul
        echo Frontend process %%a stopped
    )
) else (
    echo Frontend service is not running
)

REM Start backend service
echo Starting backend service...
start "AiStock Backend" cmd /c "cd /d f:\Stock\AiStock && uvicorn api.main:app"

REM Wait 2 seconds for backend to start
echo Waiting for backend to start...
timeout /t 2 /nobreak > nul

REM Start frontend service
echo Starting frontend service...
start "AiStock Frontend" cmd /c "cd /d f:\Stock\AiStock\frontend && npm run dev"

echo ================================
echo Services started successfully!
echo Backend: http://localhost:8000
echo Frontend: http://localhost:3000
echo API Docs: http://localhost:8000/docs
echo ================================

REM Wait for user input before exit
echo Press any key to exit...
pause > nul