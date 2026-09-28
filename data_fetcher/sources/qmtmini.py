from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from core.base import DataSourceStatus
from data_fetcher.base_fetcher import BaseDataSource
from utils.logger import get_logger

from .qmtmini_client import QmtMiniMarketClient


logger = get_logger("QmtMiniDataSource")


QMT_STOCK_SECTORS = {
    "ALL": "\u6caa\u6df1\u4eacA\u80a1",
    "SH": "\u4e0a\u8bc1A\u80a1",
    "SZ": "\u6df1\u8bc1A\u80a1",
    "BJ": "\u4eac\u5e02A\u80a1",
}

QMT_INDEX_SECTORS = {
    "ALL": "\u6caa\u6df1\u6307\u6570",
    "SH": "\u6caa\u5e02\u6307\u6570",
    "SZ": "\u6df1\u5e02\u6307\u6570",
    "BJ": "\u6caa\u6df1\u6307\u6570",
    "9": "\u6caa\u6df1\u6307\u6570",
}

PERIOD_TO_QMT = {
    "daily": "1d",
    "weekly": "1w",
    "monthly": "1mon",
    "quarterly": "1q",
    "1d": "1d",
    "1w": "1w",
    "1mon": "1mon",
    "1q": "1q",
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "60m": "60m",
}


def _normalize_market(market: Any) -> str:
    text = str(market or "ALL").strip().upper()
    if text in {"5", "ALL", "A", "STOCK"}:
        return "ALL"
    if text in {"9", "INDEX"}:
        return "9"
    return text


