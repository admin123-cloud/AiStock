from types import SimpleNamespace

from utils import kline_store
from utils.derived_minute_live import ensure_live_derived_table, live_derived_table


class Client:
    def __init__(self):
        self.commands = []

    def command(self, sql):
        self.commands.append(sql)


def test_live_derived_table_is_month_partitioned():
    client = Client()
    table = ensure_live_derived_table(client, "30m")
    assert table == "kline_minute_30_live_derived"
    assert "PARTITION BY toYYYYMM(datetime)" in client.commands[0]
    assert "ReplacingMergeTree(created_at)" in client.commands[0]


def test_live_derived_table_rejects_non_derived_period():
    try:
        live_derived_table("5m")
    except ValueError as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("5m must not have a live-derived table")


def test_current_day_query_uses_live_derived_and_older_days_use_legacy(monkeypatch):
    monkeypatch.setattr(kline_store, "clickhouse_table_exists", lambda table: table == "kline_minute_30_live_derived")
    source = kline_store._get_query_source("30m")
    assert "kline_minute_30_live_derived" in source
    assert "kline_minute_30 WHERE toDate(datetime) !=" in source


def test_query_source_uses_legacy_until_live_table_exists(monkeypatch):
    monkeypatch.setattr(kline_store, "clickhouse_table_exists", lambda _table: False)
    assert kline_store._get_query_source("15m") == "kline_minute_15"
