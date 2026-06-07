"""
鏁版嵁瀛樺偍鏈嶅姟

鎻愪緵楂樻晥鐨勬暟鎹瓨鍌ㄥ姛鑳斤紝鍖呮嫭鎵归噺鎻掑叆銆佺储寮曚紭鍖栫瓑
"""

from typing import List, Dict, Any, Optional
import pandas as pd
from sqlalchemy import text, insert, update, delete
from sqlalchemy.orm import Session
from datetime import datetime, timedelta

from utils.database import get_db
from models.stock_models import (
    KlineDaily, KlineWeekly, KlineMonthly,
    KlineMinute1, KlineMinute5, KlineMinute15, KlineMinute30, KlineMinute60,
    Stock, Sector, SectorStock, TechnicalIndicator, MoneyFlow
)
from utils.logger import get_logger

logger = get_logger("DataStorageService")


class DataStorageService:
    """
    鏁版嵁瀛樺偍鏈嶅姟
    """
    
    
    def batch_insert_klines(self, klines: List[Dict], period: str = "daily") -> int:
        """
        鎵归噺鎻掑叆K绾挎暟鎹?        
        Args:
            klines: K绾挎暟鎹垪琛?            period: 鍛ㄦ湡绫诲瀷 (daily, weekly, monthly, minute)
        
        Returns:
            鎻掑叆鐨勮褰曟暟
        """
        if not klines:
            return 0
        
        try:
            with next(get_db()) as session:
                if period == "daily":
                    return self._batch_insert_daily_klines(session, klines)
                elif period == "weekly":
                    return self._batch_insert_weekly_klines(session, klines)
                elif period == "monthly":
                    return self._batch_insert_monthly_klines(session, klines)
                elif period == "minute":
                    return self._batch_insert_minute_klines(session, klines)
                else:
                    logger.warning(f"涓嶆敮鎸佺殑鍛ㄦ湡绫诲瀷: {period}")
                    return 0
        except Exception as e:
            logger.error(f"鎵归噺鎻掑叆K绾挎暟鎹け璐? {e}")
            return 0
    
    def _batch_insert_daily_klines(self, session: Session, klines: List[Dict]) -> int:
        """鎵归噺鎻掑叆鏃ョ嚎鏁版嵁"""
        if not klines:
            return 0
        
        # 杞崲涓篋ataFrame杩涜澶勭悊
        df = pd.DataFrame(klines)
        if df.empty:
            return 0
        
        # 鍑嗗鎻掑叆鏁版嵁
        insert_data = []
        for _, row in df.iterrows():
            insert_data.append({
                'code': row.get('code'),
                'trade_date': row.get('date'),
                'open': row.get('open'),
                'high': row.get('high'),
                'low': row.get('low'),
                'close': row.get('close'),
                'volume': row.get('volume'),
                'amount': row.get('amount'),
                'amplitude': row.get('amplitude'),
                'change_pct': row.get('change_pct'),
                'change_amount': row.get('change_amount'),
                'turnover_rate': row.get('turnover_rate')
            })
        
        # 浣跨敤鎵归噺鎻掑叆
        if insert_data:
            session.execute(insert(KlineDaily), insert_data)
            session.commit()
            return len(insert_data)
        return 0
    
    def _batch_insert_minute_klines(self, session: Session, klines: List[Dict]) -> int:
        """鎵归噺鎻掑叆鍒嗛挓绾挎暟鎹?""
        if not klines:
            return 0
        
        # 杞崲涓篋ataFrame杩涜澶勭悊
        df = pd.DataFrame(klines)
        if df.empty:
            return 0
        
        # 鎸夊懆鏈熷垎缁勬彃鍏?        period_groups = df.groupby('period')
        total_inserted = 0
        
        for period, group in period_groups:
            model = self._get_minute_model(period)
            if not model:
                continue
            
            # 鍑嗗鎻掑叆鏁版嵁
            insert_data = []
            for _, row in group.iterrows():
                insert_data.append({
                    'code': row.get('code'),
                    'datetime': row.get('datetime'),
                    'open': row.get('open'),
                    'high': row.get('high'),
                    'low': row.get('low'),
                    'close': row.get('close'),
                    'volume': row.get('volume'),
                    'amount': row.get('amount')
                })
            
            # 浣跨敤鎵归噺鎻掑叆
            if insert_data:
                session.execute(insert(model), insert_data)
                total_inserted += len(insert_data)
        
        session.commit()
        return total_inserted
    
    def _get_minute_model(self, period: int):
        """鏍规嵁鍛ㄦ湡鑾峰彇瀵瑰簲鐨勫垎閽熸暟鎹ā鍨?""
        model_map = {
            1: KlineMinute1,
            5: KlineMinute5,
            15: KlineMinute15,
            30: KlineMinute30,
            60: KlineMinute60
        }
        return model_map.get(period)
    
    def _batch_insert_weekly_klines(self, session: Session, klines: List[Dict]) -> int:
        """鎵归噺鎻掑叆鍛ㄧ嚎鏁版嵁"""
        if not klines:
            return 0
        
        # 杞崲涓篋ataFrame杩涜澶勭悊
        df = pd.DataFrame(klines)
        if df.empty:
            return 0
        
        # 鍑嗗鎻掑叆鏁版嵁
        insert_data = []
        for _, row in df.iterrows():
            insert_data.append({
                'code': row.get('code'),
                'week_start_date': row.get('week_start_date'),
                'week_end_date': row.get('week_end_date'),
                'open': row.get('open'),
                'high': row.get('high'),
                'low': row.get('low'),
                'close': row.get('close'),
                'volume': row.get('volume'),
                'amount': row.get('amount')
            })
        
        # 浣跨敤鎵归噺鎻掑叆
        if insert_data:
            session.execute(insert(KlineWeekly), insert_data)
            session.commit()
            return len(insert_data)
        return 0
    
    def _batch_insert_monthly_klines(self, session: Session, klines: List[Dict]) -> int:
        """鎵归噺鎻掑叆鏈堢嚎鏁版嵁"""
        if not klines:
            return 0
        
        # 杞崲涓篋ataFrame杩涜澶勭悊
        df = pd.DataFrame(klines)
        if df.empty:
            return 0
        
        # 鍑嗗鎻掑叆鏁版嵁
        insert_data = []
        for _, row in df.iterrows():
            insert_data.append({
                'code': row.get('code'),
                'month_start_date': row.get('month_start_date'),
                'month_end_date': row.get('month_end_date'),
                'open': row.get('open'),
                'high': row.get('high'),
                'low': row.get('low'),
                'close': row.get('close'),
                'volume': row.get('volume'),
                'amount': row.get('amount')
            })
        
        # 浣跨敤鎵归噺鎻掑叆
        if insert_data:
            session.execute(insert(KlineMonthly), insert_data)
            session.commit()
            return len(insert_data)
        return 0
    
    def optimize_indexes(self) -> bool:
        """
        浼樺寲鏁版嵁搴撶储寮?       
        Returns:
            鏄惁浼樺寲鎴愬姛
        """
        try:
            with next(get_db()) as session:
                # 涓篕绾胯〃娣诲姞绱㈠紩
                self._create_indexes(session)
                return True
        except Exception as e:
            logger.error(f"浼樺寲绱㈠紩澶辫触: {e}")
            return False
    
    def _create_indexes(self, session: Session):
        """鍒涘缓蹇呰鐨勭储寮?""
        # 鏃ョ嚎琛ㄧ储寮?        session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_kline_daily_code_date 
            ON kline_daily (code, trade_date);
        """))
        
        # 鍒嗛挓绾胯〃绱㈠紩
        session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_kline_minute_1_code_datetime 
            ON kline_minute_1 (code, datetime);
        """))
        
        session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_kline_minute_5_code_datetime 
            ON kline_minute_5 (code, datetime);
        """))
        
        session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_kline_minute_15_code_datetime 
            ON kline_minute_15 (code, datetime);
        """))
        
        session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_kline_minute_30_code_datetime 
            ON kline_minute_30 (code, datetime);
        """))
        
        session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_kline_minute_60_code_datetime 
            ON kline_minute_60 (code, datetime);
        """))
        
        # 鍛ㄧ嚎琛ㄧ储寮?        session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_kline_weekly_code_week 
            ON kline_weekly (code, week_start_date);
        """))
        
        # 鏈堢嚎琛ㄧ储寮?        session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_kline_monthly_code_month 
            ON kline_monthly (code, month_start_date);
        """))
        
        # 鑲＄エ琛ㄧ储寮?        session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_stocks_code 
            ON stocks (code);
        """))
        
        session.commit()
    
    def cleanup_old_data(self, days: int = 30) -> int:
        """
        娓呯悊杩囨湡鏁版嵁
        
        Args:
            days: 淇濈暀澶╂暟
        
        Returns:
            鍒犻櫎鐨勮褰曟暟
        """
        try:
            with next(get_db()) as session:
                cutoff_date = datetime.now() - timedelta(days=days)
                
                # 娓呯悊鍒嗛挓绾挎暟鎹?                deleted = session.execute(
                    delete(KlineMinute1).where(KlineMinute1.datetime < cutoff_date)
                ).rowcount
                
                deleted += session.execute(
                    delete(KlineMinute5).where(KlineMinute5.datetime < cutoff_date)
                ).rowcount
                
                deleted += session.execute(
                    delete(KlineMinute15).where(KlineMinute15.datetime < cutoff_date)
                ).rowcount
                
                deleted += session.execute(
                    delete(KlineMinute30).where(KlineMinute30.datetime < cutoff_date)
                ).rowcount
                
                deleted += session.execute(
                    delete(KlineMinute60).where(KlineMinute60.datetime < cutoff_date)
                ).rowcount
                
                session.commit()
                logger.info(f"娓呯悊浜?{deleted} 鏉¤繃鏈熺殑鍒嗛挓绾挎暟鎹?)
                return deleted
        except Exception as e:
            logger.error(f"娓呯悊杩囨湡鏁版嵁澶辫触: {e}")
            return 0
    
    def batch_insert_stocks(self, stocks: List[Dict]) -> int:
        """
        鎵归噺鎻掑叆鑲＄エ鍩烘湰淇℃伅
        
        Args:
            stocks: 鑲＄エ淇℃伅鍒楄〃
        
        Returns:
            鎻掑叆鐨勮褰曟暟
        """
        if not stocks:
            return 0
        
        try:
            with next(get_db()) as session:
                insert_data = []
                for stock in stocks:
                    insert_data.append({
                        'code': stock.get('code'),
                        'name': stock.get('name'),
                        'market': stock.get('market', 'SH'),
                        'type': stock.get('type', 'stock'),
                        'industry': stock.get('industry'),
                        'region': stock.get('region'),
                        'list_date': stock.get('list_date')
                    })
                
                if insert_data:
                    # 浣跨敤insert ... on duplicate key update
                    for data in insert_data:
                        session.execute(
                            text("""
                                INSERT INTO stocks (code, name, market, type, industry, region, list_date)
                                VALUES (:code, :name, :market, :type, :industry, :region, :list_date)
                                ON DUPLICATE KEY UPDATE
                                name = VALUES(name),
                                market = VALUES(market),
                                type = VALUES(type),
                                industry = VALUES(industry),
                                region = VALUES(region),
                                list_date = VALUES(list_date)
                            """),
                            data
                        )
                    
                    session.commit()
                    return len(insert_data)
                return 0
        except Exception as e:
            logger.error(f"鎵归噺鎻掑叆鑲＄エ淇℃伅澶辫触: {e}")
            return 0


# 鍏ㄥ眬鏁版嵁瀛樺偍鏈嶅姟瀹炰緥
data_storage_service = DataStorageService()

