from __future__ import annotations

import os
import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.request import urlopen

import pandas as pd


DEFAULT_QMT_ROOT = Path(os.getenv("AISTOCK_QMT_ROOT", "D:\\国金QMT\\国金证券QMT交易端"))
DEFAULT_QMT_MINI_USERDATA = DEFAULT_QMT_ROOT / "userdata_mini"
DEFAULT_QMT_QUOTE_HOST = os.getenv("AISTOCK_QMT_QUOTE_HOST", "127.0.0.1")
DEFAULT_QMT_QUOTE_PORT = int(os.getenv("AISTOCK_QMT_QUOTE_PORT", "58610"))
DEFAULT_QMT_ACCOUNT_BRIDGE_URL = os.getenv("AISTOCK_QMT_ACCOUNT_BRIDGE_URL", "").strip()


class QmtMiniError(RuntimeError):
    pass


class QmtMiniCapabilityError(QmtMiniError):
    def __init__(self, function_name: str, original_error: Exception):
        self.function_name = function_name
        self.original_error = original_error
        super().__init__(
            "QMT Mini service does not expose required xtdata handler "
            f"{function_name}: {original_error}. Check broker/QMT edition, "
            "xtquant terminal compatibility, or enable the required QMT/xtdata permission."
        )


def _missing_handler_function(exc: Exception) -> str | None:
    text = str(exc)
    if "200005" not in text and "\u672a\u627e\u5230\u5904\u7406\u51fd\u6570" not in text:
        return None
    marker = "func:"
    if marker not in text:
        return "unknown"
    return text.split(marker, 1)[1].split(",", 1)[0].strip() or "unknown"


def _call_xtdata(function_name: str, callback):
    try:
        return callback()
    except RuntimeError as exc:
        missing_function = _missing_handler_function(exc)
        if missing_function:
            raise QmtMiniCapabilityError(missing_function or function_name, exc) from exc
        raise


def _load_xtdata():
    try:
        from xtquant import xtdata  # type: ignore
    except ImportError as exc:
        raise QmtMiniError("xtquant is not installed. Run: python -m pip install xtquant") from exc
    return xtdata


def _load_xttrader():
    try:
        from xtquant.xttrader import XtQuantTrader  # type: ignore
        from xtquant.xttype import StockAccount  # type: ignore
    except ImportError as exc:
        raise QmtMiniError("xtquant trading modules are not installed. Run: python -m pip install xtquant") from exc
    return XtQuantTrader, StockAccount


def _mask_account(account_id: str) -> str:
    text = str(account_id or "")
    if len(text) < 6:
        return "***"
    return f"{text[:3]}***{text[-3:]}"


def _object_to_dict(obj: Any, fields: Iterable[str]) -> dict[str, Any]:
    return {field: getattr(obj, field, None) for field in fields}


def _objects_to_dicts(items: Iterable[Any], fields: Iterable[str]) -> list[dict[str, Any]]:
    return [_object_to_dict(item, fields) for item in items or []]


@dataclass(frozen=True)
class QmtMiniConfig:
    qmt_root: Path = DEFAULT_QMT_ROOT
    userdata_dir: Path = DEFAULT_QMT_MINI_USERDATA
    quote_host: str = DEFAULT_QMT_QUOTE_HOST
    quote_port: int = DEFAULT_QMT_QUOTE_PORT
    account_type: str = "STOCK"

    @classmethod
    def from_env(cls) -> "QmtMiniConfig":
        root = Path(os.getenv("AISTOCK_QMT_ROOT", str(DEFAULT_QMT_ROOT)))
        userdata = Path(os.getenv("AISTOCK_QMT_USERDATA", str(root / "userdata_mini")))
        quote_host = os.getenv("AISTOCK_QMT_QUOTE_HOST", DEFAULT_QMT_QUOTE_HOST)
        quote_port = int(os.getenv("AISTOCK_QMT_QUOTE_PORT", str(DEFAULT_QMT_QUOTE_PORT)))
        account_type = os.getenv("AISTOCK_QMT_ACCOUNT_TYPE", "STOCK")
        return cls(qmt_root=root, userdata_dir=userdata, quote_host=quote_host, quote_port=quote_port, account_type=account_type)


