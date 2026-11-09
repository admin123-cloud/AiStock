# -*- coding: utf-8 -*-
"""R27b：_persisted_datetime_iso 双路径守卫（DESIGN §5 S7 / §3 R9）。

已标注 DateTime('Asia/Shanghai') 列经 clickhouse-connect 读回 aware 北京墙钟，
必须直通透出（旧版无条件 +8 会二次偏移）；plain 未标注审计列仍读回 naive
UTC 墙钟，必须保留 +8 还原业务时间。
"""
from datetime import datetime, timedelta, timezone

from api.system_config import _persisted_datetime_iso

UTC = timezone.utc
CN = timezone(timedelta(hours=8))


def test_annotated_aware_shanghai_passes_through_without_shift():
    value = datetime(2026, 11, 10, 9, 35, 0, tzinfo=CN)
    assert _persisted_datetime_iso(value) == "2026-11-10T09:35:00"


def test_annotated_aware_utc_renders_beijing_wall_without_second_shift():
    value = datetime(2026, 11, 10, 1, 35, 0, tzinfo=UTC)  # 同一瞬间的 UTC 墙面
    assert _persisted_datetime_iso(value) == "2026-11-10T09:35:00"


def test_plain_naive_audit_column_keeps_plus8_restore():
    value = datetime(2026, 11, 10, 1, 35, 0)  # plain 列 naive UTC 墙钟
    assert _persisted_datetime_iso(value) == "2026-11-10T09:35:00"


def test_invalid_values_return_none():
    assert _persisted_datetime_iso(None) is None
    assert _persisted_datetime_iso("") is None
