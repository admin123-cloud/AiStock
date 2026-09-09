"""Full K-line synchronization task."""



import asyncio
import gc
import psutil
from datetime import datetime, timedelta
from typing import List
from sqlalchemy import or_
import pandas as pd
from utils.logger import get_logger
from data_fetcher.manager import DataSourceManager
from scheduler.config import SCHEDULER_CONFIG
from utils.kline_units import normalize_qmt_daily_units

logger = get_logger("FullSyncKlineTask")


def get_memory_usage_gb() -> float:
    """Return current memory usage in GB."""
    return psutil.Process().memory_info().rss / (1024 ** 3)


def check_memory_limit(soft_limit_gb: float = 8.0, hard_limit_gb: float = 12.0) -> str:
    """Check whether memory usage exceeds configured limits."""





    current = get_memory_usage_gb()
    if current >= hard_limit_gb:
        return "critical"
    elif current >= soft_limit_gb:
        return "warning"
    return "ok"


class FullSyncKlineTask:
    """Full K-line synchronization task."""

    def __init__(self):
        """Initialize task state."""
        self.market_data_source = DataSourceManager()
        self.total_stocks = 0
        self.processed_stocks = 0
        self.current_stock = ""
        self.current_period = ""
        self.status = "idle"  # idle, running, completed, failed, paused
        self.start_time = None
        self.end_time = None
        # Read memory and chunking limits from the minute K-line scheduler configuration.
        self.minute_kline_config = SCHEDULER_CONFIG.get("sync", {}).get("minute_kline", {})
        self.memory_limit_gb = self.minute_kline_config.get("memory_limit_gb", 8)
        self.hard_memory_limit_gb = self.minute_kline_config.get("hard_memory_limit_gb", 12)
        self.chunk_days = self.minute_kline_config.get("chunk_days", 2)

    async def execute(self):
        """Execute the full K-line synchronization task."""
        logger.info("=" * 60)
        logger.info("full sync task message")
        logger.info("full sync task message")
        logger.info("=" * 60)

        self.status = "running"
        self.start_time = datetime.now()
        self.processed_stocks = 0
        self.current_stock = ""
        self.current_period = ""

        try:
            # Load the active stock universe from the application database.
            from utils.database import db
            from models.stock_models import Stock

            session = next(db.get_session())
            stocks = session.query(Stock.code, Stock.type).filter(
                Stock.type.in_("stock", "index"),
                or_(Stock.status == 'active', Stock.status.is_(None))
            ).all()
            stock_codes = [stock.code for stock in stocks]
            stock_types = {
                str(stock.code).upper(): str(stock.type or "stock").lower()
                for stock in stocks
            }
            session.close()

            self.total_stocks = len(stock_codes)
            logger.info("full sync task message")

            # Use the shared period mapping for daily and minute synchronization.
            # Supported periods: 5m, 15m, 30m, 60m, 1d, 1w, 1mon, 1q, 1y.
            from utils.period_constants import STANDARD_PERIODS
            periods = STANDARD_PERIODS
            
            batch_size = self.minute_kline_config.get("batch_size", 50)
            delay_between_batches = self.minute_kline_config.get("delay_between_batches", 2.0)
            delay_between_stocks = self.minute_kline_config.get("delay_between_stocks", 0.5)

            # Configure delays between symbols and batches.
            total_count = len(stock_codes)
            success_count = 0
            fail_count = 0
            paused_count = 0

            # 鐎规矮绠熼崚鍡涙寭閸涖劍婀￠崪宀勬姜閸掑棝鎸撻崨銊︽埂
            minute_periods = ["5m", "15m", "30m", "60m"]
            daily_periods = [p for p in periods if p not in minute_periods]

            # 当前日期（用于计算分块时间范围）
            end_date = datetime.now().date()
            start_date = end_date - timedelta(days=365 * 5)
            for i in range(0, total_count, batch_size):
                # Pause or collect garbage when the configured memory threshold is reached.
                memory_status = check_memory_limit(self.memory_limit_gb, self.hard_memory_limit_gb)
                if memory_status == "critical":
                    logger.warning("full sync task warning")
                    self.status = "paused"
                    await asyncio.sleep(60)  # 鏆傚?0绉掑悗閲嶈瘯
                    continue
                elif memory_status == "warning":
                    logger.warning("full sync task warning")
                    gc.collect()
                    await asyncio.sleep(1)

                batch_codes = stock_codes[i:i + batch_size]
                batch_num = i // batch_size + 1
                total_batches = (total_count + batch_size - 1) // batch_size

                logger.info("full sync task message")
                logger.info(f"褰撳墠鍐呭瓨浣跨? {get_memory_usage_gb():.2f}GB / 闄愬? {self.memory_limit_gb}GB")

                for code in batch_codes:
                    self.current_stock = code
                    try:
                        # Synchronize supported daily periods before minute-period chunks.
                        for period in daily_periods:
                            self.current_period = period
                            result = self.market_data_source.get_stock_history(
                                stock_code=code,
                                start_date=start_date.strftime("%Y-%m-%d"),
                                end_date=end_date.strftime("%Y-%m-%d"),
                                period=period
                            )

                            if result is not None and not result.empty:
                                self._save_kline_data(
                                    code,
                                    period,
                                    result,
                                    instrument_type=stock_types.get(str(code).upper(), "stock"),
                                )
                                logger.info("full sync task message")
                            else:
                                logger.warning("full sync task warning")

                        # Fetch minute K-lines in bounded date chunks to control memory use.
                        current_chunk_start = start_date
                        while current_chunk_start <= end_date:
                            chunk_end = min(current_chunk_start + timedelta(days=self.chunk_days), end_date)
                            
                            memory_status = check_memory_limit(self.memory_limit_gb, self.hard_memory_limit_gb)
                            if memory_status == "critical":
                                logger.warning("full sync task warning")
                                paused_count += 1
                                gc.collect()
                                await asyncio.sleep(30)
                                continue
                            
                            for period in minute_periods:
                                self.current_period = period
                                result = self.market_data_source.get_stock_history(
                                    stock_code=code,
                                    start_date=current_chunk_start.strftime("%Y-%m-%d"),
                                    end_date=chunk_end.strftime("%Y-%m-%d"),
                                    period=period
                                )

                                if result is not None and not result.empty:
                                    self._save_kline_data(
                                        code,
                                        period,
                                        result,
                                        instrument_type=stock_types.get(str(code).upper(), "stock"),
                                    )
                                    "task message"
                                else:
                                    "task message"
                            
                            current_chunk_start = chunk_end + timedelta(days=1)
                            # Move to the next chunk after a successful fetch.
                            await asyncio.sleep(0.1)

                        success_count += 1
                        self.processed_stocks += 1

                    except Exception as e:
                        logger.error(f"  {code}: synchronization failed - {e}")
                        fail_count += 1
                        self.processed_stocks += 1

                    # 股票间延?                    await asyncio.sleep(delay_between_stocks)
                    # Collect garbage after processing each symbol.
                    gc.collect()

                if i + batch_size < total_count:
                    logger.info(f"等?{delay_between_batches} 秒后处理下一?.")
                    await asyncio.sleep(delay_between_batches)
                    # Collect garbage between batches and report the memory footprint.
                    gc.collect()
                    logger.info(f"Memory usage after batch: {get_memory_usage_gb():.2f}GB")

            self.end_time = datetime.now()
            duration = (self.end_time - self.start_time).total_seconds()
            self.status = "completed"

            logger.info("=" * 60)
            logger.info("full sync task message")
            logger.info("full sync task message")
            logger.info("full sync task message")
            logger.info("full sync task message")
            logger.info("full sync task message")
            logger.info(f"Final memory usage: {get_memory_usage_gb():.2f}GB")
            logger.info("=" * 60)

        except Exception as e:
            logger.error(f"Full K-line synchronization failed: {e}")
            self.status = "failed"
            self.end_time = datetime.now()
            raise

    def get_progress(self):
        """Return current task progress."""





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
        
        if self.total_stocks > 0:
            progress["progress_percentage"] = round((self.processed_stocks / self.total_stocks) * 100, 2)
        
        return progress

    def _save_kline_data(self, code: str, period: str, klines, instrument_type: str = "stock"):
        """Save K-line data to database."""







        try:
            from utils.database import db

            if period == "1d":
                klines = normalize_qmt_daily_units(
                    klines,
                    instrument_type=instrument_type,
                )

            session = next(db.get_session())

            if period == "1m":
                from models.stock_models import KlineMinute1 as Model
            elif period == "5m":
                from models.stock_models import KlineMinute5 as Model
            elif period == "15m":
                from models.stock_models import KlineMinute15 as Model
            elif period == "30m":
                from models.stock_models import KlineMinute30 as Model
            elif period in {"1h", "60m"}:
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
                logger.error(f"Unsupported K-line period: {period}")
                session.close()
                return

            # 閹靛綊鍣洪懢宄板絿瀹告彃鐡ㄩ崷銊ф畱閺佺増宓?
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

            # 閹靛綊鍣洪幓鎺戝弳閹存牗娲块弬鐗堟殶閹?            batch_size = 1000
            batch = []
            update_count = 0
            insert_count = 0

            for _, row in klines.iterrows():
                try:
                    # Convert datetime values before persisting records.
                    if row["datetime"] is None:
                        logger.error("full sync task error")
                        continue
                    
                    logger.debug(f"save row {code} {period} datetime type={type(row['datetime'])} value={row['datetime']}")
                    
                    # 确保datetime是datetime类型，如果是符串则转?
                    datetime_value = row["datetime"]
                    if isinstance(datetime_value, str):
                        try:
                            datetime_value = pd.to_datetime(datetime_value)
                            logger.debug(f"converted datetime for {code} {period}: {datetime_value}")
                        except Exception as e:
                            logger.error(f"failed to convert datetime for {code} {period}: {e}")
                            continue
                    elif not isinstance(datetime_value, (pd.Timestamp, datetime.datetime)):
                        logger.error(f"invalid datetime type for {code} {period}: {type(datetime_value)}")
                        continue
                    
                    # Skip rows whose datetime cannot be normalized.
                    key = None
                    if period in ["1m", "5m", "15m", "30m", "1h"]:
                        key = datetime_value
                    elif period == "1d":
                        try:
                            key = datetime_value.date()
                            if key is None or pd.isna(key):
                                logger.error("full sync task error")
                                continue
                        except (AttributeError, ValueError, TypeError) as e:
                            logger.error(f"跳过无效的日线数?{code} - datetime格式错误: {str(e)}")
                            continue
                    elif period == "1w":
                        try:
                            key = datetime_value.date()
                            if key is None or pd.isna(key):
                                logger.error("full sync task error")
                                continue
                        except (AttributeError, ValueError, TypeError) as e:
                            logger.error(f"跳过无效的周线数?{code} - datetime格式错误: {str(e)}")
                            continue
                    elif period == "1mon":
                        try:
                            key = datetime_value.date()
                            if key is None or pd.isna(key):
                                logger.error("full sync task error")
                                continue
                        except (AttributeError, ValueError, TypeError) as e:
                            logger.error(f"跳过无效的朠线数?{code} - datetime格式错误: {str(e)}")
                            continue
                    elif period == "1q":
                        try:
                            quarter_year = datetime_value.year
                            quarter_month = datetime_value.month
                            if quarter_year is not None and quarter_month is not None:
                                quarter_num = (quarter_month - 1) // 3 + 1
                                key = (quarter_year, quarter_num)
                            else:
                                logger.error("full sync task error")
                                continue
                        except (AttributeError, ValueError, TypeError) as e:
                            logger.error(f"跳过无效的季线数?{code} - datetime格式错误: {str(e)}")
                            continue
                    elif period == "1y":
                        try:
                            key = datetime_value.year
                            if key is None:
                                logger.error(f"跳过无效的年线数?{code} - 年份为空")
                                continue
                        except (AttributeError, ValueError, TypeError) as e:
                            logger.error(f"跳过无效的年线数?{code} - datetime格式错误: {str(e)}")
                            continue

                    if key in existing_data:
                        # 閺囧瓨鏌婇弫鐗堝祦
                        existing = existing_data[key]
                        existing.open = row["open"]
                        existing.high = row["high"]
                        existing.low = row["low"]
                        existing.close = row["close"]
                        existing.volume = row["volume"]
                        existing.amount = row["amount"]
                        
                        # Populate week_end_date only once the current week has completed.
                        if period == "1w":
                            if existing.week_end_date is None:
                                from datetime import datetime, timedelta
                                today = datetime.now().date()
                                weekly_date = datetime_value.date()
                                days_until_friday = (4 - weekly_date.weekday()) % 7
                                friday_date = weekly_date + timedelta(days=days_until_friday)
                                if today >= friday_date:
                                    existing.week_end_date = friday_date
                                    logger.info(f"閺囧瓨鏌?{code} 閸涖劎鍤庨弫鐗堝祦 week_end_date: {friday_date}")
                        
                        update_count += 1
                    else:
                        if period in ["1m", "5m", "15m", "30m", "1h", "60m"]:
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
                                    logger.error("full sync task error")
                            except (AttributeError, ValueError, TypeError) as e:
                                logger.error(f"跳过无效的日线数?{code} - datetime格式错误: {str(e)}")
                        elif period == "1w":
                            try:
                                weekly_date = datetime_value.date()
                                if weekly_date is not None and not pd.isna(weekly_date):
                                    # Calculate the Friday that closes the weekly bar.
                                    from datetime import datetime, timedelta
                                    today = datetime.now().date()
                                    # 计算札五的日?
                                    days_until_friday = (4 - weekly_date.weekday()) % 7
                                    friday_date = weekly_date + timedelta(days=days_until_friday)
                                    # Keep week_end_date empty until the week has completed.
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
                                    logger.error("full sync task error")
                            except (AttributeError, ValueError, TypeError) as e:
                                logger.error(f"跳过无效的周线数?{code} - datetime格式错误: {str(e)}")
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
                                    logger.error("full sync task error")
                            except (AttributeError, ValueError, TypeError) as e:
                                logger.error(f"跳过无效的朠线数?{code} - datetime格式错误: {str(e)}")
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
                                    logger.error("full sync task error")
                            except (AttributeError, ValueError, TypeError) as e:
                                logger.error(f"跳过无效的季线数?{code} - datetime格式错误: {str(e)}")
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
                                    logger.error(f"跳过无效的年线数?{code} - 年份为空")
                            except (AttributeError, ValueError, TypeError) as e:
                                logger.error(f"跳过无效的年线数?{code} - datetime格式错误: {str(e)}")
                except (AttributeError, ValueError, TypeError) as e:
                    logger.error(f"failed to save {code} {period}: {e}")
                    continue

                # Commit the batch after all valid records are prepared.
                if len(batch) >= batch_size:
                    session.add_all(batch)
                    session.flush()
                    batch = []

            # Roll back the transaction and report the persistence error.
            if batch:
                session.add_all(batch)
                session.flush()

            session.commit()
            logger.debug(f"saved {code} {period}: rows={len(klines)}, updated={update_count}, inserted={insert_count}")
            session.close()
            
            # 閲婃斁DataFrame鍐呭?
            del klines
            import gc
            gc.collect()

        except Exception as e:
            logger.error(f"Failed to persist synchronized K-line data: {e}")
            session.rollback()
            session.close()
            raise
