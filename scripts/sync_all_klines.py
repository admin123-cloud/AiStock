"""
Full K-line data synchronization.

Synchronizes stock and index history for supported daily and minute periods.
"""

import sys
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import math
import threading
from contextlib import nullcontext

# Ensure local project modules are importable when this script runs directly.
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from data_fetcher.manager import DataSourceManager
from utils.config import ConfigManager
from utils.logger import get_logger
from utils.kline_units import (
    QMT_DAILY_SOURCE_NAMES,
    normalize_akshare_daily_units,
    normalize_qmt_daily_units,
    normalize_tushare_daily_units,
)
from utils.database import db
from utils.market_warehouse import clickhouse_available, clickhouse_table_exists, clickhouse_query_df

logger = get_logger("SyncAllKlines")

DAILY_COMPLETENESS_THRESHOLD = 0.95
EARLY_MORNING_CUTOFF_HOUR = 6


class KlineSyncer:
    """Synchronize K-line data."""
    
    def __init__(self):
        """Initialize syncer."""
        self.config = {'enabled': True, 'priority': 0}
        self.market_data_source = DataSourceManager()
        self.config_manager = ConfigManager()

        sync_settings = self.config_manager.get("data_sync", {}, config_file="settings.yaml") or {}
        self.preferred_source = str(sync_settings.get("preferred_source", "qmt_xtquant")).strip().lower()
        self.external_fallback_enabled = bool(sync_settings.get("external_fallback_enabled", True))
        self.external_fallback_periods = set(sync_settings.get("external_fallback_periods", ["1d"]) or ["1d"])
        self.external_fallback_providers = [
            str(item).strip().lower()
            for item in (sync_settings.get("external_fallback_providers", ["akshare", "tushare"]) or [])
            if str(item).strip()
        ]
        self.tushare_token = (
            os.getenv("TUSHARE_TOKEN")
            or str(sync_settings.get("tushare_token", "")).strip()
            or str((self.config_manager.get_data_sources_config().get("tushare", {}) or {}).get("token", "")).strip()
        )
        self._tushare_pro = None
        self._minute_write_lock = threading.Lock()
        self._security_type_cache: Dict[str, str] = {}
        self._canonical_code_cache: Dict[str, str] = {}
        
        # Use one canonical display-name mapping for all supported periods.
        from utils.period_constants import PERIOD_DISPLAY_NAMES
        self.periods = PERIOD_DISPLAY_NAMES
        
        # 缂佺喕顓告穱鈩冧紖
        self.stats = {
            'total': 0,
            'success': 0,
            'failed': 0,
            'skipped': 0,
            'external_fallback_success': 0,
            'external_fallback_failed': 0,
        }

    def _normalize_sync_type(self, sync_type: Optional[str]) -> Optional[str]:
        if sync_type in (None, "", "all"):
            return None
        return str(sync_type)

    def _get_type_filter_values(self, sync_type: Optional[str]) -> List[str]:
        normalized = self._normalize_sync_type(sync_type)
        if normalized == "stock":
            return ["stock"]
        if normalized == "index":
            return ["index"]
        return ["stock", "index"]

    @staticmethod
    def _strip_market_suffix(code: str) -> str:
        value = str(code or "").strip().upper()
        return value.split(".", 1)[0] if "." in value else value

    @staticmethod
    def _to_ts_code(code: str) -> str:
        value = str(code or "").strip().upper()
        if "." in value:
            return value
        if value.startswith(("60", "68", "90")):
            return f"{value}.SH"
        if value.startswith(("00", "30", "20")):
            return f"{value}.SZ"
        if value.startswith(("43", "83", "87", "92")):
            return f"{value}.BJ"
        return f"{value}.SH"

    @staticmethod
    def _normalize_external_daily_frame(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        if df is None or df.empty:
            return None

        rename_map = {
            "\u65e5\u671f": "date",
            "\u4ea4\u6613\u65e5\u671f": "date",
            "\u80a1\u7968\u4ee3\u7801": "code",
            "\u4ee3\u7801": "code",
            "\u5f00\u76d8": "open",
            "\u6700\u9ad8": "high",
            "\u6700\u4f4e": "low",
            "\u6536\u76d8": "close",
            "\u6210\u4ea4\u91cf": "volume",
            "\u6210\u4ea4\u989d": "amount",
            "trade_date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "amount": "amount",
            "open_price": "open",
            "high_price": "high",
            "low_price": "low",
            "close_price": "close",
            "turnover_volume": "volume",
            "turnover_amount": "amount",
            "vol": "volume",
        }
        work_df = df.rename(columns=rename_map).copy()
        required = ["date", "open", "high", "low", "close"]
        if not set(required).issubset(set(work_df.columns)):
            return None
        if "volume" not in work_df.columns:
            work_df["volume"] = 0
        if "amount" not in work_df.columns:
            work_df["amount"] = 0

        work_df = work_df[["date", "open", "high", "low", "close", "volume", "amount"]]
        work_df["date"] = pd.to_datetime(work_df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            work_df[col] = pd.to_numeric(work_df[col], errors="coerce")
        work_df = work_df.dropna(subset=["date", "open", "high", "low", "close"])
        if work_df.empty:
            return None
        return work_df.sort_values("date").reset_index(drop=True)

    def _get_tushare_pro(self):
        if self._tushare_pro is not None:
            return self._tushare_pro
        if not self.tushare_token:
            return None
        try:
            import tushare as ts
            ts.set_token(self.tushare_token)
            self._tushare_pro = ts.pro_api()
            return self._tushare_pro
        except Exception as e:
            logger.warning(f"init tushare pro failed: {e}")
            return None

    def _fetch_external_daily_from_akshare(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        try:
            import akshare as ak

            raw_code = self._strip_market_suffix(code)
            if not raw_code:
                return None
            df = ak.stock_zh_a_hist(
                symbol=raw_code,
                period="daily",
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
                adjust="qfq",
            )
            normalized = self._normalize_external_daily_frame(df)
            return normalize_akshare_daily_units(normalized) if normalized is not None else None
        except Exception as e:
            logger.warning(f"external akshare daily fetch failed for {code}: {e}")
            return None

    def _fetch_external_daily_from_tushare(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        pro = self._get_tushare_pro()
        if pro is None:
            return None
        try:
            ts_code = self._to_ts_code(code)
            df = pro.daily(
                ts_code=ts_code,
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
            )
            if df is None or df.empty:
                return None
            normalized = self._normalize_external_daily_frame(df)
            return normalize_tushare_daily_units(normalized) if normalized is not None else None
        except Exception as e:
            logger.warning(f"external tushare daily fetch failed for {code}: {e}")
            return None

    def _fetch_external_daily(self, code: str, start_date: str, end_date: str):
        if not self.external_fallback_enabled:
            return None, None
        if "1d" not in self.external_fallback_periods:
            return None, None

        for provider in self.external_fallback_providers:
            if provider == "akshare":
                df = self._fetch_external_daily_from_akshare(code=code, start_date=start_date, end_date=end_date)
            elif provider == "tushare":
                df = self._fetch_external_daily_from_tushare(code=code, start_date=start_date, end_date=end_date)
            else:
                continue

            if df is not None and not df.empty:
                return df, provider
        return None, None

    def _get_trade_date_candidates(self, trade_date: str, sync_type: Optional[str]) -> Dict[str, int]:
        """Return current and previous trade-date counts for the given scope."""
        from sqlalchemy import text

        type_values = self._get_type_filter_values(sync_type)
        engine = db.engine
        with engine.connect() as conn:
            previous_trade_date = conn.execute(
                text(
                    """
                    SELECT MAX(trade_date)
                    FROM trade_calendar
                    WHERE is_trading = 1 AND trade_date < :trade_date
                    """
                ),
                {"trade_date": trade_date},
            ).scalar()

            target_total = conn.execute(
                text(
                    f"""
                    SELECT COUNT(*)
                    FROM stocks
                    WHERE type IN ({",".join([f":type_{i}" for i in range(len(type_values))])})
                    """
                ),
                {f"type_{i}": value for i, value in enumerate(type_values)},
            ).scalar() or 0

            actual_count = conn.execute(
                text(
                    f"""
                    SELECT COUNT(DISTINCT k.code)
                    FROM kline_daily k
                    JOIN stocks s ON s.code = k.code
                    WHERE k.trade_date = :trade_date
                      AND s.type IN ({",".join([f":type_{i}" for i in range(len(type_values))])})
                    """
                ),
                {"trade_date": trade_date, **{f"type_{i}": value for i, value in enumerate(type_values)}},
            ).scalar() or 0

            previous_count = 0
            if previous_trade_date:
                previous_count = conn.execute(
                    text(
                        f"""
                        SELECT COUNT(DISTINCT k.code)
                        FROM kline_daily k
                        JOIN stocks s ON s.code = k.code
                        WHERE k.trade_date = :trade_date
                          AND s.type IN ({",".join([f":type_{i}" for i in range(len(type_values))])})
                        """
                    ),
                    {"trade_date": previous_trade_date, **{f"type_{i}": value for i, value in enumerate(type_values)}},
                ).scalar() or 0

        baseline_count = previous_count or target_total
        minimum_required = math.ceil(baseline_count * DAILY_COMPLETENESS_THRESHOLD) if baseline_count else 0
        ratio = (actual_count / baseline_count) if baseline_count else 1.0
        return {
            "trade_date": trade_date,
            "previous_trade_date": str(previous_trade_date) if previous_trade_date else None,
            "actual_count": int(actual_count),
            "baseline_count": int(baseline_count),
            "minimum_required": int(minimum_required),
            "target_total": int(target_total),
            "ratio": ratio,
            "is_incomplete": bool(baseline_count and actual_count < minimum_required),
        }

    def _get_latest_closed_trade_date(self, reference_dt: Optional[datetime] = None) -> str:
        """Resolve the trade date that should be used for a daily sync job."""
        from sqlalchemy import text

        reference_dt = reference_dt or datetime.now()
        today = reference_dt.strftime("%Y-%m-%d")
        engine = db.engine

        if reference_dt.hour < EARLY_MORNING_CUTOFF_HOUR:
            sql = """
                SELECT MAX(trade_date)
                FROM trade_calendar
                WHERE is_trading = 1 AND trade_date < :today
            """
        else:
            sql = """
                SELECT MAX(trade_date)
                FROM trade_calendar
                WHERE is_trading = 1 AND trade_date <= :today
            """

        with engine.connect() as conn:
            trade_date = conn.execute(text(sql), {"today": today}).scalar()

        if not trade_date:
            fallback = (reference_dt - timedelta(days=1)).strftime("%Y-%m-%d")
            logger.warning(f"No completed trade date found; falling back to {fallback}")
            return fallback
        return str(trade_date)

    def _get_scope_stocks(self, sync_type: Optional[str], codes: Optional[List[str]] = None) -> List[dict]:
        type_values = self._get_type_filter_values(sync_type)
        if clickhouse_available() and clickhouse_table_exists("stocks"):
            params: List[Any] = []
            type_sql = ",".join(["?"] * len(type_values))
            where = [f"type IN ({type_sql})"]
            params.extend(type_values)
            if codes:
                code_list = [str(c) for c in codes if c]
                if code_list:
                    code_sql = ",".join(["?"] * len(code_list))
                    where.append(f"code IN ({code_sql})")
                    params.extend(code_list)
            df = clickhouse_query_df(
                f"""
                SELECT code, name, type, market
                FROM stocks
                WHERE {' AND '.join(where)}
                ORDER BY code
                """,
                params,
            )
            if df is None or df.empty:
                return []
            return [
                {
                    "code": str(r.code),
                    "name": str(r.name or r.code),
                    "type": str(r.type or ""),
                    "market": str(r.market or ""),
                }
                for r in df.itertuples(index=False)
            ]

        from models.stock_models import Stock
        session = next(db.get_session())
        try:
            query = session.query(Stock).filter(Stock.type.in_(type_values))
            if codes:
                query = query.filter(Stock.code.in_(codes))
            stocks = query.all()
            return [
                {
                    "code": stock.code,
                    "name": stock.name,
                    "type": stock.type,
                    "market": stock.market,
                }
                for stock in stocks
            ]
        finally:
            session.close()

    def _get_missing_stocks_for_trade_date(self, trade_date: str, sync_type: Optional[str]) -> List[dict]:
        from sqlalchemy import text

        type_values = self._get_type_filter_values(sync_type)
        engine = db.engine
        params = {"trade_date": trade_date, **{f"type_{i}": value for i, value in enumerate(type_values)}}

        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT s.code, s.name, s.type, s.market
                    FROM stocks s
                    LEFT JOIN kline_daily k
                      ON k.code = s.code
                     AND k.trade_date = :trade_date
                    WHERE s.type IN ({",".join([f":type_{i}" for i in range(len(type_values))])})
                      AND k.code IS NULL
                    ORDER BY s.type, s.code
                    """
                ),
                params,
            ).fetchall()

        return [
            {"code": row[0], "name": row[1], "type": row[2], "market": row[3]}
            for row in rows
        ]

    def repair_trade_date(
        self,
        trade_date: str,
        sync_type: Optional[str] = None,
        max_workers: int = 2,
        threshold: float = DAILY_COMPLETENESS_THRESHOLD,
        stocks: Optional[List[dict]] = None,
    ) -> Dict[str, object]:
        """Repair one daily trade date by backfilling missing stock/index rows."""
        sync_type = self._normalize_sync_type(sync_type)
        before = self._get_trade_date_candidates(trade_date, sync_type)
        target_stocks = stocks if stocks is not None else self._get_missing_stocks_for_trade_date(trade_date, sync_type)

        logger.info(
            f"Daily coverage repair: trade_date={trade_date}, scope={sync_type or 'all'}, ",
            f"before={before['actual_count']}/{before['baseline_count']}, missing_targets={len(target_stocks)}"
        )

        processed = 0
        success = 0
        failed = 0
        failure_details = []

        if target_stocks:
            with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
                future_to_stock = {
                    executor.submit(
                        self.sync_kline_for_stock,
                        stock,
                        "1d",
                        start_date=trade_date,
                        end_date=trade_date,
                    ): stock
                    for stock in target_stocks
                }
                for future in as_completed(future_to_stock):
                    stock = future_to_stock[future]
                    processed += 1
                    try:
                        ok = bool(future.result())
                    except Exception as e:
                        ok = False
                        failure_details.append({"code": stock["code"], "error": str(e)})
                    if ok:
                        success += 1
                    else:
                        failed += 1
                        if not any(item["code"] == stock["code"] for item in failure_details):
                            failure_details.append({"code": stock["code"], "error": "sync failed"})

        after = self._get_trade_date_candidates(trade_date, sync_type)
        after["minimum_required"] = math.ceil(after["baseline_count"] * threshold) if after["baseline_count"] else 0
        after["is_incomplete"] = bool(after["baseline_count"] and after["actual_count"] < after["minimum_required"])

        remaining_stocks = self._get_missing_stocks_for_trade_date(trade_date, sync_type)
        result = {
            "trade_date": trade_date,
            "scope": sync_type or "all",
            "threshold": threshold,
            "before": before,
            "after": after,
            "processed": processed,
            "success": success,
            "failed": failed,
            "remaining_count": len(remaining_stocks),
            "remaining_codes": [stock["code"] for stock in remaining_stocks[:200]],
            "failure_details": failure_details[:200],
            "status": "completed" if not after["is_incomplete"] else "failed",
        }
        logger.info(
            f"琛ラ綈屾?trade_date={trade_date}, after={after['actual_count']}/{after['baseline_count']}, "
            f"status={result['status']}, remaining={result['remaining_count']}"
        )
        return result

    def ensure_trade_date_complete(
        self,
        trade_date: Optional[str] = None,
        sync_type: Optional[str] = None,
        max_workers: int = 2,
        threshold: float = DAILY_COMPLETENESS_THRESHOLD,
    ) -> Dict[str, object]:
        target_trade_date = trade_date or self._get_latest_closed_trade_date()
        before = self._get_trade_date_candidates(target_trade_date, sync_type)
        if not before["is_incomplete"]:
            return {
                "trade_date": target_trade_date,
                "scope": self._normalize_sync_type(sync_type) or "all",
                "threshold": threshold,
                "before": before,
                "after": before,
                "processed": 0,
                "success": 0,
                "failed": 0,
                "remaining_count": 0,
                "remaining_codes": [],
                "failure_details": [],
                "status": "completed",
            }

        logger.warning(
            f"Daily coverage check: trade_date={target_trade_date}, ",
            f"actual={before['actual_count']}, baseline={before['baseline_count']}, "
            f"ratio={before['ratio']:.2%}, 弢始自动修?.."
        )
        return self.repair_trade_date(
            trade_date=target_trade_date,
            sync_type=sync_type,
            max_workers=max_workers,
            threshold=threshold,
        )
    
    def get_all_stocks_and_indices(self) -> List[dict]:
        """Load stock and index universe from ClickHouse."""
        try:
            if not (clickhouse_available() and clickhouse_table_exists("stocks")):
                logger.warning("ClickHouse stocks table unavailable")
                return []
            df = clickhouse_query_df(
                """
                SELECT code, name, type, market
                FROM stocks
                WHERE type IN ('stock', 'index')
                ORDER BY code
                """
            )
            result = [] if df is None or df.empty else [
                {
                    "code": str(r.code),
                    "name": str(r.name or r.code),
                    "type": str(r.type or ""),
                    "market": str(r.market or ""),
                }
                for r in df.itertuples(index=False)
            ]
            logger.info(f"Loaded {len(result)} enabled stock/index records")
            return result
            
        except Exception as e:
            logger.error(f"Failed to load stock/index universe: {e}")
            return []
    
    def get_all_stocks(self) -> List[dict]:
        """Load stock universe from ClickHouse."""
        try:
            if not (clickhouse_available() and clickhouse_table_exists("stocks")):
                logger.warning("ClickHouse stocks table unavailable")
                return []
            df = clickhouse_query_df(
                """
                SELECT code, name, type, market
                FROM stocks
                WHERE type = 'stock' AND (quit = 0 OR quit IS NULL)
                ORDER BY code
                """
            )
            result = [] if df is None or df.empty else [
                {
                    "code": str(r.code),
                    "name": str(r.name or r.code),
                    "type": str(r.type or ""),
                    "market": str(r.market or ""),
                }
                for r in df.itertuples(index=False)
            ]
            logger.info(f"Loaded {len(result)} enabled stock records")
            return result
            
        except Exception as e:
            logger.error(f"Failed to load stock universe: {e}")
            return []
    
    def get_all_indices(self) -> List[dict]:
        """Load index universe from ClickHouse."""
        try:
            if not (clickhouse_available() and clickhouse_table_exists("stocks")):
                logger.warning("ClickHouse stocks table unavailable")
                return []
            df = clickhouse_query_df(
                """
                SELECT code, name, type, market
                FROM stocks
                WHERE type = 'index' AND (quit = 0 OR quit IS NULL)
                ORDER BY code
                """
            )
            result = [] if df is None or df.empty else [
                {
                    "code": str(r.code),
                    "name": str(r.name or r.code),
                    "type": str(r.type or ""),
                    "market": str(r.market or ""),
                }
                for r in df.itertuples(index=False)
            ]
            logger.info(f"Loaded {len(result)} index records from ClickHouse")
            return result
            
        except Exception as e:
            logger.error(f"Failed to load indices from ClickHouse: {e}")
            return []

    def _get_security_type(self, code: str) -> str:
        code = str(code or "").strip()
        if not code:
            return ""
        if code in self._security_type_cache:
            return self._security_type_cache[code]
        try:
            if clickhouse_available() and clickhouse_table_exists("stocks"):
                df = clickhouse_query_df("SELECT type FROM stocks WHERE code = ? LIMIT 1", [code])
                value = ""
                if df is not None and not df.empty:
                    value = str(df.iloc[0]["type"] or "")
                self._security_type_cache[code] = value
                return value
            from models.stock_models import Stock
            session = next(db.get_session())
            try:
                row = session.query(Stock.type).filter(Stock.code == code).first()
                value = str(row[0] or "") if row else ""
                self._security_type_cache[code] = value
                return value
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"security type lookup failed ({code}): {e}")
            return ""

    def _canonical_storage_code(self, code: str) -> str:
        """Normalize to canonical code stored in stocks table, e.g. 000001.SZ."""
        raw = str(code or "").strip()
        if not raw:
            return raw
        if raw in self._canonical_code_cache:
            return self._canonical_code_cache[raw]

        try:
            if clickhouse_available() and clickhouse_table_exists("stocks"):
                # 1) exact match first
                df = clickhouse_query_df("SELECT code FROM stocks WHERE code = ? LIMIT 1", [raw])
                if df is not None and not df.empty:
                    canonical = str(df.iloc[0]["code"] or raw)
                    self._canonical_code_cache[raw] = canonical
                    return canonical

                # 2) short code match (6 digits) -> deterministic by market priority
                short = raw[:6]
                if len(short) == 6 and short.isdigit():
                    df2 = clickhouse_query_df(
                        """
                        SELECT code
                        FROM stocks
                        WHERE substr(code, 1, 6) = ?
                        ORDER BY
                          CASE
                            WHEN code LIKE '%.SH' THEN 1
                            WHEN code LIKE '%.SZ' THEN 2
                            WHEN code LIKE '%.BJ' THEN 3
                            ELSE 9
                          END,
                          code
                        LIMIT 1
                        """,
                        [short],
                    )
                    if df2 is not None and not df2.empty:
                        canonical = str(df2.iloc[0]["code"] or raw)
                        self._canonical_code_cache[raw] = canonical
                        return canonical
        except Exception as e:
            logger.warning(f"canonical code resolve failed ({raw}): {e}")

        self._canonical_code_cache[raw] = raw
        return raw

    @staticmethod
    def _parse_ltgb_rows(rows: Optional[List[Dict[str, Any]]]) -> Dict[str, float]:
        result: Dict[str, float] = {}
        if not rows:
            return result
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                date_key = pd.Timestamp(str(int(row.get("Date")))).strftime("%Y-%m-%d")
                ltgb = float(row.get("Ltgb") or 0)
            except Exception:
                continue
            if ltgb > 0:
                result[date_key] = ltgb
        return result

    def _apply_index_daily_turnover_rate(self, code: str, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty or self._get_security_type(code) != "index":
            return df
        work_df = df.copy()
        if "trade_date" not in work_df.columns or "volume" not in work_df.columns:
            return work_df

        try:
            source = self.market_data_source.get_source("qmt_xtquant")
            info = source.get_stock_info(code) if source and hasattr(source, "get_stock_info") else None
            float_share = float((info or {}).get("float_share") or 0.0)
        except Exception as e:
            logger.warning(f"index turnover float-share fetch failed ({code}): {e}")
            float_share = 0.0

        if float_share <= 0:
            return work_df

        volumes = pd.to_numeric(work_df["volume"], errors="coerce")
        turnover = (volumes * 10000.0) / float_share
        mask = turnover.notna() & (turnover > 0)
        if mask.any():
            work_df.loc[mask, "turnover_rate"] = turnover.loc[mask]
        return work_df

    def get_watchlist_and_holding_stocks(self) -> List[dict]:
        """Load user watchlist and holding stocks."""
        try:
            if clickhouse_available():
                if clickhouse_table_exists("user_stocks") and clickhouse_table_exists("stocks"):
                    df = clickhouse_query_df(
                        """
                        SELECT DISTINCT s.code, s.name, s.type, s.market
                        FROM user_stocks u
                        JOIN stocks s ON s.code = u.code
                        ORDER BY s.code
                        """
                    )
                    result = [] if df is None or df.empty else [
                        {
                            "code": str(r.code),
                            "name": str(r.name or r.code),
                            "type": str(r.type or ""),
                            "market": str(r.market or ""),
                        }
                        for r in df.itertuples(index=False)
                    ]
                    logger.info(f"Loaded {len(result)} watchlist/holding stocks from ClickHouse")
                    return result
                logger.info("ClickHouse user_stocks table unavailable; skip watchlist sync")
                return []

            from models.stock_models import UserStock, Stock
            session = next(db.get_session())
            user_stocks = session.query(UserStock.code).distinct().all()
            watchlist_codes = [stock.code for stock in user_stocks]
            if not watchlist_codes:
                session.close()
                logger.info("No watchlist/holding stocks found")
                return []
            stocks = session.query(Stock).filter(Stock.code.in_(watchlist_codes)).all()
            result = [
                {"code": stock.code, "name": stock.name, "type": stock.type, "market": stock.market}
                for stock in stocks
            ]
            session.close()
            logger.info(f"Loaded {len(result)} watchlist/holding stocks")
            return result

        except Exception as e:
            logger.error(f"Failed to load watchlist/holding stocks: {e}")
            return []
    
    def get_kline_start_date(self, period: str) -> str:
        """Return sync start date for a K-line period."""
        today = datetime.now()

        if period in ['1m', '5m', '15m', '30m', '1h', '60m']:
            start_date = today - timedelta(days=90)
        elif period == 'daily':
            start_date = today - timedelta(days=365 * 10)
        elif period == 'weekly':
            start_date = today - timedelta(days=365 * 15)
        elif period == 'monthly':
            start_date = today - timedelta(days=365 * 20)
        else:
            start_date = today - timedelta(days=365 * 5)

        return start_date.strftime('%Y-%m-%d')

    def _is_index_stock(self, stock: dict, code: str) -> bool:
        stock_type = str((stock or {}).get("type") or "").lower()
        if stock_type == "index":
            return True
        return str(code or "").endswith((".SH", ".SZ", ".BJ"))

    def _filter_index_minute_anomalies(self, code: str, period: str, df: pd.DataFrame) -> pd.DataFrame:
        """Filter obvious index minute anomalies by comparing to daily close."""
        if df is None or df.empty or period not in {"1m", "5m", "15m", "30m", "1h", "60m"}:
            return df
        if "date" not in df.columns or "close" not in df.columns:
            return df

        try:
            from sqlalchemy import text

            work = df.copy()
            work["trade_date"] = pd.to_datetime(work["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            work["close"] = pd.to_numeric(work["close"], errors="coerce")
            work = work.dropna(subset=["trade_date", "close"])
            if work.empty:
                return df.iloc[0:0]

            min_day = work["trade_date"].min()
            max_day = work["trade_date"].max()
            sql = text(
                """
                SELECT trade_date, close
                FROM kline_daily
                WHERE code = :code
                  AND trade_date >= :min_day
                  AND trade_date <= :max_day
                """
            )
            with db._engine.connect() as conn:
                rows = conn.execute(sql, {"code": code, "min_day": min_day, "max_day": max_day}).fetchall()
            day_close_map = {str(r[0]): float(r[1]) for r in rows if r[1] is not None and float(r[1]) > 0}
            if not day_close_map:
                return work

            def _valid(row):
                day_close = day_close_map.get(row["trade_date"])
                if not day_close:
                    return True
                ratio = float(row["close"]) / day_close
                return 0.65 <= ratio <= 1.35

            before = len(work)
            filtered = work[work.apply(_valid, axis=1)].copy()
            dropped = before - len(filtered)
            if dropped > 0:
                logger.warning(
                    f"{code} {period} filtered anomaly minute rows: dropped={dropped}, kept={len(filtered)}, "
                    f"date_range={min_day}~{max_day}"
                )
            return filtered.drop(columns=["trade_date"], errors="ignore")
        except Exception as e:
            logger.error(f"{code} {period} minute anomaly filter failed, keep raw data: {e}")
            return df
    
    def sync_kline_for_stock(self, stock: dict, period: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> bool:
        """Sync one stock/index K-line period."""
        code = stock['code']
        name = stock['name']
        
        df = None  # 閸掓繂顫愰崠鏉乫閸欐﹢鍣?
        
        try:
            end_date = end_date or datetime.now().strftime('%Y-%m-%d')
            start_date = start_date or self.get_kline_start_date(period)

            def _candidate_codes(raw: str):
                s = str(raw or "").strip()
                if not s:
                    return []
                out = []
                out.append(s)
                # Expand an unqualified six-digit code for SDK-compatible lookup.
                if s.isdigit() and len(s) == 6:
                    for suf in ("SH", "SZ", "BJ"):
                        v = f"{s}.{suf}"
                        if v not in out:
                            out.append(v)
                return out

            last_err = None
            df = None
            source_used = self.preferred_source or "qmt_xtquant"
            for fetch_code in _candidate_codes(code):
                try:
                    df = self.market_data_source.get_stock_history(
                        stock_code=fetch_code,
                        start_date=start_date,
                        end_date=end_date,
                        period=period,
                    )
                    if df is not None and not df.empty:
                        break
                except Exception as e:
                    last_err = e
                    df = None
            
            if df is None or df.empty:
                if period == "1d":
                    fallback_df, fallback_provider = self._fetch_external_daily(
                        code=code,
                        start_date=start_date,
                        end_date=end_date,
                    )
                    if fallback_df is not None and not fallback_df.empty:
                        df = fallback_df
                        source_used = f"external:{fallback_provider}"
                        self.stats["external_fallback_success"] += 1
                        logger.info(f"{code} {name} {period} fallback source={fallback_provider} rows={len(df)}")
                    else:
                        self.stats["external_fallback_failed"] += 1

            if df is None or df.empty:
                same_day_query = bool(start_date and end_date and start_date == end_date)
                minute_periods = {"1m", "5m", "15m", "30m", "60m", "1h"}
                log_fn = logger.debug if (same_day_query and period in minute_periods) else logger.warning
                log_fn(f"{code} {name} {period} 閺冪姵鏆熼幑?start={start_date}, end={end_date}) err={last_err}")
                return False

            if self._is_index_stock(stock, code):
                df = self._filter_index_minute_anomalies(code, period, df)
                if df is None or df.empty:
                    logger.warning(f"{code} {name} {period} data empty after anomaly filtering, skip insert")
                    return False

            # QMT stock daily bars already use lots/yuan.  Index volume keeps
            # the established shares-to-lots conversion.
            if period == "1d" and source_used in QMT_DAILY_SOURCE_NAMES:
                instrument_type = (
                    "index"
                    if str((stock or {}).get("type") or "").strip().lower() == "index"
                    else "stock"
                )
                df = normalize_qmt_daily_units(df, instrument_type=instrument_type)
            
            # 娣囨繂鐡ㄩ崚鐗堟殶閹诡喖绨?
            serialize_minute_write = bool(self._is_index_stock(stock, code) and period in {'15m', '30m', '1h', '60m'})
            saved_rows = self._save_kline_to_db(code, period, df, serialize_minute_write=serialize_minute_write)
            if int(saved_rows or 0) <= 0:
                logger.warning(f"{code} {name} {period} has no valid rows to insert, raw rows={len(df)}")
                return False

            logger.info(f"{code} {name} {period} sync success: source={source_used}, saved_rows={saved_rows}, raw_rows={len(df)}")
            
            # 閲婃?DataFrame 鍐呭?
            del df
            import gc
            gc.collect()
            
            return True
            
        except Exception as e:
            logger.error(f"{code} {name} {period} sync failed: {e}")
            if df is not None:
                del df
                import gc
                gc.collect()
            return False
    
    def _save_kline_to_db(self, code: str, period: str, df: pd.DataFrame, serialize_minute_write: bool = False):
        """Save K-line data into database."""
        try:
            from sqlalchemy import text
            from utils.database import db
            from utils.market_warehouse import market_source
            
            # Use the active database dialect to choose the write path.
            engine = db._engine
            dialect_name = str(getattr(getattr(engine, "dialect", None), "name", "") or "").lower()
            is_clickhouse = ("clickhouse" in dialect_name) or (str(market_source() or "").lower() == "clickhouse")
            
            # Build a canonical storage code before writing K-line records.
            table_map = {
                '1m': 'kline_minute_1',
                '5m': 'kline_minute_5',
                '15m': 'kline_minute_15',
                '30m': 'kline_minute_30',
                '1h': 'kline_minute_60',
                '60m': 'kline_minute_60',
                '1d': 'kline_daily',
                '1w': 'kline_weekly',
                '1mon': 'kline_monthly',
                '1q': 'kline_quarterly',
                '1y': 'kline_yearly'
            }
            
            table_name = table_map.get(period, 'kline_daily')
            # ClickHouse single-path mode: write directly to base table.
            
            canonical_code = self._canonical_storage_code(code)
            df = df.copy()
            df['code'] = canonical_code
            df['created_at'] = datetime.now()
            
            # Normalize the source code before locating existing records.
            if table_name.startswith('kline_minute'):
                # 分钟线使用 datetime 字?
                column_map = {
                    'date': 'datetime',
                    'open': 'open',
                    'high': 'high',
                    'low': 'low',
                    'close': 'close',
                    'volume': 'volume',
                    'amount': 'amount'
                }
                
                df = df.rename(columns=column_map)
                
                columns = ['code', 'datetime', 'open', 'high', 'low', 'close', 'volume', 'amount', 'created_at']
                df = df[[col for col in columns if col in df.columns]]
                
            elif table_name == 'kline_daily':
                column_map = {
                    'date': 'trade_date',
                    'open': 'open',
                    'high': 'high',
                    'low': 'low',
                    'close': 'close',
                    'volume': 'volume',
                    'amount': 'amount'
                }
                
                df = df.rename(columns=column_map)
                
                df = df.sort_values('trade_date')
                
                if len(df) > 1:
                    # Use diff and shift for incremental daily-row repair.
                    df['change_amount'] = df['close'].diff()
                    df['change_pct'] = (df['change_amount'] / df['close'].shift(1)) * 100
                    df['amplitude'] = ((df['high'] - df['low']) / df['close'].shift(1)) * 100
                    
                    # Stop when no recent daily rows are available.
                    df.loc[df.index[0], 'change_amount'] = 0
                    df.loc[df.index[0], 'change_pct'] = 0
                    df.loc[df.index[0], 'amplitude'] = 0
                else:
                    from sqlalchemy import text
                    from utils.database import db
                    
                    engine = db._engine
                    code = df['code'].iloc[0]
                    trade_date_raw = df['trade_date'].iloc[0]
                    trade_date_dt = pd.to_datetime(trade_date_raw, errors='coerce')
                    if pd.isna(trade_date_dt):
                        trade_date = str(trade_date_raw)[:10]
                    else:
                        trade_date = trade_date_dt.strftime('%Y-%m-%d')
                    
                    query = text("""
                        SELECT close 
                        FROM kline_daily 
                        WHERE code = :code AND trade_date < :trade_date 
                        ORDER BY trade_date DESC 
                        LIMIT 1
                    """)
                    
                    with engine.connect() as conn:
                        result = conn.execute(query, {'code': str(code), 'trade_date': str(trade_date)})
                        prev_close_row = result.fetchone()
                        
                        if prev_close_row:
                            prev_close = float(prev_close_row[0])
                            df['change_amount'] = df['close'] - prev_close
                            if prev_close > 0:
                                df['change_pct'] = ((df['close'] - prev_close) / prev_close) * 100
                            else:
                                df['change_pct'] = 0
                            # 鐠侊紕鐣婚幐顖氱畽
                            if prev_close > 0:
                                df['amplitude'] = ((df['high'] - df['low']) / prev_close) * 100
                            else:
                                df['amplitude'] = 0
                        else:
                            # Keep only rows within the requested date range.
                            df['change_amount'] = 0
                            df['change_pct'] = 0
                            df['amplitude'] = 0
                
                df['turnover_rate'] = None
                df = self._apply_index_daily_turnover_rate(code, df)
                
                df['amplitude'] = df['amplitude'].replace([float('nan')], None)
                df['change_pct'] = df['change_pct'].replace([float('nan')], None)
                df['change_amount'] = df['change_amount'].replace([float('nan')], None)
                
                columns = ['code', 'trade_date', 'open', 'high', 'low', 'close', 'volume', 'amount', 
                          'amplitude', 'change_pct', 'change_amount', 'turnover_rate', 'created_at']
                df = df[[col for col in columns if col in df.columns]]
                
            elif table_name == 'kline_weekly':
                # 闁告稏鍔庨崵搴ｆ偘閵娿倕鈻忛柣?week_start_date 閻庢稒顨?
                column_map = {
                    'date': 'week_start_date',
                    'open': 'open',
                    'high': 'high',
                    'low': 'low',
                    'close': 'close',
                    'volume': 'volume',
                    'amount': 'amount'
                }
                
                df = df.rename(columns=column_map)
                
                columns = ['code', 'week_start_date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'created_at']
                df = df[[col for col in columns if col in df.columns]]
                
            elif table_name == 'kline_monthly':
                # 闁哄牆鐗忛崵搴ｆ偘閵娿倕鈻忛柣?month_start_date 閻庢稒顨?
                column_map = {
                    'date': 'month_start_date',
                    'open': 'open',
                    'high': 'high',
                    'low': 'low',
                    'close': 'close',
                    'volume': 'volume',
                    'amount': 'amount'
                }
                
                df = df.rename(columns=column_map)
                
                columns = ['code', 'month_start_date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'created_at']
                df = df[[col for col in columns if col in df.columns]]
                
            elif table_name == 'kline_quarterly':
                # Derive year and quarter fields from the K-line date.
                df['year'] = pd.to_datetime(df['date']).dt.year
                df['quarter'] = pd.to_datetime(df['date']).dt.quarter
                
                column_map = {
                    'open': 'open',
                    'high': 'high',
                    'low': 'low',
                    'close': 'close',
                    'volume': 'volume',
                    'amount': 'amount'
                }
                
                df = df.rename(columns=column_map)
                
                columns = ['code', 'year', 'quarter', 'open', 'high', 'low', 'close', 'volume', 'amount', 'created_at']
                df = df[[col for col in columns if col in df.columns]]
                
            elif table_name == 'kline_yearly':
                # Derive the year field from the K-line date.
                df['year'] = pd.to_datetime(df['date']).dt.year
                
                column_map = {
                    'open': 'open',
                    'high': 'high',
                    'low': 'low',
                    'close': 'close',
                    'volume': 'volume',
                    'amount': 'amount'
                }
                
                df = df.rename(columns=column_map)
                
                columns = ['code', 'year', 'open', 'high', 'low', 'close', 'volume', 'amount', 'created_at']
                df = df[[col for col in columns if col in df.columns]]
            
            # Define per-table keys and discard invalid rows before persistence.
            key_fields_map = {
                'kline_daily': ['trade_date'],
                'kline_weekly': ['week_start_date'],
                'kline_monthly': ['month_start_date'],
                'kline_quarterly': ['year', 'quarter'],
                'kline_yearly': ['year'],
            }
            key_fields = ['datetime'] if table_name.startswith('kline_minute') else key_fields_map.get(table_name, ['trade_date'])
            numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount', 'amplitude', 'change_pct', 'change_amount', 'turnover_rate']
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                    df[col] = df[col].replace([float('inf'), float('-inf')], pd.NA)

            required_cols = [col for col in key_fields if col in df.columns]
            if 'close' in df.columns:
                required_cols.append('close')
            if required_cols:
                before_rows = len(df)
                df = df.dropna(subset=required_cols)
                dropped_rows = before_rows - len(df)
                if dropped_rows > 0:
                    logger.warning(f"{code} {period} dropped invalid kline rows: {dropped_rows}")
            
            if table_name.startswith('kline_minute') and 'datetime' in df.columns:
                before_rows = len(df)
                df = df.dropna(subset=['datetime'])
                dropped_rows = before_rows - len(df)
                if dropped_rows > 0:
                    logger.warning(f"{code} {period} dropped rows with None datetime: {dropped_rows}")

            df = df.replace([float('inf'), float('-inf')], pd.NA)
            df = df.astype(object).where(pd.notna(df), None)

            def _normalize_temporal_value(value, mode: str):
                if value is None:
                    return None
                try:
                    ts = pd.to_datetime(value, errors='coerce')
                except Exception:
                    return value
                if pd.isna(ts):
                    return None
                try:
                    # clickhouse/pandas mixed path may call tz_convert internally;
                    # force safe native python temporal objects before execute().
                    if getattr(ts, "tzinfo", None) is not None:
                        ts = ts.tz_convert("Asia/Shanghai").tz_localize(None)
                except Exception:
                    try:
                        ts = ts.tz_localize(None)
                    except Exception:
                        pass
                if mode == "date":
                    return ts.date()
                return ts.to_pydatetime()

            if 'trade_date' in df.columns:
                df['trade_date'] = df['trade_date'].apply(lambda v: _normalize_temporal_value(v, "date"))
            if 'week_start_date' in df.columns:
                df['week_start_date'] = df['week_start_date'].apply(lambda v: _normalize_temporal_value(v, "date"))
            if 'month_start_date' in df.columns:
                df['month_start_date'] = df['month_start_date'].apply(lambda v: _normalize_temporal_value(v, "date"))
            if 'datetime' in df.columns:
                df['datetime'] = df['datetime'].apply(lambda v: _normalize_temporal_value(v, "datetime"))
            if 'created_at' in df.columns:
                df['created_at'] = df['created_at'].apply(lambda v: _normalize_temporal_value(v, "datetime"))

            if table_name.startswith('kline_minute') and 'datetime' in df.columns:
                before_rows = len(df)
                min_clickhouse_dt = datetime(1970, 1, 1)
                max_clickhouse_dt = datetime(2100, 1, 1)
                df = df[
                    df['datetime'].apply(
                        lambda v: isinstance(v, datetime) and min_clickhouse_dt <= v < max_clickhouse_dt
                    )
                ]
                dropped_rows = before_rows - len(df)
                if dropped_rows > 0:
                    logger.warning(f"{code} {period} dropped invalid ClickHouse datetime rows: {dropped_rows}")

            if table_name == 'kline_daily':
                from utils.kline_store import filter_trading_day_rows

                before_rows = len(df)
                df = filter_trading_day_rows('1d', df)
                if df.empty:
                    logger.warning(f"{code} {period} blocked by trade_calendar guard: dropped={before_rows}")
                    return 0

            def _sanitize_param_value(value):
                if value is None:
                    return None
                # hard guard for pandas/numpy temporal scalars
                if isinstance(value, pd.Timestamp):
                    if value.tzinfo is not None:
                        try:
                            value = value.tz_convert("Asia/Shanghai").tz_localize(None)
                        except Exception:
                            value = value.tz_localize(None)
                    return value.to_pydatetime()
                try:
                    # numpy.datetime64 / datetime-like string
                    if hasattr(value, "dtype") or isinstance(value, str):
                        parsed = pd.to_datetime(value, errors="coerce")
                        if not pd.isna(parsed):
                            if getattr(parsed, "tzinfo", None) is not None:
                                try:
                                    parsed = parsed.tz_convert("Asia/Shanghai").tz_localize(None)
                                except Exception:
                                    parsed = parsed.tz_localize(None)
                            return parsed.to_pydatetime()
                except Exception:
                    pass
                return value

            def _sanitize_record_for_sql(record: dict) -> dict:
                out = dict(record)
                for key in ("trade_date", "week_start_date", "month_start_date"):
                    if key in out and out[key] is not None:
                        val = _sanitize_param_value(out[key])
                        if isinstance(val, datetime):
                            out[key] = val.strftime("%Y-%m-%d")
                        elif hasattr(val, "strftime"):
                            out[key] = val.strftime("%Y-%m-%d")
                        else:
                            out[key] = val
                for key in ("datetime", "created_at"):
                    if key in out and out[key] is not None:
                        val = _sanitize_param_value(out[key])
                        if isinstance(val, datetime):
                            out[key] = val.strftime("%Y-%m-%d %H:%M:%S")
                        elif hasattr(val, "strftime"):
                            out[key] = val.strftime("%Y-%m-%d %H:%M:%S")
                        else:
                            # best effort fallback
                            parsed = pd.to_datetime(val, errors="coerce")
                            out[key] = None if pd.isna(parsed) else parsed.to_pydatetime().strftime("%Y-%m-%d %H:%M:%S")
                return out

            if table_name.startswith('kline_minute'):
                from utils.kline_store import ensure_kline_tables_exist, insert_kline_dataframe

                clickhouse_period = '60m' if period == '1h' else period
                ensure_kline_tables_exist()
                write_guard = self._minute_write_lock if serialize_minute_write else nullcontext()
                with write_guard:
                    return insert_kline_dataframe(clickhouse_period, df, on_conflict='upsert')

            # For ClickHouse 1d writes, keep all writes on the active SQLAlchemy path.
            # Keep all writes on the active SQLAlchemy engine path.

            if not df.empty:
                batch_size = 50
                total_rows = len(df)
                inserted_rows = 0
                write_guard = nullcontext()
                
                with write_guard:
                    with engine.connect() as conn:
                        for i in range(0, total_rows, batch_size):
                            batch_df = df.iloc[i:i+batch_size]
                            
                            if table_name == 'kline_daily':
                                # ClickHouse does not support this DELETE pattern on MergeTree tables.
                                # For ClickHouse: append-only INSERT (idempotency handled downstream/queries).
                                # For non-ClickHouse: keep delete+insert behavior.
                                if not is_clickhouse:
                                    date_range = batch_df['trade_date'].tolist()
                                    min_date = min(date_range)
                                    max_date = max(date_range)
                                    delete_sql = f"""
                                    DELETE FROM {table_name} 
                                    WHERE code = :code AND trade_date >= :min_date AND trade_date <= :max_date
                                    """
                                    conn.execute(
                                        text(delete_sql),
                                        {
                                            'code': code,
                                            'min_date': _sanitize_record_for_sql({'trade_date': min_date}).get('trade_date'),
                                            'max_date': _sanitize_record_for_sql({'trade_date': max_date}).get('trade_date'),
                                        }
                                    )
                                    conn.commit()

                                insert_sql = f"""
                                INSERT INTO {table_name} 
                                (code, trade_date, open, high, low, close, volume, amount, 
                                 amplitude, change_pct, change_amount, turnover_rate, created_at)
                                VALUES (:code, :trade_date, :open, :high, :low, :close, :volume, :amount, 
                                        :amplitude, :change_pct, :change_amount, :turnover_rate, :created_at)
                                """
                            elif table_name == 'kline_weekly':
                                # 周?- 先删除后插入᳡保数捜新且避免死?                                # 获取批业日期范围
                                date_range = batch_df['week_start_date'].tolist()
                                min_date = min(date_range)
                                max_date = max(date_range)
                                
                                delete_sql = f"""
                                DELETE FROM {table_name} 
                                WHERE code = :code AND week_start_date >= :min_date AND week_start_date <= :max_date
                                """
                                conn.execute(
                                    text(delete_sql),
                                    {
                                        'code': code,
                                        'min_date': _sanitize_record_for_sql({'week_start_date': min_date}).get('week_start_date'),
                                        'max_date': _sanitize_record_for_sql({'week_start_date': max_date}).get('week_start_date'),
                                    }
                                )
                                conn.commit()
                                
                                insert_sql = f"""
                                INSERT INTO {table_name} 
                                (code, week_start_date, open, high, low, close, volume, amount, created_at)
                                VALUES (:code, :week_start_date, :open, :high, :low, :close, :volume, :amount, :created_at)
                                """
                            elif table_name == 'kline_monthly':
                                # 月?- 先删除后插入᳡保数捜新且避免死?                                # 获取批业日期范围
                                date_range = batch_df['month_start_date'].tolist()
                                min_date = min(date_range)
                                max_date = max(date_range)
                                
                                delete_sql = f"""
                                DELETE FROM {table_name} 
                                WHERE code = :code AND month_start_date >= :min_date AND month_start_date <= :max_date
                                """
                                conn.execute(
                                    text(delete_sql),
                                    {
                                        'code': code,
                                        'min_date': _sanitize_record_for_sql({'month_start_date': min_date}).get('month_start_date'),
                                        'max_date': _sanitize_record_for_sql({'month_start_date': max_date}).get('month_start_date'),
                                    }
                                )
                                conn.commit()
                                
                                insert_sql = f"""
                                INSERT INTO {table_name} 
                                (code, month_start_date, open, high, low, close, volume, amount, created_at)
                                VALUES (:code, :month_start_date, :open, :high, :low, :close, :volume, :amount, :created_at)
                                """
                            elif table_name == 'kline_quarterly':
                                # 季度线?- 先删除后插入᳡保数捜新且避免死?                                years = batch_df['year'].tolist()
                                quarters = batch_df['quarter'].tolist()
                                min_year = min(years)
                                max_year = max(years)
                                min_quarter = min(quarters)
                                max_quarter = max(quarters)
                                
                                delete_sql = f"""
                                DELETE FROM {table_name} 
                                WHERE code = :code AND year >= :min_year AND year <= :max_year
                                AND quarter >= :min_quarter AND quarter <= :max_quarter
                                """
                                conn.execute(text(delete_sql), {'code': code, 'min_year': min_year, 'max_year': max_year, 'min_quarter': min_quarter, 'max_quarter': max_quarter})
                                conn.commit()
                                
                                insert_sql = f"""
                                INSERT INTO {table_name} 
                                (code, year, quarter, open, high, low, close, volume, amount, created_at)
                                VALUES (:code, :year, :quarter, :open, :high, :low, :close, :volume, :amount, :created_at)
                                """
                            elif table_name == 'kline_yearly':
                                # 年?- 先删除后插入᳡保数捜新且避免死?                                # 获取批业年份范围
                                years = batch_df['year'].tolist()
                                min_year = min(years)
                                max_year = max(years)
                                
                                delete_sql = f"""
                                DELETE FROM {table_name} 
                                WHERE code = :code AND year >= :min_year AND year <= :max_year
                                """
                                conn.execute(text(delete_sql), {'code': code, 'min_year': min_year, 'max_year': max_year})
                                conn.commit()
                                
                                insert_sql = f"""
                                INSERT INTO {table_name} 
                                (code, year, open, high, low, close, volume, amount, created_at)
                                VALUES (:code, :year, :open, :high, :low, :close, :volume, :amount, :created_at)
                                """
                            else:
                                # 默情?- 先删除后插入᳡保数捜新且避免死?                                # 获取批业日期范围
                                date_range = batch_df['trade_date'].tolist()
                                min_date = min(date_range)
                                max_date = max(date_range)
                                
                                delete_sql = f"""
                                DELETE FROM {table_name} 
                                WHERE code = :code AND trade_date >= :min_date AND trade_date <= :max_date
                                """
                                conn.execute(
                                    text(delete_sql),
                                    {
                                        'code': code,
                                        'min_date': _sanitize_record_for_sql({'trade_date': min_date}).get('trade_date'),
                                        'max_date': _sanitize_record_for_sql({'trade_date': max_date}).get('trade_date'),
                                    }
                                )
                                conn.commit()
                                
                                insert_sql = f"""
                                INSERT INTO {table_name} 
                                (code, trade_date, open, high, low, close, volume, amount, created_at)
                                VALUES (:code, :trade_date, :open, :high, :low, :close, :volume, :amount, :created_at)
                                """
                            
                            # Retry transient database write failures.
                            max_retries = 3
                            retry_count = 0
                            success = False
                            
                            while retry_count < max_retries and not success:
                                try:
                                    records = [_sanitize_record_for_sql(r) for r in batch_df.to_dict('records')]
                                    if records and retry_count == 0:
                                        type_snapshot = {k: type(v).__name__ for k, v in records[0].items()}
                                        logger.info(f"[kline-write-types] code={code}, period={period}, first_record_types={type_snapshot}")
                                    conn.execute(text(insert_sql), records)
                                    conn.commit()
                                    success = True
                                    inserted_rows += len(batch_df)
                                except Exception as e:
                                    retry_count += 1
                                    if retry_count >= max_retries:
                                        logger.exception(f"Batch write failed for {code} {period}; retry {max_retries}: {e}")
                                        # Retry each record separately after a batch failure.
                                        conn.rollback()
                                        # 尝试单条插?
                                        for _, row in batch_df.iterrows():
                                            try:
                                                conn.execute(text(insert_sql), _sanitize_record_for_sql(row.to_dict()))
                                                conn.commit()
                                                inserted_rows += 1
                                            except Exception as single_e:
                                                logger.exception(f"Single-record write failed for {code} {period}: {single_e}")
                                                conn.rollback()
                                    else:
                                        logger.warning(f"Batch write failed for {code} {period}; retry {retry_count}: {e}")
                                        conn.rollback()
                                        time.sleep(0.5 * retry_count)  # incremental retry backoff
                return inserted_rows
            return 0
                
        except Exception as e:
            logger.error(f"Failed to persist K-line data for {code} {period}: {e}")
            raise
    
    def sync_all_klines(self, max_workers: int = 2, periods: Optional[List[str]] = None, type: Optional[str] = None):
        # Sync all kline data.
        # max_workers: maximum worker count
        # periods: target periods such as ['1m', '15m', '1d']
        # type: target asset type, e.g. 'index' or 'stock'
        logger.info("=" * 80)
        logger.info(f"Start K-line sync, type={type}")
        logger.info("=" * 80)
        
        # Synchronize all selected symbols for the requested periods.
        sync_periods = periods or list(self.periods.keys())
        
        # Initialize per-period result counters.
        for period in sync_periods:
            if period not in self.periods:
                logger.warning(f"Unknown period {period}, skip")
                continue
            
            period_name = self.periods[period]
            logger.info(f"\n{'='*80}")
            logger.info(f"Start period sync: {period_name} ({period})")
            logger.info(f"{'='*80}")
            
            # Select the configured source universe for the requested synchronization type.
            if period == '1m':
                # Start stock synchronization and submit one task per symbol.
                stocks = self.get_watchlist_and_holding_stocks()
            else:
                # Select the complete eligible universe for the requested type.
                if type == 'index':
                    stocks = self.get_all_indices()
                elif type == 'stock':
                    stocks = self.get_all_stocks()
                else:
                    stocks = self.get_all_stocks_and_indices()
            
            if not stocks:
                logger.warning(f"No targets found for {period_name}; skip")
                continue
            
            self.stats['total'] += len(stocks)
            
            period_success = 0
            period_failed = 0
            job_anchor_datetime = None
            anchor_trade_date = None

            if period == '1d':
                job_anchor_datetime = datetime.now()
                anchor_trade_date = self._get_latest_closed_trade_date(job_anchor_datetime)
                logger.info(
                    f"Daily sync anchor_trade_date={anchor_trade_date}, "
                    f"job_start={job_anchor_datetime.isoformat()}"
                )
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                if period == '1d' and anchor_trade_date:
                    future_to_stock = {
                        executor.submit(
                            self.sync_kline_for_stock,
                            stock,
                            period,
                            anchor_trade_date,
                            anchor_trade_date,
                        ): stock
                        for stock in stocks
                    }
                else:
                    future_to_stock = {
                        executor.submit(self.sync_kline_for_stock, stock, period): stock
                        for stock in stocks
                    }
                
                # 婢跺嫮鎮婄紒鎾寸亯
                for future in as_completed(future_to_stock):
                    stock = future_to_stock[future]
                    try:
                        if future.result():
                            period_success += 1
                            self.stats['success'] += 1
                        else:
                            period_failed += 1
                            self.stats['failed'] += 1
                    except Exception as e:
                        logger.error(f"婢跺嫮鎮?{stock['code']} 閺冭泛鍤? {e}")
                        period_failed += 1
                        self.stats['failed'] += 1
                    finally:
                        # Release completed futures promptly to reduce memory pressure.
                        del future
                    
                    # Report progress and release memory after every 100 symbols.
                    if (period_success + period_failed) % 100 == 0:
                        logger.info(f"Progress {period_success + period_failed}/{len(stocks)}, ",
                                  f"succeeded {period_success}, failed {period_failed}")
                        # Release completed futures before the next batch.
                        import gc
                        gc.collect()
            
            # 娓呯悊future_to_stock閲婃斁鍐呭瓨
            del future_to_stock
            import gc
            gc.collect()
            
            logger.info(f"{period_name} synchronization completed: succeeded {period_success}, failed {period_failed}")

            if period == '1d' and anchor_trade_date:
                crossed_midnight = datetime.now().date() != job_anchor_datetime.date()
                if crossed_midnight:
                    logger.warning(
                        f"任务执期间跨过午，但已继绔定基准?anchor_trade_date={anchor_trade_date}"
                    )

                snapshot = self._get_trade_date_candidates(anchor_trade_date, type)
                logger.info(
                    f"鐎瑰本鏆ｉ幀?trade_date={anchor_trade_date}, actual={snapshot['actual_count']}, "
                    f"baseline={snapshot['baseline_count']}, minimum_required={snapshot['minimum_required']}, "
                    f"ratio={snapshot['ratio']:.2%}, crossed_midnight={crossed_midnight}"
                )
                if snapshot["is_incomplete"]:
                    repair_result = self.repair_trade_date(
                        trade_date=anchor_trade_date,
                        sync_type=type,
                        max_workers=max(1, min(max_workers, 3)),
                        threshold=DAILY_COMPLETENESS_THRESHOLD,
                    )
                    if repair_result["status"] != "completed":
                        logger.error(
                            f"閼峰﹤鎮楁禒宥勭瑝鐎?trade_date={anchor_trade_date}, "
                            f"after={repair_result['after']['actual_count']}/{repair_result['after']['baseline_count']}, "
                            f"remaining={repair_result['remaining_count']}"
                        )
            
            # 每个周期结束后暂停一下，避免请求过于频繁
            time.sleep(5)
        
        logger.info(f"\n{'='*80}")
        logger.info("K-line sync finished")
        logger.info(
            f"total={self.stats['total']}, success={self.stats['success']}, "
            f"failed={self.stats['failed']}, skipped={self.stats['skipped']}, "
            f"external_success={self.stats['external_fallback_success']}, "
            f"external_failed={self.stats['external_fallback_failed']}"
        )
        logger.info(f"{'='*80}")


def main():
    """Main entry."""
    syncer = KlineSyncer()
    syncer.sync_all_klines(max_workers=3)


if __name__ == "__main__":
    main()
