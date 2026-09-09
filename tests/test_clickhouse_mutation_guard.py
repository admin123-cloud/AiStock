from types import SimpleNamespace

import pytest

from utils.clickhouse_mutation_guard import (
    UnsafeMinuteMutationError,
    require_month_partitioned_minute_mutation,
)


class Client:
    def __init__(self, partition_key: str):
        self.partition_key = partition_key
        self.calls = []

    def query(self, sql, **kwargs):
        self.calls.append((sql, kwargs))
        return SimpleNamespace(result_rows=[(self.partition_key,)])


def test_month_partitioned_minute_table_allows_mutation():
    client = Client("toYYYYMM(datetime)")
    require_month_partitioned_minute_mutation(client, "kline_minute_5")
    assert client.calls[0][1]["parameters"] == {"table": "kline_minute_5"}


def test_unpartitioned_minute_table_blocks_mutation():
    with pytest.raises(UnsafeMinuteMutationError, match="candidate table"):
        require_month_partitioned_minute_mutation(Client(""), "kline_minute_60")


def test_invalid_table_is_blocked_before_query():
    client = Client("toYYYYMM(datetime)")
    with pytest.raises(ValueError, match="identifier"):
        require_month_partitioned_minute_mutation(client, "kline_minute_60; DROP TABLE stock")
    assert not client.calls
