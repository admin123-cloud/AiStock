"""
紧急恢复日线数据脚本 - 针对5月11日缺失数据

该脚本用于：
1. 检查5月11日日线数据完整性
2. 找出缺失数据的股票列表
3. 批量补全缺失数据
"""

import sys
import os
from datetime import datetime, timedelta
from typing import List, Dict
import pandas as pd

# 添加项目根目录到Python搜索路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from data_fetcher.sources.tdxquant import TdxQuantDataSource
from utils.database import db
from utils.logger import get_logger

logger = get_logger("EmergencyBackfillDaily")

TARGET_DATE = "2026-05-11"  # 目标补全日期


def get_missing_stocks(target_date: str) -> List[str]:
    """获取指定日期缺少日线数据的股票列表"""
    from sqlalchemy import text
    
    logger.info(f"正在检查 {target_date} 的日线数据完整性...")
    
    with db.engine.connect() as conn:
        # 获取所有活跃股票代码
        all_stocks = conn.execute(
            text("SELECT code FROM stocks WHERE type = 'stock' AND quit = 0 ORDER BY code")
        ).fetchall()
        all_codes = set([row[0] for row in all_stocks])
        
        # 获取已有数据的股票代码
        existing_stocks = conn.execute(
            text("SELECT DISTINCT code FROM kline_daily WHERE trade_date = :target_date"),
            {"target_date": target_date}
        ).fetchall()
        existing_codes = set([row[0] for row in existing_stocks])
        
        # 计算缺失的股票
        missing_list = sorted(list(all_codes - existing_codes))
    
    logger.info(f"股票总数: {len(all_codes)}, 已有数据: {len(existing_codes)}, 缺失数量: {len(missing_list)}")
    
    if missing_list:
        logger.info(f"缺失股票列表(前20个): {missing_list[:20]}")
    
    return missing_list


def fetch_and_save_daily_data(codes: List[str], target_date: str):
    """获取并保存日线数据"""
    tdxquant = TdxQuantDataSource(name="tdxquant", config={"enabled": True})
    
    success_count = 0
    fail_count = 0
    total = len(codes)
    
    logger.info(f"开始补全 {len(codes)} 只股票的 {target_date} 日线数据...")
    
    for i, code in enumerate(codes, 1):
        try:
            # 获取当天的日线数据
            df = tdxquant.get_stock_history(
                stock_code=code,
                start_date=target_date,
                end_date=target_date,
                period="1d"
            )
            
            if df is not None and not df.empty:
                # 保存到数据库
                save_daily_to_db(code, df)
                success_count += 1
                if i % 50 == 0:
                    logger.info(f"进度: {i}/{total}, 成功: {success_count}, 失败: {fail_count}")
            else:
                logger.warning(f"股票 {code} 未获取到数据")
                fail_count += 1
                
        except Exception as e:
            logger.error(f"股票 {code} 获取数据失败: {e}")
            fail_count += 1
    
    logger.info(f"补全完成: 成功 {success_count} 只, 失败 {fail_count} 只")


def save_daily_to_db(code: str, df: pd.DataFrame):
    """保存日线数据到数据库"""
    from models.stock_models import KlineDaily
    from sqlalchemy import insert
    
    session = next(db.get_session())
    try:
        insert_data = []
        for _, row in df.iterrows():
            # 支持 date 或 datetime 列名
            date_val = row.get("date") or row.get("datetime")
            if pd.isna(date_val):
                continue
                
            trade_date = date_val.date() if hasattr(date_val, "date") else None
            if not trade_date:
                continue
                
            insert_data.append({
                'code': code,
                'trade_date': trade_date,
                'open': row["open"],
                'high': row["high"],
                'low': row["low"],
                'close': row["close"],
                'volume': row["volume"],
                'amount': row["amount"]
            })
        
        if insert_data:
            session.execute(insert(KlineDaily), insert_data)
            session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"保存股票 {code} 数据失败: {e}")
        raise
    finally:
        session.close()


def verify_backfill(target_date: str):
    """验证补全结果"""
    from sqlalchemy import text
    
    logger.info(f"\n验证 {target_date} 日线数据补全结果...")
    
    with db.engine.connect() as conn:
        total_stocks = conn.execute(
            text("SELECT COUNT(*) FROM stocks WHERE type = 'stock' AND quit = 0")
        ).scalar() or 0
        
        existing_count = conn.execute(
            text(
                """
                SELECT COUNT(DISTINCT code) 
                FROM kline_daily 
                WHERE trade_date = :target_date
                """
            ),
            {"target_date": target_date}
        ).scalar() or 0
        
        completeness = (existing_count / total_stocks) * 100 if total_stocks > 0 else 0
        
        logger.info(f"股票总数: {total_stocks}")
        logger.info(f"已有数据: {existing_count}")
        logger.info(f"完整度: {completeness:.2f}%")
        
        return completeness


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("紧急恢复日线数据脚本")
    logger.info(f"目标日期: {TARGET_DATE}")
    logger.info("=" * 60)
    
    # 1. 查找缺失数据的股票
    missing_stocks = get_missing_stocks(TARGET_DATE)
    
    if not missing_stocks:
        logger.info(f"{TARGET_DATE} 的日线数据已经完整，无需补全")
        verify_backfill(TARGET_DATE)
        return
    
    # 2. 分批补全数据（每批50只）
    batch_size = 50
    total_batches = (len(missing_stocks) + batch_size - 1) // batch_size
    
    for batch_num in range(total_batches):
        start = batch_num * batch_size
        end = start + batch_size
        batch_codes = missing_stocks[start:end]
        
        logger.info(f"\n处理第 {batch_num + 1}/{total_batches} 批，共 {len(batch_codes)} 只股票")
        fetch_and_save_daily_data(batch_codes, TARGET_DATE)
        
        # 批次间延迟，避免请求过快
        import time
        if batch_num < total_batches - 1:
            logger.info("等待2秒后继续...")
            time.sleep(2)
    
    # 3. 验证结果
    verify_backfill(TARGET_DATE)
    
    logger.info("\n" + "=" * 60)
    logger.info("补全任务完成")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()