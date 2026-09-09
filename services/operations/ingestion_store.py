"""Collector-only bounded database clients; no shared global connection lock."""
import os
import threading
from utils.market_warehouse import _adapt_clickhouse_query

_local = threading.local()


def clickhouse_client():
    client = getattr(_local, 'client', None)
    if client is None:
        import clickhouse_connect
        client = clickhouse_connect.get_client(
            host=os.getenv('AISTOCK_CLICKHOUSE_HOST', '127.0.0.1'),
            port=int(os.getenv('AISTOCK_CLICKHOUSE_PORT', '8123')),
            database=os.getenv('AISTOCK_CLICKHOUSE_DATABASE', 'stock'),
            username=os.getenv('AISTOCK_CLICKHOUSE_USER', 'default'),
            password=os.getenv('AISTOCK_CLICKHOUSE_PASSWORD', ''),
            connect_timeout=3, send_receive_timeout=20, query_retries=0,
            settings={'max_execution_time': 15})
        _local.client = client
    return client


def clickhouse_query_df(sql, params=None):
    query, parameters = _adapt_clickhouse_query(sql, params)
    return clickhouse_client().query_df(query, parameters=parameters)


def observe(**kwargs):
    try:
        from services.operations.source_metrics import record_source_request
        record_source_request(**kwargs)
    except Exception:
        # Telemetry cannot turn an acknowledged data write into a retry.
        pass
