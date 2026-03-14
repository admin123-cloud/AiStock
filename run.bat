@echo off
REM StockPy 项目启动脚本 (Windows)

setlocal enabledelayedexpansion

echo ======================================================
echo StockPy - 大盘情绪监控系统
echo ======================================================
echo.

REM 检查Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python未找到，请确保已安装Python 3.9+
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
echo ✓ Python版本: %PYTHON_VERSION%

REM 进入backend目录
cd /d "%~dp0backend"

REM 创建环境文件
if not exist ".env" (
    if exist ".env.example" (
        echo ⚙️  创建环境配置...
        copy .env.example .env >nul
        echo   请根据需要修改.env文件配置
    )
)

REM 安装依赖
echo 📦 安装依赖...
pip install -r requirements.txt

REM 初始化数据库
echo 🗄️  初始化数据库...
python -m app.database

REM 启动应用
echo.
echo 🚀 启动StockPy后端服务...
echo    访问地址: http://localhost:8000
echo    API文档: http://localhost:8000/docs
echo.

python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

echo.
echo ✓ 应用已停止
pause
