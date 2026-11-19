
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from utils.database import db
from sqlalchemy import text


@dataclass
class BackfillStats:
    requested: int = 0
    fetched: int = 0
    written: int = 0
    skipped: int = 0
    failed: int = 0


class TushareHistoryBackfill:
    def __init__(self, token: Optional[str] = None, sleep_seconds: float = 0.7):
        from scripts.sync_all_klines import KlineSyncer

        self.syncer = KlineSyncer()
        if token:
            self.syncer.tushare_token = token.strip()
        self.sleep_seconds = max(0.0, float(sleep_seconds))
        self.stats = BackfillStats()

    def _get_pro(self):
        pro = self.syncer._get_tushare_pro()
        if pro is None:
            raise RuntimeError("Tushare Pro unavailable; please configure TUSHARE_TOKEN")
        return pro

    def _load_codes(self, asset_type: str = "stock", limit: Optional[int] = None) -> List[str]:
        sql = text(
            """
            SELECT code
            FROM stocks
            WHERE type = :asset_type
              AND quit = 0
            ORDER BY code ASC
            """
        )
        with db.engine.connect() as conn:
            rows = conn.execute(sql, {"asset_type": asset_type}).fetchall()
        codes = [str(row[0]) for row in rows if str(row[0] or "").strip()]
        return codes[:limit] if limit else codes

    def fetch_daily(self, code: str, start_date: str, end_date: str, provider: str = "tushare") -> Optional[pd.DataFrame]:
        provider = str(provider or "tushare").strip().lower()
        if provider == "tushare":
            df = self.syncer._fetch_external_daily_from_tushare(code=code, start_date=start_date, end_date=end_date)
        elif provider == "akshare":
            df = self.syncer._fetch_external_daily_from_akshare(code=code, start_date=start_date, end_date=end_date)
        else:
            raise ValueError(f"unsupported provider: {provider}")
        if df is None or df.empty:
            return None
        self.stats.fetched += 1
        return df

    def fetch_minute(self, code: str, period: str, start_dt: str, end_dt: str, asset: str = "E") -> Optional[pd.DataFrame]:
        period = str(period).strip().lower()
        freq_map = {"15m": "15min", "30m": "30min", "60m": "60min", "1h": "60min"}
        freq = freq_map.get(period)
        if not freq:
            raise ValueError(f"unsupported minute period: {period}")

        ts_code = self.syncer._to_ts_code(code)
        df = None
        last_error = None
        try:
            import tushare as ts
            ts.set_token(self.syncer.tushare_token)
            try:
                df = ts.pro_bar(
                    ts_code=ts_code,
                    asset=asset,
                    freq=freq,
                    start_date=start_dt,
                    end_date=end_dt,
                    adj="qfq" if asset == "E" else None,
                )
            except Exception as exc:
                last_error = exc
                pro = self._get_pro()
                if asset == "E":
                    df = pro.stk_mins(ts_code=ts_code, freq=freq, start_date=start_dt, end_date=end_dt)
                else:
                    raise
        except Exception as exc:
            raise RuntimeError(f"fetch minute failed for {code} {period}: {last_error or exc}") from exc

        if df is None or df.empty:
            return None

        rename_map = {
            "trade_time": "date",
            "datetime": "date",
            "vol": "volume",
            "trade_date": "date",
        }
        work_df = df.rename(columns=rename_map).copy()
        if "date" not in work_df.columns:
            raise RuntimeError(f"unexpected tushare minute columns for {code} {period}: {list(df.columns)}")
        work_df["date"] = pd.to_datetime(work_df["date"], errors="coerce")
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            if col in work_df.columns:
                work_df[col] = pd.to_numeric(work_df[col], errors="coerce")
        work_df = work_df.dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date").reset_index(drop=True)
        if work_df.empty:
            return None
        if "amount" not in work_df.columns:
            work_df["amount"] = work_df["volume"] * work_df["close"]
        self.stats.fetched += 1
        return work_df[["date", "open", "high", "low", "close", "volume", "amount"]]

    def persist(self, code: str, period: str, df: pd.DataFrame, serialize_minute_write: bool = True) -> int:
        if df is None or df.empty:
            self.stats.skipped += 1
            return 0
        written = int(self.syncer._save_kline_to_db(code=code, period=period, df=df, serialize_minute_write=serialize_minute_write) or 0)
        self.stats.written += written
        return written

    def backfill_codes(self, codes: Iterable[str], start_date: str, end_date: str, periods: List[str], provider: str = "tushare") -> BackfillStats:
        for raw_code in codes:
            code = str(raw_code or "").strip()
            if not code:
                continue
            for period in periods:
                self.stats.requested += 1
                try:
                    if period == "1d":
                        df = self.fetch_daily(code=code, start_date=start_date, end_date=end_date, provider=provider)
                    else:
                        df = self.fetch_minute(
                            code=code,
                            period=period,
                            start_dt=f"{start_date} 09:00:00",
                            end_dt=f"{end_date} 15:00:00",
                            asset="E",
                        )
                    self.persist(code=code, period=period, df=df)
                except Exception:
                    self.stats.failed += 1
                    raise
                if self.sleep_seconds > 0:
                    time.sleep(self.sleep_seconds)
        return self.stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill long-history A-share data from Tushare/AkShare into current AiStock schema")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--periods", default="1d,15m,30m")
    parser.add_argument("--provider", default="tushare", choices=["tushare", "akshare"])
    parser.add_argument("--asset-type", default="stock", choices=["stock", "index"])
    parser.add_argument("--codes", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.7)
    parser.add_argument("--token", default=os.getenv("TUSHARE_TOKEN", ""))
    args = parser.parse_args()

    backfill = TushareHistoryBackfill(token=args.token, sleep_seconds=args.sleep)
    periods = [str(item).strip().lower() for item in str(args.periods or "").split(",") if str(item).strip()]
    if args.codes.strip():
        codes = [str(item).strip() for item in args.codes.split(",") if str(item).strip()]
    else:
        codes = backfill._load_codes(asset_type=args.asset_type, limit=(args.limit or None))

    stats = backfill.backfill_codes(codes=codes, start_date=args.start_date, end_date=args.end_date, periods=periods, provider=args.provider)
    print({
        "requested": stats.requested,
        "fetched": stats.fetched,
        "written": stats.written,
        "skipped": stats.skipped,
        "failed": stats.failed,
        "codes": len(codes),
        "periods": periods,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "provider": args.provider,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
