"""
生成板块日K线数据

根据成分股的历史K线数据，按加权平均方式计算板块的涨跌幅和各项统计指标
"""

import sys
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_

# 添加项目根目录到路径
sys.path.append('.')

from utils.database import db
from models.stock_models import Sector, SectorStock, KlineDaily, SectorKlineDaily
from utils.logger import get_logger

logger = get_logger("generate_sector_kline")


def calculate_sector_kline(session, sector_code, trade_date):
    """
    计算单个板块在指定交易日的K线数据

    Args:
        session: 数据库会话
        sector_code: 板块代码
        trade_date: 交易日期

    Returns:
        dict: 板块K线数据
    """
    try:
        # 获取板块的成分股
        sector_stocks = session.query(SectorStock.stock_code).filter(
            SectorStock.sector_code == sector_code
        ).all()

        if not sector_stocks:
            # 板块没有成分股，返回默认值
            return {
                'code': sector_code,
                'trade_date': trade_date,
                'change_pct': 0,
                'stock_count': 0,
                'rise_count': 0,
                'fall_count': 0,
                'flat_count': 0,
                'limit_up_count': 0,
                'limit_down_count': 0,
                'open': 0,
                'high': 0,
                'low': 0,
                'close': 0,
                'total_amount': Decimal('0'),
                'total_volume': 0
            }

        stock_codes = [ss.stock_code for ss in sector_stocks]

        # 查询成分股在指定交易日的K线数据
        kline_data = session.query(KlineDaily).filter(
            and_(
                KlineDaily.code.in_(stock_codes),
                KlineDaily.trade_date == trade_date
            )
        ).all()

        if not kline_data:
            # 板块有成分股，但在指定交易日没有K线数据，返回默认值
            return {
                'code': sector_code,
                'trade_date': trade_date,
                'change_pct': 0,
                'stock_count': len(stock_codes),
                'rise_count': 0,
                'fall_count': 0,
                'flat_count': 0,
                'limit_up_count': 0,
                'limit_down_count': 0,
                'open': 0,
                'high': 0,
                'low': 0,
                'close': 0,
                'total_amount': Decimal('0'),
                'total_volume': 0
            }

        # 计算板块K线统计数据
        total_change_pct = 0
        total_amount = Decimal('0')
        total_volume = 0
        total_open = Decimal('0')
        total_high = Decimal('0')
        total_low = Decimal('0')
        total_close = Decimal('0')

        rise_count = 0  # 上涨家数
        fall_count = 0  # 下跌家数
        flat_count = 0  # 平盘家数
        limit_up_count = 0  # 涨停家数
        limit_down_count = 0  # 跌停家数

        valid_count = 0

        for kline in kline_data:
            # 累加各项指标
            total_amount += kline.amount if kline.amount else Decimal('0')
            total_volume += kline.volume if kline.volume else 0

            # 计算加权价格（按成交量加权）
            volume = kline.volume if kline.volume else 0
            if volume > 0:
                total_open += (Decimal(str(kline.open)) * volume) if kline.open else Decimal('0')
                total_high += (Decimal(str(kline.high)) * volume) if kline.high else Decimal('0')
                total_low += (Decimal(str(kline.low)) * volume) if kline.low else Decimal('0')
                total_close += (Decimal(str(kline.close)) * volume) if kline.close else Decimal('0')

            if kline.change_pct is not None:
                total_change_pct += float(kline.change_pct)

                # 统计涨跌家数
                if kline.change_pct > 0:
                    rise_count += 1
                    # 涨停判断（涨幅 >= 9.9% 且非ST）
                    if kline.change_pct >= 9.9 and 'ST' not in kline.code:
                        limit_up_count += 1
                elif kline.change_pct < 0:
                    fall_count += 1
                    # 跌停判断（跌幅 <= -9.9% 且非ST）
                    if kline.change_pct <= -9.9 and 'ST' not in kline.code:
                        limit_down_count += 1
                else:
                    flat_count += 1

            valid_count += 1

        if valid_count == 0:
            return None

        # 计算加权平均价格
        open_price = (total_open / total_volume) if total_volume > 0 else Decimal('0')
        high_price = (total_high / total_volume) if total_volume > 0 else Decimal('0')
        low_price = (total_low / total_volume) if total_volume > 0 else Decimal('0')
        close_price = (total_close / total_volume) if total_volume > 0 else Decimal('0')

        # 计算板块涨跌幅（使用加权平均收盘价）
        avg_change_pct = 0
        if total_volume > 0:
            # 查询前一天的板块K线数据，获取前一天的收盘价
            prev_sector_kline = session.query(SectorKlineDaily.close).filter(
                SectorKlineDaily.code == sector_code,
                SectorKlineDaily.trade_date < trade_date
            ).order_by(
                SectorKlineDaily.trade_date.desc()
            ).first()
            
            if prev_sector_kline and prev_sector_kline[0] is not None and prev_sector_kline[0] > 0:
                prev_close = prev_sector_kline[0]
                avg_change_pct = float((close_price - prev_close) / prev_close * 100)

        return {
            'code': sector_code,
            'trade_date': trade_date,
            'change_pct': round(avg_change_pct, 2),
            'stock_count': len(stock_codes),
            'rise_count': rise_count,
            'fall_count': fall_count,
            'flat_count': flat_count,
            'limit_up_count': limit_up_count,
            'limit_down_count': limit_down_count,
            'open': round(open_price, 2),
            'high': round(high_price, 2),
            'low': round(low_price, 2),
            'close': round(close_price, 2),
            'total_amount': total_amount,
            'total_volume': total_volume
        }

    except Exception as e:
        logger.error(f"计算板块 {sector_code} 在 {trade_date} 的K线数据失败: {e}")
        return None


