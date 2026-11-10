# -*- coding: utf-8 -*-
"""R37 L1 半自动做T信号引擎单测（services/t_trade_signal.py）——全部纯合成数据。

覆盖任务书 20261110_l1_semi_auto_ttrade_r37.md 验收面：
  - 周期判定三态锚定（R34 §3.0 判定式）+ R34 锚值回归对拍；
  - 参数解析（两票 R34 校准带 / 其余手册派生）；
  - 触发：价带+RSI 阈（买侧/横盘上沿卖侧/下降减仓提示）、布防-去布防-重布防、
    30min 冷却去重；
  - 预检门（fail-closed）：票池 / 单票日 2 次 / 1/3 底仓整百 / 时段窗 /
    数据新鲜 10min / 下降禁买 / 上升禁新卖T；
  - 草案落盘：drafts jsonl 追加 + pending_push + id 唯一；
  - 对账：±40min+价带容差匹配、计划外如实留痕、FIFO 回合配对、跨日 T、
    费用模型、纪律判定、日终 summary；成交源不可用不造数。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import t_trade_signal as tts  # noqa: E402

DAY = date(2026, 11, 11)          # 周三（合成交易日）
STAMP = "20261111"
T_ROOT_NAME = "t_tracker_test"


# ── 合成数据工厂 ────────────────────────────────────────────────

def daily_bars(closes, high_offset=0.05, low_offset=0.05, start=date(2026, 10, 9)):
    """按收盘序列造日线（high/low = close ± offset）。"""
    out = []
    d = start
    for c in closes:
        out.append({"date": d.isoformat(), "close": float(c),
                    "high": float(c) + high_offset, "low": float(c) - low_offset})
        d += timedelta(days=1)
    return out


def minute_bars(closes, day=DAY, start_h=9, start_m=35, step=5):
    """按收盘序列造当日分时 bar（ts=周期结束时刻，东财口径）。"""
    out = []
    t = datetime.combine(day, datetime.min.time()).replace(hour=start_h, minute=start_m)
    for c in closes:
        vol, amt = 1000.0, float(c) * 1000 * 100  # 1000 手 × 100 股 → 额=价×量×100
        out.append({"ts": t.strftime("%Y-%m-%d %H:%M"), "close": float(c),
                    "volume": vol, "amount": amt})
        t += timedelta(minutes=step)
    return out


def down_series(n, end, step=0.01):
    """严格递减序列（末端=end）：RSI=0。"""
    return [round(end + step * i, 3) for i in range(n - 1, -1, -1)]


def up_series(n, end, step=0.01):
    """严格递增序列（末端=end）：RSI=100。"""
    return [round(end - step * i, 3) for i in range(n - 1, -1, -1)]


HOLDING_002015 = {"code": "002015", "name": "协鑫能科", "shares": 2000}
HOLDING_300054 = {"code": "300054", "name": "鼎龙股份", "shares": 100}
FAKE_HOLDINGS = {"holdings": [HOLDING_002015, HOLDING_300054],
                 "updated_at": "2026-11-11 09:00:00", "holding_source": "test"}


def sideways_regime_info():
    bars = daily_bars([12.5 + 0.3 * ((i * 7) % 5 - 2) for i in range(25)])
    info = tts.compute_regime(bars)
    return info


def make_regime_doc(codes_info):
    return {"schema": tts.SCHEMA_REGIME, "date": DAY.isoformat(), "status": "ok",
            "codes": codes_info}


def beat_env(tmp_path, codes_info, bars5_map, bars15_map, *, holdings=None):
    """组装 run_beat 的可注入依赖（零网络零 DB）。"""
    root = tmp_path / T_ROOT_NAME
    regime_doc = make_regime_doc(codes_info)
    return {
        "out_dir": root,
        "holdings_loader": lambda: holdings or FAKE_HOLDINGS,
        "regime_loader": lambda d: regime_doc if d == DAY else None,
        "bars5_loader": lambda code: bars5_map.get(code, []),
        "bars15_loader": lambda code: bars15_map.get(code, []),
        "snapshot_loader": lambda code: None,
    }


def info_with_params(code, regime="sideways", **overrides):
    info = {"regime": regime, "ma5": 13.0, "ma10": 12.8, "ma20": 13.0,
            "pos20": 0.5, "hi20": 14.0, "lo20": 12.0, "close": 13.0,
            "chg5d_pct": 1.0, "t1_date": "2026-11-10",
            "params": tts.resolve_params(code, regime, {"hi20": 14.0, "lo20": 12.0})}
    info.update(overrides)
    return info


# ── 周期判定（三态锚定）────────────────────────────────────────

class TestRegime:
    def test_up_full_alignment(self):
        bars = daily_bars(up_series(25, 15.0, 0.2))
        info = tts.compute_regime(bars)
        assert info["regime"] == "up"
        assert info["ma5"] > info["ma10"] > info["ma20"]
        assert info["close"] >= info["ma5"]
        assert info["pos20"] > 0.9

    def test_down_full_alignment(self):
        bars = daily_bars(down_series(25, 10.0, 0.2))
        info = tts.compute_regime(bars)
        assert info["regime"] == "down"
        assert info["ma5"] < info["ma10"] < info["ma20"]

    def test_sideways_partial_order(self):
        # 显式构造：MA5 12.9 > MA10 12.58 < MA20 12.765（反弹抬 MA5、长尾压 MA10
        # 低于 MA20）→ 金叉过渡态 → 横盘（R34：其余一切情形）
        closes = [round(13.4 - 0.1 * i, 2) for i in range(14)] + \
                 [12.3, 12.5, 12.7, 12.9, 13.1, 13.3]
        info = tts.compute_regime(daily_bars(closes))
        assert info["regime"] == "sideways"
        assert info["ma5"] > info["ma10"] and info["ma10"] < info["ma20"]

    def test_pos20_bounds_and_insufficient_bars(self):
        info = tts.compute_regime(daily_bars([13.0] * 20))
        assert 0.0 <= info["pos20"] <= 1.0
        with pytest.raises(ValueError):
            tts.compute_regime(daily_bars([13.0] * 19))

    def test_anchor_check_against_r34(self):
        bars = daily_bars(down_series(10, 12.99, 0.35) + up_series(10, 12.99, 0.35))
        # 构造不出精确锚值无妨：直接喂锚值形状的 info 验证对拍逻辑
        info = {"regime": "sideways", "ma5": 12.99, "ma10": 12.64, "ma20": 13.05,
                "pos20": 0.67}
        check = tts.check_against_r34_anchor("002015", info)
        assert check and check["within_tolerance"] and check["regime_match"]
        bad = dict(info, regime="up")
        assert tts.check_against_r34_anchor("002015", bad)["within_tolerance"] is False


# ── 参数解析 ────────────────────────────────────────────────────

class TestParams:
    def test_r34_calib_bands(self):
        p = tts.resolve_params("002015", "sideways", {})
        assert p["buy_band"] == [13.00, 13.20] and p["buy_rsi5_max"] == 35.0
        assert p["sell_band"] == [13.70, 13.90] and p["sell_rsi5_min"] == 70.0
        assert p["stop"] == 12.90 and p["source"] == tts.CALIB_SOURCE
        p2 = tts.resolve_params("300054", "up", {})
        assert p2["buy_band"] == [59.90, 60.70] and p2["buy_rsi5_max"] == 30.0
        assert p2["stop"] == 59.30

    def test_derived_sideways_from_20d_range(self):
        p = tts.resolve_params("600000", "sideways", {"hi20": 14.0, "lo20": 12.0})
        assert p["source"] == tts.DERIVED_SOURCE
        assert p["buy_band"] == [12.0, 12.3] and p["sell_band"] == [13.7, 14.0]
        assert p["buy_rsi5_max"] == 30.0 and p["buy_rsi15_max"] == 40.0

    def test_derived_up_and_down(self):
        up = tts.resolve_params("600000", "up", {})
        assert up["buy_rsi5_max"] == 30.0 and up["vwap_dev_buy_max"] == -0.005
        down = tts.resolve_params("600000", "down", {})
        assert down["reduce_rsi5_min"] == 65.0 and down["reduce_vwap_dev_min"] == 0.015


# ── 纯函数 ──────────────────────────────────────────────────────

class TestPureHelpers:
    def test_rsi_extremes(self):
        assert tts.rsi_wilder(up_series(30, 15.0)) == 100.0
        assert tts.rsi_wilder(down_series(30, 15.0)) == 0.0
        assert tts.rsi_wilder(up_series(10, 15.0)) is None  # 样本不足

    def test_day_vwap_units(self):
        bars = [{"amount": 130000.0, "volume": 100.0}]  # 13 万元 / 100 手
        assert tts.day_vwap(bars) == pytest.approx(13.0)

    def test_lot_cap(self):
        assert tts.t_lot_cap(2000) == 600
        assert tts.t_lot_cap(8300) == 2700
        assert tts.t_lot_cap(100) == 0
        assert tts.t_lot_cap(299) == 0
        assert tts.t_lot_cap(300) == 100

    def test_time_windows(self):
        assert tts.in_time_window(datetime(2026, 11, 11, 9, 35))
        assert tts.in_time_window(datetime(2026, 11, 11, 11, 30))
        assert not tts.in_time_window(datetime(2026, 11, 11, 11, 31))
        assert tts.in_time_window(datetime(2026, 11, 11, 13, 0))
        assert tts.in_time_window(datetime(2026, 11, 11, 14, 55))
        assert not tts.in_time_window(datetime(2026, 11, 11, 14, 56))

    def test_closed_bars_filters_forming_bar(self):
        bars = minute_bars([13.0, 13.1], start_h=10, start_m=0)  # ts 10:00/10:05
        now = datetime(2026, 11, 11, 10, 3)
        got = tts.closed_bars(bars, now)
        assert [b["ts"] for b in got] == ["2026-11-11 10:00"]

    def test_code_suffix(self):
        assert tts.code_with_suffix("002015") == "002015.SZ"
        assert tts.code_with_suffix("600000") == "600000.SH"
        assert tts.code_with_suffix("920002") == "920002.BJ"
        assert tts.east_secid("002015") == "0.002015"


# ── 评估：触发 / 布防 / 冷却 / 预检门 ───────────────────────────

class TestEvaluate:
    def _env(self, *, now="10:01", px=13.10, series5=None, series15=None,
             regime="sideways", code="002015", shares=2000, stop=12.90):
        now_dt = datetime.combine(DAY, datetime.strptime(now, "%H:%M").time())
        closes5 = series5 if series5 is not None else down_series(20, px)
        closes15 = series15 if series15 is not None else down_series(20, px)
        # 末根已收 bar 落在 now-1min（新鲜度门内），向前每 5min 一根
        n = len(closes5)
        last_ts = now_dt - timedelta(minutes=1)
        first_ts = last_ts - timedelta(minutes=5 * (n - 1))
        bars5 = [{"ts": (first_ts + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M"),
                  "close": c, "volume": 1000.0, "amount": c * 100000.0}
                 for i, c in enumerate(closes5)]
        bars15 = [{"ts": b["ts"], "close": c, "volume": 1000.0, "amount": c * 100000.0}
                  for b, c in zip(bars5, closes15)]
        info = info_with_params(code, regime)
        if stop is not None:
            info["params"]["stop"] = stop
        state = tts.SignalState()
        return {
            "code": code, "name": "协鑫能科", "regime_info": info,
            "params": info["params"], "bars5": bars5, "bars15": bars15,
            "px": px, "px_source": "test", "now": now_dt, "state": state,
            "drafts_today": [], "holding": {"code": code, "name": "协鑫能科", "shares": shares},
            "holdings_updated_at": "2026-11-11 09:00:00",
        }

    def test_buy_t_fires_in_band_with_low_rsi(self):
        env = self._env(px=13.10)
        res = tts.evaluate_code(**env)
        d = res["draft"]
        assert d and d["side"] == tts.SIDE_BUY and d["subtype"] == "entry"
        assert d["qty"] == 600 and d["trigger"]["rsi5"] == 0.0
        assert d["trigger"]["band"] == [13.00, 13.20]
        assert d["target_band"] == [13.70, 13.90] and d["stop"] == 12.90
        assert d["regime"] == "sideways" and d["params_source"] == tts.CALIB_SOURCE
        assert env["state"].open_t("002015")["draft_id"] == d["id"]

    def test_no_trigger_outside_band(self):
        res = tts.evaluate_code(**self._env(px=13.50))
        assert res["draft"] is None and res["suppressed"] is None

    def test_no_trigger_rsi_too_high(self):
        # 价在带内但 RSI 高（上升序列）→ 不触发
        res = tts.evaluate_code(**self._env(px=13.10, series5=up_series(20, 13.10),
                                            series15=up_series(20, 13.10)))
        assert res["draft"] is None

    def test_zone_sell_t_fires_at_upper_band(self):
        env = self._env(px=13.75, series5=up_series(20, 13.75),
                        series15=up_series(20, 13.75))
        res = tts.evaluate_code(**env)
        d = res["draft"]
        assert d and d["side"] == tts.SIDE_SELL and d["subtype"] == "zone"
        assert d["qty"] == 600 and d["trigger"]["rsi5"] == 100.0
        assert d["target_band"] == [13.00, 13.20]

    def test_up_regime_rejects_new_sell_t(self):
        # 周期×方向门（fail-closed 纵深防御，独立单元断言）：
        # 上升禁新卖T开仓（正T离场除外）；下降禁买侧草案
        assert tts._regime_side_gate("up", tts.SIDE_SELL, "zone") == (False, "up_regime_no_new_sellT")
        assert tts._regime_side_gate("up", tts.SIDE_SELL, "exit") == (True, "")
        assert tts._regime_side_gate("down", tts.SIDE_BUY, "entry") == (False, "down_regime_no_buy")
        assert tts._regime_side_gate("up", tts.SIDE_BUY, "entry") == (True, "")
        assert tts._regime_side_gate("sideways", tts.SIDE_SELL, "zone") == (True, "")
        # 集成面：上升周期即便误配卖带参数也不出新卖T草案
        env = self._env(px=13.75, series5=up_series(20, 13.75),
                        series15=up_series(20, 13.75), regime="up")
        env["params"].update({"sell_band": [13.70, 13.90], "sell_rsi5_min": 70.0})
        res = tts.evaluate_code(**env)
        assert res["draft"] is None

    def test_down_regime_no_buy_only_reduce_hint(self):
        # 下降周期：注入买带也不出买侧草案（引擎只在 up/sideways 生成买候选）
        env_buy = self._env(px=13.10, regime="down")
        env_buy["params"].update({"buy_band": [13.00, 13.20], "buy_rsi5_max": 35.0})
        res_buy = tts.evaluate_code(**env_buy)
        assert res_buy["draft"] is None
        # 反弹高 RSI+VWAP 上乖离 ≥1.5% → 只发减仓提示
        series = [12.0 + 0.09 * i for i in range(20)]  # 12.0→13.71
        env = self._env(px=13.71, series5=series, series15=series, regime="down")
        res = tts.evaluate_code(**env)
        d = res["draft"]
        assert d and d["side"] == tts.SIDE_REDUCE and d["subtype"] == "hint"
        assert d["trigger"]["vwap_dev_pct"] > 1.5

    def test_cooldown_30min_dedup(self):
        # 卖侧（横盘上沿，无在途T）：10:01 触发 → 10:06 仍在带内=未重布防抑制 →
        # 10:20 离带重布防 → 10:33 回带（32min>30min 冷却已过）→ 新草案
        env = self._env(now="10:01", px=13.75, series5=up_series(20, 13.75),
                        series15=up_series(20, 13.75))
        first = tts.evaluate_code(**env)
        assert first["draft"] and first["draft"]["side"] == tts.SIDE_SELL
        shared = env["state"]
        env2 = self._env(now="10:06", px=13.75, series5=up_series(20, 13.75),
                         series15=up_series(20, 13.75))
        env2["state"] = shared
        res2 = tts.evaluate_code(**env2)
        assert res2["draft"] is None and res2["suppressed"]["reason"] == "disarmed_wait_rearm"
        env3 = self._env(now="10:20", px=13.50)
        env3["state"] = shared
        tts.evaluate_code(**env3)
        env4 = self._env(now="10:33", px=13.75, series5=up_series(20, 13.75),
                         series15=up_series(20, 13.75))
        env4["state"] = shared
        env4["drafts_today"] = [first["draft"]]  # run_beat 内自动累积当日草案
        res4 = tts.evaluate_code(**env4)
        assert res4["draft"] and res4["draft"]["id"] != first["draft"]["id"]

    def test_cooldown_blocks_within_30min_after_rearm(self):
        # 买侧：10:01 入场 → 10:10 目标带离场（清在途T）→ 10:20 回踩买带，
        # 距上次买侧触发 19min < 30min → 冷却抑制
        env = self._env(now="10:01", px=13.10)
        assert tts.evaluate_code(**env)["draft"]
        shared = env["state"]
        exit_env = self._env(now="10:10", px=13.80, series5=up_series(20, 13.80),
                             series15=up_series(20, 13.80))
        exit_env["state"] = shared
        assert tts.evaluate_code(**exit_env)["draft"]["subtype"] == "exit"
        back = self._env(now="10:20", px=13.10)
        back["state"] = shared
        res = tts.evaluate_code(**back)
        assert res["draft"] is None
        assert res["suppressed"]["reason"] == "cooldown_30min"

    def test_daily_cap_two(self):
        env = self._env(now="10:01", px=13.10)
        d1 = tts.evaluate_code(**env)["draft"]
        draft_rows = [d1]
        # 直接预置今日已有 2 条开仓类草案 → 第三次拒
        env2 = self._env(now="10:40", px=13.10)
        env2["drafts_today"] = draft_rows + [{"code": "002015", "subtype": "zone"}]
        res2 = tts.evaluate_code(**env2)
        assert res2["draft"] is None
        assert res2["suppressed"]["reason"] == "daily_t_cap"
        assert res2["suppressed"]["cap"] == 2

    def test_lot_below_min_fail_closed(self):
        # 300054 底仓 100 股：1/3 取整百 = 0 → fail-closed（R34：需先恢复底仓）
        env = self._env(code="300054", px=60.30, shares=100, regime="up", stop=59.30)
        # 300054 up 参数：买带 59.9-60.7 RSI≤30；造低 RSI
        env["params"] = tts.resolve_params("300054", "up", {})
        res = tts.evaluate_code(**env)
        assert res["draft"] is None
        assert res["suppressed"]["reason"] == "t_lot_below_min"
        assert res["suppressed"]["base_shares"] == 100

    def test_stale_data_rejected(self):
        env = self._env(now="10:01", px=13.10)
        # 把末根 bar 时间改到 15min 前 → 新鲜度门拒
        bars = list(env["bars5"])
        last = bars[-1]
        old = (datetime.strptime(last["ts"], "%Y-%m-%d %H:%M") - timedelta(minutes=15))
        bars[-1] = {**last, "ts": old.strftime("%Y-%m-%d %H:%M")}
        env["bars5"] = bars
        res = tts.evaluate_code(**env)
        assert res["draft"] is None
        assert res["suppressed"]["reason"] == "stale_minute_data"

    def test_outside_time_window_rejected(self):
        env = self._env(now="13:40", px=13.10)  # 午后窗内 → 正常出草案
        assert tts.evaluate_code(**env)["draft"]
        env_lunch = self._env(now="11:40", px=13.10)
        res = tts.evaluate_code(**env_lunch)
        assert res["draft"] is None
        assert res["suppressed"]["reason"] == "outside_time_window"
        env_open = self._env(now="09:34", px=13.10)
        res_open = tts.evaluate_code(**env_open)
        assert res_open["suppressed"]["reason"] == "outside_time_window"

    def test_exit_paths_with_open_t(self):
        env = self._env(now="10:01", px=13.10)
        entry = tts.evaluate_code(**env)["draft"]
        state = env["state"]
        # 目标带上沿 → exit
        env2 = self._env(now="10:40", px=13.80, series5=up_series(20, 13.80),
                         series15=up_series(20, 13.80))
        env2["state"] = state
        res2 = tts.evaluate_code(**env2)
        d = res2["draft"]
        assert d and d["side"] == tts.SIDE_SELL and d["subtype"] == "exit"
        assert d["open_t_ref"] == entry["id"] and d["qty"] == 600
        assert state.open_t("002015") is None

    def test_stop_break_urgent_exit(self):
        env = self._env(now="10:01", px=13.10)
        tts.evaluate_code(**env)
        state = env["state"]
        env2 = self._env(now="10:30", px=12.85, series5=down_series(20, 12.85),
                         series15=down_series(20, 12.85))
        env2["state"] = state
        res = tts.evaluate_code(**env2)
        assert res["draft"]["subtype"] == "stop"

    def test_fallback_eod_exit(self):
        env = self._env(now="10:01", px=13.10)
        tts.evaluate_code(**env)
        state = env["state"]
        env2 = self._env(now="14:51", px=13.30)
        env2["state"] = state
        res = tts.evaluate_code(**env2)
        assert res["draft"]["subtype"] == "fallback_eod"


# ── 节拍端到端（临时目录，零网络零 DB）─────────────────────────

class TestBeatEndToEnd:
    def _providers(self, tmp_path, *, closes5, closes15, holdings=None):
        bars5 = minute_bars(closes5, start_h=9, start_m=36)
        bars15 = minute_bars(closes15, start_h=9, start_m=36)
        codes = {"002015": info_with_params("002015", "sideways")}
        return beat_env(tmp_path, codes, {"002015": bars5}, {"002015": bars15},
                        holdings=holdings)

    def test_beat_writes_draft_pending_state(self, tmp_path):
        base5 = minute_bars(down_series(20, 13.10))
        base15 = minute_bars(down_series(20, 13.10))
        now = datetime.combine(DAY, datetime.strptime("10:01", "%H:%M").time())
        env = self._providers(tmp_path, closes5=down_series(20, 13.10),
                              closes15=down_series(20, 13.10))
        env["bars5_loader"] = lambda code: _shift_last(base5, now)
        env["bars15_loader"] = lambda code: _shift_last(base15, now)
        report = tts.run_beat(now=now, **env)
        root = tmp_path / T_ROOT_NAME
        drafts = tts.read_jsonl(root / "drafts" / f"{STAMP}.jsonl")
        assert report["status"] == "ok" and len(drafts) == 1
        assert (root / "pending_push" / f"{drafts[0]['id']}.json").is_file()
        assert (root / f"state_{STAMP}.json").is_file()
        assert (root / "beats" / f"{STAMP}.jsonl").is_file()
        beats = tts.read_jsonl(root / "beats" / f"{STAMP}.jsonl")
        assert beats[0]["new_draft_ids"] == [drafts[0]["id"]]

    def test_beat_fail_closed_when_regime_missing(self, tmp_path):
        env = self._providers(tmp_path, closes5=down_series(20, 13.10),
                              closes15=down_series(20, 13.10))
        env["regime_loader"] = lambda d: None
        now = datetime.combine(DAY, datetime.strptime("10:01", "%H:%M").time())
        report = tts.run_beat(now=now, **env)
        assert report["status"] == "failure" and report["reason"] == "regime_file_missing"
        assert not (tmp_path / T_ROOT_NAME / "drafts" / f"{STAMP}.jsonl").exists()

    def test_beat_fail_closed_when_holdings_unreadable(self, tmp_path):
        env = self._providers(tmp_path, closes5=down_series(20, 13.10),
                              closes15=down_series(20, 13.10))
        def boom():
            raise RuntimeError("broker_state unreadable")
        env["holdings_loader"] = boom
        now = datetime.combine(DAY, datetime.strptime("10:01", "%H:%M").time())
        report = tts.run_beat(now=now, **env)
        assert report["status"] == "failure"
        assert "holdings_unreadable" in report["reason"]

    def test_draft_id_unique_on_appends(self, tmp_path):
        # 序列：10:01 买T入场 → 10:20 冲高入目标带（卖T离场、清 open_t）→
        # 10:40 回踩买带（冷却已过 39min）→ 第二次入场；全程 id 唯一
        base_in = minute_bars(down_series(20, 13.10))
        base_target = minute_bars(up_series(20, 13.80))
        now1 = datetime.combine(DAY, datetime.strptime("10:01", "%H:%M").time())
        now2 = now1 + timedelta(minutes=19)
        now3 = now1 + timedelta(minutes=39)
        env = self._providers(tmp_path, closes5=down_series(20, 13.10),
                              closes15=down_series(20, 13.10))
        env.pop("bars5_loader", None)
        env.pop("bars15_loader", None)

        def loader_for(target_now, bars):
            return lambda code: _shift_last(bars, target_now)

        r1 = tts.run_beat(now=now1, bars5_loader=loader_for(now1, base_in),
                          bars15_loader=loader_for(now1, base_in), **env)
        r2 = tts.run_beat(now=now2, bars5_loader=loader_for(now2, base_target),
                          bars15_loader=loader_for(now2, base_target), **env)
        r3 = tts.run_beat(now=now3, bars5_loader=loader_for(now3, base_in),
                          bars15_loader=loader_for(now3, base_in), **env)
        assert r1["new_draft_ids"] and r2["new_draft_ids"] and r3["new_draft_ids"]
        drafts = tts.read_jsonl(tmp_path / T_ROOT_NAME / "drafts" / f"{STAMP}.jsonl")
        ids = [d["id"] for d in drafts]
        subtypes = [d["subtype"] for d in drafts]
        assert subtypes == ["entry", "exit", "entry"]
        assert len(ids) == len(set(ids)) == 3


def _shift_last(bars, now):
    """把合成 bar 序列整体平移，使末根落在 now-1min（并滤掉未来时间戳）。"""
    if not bars:
        return bars
    last = datetime.strptime(bars[-1]["ts"], "%Y-%m-%d %H:%M")
    delta = (now - timedelta(minutes=1)) - last
    out = []
    for b in bars:
        ts = datetime.strptime(b["ts"], "%Y-%m-%d %H:%M") + delta
        if ts <= now:
            out.append({**b, "ts": ts.strftime("%Y-%m-%d %H:%M")})
    return out


# ── 对账 ────────────────────────────────────────────────────────

def fill_row(day, hhmmss, side, code, shares, price, source="test"):
    return {"trade_time": f"{day} {hhmmss}", "side": side, "code": code,
            "name": "协鑫能科", "shares": shares, "price": price,
            "amount": round(shares * price, 2), "source": source}


def buy_draft(ts, code="002015", qty=600, band=(13.00, 13.20)):
    return {"id": f"D-{ts}", "ts": ts, "code": code, "side": tts.SIDE_BUY,
            "subtype": "entry", "px": 13.10, "qty": qty,
            "trigger": {"band": list(band)}, "target_band": [13.70, 13.90]}


def sell_draft(ts, code="002015", qty=600, band=(13.70, 13.90)):
    return {"id": f"D-{ts}", "ts": ts, "code": code, "side": tts.SIDE_SELL,
            "subtype": "zone", "px": 13.75, "qty": qty,
            "trigger": {"band": list(band)}}


class TestReconcile:
    def _run(self, tmp_path, fills, drafts):
        loader = lambda d: (fills, "file:test")
        return tts.run_reconcile(
            for_date=DAY, now=datetime.combine(DAY, datetime.min.time()) + timedelta(hours=15, minutes=10),
            out_dir=tmp_path / T_ROOT_NAME, fills_loader=loader, drafts_rows=drafts)

    def test_full_match_round_trip_and_summary(self, tmp_path):
        drafts = [buy_draft("2026-11-11 09:48:00"), sell_draft("2026-11-11 13:01:00")]
        fills = [fill_row("2026-11-11", "09:50:00", "BUY", "002015", 600, 13.10),
                 fill_row("2026-11-11", "13:05:00", "SELL", "002015", 600, 13.75)]
        summary = self._run(tmp_path, fills, drafts)
        assert summary["t_round_trips"] == 1
        assert summary["net_pnl"] == pytest.approx(390.0 - 5.0 - 5.49, abs=0.01)
        assert summary["discipline_rate"] == 1.0
        assert summary["off_plan_count"] == 0
        assert summary["matched_draft_ids"] == ["D-2026-11-11 09:48:00",
                                                "D-2026-11-11 13:01:00"]
        ledger = tts.read_jsonl(tmp_path / T_ROOT_NAME / "ledger" / f"{STAMP}.jsonl")
        assert ledger[0]["kind"] == "round_trip" and ledger[0]["qty"] == 600

    def test_time_window_40min(self):
        d = buy_draft("2026-11-11 09:48:00")
        fill = {"trade_time": "2026-11-11 09:05:00", "side": "BUY", "code": "002015",
                "shares": 600, "price": 13.10, "amount": 7860.0}
        nf = tts.normalize_fill(fill)
        assert tts.match_fill_to_draft(nf, [d], set()) is None
        fill_ok = dict(fill, trade_time="2026-11-11 09:30:00")  # 18min → ok
        nf2 = tts.normalize_fill(fill_ok)
        assert tts.match_fill_to_draft(nf2, [d], set())["id"] == d["id"]

    def test_price_band_tolerance(self):
        d = sell_draft("2026-11-11 13:01:00")
        base = {"trade_time": "2026-11-11 13:05:00", "side": "SELL", "code": "002015",
                "shares": 600, "amount": 8400.0}
        near = tts.normalize_fill(dict(base, price=13.98))   # 带上沿 13.90 +1.5% 内
        far = tts.normalize_fill(dict(base, price=14.20))    # 超容差
        assert tts.match_fill_to_draft(near, [d], set()) is not None
        assert tts.match_fill_to_draft(far, [d], set()) is None

    def test_off_plan_fill_recorded(self, tmp_path):
        drafts = [buy_draft("2026-11-11 09:48:00")]
        fills = [fill_row("2026-11-11", "14:00:00", "SELL", "002015", 300, 13.40)]
        summary = self._run(tmp_path, fills, drafts)
        assert summary["off_plan_count"] == 1
        ledger = tts.read_jsonl(tmp_path / T_ROOT_NAME / "ledger" / f"{STAMP}.jsonl")
        assert ledger[0]["note"] == "off_plan"

    def test_cross_day_t_counts_on_exit_day(self, tmp_path):
        # R34 T3 形状：11-09 买 2000@13.44 → 11-10(本例 D-1=11-10) 两笔卖出
        # 100+1900@13.72；倒序输入（OCR 快照原序）验证时间排序 + 一草案多成交；
        # 两笔卖各成一回合行，净利合计与 R34 台账 +536.72 精确一致（买腿费用
        # 按量分摊）
        prev, cur = date(2026, 11, 9), DAY
        drafts = [sell_draft(f"{cur.isoformat()} 09:40:00", qty=600)]
        fills = [fill_row(cur.isoformat(), "09:38:58", "SELL", "002015", 1900, 13.72),
                 fill_row(cur.isoformat(), "09:38:52", "SELL", "002015", 100, 13.72),
                 fill_row(prev.isoformat(), "14:55:17", "BUY", "002015", 2000, 13.44)]
        summary = tts.run_reconcile(
            for_date=cur, now=datetime.combine(cur, datetime.min.time()) + timedelta(hours=15, minutes=10),
            out_dir=tmp_path / T_ROOT_NAME, fills_loader=lambda d: (fills, "file:test"),
            drafts_rows=drafts)
        assert summary["t_round_trips"] == 2        # 两笔卖 = 两行回合
        assert summary["fills_today"] == 2 and summary["prior_day_fills_for_pairing"] == 1
        assert summary["off_plan_count"] == 0       # 两笔卖都落在同一草案窗口内
        assert summary["matched_draft_ids"] == ["D-2026-11-11 09:40:00"]
        # 合计净利：毛利 560 − 买费 5.00 − 卖费(1372+26068)×0.000666 ≈ R34 T3 +536.72
        assert summary["net_pnl"] == pytest.approx(536.72, abs=0.05)
        # 前日买入无草案 → 纪律不通过
        assert summary["discipline_rate"] == 0.0

    def test_no_fills_source_no_fabrication(self, tmp_path):
        summary = self._run(tmp_path, [], [])
        assert summary["fills_source"] == "file:test"
        assert summary["t_round_trips"] == 0 and summary["discipline_rate"] is None

    def test_normalize_fill_rejects_bad_rows(self):
        assert tts.normalize_fill({"trade_time": "bad", "side": "BUY", "code": "1",
                                   "shares": 100, "price": 1.0}) is None
        assert tts.normalize_fill(fill_row("2026-11-11", "10:00:00", "SELL",
                                           "002015", 0, 13.0)) is None

    def test_fee_model_matches_r34_rates(self):
        assert tts._fee_estimate("BUY", 26320.0) == 5.0          # 最低佣金 5 元
        assert tts._fee_estimate("SELL", 30074.0) == pytest.approx(20.03, abs=0.05)


# ── run_regime 端到端（合成日线 + 注入持仓）─────────────────────

class TestRegimeJob:
    def test_regime_writes_file_with_params(self, tmp_path):
        bars = daily_bars(down_series(30, 12.9, 0.2))
        report = tts.run_regime(
            for_date=DAY, now=datetime.combine(DAY, datetime.min.time()) + timedelta(hours=9, minutes=25),
            out_dir=tmp_path / T_ROOT_NAME,
            holdings_loader=lambda: FAKE_HOLDINGS,
            daily_bars_loader=lambda code, d: bars if code == "002015" else daily_bars(up_series(30, 60.0, 0.3)))
        assert report["status"] == "ok"
        root = tmp_path / T_ROOT_NAME
        doc = json.loads((root / f"regime_{STAMP}.json").read_text(encoding="utf-8"))
        assert set(doc["codes"]) == {"002015", "300054"}
        assert doc["codes"]["002015"]["regime"] == "down"
        assert doc["codes"]["002015"]["params"]["reduce_rsi5_min"] == 65.0
        assert doc["codes"]["300054"]["regime"] == "up"
        assert doc["codes"]["300054"]["params"]["buy_band"] == [59.90, 60.70]

    def test_regime_failures_recorded(self, tmp_path):
        def bad_loader(code, d):
            raise RuntimeError("ch down")
        report = tts.run_regime(
            for_date=DAY, now=datetime(2026, 11, 11, 9, 25),
            out_dir=tmp_path / T_ROOT_NAME,
            holdings_loader=lambda: FAKE_HOLDINGS,
            daily_bars_loader=bad_loader)
        assert report["status"] == "failure"
        assert "002015" in report["errors"]
