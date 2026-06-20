"""Data storage service.

Provides core storage helpers for candle records and stock metadata.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List

import pandas as pd
from sqlalchemy import delete, insert, text
from sqlalchemy.orm import Session

from models.stock_models import (
    KlineDaily,
    KlineMinute1,
    KlineMinute15,
    KlineMinute30,
    KlineMinute5,
    KlineMinute60,
    KlineMonthly,
    KlineWeekly,
    MoneyFlow,
    Sector,
    SectorStock,
    Stock,
    TechnicalIndicator,
)
from utils.database import get_db
from utils.logger import get_logger

logger = get_logger("DataStorageService")


class DataStorageService:
    """Data storage service."""

    def batch_insert_klines(self, klines: List[Dict], period: str = "daily") -> int:
        """Batch insert kline data.

        Args:
            klines: List of candle records.
            period: Candle period (daily, weekly, monthly, minute).

        Returns:
            Number of inserted rows.
        """
        if not klines:
            return 0

        try:
            with next(get_db()) as session:
                if period == "daily":
                    return self._batch_insert_daily_klines(session, klines)
                if period == "weekly":
                    return self._batch_insert_weekly_klines(session, klines)
                if period == "monthly":
                    return self._batch_insert_monthly_klines(session, klines)
                if period == "minute":
                    return self._batch_insert_minute_klines(session, klines)
                logger.warning(f"Unsupported period: {period}")
                return 0
        except Exception as exc:
            logger.error(f"batch insert kline failed: {exc}")
            return 0

    def _batch_insert_daily_klines(self, session: Session, klines: List[Dict]) -> int:
        """Batch insert daily candles."""
        if not klines:
            return 0

        df = pd.DataFrame(klines)
        if df.empty:
            return 0

        insert_data = []
        for _, row in df.iterrows():
            insert_data.append(
                {
                    "code": row.get("code"),
                    "trade_date": row.get("date"),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("volume"),
                    "amount": row.get("amount"),
                    "amplitude": row.get("amplitude"),
                    "change_pct": row.get("change_pct"),
                    "change_amount": row.get("change_amount"),
                    "turnover_rate": row.get("turnover_rate"),
                }
            )

        if insert_data:
            session.execute(insert(KlineDaily), insert_data)
            session.commit()
            return len(insert_data)
        return 0

    def _batch_insert_minute_klines(self, session: Session, klines: List[Dict]) -> int:
        """Batch insert minute candles."""
        if not klines:
            return 0

        df = pd.DataFrame(klines)
        if df.empty:
            return 0

        period_groups = df.groupby("period")
        total_inserted = 0

        for period, group in period_groups:
            model = self._get_minute_model(period)
            if not model:
                continue

            insert_data = []
            for _, row in group.iterrows():
                insert_data.append(
                    {
                        "code": row.get("code"),
                        "datetime": row.get("datetime"),
                        "open": row.get("open"),
                        "high": row.get("high"),
                        "low": row.get("low"),
                        "close": row.get("close"),
                        "volume": row.get("volume"),
                        "amount": row.get("amount"),
                    }
                )

            if insert_data:
                session.execute(insert(model), insert_data)
                total_inserted += len(insert_data)

        session.commit()
        return total_inserted

    def _get_minute_model(self, period: int):
        """Return SQLAlchemy model by minute period."""
        model_map = {
            1: KlineMinute1,
            5: KlineMinute5,
            15: KlineMinute15,
            30: KlineMinute30,
            60: KlineMinute60,
        }
        return model_map.get(period)

    def _batch_insert_weekly_klines(self, session: Session, klines: List[Dict]) -> int:
        """Batch insert weekly candles."""
        if not klines:
            return 0

        df = pd.DataFrame(klines)
        if df.empty:
            return 0

        insert_data = []
        for _, row in df.iterrows():
            insert_data.append(
                {
                    "code": row.get("code"),
                    "week_start_date": row.get("week_start_date"),
                    "week_end_date": row.get("week_end_date"),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("volume"),
                    "amount": row.get("amount"),
                }
            )

        if insert_data:
            session.execute(insert(KlineWeekly), insert_data)
            session.commit()
            return len(insert_data)
        return 0

    def _batch_insert_monthly_klines(self, session: Session, klines: List[Dict]) -> int:
        """Batch insert monthly candles."""
        if not klines:
            return 0

        df = pd.DataFrame(klines)
        if df.empty:
            return 0

        insert_data = []
        for _, row in df.iterrows():
            insert_data.append(
                {
                    "code": row.get("code"),
                    "month_start_date": row.get("month_start_date"),
                    "month_end_date": row.get("month_end_date"),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("volume"),
                    "amount": row.get("amount"),
                }
            )

        if insert_data:
            session.execute(insert(KlineMonthly), insert_data)
            session.commit()
            return len(insert_data)
        return 0

    def optimize_indexes(self) -> bool:
        """Create or refresh common index definitions."""
        try:
            with next(get_db()) as session:
                self._create_indexes(session)
                return True
        except Exception as exc:
            logger.error(f"optimize indexes failed: {exc}")
            return False

    def _create_indexes(self, session: Session):
        """Create required indexes."""
        session.execute(
            text(
                """
            CREATE INDEX IF NOT EXISTS idx_kline_daily_code_date
            ON kline_daily (code, trade_date);
            """
            )
        )
        session.execute(
            text(
                """
            CREATE INDEX IF NOT EXISTS idx_kline_minute_1_code_datetime
            ON kline_minute_1 (code, datetime);
            """
            )
        )
        session.execute(
            text(
                """
            CREATE INDEX IF NOT EXISTS idx_kline_minute_5_code_datetime
            ON kline_minute_5 (code, datetime);
            """
            )
        )
        session.execute(
            text(
                """
            CREATE INDEX IF NOT EXISTS idx_kline_minute_15_code_datetime
            ON kline_minute_15 (code, datetime);
            """
            )
        )
        session.execute(
            text(
                """
            CREATE INDEX IF NOT EXISTS idx_kline_minute_30_code_datetime
            ON kline_minute_30 (code, datetime);
            """
            )
        )
        session.execute(
            text(
                """
            CREATE INDEX IF NOT EXISTS idx_kline_minute_60_code_datetime
            ON kline_minute_60 (code, datetime);
            """
            )
        )
        session.execute(
            text(
                """
            CREATE INDEX IF NOT EXISTS idx_kline_weekly_code_week
            ON kline_weekly (code, week_start_date);
            """
            )
        )
        session.execute(
            text(
                """
            CREATE INDEX IF NOT EXISTS idx_kline_monthly_code_month
            ON kline_monthly (code, month_start_date);
            """
            )
        )
        session.execute(
            text(
                """
            CREATE INDEX IF NOT EXISTS idx_stocks_code
            ON stocks (code);
            """
            )
        )
        session.commit()

    def cleanup_old_data(self, days: int = 30) -> int:
        """Delete expired minute candles.

        Args:
            days: Retention window in days.

        Returns:
            Number of rows deleted.
        """
        try:
            with next(get_db()) as session:
                cutoff_date = datetime.now() - timedelta(days=days)
                deleted = session.execute(delete(KlineMinute1).where(KlineMinute1.datetime < cutoff_date)).rowcount
                deleted += session.execute(delete(KlineMinute5).where(KlineMinute5.datetime < cutoff_date)).rowcount
                deleted += session.execute(delete(KlineMinute15).where(KlineMinute15.datetime < cutoff_date)).rowcount
                deleted += session.execute(delete(KlineMinute30).where(KlineMinute30.datetime < cutoff_date)).rowcount
                deleted += session.execute(delete(KlineMinute60).where(KlineMinute60.datetime < cutoff_date)).rowcount

                session.commit()
                logger.info(f"cleaned {deleted} expired minute rows.")
                return deleted
        except Exception as exc:
            logger.error(f"cleanup old data failed: {exc}")
            return 0

    def batch_insert_stocks(self, stocks: List[Dict]) -> int:
        """Batch insert stock metadata."""
        if not stocks:
            return 0

        try:
            with next(get_db()) as session:
                insert_data = []
                for stock in stocks:
                    insert_data.append(
                        {
                            "code": stock.get("code"),
                            "name": stock.get("name"),
                            "market": stock.get("market", "SH"),
                            "type": stock.get("type", "stock"),
                            "industry": stock.get("industry"),
                            "region": stock.get("region"),
                            "list_date": stock.get("list_date"),
                        }
                    )

                if insert_data:
                    for data in insert_data:
                        session.execute(
                            text(
                                """
                                INSERT INTO stocks (code, name, market, type, industry, region, list_date)
                                VALUES (:code, :name, :market, :type, :industry, :region, :list_date)
                                ON DUPLICATE KEY UPDATE
                                    name = VALUES(name),
                                    market = VALUES(market),
                                    type = VALUES(type),
                                    industry = VALUES(industry),
                                    region = VALUES(region),
                                    list_date = VALUES(list_date)
                                """
                            ),
                            data,
                        )

                    session.commit()
                    return len(insert_data)
                return 0
        except Exception as exc:
            logger.error(f"batch insert stock info failed: {exc}")
            return 0


data_storage_service = DataStorageService()
