from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from utils.database import db


@dataclass
class BackfillStats:
    requested: int = 0
    fetched: int = 0
    written: int = 0
    skipped: int = 0
    failed: int = 0


class BaoStockHistoryBackfill:
    def __init__(self, sleep_seconds: float = 0.1, adjustflag: str = "2"):
        from scripts.sync_all_klines import KlineSyncer

        self.syncer = KlineSyncer()
        self.sleep_seconds = max(0.0, float(sleep_seconds))
        self.adjustflag = str(adjustflag or "2").strip()
        self.stats = BackfillStats()
        self._bs = None
        self._logged_in = False

    def login(self) -> None:
        if self._logged_in:
            return
        try:
            import baostock as bs
        except ImportError as exc:
            raise RuntimeError("baostock not installed; run `pip install baostock` first") from exc

        result = bs.login()
        if getattr(result, "error_code", "0") != "0":
            raise RuntimeError(f"baostock login failed: {result.error_code} {result.error_msg}")
        self._bs = bs
        self._logged_in = True

    def logout(self) -> None:
        if self._bs is not None and self._logged_in:
            try:
                self._bs.logout()
            finally:
                self._logged_in = False

    @staticmethod
    def _strip_market_suffix(code: str) -> str:
        value = str(code or "").strip().upper()
        return value.split(".", 1)[0] if "." in value else value

    @staticmethod
    def _to_baostock_code(code: str) -> str:
        value = str(code or "").strip()
        if not value:
            raise ValueError("empty code")
        if "." in value:
            left, right = value.split(".", 1)
            if len(left) == 2 and left.lower() in {"sh", "sz"}:
                return f"{left.lower()}.{right}"
            return f"{right.lower()}.{left}"

        upper = value.upper()
        if upper.startswith(("60", "68", "90", "000")):
            return f"sh.{upper}"
        if upper.startswith(("00", "30", "20")):
            return f"sz.{upper}"
        raise ValueError(f"unsupported market for baostock code: {code}")

    @staticmethod
    def _to_storage_code(code: str) -> str:
        value = str(code or "").strip()
        if not value:
            raise ValueError("empty code")
        if "." in value:
            left, right = value.split(".", 1)
            if len(left) == 2 and left.lower() in {"sh", "sz", "bj"}:
                return f"{right.upper()}.{left.upper()}"
            return f"{left.upper()}.{right.upper()}"

        upper = value.upper()
        if upper.startswith(("60", "68", "90", "000")):
            return f"{upper}.SH"
        if upper.startswith(("00", "30", "20")):
            return f"{upper}.SZ"
        if upper.startswith(("43", "83", "87", "92")):
            return f"{upper}.BJ"
        raise ValueError(f"unsupported storage code: {code}")

    @staticmethod
    def _normalize_time(value: str, trade_date: Optional[str] = None) -> Optional[pd.Timestamp]:
        raw = str(value or "").strip()
        if not raw:
            return None

        digits = "".join(ch for ch in raw if ch.isdigit())
        candidates = []
        if len(digits) >= 14:
            candidates.append(digits[:14])
        if len(digits) >= 12:
            candidates.append(digits[:12])

        if trade_date:
            day = str(trade_date).replace("-", "").strip()
            if len(day) == 8:
                if len(digits) in {4, 6}:
                    hhmmss = digits if len(digits) == 6 else f"{digits}00"
                    candidates.append(f"{day}{hhmmss}")
                elif len(digits) >= 14:
                    candidates.append(digits[:14])

        for candidate in candidates:
            fmt = "%Y%m%d%H%M%S" if len(candidate) == 14 else "%Y%m%d%H%M"
            parsed = pd.to_datetime(candidate, format=fmt, errors="coerce")
            if not pd.isna(parsed):
                return parsed

        parsed = pd.to_datetime(raw, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed

    def _load_assets(self, asset_type: str = "stock", limit: Optional[int] = None) -> List[dict]:
        sql = text(
            """
            SELECT code, market, type
            FROM stocks
            WHERE type = :asset_type
              AND market IN ('sh', 'sz', 'SH', 'SZ')
              AND (quit = 0 OR quit IS NULL)
            ORDER BY code ASC
            """
        )
        with db.engine.connect() as conn:
            rows = conn.execute(sql, {"asset_type": asset_type}).fetchall()
        assets = [
            {
                "code": str(row[0]).strip(),
                "market": str(row[1] or "").strip().lower(),
                "type": str(row[2] or "").strip().lower(),
            }
            for row in rows
            if str(row[0] or "").strip()
        ]
        return assets[:limit] if limit else assets

    def _resolve_assets(self, codes: Iterable[str], asset_type: str) -> List[dict]:
        normalized_codes = []
        for raw_code in codes:
            value = str(raw_code or "").strip()
            if not value:
                continue
            normalized_codes.append(self._to_storage_code(value))
        if not normalized_codes:
            return []

        placeholders = ", ".join([f":code_{idx}" for idx in range(len(normalized_codes))])
        sql = text(
            f"""
            SELECT code, market, type
            FROM stocks
            WHERE type = :asset_type
              AND code IN ({placeholders})
            ORDER BY code ASC
            """
        )
        params = {"asset_type": asset_type}
        params.update({f"code_{idx}": code for idx, code in enumerate(normalized_codes)})
        with db.engine.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "code": str(row[0]).strip(),
                "market": str(row[1] or "").strip().lower(),
                "type": str(row[2] or "").strip().lower(),
            }
            for row in rows
            if str(row[0] or "").strip()
        ]

    def fetch_minute(self, code: str, period: str, start_date: str, end_date: str, adjustflag: Optional[str] = None) -> Optional[pd.DataFrame]:
        self.login()

        period = str(period or "60m").strip().lower()
        freq_map = {"5m": "5", "15m": "15", "30m": "30", "60m": "60", "1h": "60"}
        frequency = freq_map.get(period)
        if not frequency:
            raise ValueError(f"unsupported baostock minute period: {period}")

        bs_code = self._to_baostock_code(code)
        fields = "date,time,code,open,high,low,close,volume,amount"
        result = self._bs.query_history_k_data_plus(
            code=bs_code,
            fields=fields,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjustflag=str(adjustflag or self.adjustflag),
        )
        if getattr(result, "error_code", "0") != "0":
            raise RuntimeError(f"baostock query failed for {code}: {result.error_code} {result.error_msg}")

        rows = []
        while result.next():
            rows.append(result.get_row_data())
        if not rows:
            return None

        df = pd.DataFrame(rows, columns=result.fields)
        if df.empty:
            return None

        df["date"] = [
            self._normalize_time(time_value, trade_date=date_value)
            for time_value, date_value in zip(df.get("time", []), df.get("date", []))
        ]
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["date", "open", "high", "low", "close"])
        if df.empty:
            return None

        if "volume" not in df.columns:
            df["volume"] = 0
        if "amount" not in df.columns:
            df["amount"] = 0
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)

        df = df[["date", "open", "high", "low", "close", "volume", "amount"]]
        df = df.sort_values("date").reset_index(drop=True)
        self.stats.fetched += 1
        return df

    def _persist_minute_optimized(self, code: str, period: str, df: pd.DataFrame) -> int:
        table_map = {
            "5m": "kline_minute_5",
            "15m": "kline_minute_15",
            "30m": "kline_minute_30",
            "60m": "kline_minute_60",
            "1h": "kline_minute_60",
        }
        table_name = table_map.get(str(period or "").strip().lower())
        if not table_name:
            raise ValueError(f"unsupported minute period for persist: {period}")

        work_df = df.copy()
        work_df = work_df.rename(columns={"date": "datetime"})
        work_df["code"] = code
        work_df["created_at"] = datetime.now()
        work_df = work_df[["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at"]]
        work_df = work_df.dropna(subset=["code", "datetime", "open", "high", "low", "close"])
        if work_df.empty:
            return 0

        work_df["volume"] = pd.to_numeric(work_df["volume"], errors="coerce").fillna(0).astype("int64")
        work_df["amount"] = pd.to_numeric(work_df["amount"], errors="coerce").fillna(0.0)
        for col in ["open", "high", "low", "close"]:
            work_df[col] = pd.to_numeric(work_df[col], errors="coerce")
        work_df = work_df.sort_values("datetime").reset_index(drop=True)

        min_dt = work_df["datetime"].min()
        max_dt = work_df["datetime"].max()
        delete_sql = text(
            f"""
            DELETE FROM {table_name}
            WHERE code = :code
              AND datetime >= :min_dt
              AND datetime <= :max_dt
            """
        )
        insert_sql = text(
            f"""
            INSERT INTO {table_name}
            (code, datetime, open, high, low, close, volume, amount, created_at)
            VALUES (:code, :datetime, :open, :high, :low, :close, :volume, :amount, :created_at)
            """
        )

        chunk_size = 1000
        inserted = 0
        with db.engine.begin() as conn:
            conn.execute(delete_sql, {"code": code, "min_dt": min_dt, "max_dt": max_dt})
            for start in range(0, len(work_df), chunk_size):
                chunk = work_df.iloc[start:start + chunk_size]
                conn.execute(insert_sql, chunk.to_dict("records"))
                inserted += len(chunk)
        return inserted

    def persist(self, code: str, period: str, df: pd.DataFrame, is_index: bool = False) -> int:
        if df is None or df.empty:
            self.stats.skipped += 1
            return 0
        storage_code = self._to_storage_code(code)
        written = int(self._persist_minute_optimized(code=storage_code, period=period, df=df) or 0)
        self.stats.written += written
        return written

    def backfill_assets(self, assets: Iterable[dict], start_date: str, end_date: str, periods: List[str], asset_type: str = "stock") -> BackfillStats:
        is_index = str(asset_type or "").strip().lower() == "index"
        try:
            self.login()
            for asset in assets:
                code = str((asset or {}).get("code") or "").strip()
                if not code:
                    continue
                for period in periods:
                    self.stats.requested += 1
                    try:
                        df = self.fetch_minute(
                            code=code,
                            period=period,
                            start_date=start_date,
                            end_date=end_date,
                            adjustflag="3" if is_index else self.adjustflag,
                        )
                        self.persist(code=code, period=period, df=df, is_index=is_index)
                    except Exception:
                        self.stats.failed += 1
                        raise
                    if self.sleep_seconds > 0:
                        time.sleep(self.sleep_seconds)
        finally:
            self.logout()
        return self.stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill A-share/index minute history from BaoStock into current AiStock schema")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--periods", default="60m")
    parser.add_argument("--asset-type", default="stock", choices=["stock", "index"])
    parser.add_argument("--codes", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.1)
    parser.add_argument("--adjustflag", default="2", choices=["1", "2", "3"])
    args = parser.parse_args()

    backfill = BaoStockHistoryBackfill(sleep_seconds=args.sleep, adjustflag=args.adjustflag)
    periods = [str(item).strip().lower() for item in str(args.periods or "").split(",") if str(item).strip()]
    if args.codes.strip():
        assets = backfill._resolve_assets(
            codes=[str(item).strip() for item in args.codes.split(",") if str(item).strip()],
            asset_type=args.asset_type,
        )
    else:
        assets = backfill._load_assets(asset_type=args.asset_type, limit=(args.limit or None))

    stats = backfill.backfill_assets(
        assets=assets,
        start_date=args.start_date,
        end_date=args.end_date,
        periods=periods,
        asset_type=args.asset_type,
    )
    print(
        {
            "requested": stats.requested,
            "fetched": stats.fetched,
            "written": stats.written,
            "skipped": stats.skipped,
            "failed": stats.failed,
            "codes": len(assets),
            "periods": periods,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "asset_type": args.asset_type,
            "adjustflag": args.adjustflag,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
