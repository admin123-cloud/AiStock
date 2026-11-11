# -*- coding: utf-8 -*-
"""R55（2026-11-11）：市场看板三端点白名单 + 调度接入 单元测试。

契约：
  - 三条 GET（/api/market/breadth、/api/market/advance-decline-distribution、
    /api/market/limit-up-stats）入预览栈白名单，GET+HEAD 放行、POST 等封；
  - 盘中快照节拍成功后追加 market_mood 计算（独立异常面：mood 失败不影响
    快照 success 状态）；
  - 盘后 16:40 market_mood finalize 节拍（窗口守卫 + 交易日守卫 + 幂等）。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BJ = ZoneInfo("Asia/Shanghai")

R55_PATHS = (
    "/api/market/breadth",
    "/api/market/advance-decline-distribution",
    "/api/market/limit-up-stats",
)


def _make_runner(results):
    calls = []

    def runner(cmd):
        calls.append(list(cmd))
        rc = results[len(calls) - 1] if len(results) >= len(calls) else 0
        return {"returncode": rc, "stdout": "", "stderr": ""}

    return calls, runner


class TestAllowlist:
    def test_three_paths_in_preview_allowlist(self):
        from api import operations_preview as op

        for path in R55_PATHS:
            assert path in op._ALLOWED_PREVIEW_PATHS, f"{path} 未入白名单"

    def test_pairs_in_safety_matrix(self):
        from tests.test_operations_preview_safety import ALLOWED_BUSINESS_ROUTE_PAIRS

        for path in R55_PATHS:
            assert ("GET", path) in ALLOWED_BUSINESS_ROUTE_PAIRS
            assert ("HEAD", path) in ALLOWED_BUSINESS_ROUTE_PAIRS
            assert ("POST", path) not in ALLOWED_BUSINESS_ROUTE_PAIRS

    def test_routes_registered_on_market_router(self):
        from api.market_overview import router

        registered = {getattr(r, "path", None) for r in router.routes}
        for sub in ("/breadth", "/advance-decline-distribution", "/limit-up-stats"):
            assert f"/market{sub}" in registered, f"路由 {sub} 未注册"


class TestSchedulerIntradayAppend:
    def test_mood_appended_after_snapshot_success(self, tmp_path, monkeypatch):
        import scripts.pipeline_scheduler as ps

        calls, runner = _make_runner([0, 0])
        monkeypatch.setattr(ps, "_default_is_trading_day", lambda d: True)
        result = ps.run_intraday_snapshot_beat(
            mode="scheduled",
            out_root=tmp_path / "postclose_auto",
            runner=runner,
            now_fn=lambda: datetime(2026, 11, 11, 10, 0, tzinfo=BJ),
        )
        assert result["status"] == "success"
        assert len(calls) == 2, "快照成功后必须追加 market_mood 计算"
        assert "intraday_snapshot_collect.py" in calls[0][-1] or any(
            "intraday_snapshot_collect.py" in part for part in calls[0])
        assert any("market_mood_update.py" in part for part in calls[1]), calls[1]
        assert "--intraday" in calls[1]
        assert result.get("mood_rc") == 0

    def test_mood_failure_does_not_fail_snapshot(self, tmp_path, monkeypatch):
        import scripts.pipeline_scheduler as ps

        calls, runner = _make_runner([0, 1])  # 快照成功、mood 失败
        monkeypatch.setattr(ps, "_default_is_trading_day", lambda d: True)
        result = ps.run_intraday_snapshot_beat(
            mode="scheduled",
            out_root=tmp_path / "postclose_auto",
            runner=runner,
            now_fn=lambda: datetime(2026, 11, 11, 10, 0, tzinfo=BJ),
        )
        assert result["status"] == "success", "mood 失败是独立异常面，不拖垮快照状态"
        assert result.get("mood_rc") == 1

    def test_snapshot_failure_skips_mood(self, tmp_path, monkeypatch):
        import scripts.pipeline_scheduler as ps

        calls, runner = _make_runner([3])
        monkeypatch.setattr(ps, "_default_is_trading_day", lambda d: True)
        result = ps.run_intraday_snapshot_beat(
            mode="scheduled",
            out_root=tmp_path / "postclose_auto",
            runner=runner,
            now_fn=lambda: datetime(2026, 11, 11, 10, 0, tzinfo=BJ),
        )
        assert result["status"] == "failure"
        assert len(calls) == 1, "快照失败不追加 mood"


class TestSchedulerFinalizeBeat:
    def test_finalize_runs_within_window(self, tmp_path, monkeypatch):
        import scripts.pipeline_scheduler as ps

        calls, runner = _make_runner([0])
        monkeypatch.setattr(ps, "_default_is_trading_day", lambda d: True)
        result = ps.run_market_mood_finalize_beat(
            mode="scheduled",
            out_root=tmp_path / "postclose_auto",
            runner=runner,
            now_fn=lambda: datetime(2026, 11, 11, 16, 40, tzinfo=BJ),
        )
        assert result["status"] == "success"
        assert any("market_mood_update.py" in part for part in calls[0])
        assert "--finalize" in calls[0]

    def test_finalize_guard_before_window(self, tmp_path, monkeypatch):
        import scripts.pipeline_scheduler as ps

        calls, runner = _make_runner([0])
        result = ps.run_market_mood_finalize_beat(
            mode="scheduled",
            out_root=tmp_path / "postclose_auto",
            runner=runner,
            now_fn=lambda: datetime(2026, 11, 11, 15, 30, tzinfo=BJ),
        )
        assert result["status"] == "no_op_window"
        assert calls == []

    def test_finalize_guard_non_trading_day(self, tmp_path, monkeypatch):
        import scripts.pipeline_scheduler as ps

        calls, runner = _make_runner([0])
        monkeypatch.setattr(ps, "_default_is_trading_day", lambda d: False)
        result = ps.run_market_mood_finalize_beat(
            mode="scheduled",
            out_root=tmp_path / "postclose_auto",
            runner=runner,
            now_fn=lambda: datetime(2026, 11, 14, 16, 40, tzinfo=BJ),
        )
        assert result["status"] == "no_op_non_trading"
        assert calls == []

    def test_finalize_job_registered(self):
        import scripts.pipeline_scheduler as ps

        class FakeScheduler:
            def __init__(self):
                self.jobs = []

            def add_job(self, func, trigger=None, **kwargs):
                self.jobs.append((func, trigger, kwargs))

            def get_job(self, job_id):
                for func, trigger, kwargs in self.jobs:
                    if kwargs.get("id") == job_id:
                        return func
                return None

        sched = FakeScheduler()
        ps.register_market_mood_finalize_job(sched)
        assert len(sched.jobs) == 1


class TestMoodUpdateCli:
    def test_intraday_mode_writes_realtime(self, tmp_path, monkeypatch):
        from scripts import market_mood_update as mu

        monkeypatch.setattr(
            mu, "compute_intraday_mood",
            lambda now=None: {"mode": "intraday", "date": "2026-11-11",
                              "limit_up_count": 30, "as_of": "2026-11-11 10:00"})
        rc = mu.main(["--intraday", "--out-root", str(tmp_path)])
        assert rc == 0
        import json
        payload = json.loads(
            (tmp_path / "market_mood" / "20261111.json").read_text(encoding="utf-8"))
        assert payload["realtime"]["limit_up_count"] == 30

    def test_intraday_no_data_rc_nonzero(self, tmp_path, monkeypatch):
        from scripts import market_mood_update as mu

        monkeypatch.setattr(mu, "compute_intraday_mood", lambda now=None: None)
        rc = mu.main(["--intraday", "--out-root", str(tmp_path)])
        assert rc != 0

    def test_finalize_mode_writes_final(self, tmp_path, monkeypatch):
        from scripts import market_mood_update as mu

        monkeypatch.setattr(
            mu, "compute_final_mood",
            lambda d: {"mode": "final", "date": d, "limit_up_count": 32})
        rc = mu.main(["--finalize", "--date", "2026-11-11",
                      "--out-root", str(tmp_path)])
        assert rc == 0
        import json
        payload = json.loads(
            (tmp_path / "market_mood" / "20261111.json").read_text(encoding="utf-8"))
        assert payload["final"]["limit_up_count"] == 32

    def test_finalize_missing_daily_rc_nonzero(self, tmp_path, monkeypatch):
        from scripts import market_mood_update as mu

        monkeypatch.setattr(mu, "compute_final_mood", lambda d: None)
        rc = mu.main(["--finalize", "--date", "2026-11-11",
                      "--out-root", str(tmp_path)])
        assert rc != 0
