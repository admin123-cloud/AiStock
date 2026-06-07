"""检查过去30天的K线数据完整性

检查内容：
- 每个交易日的股票数量
- 数据缺失情况
- 涨跌幅字段完整性
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, date, timedelta
from sqlalchemy import func, and_
from utils.database import db
from utils.logger import get_logger
from models.stock_models import KlineDaily

logger = get_logger("CheckKlineData")


def check_kline_data(days=30):
    """检查过去N天的K线数据完整性"""
    session = next(db.get_session())
    
    try:
        end_date = date.today()
        start_date = end_date - timedelta(days=days - 1)
        
        logger.info(f"检查日期范围: {start_date} 到 {end_date}")
        
        # 获取日期范围内的所有交易日
        trade_dates = session.query(KlineDaily.trade_date).filter(
            KlineDaily.trade_date >= start_date,
            KlineDaily.trade_date <= end_date
        ).distinct().order_by(KlineDaily.trade_date.desc()).all()
        
        trade_dates = [d[0] for d in trade_dates]
        
        logger.info(f"共找到 {len(trade_dates)} 个交易日")
        
        if not trade_dates:
            logger.warning("未找到任何交易日数据")
            return
        
        # 检查每个交易日的数据
        total_stocks = 0
        total_missing_change_pct = 0
        total_missing_close = 0
        
        print("\n" + "="*80)
        print(f"{'交易日期':<15} {'股票数量':<10} {'缺失涨跌幅':<12} {'缺失收盘价':<12}")
        print("="*80)
        
        for trade_date in trade_dates:
            # 查询该日的所有股票
            klines = session.query(KlineDaily).filter(
                KlineDaily.trade_date == trade_date
            ).all()
            
            stock_count = len(klines)
            missing_change_pct = sum(1 for k in klines if k.change_pct is None)
            missing_close = sum(1 for k in klines if k.close is None)
            
            total_stocks += stock_count
            total_missing_change_pct += missing_change_pct
            total_missing_close += missing_close
            
            print(f"{trade_date.strftime('%Y-%m-%d'):<15} {stock_count:<10} "
                  f"{missing_change_pct:<12} {missing_close:<12}")
        
        print("="*80)
        print(f"{'总计':<15} {total_stocks:<10} {total_missing_change_pct:<12} {total_missing_close:<12}")
        print("="*80)
        
        # 统计信息
        avg_stocks_per_day = total_stocks / len(trade_dates) if trade_dates else 0
        
        print(f"\n统计信息:")
        print(f"  交易日数量: {len(trade_dates)}")
        print(f"  平均每日股票数: {avg_stocks_per_day:.0f}")
        print(f"  涨跌幅缺失率: {total_missing_change_pct/total_stocks*100:.2f}%")
        print(f"  收盘价缺失率: {total_missing_close/total_stocks*100:.2f}%")
        
        # 检查是否有严重缺失
        if total_missing_change_pct > total_stocks * 0.1:
            logger.warning("警告: 涨跌幅数据缺失超过10%，建议先修复K线数据")
        if total_missing_close > total_stocks * 0.1:
            logger.warning("警告: 收盘价数据缺失超过10%，建议先更新K线数据")
        
        # 检查市场分布
        print(f"\n市场分布 (最新交易日 {trade_dates[0].strftime('%Y-%m-%d')}):")
        latest_date = trade_dates[0]
        
        sh_count = session.query(func.count(KlineDaily.code)).filter(
            KlineDaily.trade_date == latest_date,
            KlineDaily.code.like('6%')
        ).scalar()
        
        sz_count = session.query(func.count(KlineDaily.code)).filter(
            KlineDaily.trade_date == latest_date,
            (KlineDaily.code.like('0%') | KlineDaily.code.like('3%'))
        ).scalar()
        
        cyb_count = session.query(func.count(KlineDaily.code)).filter(
            KlineDaily.trade_date == latest_date,
            KlineDaily.code.like('3%')
        ).scalar()
        
        print(f"  上证市场 (6开头): {sh_count} 只")
        print(f"  深证市场 (0/3开头): {sz_count} 只")
        print(f"  创业板 (3开头): {cyb_count} 只")
        
        return {
            'trade_days': len(trade_dates),
            'total_stocks': total_stocks,
            'avg_stocks_per_day': avg_stocks_per_day,
            'missing_change_pct': total_missing_change_pct,
            'missing_close': total_missing_close
        }
        
    finally:
        session.close()


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='检查K线数据完整性')
    parser.add_argument('days', type=int, nargs='?', default=30, help='检查天数，默认30天')
    
    args = parser.parse_args()
    
    check_kline_data(args.days)
