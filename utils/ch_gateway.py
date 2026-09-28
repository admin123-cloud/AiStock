from __future__ import annotations

import time
from typing import Any, Iterable, Optional

import pandas as pd

from utils.logger import get_logger
from utils.market_warehouse import clickhouse_client

logger = get_logger("ch_gateway")


class ClickHouseGateway:
    """Unified ClickHouse access entrypoint."""

    def query_df(self, sql: str, params: Optional[Iterable[Any]] = None) -> pd.DataFrame:
        started = time.perf_counter()
        client = clickhouse_client()
        try:
            df = client.query_df(sql, parameters=list(params or []))
            rows = 0 if df is None else len(df)
            logger.debug(f"query_df ok: rows={rows}")
            return df
        except Exception as exc:
            logger.error(f"query_df failed: err_type={type(exc).__name__}, err={exc}")
            raise
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            logger.debug(f"query_df ok: {elapsed:.2f}ms")

    def query_scalar(self, sql: str, params: Optional[Iterable[Any]] = None) -> Any:
        started = time.perf_counter()
        client = clickhouse_client()
        try:
            result = client.query(sql, parameters=list(params or []))
            if not result.result_rows:
                logger.debug("query_scalar ok: rows=0")
                return None
            logger.debug("query_scalar ok: rows=1")
            return result.result_rows[0][0]
        except Exception as exc:
            logger.error(f"query_scalar failed: err_type={type(exc).__name__}, err={exc}")
            raise
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            logger.debug(f"query_scalar ok: {elapsed:.2f}ms")

    def execute(self, sql: str, params: Optional[Iterable[Any]] = None) -> Any:
        started = time.perf_counter()
        client = clickhouse_client()
        try:
            return client.command(sql, parameters=list(params or []))
        except Exception as exc:
            logger.error(f"execute failed: err_type={type(exc).__name__}, err={exc}")
            raise
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            logger.debug(f"execute ok: {elapsed:.2f}ms")


ch_gateway = ClickHouseGateway()
