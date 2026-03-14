@echo off
echo 正在重新启动 StockPy 服务...

echo.
echo 停止后端服务...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq uvicorn*" 2>nul
taskkill /F /IM python.exe /FI "WINDOWTITLE eq http.server*" 2>nul

timeout /t 3 /nobreak

echo.
echo 启动后端服务...
start /B F:\StockPy\backend python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

timeout /t 2 /nobreak

echo 启动前端服务...
start /B F:\StockPy\frontend python -m http.server 3000 --directory src

echo.
echo StockPy 服务重新启动完成！
echo 后端: http://localhost:8000
echo 前端: http://localhost:3000
pause