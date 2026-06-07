"""
自动检测并拉取完整K线历史数据

当检测到第一根K线的时间明显晚于上市时间时，自动从TickFlow拉取完整历史数据
"""

from datetime import datetime, timedelta, date
from utils.database import db_manager
from models import Stock, KlineDaily, KlineWeekly, KlineMonthly, KlineQuarterly, KlineYearly
from data_fetcher.pytdx import PytdxDataSource
from sqlalchemy import func
import asyncio

# 周期配置（使用统一的时间单位标准）
# 标准格式：1m, 5m, 15m, 30m, 60m, 1d, 1w, 1mon, 1q, 1y
PERIOD_CONFIG = {
    '1d': {'limit': 5000, 'description': '日线'},
    '1w': {'limit': 2000, 'description': '周线'},
    '1mon': {'limit': 600, 'description': '月线'},
    '1q': {'limit': 200, 'description': '季线'},
    '1y': {'limit': 100, 'description': '年线'}
}

# 允许的最大数据差距（天数）
MAX_DATA_GAP_DAYS = {
    '1d': 90,      # 日线允许90天差距
    '1w': 180,     # 周线允许6个月差距
    '1mon': 365,   # 月线允许1年差距
    '1q': 730,     # 季线允许2年差距
    '1y': 1825     # 年线允许5年差距
}


def get_first_kline_date(session, model_class, code):
    """获取第一根K线的日期"""
    date_field = None
    if model_class == KlineDaily:
        date_field = KlineDaily.trade_date
    elif model_class == KlineWeekly:
        date_field = KlineWeekly.week_start_date
    elif model_class == KlineMonthly:
        date_field = KlineMonthly.month_start_date
    elif model_class == KlineQuarterly:
        date_field = KlineQuarterly.year

    if date_field is None:
        return None

    first_kline = session.query(model_class).filter(
        model_class.code == code
    ).order_by(date_field.asc()).first()

    if first_kline:
        if model_class == KlineQuarterly:
            return datetime(first_kline.year, (first_kline.quarter - 1) * 3 + 1, 1)
        else:
            return date_field.__get__(first_kline)
    return None


async def sync_full_history_for_stock(session, code, stock_info):
    """为单只股票同步完整历史数据"""
    print(f"\n{'='*60}")
    print(f"检查股票 {stock_info.name} ({code})")
    print(f"上市日期: {stock_info.list_date}")
    print(f"{'='*60}")

    # 获取 pytdx 配置
    from utils.config import config as config_manager
    pytdx_config = config_manager.get("data_sources", {}).get("pytdx", {"enabled": True, "batch_size": 150})
    client = PytdxDataSource(pytdx_config)

    for period, config in PERIOD_CONFIG.items():
        print(f"\n检查{config['description']}...")

        # 获取第一根K线日期
        # 使用统一的时间单位标准：1d, 1w, 1mon, 1q, 1y
        model_class = {
            '1d': KlineDaily,
            '1w': KlineWeekly,
            '1mon': KlineMonthly,
            '1q': KlineQuarterly,
            '1y': KlineYearly
        }.get(period)

        first_kline_date = get_first_kline_date(session, model_class, code)

        if not first_kline_date:
            print(f"  ⚠️  没有{config['description']}数据，开始同步...")

            try:
                # 拉取完整历史数据
                print(f"  正在从Pytdx拉取{config['limit']}条{config['description']}...")
                # 转换周期格式（使用统一的时间单位标准）
                from utils.period_constants import PERIOD_TO_TDX
                pytdx_period = PERIOD_TO_TDX.get(period, period)
                
                # 计算开始日期
                end_date = date.today().strftime('%Y%m%d')
                start_date = stock_info.list_date.strftime('%Y%m%d') if stock_info.list_date else '20000101'
                
                result = client.get_stock_history(code, start_date, end_date, pytdx_period)
                print(f"  成功获取 {len(result)} 条{config['description']}")
            except Exception as e:
                print(f"  拉取失败: {e}")
        else:
            # 计算日期差距
            date_diff = (first_kline_date.date() - stock_info.list_date).days

            print(f"  第一根K线: {first_kline_date.date()}")
            print(f"  日期差距: {date_diff}天")

            if date_diff > MAX_DATA_GAP_DAYS[period]:
                print(f"  ⚠️  检测到数据缺失（差距{date_diff}天 > {MAX_DATA_GAP_DAYS[period]}天）")
                print(f"  正在从Pytdx拉取完整{config['description']}数据...")

                try:
                    # 转换周期格式
                    pytdx_period = period
                    # 转换周期格式（使用统一的时间单位标准）
                    from utils.period_constants import PERIOD_TO_TDX
                    pytdx_period = PERIOD_TO_TDX.get(period, period)
                    
                    # 计算开始日期
                    end_date = date.today().strftime('%Y%m%d')
                    start_date = stock_info.list_date.strftime('%Y%m%d') if stock_info.list_date else '20000101'
                    
                    result = client.get_stock_history(code, start_date, end_date, pytdx_period)
                    print(f"  成功获取 {len(result)} 条{config['description']}")
                except Exception as e:
                    print(f"  拉取失败: {e}")
            else:
                print(f"  {config['description']}数据完整")


async def sync_all_stocks(limit=None):
    """同步所有需要补全数据的股票"""
    session = next(db_manager.get_session())

    try:
        # 获取所有股票
        query = session.query(Stock).filter(Stock.list_date.isnot(None))
        if limit:
            query = query.limit(limit)

        stocks = query.all()

        print(f"\n共检查{len(stocks)} 只股票\n")

        for stock in stocks:
            await sync_full_history_for_stock(session, stock.code, stock)

    finally:
        session.close()


async def sync_specific_stock(code):
    """同步指定股票"""
    session = next(db_manager.get_session())

    try:
        stock = session.query(Stock).filter(Stock.code == code).first()
        if not stock:
            print(f"股票 {code} 不存在")
            return

        await sync_full_history_for_stock(session, code, stock)

    finally:
        session.close()


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description='同步完整K线历史数据')
    parser.add_argument('--code', type=str, help='指定股票代码')
    parser.add_argument('--limit', type=int, help='检查股票数量限制')
    parser.add_argument('--all', action='store_true', help='检查所有股票')

    args = parser.parse_args()

    if args.code:
        # 同步指定股票
        print(f"同步股票: {args.code}")
        asyncio.run(sync_specific_stock(args.code))
    elif args.all:
        # 同步所有股票
        print("同步所有股票...")
        asyncio.run(sync_all_stocks())
    else:
        # 默认检查前100只股票
        print("检查前100只股票...")
        asyncio.run(sync_all_stocks(limit=100))


if __name__ == "__main__":
    main()