def _normalize_code(stock_code: str) -> str:
    code = str(stock_code or "").strip()
    if "." in code:
        parts = code.split(".")
        return f"{parts[0]}.{parts[-1].upper()}"
    if code.startswith(("6", "5", "9")):
        return f"{code}.SH"
    if code.startswith(("0", "2", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return code


def _market_from_code(code: str) -> str:
    if "." in code:
        return code.split(".")[-1].lower()
    return ""


def _compact_code(code: str) -> str:
    return str(code or "").split(".")[0]


def _to_yyyymmdd(value: str) -> str:
    text = str(value or "").strip()
    return text.replace("-", "").replace("/", "")[:8]


def _expired_date(value: Any) -> str:
    text = _to_yyyymmdd(value)
    return text if _has_past_expire_date(text) else ""


def _detail_to_stock_row(code: str, detail: Optional[Dict[str, Any]], stock_type: str) -> Dict[str, Any]:
    detail = detail or {}
    full_code = _normalize_code(code)
    name = detail.get("InstrumentName") or _compact_code(full_code)
    return {
        "code": full_code,
        "Code": full_code,
        "name": name,
        "Name": name,
        "market": _market_from_code(full_code),
        "type": stock_type,
        "list_date": detail.get("OpenDate") or detail.get("CreateDate") or "",
        "delist_date": _expired_date(detail.get("ExpireDate")),
        "st": 1 if "ST" in str(name).upper() else 0,
        "quit": 0 if _is_active_stock_detail(detail) else 1,
        "float_share": float(detail.get("FloatVolume") or 0.0),
        "total_share": float(detail.get("TotalVolume") or 0.0),
        "industry": "",
        "industry_code": "",
        "region": "",
        "source": "qmt_xtquant",
    }


def _has_past_expire_date(value: Any) -> bool:
    text = str(value or "").strip()
    if text in {"", "0", "99999999"}:
        return False
    try:
        expire = datetime.strptime(text[:8], "%Y%m%d").date()
    except Exception:
        return False
    # QMT's historical-contract cache also exposes values such as ``10011011``.
    # They are internal sentinels rather than calendar dates; treating them as
    # year-1001 dates both creates false delistings and cannot be written to a
    # ClickHouse Date column.  Mainland A-share instruments start in 1990.
    return date(1990, 1, 1) <= expire <= datetime.now().date()


def _is_active_stock_detail(detail: Optional[Dict[str, Any]]) -> bool:
    if not detail:
        return False
    if _has_past_expire_date(detail.get("ExpireDate")):
        return False
    product_id = str(detail.get("ProductID") or "").upper()
    if product_id in {"R"}:
        return False
    return True


def _market_data_field(data: Dict[str, Any], lower_name: str) -> Optional[pd.DataFrame]:
    value = data.get(lower_name)
    if value is None:
        value = data.get(lower_name[:1].upper() + lower_name[1:])
    return value


def _aggregate_5m_history(df: pd.DataFrame, target_period: str) -> pd.DataFrame:
    if df is None or df.empty or target_period not in {"15m", "30m", "60m"}:
        return df

    group_size = {"15m": 3, "30m": 6, "60m": 12}[target_period]
    work = df.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    work = work.dropna(subset=["datetime"]).sort_values("datetime")
    if work.empty:
        return pd.DataFrame()

    out: list[dict[str, Any]] = []
    for _, day_df in work.groupby(work["datetime"].dt.strftime("%Y-%m-%d")):
        day_df = day_df.sort_values("datetime").copy()
        times = day_df["datetime"].dt.strftime("%H:%M")
        morning = day_df[(times >= "09:35") & (times <= "11:30")]
        afternoon = day_df[(times >= "13:05") & (times <= "15:00")]
        for session_df in (morning, afternoon):
            for idx in range(0, len(session_df), group_size):
                bucket = session_df.iloc[idx : idx + group_size]
                if len(bucket) < group_size:
                    continue
                dt = pd.Timestamp(bucket.iloc[-1]["datetime"])
                out.append(
                    {
                        "date": dt,
                        "datetime": dt,
                        "open": float(bucket.iloc[0]["open"]),
                        "high": float(pd.to_numeric(bucket["high"], errors="coerce").max()),
                        "low": float(pd.to_numeric(bucket["low"], errors="coerce").min()),
                        "close": float(bucket.iloc[-1]["close"]),
                        "volume": float(pd.to_numeric(bucket["volume"], errors="coerce").fillna(0).sum()),
                        "amount": float(pd.to_numeric(bucket["amount"], errors="coerce").fillna(0).sum()),
                    }
                )
    return pd.DataFrame(out)


class QmtMiniDataSource(BaseDataSource):
    def __init__(self, name: str = "qmtmini", config: Optional[Dict[str, Any]] = None):
        default_config = {
            "enabled": True,
            "priority": 0,
            "supported_types": [
                "stock_quote",
                "realtime_quote",
                "stock_history",
                "stock_info",
                "stock_list",
            ],
        }
        merged = {**default_config, **(config or {})}
        super().__init__(name, merged)
        self.client = QmtMiniMarketClient()
        self._connected = False

    def _ensure_client(self) -> QmtMiniMarketClient:
        if not self._connected:
            self.client.connect()
            self._connected = True
        return self.client

    def health_check(self) -> bool:
        try:
            self._ensure_client().get_market_last_trade_date("SH")
            self.mark_success()
            return True
        except Exception as exc:
            self.mark_failure(str(exc))
            return False

    def get_stock_info(self, stock_code: str) -> Optional[Dict[str, Any]]:
        try:
            code = _normalize_code(stock_code)
            detail = self._ensure_client().get_instrument_detail(code)
            if not detail:
                return None
            self.mark_success()
            return _detail_to_stock_row(code, detail, "stock")
        except Exception as exc:
            self.mark_failure(str(exc))
            logger.warning(f"QMT stock info failed for {stock_code}: {exc}")
            return None

    def refresh_expired_contracts(self) -> None:
        """Refresh QMT's expired-contract cache before metadata reconciliation."""
        self._ensure_client().download_history_contracts(incrementally=True)

    def get_expired_stock_info(self, stock_codes: List[str]) -> Dict[str, Dict[str, Any]]:
        """Return only stocks whose QMT contract detail has a past ExpireDate."""
        if not stock_codes:
            return {}
        details = self._ensure_client().get_instrument_detail_list(stock_codes, True)
        result: Dict[str, Dict[str, Any]] = {}
        for code in stock_codes:
            detail = details.get(code) or {}
            if _has_past_expire_date(detail.get("ExpireDate")):
                result[_normalize_code(code)] = _detail_to_stock_row(code, detail, "stock")
        return result

    def get_stock_list(
        self,
        market: str = "SH",
        stock_type: str = "stock",
        list_type: Any = None,
        market_type: Any = None,
        **_: Any,
    ) -> Optional[List[Dict[str, Any]]]:
        try:
            normalized_market = _normalize_market(market_type if market_type is not None else market)
            normalized_type = str(stock_type or "stock").lower()
            if normalized_market == "9" or normalized_type == "index" or list_type == 1:
                sector_name = QMT_INDEX_SECTORS.get(normalized_market, QMT_INDEX_SECTORS["ALL"])
                row_type = "index"
            else:
                sector_name = QMT_STOCK_SECTORS.get(normalized_market, QMT_STOCK_SECTORS["ALL"])
                row_type = "stock"

            client = self._ensure_client()
            # QMT removes delisted instruments from the normal A-share sectors.
            # Refresh their separate history-contract cache first so the caller
            # can reconcile removed codes with ExpireDate afterwards.
            client.download_history_contracts(incrementally=True)
            codes = client.get_stock_list_in_sector(sector_name)
            if not codes:
                return []

            details = client.get_instrument_detail_list(codes, False)
            rows = []
            for code in codes:
                detail = details.get(code)
                if row_type == "stock" and not _is_active_stock_detail(detail):
                    continue
                rows.append(_detail_to_stock_row(code, detail, row_type))
            self.mark_success()
            return rows
        except Exception as exc:
            self.mark_failure(str(exc))
            logger.warning(f"QMT stock list failed: {exc}")
            return None

    def get_stock_quote(self, stock_code: str) -> Optional[Dict[str, Any]]:
        quotes = self.get_realtime_quotes([stock_code])
        return quotes[0] if quotes else None

    def get_realtime_quotes(self, stock_codes: List[str]) -> Optional[List[Dict[str, Any]]]:
        try:
            codes = [_normalize_code(code) for code in stock_codes]
            ticks = self._ensure_client().get_full_tick(codes) or {}
            rows: List[Dict[str, Any]] = []
            for code in codes:
                tick = ticks.get(code) or {}
                last = float(tick.get("lastPrice") or tick.get("last_price") or 0.0)
                pre_close = float(tick.get("lastClose") or tick.get("preClose") or 0.0)
                rows.append(
                    {
                        "code": code,
                        "price": last,
                        "open": float(tick.get("open") or 0.0),
                        "high": float(tick.get("high") or 0.0),
                        "low": float(tick.get("low") or 0.0),
                        "pre_close": pre_close,
                        "volume": int(tick.get("volume") or 0),
                        "amount": float(tick.get("amount") or 0.0),
                        "change": last - pre_close if pre_close else 0.0,
                        "change_pct": ((last - pre_close) / pre_close * 100) if pre_close else 0.0,
                        "time": datetime.now().isoformat(),
                        "source": "qmt_xtquant",
                    }
                )
            self.mark_success()
            return rows
        except Exception as exc:
            self.mark_failure(str(exc))
            logger.warning(f"QMT realtime quotes failed: {exc}")
            return None

    def get_stock_history(
        self,
        stock_code: str,
        start_date: str,
        end_date: str,
        period: str = "daily",
    ) -> Optional[pd.DataFrame]:
        try:
            qmt_period = PERIOD_TO_QMT.get(str(period), str(period))
            request_period = "5m" if qmt_period in {"15m", "30m", "60m"} else qmt_period
            code = _normalize_code(stock_code)
            client = self._ensure_client()
            try:
                client.download_history_data(
                    code,
                    request_period,
                    _to_yyyymmdd(start_date),
                    _to_yyyymmdd(end_date),
                )
            except Exception as exc:
                logger.warning(f"QMT history download failed for {code} {request_period}: {exc}")
            data = client.get_market_data(
                field_list=["open", "high", "low", "close", "volume", "amount"],
                stock_list=[code],
                period=request_period,
                start_time=_to_yyyymmdd(start_date),
                end_time=_to_yyyymmdd(end_date),
                count=-1,
                dividend_type="front",
                fill_data=False,
            )
            close_df = _market_data_field(data, "close")
            if not isinstance(close_df, pd.DataFrame) or close_df.empty:
                return pd.DataFrame()
            open_df = _market_data_field(data, "open")
            high_df = _market_data_field(data, "high")
            low_df = _market_data_field(data, "low")
            volume_df = _market_data_field(data, "volume")
            amount_df = _market_data_field(data, "amount")
            if not all(isinstance(df, pd.DataFrame) for df in [open_df, high_df, low_df, volume_df, amount_df]):
                return pd.DataFrame()
            if code not in close_df.index and code not in close_df.columns:
                return pd.DataFrame()
            rows = []
            if code in close_df.index:
                time_values = list(close_df.columns)
                value_at = lambda df, idx: df.loc[code, idx]
            else:
                time_values = list(close_df.index)
                value_at = lambda df, idx: df.loc[idx, code]
            for idx in time_values:
                rows.append(
                    {
                        "date": idx,
                        "datetime": idx,
                        "open": float(value_at(open_df, idx)),
                        "high": float(value_at(high_df, idx)),
                        "low": float(value_at(low_df, idx)),
                        "close": float(value_at(close_df, idx)),
                        "volume": float(value_at(volume_df, idx)),
                        "amount": float(value_at(amount_df, idx)),
                    }
                )
            result = pd.DataFrame(rows)
            if qmt_period in {"15m", "30m", "60m"} and not result.empty:
                result = _aggregate_5m_history(result, qmt_period)
            self.mark_success()
            return result
        except Exception as exc:
            self.mark_failure(str(exc))
            logger.warning(f"QMT stock history failed for {stock_code}: {exc}")
            return None

    def get_sector_data(self, sector_code: str = None) -> Optional[Any]:
        self.status = DataSourceStatus.DEGRADED
        return None