def generate_sector_klines_for_date(session, trade_date):
    """
    生成指定交易日的所有板块K线数据

    Args:
        session: 数据库会话
        trade_date: 交易日期

    Returns:
        int: 生成的记录数
    """
    try:
        # 获取所有板块
        sectors = session.query(Sector.code).all()
        sector_codes = [s.code for s in sectors]

        if not sector_codes:
            logger.warning("数据库中没有板块数据")
            return 0

        logger.info(f"开始生成 {trade_date} 的板块K线数据，共 {len(sector_codes)} 个板块")

        success_count = 0
        skip_count = 0

        for sector_code in sector_codes:
            # 计算板块K线数据
            kline_data = calculate_sector_kline(session, sector_code, trade_date)

            if kline_data:
                # 检查是否已存在
                existing = session.query(SectorKlineDaily).filter(
                    and_(
                        SectorKlineDaily.code == sector_code,
                        SectorKlineDaily.trade_date == trade_date
                    )
                ).first()

                if existing:
                    # 更新现有记录
                    for key, value in kline_data.items():
                        if key not in ['code', 'trade_date']:
                            setattr(existing, key, value)
                    logger.debug(f"更新板块 {sector_code} 在 {trade_date} 的K线数据")
                else:
                    # 插入新记录
                    sector_kline = SectorKlineDaily(**kline_data)
                    session.add(sector_kline)
                    logger.debug(f"插入板块 {sector_code} 在 {trade_date} 的K线数据")

                success_count += 1
            else:
                skip_count += 1
                logger.debug(f"板块 {sector_code} 在 {trade_date} 无有效K线数据，跳过")

        session.commit()

        logger.info(f"完成生成 {trade_date} 的板块K线数据：成功 {success_count} 条，跳过 {skip_count} 条")

        return success_count

    except Exception as e:
        session.rollback()
        logger.error(f"生成 {trade_date} 的板块K线数据失败: {e}")
        raise


def generate_sector_klines_for_sector_range(session, sector_code, start_date, end_date):
    """
    生成指定板块在指定日期范围内的日线数据（写入 sector_kline_daily）。

    Args:
        session: 数据库会话
        sector_code: 板块代码
        start_date: 开始日期（date）
        end_date: 结束日期（date）

    Returns:
        dict: 统计信息
    """
    from datetime import datetime as _dt
    from sqlalchemy import and_
    from scheduler.trading_calendar import get_trading_dates_from_db

    if start_date is None or end_date is None:
        raise ValueError("start_date/end_date 不能为空")

    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    trading_dates = get_trading_dates_from_db(start_str, end_str) or []

    total_days = len(trading_dates)
    saved = 0
    skipped = 0

    for d in trading_dates:
        trade_date = _dt.strptime(d, "%Y-%m-%d").date()
        kline_data = calculate_sector_kline(session, sector_code, trade_date)
        if not kline_data:
            skipped += 1
            continue

        existing = session.query(SectorKlineDaily).filter(
            and_(SectorKlineDaily.code == sector_code, SectorKlineDaily.trade_date == trade_date)
        ).first()
        if existing:
            for key, value in kline_data.items():
                if key not in ["code", "trade_date"]:
                    setattr(existing, key, value)
            # 统一刷新时间，便于排查
            existing.created_at = _dt.now()
        else:
            kline_data = dict(kline_data)
            kline_data["created_at"] = _dt.now()
            session.add(SectorKlineDaily(**kline_data))

        saved += 1

        # 每 50 天提交一次，避免事务太大
        if saved % 50 == 0:
            session.commit()

    session.commit()
    return {
        "sector_code": sector_code,
        "start_date": start_str,
        "end_date": end_str,
        "total_trading_days": total_days,
        "saved": saved,
        "skipped": skipped,
    }


