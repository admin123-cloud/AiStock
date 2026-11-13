# -*- coding: utf-8 -*-
"""A 方案（2026-11-13）：beat suppressed 异常告警回归。

覆盖：
  - 判别：evaluate_error/Exception 类关键词 → 告警；业务闸/数据闸 → 静默；
  - 同异常（code+异常类名）10min 去重冷却；不同 code/不同异常类不互相压制；
  - 产物：alerts/YYYYMMDD.jsonl 审计（deduped 标记）+ cooldown.json + pending/；
  - 推送注入；告警链路失败绝不打断 run_beat 节拍（集成）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from services import t_tracker_alerts as tta
from services import t_trade_signal as tts

DAY = datetime(2026, 11, 13, 10, 30, 0)


def beat_record(codes: dict) -> dict:
    return {"schema": tts.SCHEMA_BEAT, "ts": "2026-11-13 10:30:00",
            "status": "ok", "codes": codes}


# ── 判别（纯函数）────────────────────────────────────────────

class TestClassifier:
    @pytest.mark.parametrize("reason", [
        "evaluate_error: KeyError: 'sell'",
        "evaluate_error: NameError: name '_logger' is not defined",
        "evaluate_error: HTTPError: HTTP Error 502: Bad Gateway",
        "evaluate_error: TypeError: can't compare offset-naive and offset-aware",
        "KeyError: 'buy'",           # 无前缀但含异常类名
        "未捕获 Traceback",          # 关键词兜底
    ])
    def test_exception_reasons_alert(self, reason):
        assert tta.is_exception_reason(reason) is True

    @pytest.mark.parametrize("reason", [
        "t_lot_below_min", "not_in_pool", "cooldown_30min",
        "outside_time_window", "disarmed_wait_rearm", "daily_t_cap",
        "rsi_insufficient_data", "stale_minute_data",
        "empty_minute_data", "no_regime_info", "position_too_small",
        "insufficient_ticks", "", None,
    ])
    def test_business_gates_silent(self, reason):
        assert tta.is_exception_reason(reason) is False

    def test_exception_class_extraction(self):
        assert tta.exception_class_of("evaluate_error: KeyError: 'sell'") == "KeyError"
        assert tta.exception_class_of(
            "evaluate_error: NameError: name '_logger' is not defined") == "NameError"
        assert tta.exception_class_of("t_lot_below_min") == "unknown"

    def test_scan_beat_codes(self):
        rec = beat_record({
            "002015": {"code": "002015",
                       "suppressed": {"reason": "evaluate_error: KeyError: 'sell'"}},
            "300054": {"code": "300054", "suppressed": {"reason": "not_in_pool"}},
            "600000.SH": {"code": "600000.SH",
                          "suppressed": {"reason": "evaluate_error: TypeError: x"}},
        })
        finds = tta.scan_beat_codes(rec)
        assert [f["code"] for f in finds] == ["002015", "600000"]
        assert finds[0]["exc_class"] == "KeyError"


# ── 主流程 + 产物 + 去重 ─────────────────────────────────────

class TestProcessBeatSuppressions:
    def _run(self, tmp_path, codes, now, pushed, lane="fast"):
        return tta.process_beat_suppressions(
            root=tmp_path / "t_tracker", beat_record=beat_record(codes),
            now=now, lane=lane, push=pushed.append)

    def test_alert_written_and_pushed(self, tmp_path):
        pushed: list = []
        rep = self._run(tmp_path, {
            "002015": {"code": "002015",
                       "suppressed": {"reason": "evaluate_error: KeyError: 'sell'"}}},
            DAY, pushed)
        assert rep["status"] == "alerted" and rep["found"] == 1 and rep["deduped"] == 0
        # 审计 jsonl
        log_rows = [json.loads(x) for x in
                    (tmp_path / "t_tracker" / "alerts" / "20261113.jsonl")
                    .read_text(encoding="utf-8").splitlines()]
        assert len(log_rows) == 1 and log_rows[0]["deduped"] is False
        assert log_rows[0]["fingerprint"] == "002015:KeyError"
        # pending 待推送 + 推送文本含 code 与异常摘要
        pending = list((tmp_path / "t_tracker" / "alerts" / "pending").glob("*.json"))
        assert len(pending) == 1
        assert json.loads(pending[0].read_text(encoding="utf-8"))["exc_class"] == "KeyError"
        assert len(pushed) == 1
        assert "002015" in pushed[0][0] and "KeyError" in pushed[0][0]

    def test_business_gate_only_beat_is_silent(self, tmp_path):
        pushed: list = []
        rep = self._run(tmp_path, {
            "002015": {"code": "002015", "suppressed": {"reason": "t_lot_below_min"}},
            "300054": {"code": "300054", "suppressed": {"reason": "not_in_pool"}},
        }, DAY, pushed)
        assert rep == {"status": "no_exceptions", "found": 0, "alerted": [], "deduped": 0}
        assert not (tmp_path / "t_tracker" / "alerts").exists()
        assert pushed == []

    def test_same_exception_cooldown_10min(self, tmp_path):
        pushed: list = []
        reason = {"002015": {"code": "002015",
                             "suppressed": {"reason": "evaluate_error: KeyError: 'sell'"}}}
        rep1 = self._run(tmp_path, reason, DAY, pushed)
        assert rep1["status"] == "alerted"
        # 5min 后同异常 → 去重（审计仍记，推送压掉）
        rep2 = self._run(tmp_path, reason, DAY + timedelta(minutes=5), pushed)
        assert rep2["status"] == "all_deduped" and rep2["deduped"] == 1
        assert len(pushed) == 1
        # 10min 整点过后 → 重新告警
        rep3 = self._run(tmp_path, reason, DAY + timedelta(minutes=10, seconds=1), pushed)
        assert rep3["status"] == "alerted"
        assert len(pushed) == 2
        # 审计三行全在
        log_rows = (tmp_path / "t_tracker" / "alerts" / "20261113.jsonl") \
            .read_text(encoding="utf-8").splitlines()
        assert len(log_rows) == 3

    def test_different_code_or_class_not_deduped(self, tmp_path):
        pushed: list = []
        self._run(tmp_path, {"002015": {"code": "002015",
                           "suppressed": {"reason": "evaluate_error: KeyError: 'sell'"}}},
                  DAY, pushed)
        # 同刻不同 code → 告警
        rep = self._run(tmp_path, {"300054": {"code": "300054",
                                  "suppressed": {"reason": "evaluate_error: KeyError: 'x'"}}},
                        DAY, pushed)
        assert rep["status"] == "alerted"
        # 同 code 不同异常类 → 告警
        rep = self._run(tmp_path, {"002015": {"code": "002015",
                           "suppressed": {"reason": "evaluate_error: NameError: _logger"}}},
                        DAY, pushed)
        assert rep["status"] == "alerted"
        assert len(pushed) == 3

    def test_push_failure_does_not_raise_and_pending_kept(self, tmp_path):
        def boom(texts):
            raise RuntimeError("微信通道炸了")

        rep = tta.process_beat_suppressions(
            root=tmp_path / "t_tracker",
            beat_record=beat_record({
                "002015": {"code": "002015",
                           "suppressed": {"reason": "evaluate_error: KeyError: 'sell'"}}}),
            now=DAY, lane="slow", push=boom)
        assert rep["status"] == "alerted"
        assert len(list((tmp_path / "t_tracker" / "alerts" / "pending").glob("*.json"))) == 1

    def test_corrupt_cooldown_fail_open(self, tmp_path):
        root = tmp_path / "t_tracker"
        (root / "alerts").mkdir(parents=True)
        (root / "alerts" / "cooldown.json").write_text("{broken", encoding="utf-8")
        pushed: list = []
        rep = self._run(root.parent, {
            "002015": {"code": "002015",
                       "suppressed": {"reason": "evaluate_error: KeyError: 'sell'"}}},
            DAY, pushed)
        assert rep["status"] == "alerted"  # 状态损坏 → 宁可多告


# ── 集成：告警绝不打断节拍 ────────────────────────────────────

class TestBeatIntegration:
    """自合成 beat 环境（零网络零 DB）；评估异常 → 告警产物 + 节拍照常 ok。"""

    NOW = datetime(2026, 11, 13, 11, 0, 0)

    def _env(self, tmp_path):
        from datetime import date, datetime as dt, time as t, timedelta

        bars5, bars15 = [], []
        t0 = dt(2026, 11, 13, 9, 35)
        closes = [13.0 + 0.02 * i for i in range(15)]
        for i, c in enumerate(closes):
            ts = (t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")
            bars5.append({"ts": ts, "close": c, "volume": 1000.0,
                          "amount": c * 1000 * 100})
            bars15.append({"ts": ts, "close": c, "volume": 3000.0,
                           "amount": c * 3000 * 100})
        info = {"regime": "sideways", "ma5": 13.0, "ma10": 12.8, "ma20": 13.0,
                "pos20": 0.5, "hi20": 14.0, "lo20": 12.0, "close": 13.0,
                "chg5d_pct": 1.0, "t1_date": "2026-11-12",
                "params": tts.resolve_params("002015", "sideways",
                                             {"hi20": 14.0, "lo20": 12.0})}
        regime_doc = {"schema": tts.SCHEMA_REGIME, "date": "2026-11-13",
                      "status": "ok", "codes": {"002015": info}}
        return {
            "out_dir": tmp_path / "t_tracker_test",
            "holdings_loader": lambda: {"holdings": [
                {"code": "002015", "name": "协鑫能科", "shares": 2000}],
                "updated_at": "2026-11-13 09:00:00", "holding_source": "test"},
            "regime_loader": lambda d: regime_doc if d == date(2026, 11, 13) else None,
            "bars5_loader": lambda code: bars5,
            "bars15_loader": lambda code: bars15,
            "snapshot_loader": lambda code: None,
        }

    def test_run_beat_alerts_and_survives_evaluate_error(self, tmp_path, monkeypatch):
        env = self._env(tmp_path)

        def boom(**kwargs):
            raise KeyError("sell")

        monkeypatch.setattr(tts, "evaluate_code", boom)
        report = tts.run_beat(now=self.NOW, **env)
        # 节拍本体不受影响：beat ok + suppressed=evaluate_error + beats 落盘
        assert report["status"] == "ok"
        assert report["codes"]["002015"]["suppressed"]["reason"].startswith(
            "evaluate_error: KeyError")
        root = tmp_path / "t_tracker_test"
        assert (root / "beats" / "20261113.jsonl").is_file()
        # 告警产物已生成
        pending = list((root / "alerts" / "pending").glob("*.json"))
        assert len(pending) == 1
        alert = json.loads(pending[0].read_text(encoding="utf-8"))
        assert alert["code"] == "002015" and alert["exc_class"] == "KeyError"
        assert alert["lane"] == "slow"

    def test_run_beat_alert_hook_failure_never_breaks_beat(self, tmp_path, monkeypatch):
        env = self._env(tmp_path)

        monkeypatch.setattr(tts, "evaluate_code", lambda **kw: (_ for _ in ()).throw(
            NameError("_logger")))
        monkeypatch.setattr(
            tta, "process_beat_suppressions",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("告警链路炸了")))

        report = tts.run_beat(now=self.NOW, **env)
        assert report["status"] == "ok"  # 告警失败被 _alert_beat_suppressions 兜底吞掉
