"""
通达信数据导入脚本
将通达信本地数据导入到MySQL数据库
"""
import asyncio
import logging
from datetime import date, timedelta
from app.database import SessionLocal
from app.models.models import Stock, KlineData
from app.services.data_fetcher import LocalDataFetcher

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TDXDataImporter:
    """通达信数据导入器"""

    def __init__(self):
        self.db = SessionLocal()
        self.data_fetcher = LocalDataFetcher(data_path="./data")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.db.close()

    async def import_stock_list(self):
        """导入股票列表"""
        logger.info("=" * 50)
        logger.info("开始导入股票列表")
        logger.info("=" * 50)

        try:
            # 获取通达信股票列表
            stocks = await self.data_fetcher.fetch_all_stocks()

            if not stocks:
                logger.warning("未获取到股票列表")
                return

            imported_count = 0
            updated_count = 0

            for stock_info in stocks:
                code = stock_info.get('code')
                name = stock_info.get('name', f'股票{code}')
                market = stock_info.get('market', 'SH')

                try:
                    # 检查股票是否已存在
                    existing = self.db.query(Stock).filter(Stock.code == code).first()

                    if existing:
                        # 更新现有股票
                        existing.name = name
                        existing.market = market
                        existing.is_active = 1
                        updated_count += 1
                    else:
                        # 创建新股票
                        new_stock = Stock(
                            code=code,
                            name=name,
                            market=market,
                            is_active=1
                        )
                        self.db.add(new_stock)
                        imported_count += 1

                    self.db.commit()

                except Exception as e:
                    logger.error(f"导入股票 {code} 失败：{e}")
                    self.db.rollback()
                    continue

            logger.info(f"✓ 股票列表导入完成")
            logger.info(f"  - 新增股票：{imported_count} 只")
            logger.info(f"  - 更新股票：{updated_count} 只")
            logger.info(f"  - 总计：{imported_count + updated_count} 只")

        except Exception as e:
            logger.error(f"股票列表导入失败：{e}")
            self.db.rollback()

    async def import_kline_data(self, stock_codes=None, limit=None):
        """
        导入K线数据

        Args:
            stock_codes: 要导入的股票代码列表，None表示导入全部
            limit: 限制导入的股票数量，用于测试
        """
        logger.info("=" * 50)
        logger.info("开始导入K线数据")
        logger.info("=" * 50)

        try:
            # 获取要导入的股票列表
            if stock_codes:
                stocks = [{'code': code} for code in stock_codes]
            else:
                stocks = await self.data_fetcher.fetch_all_stocks()

            if limit:
                stocks = stocks[:limit]

            if not stocks:
                logger.warning("未获取到股票列表")
                return

            logger.info(f"准备导入 {len(stocks)} 只股票的K线数据")

            imported_count = 0
            total_records = 0

            for i, stock_info in enumerate(stocks, 1):
                code = stock_info.get('code')
                logger.info(f"[{i}/{len(stocks)}] 导入 {code}...")

                try:
                    # 从通达信读取K线数据，限制为过去1年（确保有足够数据但不过多）
                    end_date = date.today()
                    start_date = end_date - timedelta(days=365)  # 限制为过去1年

                    klines = await self.data_fetcher.fetch_kline(code, period="D", start_date=start_date, end_date=end_date)

                    if not klines:
                        logger.warning(f"  {code} 没有K线数据，跳过")
                        continue

                    # 检查是否至少有10天数据
                    if len(klines) < 10:
                        logger.warning(f"  {code} 数据不足10天（{len(klines)}天），跳过")
                        continue

                    # 获取股票ID
                    stock = self.db.query(Stock).filter(Stock.code == code).first()
                    if not stock:
                        logger.warning(f"  股票 {code} 不存在，跳过")
                        continue

                    # 导入K线数据
                    for kline_data in klines:
                        trade_date = kline_data.get('date')

                        # 检查K线是否已存在
                        existing = self.db.query(KlineData).filter(
                            KlineData.code == code,
                            KlineData.date == trade_date,
                            KlineData.period == "D"
                        ).first()

                        if not existing:
                            new_kline = KlineData(
                                stock_id=stock.id,
                                code=code,
                                date=trade_date,
                                period="D",
                                open_price=kline_data.get('open_price'),
                                high_price=kline_data.get('high_price'),
                                low_price=kline_data.get('low_price'),
                                close_price=kline_data.get('close_price'),
                                volume=kline_data.get('volume'),
                                amount=kline_data.get('amount'),
                                change_percent=kline_data.get('change_percent'),
                            )
                            self.db.add(new_kline)
                            total_records += 1
                        else:
                            # 更新现有K线
                            existing.open_price = kline_data.get('open_price')
                            existing.high_price = kline_data.get('high_price')
                            existing.low_price = kline_data.get('low_price')
                            existing.close_price = kline_data.get('close_price')
                            existing.volume = kline_data.get('volume')
                            existing.amount = kline_data.get('amount')
                            existing.change_percent = kline_data.get('change_percent')

                    self.db.commit()
                    imported_count += 1

                    # 每10只股票提交一次
                    if i % 10 == 0:
                        logger.info(f"  已导入 {i}/{len(stocks)} 只股票")

                except Exception as e:
                    logger.error(f"  导入 {code} 失败：{e}")
                    self.db.rollback()
                    continue

            logger.info(f"✓ K线数据导入完成")
            logger.info(f"  - 导入股票：{imported_count} 只")
            logger.info(f"  - 总记录数：{total_records} 条")

        except Exception as e:
            logger.error(f"K线数据导入失败：{e}")
            self.db.rollback()


async def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description='通达信数据导入工具')
    parser.add_argument('--stocks', action='store_true', help='导入股票列表')
    parser.add_argument('--kline', action='store_true', help='导入K线数据')
    parser.add_argument('--all', action='store_true', help='导入所有数据')
    parser.add_argument('--limit', type=int, default=None, help='限制导入股票数量')
    parser.add_argument('--codes', type=str, default=None, help='指定股票代码，逗号分隔')

    args = parser.parse_args()

    if not any([args.stocks, args.kline, args.all]):
        print("请指定要导入的数据类型：--stocks, --kline, 或 --all")
        return

    with TDXDataImporter() as importer:
        if args.stocks or args.all:
            await importer.import_stock_list()

        if args.kline or args.all:
            stock_codes = None
            if args.codes:
                stock_codes = [code.strip() for code in args.codes.split(',')]

            await importer.import_kline_data(
                stock_codes=stock_codes,
                limit=args.limit
            )


if __name__ == "__main__":
    asyncio.run(main())