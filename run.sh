#!/bin/bash

# StockPy 项目启动脚本

set -e

echo "======================================================"
echo "StockPy - 大盘情绪监控系统"
echo "======================================================"

# 检查Python版本
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo "✓ Python版本: $python_version"

# 创建虚拟环境（如需要）
if [ ! -d "venv" ]; then
    echo "📦 创建虚拟环境..."
    python3 -m venv venv
fi

# 激活虚拟环境
source venv/bin/activate

# 进入backend目录
cd backend

# 创建环境文件
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    echo "⚙️  创建环境配置..."
    cp .env.example .env
    echo "  请根据需要修改.env文件配置"
fi

# 安装依赖
echo "📦 安装依赖..."
pip install -r requirements.txt

# 初始化数据库
echo "🗄️  初始化数据库..."
python3 -m app.database

# 启动应用
echo ""
echo "🚀 启动StockPy后端服务..."
echo "   访问地址: http://localhost:8000"
echo "   API文档: http://localhost:8000/docs"
echo ""

python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

echo ""
echo "✓ 应用已停止"
