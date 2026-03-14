"""
StockPy 项目启动脚本
用于初始化和启动整个项目
"""

import os
import sys
import subprocess
from pathlib import Path

def check_python_version():
    """检查Python版本"""
    if sys.version_info < (3, 9):
        print("❌ 需要Python 3.9或更高版本")
        sys.exit(1)
    print(f"✓ Python版本: {sys.version.split()[0]}")

def install_dependencies():
    """安装项目依赖"""
    print("\n📦 安装依赖...")
    
    # 后端依赖
    backend_path = Path("backend")
    if backend_path.exists():
        print("  - 安装后端依赖...")
        subprocess.run([
            sys.executable, "-m", "pip", "install", "-r",
            str(backend_path / "requirements.txt")
        ], check=False)
    
    print("✓ 依赖安装完成")

def create_env_file():
    """创建环境配置文件"""
    print("\n⚙️  创建环境配置...")
    
    env_path = Path("backend/.env")
    env_example = Path("backend/.env.example")
    
    if env_example.exists() and not env_path.exists():
        import shutil
        shutil.copy(env_example, env_path)
        print(f"✓ 已创建 .env 文件（从.env.example复制）")
        print("  请根据需要修改.env文件配置")
    elif env_path.exists():
        print("✓ .env 文件已存在")

def init_database():
    """初始化数据库"""
    print("\n🗄️  初始化数据库...")
    
    try:
        from app.database import init_db
        init_db()
        print("✓ 数据库初始化完成")
    except Exception as e:
        print(f"❌ 数据库初始化失败: {e}")

def start_backend():
    """启动后端服务"""
    print("\n🚀 启动后端服务...")
    print("  访问地址: http://localhost:8000")
    print("  API文档: http://localhost:8000/docs")
    
    try:
        subprocess.run([
            sys.executable, "-m", "uvicorn",
            "app.main:app",
            "--host", "0.0.0.0",
            "--port", "8000",
            "--reload"
        ])
    except KeyboardInterrupt:
        print("\n✓ 后端服务已停止")

def start_frontend():
    """启动前端服务"""
    print("\n🌐 启动前端服务...")
    print("  访问地址: http://localhost:3000")
    
    frontend_path = Path("frontend/src")
    os.chdir(frontend_path)
    
    try:
        subprocess.run([
            sys.executable, "-m", "http.server", "3000"
        ])
    except KeyboardInterrupt:
        print("\n✓ 前端服务已停止")

def main():
    """主函数"""
    print("=" * 60)
    print("StockPy - 大盘情绪监控系统")
    print("=" * 60)
    
    # 检查Python版本
    check_python_version()
    
    # 切换到backend目录
    os.chdir(Path(__file__).parent / "backend")
    
    # 创建环境文件
    create_env_file()
    
    # 安装依赖
    install_dependencies()
    
    # 初始化数据库
    try:
        init_database()
    except Exception as e:
        print(f"⚠️  数据库初始化失败: {e}")
        print("  将在启动时自动创建表")
    
    # 启动后端
    try:
        start_backend()
    except KeyboardInterrupt:
        print("\n✓ 应用已停止")
        sys.exit(0)

if __name__ == "__main__":
    main()
