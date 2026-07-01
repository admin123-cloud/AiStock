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
        # 閸愬懎鐡ㄩ惄鎴炲付闁板秶鐤?
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
            # 閼惧嘲褰囬幍鈧張澶庡亗缁併劋鍞惍?            from utils.database import db
            from models.stock_models import Stock

            session = next(db.get_session())
            stocks = session.query(Stock.code).filter(
                Stock.type.in_("stock", "index"),
                or_(Stock.status == 'active', Stock.status.is_(None))
            ).all()
            stock_codes = [stock.code for stock in stocks]
            session.close()

            self.total_stocks = len(stock_codes)
            logger.info("full sync task message")

            # 閸氬本顒為惃鍕噯閺堢噦绱欐担璺ㄦ暏缂佺喍绔撮惃鍕闂傛潙宕熸担宥嗙垼閸戝棴绱?
            # 閺嶅洤鍣弽鐓庣础閿?m, 5m, 15m, 30m, 60m, 1d, 1w, 1mon, 1q, 1y
            from utils.period_constants import STANDARD_PERIODS
            periods = STANDARD_PERIODS
            
            # 娴犲酣鍘ょ純顔款嚢閸欐牕寮弫?            batch_size = self.minute_kline_config.get("batch_size", 50)
            delay_between_batches = self.minute_kline_config.get("delay_between_batches", 2.0)
            delay_between_stocks = self.minute_kline_config.get("delay_between_stocks", 0.5)

            # 閸掑棙澹掓径鍕倞
            total_count = len(stock_codes)
            success_count = 0
            fail_count = 0
            paused_count = 0

            # 鐎规矮绠熼崚鍡涙寭閸涖劍婀￠崪宀勬姜閸掑棝鎸撻崨銊︽埂
            minute_periods = ["5m", "15m", "30m", "60m"]
            daily_periods = [p for p in periods if p not in minute_periods]

            # 褰撳墠鏃ユ湡锛堢敤浜庤绠楀垎鍧楁椂闂磋寖鍥达級
            end_date = datetime.now().date()
            start_date = end_date - timedelta(days=365 * 5)
            for i in range(0, total_count, batch_size):
                # 閸愬懎鐡ㄥΛ鈧弻?- 閸︺劍鐦℃稉顏呭濞嗏€崇磻婵澧犲Λ鈧弻?                memory_status = check_memory_limit(self.memory_limit_gb, self.hard_memory_limit_gb)
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
                        # 閸忓牆鎮撳銉︽）缁惧灝寮锋禒銉ょ瑐閸涖劍婀￠敍鍫熸殶閹诡噣鍣虹亸蹇ョ礆
                        for period in daily_periods:
                            self.current_period = period
                            result = self.market_data_source.get_stock_history(
                                stock_code=code,
                                start_date=start_date.strftime("%Y-%m-%d"),
                                end_date=end_date.strftime("%Y-%m-%d"),
                                period=period
                            )

                            if result is not None and not result.empty:
                                self._save_kline_data(code, period, result)
                                logger.info("full sync task message")
                            else:
                                logger.warning("full sync task warning")

                        # 閸掑棝鎸撻崨銊︽埂娴ｈ法鏁ら崚鍡楁健閺傜懓绱￠崥灞绢劄閿涘矂浼╅崗宥勭濞嗏剝鈧冨鏉炲€熺箖婢舵碍鏆熼幑?                        current_chunk_start = start_date
                        while current_chunk_start <= end_date:
                            chunk_end = min(current_chunk_start + timedelta(days=self.chunk_days), end_date)
                            
                            # 閸愬懎鐡ㄥΛ鈧弻?                            memory_status = check_memory_limit(self.memory_limit_gb, self.hard_memory_limit_gb)
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
                                    self._save_kline_data(code, period, result)
                                    "task message"
                                else:
                                    "task message"
                            
                            current_chunk_start = chunk_end + timedelta(days=1)
                            # 閸掑棗娼￠梻鎾
                            await asyncio.sleep(0.1)

                        success_count += 1
                        self.processed_stocks += 1

                    except Exception as e:
                        logger.error(f"  {code}: 閸氬本顒炴径杈Е - {e}")
                        fail_count += 1
                        self.processed_stocks += 1

                    # 鑲＄エ闂村欢?                    await asyncio.sleep(delay_between_stocks)
                    # 濮ｅ繐顦╅悶鍡楃暚娑撯偓閸欘亣鍋傜粊銊ユ倵濡偓閺屻儱鍞寸€涙ê鑻熼柌濠冩杹
                    gc.collect()

                # 鎵规闂村欢?                if i + batch_size < total_count:
                    logger.info(f"绛夊?{delay_between_batches} 绉掑悗澶勭悊涓嬩竴鎵?.")
                    await asyncio.sleep(delay_between_batches)
                    # 閹佃顐肩紒鎾存将閸氬骸宸遍崚璺虹€崷鎯ф礀閺€?                    gc.collect()
                    logger.info(f"閹佃顐肩紒鎾存将閸氬骸鍞寸€涙ü濞囬悽? {get_memory_usage_gb():.2f}GB")

            # 濮瑰洦鈧崵绮ㄩ弸?            self.end_time = datetime.now()
            duration = (self.end_time - self.start_time).total_seconds()
            self.status = "completed"

            logger.info("=" * 60)
            logger.info("full sync task message")
            logger.info("full sync task message")
            logger.info("full sync task message")
            logger.info("full sync task message")
            logger.info("full sync task message")
            logger.info(f"閺堚偓缂佸牆鍞寸€涙ü濞囬悽? {get_memory_usage_gb():.2f}GB")
            logger.info("=" * 60)

        except Exception as e:
            logger.error(f"閸忋劑鍣篕缁炬寧鏆熼幑顔兼倱濮濄儰鎹㈤崝鈩冨⒔鐞涘苯銇戠拹?{e}")
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

    def _save_kline_data(self, code: str, period: str, klines):
        """Save K-line data to database."""







        try:
            from utils.database import db

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
                logger.error(f"閺堫亞鐓￠惃鍕噯閺?{period}")
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
                    # 杩囨护鏈夋晥鏁版嵁骞惰绠梔ates
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
                    # 杩囨护鏈夋晥鏁版嵁骞惰绠梔ates
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
                    # 杩囨护鏈夋晥鏁版嵁骞惰绠梔ates
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
                    # 杩囨护鏈夋晥鏁版嵁骞惰绠梣uarters
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
                    # 杩囨护鏈夋晥鏁版嵁骞惰绠梱ears
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
                    # 濡偓閺岊櫔atetime閺勵垰鎯侀張澶嬫櫏
                    if row["datetime"] is None:
                        logger.error("full sync task error")
                        continue
                    
                    logger.debug(f"save row {code} {period} datetime type={type(row['datetime'])} value={row['datetime']}")
                    
                    # 纭繚datetime鏄痙atetime绫诲瀷锛屽鏋滄槸楃涓插垯杞?
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
                    
                    # 濡偓閺屻儲鏆熼幑顔芥Ц閸氾箑鍑＄€涙ê婀?
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
                            logger.error(f"璺宠繃鏃犳晥鐨勬棩绾挎暟?{code} - datetime鏍煎紡閿欒: {str(e)}")
                            continue
                    elif period == "1w":
                        try:
                            key = datetime_value.date()
                            if key is None or pd.isna(key):
                                logger.error("full sync task error")
                                continue
                        except (AttributeError, ValueError, TypeError) as e:
                            logger.error(f"璺宠繃鏃犳晥鐨勫懆绾挎暟?{code} - datetime鏍煎紡閿欒: {str(e)}")
                            continue
                    elif period == "1mon":
                        try:
                            key = datetime_value.date()
                            if key is None or pd.isna(key):
                                logger.error("full sync task error")
                                continue
                        except (AttributeError, ValueError, TypeError) as e:
                            logger.error(f"璺宠繃鏃犳晥鐨勬湢绾挎暟鎹?{code} - datetime鏍煎紡閿欒: {str(e)}")
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
                            logger.error(f"璺宠繃鏃犳晥鐨勫绾挎暟?{code} - datetime鏍煎紡閿欒: {str(e)}")
                            continue
                    elif period == "1y":
                        try:
                            key = datetime_value.year
                            if key is None:
                                logger.error(f"璺宠繃鏃犳晥鐨勫勾绾挎暟?{code} - 骞翠唤涓虹┖")
                                continue
                        except (AttributeError, ValueError, TypeError) as e:
                            logger.error(f"璺宠繃鏃犳晥鐨勫勾绾挎暟?{code} - datetime鏍煎紡閿欒: {str(e)}")
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
                        
                        # 鐎甸€涚艾閸涖劎鍤庨弫鐗堝祦閿涘苯顩ч弸婊€绠ｉ崜宄竐ek_end_date娑撶皠ull娑撴梹婀伴崨銊ュ嚒缂佹挻娼敍灞藉灟閺囧瓨鏌?
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
                                logger.error(f"璺宠繃鏃犳晥鐨勬棩绾挎暟?{code} - datetime鏍煎紡閿欒: {str(e)}")
                        elif period == "1w":
                            try:
                                weekly_date = datetime_value.date()
                                if weekly_date is not None and not pd.isna(weekly_date):
                                    # 閸掋倖鏌囬張顒€鎳嗛弰顖氭儊瀹歌尙绮ㄩ弶?                                    from datetime import datetime, timedelta
                                    today = datetime.now().date()
                                    # 璁＄畻鏈懆浜旂殑鏃ユ?
                                    days_until_friday = (4 - weekly_date.weekday()) % 7
                                    friday_date = weekly_date + timedelta(days=days_until_friday)
                                    # 濡傛灉鏈懆浜旇繕娌埌锛屽垯week_end_date涓篘one
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
                                logger.error(f"璺宠繃鏃犳晥鐨勫懆绾挎暟?{code} - datetime鏍煎紡閿欒: {str(e)}")
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
                                logger.error(f"璺宠繃鏃犳晥鐨勬湢绾挎暟鎹?{code} - datetime鏍煎紡閿欒: {str(e)}")
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
                                logger.error(f"璺宠繃鏃犳晥鐨勫绾挎暟?{code} - datetime鏍煎紡閿欒: {str(e)}")
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
                                    logger.error(f"璺宠繃鏃犳晥鐨勫勾绾挎暟?{code} - 骞翠唤涓虹┖")
                            except (AttributeError, ValueError, TypeError) as e:
                                logger.error(f"璺宠繃鏃犳晥鐨勫勾绾挎暟?{code} - datetime鏍煎紡閿欒: {str(e)}")
                except (AttributeError, ValueError, TypeError) as e:
                    logger.error(f"failed to save {code} {period}: {e}")
                    continue

                # 閹靛綊鍣洪幓鎰唉
                if len(batch) >= batch_size:
                    session.add_all(batch)
                    session.flush()
                    batch = []

            # 閹绘劒姘﹂崜鈺€缍戦惃鍕婢跺嫮鎮?
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
            logger.error(f"娣囨繂鐡↘缁炬寧鏆熼幑顔笺亼鐠?{e}")
            session.rollback()
            session.close()
            raise
