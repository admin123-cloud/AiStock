# -*- coding: utf-8 -*-
"""R40 影子做T引擎单测（services/t_trade_shadow.py）——零网络零 DB。

覆盖任务书 20261110_shadow_ttrade_r40.md 验收面：
  - 判别式触发：R39 正例锚 2026-11-10 09:40 bar 必触发（真实东财 5m fixture）；
  - 下降态排除负例 + 各特征门负例（pos20/直线度/RSI 带/乖离）；
  - observe_only 隔离断言：影子触发后 drafts/pending_push/state 零出现零改动；
  - 虚拟成交（信号 bar 次开价、量=底仓 1/3 整百）与结局生命周期
    （awaiting_fill → tracking → finalized；30/60/120min/收盘；费用模型）；
  - 尾盘触发 h120 不可达 → close_fallback；
  - 4 周滚动统计与通过标准达成度（wr≥60% ∧ avg≥+0.3%）；
  - 布防语义（热段内不重复触发）。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import t_trade_shadow as tsh  # noqa: E402
from services import t_trade_signal as tts  # noqa: E402

DAY = date(2026, 11, 10)          # R39 锚日（真实交易日）
STAMP = "20261110"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "shadow_anchor_bars5_20261110.json"

HOLDINGS = {"holdings": [
    {"code": "002015", "name": "协鑫能科", "shares": 4000},
    {"code": "300054", "name": "鼎龙股份", "shares": 100},
], "updated_at": "2026-11-10 09:00:00", "holding_source": "test"}

REGIME_1110 = {  # 引擎 09:25 周期判定实值（runtime/t_tracker/regime_20261110.json 实档）：
    # 002015 sideways pos20=0.6709；300054 严格式边界判横盘 pos20=0.652（R37 已证，
    # MA10 差 0.06 未上穿——非 R34 叙事锚的 up/0.75），故 300054 11-10 也在影子门内。
    "schema": tts.SCHEMA_REGIME, "date": DAY.isoformat(), "status": "ok",
    "codes": {
        "002015": {"regime": "sideways", "pos20": 0.6709, "ma5": 12.99, "ma10": 12.64,
                   "ma20": 13.05, "hi20": 13.6, "lo20": 12.2, "close": 13.43,
                   "t1_date": "2026-11-09", "name": "协鑫能科", "params": {}},
        "300054": {"regime": "sideways", "pos20": 0.652, "ma5": 61.48, "ma10": 58.83,
                   "ma20": 58.29, "hi20": 62.0, "lo20": 55.0, "close": 60.7,
                   "t1_date": "2026-11-09", "name": "鼎龙股份", "params": {}},
    },
}


def load_fixture() -> dict[str, list[dict]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def shadow_env(tmp_path, *, bars_map=None, regime_doc=None, holdings=None):
    bars = bars_map if bars_map is not None else load_fixture()
    doc = regime_doc if regime_doc is not None else REGIME_1110
    root = tmp_path / "t_tracker_shadow_test"
    return {
        "out_dir": root,
        "holdings_loader": lambda: holdings or HOLDINGS,
        "regime_loader": lambda d: doc if d == DAY else None,
        "bars5_loader": lambda code: bars.get(code, []),
    }, root


def beat_times(day=DAY, start=dt_time(9, 36), end=dt_time(14, 56), step=5):
    out, cur = [], datetime.combine(day, start)
    stop = datetime.combine(day, end)
    while cur <= stop:
        out.append(cur)
        cur += timedelta(minutes=step)
    return out


def synth_bars(today_bars: dict[str, tuple[float, float]], *, prior_close=13.40,
               prior_n=30, day=DAY):
    """合成 5m bar 序列：前一日交替微幅（RSI 基线≈50、无趋势）+ 指定当日 bar。

    today_bars: {"09:35": (open, close), ...}；amount=open/close×volume×100（手→股）。
    """
    out = []
    d1 = day - timedelta(days=1)
    t = datetime.combine(d1, dt_time(10, 0))
    closes = []
    for i in range(prior_n):
        c = round(prior_close + (0.012 if i % 2 == 0 else -0.006), 3)
        closes.append(c)
        out.append({"ts": t.strftime("%Y-%m-%d %H:%M"), "open": c, "close": c,
                    "volume": 900.0, "amount": c * 900 * 100})
        t += timedelta(minutes=5)
    ts0 = datetime.combine(day, dt_time(9, 35))
    for hm, (o, c) in sorted(today_bars.items()):
        h, m = int(hm[:2]), int(hm[3:])
        assert datetime.combine(day, dt_time(h, m)) >= ts0
        vol = 900.0
        out.append({"ts": f"{day.isoformat()} {hm}", "open": o, "close": c,
                    "volume": vol, "amount": (o + c) / 2 * vol * 100})
    return out


# ── 判别式（纯函数）────────────────────────────────────────────

class TestGate:
    FEATS = {"gain30_pct": 2.9, "down30": 2, "rsi5": 79.3, "vwap_dev_pct": 1.02}

    def test_anchor_gate_all_pass(self):
        ok, reasons = tsh.evaluate_gate({"regime": "sideways", "pos20": 0.671}, self.FEATS)
        assert ok and reasons == []

    def test_down_regime_hard_excluded(self):
        # 下降态排除：即使四特征全部满足也不触发（R40 红线）
        ok, reasons = tsh.evaluate_gate({"regime": "down", "pos20": 0.9}, self.FEATS)
        assert not ok and reasons == ["down_regime_excluded"]

    def test_not_sideways_excluded(self):
        ok, reasons = tsh.evaluate_gate({"regime": "up", "pos20": 0.9}, self.FEATS)
        assert not ok and "not_sideways_regime" in reasons

    def test_pos20_below_min(self):
        ok, reasons = tsh.evaluate_gate({"regime": "sideways", "pos20": 0.64}, self.FEATS)
        assert not ok and "pos20_below_min" in reasons

    def test_gain_below_min(self):
        feats = {**self.FEATS, "gain30_pct": 0.9}
        ok, reasons = tsh.evaluate_gate({"regime": "sideways", "pos20": 0.671}, feats)
        assert not ok and "gain30_below_min" in reasons

    def test_ramp_not_straight(self):
        feats = {**self.FEATS, "down30": 4}
        ok, reasons = tsh.evaluate_gate({"regime": "sideways", "pos20": 0.671}, feats)
        assert not ok and "ramp_not_straight" in reasons

    def test_rsi_out_of_band(self):
        for rsi in (54.9, 80.0, 85.0):
            feats = {**self.FEATS, "rsi5": rsi}
            ok, reasons = tsh.evaluate_gate({"regime": "sideways", "pos20": 0.671}, feats)
            assert not ok and "rsi5_out_of_band" in reasons, rsi

    def test_vwap_dev_below_min(self):
        feats = {**self.FEATS, "vwap_dev_pct": 0.4}
        ok, reasons = tsh.evaluate_gate({"regime": "sideways", "pos20": 0.671}, feats)
        assert not ok and "vwap_dev_below_min" in reasons


# ── 锚例集成：真实 11-10 数据，09:40 bar 必触发 ─────────────────

class TestAnchorTrigger:
    def test_anchor_bar_triggers_at_0941_beat(self, tmp_path):
        env, root = shadow_env(tmp_path)
        report = tsh.shadow_beat(now=datetime(2026, 11, 10, 9, 41), **env)
        assert report["status"] == "ok"
        assert report["new_signal_ids"] and len(report["new_signal_ids"]) == 1
        doc = tsh.load_day_doc(root, DAY)
        sig = doc["signals"][0]
        assert sig["code"] == "002015" and sig["observe_only"] is True
        assert sig["trigger_kind"] == "vertical_ramp_distribution"
        assert sig["signal_bar_ts"] == "2026-11-10 09:40"
        f = sig["features"]
        # 与 R39 报告 §五 11-10 回放对拍：RSI79.3 / gain2.9% / 乖离+1.02%
        assert 78.0 <= f["rsi5"] <= 80.0
        assert f["gain30_pct"] >= 1.0 and f["gain30_pct"] == pytest.approx(2.9, abs=0.3)
        assert f["vwap_dev_pct"] >= 0.5 and f["vwap_dev_pct"] == pytest.approx(1.02, abs=0.2)
        assert f["down30"] <= 3
        # 300054 当日 09:41 尚未满足特征门（其首个触发在 10:35，见全日回放测试）
        assert all(s["code"] != "300054" for s in doc["signals"])
        suppressed_300054 = [e for e in doc["eval_log"]
                             if e["code"] == "300054" and e["decision"] == "suppressed"]
        assert suppressed_300054 and all(
            set(e["reasons"]) & {"gain30_below_min", "rsi5_out_of_band", "vwap_dev_below_min"}
            for e in suppressed_300054)

    def test_isolation_no_drafts_no_pending_push(self, tmp_path):
        """隔离红线：影子触发后 drafts/pending_push/state 零出现，真草案链零改动。"""
        env, root = shadow_env(tmp_path)
        # 先放一份真草案文件与 pending_push，验证影子拍不碰它们
        drafts_file = tts.drafts_path(root, DAY)
        drafts_file.parent.mkdir(parents=True, exist_ok=True)
        drafts_file.write_text(json.dumps({"id": "T_real_01"}) + "\n", encoding="utf-8")
        (root / "pending_push").mkdir(parents=True, exist_ok=True)
        (root / "pending_push" / "T_real_01.json").write_text("{}", encoding="utf-8")
        before = (root / "pending_push").stat().st_mtime_ns

        report = tsh.shadow_beat(now=datetime(2026, 11, 10, 9, 41), **env)
        assert report["new_signal_ids"]

        lines = drafts_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["id"] == "T_real_01"
        assert list((root / "pending_push").iterdir()) and \
            (root / "pending_push").stat().st_mtime_ns == before
        assert not (root / f"state_{STAMP}.json").exists()   # 不写真引擎状态
        assert (root / "shadow" / f"{STAMP}.json").is_file()  # 只写影子台账
        # 唯一新增文件全在 shadow/ 下
        new_files = [p for p in root.rglob("*") if p.is_file()]
        assert all(p.relative_to(root).parts[0] in ("shadow", "drafts", "pending_push")
                   for p in new_files)
        shadow_only = [p for p in new_files if p.relative_to(root).parts[0] == "shadow"]
        assert len(shadow_only) == 1

    def test_hot_episode_no_refire(self, tmp_path):
        """布防语义：热段内后续拍不重复触发（09:41 触发后 09:46 仍热 → suppressed）。"""
        env, root = shadow_env(tmp_path)
        r1 = tsh.shadow_beat(now=datetime(2026, 11, 10, 9, 41), **env)
        assert len(r1["new_signal_ids"]) == 1
        r2 = tsh.shadow_beat(now=datetime(2026, 11, 10, 9, 46), **env)
        assert r2["new_signal_ids"] == []
        doc = tsh.load_day_doc(root, DAY)
        assert len(doc["signals"]) == 1


# ── 全日回放：虚拟成交 + 结局 + 判定（对照 R39 事件研究）────────

class TestFullDayLifecycle:
    def test_replay_day_four_signals_two_and_two(self, tmp_path):
        """11-10 全日回放（真实东财 fixture + 真实 regime 实档）。

        实证注记：任务书预期"全日 1 条"，出自 R39 r39_p5_robust.py 回放循环在
        10:30 截断（`if series[i][1] > "10:30": break`）且只扫 002015。全日全池扫描下：
        - 002015 两独立热段（09:40 / 11:05）均过门，虚拟卖均赢，锚例结局与 R39
          事件研究一致（13.84→13.64 方向正确，h120 净 +1.567%）；
        - 300054 严格式边界判横盘 pos20=0.652 ≥0.65 亦在门内，10:35/13:30 两触发，
          当日下午大涨 → 虚拟卖如实记亏（h120 −8.01% / close −8.00%），
          底仓 100 股 → 虚拟量 0、纯比率口径。
        已用库内 CH FINAL 5m 交叉源逐 bar 对拍零差异、11:05 过门两源同判。
        """
        env, root = shadow_env(tmp_path)
        for t in beat_times():
            tsh.shadow_beat(now=t, **env)
        summary = tsh.finalize_shadow_day(
            for_date=DAY, now=datetime(2026, 11, 10, 15, 16), **env)
        assert summary["beat_status"] == "ok"
        assert summary["signals_total"] == 4          # 002015×2 + 300054×2
        assert summary["finalized"] == 4
        doc = tsh.load_day_doc(root, DAY)
        sig = doc["signals"][0]
        assert sig["signal_bar_ts"] == "2026-11-10 09:40"
        # 虚拟成交：信号 bar(09:40) 次根 bar 09:45 开盘价，量=底仓 4000 的 1/3 整百=1300
        fill = sig["virtual_fill"]
        assert fill["bar_ts"] == "2026-11-10 09:45"
        assert fill["qty"] == 1300
        assert fill["price"] == pytest.approx(13.84, abs=0.01)  # R39 同价位带 13.72-13.84
        # 结局：h30/h60/h120/收盘全终结，方向=卖出正确（13.84 → 13.6x）
        o = sig["outcomes"]
        assert o["h30"]["bar_ts"] == "2026-11-10 10:15" and o["h30"]["px"] < fill["price"]
        assert o["h120"]["px"] < fill["price"] and o["close"]["px"] == pytest.approx(13.64, abs=0.01)
        # 费用模型：卖 0.0666%+买 0.0186%(min5)；h120 净 ≈ +1.57%（毛 1.66%）
        assert o["h120"]["net_pct"] == pytest.approx(1.567, abs=0.05)
        assert o["close"]["net_pct"] == pytest.approx(1.351, abs=0.05)
        assert sig["judged"]["basis"] == "h120" and sig["judged"]["win"] is True
        assert sig["status"] == "finalized"
        # 002015 第二热段 11:05：次开 13.75 虚拟卖、终结为赢
        sig2 = [s for s in doc["signals"] if s["code"] == "002015"][1]
        assert sig2["signal_bar_ts"] == "2026-11-10 11:05"
        assert sig2["virtual_fill"]["price"] == pytest.approx(13.75, abs=0.01)
        assert sig2["judged"]["win"] is True and sig2["status"] == "finalized"
        # 300054 两触发：量 0 比率口径，如实记亏（当日午后大涨）
        s300054 = [s for s in doc["signals"] if s["code"] == "300054"]
        assert [s["signal_bar_ts"] for s in s300054] == ["2026-11-10 10:35", "2026-11-10 13:30"]
        assert all(s["virtual_qty"] == 0 and s["virtual_fill"]["qty_note"] == "t_lot_below_min_ratio_basis"
                   for s in s300054)
        assert [s["judged"]["win"] for s in s300054] == [False, False]
        assert s300054[0]["judged"]["net_pct"] == pytest.approx(-8.013, abs=0.05)
        assert s300054[1]["judged"]["basis"] == "close_fallback"
        # 日统计 + 4 周滚动 + 通过标准达成度（4 笔 2 胜 2 负，wr=0.5、avg=(1.567+0.851-8.013-8.001)/4）
        assert summary["judged_stats"]["n"] == 4
        assert summary["judged_stats"]["win_rate"] == 0.5
        assert summary["judged_stats"]["avg_net_pct"] == pytest.approx(-3.399, abs=0.05)
        prog = summary["rolling_4w"]["progress"]
        assert prog["win_rate_pass"] is False and prog["avg_net_pct_pass"] is False
        assert prog["overall_pass"] is False
        assert summary["rolling_4w"]["stats"]["n"] == 4
        assert summary["rolling_4w"]["per_code"]["002015"]["n"] == 2
        assert summary["rolling_4w"]["per_code"]["300054"]["n"] == 2

    def test_late_signal_h120_unreachable_close_fallback(self, tmp_path):
        """尾盘触发（14:50 bar）：h30/60/120 同日不可达 → close_fallback 判定。"""
        today = {}
        t = datetime(2026, 11, 10, 9, 35)
        while t <= datetime(2026, 11, 10, 14, 25):   # 早段横盘（不触发：gain/dev 低）
            today[t.strftime("%H:%M")] = (13.52, 13.51 if t.minute % 10 else 13.52)
            t += timedelta(minutes=5)
        ramp = {"14:30": 13.54, "14:35": 13.59, "14:40": 13.57, "14:45": 13.65,
                "14:50": 13.72}                       # 尾盘拉升 +1.5%（信号 bar=14:50，RSI≈79）
        for hm, c in ramp.items():
            today[hm] = (round(c - 0.01, 3), c)
        today["14:55"] = (13.68, 13.66)
        today["15:00"] = (13.62, 13.53)
        bars_map = {"002015": synth_bars(today)}
        env, root = shadow_env(tmp_path, bars_map=bars_map)
        for t in beat_times():
            tsh.shadow_beat(now=t, **env)
        summary = tsh.finalize_shadow_day(
            for_date=DAY, now=datetime(2026, 11, 10, 15, 16), **env)
        doc = tsh.load_day_doc(root, DAY)
        late = [s for s in doc["signals"] if s["signal_bar_ts"] >= "2026-11-10 14:45"]
        assert late, "尾盘拉升必须触发影子信号"
        sig = late[0]
        assert sig["signal_bar_ts"] == "2026-11-10 14:45"   # 拉升第 4 根 bar 恰过 gain 门
        o = sig["outcomes"]
        assert o["h30"]["unreachable"] is True
        assert o["h120"]["unreachable"] is True
        assert sig["virtual_fill"]["bar_ts"] == "2026-11-10 14:50"
        assert sig["judged"]["basis"] == "close_fallback"
        assert sig["status"] == "finalized"
        assert summary["close_fallback_count"] == 1

    def test_qty_floor_ratio_basis(self, tmp_path):
        """底仓 100 股（<300）触发：量=0，转纯比率口径并标记。"""
        bars_map = {"002015": synth_bars({
            "09:35": (13.30, 13.30), "09:40": (13.90, 13.95),
            "09:45": (13.80, 13.75), "09:50": (13.74, 13.72),
            "10:15": (13.70, 13.68), "10:20": (13.67, 13.65),
            "11:45": (13.62, 13.60), "13:05": (13.60, 13.58),
            "13:10": (13.58, 13.57), "15:00": (13.55, 13.50),
        })}
        holdings = {"holdings": [{"code": "002015", "name": "协鑫能科", "shares": 100}],
                    "updated_at": "2026-11-10 09:00:00", "holding_source": "test"}
        env, root = shadow_env(tmp_path, bars_map=bars_map, holdings=holdings)
        r = tsh.shadow_beat(now=datetime(2026, 11, 10, 9, 41), **env)
        assert len(r["new_signal_ids"]) == 1, [
            e for e in tsh.load_day_doc(root, DAY).get("eval_log", [])
            if e.get("decision") == "suppressed"]
        tsh.finalize_shadow_day(for_date=DAY, now=datetime(2026, 11, 10, 15, 16), **env)
        doc = tsh.load_day_doc(root, DAY)
        sig = doc["signals"][0]
        assert sig["virtual_fill"]["qty"] == 0
        assert sig["virtual_fill"]["qty_note"] == "t_lot_below_min_ratio_basis"
        assert sig["judged"]["net_pct"] is not None   # 比率口径仍可判定


# ── 只读聚合（API 端点数据面）──────────────────────────────────

class TestReadReview:
    def test_read_shadow_review_reflects_ledger(self, tmp_path, monkeypatch):
        env, root = shadow_env(tmp_path)
        for t in beat_times():
            tsh.shadow_beat(now=t, **env)
        tsh.finalize_shadow_day(for_date=DAY, now=datetime(2026, 11, 10, 15, 16), **env)
        monkeypatch.setenv("AISTOCK_T_TRACKER_DIR", str(root))
        review = tsh.read_shadow_review(DAY.isoformat())
        assert review["exists"] is True and len(review["signals"]) == 4
        sig = review["signals"][0]
        assert sig["observe_only"] is True
        assert sig["latest_net_pct"] == pytest.approx(1.567, abs=0.05)
        assert review["rolling_4w"]["stats"]["n"] == 4
        assert review["isolation"]["pending_push"] == "never"
        assert review["params"]["gain30_min_pct"] == 1.0

    def test_read_shadow_review_missing_day_is_empty_not_error(self, tmp_path, monkeypatch):
        root = tmp_path / "t_tracker_shadow_test"
        monkeypatch.setenv("AISTOCK_T_TRACKER_DIR", str(root))
        review = tsh.read_shadow_review("2026-11-10")
        assert review["exists"] is False and review["signals"] == []
        assert review["rolling_4w"]["stats"]["n"] == 0
        assert review["rolling_4w"]["progress"]["overall_pass"] is None

    def test_rolling_ignores_companion_files(self, tmp_path, monkeypatch):
        """回归：shadow/ 里的伴生件（*_summary / *_replay_report 等）不得计入滚动统计。"""
        env, root = shadow_env(tmp_path)
        for t in beat_times():
            tsh.shadow_beat(now=t, **env)
        tsh.finalize_shadow_day(for_date=DAY, now=datetime(2026, 11, 10, 15, 16), **env)
        # 伴生件：回放报告（signals 全量副本）+ 摘要 + 乱码文件
        day_file = tsh.shadow_day_path(root, DAY)
        doc = json.loads(day_file.read_text(encoding="utf-8"))
        (root / "shadow" / "20261110_replay_report.json").write_text(
            json.dumps({"signals": doc["signals"]}, ensure_ascii=False), encoding="utf-8")
        (root / "shadow" / "notes.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("AISTOCK_T_TRACKER_DIR", str(root))
        review = tsh.read_shadow_review(DAY.isoformat())
        assert review["rolling_4w"]["stats"]["n"] == 4   # 恰日台账 4 笔，无双计


# ── 与真引擎的关系：run_beat 不产影子，影子不产草案 ─────────────

class TestEngineBoundary:
    def test_real_run_beat_writes_no_shadow(self, tmp_path):
        """真节拍（R37 引擎）不写 shadow/；影子状态不进 SignalState。"""
        bars = load_fixture()
        bars15 = [dict(b) for b in bars["002015"]]   # 15m 用同源近似（仅验证目录面）
        info = REGIME_1110["codes"]["002015"]
        info = {**info, "params": tts.resolve_params("002015", "sideways",
                                                     {"hi20": 13.6, "lo20": 12.2})}
        regime_doc = {"schema": tts.SCHEMA_REGIME, "date": DAY.isoformat(), "status": "ok",
                      "codes": {"002015": info, "300054": REGIME_1110["codes"]["300054"]}}
        root = tmp_path / "t_tracker_shadow_test"
        report = tts.run_beat(
            now=datetime(2026, 11, 10, 9, 41), out_dir=root,
            holdings_loader=lambda: HOLDINGS,
            regime_loader=lambda d: regime_doc if d == DAY else None,
            bars5_loader=lambda code: bars.get(code, []),
            bars15_loader=lambda code: bars15, snapshot_loader=lambda code: None)
        assert report["status"] == "ok"
        assert not (root / "shadow").exists()
