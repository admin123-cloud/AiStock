"""全量K线同步任务

该任务负责：
- 每周六凌晨1点执行
- 同步所有股票和指数的K线数据（1m, 5m, 15m, 30m, 1h, 1d, 1w, 1mon, 1q, 1y）
- 使用TdxQuant数据源
- 支持内存监控和自动限流
"""

import asyncio
import gc
import psutil
from datetime import datetime, timedelta
from typing import List
from sqlalchemy import or_
import pandas as pd
from utils.logger import get_logger
from data_fetcher.sources.tdxquant import TdxQuantDataSource
from scheduler.config import SCHEDULER_CONFIG

logger = get_logger("FullSyncKlineTask")


def get_memory_usage_gb() -> float:
    """获取当前内存使用量（GB）"""
    return psutil.Process().memory_info().rss / (1024 ** 3)


def check_memory_limit(soft_limit_gb: float = 8.0, hard_limit_gb: float = 12.0) -> str:
    """
    检查内存使用是否超过限制
    
    Returns:
        - 'ok': 内存使用正常
        - 'warning': 超过软限制，建议释放内存
        - 'critical': 超过硬限制，需要暂停任务
    """
    current = get_memory_usage_gb()
    if current >= hard_limit_gb:
        return "critical"
    elif current >= soft_limit_gb:
        return "warning"
    return "ok"


