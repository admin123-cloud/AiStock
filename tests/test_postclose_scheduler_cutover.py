# -*- coding: utf-8 -*-
"""P1-3 盘后调度切主与 S9/S11 发布控制面联动单测。"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import pipeline_scheduler as ps  # noqa: E402
from services.postclose_publication import PublicationStore  # noqa: E402

BJ_TZ = timezone(timedelta(hours=8))
TD = date(2026, 11, 19)
NOW_BJ = datetime(2026, 11, 19, 16, 25, 0, tzinfo=BJ_TZ)


class TestPostcloseSchedulerCutover:
    def test_run_postclose_pipelines_marks_importing_then_certified(self, tmp_path):
        store = PublicationStore(tmp_path / "control")
        observed_rebuilding: list[bool] = []

        def fake_runner(_cmd):
            observed_rebuilding.append(store.is_rebuilding(TD))
            return {"returncode": 0, "stdout": "ok", "stderr": ""}

        report = ps.run_postclose_pipelines(
            run_date=TD,
            out_root=tmp_path / "out",
            mode="scheduled",
            is_trading_day=lambda _d: True,
            runner=fake_runner,
            now_fn=lambda: NOW_BJ,
            sleep_fn=lambda _s: None,
            publication_store=store,
        )
        assert report["status"] == "success"
        assert observed_rebuilding and all(observed_rebuilding)
        assert store.is_rebuilding(TD) is False

        view = store.read_view(requested_date=TD, now=NOW_BJ)
        assert view.ready is True
        assert view.status == "CERTIFIED"
        assert view.generation == 1
        assert view.last_certified_date == TD.isoformat()

    def test_run_postclose_pipelines_failure_marks_import_failed_preserving_last_certified(self, tmp_path):
        store = PublicationStore(tmp_path / "control")
        prev_day = date(2026, 11, 18)
        prev_now = datetime(2026, 11, 18, 16, 25, 0, tzinfo=BJ_TZ)

        # 前一日成功发布 CERTIFIED
        ps.run_postclose_pipelines(
            run_date=prev_day,
            out_root=tmp_path / "out",
            is_trading_day=lambda _d: True,
            runner=lambda _cmd: {"returncode": 0, "stdout": "ok", "stderr": ""},
            now_fn=lambda: prev_now,
            sleep_fn=lambda _s: None,
            publication_store=store,
        )
        assert store.read_view(requested_date=prev_day, now=prev_now).ready is True

        # 今日 minute_collect 返回 rc=3 源不可达 → 写入 IMPORT_FAILED 且保留前一日 last_certified_date
        calls = {"n": 0}

        def failing_runner(_cmd):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"returncode": 0, "stdout": "daily ok", "stderr": ""}
            return {"returncode": 3, "stdout": "", "stderr": "source down"}

        report = ps.run_postclose_pipelines(
            run_date=TD,
            out_root=tmp_path / "out",
            is_trading_day=lambda _d: True,
            runner=failing_runner,
            now_fn=lambda: NOW_BJ,
            sleep_fn=lambda _s: None,
            publication_store=store,
        )
        assert report["status"] == "failure"
        view = store.read_view(requested_date=TD, now=NOW_BJ)
        assert view.ready is False
        assert view.status == "IMPORT_FAILED"
        assert view.generation == 2
        assert view.last_certified_date == prev_day.isoformat()

    def test_non_trading_day_noop_does_not_overwrite_certified_state(self, tmp_path):
        store = PublicationStore(tmp_path / "control")
        ps.run_postclose_pipelines(
            run_date=TD,
            out_root=tmp_path / "out",
            is_trading_day=lambda _d: True,
            runner=lambda _cmd: {"returncode": 0, "stdout": "ok", "stderr": ""},
            now_fn=lambda: NOW_BJ,
            sleep_fn=lambda _s: None,
            publication_store=store,
        )
        sat = date(2026, 11, 21)
        rep = ps.run_postclose_pipelines(
            run_date=sat,
            out_root=tmp_path / "out",
            is_trading_day=lambda _d: False,
            runner=lambda _cmd: {"returncode": 0, "stdout": "", "stderr": ""},
            now_fn=lambda: datetime(2026, 11, 21, 16, 15, 0, tzinfo=BJ_TZ),
            sleep_fn=lambda _s: None,
            publication_store=store,
        )
        assert rep["status"] == "no_op"
        assert store.read_view(requested_date=TD, now=NOW_BJ).ready is True