def generate_sector_klines_for_range(start_date, end_date):
    """
    生成指定日期范围内所有板块的K线数据

    Args:
        start_date: 开始日期
        end_date: 结束日期

    Returns:
        dict: 统计信息
    """
    session = next(db.get_session())

    try:
        current_date = start_date
        total_count = 0

        while current_date <= end_date:
            # 跳过周末
            if current_date.weekday() < 5:  # 0-4 表示周一到周五
                count = generate_sector_klines_for_date(session, current_date)
                total_count += count

            current_date += timedelta(days=1)

        logger.info(f"完成生成板块K线数据，日期范围：{start_date} 至 {end_date}，共生成 {total_count} 条记录")

        return {
            'start_date': start_date,
            'end_date': end_date,
            'total_count': total_count
        }

    finally:
        session.close()


def generate_recent_sector_klines(days=30):
    """
    生成最近N个交易日的板块K线数据

    Args:
        days: 交易天数

    Returns:
        dict: 统计信息
    """
    session = next(db.get_session())

    try:
        # 从kline_daily表获取最近N个交易日期
        recent_dates = session.query(
            KlineDaily.trade_date
        ).distinct().order_by(
            KlineDaily.trade_date.desc()
        ).limit(days).all()

        if not recent_dates:
            logger.warning("kline_daily表中没有数据，无法生成板块K线")
            return {'total_count': 0}

        date_list = sorted([d[0] for d in recent_dates])
        start_date = date_list[0]
        end_date = date_list[-1]

        logger.info(f"生成最近 {days} 个交易日的板块K线数据：{start_date} 至 {end_date}")

        total_count = 0
        for trade_date in date_list:
            count = generate_sector_klines_for_date(session, trade_date)
            total_count += count

        logger.info(f"完成生成最近 {days} 个交易日的板块K线数据，共生成 {total_count} 条记录")

        return {
            'start_date': start_date,
            'end_date': end_date,
            'days': len(date_list),
            'total_count': total_count
        }

    finally:
        session.close()


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description='生成板块日K线数据')
    parser.add_argument('--mode', choices=['recent', 'range', 'date'], default='recent',
                        help='生成模式：recent（最近N天）、range（日期范围）、date（指定日期）')
    parser.add_argument('--days', type=int, default=30,
                        help='最近N个交易日（仅用于recent模式）')
    parser.add_argument('--start', type=str, help='开始日期，格式：YYYY-MM-DD（仅用于range模式）')
    parser.add_argument('--end', type=str, help='结束日期，格式：YYYY-MM-DD（仅用于range模式）')
    parser.add_argument('--date', type=str, help='指定日期，格式：YYYY-MM-DD（仅用于date模式）')

    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("开始生成板块日K线数据")
    logger.info("=" * 60)

    try:
        if args.mode == 'recent':
            result = generate_recent_sector_klines(args.days)
            logger.info(f"生成完成：{result}")

        elif args.mode == 'range':
            if not args.start or not args.end:
                logger.error("range模式需要指定 --start 和 --end 参数")
                return 1

            start_date = date.fromisoformat(args.start)
            end_date = date.fromisoformat(args.end)
            result = generate_sector_klines_for_range(start_date, end_date)
            logger.info(f"生成完成：{result}")

        elif args.mode == 'date':
            if not args.date:
                logger.error("date模式需要指定 --date 参数")
                return 1

            target_date = date.fromisoformat(args.date)
            session = next(db.get_session())
            try:
                count = generate_sector_klines_for_date(session, target_date)
                logger.info(f"生成完成：{target_date} 生成 {count} 条记录")
            finally:
                session.close()

        logger.info("=" * 60)
        logger.info("板块日K线数据生成成功")
        logger.info("=" * 60)

        return 0

    except Exception as e:
        logger.error(f"生成板块日K线数据失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
