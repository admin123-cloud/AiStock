"""
查询过去30天所有板块的涨跌幅，并记录到sector_kline_daily表

使用方法:
    python scripts/fetch_sector_change_pct.py
"""
import sys
sys.path.insert(0, '.')

from datetime import datetime, timedelta
from decimal import Decimal
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
import pandas as pd

from data_fetcher.sources.tdxquant import TdxQuantDataSource
from data_fetcher.sources.tdxquant_pool import tdxquant_pool
from models.stock_models import SectorKlineDaily
from utils.database import db
from utils.logger import get_logger

logger = get_logger("FetchSectorChangePct")


def get_sector_list():
    """
    获取所有板块列表
    
    Returns:
        list: 板块列表，每个板块包含code和name
    """
    try:
        # 使用 TdxQuantDataSource 获取板块列表
        tdxquant = TdxQuantDataSource("tdxquant", {"enabled": True, "priority": 0})
        result = tdxquant.get_sector_list()
        
        if not result:
            logger.warning("获取板块列表失败: 无数据")
            return []
        
        logger.info(f"获取板块列表成功: {len(result)} 个板块")
        return result
        
    except Exception as e:
        logger.error(f"获取板块列表异常: {e}")
        return None


def get_sector_kline(sector_code, start_date, end_date):
    """
    获取板块K线数据
    
    Args:
        sector_code: 板块代码
        start_date: 开始日期（YYYYMMDD）
        end_date: 结束日期（YYYYMMDD）
    
    Returns:
        list: K线数据列表
    """
    try:
        # 使用 TdxQuantDataSource 获取板块K线数据
        tdxquant = TdxQuantDataSource("tdxquant", {"enabled": True, "priority": 0})
        
        # 转换日期格式为 YYYY-MM-DD
        start_date_formatted = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        end_date_formatted = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
        
        kline_data = tdxquant.get_stock_history(
            stock_code=sector_code,
            start_date=start_date_formatted,
            end_date=end_date_formatted,
            period='1d',
            dividend_type='none'
        )
        
        if not kline_data or kline_data.empty:
            logger.warning(f"获取板块 {sector_code} K线数据失败: 无数据")
            return None
        
        # 构建K线数据列表
        klines = []
        
        # 遍历每一行（每个日期）
        for i, row in kline_data.iterrows():
            try:
                # 获取日期
                if 'trade_date' in row:
                    trade_date = row['trade_date'].date() if hasattr(row['trade_date'], 'date') else pd.to_datetime(row['trade_date']).date()
                else:
                    logger.warning(f"数据中无trade_date字段: {row}")
                    continue
                
                close_price = float(row['close'])
                open_price = float(row['open'])
                high_price = float(row['high'])
                low_price = float(row['low'])
                volume = int(row['volume'])
                amount = float(row['amount'])
                
                # 计算涨跌幅（与前一日比较）
                change_pct = 0.0
                if i < len(kline_data) - 1:
                    prev_close = float(kline_data.iloc[i + 1]['close'])
                    if prev_close > 0:
                        change_pct = (close_price - prev_close) / prev_close * 100
                
                klines.append({
                    'trade_date': trade_date,
                    'open': open_price,
                    'high': high_price,
                    'low': low_price,
                    'close': close_price,
                    'volume': volume,
                    'amount': amount,
                    'change_pct': round(change_pct, 2)
                })
            except Exception as e:
                logger.warning(f"处理第 {i} 条数据失败: {e}")
                continue
        
        return klines
        
    except Exception as e:
        logger.error(f"获取板块 {sector_code} K线数据异常: {e}")
        return None


def save_sector_kline_to_db(session, sector_code, klines):
    """
    保存板块K线数据到数据库
    
    Args:
        session: 数据库会话
        sector_code: 板块代码
        klines: K线数据列表
    
    Returns:
        int: 保存的记录数
    """
    saved_count = 0
    
    try:
        for kline in klines:
            try:
                # 检查是否已存在
                existing = session.query(SectorKlineDaily).filter(
                    and_(
                        SectorKlineDaily.code == sector_code,
                        SectorKlineDaily.trade_date == kline['trade_date']
                    )
                ).first()
                
                if existing:
                    # 更新现有记录
                    existing.change_pct = Decimal(str(kline['change_pct']))
                    existing.total_volume = int(kline['volume'])
                    existing.total_amount = Decimal(str(kline['amount']))
                    existing.created_at = datetime.now()
                else:
                    # 创建新记录
                    new_record = SectorKlineDaily(
                        code=sector_code,
                        trade_date=kline['trade_date'],
                        change_pct=Decimal(str(kline['change_pct'])),
                        total_volume=int(kline['volume']),
                        total_amount=Decimal(str(kline['amount'])),
                        created_at=datetime.now()
                    )
                    session.add(new_record)
                
                saved_count += 1
                
            except IntegrityError as e:
                logger.warning(f"保存记录失败（可能已存在）: {e}")
                session.rollback()
                continue
            except Exception as e:
                logger.error(f"保存记录失败: {e}")
                session.rollback()
                continue
        
        # 提交事务
        session.commit()
        return saved_count
        
    except Exception as e:
        logger.error(f"保存板块 {sector_code} K线数据到数据库异常: {e}")
        session.rollback()
        return 0


def main():
    """
    主函数：查询过去30天所有板块的涨跌幅并记录到数据库
    """
    print("=" * 60)
    print("开始查询过去30天所有板块的涨跌幅")
    print("=" * 60)
    
    # 计算日期范围
    end_date = datetime.now()
    start_date = end_date - timedelta(days=30)
    
    start_date_str = start_date.strftime('%Y%m%d')
    end_date_str = end_date.strftime('%Y%m%d')
    
    print(f"日期范围: {start_date_str} 至 {end_date_str}")
    
    # 1. 获取所有板块列表
    print("\n1. 获取板块列表...")
    sector_list = get_sector_list()
    
    if not sector_list:
        print("❌ 获取板块列表失败")
        return
    
    print(f"✅ 获取板块列表成功: {len(sector_list)} 个板块")
    
    # 2. 获取数据库会话
    session = next(db.get_session())
    
    try:
        # 3. 遍历每个板块，获取K线数据并保存
        print(f"\n2. 开始获取板块K线数据...")
        total_saved = 0
        
        for i, sector in enumerate(sector_list):
            sector_code = sector['code']
            sector_name = sector['name']
            
            print(f"\n  [{i+1}/{len(sector_list)}] 处理板块: {sector_code} ({sector_name})")
            
            # 获取板块K线数据
            klines = get_sector_kline(sector_code, start_date_str, end_date_str)
            
            if not klines:
                print(f"    ⚠️ 无K线数据")
                continue
            
            print(f"    ✅ 获取K线数据: {len(klines)} 条")
            
            # 保存到数据库
            saved_count = save_sector_kline_to_db(session, sector_code, klines)
            total_saved += saved_count
            
            print(f"    ✅ 保存到数据库: {saved_count} 条")
        
        print(f"\n" + "=" * 60)
        print(f"处理完成！")
        print(f"总板块数: {len(sector_list)}")
        print(f"总保存记录数: {total_saved}")
        print("=" * 60)
        
    except Exception as e:
        logger.error(f"处理异常: {e}")
        print(f"❌ 处理异常: {e}")
        
    finally:
        session.close()


if __name__ == "__main__":
    main()
