from __future__ import annotations

from typing import Any, Optional

import pandas as pd
import requests


class TdxGatewayClient:
    def __init__(self, base_url: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _decode(self, value: Any) -> Any:
        if isinstance(value, dict) and value.get("__type__") == "dataframe":
            return pd.DataFrame(
                value.get("data") or [],
                index=value.get("index") or [],
                columns=value.get("columns") or [],
            )
        if isinstance(value, dict) and value.get("__type__") == "series":
            return pd.Series(value.get("data") or [], index=value.get("index") or [])
        if isinstance(value, dict):
            return {key: self._decode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._decode(item) for item in value]
        return value

    def _request(self, method: str, path: str, payload: Optional[dict[str, Any]] = None) -> Any:
        response = requests.request(
            method,
            f"{self.base_url}{path}",
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("error") or f"TDX gateway request failed: {path}")
        return self._decode(body.get("data"))

    def health(self) -> dict[str, Any]:
        response = requests.get(f"{self.base_url}/health", timeout=min(self.timeout, 10))
        response.raise_for_status()
        return response.json()

    def initialize(self) -> bool:
        response = requests.post(f"{self.base_url}/initialize", timeout=self.timeout)
        response.raise_for_status()
        return bool(response.json().get("ok"))

    def get_stock_list(self, market: str = "ALL", stock_type: str = "stock", list_type: int = 1) -> Optional[list]:
        return self._request("POST", "/stock-list", {
            "market": market,
            "stock_type": stock_type,
            "list_type": list_type,
        })

    def get_stock_info(self, stock_code: str) -> Optional[dict[str, Any]]:
        return self._request("GET", f"/stock-info/{stock_code}")

    def get_realtime_quotes(self, stock_codes: list) -> Optional[list]:
        return self._request("POST", "/realtime-quotes", {"stock_codes": stock_codes})

    def get_market_data(
        self,
        field_list: list,
        stock_list: list,
        period: str,
        start_time: str = None,
        end_time: str = None,
        count: int = -1,
        dividend_type: str = "none",
        fill_data: bool = True,
    ) -> Optional[dict[str, Any]]:
        return self._request("POST", "/market-data", {
            "field_list": field_list,
            "stock_list": stock_list,
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
            "count": count,
            "dividend_type": dividend_type,
            "fill_data": fill_data,
        })

    def refresh_cache(self, force: bool = False, market: str = "") -> Any:
        return self._request("POST", "/refresh-cache", {"force": force, "market": market})

    def refresh_kline(self, stock_list: list, period: str) -> Any:
        return self._request("POST", "/refresh-kline", {"stock_list": stock_list, "period": period})

    def get_gb_info(self, stock_code: str, date_list: list, count: int = -1) -> Optional[list]:
        return self._request("POST", "/gb-info", {
            "stock_code": stock_code,
            "date_list": date_list,
            "count": count,
        })

    def get_gb_info_by_date(self, stock_code: str, start_date: str, end_date: str) -> Optional[list]:
        return self._request("POST", "/gb-info-by-date", {
            "stock_code": stock_code,
            "start_date": start_date,
            "end_date": end_date,
        })

    def get_financial_data(
        self,
        stock_code: str,
        report_type: int = 1,
        report_period: str = None,
    ) -> Optional[dict[str, Any]]:
        path = f"/financial-data/{stock_code}?report_type={report_type}"
        if report_period:
            path += f"&report_period={report_period}"
        return self._request("GET", path)

    def get_sector_data(self, sector_code: str = None) -> Any:
        path = "/sector-data"
        if sector_code:
            path += f"?sector_code={sector_code}"
        return self._request("GET", path)

    def get_sector_list(self) -> Optional[list[dict[str, Any]]]:
        return self._request("GET", "/sector-list")

    def get_stock_list_in_sector(self, sector_code: str) -> Optional[list[dict[str, Any]]]:
        return self._request("GET", f"/sector/{sector_code}/stocks")
