"""生成情绪周期数据脚本

功能：
- 从K线数据中计算各种上涨率指标
- 支持：收盘上涨率、盘中上涨率、上证/深证/创业板上涨率
- 支持：昨日妖股/强势股/弱势股上涨率
- 支持指定日期范围和天数

使用方法：
    python scripts/generate_emotion_cycle.py [days] [start_date] [end_date]
    
示例：
    python scripts/generate_emotion_cycle.py 30              # 最近30天
    python scripts/generate_emotion_cycle.py 30 20250301     # 从2025-03-01开始生成30天
"""

import sys
import os

# 添加项目根目录到Python模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, date, timedelta
from decimal import Decimal

from utils.database import db
from utils.logger import get_logger
from models.stock_models import Stock, KlineDaily, EmotionCycle

logger = get_logger("GenerateEmotionCycle")


class EmotionCycleGenerator:
    """情绪周期数据生成器"""

    def __init__(self):
        self.session = None

    def calculate_up_rate(self, stocks: list) -> float:
        """
        计算上涨率

        Args:
            stocks: 股票K线数据列表

        Returns:
            上涨率（百分比）
        """
        if not stocks:
            return 0.0

        up_count = sum(1 for s in stocks if s.change_pct and s.change_pct > 0)
        total = len(stocks)

        return round((up_count / total) * 100, 2) if total > 0 else 0.0

    def calculate_ratio(self, stocks: list, predicate) -> float:
        if not stocks:
            return 0.0
        count = sum(1 for s in stocks if predicate(s))
        return round((count / len(stocks)) * 100, 2)

    def calculate_market_up_rate(self, market: str, trade_date: date) -> float:
        """
        计算指定市场的上涨率

        Args:
            market: 市场（SH/SZ）
            trade_date: 交易日期

        Returns:
            上涨率（百分比）
        """
        if market == 'SH':
            # 上证：6开头的股票
            stocks = self.session.query(KlineDaily.code, KlineDaily.change_pct).filter(
                KlineDaily.trade_date == trade_date,
                KlineDaily.code.like('6%')
            ).all()
        elif market == 'SZ':
            # 深证：0或3开头的股票
            stocks = self.session.query(KlineDaily.code, KlineDaily.change_pct).filter(
                KlineDaily.trade_date == trade_date,
                (KlineDaily.code.like('0%') | KlineDaily.code.like('3%'))
            ).all()
        else:
            stocks = []

        return self.calculate_up_rate(stocks)

    def get_yesterday_stocks_by_category(self, prev_date: date, category: str) -> list:
        """
        获取前一交易日指定分类的股票

        Args:
            prev_date: 前一交易日
            category: 分类（monster/strong/weak）

        Returns:
            股票代码列表
        """
        # 获取前一交易日的所有股票K线数据
        prev_klines = self.session.query(KlineDaily.code, KlineDaily.change_pct).filter(
            KlineDaily.trade_date == prev_date
        ).all()

        if category == 'monster':
            # 妖股：涨幅 > 9.5%
            codes = [k.code for k in prev_klines if k.change_pct and k.change_pct > 9.5]
        elif category == 'strong':
            # 强势股：涨幅 > 5%
            codes = [k.code for k in prev_klines if k.change_pct and k.change_pct > 5]
        elif category == 'weak':
            # 弱势股：跌幅 < -5%
            codes = [k.code for k in prev_klines if k.change_pct and k.change_pct < -5]
        else:
            codes = []

        return codes

    def calculate_category_up_rate(self, trade_date: date, prev_date: date, category: str) -> float:
        """
        计算指定分类股票在当日上涨的比例

        Args:
            trade_date: 交易日期
            prev_date: 前一交易日
            category: 分类（monster/strong/weak）

        Returns:
            上涨率（百分比）
        """
        # 获取前一交易日指定分类的股票代码
        prev_codes = self.get_yesterday_stocks_by_category(prev_date, category)

        if not prev_codes:
            return 0.0

        # 查询这些股票在当日的表现
        today_klines = self.session.query(KlineDaily.code, KlineDaily.change_pct).filter(
            KlineDaily.trade_date == trade_date,
            KlineDaily.code.in_(prev_codes)
        ).all()

        return self.calculate_up_rate(today_klines)

    def generate_emotion_cycle(self, trade_date: date) -> EmotionCycle:
        """
        生成指定日期的情绪周期数据

        Args:
            trade_date: 交易日期

        Returns:
            EmotionCycle对象
        """
        # 获取前一交易日
        prev_kline = self.session.query(KlineDaily.trade_date).filter(
            KlineDaily.trade_date < trade_date
        ).order_by(KlineDaily.trade_date.desc()).first()

        if not prev_kline:
            logger.warning(f"未找到 {trade_date} 的前一交易日数据")
            return None

        prev_date = prev_kline[0]

        # 计算各种上涨率
        # 1. 收盘上涨率：所有股票收盘价相对于前收盘的上涨比例
        all_stocks = self.session.query(KlineDaily.code, KlineDaily.change_pct).filter(
            KlineDaily.trade_date == trade_date
        ).all()

        close_up_rate = self.calculate_up_rate(all_stocks)

        # 2. 盘中上涨率：暂时用收盘上涨率代替
        intraday_up_rate = close_up_rate

        # 3. 上证上涨率
        sh_up_rate = self.calculate_market_up_rate('SH', trade_date)

        # 4. 深证上涨率
        sz_up_rate = self.calculate_market_up_rate('SZ', trade_date)

        # 5. 创业板上涨率（3开头的股票）
        cyb_stocks = self.session.query(KlineDaily.code, KlineDaily.change_pct).filter(
            KlineDaily.trade_date == trade_date,
            KlineDaily.code.like('3%')
        ).all()
        cyb_up_rate = self.calculate_up_rate(cyb_stocks)

        # 6. 昨日妖股上涨率
        yesterday_monster_up_rate = self.calculate_category_up_rate(
            trade_date, prev_date, 'monster'
        )

        # 7. 昨日强势股上涨率
        yesterday_strong_up_rate = self.calculate_category_up_rate(
            trade_date, prev_date, 'strong'
        )

        # 8. 昨日弱势股上涨率
        yesterday_weak_up_rate = self.calculate_category_up_rate(
            trade_date, prev_date, 'weak'
        )

        # 强势上涨率（涨幅 > 3%）
        strong_up_rate = self.calculate_ratio(
            all_stocks,
            lambda s: s.change_pct is not None and s.change_pct > 3,
        )

        # 弱势下跌率（跌幅 < -3%）
        weak_up_rate = self.calculate_ratio(
            all_stocks,
            lambda s: s.change_pct is not None and s.change_pct < -3,
        )

        # 涨停溢价率（暂时设为0，需要更复杂的计算）
        limit_up_follow_rate = 0.0

        # 统计股票总数
        total_stocks = len(all_stocks)

        # 创建情绪周期数据
        emotion_cycle = EmotionCycle(
            id=int(trade_date.strftime("%Y%m%d")),
            date=trade_date,
            close_up_rate=Decimal(str(close_up_rate)),
            intraday_up_rate=Decimal(str(intraday_up_rate)),
            sh_up_rate=Decimal(str(sh_up_rate)),
            sz_up_rate=Decimal(str(sz_up_rate)),
            cyb_up_rate=Decimal(str(cyb_up_rate)),
            strong_up_rate=Decimal(str(strong_up_rate)),
            weak_up_rate=Decimal(str(weak_up_rate)),
            limit_up_follow_rate=Decimal(str(limit_up_follow_rate)),
            yesterday_monster_up_rate=Decimal(str(yesterday_monster_up_rate)),
            yesterday_strong_up_rate=Decimal(str(yesterday_strong_up_rate)),
            yesterday_weak_up_rate=Decimal(str(yesterday_weak_up_rate)),
            total_stocks=total_stocks,
            is_confirmed=0
        )

        logger.info(f"{trade_date}: 收盘上涨率={close_up_rate}%, "
                   f"上证={sh_up_rate}%, 深证={sz_up_rate}%, 创业板={cyb_up_rate}%")

        return emotion_cycle

    def generate_range(self, days: int, start_date: date = None, end_date: date = None):
        """
        生成指定天数范围的情绪周期数据

        Args:
            days: 天数
            start_date: 开始日期（可选）
            end_date: 结束日期（可选）
        """
        self.session = next(db.get_session())

        try:
            # 确定日期范围
            if end_date is None:
                end_date = date.today()
            if start_date is None:
                start_date = end_date - timedelta(days=days - 1)

            logger.info(f"开始生成情绪周期数据：{start_date} 到 {end_date}")

            # 获取日期范围内的所有交易日
            trade_dates = self.session.query(KlineDaily.trade_date).filter(
                KlineDaily.trade_date >= start_date,
                KlineDaily.trade_date <= end_date
            ).distinct().order_by(KlineDaily.trade_date.desc()).all()

            trade_dates = [d[0] for d in trade_dates]

            logger.info(f"共找到 {len(trade_dates)} 个交易日")

            success_count = 0
            skip_count = 0
            error_count = 0

            for trade_date in trade_dates:
                try:
                    # 检查是否已存在
                    existing = self.session.query(EmotionCycle).filter(
                        EmotionCycle.date == trade_date
                    ).first()

                    if existing:
                        logger.info(f"{trade_date}: 已存在，跳过")
                        skip_count += 1
                        continue

                    # 生成情绪周期数据
                    emotion_cycle = self.generate_emotion_cycle(trade_date)

                    if emotion_cycle:
                        self.session.add(emotion_cycle)
                        self.session.commit()
                        success_count += 1
                    else:
                        error_count += 1

                except Exception as e:
                    logger.error(f"{trade_date}: 生成失败 - {e}")
                    error_count += 1
                    self.session.rollback()

            logger.info("=" * 60)
            logger.info(f"情绪周期数据生成完成")
            logger.info(f"成功: {success_count}, 跳过: {skip_count}, 错误: {error_count}")
            logger.info("=" * 60)

        finally:
            if self.session:
                self.session.close()


def main():
    """主函数"""
    # 解析命令行参数
    days = 30
    start_date = None
    end_date = None

    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            logger.error(f"无效的天数参数: {sys.argv[1]}")
            return

    if len(sys.argv) > 2:
        try:
            start_date = datetime.strptime(sys.argv[2], "%Y%m%d").date()
        except ValueError:
            logger.error(f"无效的开始日期格式: {sys.argv[2]}，请使用YYYYMMDD格式")
            return

    if len(sys.argv) > 3:
        try:
            end_date = datetime.strptime(sys.argv[3], "%Y%m%d").date()
        except ValueError:
            logger.error(f"无效的结束日期格式: {sys.argv[3]}，请使用YYYYMMDD格式")
            return

    # 生成情绪周期数据
    generator = EmotionCycleGenerator()
    generator.generate_range(days, start_date, end_date)


if __name__ == "__main__":
    main()
