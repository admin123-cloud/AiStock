# -*- coding: utf-8 -*-
"""R46 影子做T 1 分钟快车道单测（services/t_trade_shadow.py fast 面）——纯合成。

覆盖：
  - shadow_fast_tick：intrabar 特征重估 → fast_vertical_ramp_distribution
    （lane=fast、bar_status=open、intrabar_rsi5 实值、observe_only）；
  - 物理隔离红线不变：只写 shadow/，零 drafts/pending_push 触碰（目录断言）；
  - armed/30min 冷却与慢车道 shadow_beat 共用同一日台账 meta；
  - shadow_beat 收线结算：confirmed_bar（虚拟成交照常）/ expired_unconfirmed
    （不虚拟成交、不进 judged 统计、留档）；
  - 同波去重并入（shadow_beat_merge：不另发新信号）。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import t_trade_shadow as shadow  # noqa: E402
from services import t_trade_signal as tts  # noqa: E402

DAY = date(2026, 11, 11)


def minute_bars(closes, end_ts, step=5):
    out = []
    t = end_ts
    for c in reversed(closes):
        out.append({"ts": t.strftime("%Y-%m-%d %H:%M"), "close": float(c),
                    "open": float(c), "volume": 1000.0, "amount": float(c) * 1000 * 100})
        t -= timedelta(minutes=step)
    out.reverse()
    return out


def history_osc(n=114, base=13.0, amp=0.02):
    """小幅振荡（RSI 中位），供 RSI 收窗。"""
    return [round(base + (amp if i % 2 else -amp), 3) for i in range(n)]


def ramp_tail(n=7, start=13.02, step=0.03):
    """当日拉升尾段：n 根累计约 +1.3%（>1% 门），全升（down30=0）。"""
    return [round(start + step * i, 3) for i in range(n)]


HOLDINGS = {"holdings": [{"code": "002015", "name": "协鑫能科", "shares": 6000}],
            "updated_at": "t", "holding_source": "test"}

REGIME_INFO = {"regime": "sideways", "pos20": 0.70}
REGIME_DOC = {"schema": "t_tracker_regime/1", "date": DAY.isoformat(),
              "codes": {"002015": REGIME_INFO}}


def build_bars(now, px):
    """历史振荡 + 当日拉升（末根 forming ts>now 由调用方追加）。"""
    tail = ramp_tail()
    end_closed = datetime(2026, 11, 11, 10, 0)
    bars = minute_bars(history_osc(), end_closed - timedelta(minutes=0))
    # 把尾段落在今日：09:30..10:00 共 7 根（含 09:30 起点）
    today = minute_bars(tail, end_closed)
    return bars[:-len(today)] + today


def env(tmp_path, sub, bars5_map, quotes):
    root = tmp_path / sub
    return {
        "out_dir": root,
        "holdings_loader": lambda: HOLDINGS,
        "regime_loader": lambda d: REGIME_DOC if d == DAY else None,
        "bars5_loader": lambda code: bars5_map.get(code, []),
        "quotes_loader": lambda codes: dict(quotes),
    }


class TestShadowFastTick:
    def test_fast_signal_fires_and_isolated(self, tmp_path):
        now = datetime(2026, 11, 11, 10, 3, 23)
        tail_end_px = 13.02 + 0.03 * 6                     # 13.20
        bars = build_bars(now, tail_end_px)
        forming = {"ts": "2026-11-11 10:05", "open": 13.20, "close": 13.20,
                   "volume": 800.0, "amount": 800.0 * 13.25 * 100}
        px_quote = round(tail_end_px + 0.05, 3)            # 实时价再抬高（乖离↑）
        bars5_map = {"002015": bars + [forming]}
        e = env(tmp_path, "s1", bars5_map, {"002015": {"price": px_quote,
                                                       "ts": "2026-11-11 10:03:20"}})
        rep = shadow.shadow_fast_tick(now=now, **e)
        assert rep["status"] == "ok" and len(rep["new_signal_ids"]) == 1
        root = tts.tracker_dir(e["out_dir"])
        doc = shadow.load_day_doc(root, DAY)
        sig = doc["signals"][0]
        assert sig["lane"] == "fast" and sig["bar_status"] == "open"
        assert sig["trigger_kind"] == "fast_vertical_ramp_distribution"
        assert sig["observe_only"] is True
        assert sig["intrabar_rsi5"] == sig["features"]["rsi5"]
        assert sig["forming_bar_ts"] == "2026-11-11 10:05"
        assert sig["virtual_qty"] == tts.t_lot_cap(6000) == 2000
        # 物理隔离红线：零 drafts/pending_push 触碰
        assert not (root / "drafts").exists()
        assert not (root / "pending_push").exists()
        # armed 解除 + 冷却共享 meta
        assert doc["meta"]["002015"]["armed"] is False

        # 冷却内第二拍：仍在门内 → 不再发
        rep2 = shadow.shadow_fast_tick(now=now + timedelta(minutes=1), **e)
        assert rep2["new_signal_ids"] == []

    def test_fast_signal_off_gate_no_fire(self, tmp_path):
        """下降态硬排除 / 增幅不足 → 不触发且不动台账。"""
        now = datetime(2026, 11, 11, 10, 3, 23)
        bars = build_bars(now, 13.20)
        forming = {"ts": "2026-11-11 10:05", "open": 13.2, "close": 13.2,
                   "volume": 800.0, "amount": 800.0 * 13.2 * 100}
        bars5_map = {"002015": bars + [forming]}
        # down regime → 硬排除
        down_doc = {"schema": "t_tracker_regime/1", "date": DAY.isoformat(),
                    "codes": {"002015": {"regime": "down", "pos20": 0.2}}}
        e = dict(env(tmp_path, "s2", bars5_map,
                     {"002015": {"price": 13.25, "ts": "x"}}))
        e["regime_loader"] = lambda d: down_doc
        rep = shadow.shadow_fast_tick(now=now, **e)
        assert rep["new_signal_ids"] == []
        root = tts.tracker_dir(e["out_dir"])
        assert shadow.load_day_doc(root, DAY)["signals"] == []


class TestShadowFastSettlement:
    def _fire(self, tmp_path, sub):
        now = datetime(2026, 11, 11, 10, 3, 23)
        bars = build_bars(now, 13.20)
        forming = {"ts": "2026-11-11 10:05", "open": 13.20, "close": 13.20,
                   "volume": 800.0, "amount": 800.0 * 13.25 * 100}
        bars5_map = {"002015": bars + [forming]}
        e = env(tmp_path, sub, bars5_map,
                {"002015": {"price": 13.25, "ts": "2026-11-11 10:03:20"}})
        rep = shadow.shadow_fast_tick(now=now, **e)
        return e, rep["new_signal_ids"][0]

    def test_settle_confirmed_then_virtual_fill(self, tmp_path):
        e, sig_id = self._fire(tmp_path, "c1")
        root = tts.tracker_dir(e["out_dir"])
        # 10:06 慢拍：10:05 bar 已收且门仍成立（收线特征复核）
        bars_closed = build_bars(None, 13.2) + [
            {"ts": "2026-11-11 10:05", "open": 13.21, "close": 13.24,
             "volume": 900.0, "amount": 900.0 * 13.24 * 100}]
        e2 = dict(e)
        e2["bars5_loader"] = lambda code: bars_closed
        e2.pop("quotes_loader", None)
        rep = shadow.shadow_beat(now=datetime(2026, 11, 11, 10, 6, 0), **e2)
        assert sig_id in rep["fast_settled"]
        doc = shadow.load_day_doc(root, DAY)
        sig = next(s for s in doc["signals"] if s["id"] == sig_id)
        assert sig["bar_status"] == "confirmed_bar"
        assert sig["confirm_source"] == "shadow_beat_settle"
        # 确认后虚拟成交照常推进（下一根 bar 10:10 已收）
        bars_more = bars_closed + [
            {"ts": "2026-11-11 10:10", "open": 13.22, "close": 13.20,
             "volume": 900.0, "amount": 900.0 * 13.2 * 100}]
        e3 = dict(e)
        e3["bars5_loader"] = lambda code: bars_more
        e3.pop("quotes_loader", None)
        shadow.shadow_beat(now=datetime(2026, 11, 11, 10, 11, 0), **e3)
        doc = shadow.load_day_doc(root, DAY)
        sig = next(s for s in doc["signals"] if s["id"] == sig_id)
        assert sig["virtual_fill"] is not None
        assert sig["status"] == "tracking"

    def test_settle_expired_no_fill_not_judged(self, tmp_path):
        e, sig_id = self._fire(tmp_path, "c2")
        root = tts.tracker_dir(e["out_dir"])
        # 10:05 收线拉回（大幅回落出增益门）
        bars_drop = build_bars(None, 13.2) + [
            {"ts": "2026-11-11 10:05", "open": 13.2, "close": 13.05,
             "volume": 900.0, "amount": 900.0 * 13.05 * 100}]
        e2 = dict(e)
        e2["bars5_loader"] = lambda code: bars_drop
        e2.pop("quotes_loader", None)
        rep = shadow.shadow_beat(now=datetime(2026, 11, 11, 10, 6, 0), **e2)
        assert sig_id in rep["fast_settled"]
        doc = shadow.load_day_doc(root, DAY)
        sig = next(s for s in doc["signals"] if s["id"] == sig_id)
        assert sig["bar_status"] == "expired_unconfirmed"
        assert sig["status"] == "expired_unconfirmed"
        assert sig["virtual_fill"] is None                   # 不虚拟成交
        assert shadow.judged_records(doc["signals"]) == []   # 不进判定统计
        assert len(doc["signals"]) == 1                      # 留档不删除
        # 后续拍不再推进
        bars_more = bars_drop + [
            {"ts": "2026-11-11 10:10", "open": 13.0, "close": 13.0,
             "volume": 900.0, "amount": 900.0 * 13.0 * 100}]
        e3 = dict(e)
        e3["bars5_loader"] = lambda code: bars_more
        e3.pop("quotes_loader", None)
        shadow.shadow_beat(now=datetime(2026, 11, 11, 10, 11, 0), **e3)
        sig = next(s for s in shadow.load_day_doc(root, DAY)["signals"]
                   if s["id"] == sig_id)
        assert sig["virtual_fill"] is None and sig["status"] == "expired_unconfirmed"


class TestShadowFastMerge:
    def test_slow_beat_merges_into_open_fast(self, tmp_path):
        """armed 丢失场景：慢拍重新过门 → 并入 open fast 信号，不另发。"""
        now = datetime(2026, 11, 11, 10, 3, 23)
        bars = build_bars(now, 13.20)
        forming = {"ts": "2026-11-11 10:05", "open": 13.20, "close": 13.20,
                   "volume": 800.0, "amount": 800.0 * 13.25 * 100}
        bars5_map = {"002015": bars + [forming]}
        e = env(tmp_path, "m1", bars5_map,
                {"002015": {"price": 13.25, "ts": "2026-11-11 10:03:20"}})
        rep = shadow.shadow_fast_tick(now=now, **e)
        sig_id = rep["new_signal_ids"][0]
        root = tts.tracker_dir(e["out_dir"])
        # 清空 meta（模拟布防丢失）→ 慢拍重新评估
        doc = shadow.load_day_doc(root, DAY)
        doc["meta"] = {}
        shadow.save_day_doc(root, DAY, doc, now)
        # forming 未收（10:04）→ 结算不推进、保持 open；慢拍过门 → 并入
        e2 = dict(e)
        e2.pop("quotes_loader", None)
        rep2 = shadow.shadow_beat(now=datetime(2026, 11, 11, 10, 4, 0), **e2)
        assert rep2["new_signal_ids"] == []                  # 不另发
        doc = shadow.load_day_doc(root, DAY)
        assert len(doc["signals"]) == 1
        sig = doc["signals"][0]
        assert sig["id"] == sig_id
        assert sig["bar_status"] == "confirmed_bar"
        assert sig["confirm_source"] == "shadow_beat_merge"


class TestShadowFastStats:
    def test_finalize_fast_lane_stats(self, tmp_path):
        e = env(tmp_path, "f1", {"002015": build_bars(None, 13.2)}, {})
        root = tts.tracker_dir(e["out_dir"])
        doc = shadow._empty_day_doc(DAY)
        doc["signals"] = [
            {"id": "S1", "lane": "fast", "bar_status": "confirmed_bar",
             "status": "finalized", "judged": {"net_pct": 1.0, "win": True}},
            {"id": "S2", "lane": "fast", "bar_status": "expired_unconfirmed",
             "status": "expired_unconfirmed", "judged": None},
            {"id": "S3", "lane": "fast", "bar_status": "open",
             "status": "awaiting_fill", "judged": None},
            {"id": "S4", "status": "finalized",
             "judged": {"net_pct": -0.5, "win": False}},
        ]
        shadow.save_day_doc(root, DAY, doc, datetime(2026, 11, 11, 15, 16))

        def _beat(**kw):
            return {"status": "ok"}

        orig = shadow.shadow_beat
        shadow.shadow_beat = lambda **kw: {"status": "ok"}
        try:
            summary = shadow.finalize_shadow_day(
                for_date=DAY, now=datetime(2026, 11, 11, 15, 16),
                out_dir=root, holdings_loader=lambda: HOLDINGS,
                regime_loader=lambda d: REGIME_DOC,
                bars5_loader=lambda code: [])
        finally:
            shadow.shadow_beat = orig
        fl = summary["fast_lane"]
        assert fl == {"signals": 3, "confirmed_bar": 1, "expired_unconfirmed": 1,
                      "open_unresolved": 1, "hit_rate": 0.5}
        # 慢车道信号判定统计不受 fast expired 干扰（judged 只认净%已判）
        assert summary["judged_stats"]["n"] == 2
