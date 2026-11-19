# -*- coding: utf-8 -*-
"""运营层采集泳道并发隔离测试（QMT 全推聚合用例已随 QMT 全线退役移除）。"""

from threading import Event

from services.operations.ingestion_lanes import IngestionLane


def test_slow_minute_lane_does_not_block_snapshot_or_spawn_duplicates():
    gate = Event()
    slow = IngestionLane("minutes", lambda: gate.wait(2))
    fast = IngestionLane("snapshot", lambda: {"rows": 6000})
    try:
        assert slow.start()
        assert not slow.start()
        assert fast.start()
        assert fast.join(1)
        assert fast.state["result"]["rows"] == 6000
        assert slow.snapshot()["status"] == "running"
    finally:
        gate.set()
        slow.join(2)
