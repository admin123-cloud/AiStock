"""
TdxQuant data source wrapper.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from ..base_fetcher import BaseDataSource
from core.base import DataSourceStatus
from utils.logger import get_logger
from .tdxquant_pool import tdxquant_pool

logger = get_logger("TdxQuantDataSource")


class TdxQuantDataSource(BaseDataSource):
    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        self._initialized = True
        self.status = tdxquant_pool.get_status()
        logger.info(f"{self.name} initialized (pool mode), status={self.status}")

    def _initialize(self):
        return True

    def _ensure_initialized(self):
        return True

    def get_market_data(
        self,
        field_list: list,
        stock_list: list,
        period: str,
        start_time: str = None,
        end_time: str = None,
        count: int = -1,
        dividend_type: str = 'none',
        fill_data: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        获取市场数据（透传给连接池）

        Args:
            field_list: 字段列表
            stock_list: 股票代码列表
            period: 周期 ('1d', '1w', '1mo', '1m', '5m', '15m', '30m', '60m')
            start_time: 开始时间
            end_time: 结束时间
            count: 数据条数，-1 表示全部
            dividend_type: 复权类型 ('none', 'front', 'back')
            fill_data: 是否填充

        Returns:
            市场数据字典
        """
        try:
            # 兼容旧版 period 命名（daily/weekly/monthly → 1d/1w/1mo）
            _period_map = {
                "daily": "1d", "weekly": "1w", "monthly": "1mo",
                "quarterly": "1q", "yearly": "1y",
            }
            mapped_period = _period_map.get(period, period)
            return tdxquant_pool.get_market_data(
                field_list=field_list, stock_list=stock_list,
                period=mapped_period, start_time=start_time,
                end_time=end_time, count=count,
                dividend_type=dividend_type, fill_data=fill_data,
            )
        except Exception as e:
            logger.error(f"{self.name} get_market_data failed: {e}")
            self.failure_count += 1
            self.last_failure_time = datetime.now()
            self.last_error_message = str(e)
            return None

    def close(self):
        try:
            tdxquant_pool.close()
            self.status = DataSourceStatus.UNAVAILABLE
            logger.info(f"{self.name} connection closed")
        except Exception as e:
            logger.error(f"{self.name} close failed: {e}")

    def get_stock_quote(self, stock_code: str) -> Optional[Dict[str, Any]]:
        try:
            if not self._ensure_initialized():
                logger.warning(f"{self.name} SDK not initialized, cannot fetch quote: {stock_code}")
                return None

            result = tdxquant_pool.get_realtime_quotes([stock_code])
            if not result:
                market_data = tdxquant_pool.get_market_data(
                    field_list=[],
                    stock_list=[stock_code],
                    period="1d",
                    count=1,
                    dividend_type="none",
                    fill_data=False,
                )
                if not market_data or "Close" not in market_data:
                    logger.warning(f"{self.name} quote failed: no data ({stock_code})")
                    return None
                close_df = market_data["Close"]
                if close_df is None or close_df.empty:
                    return None
                latest_date = close_df.index[0]
                return {
                    "code": stock_code,
                    "name": "",
                    "price": float(market_data["Close"].iloc[0, 0]),
                    "open": float(market_data["Open"].iloc[0, 0]),
                    "high": float(market_data["High"].iloc[0, 0]),
                    "low": float(market_data["Low"].iloc[0, 0]),
                    "pre_close": 0.0,
                    "volume": float(market_data["Volume"].iloc[0, 0]),
                    "amount": float(market_data["Amount"].iloc[0, 0]),
                    "change": 0.0,
                    "change_pct": 0.0,
                    "time": str(latest_date),
                }

            quote = result[0]
            return {
                "code": stock_code,
                "name": quote.get("Name", ""),
                "price": float(quote.get("Price", 0) or 0),
                "open": float(quote.get("Open", 0) or 0),
                "high": float(quote.get("High", 0) or 0),
                "low": float(quote.get("Low", 0) or 0),
                "pre_close": float(quote.get("PreClose", 0) or 0),
                "volume": float(quote.get("Volume", 0) or 0),
                "amount": float(quote.get("Amount", 0) or 0),
                "change": float(quote.get("Change", 0) or 0),
                "change_pct": float(quote.get("ChangePercent", 0) or 0),
                "time": quote.get("Time", str(datetime.now())),
            }
        except Exception as e:
            logger.error(f"{self.name} get quote failed ({stock_code}): {e}")
            self.failure_count += 1
            self.last_failure_time = datetime.now()
            self.last_error_message = str(e)
            return None

    def get_stock_history(
        self,
        stock_code: str,
        start_date: str,
        end_date: str,
        period: str = "daily",
        dividend_type: str = "none",
    ) -> Optional[pd.DataFrame]:
        try:
            if not self._ensure_initialized():
                logger.warning(f"{self.name} SDK not initialized, cannot fetch history: {stock_code}")
                return None

            period_map = {
                "1m": "1m",
                "5m": "5m",
                "15m": "15m",
                "30m": "30m",
                "1h": "1h",
                "60m": "1h",
                "1d": "1d",
                "1w": "1w",
                "1mon": "1mon",
                "1q": "1q",
                "1y": "1y",
            }
            tdx_period = period_map.get(period, "1d")
            minute_periods = {"1m", "5m", "15m", "30m", "60m", "1h"}
            derived_from_5m_periods = {"15m", "30m", "60m", "1h"}

            start_time = (start_date or "").replace("-", "")
            end_time = (end_date or "").replace("-", "")
            same_day_query = bool(start_date and end_date and start_date == end_date)
            same_day_minute_query = same_day_query and period in minute_periods
            request_count = -1 if same_day_minute_query else (1 if same_day_query else -1)
            request_fill_data = True if same_day_minute_query else (False if same_day_query else True)

            def _last_completed_intraday_bar_minutes(delay_seconds: int = 120) -> int:
                effective_dt = datetime.fromtimestamp(datetime.now().timestamp() - max(0, int(delay_seconds)))
                minute_of_day = effective_dt.hour * 60 + effective_dt.minute
                morning_open = 9 * 60 + 30
                morning_close = 11 * 60 + 30
                afternoon_open = 13 * 60
                afternoon_close = 15 * 60
                if minute_of_day <= morning_open:
                    return 0
                if minute_of_day <= morning_close:
                    return minute_of_day - morning_open
                if minute_of_day <= afternoon_open:
                    return 120
                if minute_of_day <= afternoon_close:
                    return 120 + (minute_of_day - afternoon_open)
                return 240

            def _same_day_minute_ready(req_period: str) -> bool:
                period_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1h": 60}
                required = period_minutes.get(req_period, 999)
                return _last_completed_intraday_bar_minutes() >= required

            if same_day_minute_query and not _same_day_minute_ready(period):
                logger.debug(
                    f"{self.name} skip same-day minute before confirmed bar "
                    f"(code={stock_code}, period={period}, date={start_date})"
                )
                return None

            def _query_market_data(req_start: str, req_end: str, req_count: int, req_fill: bool):
                return tdxquant_pool.get_market_data(
                    field_list=[],
                    stock_list=[stock_code],
                    period=tdx_period,
                    start_time=req_start,
                    end_time=req_end,
                    count=req_count,
                    dividend_type=dividend_type,
                    fill_data=req_fill,
                )

            def _has_close(res: Optional[Dict[str, Any]]) -> bool:
                if not res or "Close" not in res:
                    return False
                close_obj = res.get("Close")
                return close_obj is not None and not close_obj.empty and close_obj.shape[1] > 0

            def _result_to_dataframe(res: Dict[str, Any]) -> pd.DataFrame:
                close_df = res["Close"]
                return pd.DataFrame(
                    {
                        "date": close_df.index,
                        "open": res["Open"].iloc[:, 0].values,
                        "high": res["High"].iloc[:, 0].values,
                        "low": res["Low"].iloc[:, 0].values,
                        "close": res["Close"].iloc[:, 0].values,
                        "volume": res["Volume"].iloc[:, 0].values,
                        "amount": res["Amount"].iloc[:, 0].values,
                    }
                ).sort_values("date").reset_index(drop=True)

            def _filter_target_date(data: pd.DataFrame, target_date: str) -> pd.DataFrame:
                if data.empty:
                    return data
                date_values = pd.to_datetime(data["date"], errors="coerce")
                exact_mask = date_values.dt.strftime("%Y-%m-%d") == target_date
                return data.loc[exact_mask].reset_index(drop=True)

            def _filter_market_minutes(data: pd.DataFrame) -> pd.DataFrame:
                if data.empty:
                    return data
                ts = pd.to_datetime(data["date"], errors="coerce")
                mins = ts.dt.hour * 60 + ts.dt.minute
                mask = ((mins > 9 * 60 + 30) & (mins <= 11 * 60 + 30)) | (
                    (mins > 13 * 60) & (mins <= 15 * 60)
                )
                return data.loc[mask].reset_index(drop=True)

            def _aggregate_5m_to_period(data_5m: pd.DataFrame, target_period: str) -> pd.DataFrame:
                if data_5m.empty:
                    return data_5m
                group_size = {"15m": 3, "30m": 6, "60m": 12, "1h": 12}.get(target_period)
                if not group_size:
                    return data_5m
                data_5m = _filter_market_minutes(data_5m.copy())
                data_5m["date"] = pd.to_datetime(data_5m["date"], errors="coerce")
                data_5m = data_5m.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
                rows = []
                for _, day_df in data_5m.groupby(data_5m["date"].dt.strftime("%Y-%m-%d")):
                    day_df = day_df.reset_index(drop=True)
                    for start_idx in range(0, len(day_df), group_size):
                        chunk = day_df.iloc[start_idx:start_idx + group_size]
                        if len(chunk) < group_size:
                            continue
                        rows.append(
                            {
                                "date": chunk.iloc[-1]["date"],
                                "open": float(chunk.iloc[0]["open"]),
                                "high": float(chunk["high"].max()),
                                "low": float(chunk["low"].min()),
                                "close": float(chunk.iloc[-1]["close"]),
                                "volume": float(pd.to_numeric(chunk["volume"], errors="coerce").fillna(0).sum()),
                                "amount": float(pd.to_numeric(chunk["amount"], errors="coerce").fillna(0).sum()),
                            }
                        )
                return pd.DataFrame(rows).sort_values("date").reset_index(drop=True) if rows else pd.DataFrame()

            def _akshare_minute_fallback(req_start: str, req_end: str, req_period: str) -> Optional[pd.DataFrame]:
                if req_period not in {"5m", "15m", "30m", "60m", "1h"}:
                    return None
                try:
                    from .akshare_minute_fallback import fetch_recent_minute

                    fallback_df = fetch_recent_minute(
                        code=stock_code,
                        start_date=str(req_start)[:10],
                        end_date=str(req_end)[:10],
                        period=req_period,
                    )
                    if fallback_df is not None and not fallback_df.empty:
                        logger.info(
                            f"{self.name} filled minute data from AkShare/Sina fallback "
                            f"(code={stock_code}, period={req_period}, rows={len(fallback_df)})"
                        )
                        return fallback_df
                except Exception as exc:
                    logger.warning(
                        f"{self.name} AkShare/Sina minute fallback exception "
                        f"(code={stock_code}, period={req_period}, start={req_start}, end={req_end}): {exc}"
                    )
                return None

            def _tdx_refresh_markets_for_code() -> List[str]:
                text = str(stock_code or "").strip().upper()
                raw = text.split(".", 1)[0]
                index_prefixes = ("399", "000", "880", "881", "882", "883", "884", "885", "886", "887", "888", "889")
                known_index_codes = {"999999", "000001", "000300", "000852", "000905", "000906", "000016", "000010"}
                if raw in known_index_codes or raw.startswith(index_prefixes):
                    # ZS covers exchange indices; ZZ covers CSI/CNI and similar indices.
                    return ["ZS", "ZZ"]
                return ["AG"]

            def _refresh_tdx_minute_cache(refresh_period: str) -> None:
                markets = _tdx_refresh_markets_for_code()
                for market in markets:
                    try:
                        tdxquant_pool.refresh_cache(force=True, market=market)
                    except Exception as exc:
                        logger.warning(
                            f"{self.name} refresh_cache failed "
                            f"(code={stock_code}, period={refresh_period}, market={market}): {exc}"
                        )
                try:
                    tdxquant_pool.refresh_kline([stock_code], refresh_period)
                except Exception as exc:
                    logger.warning(f"{self.name} refresh_kline failed ({stock_code}, {refresh_period}): {exc}")

            def _refresh_and_get_5m(target_date: str) -> Optional[pd.DataFrame]:
                _refresh_tdx_minute_cache("5m")
                ymd = target_date.replace("-", "")
                base_result = _query_market_data(ymd, ymd, -1, True)
                if not _has_close(base_result):
                    return None
                base_df = _filter_target_date(_result_to_dataframe(base_result), target_date)
                return base_df if not base_df.empty else None

            def _same_day_minute_fallback(target_date: str, req_period: str) -> Optional[pd.DataFrame]:
                if req_period not in {"5m", "15m", "30m", "60m", "1h"}:
                    return None
                return _akshare_minute_fallback(target_date, target_date, req_period)

            result = _query_market_data(start_time, end_time, request_count, request_fill_data)

            if same_day_minute_query and period in {"5m", "15m", "30m", "60m", "1h"}:
                if _has_close(result):
                    data = _filter_target_date(_result_to_dataframe(result), str(start_date)[:10])
                    if data is not None and not data.empty:
                        return data
                fallback_df = _same_day_minute_fallback(str(start_date)[:10], period)
                if fallback_df is not None and not fallback_df.empty:
                    return fallback_df
                logger.info(
                    f"{self.name} skip legacy same-day TDX minute refresh path "
                    f"(code={stock_code}, period={period}, date={start_date}); "
                    f"use downloaded TDX data after close or global snapshot collector instead"
                )
                return None

            if same_day_minute_query and not _has_close(result):
                logger.warning(
                    f"{self.name} same-day minute query returned no exact data "
                    f"(code={stock_code}, period={period}, date={start_date})"
                )
                fallback_df = _same_day_minute_fallback(str(start_date)[:10], period)
                if fallback_df is not None and not fallback_df.empty:
                    return fallback_df
                if period in {"1m", "5m"}:
                    _refresh_tdx_minute_cache(period)
                    result = _query_market_data(start_time, end_time, request_count, request_fill_data)
                elif period in derived_from_5m_periods:
                    base_df = _refresh_and_get_5m(str(start_date)[:10])
                    if base_df is not None and not base_df.empty:
                        derived_df = _aggregate_5m_to_period(base_df, period)
                        if derived_df is not None and not derived_df.empty:
                            logger.info(
                                f"{self.name} derived {stock_code} {period} from refreshed 5m: {len(derived_df)} rows"
                            )
                            return derived_df

            if not _has_close(result):
                target_date_for_refresh = str(end_date)[:10] if end_date else datetime.now().strftime("%Y-%m-%d")
                if target_date_for_refresh == datetime.now().strftime("%Y-%m-%d"):
                    fallback_df = _same_day_minute_fallback(target_date_for_refresh, period)
                    if fallback_df is not None and not fallback_df.empty:
                        return fallback_df
                if period in {"1m", "5m"} and target_date_for_refresh == datetime.now().strftime("%Y-%m-%d"):
                    _refresh_tdx_minute_cache(period)
                    ymd = target_date_for_refresh.replace("-", "")
                    result = _query_market_data(ymd, ymd, -1, True)
                elif period in derived_from_5m_periods and target_date_for_refresh == datetime.now().strftime("%Y-%m-%d"):
                    base_df = _refresh_and_get_5m(target_date_for_refresh)
                    if base_df is not None and not base_df.empty:
                        derived_df = _aggregate_5m_to_period(base_df, period)
                        if derived_df is not None and not derived_df.empty:
                            logger.info(
                                f"{self.name} derived {stock_code} {period} from refreshed 5m after empty result: "
                                f"{len(derived_df)} rows"
                            )
                            return derived_df
                if period in {"5m", "15m", "30m", "60m", "1h"}:
                    fallback_df = _akshare_minute_fallback(start_date, end_date, period)
                    if fallback_df is not None and not fallback_df.empty:
                        return fallback_df

            if not _has_close(result):
                logger.warning(
                    f"{self.name} get history failed: missing Close "
                    f"(code={stock_code}, period={period}, tdx_period={tdx_period}, "
                    f"start={start_date}, end={end_date}, count={request_count}, fill_data={request_fill_data}, "
                    f"keys={list(result.keys()) if isinstance(result, dict) else None}, "
                    f"api_error={(result.get('error') if isinstance(result, dict) else None)}, "
                    f"api_msg={(result.get('msg') if isinstance(result, dict) else None)})"
                )
                return None

            data = _result_to_dataframe(result)
            if "ForwardFactor" in result:
                data["forward_factor"] = result["ForwardFactor"].iloc[:, 0].values

            if period in minute_periods and end_date:
                target_date_for_refresh = str(end_date)[:10]
                today_text = datetime.now().strftime("%Y-%m-%d")
                if target_date_for_refresh == today_text and _filter_target_date(data, target_date_for_refresh).empty:
                    refreshed_df = None
                    fallback_df = _same_day_minute_fallback(target_date_for_refresh, period)
                    if fallback_df is not None and not fallback_df.empty:
                        refreshed_df = fallback_df
                    if refreshed_df is None and period in {"1m", "5m"}:
                        _refresh_tdx_minute_cache(period)
                        ymd = target_date_for_refresh.replace("-", "")
                        refreshed_result = _query_market_data(ymd, ymd, -1, True)
                        if _has_close(refreshed_result):
                            refreshed_df = _filter_target_date(_result_to_dataframe(refreshed_result), target_date_for_refresh)
                    elif refreshed_df is None and period in derived_from_5m_periods:
                        base_df = _refresh_and_get_5m(target_date_for_refresh)
                        if base_df is not None and not base_df.empty:
                            refreshed_df = _aggregate_5m_to_period(base_df, period)

                    if refreshed_df is not None and not refreshed_df.empty:
                        old_data = data
                        old_dates = pd.to_datetime(old_data["date"], errors="coerce")
                        old_data = old_data.loc[
                            old_dates.dt.strftime("%Y-%m-%d") != target_date_for_refresh
                        ].reset_index(drop=True)
                        data = pd.concat([old_data, refreshed_df], ignore_index=True).sort_values("date").reset_index(drop=True)
                        logger.info(
                            f"{self.name} filled today's {period} from refresh path "
                            f"(code={stock_code}, date={target_date_for_refresh}, rows={len(refreshed_df)})"
                        )

            if same_day_minute_query:
                date_values = pd.to_datetime(data["date"], errors="coerce")
                target_date = str(start_date)[:10]
                exact_mask = date_values.dt.strftime("%Y-%m-%d") == target_date
                if not bool(exact_mask.any()):
                    if period in derived_from_5m_periods:
                        base_df = _refresh_and_get_5m(target_date)
                        if base_df is not None and not base_df.empty:
                            derived_df = _aggregate_5m_to_period(base_df, period)
                            if derived_df is not None and not derived_df.empty:
                                logger.info(
                                    f"{self.name} derived {stock_code} {period} from refreshed 5m after off-date result: "
                                    f"{len(derived_df)} rows"
                                )
                                return derived_df
                    if period in {"5m", "15m", "30m", "60m", "1h"}:
                        fallback_df = _akshare_minute_fallback(target_date, target_date, period)
                        if fallback_df is not None and not fallback_df.empty:
                            return fallback_df
                    first_date = str(data["date"].iloc[0]) if len(data) else None
                    last_date = str(data["date"].iloc[-1]) if len(data) else None
                    logger.warning(
                        f"{self.name} rejected off-date minute data "
                        f"(code={stock_code}, period={period}, target={target_date}, "
                        f"first={first_date}, last={last_date}, rows={len(data)})"
                    )
                    return None
                dropped = len(data) - int(exact_mask.sum())
                if dropped > 0:
                    logger.warning(
                        f"{self.name} dropped off-date minute rows "
                        f"(code={stock_code}, period={period}, target={target_date}, dropped={dropped})"
                    )
                    data = data.loc[exact_mask].reset_index(drop=True)

            # 校验 date 列：SDK 对停牌/退市股票可能返回 NaT 或空索引
            null_date_count = data["date"].isna().sum()
            if null_date_count > 0:
                total = len(data)
                if null_date_count == total:
                    # 全部为空：该股票在请求日期范围内无交易数据（停牌或退市）
                    raise ValueError(
                        f"{stock_code} 在 {start_date}~{end_date} 无 {period} 交易数据（停牌/退市/通达信未收录）"
                    )
                else:
                    # 部分为空：数据异常
                    raise ValueError(
                        f"{stock_code} {period} 数据中有 {null_date_count}/{total} 行 date 为空，"
                        f"请检查通达信本地数据完整性（start={start_date}, end={end_date}）"
                    )

            logger.info(f"{self.name} fetched {stock_code} {period}: {len(data)} rows")
            return data
        except Exception as e:
            logger.error(f"{self.name} get history failed ({stock_code}): {e}")
            self.failure_count += 1
            self.last_failure_time = datetime.now()
            self.last_error_message = str(e)
            return None

    def get_stock_info(self, stock_code: str) -> Optional[Dict[str, Any]]:
        try:
            market = ""
            if "." in stock_code:
                market = stock_code.split(".")[-1].upper()

            result = tdxquant_pool.get_stock_info(stock_code)
            if not result or result.get("ErrorId") != "0":
                logger.warning(f"{self.name} get info failed ({stock_code}): {result}")
                return None

            return {
                "code": stock_code,
                "name": result.get("Name", ""),
                "unit": result.get("Unit", ""),
                "market": market,
                "type": "stock",
                "region": result.get("tdx_dyname", ""),
                "industry": result.get("rs_hyname", ""),
                "industry_code": result.get("blockzscode", 0),
                "st": result.get("IsSTGP", "0") == "1",
                "quit": result.get("IsQuitGP", "0") == "1",
                "list_date": result.get("J_start", ""),
                "float_share": result.get("ActiveCapital", ""),
                "total_share": result.get("J_zgb", ""),
            }
        except Exception as e:
            logger.error(f"{self.name} get info failed ({stock_code}): {e}")
            self.failure_count += 1
            self.last_failure_time = datetime.now()
            self.last_error_message = str(e)
            return None

    def get_stock_list(self, market: str = "ALL", stock_type: str = "stock", list_type: int = 1) -> Optional[List[Dict[str, Any]]]:
        try:
            stock_list = tdxquant_pool.get_stock_list(market=market, stock_type=stock_type, list_type=list_type)
            if not stock_list:
                logger.warning(f"{self.name} get stock list failed: no data")
                return []

            result: List[Dict[str, Any]] = []
            for stock in stock_list:
                code = stock.get("Code") or stock.get("code", "")
                name = stock.get("Name") or stock.get("name", "")
                stock_market = code.split(".")[-1].upper() if "." in code else ""
                if code and name:
                    result.append({"code": code, "name": name, "market": stock_market, "type": "stock"})
            logger.info(f"{self.name} get stock list success: {len(result)}")
            return result
        except Exception as e:
            logger.error(f"{self.name} get stock list failed: {e}")
            self.failure_count += 1
            self.last_failure_time = datetime.now()
            self.last_error_message = str(e)
            return None

    def get_realtime_quotes(self, stock_codes: List[str]) -> Optional[List[Dict[str, Any]]]:
        try:
            if not self._ensure_initialized():
                logger.warning(f"{self.name} SDK not initialized, cannot fetch realtime quotes")
                return None
            if not stock_codes:
                return []

            result = tdxquant_pool.get_realtime_quotes(stock_codes)
            if not result:
                logger.warning(f"{self.name} realtime quotes empty: {len(stock_codes)} codes")
                return []

            quote_list: List[Dict[str, Any]] = []
            for quote in result:
                code = quote.get("Code", "")
                quote_list.append(
                    {
                        "code": code,
                        "name": quote.get("Name", ""),
                        "price": float(quote.get("Price", 0) or 0),
                        "open": float(quote.get("Open", 0) or 0),
                        "high": float(quote.get("High", 0) or 0),
                        "low": float(quote.get("Low", 0) or 0),
                        "pre_close": float(quote.get("PreClose", 0) or 0),
                        "volume": float(quote.get("Volume", 0) or 0),
                        "amount": float(quote.get("Amount", 0) or 0),
                        "change": float(quote.get("Change", 0) or 0),
                        "change_pct": float(quote.get("ChangePercent", 0) or 0),
                        "time": quote.get("Time", str(datetime.now())),
                    }
                )
            return quote_list
        except Exception as e:
            logger.error(f"{self.name} get realtime quotes failed: {e}")
            self.failure_count += 1
            self.last_failure_time = datetime.now()
            self.last_error_message = str(e)
            return None

    def get_sector_data(self, sector_code: str = None) -> Optional[Any]:
        try:
            return tdxquant_pool.get_sector_data(sector_code)
        except Exception as e:
            logger.error(f"{self.name} get sector data failed: {e}")
            self.failure_count += 1
            self.last_failure_time = datetime.now()
            self.last_error_message = str(e)
            return None

    def get_sector_list(self) -> Optional[List[Dict[str, Any]]]:
        try:
            return tdxquant_pool.get_sector_list()
        except Exception as e:
            logger.error(f"{self.name} get sector list failed: {e}")
            self.failure_count += 1
            self.last_failure_time = datetime.now()
            self.last_error_message = str(e)
            return None

    def get_financial_data(self, stock_code: str, report_type: int = 1, report_period: str = None) -> Optional[Dict[str, Any]]:
        try:
            return tdxquant_pool.get_financial_data(stock_code, report_type, report_period)
        except Exception as e:
            logger.error(f"{self.name} get financial data failed ({stock_code}): {e}")
            self.failure_count += 1
            self.last_failure_time = datetime.now()
            self.last_error_message = str(e)
            return None

    def health_check(self) -> bool:
        try:
            status = tdxquant_pool.get_status()
            self.status = status
            return status == DataSourceStatus.AVAILABLE
        except Exception as e:
            logger.error(f"{self.name} health check failed: {e}")
            return False


def test_all_methods():
    print("TdxQuantDataSource test placeholder")


if __name__ == "__main__":
    test_all_methods()
