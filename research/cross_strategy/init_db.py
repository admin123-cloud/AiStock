"""
数据库表初始化脚本

创建所有数据表
"""

import sys
import os

# 添加项目根目录到Python路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from utils.database import db
from utils.logger import get_logger

logger = get_logger("init_db")

def init_database():
    """初始化数据库表"""
    try:
        logger.info("开始创建数据库表...")
        
        # 创建所有数据表
        db.create_tables()
        
        logger.info("数据库表创建成功")
        return True
    except Exception as e:
        logger.error(f"数据库表创建失败: {e}")
        return False

if __name__ == "__main__":
    success = init_database()
    if success:
        print("数据库表创建成功")
    else:
        print("数据库表创建失败")
