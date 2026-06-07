"""清理K线中的无效数据(volume=0的重复记录)"""

import sys
import os

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.database import db
from sqlalchemy import text

conn = db.engine.connect()

# 找出所有volume=0且存在重复的记录
result = conn.execute(text("""
    SELECT code, trade_date, COUNT(*) as cnt
    FROM kline_daily
    WHERE volume = 0 OR volume IS NULL
    GROUP BY code, trade_date
    HAVING COUNT(*) > 1
"""))
duplicates = result.fetchall()

print(f"发现 {len(duplicates)} 组无效重复数据")

if duplicates:
    print("\n正在清理...")
    deleted_count = 0
    for code, trade_date, cnt in duplicates:
        # 删除volume=0或volume=NULL的重复记录，保留一条
        conn.execute(text(f"""
            DELETE FROM kline_daily
            WHERE code = '{code}'
            AND trade_date = '{trade_date}'
            AND (volume = 0 OR volume IS NULL)
            AND rowNumberInAllBlocks() > 1
        """))
        deleted_count += cnt - 1  # 每组删除cnt-1条

    conn.execute(text("SYSTEM FLUSH LOGS"))
    conn.commit()
    print(f"已删除 {deleted_count} 条无效数据")

conn.close()
print("\n清理完成")