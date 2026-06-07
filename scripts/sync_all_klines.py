"""
全量K线数据同步脚本

同步所有 stock 和 index 类型的历史 K 线数据。
支持所有周期：1m, 5m, 15m, 30m, 60m, 1d, 1w, 1mon, 1q, 1y。
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

# 添加项目根目录到 Python 搜索路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from data_fetcher.sources.tdxquant import TdxQuantDataSource
from data_fetcher.sources.tdxquant_pool import tdxquant_pool
from utils.config import ConfigManager
from utils.logger import get_logger
from utils.database import db
from utils.market_warehouse import clickhouse_available, clickhouse_table_exists, clickhouse_query_df

logger = get_logger("SyncAllKlines")

DAILY_COMPLETENESS_THRESHOLD = 0.95
EARLY_MORNING_CUTOFF_HOUR = 6


class KlineSyncer:
    """K线数据同步器"""
    
    def __init__(self):
        """初始化"""
        self.config = {'enabled': True, 'priority': 0}
        self.tdxquant = TdxQuantDataSource(name="tdxquant", config=self.config)
        self.config_manager = ConfigManager()

        sync_settings = self.config_manager.get("data_sync", {}, config_file="settings.yaml") or {}
        self.preferred_source = str(sync_settings.get("preferred_source", "tdxquant")).strip().lower()
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
        
        # 支持的周期列表（使用统一的时间单位标准）。
        # 标准格式：1m, 5m, 15m, 30m, 60m, 1d, 1w, 1mon, 1q, 1y
        from utils.period_constants import PERIOD_DISPLAY_NAMES
        self.periods = PERIOD_DISPLAY_NAMES
        
        # 统计信息
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
            "日期": "date",
            "交易日期": "date",
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
            "鏀剁洏": "close",
            "turnover_volume": "volume",
            "turnover_amount": "amount",
            "vol": "volume",
            "amount(鍗冨厓)": "amount",
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
            return self._normalize_external_daily_frame(df)
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
            return self._normalize_external_daily_frame(df)
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
            logger.warning(f"朻交易日历解析到已收盘交易日，使用回??日期 {fallback}")
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
            f"开始修复 trade_date={trade_date}, scope={sync_type or 'all'}, "
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
            f"补齐完成 trade_date={trade_date}, after={after['actual_count']}/{after['baseline_count']}, "
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
            f"?测到异常必 trade_date={target_trade_date}, "
            f"actual={before['actual_count']}, baseline={before['baseline_count']}, "
            f"ratio={before['ratio']:.2%}, 开始自动修复..."
        )
        return self.repair_trade_date(
            trade_date=target_trade_date,
            sync_type=sync_type,
            max_workers=max_workers,
            threshold=threshold,
        )
    
    def get_all_stocks_and_indices(self) -> List[dict]:
        """获取全部股票和指数代码。"""
        try:
            if not (clickhouse_available() and clickhouse_table_exists("stocks")):
                logger.warning("ClickHouse stocks 表不可用，返回空证券列表")
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
            logger.info(f"获取到 {len(result)} 只 stock/index 类型证券")
            return result
            
        except Exception as e:
            logger.error(f"获取股票列表失败: {e}")
            return []
    
    def get_all_stocks(self) -> List[dict]:
        """获取全部股票代码。"""
        try:
            if not (clickhouse_available() and clickhouse_table_exists("stocks")):
                logger.warning("ClickHouse stocks 表不可用，返回空股票列表")
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
            logger.info(f"获取到 {len(result)} 只 stock 类型股票")
            return result
            
        except Exception as e:
            logger.error(f"获取股票列表失败: {e}")
            return []
    
    def get_all_indices(self) -> List[dict]:
        """获取全部指数代码。"""
        try:
            if not (clickhouse_available() and clickhouse_table_exists("stocks")):
                logger.warning("ClickHouse stocks 表不可用，返回空指数列表")
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
            logger.info(f"获取到 {len(result)} 只 index 类型股票")
            return result
            
        except Exception as e:
            logger.error(f"获取指数列表失败: {e}")
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

        date_values = (
            pd.to_datetime(work_df["trade_date"], errors="coerce")
            .dropna()
            .dt.strftime("%Y%m%d")
            .drop_duplicates()
            .tolist()
        )
        if not date_values:
            return work_df

        try:
            rows = tdxquant_pool.get_gb_info(stock_code=code, date_list=date_values, count=len(date_values))
        except Exception as e:
            logger.warning(f"index turnover ltgb fetch failed ({code}): {e}")
            rows = None

        ltgb_by_date = self._parse_ltgb_rows(rows)
        if not ltgb_by_date:
            return work_df

        trade_dates = pd.to_datetime(work_df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        volumes = pd.to_numeric(work_df["volume"], errors="coerce")
        ltgb = trade_dates.map(ltgb_by_date)
        turnover = (volumes * 10000.0) / ltgb
        mask = turnover.notna() & (turnover > 0)
        if mask.any():
            work_df.loc[mask, "turnover_rate"] = turnover.loc[mask]
        return work_df

    def get_watchlist_and_holding_stocks(self) -> List[dict]:
        """获取自选股和持仓股。"""
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
                    logger.info(f"获取到 {len(result)} 只自选股和持仓股")
                    return result
                logger.info("ClickHouse 缺少 user_stocks 表，按空自选池处理")
                return []

            from models.stock_models import UserStock, Stock
            session = next(db.get_session())
            user_stocks = session.query(UserStock.code).distinct().all()
            watchlist_codes = [stock.code for stock in user_stocks]
            if not watchlist_codes:
                session.close()
                logger.info("暂无自选股和持仓股")
                return []
            stocks = session.query(Stock).filter(Stock.code.in_(watchlist_codes)).all()
            result = [
                {"code": stock.code, "name": stock.name, "type": stock.type, "market": stock.market}
                for stock in stocks
            ]
            session.close()
            logger.info(f"获取到 {len(result)} 只自选股和持仓股")
            return result

        except Exception as e:
            logger.error(f"获取自选股和持仓股失败: {e}")
            return []
    
    def get_kline_start_date(self, period: str) -> str:
        """根据周期返回默认的 K 线起始日期。"""
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
        """同单只股票的K线数?"""
        code = stock['code']
        name = stock['name']
        
        df = None  # 初始化df变量
        
        try:
            # 确日期范围
            end_date = end_date or datetime.now().strftime('%Y-%m-%d')
            start_date = start_date or self.get_kline_start_date(period)

            def _candidate_codes(raw: str):
                s = str(raw or "").strip()
                if not s:
                    return []
                out = []
                out.append(s)
                # 避免向底?SDK 传?裸代码（ 600000），仅在纕字时补全后缀候??                # 600000 -> 600000.SH / 600000.SZ / 600000.BJ (尽量覆盖常市场后缀)
                if s.isdigit() and len(s) == 6:
                    for suf in ("SH", "SZ", "BJ"):
                        v = f"{s}.{suf}"
                        if v not in out:
                            out.append(v)
                return out

            # 获取 K 线数据，对数据源 code 格式做容错
            last_err = None
            df = None
            source_used = self.preferred_source or "tdxquant"
            for fetch_code in _candidate_codes(code):
                try:
                    df = self.tdxquant.get_stock_history(
                        stock_code=fetch_code,
                        start_date=start_date,
                        end_date=end_date,
                        period=period,
                        dividend_type='front'  # 前复权
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
                log_fn(f"{code} {name} {period} 无数据(start={start_date}, end={end_date}) err={last_err}")
                return False

            if self._is_index_stock(stock, code):
                df = self._filter_index_minute_anomalies(code, period, df)
                if df is None or df.empty:
                    logger.warning(f"{code} {name} {period} data empty after anomaly filtering, skip insert")
                    return False
            
            # 保存到数据库
            serialize_minute_write = bool(self._is_index_stock(stock, code) and period in {'15m', '30m', '1h', '60m'})
            saved_rows = self._save_kline_to_db(code, period, df, serialize_minute_write=serialize_minute_write)
            if int(saved_rows or 0) <= 0:
                logger.warning(f"{code} {name} {period} has no valid rows to insert, raw rows={len(df)}")
                return False

            logger.info(f"{code} {name} {period} sync success: source={source_used}, saved_rows={saved_rows}, raw_rows={len(df)}")
            
            # 释放 DataFrame 内存
            del df
            import gc
            gc.collect()
            
            return True
            
        except Exception as e:
            logger.error(f"{code} {name} {period} 同步失败: {e}")
            # 硿异常时也释放内存
            if df is not None:
                del df
                import gc
                gc.collect()
            return False
    
    def _save_kline_to_db(self, code: str, period: str, df: pd.DataFrame, serialize_minute_write: bool = False):
        """保存 K 线数据到数据库。"""
        try:
            from sqlalchemy import text
            from utils.database import db
            from utils.market_warehouse import market_source
            
            # 重用全局数据库连接池
            engine = db._engine
            dialect_name = str(getattr(getattr(engine, "dialect", None), "name", "") or "").lower()
            is_clickhouse = ("clickhouse" in dialect_name) or (str(market_source() or "").lower() == "clickhouse")
            
            # 根据周期确定表名
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
            
            # 数据准备（统一 code 为 stocks 表中的规范 code）
            canonical_code = self._canonical_storage_code(code)
            df = df.copy()
            df['code'] = canonical_code
            df['created_at'] = datetime.now()
            
            # 根据表类型确定列名映射和数据处理
            if table_name.startswith('kline_minute'):
                # 分钟线表使用 datetime 字段
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
                # 日线表使?trade_date 字，并计算涨跌幅等信息
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
                    # 如果有多天数据，使用diff和shift计算
                    df['change_amount'] = df['close'].diff()
                    df['change_pct'] = (df['change_amount'] / df['close'].shift(1)) * 100
                    df['amplitude'] = ((df['high'] - df['low']) / df['close'].shift(1)) * 100
                    
                    # 笸天没有前?天数捼设置?
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
                            # 计算振幅
                            if prev_close > 0:
                                df['amplitude'] = ((df['high'] - df['low']) / prev_close) * 100
                            else:
                                df['amplitude'] = 0
                        else:
                            # 没有前一天的数据，罸0
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
                # 鍛ㄧ嚎琛ㄤ娇鐢?week_start_date 瀛楁
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
                # 鏈堢嚎琛ㄤ娇鐢?month_start_date 瀛楁
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
                # 季度线表?要特殊理，从date丏取year和quarter
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
                # 年线表需要特殊理，从date丏取year
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
            
            # 批量处理数据，减少数捺操作次数
            # 统一清洗：将非法数?标准化，避?NaN/inf 入库
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
            
            # 对于分钟线数据，额外确保datetime列没有None值
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
                # 按批次理，每批50条（减少批大小以降低锁率）
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
                                # 周线?- 先删除后插入，确保数捜新且避免死锁
                                # 获取批业日期范围
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
                                # 月线?- 先删除后插入，确保数捜新且避免死锁
                                # 获取批业日期范围
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
                                # 季度线表 - 先删除后插入，确保数捜新且避免死锁
                                years = batch_df['year'].tolist()
                                quarters = batch_df['quarter'].tolist()
                                min_year = min(years)
                                max_year = max(years)
                                min_quarter = min(quarters)
                                max_quarter = max(quarters)
                                
                                # 先删除股票在年份和季度范围内的数据
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
                                # 年线?- 先删除后插入，确保数捜新且避免死锁
                                # 获取批业年份范围
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
                                # 默情况 - 先删除后插入，确保数捜新且避免死锁
                                # 获取批业日期范围
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
                            
                            # 带重试机制的批量插入
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
                                        logger.exception(f"批量插入数据失败 {code} {period}，已重试{max_retries}: {e}")
                                        # 回滚事务
                                        conn.rollback()
                                        # 尝试单条插入
                                        for _, row in batch_df.iterrows():
                                            try:
                                                conn.execute(text(insert_sql), _sanitize_record_for_sql(row.to_dict()))
                                                conn.commit()
                                                inserted_rows += 1
                                            except Exception as single_e:
                                                logger.exception(f"单条插入数据失败 {code} {period}: {single_e}")
                                                conn.rollback()
                                    else:
                                        logger.warning(f"批量插入数据失败 {code} {period}，第 {retry_count} 次重试: {e}")
                                        conn.rollback()
                                        time.sleep(0.5 * retry_count)  # 指数退避，减少锁冲突

                return inserted_rows
            return 0
                
        except Exception as e:
            logger.error(f"保存K线数据失败 {code} {period}: {e}")
            raise
    
    def sync_all_klines(self, max_workers: int = 2, periods: Optional[List[str]] = None, type: Optional[str] = None):
        # Sync all kline data.
        # max_workers: maximum worker count
        # periods: target periods such as ['1m', '15m', '1d']
        # type: target asset type, e.g. 'index' or 'stock'
        logger.info("=" * 80)
        logger.info(f"开始同步 K 线数据，类型: {type}")
        logger.info("=" * 80)
        
        # 确定要同步的周期
        sync_periods = periods or list(self.periods.keys())
        
        # 逐个周期同步
        for period in sync_periods:
            if period not in self.periods:
                logger.warning(f"Unknown period {period}, skip")
                continue
            
            period_name = self.periods[period]
            logger.info(f"\n{'='*80}")
            logger.info(f"开始同步 {period_name} ({period}) 数据")
            logger.info(f"{'='*80}")
            
            # 根据周期和类型选择证券列表
            if period == '1m':
                # 1分钟数据只同步自选股和持仓股
                stocks = self.get_watchlist_and_holding_stocks()
            else:
                # 根据类型选择证券列表
                if type == 'index':
                    stocks = self.get_all_indices()
                elif type == 'stock':
                    stocks = self.get_all_stocks()
                else:
                    # 同步全部股票和指数
                    stocks = self.get_all_stocks_and_indices()
            
            if not stocks:
                logger.warning(f"没有获取到证券列表，跳过 {period_name} 同步")
                continue
            
            # 更新统信息
            self.stats['total'] += len(stocks)
            
            period_success = 0
            period_failed = 0
            job_anchor_datetime = None
            anchor_trade_date = None

            if period == '1d':
                job_anchor_datetime = datetime.now()
                anchor_trade_date = self._get_latest_closed_trade_date(job_anchor_datetime)
                logger.info(
                    f"任务已锁定基准日 anchor_trade_date={anchor_trade_date}, "
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
                
                # 处理结果
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
                        logger.error(f"处理 {stock['code']} 时出? {e}")
                        period_failed += 1
                        self.stats['failed'] += 1
                    finally:
                        # 删除future释放内存
                        del future
                    
                    # 每处理100只打印进度并强制垃圾回收
                    if (period_success + period_failed) % 100 == 0:
                        logger.info(f"进度: {period_success + period_failed}/{len(stocks)}, "
                                  f"成功: {period_success}, 失败: {period_failed}")
                        # 强制垃圾回收
                        import gc
                        gc.collect()
            
            # 清理future_to_stock释放内存
            del future_to_stock
            import gc
            gc.collect()
            
            logger.info(f"{period_name} 同步完成: 成功 {period_success}, 失败 {period_failed}")

            if period == '1d' and anchor_trade_date:
                crossed_midnight = datetime.now().date() != job_anchor_datetime.date()
                if crossed_midnight:
                    logger.warning(
                        f"任务执期间跨过午，但已继绔定基准日 anchor_trade_date={anchor_trade_date}"
                    )

                snapshot = self._get_trade_date_candidates(anchor_trade_date, type)
                logger.info(
                    f"完整性?trade_date={anchor_trade_date}, actual={snapshot['actual_count']}, "
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
                            f"臊后仍不完?trade_date={anchor_trade_date}, "
                            f"after={repair_result['after']['actual_count']}/{repair_result['after']['baseline_count']}, "
                            f"remaining={repair_result['remaining_count']}"
                        )
            
            # 每个周期结束后暂停一下，避免请求过于频繁
            time.sleep(5)
        
        # 打印最终统计
        logger.info(f"\n{'='*80}")
        logger.info("全量 K 线数据同步完成")
        logger.info(
            f"总计: {self.stats['total']}, 成功: {self.stats['success']}, "
            f"失败: {self.stats['failed']}, 跳过: {self.stats['skipped']}, "
            f"外部回填成功: {self.stats['external_fallback_success']}, "
            f"外部回填失败: {self.stats['external_fallback_failed']}"
        )
        logger.info(f"{'='*80}")


def main():
    """Main entry."""
    syncer = KlineSyncer()
    syncer.sync_all_klines(max_workers=3)


if __name__ == "__main__":
    main()