class QmtMiniMarketClient:
    def __init__(self, config: Optional[QmtMiniConfig] = None):
        self.config = config or QmtMiniConfig.from_env()
        self._xtdata = None
        self._connected = False

    def connect(self) -> dict[str, Any]:
        xtdata = _load_xtdata()
        xtdata.connect(self.config.quote_host, self.config.quote_port)
        self._xtdata = xtdata
        self._connected = True
        return {
            "ok": True,
            "quote_host": self.config.quote_host,
            "quote_port": self.config.quote_port,
            "userdata_dir": str(self.config.userdata_dir),
        }

    def _ensure_connected(self):
        if not self._connected:
            self.connect()
        return self._xtdata

    def get_full_tick(self, stock_codes: list[str]) -> dict[str, Any]:
        xtdata = self._ensure_connected()
        return _call_xtdata("get_full_tick", lambda: xtdata.get_full_tick(stock_codes))

    def get_sector_list(self) -> list[str]:
        xtdata = self._ensure_connected()
        return list(_call_xtdata("get_sector_list", lambda: xtdata.get_sector_list()) or [])

    def get_stock_list_in_sector(self, sector_name: str, real_timetag: Any = -1) -> list[str]:
        xtdata = self._ensure_connected()
        return list(
            _call_xtdata(
                "get_stock_list_in_sector",
                lambda: xtdata.get_stock_list_in_sector(sector_name, real_timetag),
            )
            or []
        )

    def get_instrument_detail(self, stock_code: str, iscomplete: bool = False) -> dict[str, Any]:
        xtdata = self._ensure_connected()
        return dict(
            _call_xtdata(
                "get_instrument_detail",
                lambda: xtdata.get_instrument_detail(stock_code, iscomplete),
            )
            or {}
        )

    def get_instrument_detail_list(self, stock_list: list[str], iscomplete: bool = False) -> dict[str, Any]:
        xtdata = self._ensure_connected()
        return dict(
            _call_xtdata(
                "get_instrument_detail_list",
                lambda: xtdata.get_instrument_detail_list(stock_list, iscomplete),
            )
            or {}
        )

    def download_history_contracts(self, incrementally: bool = True) -> None:
        """Populate QMT's local expired/delisted-contract metadata cache."""
        xtdata = self._ensure_connected()
        _call_xtdata(
            "download_history_contracts",
            lambda: xtdata.download_history_contracts(incrementally=incrementally),
        )

    def download_history_data(
        self,
        stock_code: str,
        period: str,
        start_time: str = "",
        end_time: str = "",
    ) -> None:
        xtdata = self._ensure_connected()
        _call_xtdata(
            "download_history_data",
            lambda: xtdata.download_history_data(stock_code, period, start_time, end_time),
        )

    def download_history_data2(
        self,
        stock_list: list[str],
        period: str,
        start_time: str = "",
        end_time: str = "",
        callback: Optional[Any] = None,
        incrementally: Optional[bool] = None,
    ) -> Any:
        xtdata = self._ensure_connected()
        kwargs: dict[str, Any] = {}
        if callback is not None:
            kwargs["callback"] = callback
        if incrementally is not None:
            kwargs["incrementally"] = incrementally
        return _call_xtdata(
            "download_history_data2",
            lambda: xtdata.download_history_data2(stock_list, period, start_time, end_time, **kwargs),
        )

    def get_market_data(
        self,
        field_list: Optional[list[str]] = None,
        stock_list: Optional[list[str]] = None,
        period: str = "1d",
        start_time: str = "",
        end_time: str = "",
        count: int = -1,
        dividend_type: str = "none",
        fill_data: bool = False,
    ) -> dict[str, pd.DataFrame]:
        xtdata = self._ensure_connected()
        return _call_xtdata(
            "get_market_data",
            lambda: xtdata.get_market_data(
                field_list=field_list or [],
                stock_list=stock_list or [],
                period=period,
                start_time=start_time,
                end_time=end_time,
                count=count,
                dividend_type=dividend_type,
                fill_data=fill_data,
            ),
        )

    def get_market_data_tdx_shape(
        self,
        field_list: Optional[list[str]] = None,
        stock_list: Optional[list[str]] = None,
        period: str = "1d",
        start_time: str = "",
        end_time: str = "",
        count: int = -1,
        dividend_type: str = "none",
        fill_data: bool = False,
    ) -> dict[str, pd.DataFrame]:
        normalized_fields = [str(field).lower() for field in (field_list or [])]
        raw = self.get_market_data(
            field_list=normalized_fields,
            stock_list=stock_list,
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            fill_data=fill_data,
        )
        out: dict[str, pd.DataFrame] = {}
        key_map = {
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
            "amount": "Amount",
        }
        normalized_codes = {str(code).upper() for code in (stock_list or [])}
        for key, value in (raw or {}).items():
            if not isinstance(value, pd.DataFrame):
                continue
            frame = value.copy()
            if any(str(idx).upper() in normalized_codes for idx in frame.index):
                frame = frame.T
            out[key_map.get(str(key).lower(), str(key))] = frame
        return out

    def get_trading_dates(
        self,
        market: str = "SH",
        start_time: str = "",
        end_time: str = "",
        count: int = -1,
    ) -> list[Any]:
        xtdata = self._ensure_connected()
        return list(
            _call_xtdata(
                "get_trading_dates",
                lambda: xtdata.get_trading_dates(market, start_time, end_time, count),
            )
            or []
        )

    def get_market_last_trade_date(self, market: str = "SH") -> Any:
        xtdata = self._ensure_connected()
        return _call_xtdata(
            "get_market_last_trade_date",
            lambda: xtdata.get_market_last_trade_date(market),
        )

    def probe_capabilities(self, stock_code: str = "600519.SH") -> dict[str, Any]:
        result: dict[str, Any] = {"ok": True, "stock_code": stock_code, "capabilities": {}}

        def record(name: str, callback) -> None:
            try:
                value = callback()
                item: dict[str, Any] = {"ok": True, "type": type(value).__name__}
                try:
                    item["length"] = len(value)
                except Exception:
                    pass
                result["capabilities"][name] = item
            except Exception as exc:
                result["ok"] = False
                item = {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                if isinstance(exc, QmtMiniCapabilityError):
                    item["missing_handler"] = exc.function_name
                result["capabilities"][name] = item

        record("full_tick", lambda: self.get_full_tick([stock_code]))
        record("instrument_detail", lambda: self.get_instrument_detail(stock_code))
        record("sector_list", self.get_sector_list)
        record("trading_dates", lambda: self.get_trading_dates("SH", "", "", 5))
        record("market_last_trade_date", lambda: self.get_market_last_trade_date("SH"))
        record(
            "market_data_1d",
            lambda: self.get_market_data_tdx_shape(
                field_list=["open", "high", "low", "close", "volume", "amount"],
                stock_list=[stock_code],
                period="1d",
                count=5,
                dividend_type="none",
                fill_data=False,
            ),
        )
        return result

    def history_summary(self, stock_code: str, periods: list[str], start_time: str, end_time: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for period in periods:
            self.download_history_data(stock_code, period, start_time, end_time)
            data = self.get_market_data(
                field_list=["open", "close", "volume"],
                stock_list=[stock_code],
                period=period,
                start_time=start_time,
                end_time=end_time,
                fill_data=False,
            )
            close_df = data.get("close")
            result[period] = {
                "ok": isinstance(close_df, pd.DataFrame) and not close_df.empty and close_df.shape[1] > 0,
                "bars": int(close_df.shape[1]) if isinstance(close_df, pd.DataFrame) else 0,
            }
        return result


class QmtMiniTradingClient:
    def __init__(self, config: Optional[QmtMiniConfig] = None, session_id: Optional[int] = None):
        self.config = config or QmtMiniConfig.from_env()
        self.session_id = int(session_id if session_id is not None else time.time()) % 100000000
        self._trader = None
        self._bridge_snapshot: dict[str, Any] | None = None

    def _load_bridge_snapshot(self) -> dict[str, Any]:
        if not DEFAULT_QMT_ACCOUNT_BRIDGE_URL:
            raise QmtMiniError("QMT account bridge is not configured")
        try:
            with urlopen(DEFAULT_QMT_ACCOUNT_BRIDGE_URL, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise QmtMiniError(f"QMT account bridge request failed: {exc}") from exc
        snapshot = payload.get("snapshot") if isinstance(payload, dict) else None
        if not isinstance(snapshot, dict) or not payload.get("ok") or not snapshot.get("ok"):
            message = payload.get("message") if isinstance(payload, dict) else "invalid response"
            raise QmtMiniError(f"QMT account bridge returned no usable snapshot: {message}")
        return snapshot

    def connect(self) -> dict[str, Any]:
        try:
            XtQuantTrader, _ = _load_xttrader()
        except QmtMiniError:
            self._bridge_snapshot = self._load_bridge_snapshot()
            return {"ok": True, "mode": "read_only_account_bridge", "session_id": self.session_id}
        if not self.config.userdata_dir.exists():
            raise QmtMiniError(f"QMT Mini userdata directory not found: {self.config.userdata_dir}")
        trader = XtQuantTrader(str(self.config.userdata_dir), self.session_id)
        trader.start()
        ret = trader.connect()
        self._trader = trader
        return {
            "ok": ret == 0,
            "connect_ret": ret,
            "userdata_dir": str(self.config.userdata_dir),
            "session_id": self.session_id,
        }

    def close(self) -> None:
        if self._trader is not None:
            try:
                self._trader.stop()
            finally:
                self._trader = None

    def __enter__(self) -> "QmtMiniTradingClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_trader(self):
        if self._bridge_snapshot is not None:
            raise QmtMiniError("QMT account bridge is read-only and cannot submit orders")
        if self._trader is None:
            status = self.connect()
            if not status["ok"]:
                raise QmtMiniError(f"QMT Mini trading connect failed: {status}")
            if self._bridge_snapshot is not None:
                raise QmtMiniError("QMT account bridge is read-only and cannot submit orders")
        return self._trader

    def query_account_infos(self, masked: bool = True) -> list[dict[str, Any]]:
        if self._bridge_snapshot is not None:
            account_id = str(self._bridge_snapshot.get("account_id") or "")
            return [{"account_id": _mask_account(account_id) if masked else account_id, "account_type": None}] if account_id else []
        trader = self._ensure_trader()
        infos = trader.query_account_infos() or []
        rows: list[dict[str, Any]] = []
        for item in infos:
            account_id = str(getattr(item, "account_id", "") or "")
            rows.append(
                {
                    "account_id": _mask_account(account_id) if masked else account_id,
                    "account_type": getattr(item, "account_type", None),
                }
            )
        return rows

    def _first_account_id(self) -> str:
        infos = self.query_account_infos(masked=False)
        if not infos:
            raise QmtMiniError("No QMT Mini trading account is available")
        return str(infos[0]["account_id"])

    def _stock_account(self, account_id: Optional[str] = None):
        _, StockAccount = _load_xttrader()
        return StockAccount(account_id or self._first_account_id(), self.config.account_type)

    def account_snapshot(self, include_sensitive: bool = False) -> dict[str, Any]:
        if self._bridge_snapshot is not None:
            snapshot = dict(self._bridge_snapshot)
            if not include_sensitive:
                snapshot["account_id"] = _mask_account(str(snapshot.get("account_id") or ""))
                snapshot.pop("asset", None)
                snapshot.pop("positions", None)
                snapshot.pop("orders", None)
                snapshot.pop("trades", None)
            return snapshot
        trader = self._ensure_trader()
        account_id = self._first_account_id()
        account = self._stock_account(account_id)
        asset = trader.query_stock_asset(account)
        positions = trader.query_stock_positions(account) or []
        orders = trader.query_stock_orders(account) or []
        trades = trader.query_stock_trades(account) or []
        data: dict[str, Any] = {
            "ok": asset is not None,
            "account_id": account_id if include_sensitive else _mask_account(account_id),
            "asset_available": asset is not None,
            "positions_count": len(positions),
            "orders_count": len(orders),
            "trades_count": len(trades),
        }
        if include_sensitive and asset is not None:
            data["asset"] = _object_to_dict(
                asset,
                ["cash", "total_asset", "market_value", "frozen_cash", "account_id", "account_type"],
            )
            data["positions"] = _objects_to_dicts(
                positions,
                [
                    "stock_code",
                    "stock_name",
                    "volume",
                    "can_use_volume",
                    "open_price",
                    "avg_price",
                    "cost_price",
                    "last_price",
                    "market_value",
                    "position_cost",
                    "frozen_volume",
                    "on_road_volume",
                    "yesterday_volume",
                    "direction",
                    "account_id",
                    "account_type",
                ],
            )
            data["orders"] = _objects_to_dicts(
                orders,
                ["stock_code", "order_id", "order_sysid", "order_status", "order_type", "price", "volume", "traded_volume"],
            )
            data["trades"] = _objects_to_dicts(
                trades,
                ["stock_code", "traded_id", "traded_time", "traded_price", "traded_volume", "traded_amount", "order_type"],
            )
        return data

    def submit_stock_order(
        self,
        stock_code: str,
        order_type: int,
        volume: int,
        price_type: int,
        price: float = 0.0,
        strategy_name: str = "AiStockG3",
        order_remark: str = "",
        account_id: Optional[str] = None,
    ) -> int:
        """Submit one QMT stock order and return QMT's order id.

        Callers must enforce trading/risk/idempotency policy before this method.
        """
        trader = self._ensure_trader()
        account = self._stock_account(account_id)
        return int(
            trader.order_stock(
                account,
                str(stock_code).upper(),
                int(order_type),
                int(volume),
                int(price_type),
                float(price),
                str(strategy_name),
                str(order_remark),
            )
        )
