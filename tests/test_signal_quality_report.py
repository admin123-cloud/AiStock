# -*- coding: utf-8 -*-
"""B 方案（2026-11-13）：做T信号质量日报回归。

锚定 2026-11-13 真实样本形状（drafts/20261113.jsonl：002015 快车道
1 confirmed + 3 expired，收盘 13.00）：
  - fast_buy_band px=13.15 → bps=(13.00-13.15)/13.15*1e4 = -114.1
  - fast_l2_rsi5_rollup 买 px=13.06 → -45.9
  - fast_l2_price_break_ma5 卖 px=13.06/13.04 → +45.9 / +30.7（均值 +38.3）
覆盖：分类/率/bps 口径、JSON+MD 落盘幂等、收盘价回退链标注、
调度注册（15:20 工作日）与 runner 契约。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import signal_quality_report as sqr  # noqa: E402
from services import t_trade_signal as tts  # noqa: E402

DAY = date(2026, 11, 13)
CLOSE = 13.00


def draft(draft_id, side, kind, px, bar_status, **extra):
    row = {
        "schema": tts.SCHEMA_BEAT, "id": draft_id, "ts": "2026-11-13 13:28:23",
        "code": "002015", "name": "协鑫能科", "side": side, "lane": "fast",
        "px": px, "trigger": {"kind": kind, "rsi5": 30.0},
        "bar_status": bar_status,
    }
    row.update(extra)
    return row


# 2026-11-13 真实样本形状（1 confirmed + 3 expired，全 002015）
REAL_LIKE_DRAFTS = [
    draft("T20261113_002015_buy_01", "buy-T", "fast_buy_band", 13.15,
          "confirmed_bar", confirmed_at="2026-11-13 13:56:00",
          confirm_source="slow_beat_settle"),
    draft("T20261113_002015_sell_02", "sell", "fast_l2_price_break_ma5", 13.06,
          "expired_unconfirmed", expire_reason="unknown_kind"),
    draft("T20261113_002015_buy_03", "buy", "fast_l2_rsi5_rollup", 13.06,
          "expired_unconfirmed", expire_reason="unknown_kind"),
    draft("T20261113_002015_sell_04", "sell", "fast_l2_price_break_ma5", 13.04,
          "expired_unconfirmed", expire_reason="unknown_kind"),
]


class TestPureAggregation:
    def test_real_like_day_full_math(self):
        report = sqr.build_report(
            day=DAY, drafts=REAL_LIKE_DRAFTS, closes={"002015": CLOSE},
            close_source="kline_daily", generated_at="2026-11-13 15:20:00")
        assert report["schema"] == "signal_quality_report/1"
        assert report["date"] == "2026-11-13"
        assert report["closes"] == {"002015": CLOSE}
        t = report["totals"]
        assert t["triggered"] == 4 and t["confirmed"] == 1 and t["expired"] == 3
        assert t["open"] == 0 and t["settled"] == 4
        assert t["confirm_rate"] == pytest.approx(0.25)
        assert t["breakdown_rate"] == pytest.approx(0.75)
        assert t["avg_bps"] == pytest.approx(
            (-114.0684 + 45.9418 - 45.9418 + 30.6748) / 4, abs=0.05)
        by = report["by_trigger_kind"]
        assert set(by) == {"fast_buy_band", "fast_l2_price_break_ma5",
                           "fast_l2_rsi5_rollup"}
        assert by["fast_buy_band"] == {
            "triggered": 1, "confirmed": 1, "expired": 0, "open": 0, "settled": 1,
            "confirm_rate": 1.0, "breakdown_rate": 0.0,
            "avg_bps": pytest.approx(-114.0684, abs=0.05), "bps_samples": 1}
        assert by["fast_l2_price_break_ma5"]["triggered"] == 2
        assert by["fast_l2_price_break_ma5"]["avg_bps"] == pytest.approx(38.3, abs=0.05)
        assert by["fast_l2_rsi5_rollup"]["breakdown_rate"] == 1.0
        assert len(report["drafts"]) == 4

    def test_direction_bps(self):
        assert sqr.direction_bps("buy-T", 13.15, 13.0) == pytest.approx(-114.0684, abs=0.01)
        assert sqr.direction_bps("sell-T", 13.06, 13.0) == pytest.approx(45.9418, abs=0.01)
        assert sqr.direction_bps("reduce-hint", 13.06, 13.0) == pytest.approx(45.9418, abs=0.01)
        assert sqr.direction_bps("buy", None, 13.0) is None
        assert sqr.direction_bps("buy", 0, 13.0) is None
        assert sqr.direction_bps("hold", 13.0, 13.0) is None

    def test_status_classification(self):
        assert sqr.classify_status({"bar_status": "confirmed_bar"}) == "confirmed"
        assert sqr.classify_status({"bar_status": "expired_unconfirmed"}) == "expired"
        assert sqr.classify_status({"bar_status": "open", "lane": "fast"}) == "open"
        # 慢车道最终草案（无 bar_status）→ 视为已确认
        assert sqr.classify_status({"lane": None}) == "confirmed"
        assert sqr.classify_status({"confirmed_at": "2026-11-13 13:56:00"}) == "confirmed"

    def test_open_excluded_from_rate_denominator(self):
        drafts = [
            draft("a_buy_1", "buy", "fast_buy_band", 13.10, "open"),
            draft("a_buy_2", "buy", "fast_buy_band", 13.10, "confirmed_bar"),
        ]
        report = sqr.build_report(day=DAY, drafts=drafts, closes={"002015": CLOSE},
                                  close_source="kline_daily", generated_at="x")
        b = report["by_trigger_kind"]["fast_buy_band"]
        assert b["triggered"] == 2 and b["open"] == 1
        assert b["settled"] == 1
        assert b["confirm_rate"] == 1.0 and b["breakdown_rate"] == 0.0

    def test_missing_close_gives_null_bps(self):
        report = sqr.build_report(day=DAY, drafts=REAL_LIKE_DRAFTS[:1], closes={},
                                  close_source=None, generated_at="x")
        b = report["by_trigger_kind"]["fast_buy_band"]
        assert b["avg_bps"] is None and b["bps_samples"] == 0
        assert report["drafts"][0]["bps"] is None

    def test_empty_day_report(self):
        report = sqr.build_report(day=DAY, drafts=[], closes={}, close_source=None,
                                  generated_at="x")
        assert report["totals"]["triggered"] == 0
        assert report["totals"]["confirm_rate"] is None
        assert report["by_trigger_kind"] == {}

    def test_markdown_renders_kinds_and_rows(self):
        report = sqr.build_report(
            day=DAY, drafts=REAL_LIKE_DRAFTS, closes={"002015": CLOSE},
            close_source="kline_daily", generated_at="2026-11-13 15:20:00")
        md = sqr.render_markdown(report)
        assert "# 做T信号质量日报 2026-11-13" in md
        assert "fast_buy_band" in md and "fast_l2_price_break_ma5" in md
        assert "kline_daily" in md
        assert "T20261113_002015_buy_01" in md
        assert "-114.1" in md  # 明细 bps 保留 1 位


class TestRunPipeline:
    def _tracker_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "t_tracker"
        (root / "drafts").mkdir(parents=True)
        with (root / "drafts" / "20261113.jsonl").open("w", encoding="utf-8") as fh:
            for row in REAL_LIKE_DRAFTS:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return root

    def test_run_writes_json_and_md_idempotent(self, tmp_path):
        tracker = self._tracker_root(tmp_path)
        out = tmp_path / "signal_quality"
        kwargs = dict(day=DAY, tracker_root=tracker, out_dir=out,
                      closes_loader=lambda codes, day: {"002015": CLOSE},
                      now=datetime(2026, 11, 13, 15, 20, 0))
        r1 = sqr.run(**kwargs)
        assert r1["close_source"] == "injected"
        assert (out / "20261113.json").is_file() and (out / "20261113.md").is_file()
        r2 = sqr.run(**kwargs)  # 幂等重跑覆盖
        assert (out / "20261113.json").is_file()
        assert json.loads((out / "20261113.json").read_text(encoding="utf-8"))["totals"] \
            == r2["totals"] == r1["totals"]
        assert not list(out.glob("*.tmp"))

    def test_run_missing_drafts_file_yields_zero_report(self, tmp_path):
        out = tmp_path / "signal_quality"
        report = sqr.run(day=DAY, tracker_root=tmp_path / "empty_tracker",
                         out_dir=out, closes_loader=lambda codes, day: {},
                         now=datetime(2026, 11, 13, 15, 20, 0))
        assert report["totals"]["triggered"] == 0
        assert (out / "20261113.json").is_file()

    def test_load_drafts_parses_jsonl(self, tmp_path):
        tracker = self._tracker_root(tmp_path)
        rows = sqr.load_drafts(tracker, DAY)
        assert len(rows) == 4 and rows[0]["id"] == "T20261113_002015_buy_01"


class TestSchedulerRegistration:
    def test_job_registered_at_1520_weekday(self):
        from apscheduler.schedulers.blocking import BlockingScheduler
        from scripts import pipeline_scheduler as ps

        sched = BlockingScheduler(timezone=ps.BEIJING_TZ)
        try:
            ps.register_signal_quality_job(sched)
            job = sched.get_job(ps.SIGNAL_QUALITY_JOB_ID)
            assert job is not None and job.max_instances == 1 and job.coalesce is True
            # 周五 15:19 → 下一次 15:20；周六 → 下周一 15:20
            nxt = job.trigger.get_next_fire_time(
                None, datetime(2026, 11, 13, 15, 19, tzinfo=ps.BEIJING_TZ))
            assert nxt is not None
            assert nxt.astimezone(ps.BEIJING_TZ).strftime("%a %H:%M") == "Fri 15:20"
            nxt2 = job.trigger.get_next_fire_time(
                None, datetime(2026, 11, 14, 12, 0, tzinfo=ps.BEIJING_TZ))
            assert nxt2 is not None
            assert nxt2.astimezone(ps.BEIJING_TZ).strftime("%a %H:%M") == "Mon 15:20"
        finally:
            sched.shutdown(wait=False) if sched.running else None

    def test_run_invokes_script_with_today_flag(self):
        from scripts import pipeline_scheduler as ps

        calls = []

        def runner(cmd):
            calls.append(list(cmd))
            return {"returncode": 0, "stdout": "", "stderr": ""}

        report = ps.run_signal_quality_report(
            is_trading_day=lambda d: True, runner=runner,
            now_fn=lambda: datetime(2026, 11, 13, 15, 20,
                                    tzinfo=ps.BEIJING_TZ))
        assert report["status"] == "success" and report["step"] == "signal_quality"
        assert calls[0][1].endswith("signal_quality_report.py")
        assert calls[0][-1] == "--today"

    def test_run_non_trading_day_noop(self):
        from scripts import pipeline_scheduler as ps

        report = ps.run_signal_quality_report(
            is_trading_day=lambda d: False, runner=lambda cmd: pytest.fail("不应执行"),
            now_fn=lambda: datetime(2026, 11, 14, 15, 20, tzinfo=ps.BEIJING_TZ))
        assert report["status"] == "no_op_non_trading"

    def test_run_failure_rc_propagates(self):
        from scripts import pipeline_scheduler as ps

        report = ps.run_signal_quality_report(
            is_trading_day=lambda d: True,
            runner=lambda cmd: {"returncode": 1, "stdout": "", "stderr": "boom"},
            now_fn=lambda: datetime(2026, 11, 13, 15, 20, tzinfo=ps.BEIJING_TZ))
        assert report["status"] == "failure" and report["rc"] == 1

    def test_serve_registers_signal_quality_job(self):
        from apscheduler.schedulers.blocking import BlockingScheduler
        from scripts import pipeline_scheduler as ps

        sched = BlockingScheduler(timezone=ps.BEIJING_TZ)
        try:
            ps.register_postclose_job(sched, lambda: {"status": "ok"})
            ps.register_intraday_job(sched)
            ps.register_t_tracker_jobs(sched)
            ps.register_signal_quality_job(sched)
            assert sched.get_job(ps.SIGNAL_QUALITY_JOB_ID) is not None
        finally:
            sched.shutdown(wait=False) if sched.running else None
