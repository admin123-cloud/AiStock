"""
定时任务调度器
负责数据同步、指标计算等定时操作
"""
import logging
from datetime import datetime, time
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from app.config import settings
from app.database import SessionLocal
from app.models.models import KlineData, SentimentData, Stock
from app.services.data_fetcher import DataFetcherFactory
from app.services.sentiment_calculator import SentimentService

logger = logging.getLogger(__name__)
sentiment_service = SentimentService()


class TaskScheduler:
    """任务调度器"""
    
    def __init__(self):
        # 使用指定时区
        tz = pytz.timezone(settings.update_timezone)
        self.scheduler = AsyncIOScheduler(timezone=tz)
        self.data_fetcher = DataFetcherFactory.create_fetcher(
            settings.data_source_type,
            data_path=settings.local_data_path
        )
    
    def start(self):
        """启动定时任务"""
        logger.info("启动定时任务调度器...")
        
        # 解析更新时间（格式：HH:MM）
        try:
            hour, minute = map(int, settings.update_schedule_time.split(":"))
        except ValueError:
            logger.error(f"无效的更新时间格式：{settings.update_schedule_time}")
            hour, minute = 16, 0
        
        # 注册任务：每天下午4点更新数据
        self.scheduler.add_job(
            self.update_market_data,
            CronTrigger(hour=hour, minute=minute, second=0),
            id="update_market_data",
            name="每日市场数据更新",
            replace_existing=True,
        )
        
        # 注册任务：实时数据更新（可选，每5分钟）
        self.scheduler.add_job(
            self.update_realtime_data,
            CronTrigger(minute="*/5"),
            id="update_realtime_data",
            name="实时数据更新",
            replace_existing=True,
        )
        
        self.scheduler.start()
        logger.info("定时任务调度器启动完成")
    
    def shutdown(self):
        """关闭调度器"""
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("定时任务调度器已关闭")
    
    async def update_market_data(self):
        """
        每日市场数据更新任务
        1. 获取所有股票最新K线
        2. 计算技术指标
        3. 生成大盘情绪数据
        """
        logger.info("=" * 50)
        logger.info("开始执行每日市场数据更新（下午4点）")
        logger.info("=" * 50)
        
        db = SessionLocal()
        try:
            # 1. 获取所有股票列表
            logger.info("1. 获取股票列表...")
            stocks = await self.data_fetcher.fetch_all_stocks()
            
            if not stocks:
                logger.warning("未获取到股票列表")
                return
            
            logger.info(f"获取到 {len(stocks)} 只股票")
            
            # 2. 更新K线数据
            logger.info("2. 更新K线数据...")
            updated_count = 0
            all_klines = []
            
            for stock in stocks:
                code = stock.get('code')
                if not code:
                    continue
                
                try:
                    # 获取该股票的K线数据
                    klines = await self.data_fetcher.fetch_kline(code, period="D")
                    all_klines.extend(klines)
                    
                    # 保存到数据库
                    for kline_data in klines:
                        existing = db.query(KlineData).filter(
                            KlineData.code == code,
                            KlineData.date == kline_data.get('date'),
                            KlineData.period == "D"
                        ).first()
                        
                        if not existing:
                            new_kline = KlineData(
                                code=code,
                                date=kline_data.get('date'),
                                period="D",
                                open_price=kline_data.get('open_price'),
                                high_price=kline_data.get('high_price'),
                                low_price=kline_data.get('low_price'),
                                close_price=kline_data.get('close_price'),
                                volume=kline_data.get('volume'),
                                amount=kline_data.get('amount'),
                                change_percent=kline_data.get('change_percent'),
                            )
                            db.add(new_kline)
                            updated_count += 1
                    
                    db.commit()
                
                except Exception as e:
                    logger.error(f"更新 {code} K线数据失败：{e}")
                    db.rollback()
                    continue
            
            logger.info(f"♦ K线数据更新完成，新增 {updated_count} 条记录")
            
            # 3. 生成大盘情绪数据
            logger.info("3. 计算大盘情绪数据...")
            if all_klines:
                sentiment_data = sentiment_service.calculate_daily_sentiment(
                    all_klines,
                    datetime.now().date()
                )
                
                # 保存情绪数据
                existing_sentiment = db.query(SentimentData).filter(
                    SentimentData.date == sentiment_data['date']
                ).first()
                
                if not existing_sentiment:
                    new_sentiment = SentimentData(**sentiment_data)
                    db.add(new_sentiment)
                    db.commit()
                    logger.info(f"♦ 情绪数据已保存")
                    logger.info(f"  - 上涨家数：{sentiment_data['up_count']}")
                    logger.info(f"  - 下跌家数：{sentiment_data['down_count']}")
                    logger.info(f"  - 情绪评分：{sentiment_data['sentiment_score']} ({sentiment_data['sentiment_level']})")
                else:
                    logger.info(f"♦ 情绪数据已存在，跳过保存")
            
            logger.info("=" * 50)
            logger.info("每日市场数据更新完成")
            logger.info("=" * 50)
        
        except Exception as e:
            logger.error(f"数据更新任务执行失败：{e}")
            db.rollback()
        
        finally:
            db.close()
    
    async def update_realtime_data(self):
        """
        实时数据更新任务（盘中每5分钟更新一次）
        """
        # 检查当前是否在交易时间（9:30 - 15:00）
        now = datetime.now().time()
        if time(9, 30) <= now <= time(15, 0):
            logger.debug("实时数据更新中...")
            # TODO: 实现实时数据更新逻辑
            pass


# 全局调度器实例
scheduler = TaskScheduler()