class FullSyncKlineTask:
    """全量K线同步任务"""

    def __init__(self):
        """初始化任务"""
        self.tdxquant_ds = TdxQuantDataSource(name="tdxquant", config={"enabled": True})
        # 进度跟踪属性
        self.total_stocks = 0
        self.processed_stocks = 0
        self.current_stock = ""
        self.current_period = ""
        self.status = "idle"  # idle, running, completed, failed, paused
        self.start_time = None
        self.end_time = None
        # 内存监控配置
        self.minute_kline_config = SCHEDULER_CONFIG.get("sync", {}).get("minute_kline", {})
        self.memory_limit_gb = self.minute_kline_config.get("memory_limit_gb", 8)
        self.hard_memory_limit_gb = self.minute_kline_config.get("hard_memory_limit_gb", 12)
        self.chunk_days = self.minute_kline_config.get("chunk_days", 2)

    async def execute(self):
        """
        执行全量K线同步任务
        """
        logger.info("=" * 60)
        logger.info("开始执行全量K线数据同步任务")
        logger.info("=" * 60)

        # 初始化进度信息
        self.status = "running"
        self.start_time = datetime.now()
        self.processed_stocks = 0
        self.current_stock = ""
        self.current_period = ""

        try:
            # 获取所有股票代码
            from utils.database import db
            from models.stock_models import Stock

            session = next(db.get_session())
            stocks = session.query(Stock.code).filter(
                Stock.type.in_("stock", "index"),
                or_(Stock.status == 'active', Stock.status.is_(None))
            ).all()
            stock_codes = [stock.code for stock in stocks]
            session.close()

            self.total_stocks = len(stock_codes)
            logger.info(f"共{self.total_stocks} 只股票需要同步K线数据")

            # 同步的周期（使用统一的时间单位标准）
            # 标准格式：1m, 5m, 15m, 30m, 60m, 1d, 1w, 1mon, 1q, 1y
            from utils.period_constants import STANDARD_PERIODS
            periods = STANDARD_PERIODS
            
            # 从配置读取参数
            batch_size = self.minute_kline_config.get("batch_size", 50)
            delay_between_batches = self.minute_kline_config.get("delay_between_batches", 2.0)
            delay_between_stocks = self.minute_kline_config.get("delay_between_stocks", 0.5)

            # 分批处理
            total_count = len(stock_codes)
            success_count = 0
            fail_count = 0
            paused_count = 0

            # 定义分钟周期和非分钟周期
            minute_periods = ["1m", "5m", "15m", "30m", "1h"]
            daily_periods = [p for p in periods if p not in minute_periods]

            # 当前日期（用于计算分块时间范围）
            end_date = datetime.now().date()
            # 日线及以上周期需要至少3年历史数据，指数可能需要更长时间维度的技术分析
            start_date = end_date - timedelta(days=365 * 3)  # 默认3年数据

            for i in range(0, total_count, batch_size):
                # 内存检查 - 在每个批次开始前检查
                memory_status = check_memory_limit(self.memory_limit_gb, self.hard_memory_limit_gb)
                if memory_status == "critical":
                    logger.warning(f"内存使用过高({get_memory_usage_gb():.2f}GB > {self.hard_memory_limit_gb}GB)，暂停任务")
                    self.status = "paused"
                    await asyncio.sleep(60)  # 暂停60秒后重试
                    continue
                elif memory_status == "warning":
                    logger.warning(f"内存使用警告({get_memory_usage_gb():.2f}GB > {self.memory_limit_gb}GB)，释放内存")
                    gc.collect()
                    await asyncio.sleep(1)

                batch_codes = stock_codes[i:i + batch_size]
                batch_num = i // batch_size + 1
                total_batches = (total_count + batch_size - 1) // batch_size

                logger.info(f"处理第{batch_num}/{total_batches} 批，共{len(batch_codes)} 只股票")
                logger.info(f"当前内存使用: {get_memory_usage_gb():.2f}GB / 限制: {self.memory_limit_gb}GB")

                for code in batch_codes:
                    self.current_stock = code
                    try:
                        # 先同步日线及以上周期（数据量小）
                        for period in daily_periods:
                            self.current_period = period
                            result = self.tdxquant_ds.get_stock_history(
                                stock_code=code,
                                start_date=start_date.strftime("%Y-%m-%d"),
                                end_date=end_date.strftime("%Y-%m-%d"),
                                period=period
                            )

                            if result is not None and not result.empty:
                                self._save_kline_data(code, period, result)
                                logger.info(f"  {code} {period}: 成功获取 {len(result)} 条数据")
                            else:
                                logger.warning(f"  {code} {period}: 获取数据失败")

                        # 分钟周期使用分块方式同步，避免一次性加载过多数据
                        current_chunk_start = start_date
                        while current_chunk_start <= end_date:
                            chunk_end = min(current_chunk_start + timedelta(days=self.chunk_days), end_date)
                            
                            # 内存检查
                            memory_status = check_memory_limit(self.memory_limit_gb, self.hard_memory_limit_gb)
                            if memory_status == "critical":
                                logger.warning(f"内存使用过高，暂停分钟K线同步")
                                paused_count += 1
                                gc.collect()
                                await asyncio.sleep(30)
                                continue
                            
                            for period in minute_periods:
                                self.current_period = period
                                result = self.tdxquant_ds.get_stock_history(
                                    stock_code=code,
                                    start_date=current_chunk_start.strftime("%Y-%m-%d"),
                                    end_date=chunk_end.strftime("%Y-%m-%d"),
                                    period=period
                                )

                                if result is not None and not result.empty:
                                    self._save_kline_data(code, period, result)
                                    logger.debug(f"  {code} {period} [{current_chunk_start}:{chunk_end}]: {len(result)} 条")
                                else:
                                    logger.debug(f"  {code} {period} [{current_chunk_start}:{chunk_end}]: 无数据")
                            
                            current_chunk_start = chunk_end + timedelta(days=1)
                            # 分块间隔
                            await asyncio.sleep(0.1)

                        success_count += 1
                        self.processed_stocks += 1

                    except Exception as e:
                        logger.error(f"  {code}: 同步失败 - {e}")
                        fail_count += 1
                        self.processed_stocks += 1

                    # 股票间延迟
                    await asyncio.sleep(delay_between_stocks)
                    # 每处理完一只股票后检查内存并释放
                    gc.collect()

                # 批次间延迟
                if i + batch_size < total_count:
                    logger.info(f"等待 {delay_between_batches} 秒后处理下一批..")
                    await asyncio.sleep(delay_between_batches)
                    # 批次结束后强制垃圾回收
                    gc.collect()
                    logger.info(f"批次结束后内存使用: {get_memory_usage_gb():.2f}GB")

            # 汇总结果
            self.end_time = datetime.now()
            duration = (self.end_time - self.start_time).total_seconds()
            self.status = "completed"

            logger.info("=" * 60)
            logger.info("全量K线数据同步任务完成")
            logger.info(f"总耗时: {duration:.2f} 秒")
            logger.info(f"成功: {success_count} 只股票")
            logger.info(f"失败: {fail_count} 只股票")
            logger.info(f"因内存限制暂停: {paused_count} 次")
            logger.info(f"最终内存使用: {get_memory_usage_gb():.2f}GB")
            logger.info("=" * 60)

        except Exception as e:
            logger.error(f"全量K线数据同步任务执行失败 {e}")
            self.status = "failed"
            self.end_time = datetime.now()
            raise

    def get_progress(self):
        """
        获取当前任务进度

        Returns:
            进度信息字典
        """
        progress = {
            "status": self.status,
            "total_stocks": self.total_stocks,
            "processed_stocks": self.processed_stocks,
            "current_stock": self.current_stock,
            "current_period": self.current_period,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "progress_percentage": 0
        }
        
        # 计算进度百分比
        if self.total_stocks > 0:
            progress["progress_percentage"] = round((self.processed_stocks / self.total_stocks) * 100, 2)
        
        return progress

    def _save_kline_data(self, code: str, period: str, klines):
        """
        保存K线数据到数据库

        Args:
            code: 股票代码
            period: 周期
            klines: K线数据DataFrame
        """

        try:
            from utils.database import db

            session = next(db.get_session())

            # 根据周期选择对应的模型
            if period == "1m":
                from models.stock_models import KlineMinute1 as Model
            elif period == "5m":
                from models.stock_models import KlineMinute5 as Model
            elif period == "15m":
                from models.stock_models import KlineMinute15 as Model
            elif period == "30m":
                from models.stock_models import KlineMinute30 as Model
            elif period == "1h":
                from models.stock_models import KlineMinute60 as Model
            elif period == "1d":
                from models.stock_models import KlineDaily as Model
            elif period == "1w":
                from models.stock_models import KlineWeekly as Model
            elif period == "1mon":
                from models.stock_models import KlineMonthly as Model
            elif period == "1q":
                from models.stock_models import KlineQuarterly as Model
            elif period == "1y":
                from models.stock_models import KlineYearly as Model
            else:
                logger.error(f"未知的周期 {period}")
                session.close()
                return

            # 批量获取已存在的数据
            existing_data = {}
            if not klines.empty:
                if period in ["1m", "5m", "15m", "30m", "1h"]:
                    datetimes = klines["datetime"].tolist()
                    existing_records = session.query(Model).filter(
                        Model.code == code,
                        Model.datetime.in_(datetimes)
                    ).all()
                    for record in existing_records:
                        existing_data[record.datetime] = record
                elif period == "1d":
                    # 过滤有效数据并计算dates
                    dates = []
                    for _, row in klines.iterrows():
                        try:
                            if row["datetime"] is not None:
                                date = row["datetime"].date()
                                if date is not None:
                                    dates.append(date)
                        except (AttributeError, ValueError, TypeError):
                            pass
                    
                    if dates:
                        existing_records = session.query(Model).filter(
                            Model.code == code,
                            Model.trade_date.in_(dates)
                        ).all()
                        for record in existing_records:
                            existing_data[record.trade_date] = record
                elif period == "1w":
                    # 过滤有效数据并计算dates
                    dates = []
                    for _, row in klines.iterrows():
                        try:
                            if row["datetime"] is not None:
                                date = row["datetime"].date()
                                if date is not None:
                                    dates.append(date)
                        except (AttributeError, ValueError, TypeError):
                            pass
                    
                    if dates:
                        existing_records = session.query(Model).filter(
                            Model.code == code,
                            Model.week_start_date.in_(dates)
                        ).all()
                        for record in existing_records:
                            existing_data[record.week_start_date] = record
                elif period == "1mon":
                    # 过滤有效数据并计算dates
                    dates = []
                    for _, row in klines.iterrows():
                        try:
                            if row["datetime"] is not None:
                                date = row["datetime"].date()
                                if date is not None:
                                    dates.append(date)
                        except (AttributeError, ValueError, TypeError):
                            pass
                    
                    if dates:
                        existing_records = session.query(Model).filter(
                            Model.code == code,
                            Model.month_start_date.in_(dates)
                        ).all()
                        for record in existing_records:
                            existing_data[record.month_start_date] = record
                elif period == "1q":
                    # 过滤有效数据并计算quarters
                    quarters = []
                    for _, row in klines.iterrows():
                        try:
                            if row["datetime"] is not None:
                                year = row["datetime"].year
                                month = row["datetime"].month
                                if year is not None and month is not None:
                                    quarter_num = (month - 1) // 3 + 1
                                    quarters.append((year, quarter_num))
                        except (AttributeError, ValueError, TypeError):
                            pass
                    
                    if quarters:
                        existing_records = session.query(Model).filter(
                            Model.code == code,
                            Model.year.in_([q[0] for q in quarters]),
                            Model.quarter.in_([q[1] for q in quarters])
                        ).all()
                        for record in existing_records:
                            existing_data[(record.year, record.quarter)] = record
                elif period == "1y":
                    # 过滤有效数据并计算years
                    years = []
                    for _, row in klines.iterrows():
                        try:
                            if row["datetime"] is not None:
                                year = row["datetime"].year
                                if year is not None:
                                    years.append(year)
                        except (AttributeError, ValueError, TypeError):
                            pass
                    
                    if years:
                        existing_records = session.query(Model).filter(
                            Model.code == code,
                            Model.year.in_(years)
                        ).all()
                        for record in existing_records:
                            existing_data[record.year] = record

            # 批量插入或更新数据
            batch_size = 1000
            batch = []
            update_count = 0
            insert_count = 0

            for _, row in klines.iterrows():
                try:
                    # 检查datetime是否有效
                    if row["datetime"] is None:
                        logger.error(f"跳过无效的{period}数据: {code} - datetime为空")
                        continue
                    
                    # 调试：打印datetime的类型和值
                    logger.debug(f"[调试] {code} {period} datetime类型: {type(row['datetime'])}, 值 {row['datetime']}")
                    
                    # 确保datetime是datetime类型，如果是字符串则转换
                    datetime_value = row["datetime"]
                    if isinstance(datetime_value, str):
                        try:
                            datetime_value = pd.to_datetime(datetime_value)
                            logger.debug(f"[调试] {code} {period} 将字符串转换为datetime: {datetime_value}")
                        except Exception as e:
                            logger.error(f"跳过无效的{period}数据: {code} - 无法转换datetime字符串 {str(e)}")
                            continue
                    elif not isinstance(datetime_value, (pd.Timestamp, datetime.datetime)):
                        logger.error(f"跳过无效的{period}数据: {code} - datetime类型错误: {type(datetime_value)}")
                        continue
                    
                    # 检查数据是否已存在
                    key = None
                    if period in ["1m", "5m", "15m", "30m", "1h"]:
                        key = datetime_value
                    elif period == "1d":
                        try:
                            key = datetime_value.date()
                            if key is None or pd.isna(key):
                                logger.error(f"跳过无效的日线数据 {code} - date为空或无效")
                                continue
                        except (AttributeError, ValueError, TypeError) as e:
                            logger.error(f"跳过无效的日线数据 {code} - datetime格式错误: {str(e)}")
                            continue
                    elif period == "1w":
                        try:
                            key = datetime_value.date()
                            if key is None or pd.isna(key):
                                logger.error(f"跳过无效的周线数据 {code} - date为空或无效")
                                continue
                        except (AttributeError, ValueError, TypeError) as e:
                            logger.error(f"跳过无效的周线数据 {code} - datetime格式错误: {str(e)}")
                            continue
                    elif period == "1mon":
                        try:
                            key = datetime_value.date()
                            if key is None or pd.isna(key):
                                logger.error(f"跳过无效的月线数据 {code} - date为空或无效")
                                continue
                        except (AttributeError, ValueError, TypeError) as e:
                            logger.error(f"跳过无效的月线数据 {code} - datetime格式错误: {str(e)}")
                            continue
                    elif period == "1q":
                        try:
                            quarter_year = datetime_value.year
                            quarter_month = datetime_value.month
                            if quarter_year is not None and quarter_month is not None:
                                quarter_num = (quarter_month - 1) // 3 + 1
                                key = (quarter_year, quarter_num)
                            else:
                                logger.error(f"跳过无效的季线数据 {code} - 年份或月份为空")
                                continue
                        except (AttributeError, ValueError, TypeError) as e:
                            logger.error(f"跳过无效的季线数据 {code} - datetime格式错误: {str(e)}")
                            continue
                    elif period == "1y":
                        try:
                            key = datetime_value.year
                            if key is None:
                                logger.error(f"跳过无效的年线数据 {code} - 年份为空")
                                continue
                        except (AttributeError, ValueError, TypeError) as e:
                            logger.error(f"跳过无效的年线数据 {code} - datetime格式错误: {str(e)}")
                            continue

                    if key in existing_data:
                        # 更新数据
                        existing = existing_data[key]
                        existing.open = row["open"]
                        existing.high = row["high"]
                        existing.low = row["low"]
                        existing.close = row["close"]
                        existing.volume = row["volume"]
                        existing.amount = row["amount"]
                        
                        # 对于周线数据，如果之前week_end_date为null且本周已结束，则更新
                        if period == "1w":
                            if existing.week_end_date is None:
                                from datetime import datetime, timedelta
                                today = datetime.now().date()
                                weekly_date = datetime_value.date()
                                days_until_friday = (4 - weekly_date.weekday()) % 7
                                friday_date = weekly_date + timedelta(days=days_until_friday)
                                if today >= friday_date:
                                    existing.week_end_date = friday_date
                                    logger.info(f"更新 {code} 周线数据 week_end_date: {friday_date}")
                        
                        update_count += 1
                    else:
                        # 插入新数据
                        if period in ["1m", "5m", "15m", "30m", "1h"]:
                            new_record = Model(
                                code=code,
                                datetime=datetime_value,
                                open=row["open"],
                                high=row["high"],
                                low=row["low"],
                                close=row["close"],
                                volume=row["volume"],
                                amount=row["amount"]
                            )
                            batch.append(new_record)
                            insert_count += 1
                        elif period == "1d":
                            try:
                                daily_date = datetime_value.date()
                                if daily_date is not None and not pd.isna(daily_date):
                                    new_record = Model(
                                        code=code,
                                        trade_date=daily_date,
                                        open=row["open"],
                                        high=row["high"],
                                        low=row["low"],
                                        close=row["close"],
                                        volume=row["volume"],
                                        amount=row["amount"]
                                    )
                                    batch.append(new_record)
                                    insert_count += 1
                                else:
                                    logger.error(f"跳过无效的日线数据 {code} - date为空或无效")
                            except (AttributeError, ValueError, TypeError) as e:
                                logger.error(f"跳过无效的日线数据 {code} - datetime格式错误: {str(e)}")
                        elif period == "1w":
                            try:
                                weekly_date = datetime_value.date()
                                if weekly_date is not None and not pd.isna(weekly_date):
                                    # 判断本周是否已结束
                                    from datetime import datetime, timedelta
                                    today = datetime.now().date()
                                    # 计算本周五的日期
                                    days_until_friday = (4 - weekly_date.weekday()) % 7
                                    friday_date = weekly_date + timedelta(days=days_until_friday)
                                    # 如果本周五还没到，则week_end_date为None
                                    week_end_date = friday_date if today >= friday_date else None
                                    
                                    new_record = Model(
                                        code=code,
                                        week_start_date=weekly_date,
                                        week_end_date=week_end_date,
                                        open=row["open"],
                                        high=row["high"],
                                        low=row["low"],
                                        close=row["close"],
                                        volume=row["volume"],
                                        amount=row["amount"]
                                    )
                                    batch.append(new_record)
                                    insert_count += 1
                                else:
                                    logger.error(f"跳过无效的周线数据 {code} - date为空或无效")
                            except (AttributeError, ValueError, TypeError) as e:
                                logger.error(f"跳过无效的周线数据 {code} - datetime格式错误: {str(e)}")
                        elif period == "1mon":
                            try:
                                month_date = datetime_value.date()
                                if month_date is not None and not pd.isna(month_date):
                                    new_record = Model(
                                        code=code,
                                        month_start_date=month_date,
                                        month_end_date=month_date,
                                        open=row["open"],
                                        high=row["high"],
                                        low=row["low"],
                                        close=row["close"],
                                        volume=row["volume"],
                                        amount=row["amount"]
                                    )
                                    batch.append(new_record)
                                    insert_count += 1
                                else:
                                    logger.error(f"跳过无效的月线数据 {code} - date为空或无效")
                            except (AttributeError, ValueError, TypeError) as e:
                                logger.error(f"跳过无效的月线数据 {code} - datetime格式错误: {str(e)}")
                        elif period == "1q":
                            try:
                                quarter_year = datetime_value.year
                                quarter_month = datetime_value.month
                                if quarter_year is not None and quarter_month is not None:
                                    quarter_num = (quarter_month - 1) // 3 + 1
                                    new_record = Model(
                                        code=code,
                                        year=quarter_year,
                                        quarter=quarter_num,
                                        open=row["open"],
                                        high=row["high"],
                                        low=row["low"],
                                        close=row["close"],
                                        volume=row["volume"],
                                        amount=row["amount"]
                                    )
                                    batch.append(new_record)
                                    insert_count += 1
                                else:
                                    logger.error(f"跳过无效的季线数据 {code} - 年份或月份为空")
                            except (AttributeError, ValueError, TypeError) as e:
                                logger.error(f"跳过无效的季线数据 {code} - datetime格式错误: {str(e)}")
                        elif period == "1y":
                            try:
                                year = datetime_value.year
                                if year is not None:
                                    new_record = Model(
                                        code=code,
                                        year=year,
                                        open=row["open"],
                                        high=row["high"],
                                        low=row["low"],
                                        close=row["close"],
                                        volume=row["volume"],
                                        amount=row["amount"]
                                    )
                                    batch.append(new_record)
                                    insert_count += 1
                                else:
                                    logger.error(f"跳过无效的年线数据 {code} - 年份为空")
                            except (AttributeError, ValueError, TypeError) as e:
                                logger.error(f"跳过无效的年线数据 {code} - datetime格式错误: {str(e)}")
                except (AttributeError, ValueError, TypeError) as e:
                    logger.error(f"跳过无效的{period}数据: {code} - {str(e)}")
                    continue

                # 批量提交
                if len(batch) >= batch_size:
                    session.add_all(batch)
                    session.flush()
                    batch = []

            # 提交剩余的批处理
            if batch:
                session.add_all(batch)
                session.flush()

            session.commit()
            logger.debug(f"成功保存 {code} {period} 共{len(klines)} 条K线数据(更新: {update_count}, 新增: {insert_count})")
            session.close()
            
            # 释放DataFrame内存
            del klines
            import gc
            gc.collect()

        except Exception as e:
            logger.error(f"保存K线数据失败 {e}")
            session.rollback()
            session.close()
            raise
