# -*- coding: utf-8 -*-
"""R46 做T 1 分钟快车道单测（services/t_trade_signal.py fast 面）——纯合成数据。

覆盖任务书 20261111_fast_lane_r46.md 验收面：
  - intrabar 确定性：固定序列 + 实时价 → intrabar RSI5 = rsi_wilder(收序+[px])；
    形成中 bar 检出/实时价覆盖/无形成中 bar 退化；
  - fast 触发（买带/卖带/在途T目标带/止损线）+ fast_ 前缀 + bar_status=open
    + intrabar_rsi5 实值 + pending_push 落盘；
  - RSI 类触发（exit_rsi）不进快车道（价带类限定）；
  - 布防-重布防-30min 冷却与慢车道共用同一 SignalState；
  - 预检门一致（时段窗/日 2 次/1/3 整百）；
  - 同波去重并入（slow_beat_merge：升级不另发）；收线结算
    confirmed_bar / expired_unconfirmed（草案留档行数不变）；
  - 票池并集（broker_state ∪ watchlist，去重/qty/观察票 t_lot_below_min）；
  - ulist 解析（含停牌 '-'/分块 ≤10）与分时K TTL 限频缓存；
  - 对账汇总 fast 命中率统计。
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


# ── 合成数据工厂 ────────────────────────────────────────────────

def minute_bars(closes, end_ts, step=5):
    """closes 升序；最后一根 ts=end_ts，向前每 step 分钟（合成网格跨日连续）。"""
    out = []
    t = end_ts
    for c in reversed(closes):
        out.append({"ts": t.strftime("%Y-%m-%d %H:%M"), "close": float(c),
                    "volume": 1000.0, "amount": float(c) * 1000 * 100})
        t -= timedelta(minutes=step)
    out.reverse()
    return out


def up_series(n, end, step=0.01):
    return [round(end - step * i, 3) for i in range(n - 1, -1, -1)]


def down_series(n, end, step=0.01):
    return [round(end + step * i, 3) for i in range(n - 1, -1, -1)]


HOLDING_002015 = {"code": "002015", "name": "协鑫能科", "shares": 2000}
FAKE_HOLDINGS = {"holdings": [HOLDING_002015],
                 "updated_at": "2026-11-11 09:00:00", "holding_source": "test"}

END_1000 = datetime(2026, 11, 11, 10, 0)


def info_002015_sideways():
    params = tts.resolve_params("002015", "sideways", {"hi20": 14.0, "lo20": 12.0})
    return {"regime": "sideways", "pos20": 0.67, "hi20": 14.0, "lo20": 12.0,
            "params": params}


def info_300054_up():
    params = tts.resolve_params("300054", "up", {})
    return {"regime": "up", "pos20": 0.75, "hi20": 64.0, "lo20": 58.0,
            "params": params}


def make_regime_doc(codes_info):
    return {"schema": tts.SCHEMA_REGIME, "date": DAY.isoformat(), "status": "ok",
            "codes": codes_info}


def bars15_neutral(n=40, end=13.0):
    return minute_bars([end] * n, END_1000, step=15)


def _env(tmp_path, sub, codes_info, bars5_map, bars15_map, *, holdings=None,
         quotes=None, snapshot=None):
    root = tmp_path / sub
    regime_doc = make_regime_doc(codes_info)
    env = {
        "out_dir": root,
        "holdings_loader": lambda: holdings or FAKE_HOLDINGS,
        "regime_loader": lambda d: regime_doc if d == DAY else None,
        "bars5_loader": lambda code: bars5_map.get(code, []),
        "bars15_loader": lambda code: bars15_map.get(code, []),
    }
    if quotes is not None:
        env["quotes_loader"] = lambda codes: dict(quotes)
    if snapshot is not None:
        env["snapshot_loader"] = lambda code: snapshot
    else:
        env["snapshot_loader"] = lambda code: None
    return env


def fast_env(tmp_path, codes_info, bars5_map, bars15_map, quotes, *,
             holdings=None, sub="fast"):
    return _env(tmp_path, sub, codes_info, bars5_map, bars15_map,
                holdings=holdings, quotes=quotes)


def beat_env(tmp_path, codes_info, bars5_map, bars15_map, *, holdings=None, sub="fast"):
    return _env(tmp_path, sub, codes_info, bars5_map, bars15_map, holdings=holdings)


FORMING_1005 = {"ts": "2026-11-11 10:05", "close": 13.90, "volume": 500.0,
                "amount": 500.0 * 13.8 * 100}
SELL_QUOTES = {"002015": {"price": 13.80, "ts": "2026-11-11 10:03:20"}}


def sell_bars(extra=()):
    """120 根升序（末 13.69，末根 ts=10:00）+ 可选附加 bar。"""
    return minute_bars(up_series(120, 13.69), END_1000) + list(extra)


# ── intrabar 确定性 ─────────────────────────────────────────────

class TestIntrabarDeterminism:
    def test_build_intrabar_refreshes_forming_close(self):
        now = datetime(2026, 11, 11, 10, 3, 23)
        raw = minute_bars(up_series(30, 13.5), datetime(2026, 11, 11, 10, 0))
        forming = {"ts": "2026-11-11 10:05", "close": 99.0, "volume": 300.0,
                   "amount": 300.0 * 13.5 * 100}
        bars_fast, forming_ts = tts.build_intrabar_bars5(raw + [forming], 13.80, now)
        assert forming_ts == "2026-11-11 10:05"
        assert bars_fast[-1]["close"] == 13.80          # 实时价覆盖
        assert bars_fast[-1]["volume"] == 300.0         # volume/amount 维持 partial 值
        # intrabar RSI = 收序 + 实时价 的 Wilder RSI（确定性）
        assert tts.rsi_wilder([b["close"] for b in bars_fast]) == pytest.approx(
            tts.rsi_wilder([b["close"] for b in raw] + [13.80]))

    def test_no_forming_bar_degrades_to_closed(self):
        now = datetime(2026, 11, 11, 10, 0, 30)
        raw = minute_bars(up_series(6, 13.5), datetime(2026, 11, 11, 10, 0))
        bars_fast, forming_ts = tts.build_intrabar_bars5(raw, 13.60, now)
        assert forming_ts is None
        assert [b["ts"] for b in bars_fast] == [b["ts"] for b in raw]
        assert bars_fast[-1]["close"] == 13.5           # 不覆盖已收 bar

    def test_synthetic_fast_view_quote_and_forming(self):
        now = datetime(2026, 11, 11, 10, 3, 23)
        bars5 = minute_bars(up_series(8, 13.5), datetime(2026, 11, 11, 10, 5))
        m1 = [
            {"ts": "2026-11-11 10:01", "close": 13.51, "volume": 100.0, "amount": 135100.0},
            {"ts": "2026-11-11 10:02", "close": 13.52, "volume": 200.0, "amount": 270400.0},
            {"ts": "2026-11-11 10:03", "close": 13.53, "volume": 300.0, "amount": 405900.0},
        ]
        bars, forming_ts, quote = tts.synthetic_fast_view(bars5, m1, now)
        assert forming_ts == "2026-11-11 10:05"          # start 10:00 < now < end 10:05
        assert quote == {"price": 13.53, "ts": "2026-11-11 10:03"}
        assert bars[-1]["ts"] == "2026-11-11 10:05"
        assert bars[-1]["close"] == 13.53                # quote 覆盖
        assert bars[-1]["volume"] == pytest.approx(600.0)  # 区间内 1m 聚合
        assert bars[-1]["amount"] == pytest.approx(811400.0)
        assert all(tts.parse_ts(b["ts"]) <= now for b in bars[:-1])

    def test_synthetic_fast_view_lunch_no_forming(self):
        now = datetime(2026, 11, 11, 13, 0, 23)
        bars5 = minute_bars(up_series(30, 13.5), datetime(2026, 11, 11, 13, 0))
        m1 = [{"ts": "2026-11-11 11:30", "close": 13.6, "volume": 1.0, "amount": 1360.0}]
        bars, forming_ts, quote = tts.synthetic_fast_view(bars5, m1, now)
        assert forming_ts is None                        # 13:00 处于午休，无形成中 bar
        assert quote["ts"] == "2026-11-11 11:30"


# ── fast 触发 / 布防 / 冷却 / 预检门 ─────────────────────────────

class TestFastTriggers:
    def test_fast_sell_band_draft(self, tmp_path):
        now = datetime(2026, 11, 11, 10, 3, 23)
        bars5 = sell_bars([FORMING_1005])
        env = fast_env(tmp_path, {"002015": info_002015_sideways()},
                       {"002015": bars5}, {"002015": bars15_neutral()}, SELL_QUOTES)
        rep = tts.run_fast_tick(now=now, **env)
        assert rep["status"] == "ok"
        assert len(rep["new_draft_ids"]) == 1
        d = rep["codes"]["002015"]["draft"]
        assert d["trigger"]["kind"] == "fast_sell_band"
        assert d["lane"] == "fast" and d["bar_status"] == "open"
        expected_rsi = tts.rsi_wilder([b["close"] for b in bars5[:-1]] + [13.80])
        assert d["intrabar_rsi5"] == round(expected_rsi, 1)
        assert d["px"] == 13.80 and d["px_source"] == "east_ulist"
        assert d["forming_bar_ts"] == "2026-11-11 10:05"
        assert d["fast_settle_check"] == {"kind": "sell_band", "band": [13.7, 13.9],
                                          "rsi5_min": 70.0}
        # 落盘：drafts + pending_push（协议不变）
        root = tts.tracker_dir(env["out_dir"])
        drafts = tts.read_jsonl(tts.drafts_path(root, DAY))
        assert [x["id"] for x in drafts] == [d["id"]]
        pp = root / "pending_push" / f"{d['id']}.json"
        assert pp.is_file() and json.loads(pp.read_text(encoding="utf-8"))["bar_status"] == "open"
        # fast 节拍记录独立文件，不污染慢车道 beats
        assert (root / "beats" / f"fast_{STAMP}.jsonl").is_file()
        assert not (root / "beats" / f"{STAMP}.jsonl").exists()

    def test_fast_px_fallback_chain(self, tmp_path):
        """ulist 缺价 → CH 快照（≤10min）→ 5m 收价，来源如实标注。"""
        now = datetime(2026, 11, 11, 10, 3, 23)
        info = info_002015_sideways()
        bars5 = sell_bars([FORMING_1005])
        # 1) 快照兜底
        env = _env(tmp_path, "fb1", {"002015": info}, {"002015": bars5},
                   {"002015": bars15_neutral()},
                   quotes={"300054": {"price": 61.0, "ts": "x"}},
                   snapshot={"price": 13.80, "snapshot_ts": "2026-11-11 10:03:00"})
        rep = tts.run_fast_tick(now=now, **env)
        d = rep["codes"]["002015"]["draft"]
        assert d["px_source"] == "intraday_snapshot" and d["px"] == 13.80
        # 2) 快照过期 → 5m 收价（13.69 出带 → 无草案，但来源标注可查）
        env2 = _env(tmp_path, "fb2", {"002015": info}, {"002015": bars5},
                    {"002015": bars15_neutral()},
                    quotes={}, snapshot={"price": 13.80, "snapshot_ts": "2026-11-11 09:00:00"})
        rep2 = tts.run_fast_tick(now=now, **env2)
        assert rep2["codes"]["002015"]["draft"] is None
        assert rep2["codes"]["002015"]["px_source"] == "m5_close"

    def test_fast_disarm_then_cooldown_then_rearm(self, tmp_path):
        now0 = datetime(2026, 11, 11, 10, 3, 23)
        info = info_002015_sideways()
        bars5 = sell_bars([FORMING_1005])
        env = fast_env(tmp_path, {"002015": info}, {"002015": bars5},
                       {"002015": bars15_neutral()}, SELL_QUOTES)
        root = tts.tracker_dir(env["out_dir"])
        # 1) 触发 → 布防解除
        rep1 = tts.run_fast_tick(now=now0, **env)
        assert len(rep1["new_draft_ids"]) == 1
        # 2) 下一分钟条件仍在 → disarmed_wait_rearm
        rep2 = tts.run_fast_tick(now=now0 + timedelta(minutes=1), **env)
        assert rep2["codes"]["002015"]["suppressed"]["reason"] == "disarmed_wait_rearm"
        # 3) 离带（px=13.40）→ 重布防
        quotes_out = {"002015": {"price": 13.40, "ts": "2026-11-11 10:05:20"}}
        env_out = fast_env(tmp_path, {"002015": info}, {"002015": bars5},
                           {"002015": bars15_neutral()}, quotes_out)
        rep3 = tts.run_fast_tick(now=now0 + timedelta(minutes=2), **env_out)
        st = tts.load_state(root, DAY)
        assert st.side_state("002015", "sell_zone")["armed"] is True
        # 4) 回带但 <30min → cooldown_30min（形成中 bar 已收场景）
        bars_closed = sell_bars([{"ts": "2026-11-11 10:05", "close": 13.88,
                                  "volume": 900.0, "amount": 900.0 * 13.88 * 100}])
        env_back = fast_env(tmp_path, {"002015": info}, {"002015": bars_closed},
                            {"002015": bars15_neutral()}, SELL_QUOTES)
        rep4 = tts.run_fast_tick(now=now0 + timedelta(minutes=3), **env_back)
        assert rep4["codes"]["002015"]["suppressed"]["reason"] == "cooldown_30min"
        # 5) 30min 后再触发（波间冷却语义与慢车道一致）
        bars_later = sell_bars([
            {"ts": "2026-11-11 10:30", "close": 13.75, "volume": 900.0,
             "amount": 900.0 * 13.75 * 100},
            {"ts": "2026-11-11 10:35", "close": 13.9, "volume": 900.0,
             "amount": 900.0 * 13.9 * 100}])
        env_later = fast_env(tmp_path, {"002015": info}, {"002015": bars_later},
                             {"002015": bars15_neutral()}, SELL_QUOTES)
        rep5 = tts.run_fast_tick(now=now0 + timedelta(minutes=30, seconds=1), **env_later)
        assert len(rep5["new_draft_ids"]) == 1

    def test_fast_buy_band_entry(self, tmp_path):
        """300054 上升：买带 [59.90,60.70] RSI5≤30 → fast_buy_band + open_t。"""
        closes = down_series(120, 60.20)                  # rsi=0
        bars5 = minute_bars(closes, END_1000) + [
            {"ts": "2026-11-11 10:05", "close": 60.20, "volume": 500.0,
             "amount": 500.0 * 60.2 * 100}]
        quotes = {"300054": {"price": 60.15, "ts": "2026-11-11 10:03:20"}}
        holdings = {"holdings": [{"code": "300054", "name": "鼎龙股份", "shares": 9000}],
                    "updated_at": "2026-11-11 09:00:00", "holding_source": "test"}
        env = fast_env(tmp_path, {"300054": info_300054_up()},
                       {"300054": bars5}, {"300054": bars15_neutral(end=60.0)},
                       quotes, holdings=holdings)
        rep = tts.run_fast_tick(now=datetime(2026, 11, 11, 10, 3, 23), **env)
        d = rep["codes"]["300054"]["draft"]
        assert d["trigger"]["kind"] == "fast_buy_band"
        assert d["subtype"] == "entry" and d["bar_status"] == "open"
        root = tts.tracker_dir(env["out_dir"])
        st = tts.load_state(root, DAY)
        assert st.open_t("300054")["draft_id"] == d["id"]   # open_t 与慢车道同构

    def test_fast_target_band_exit_rsi_filtered_stop_ok(self, tmp_path):
        """在途 T：目标带/止损线属价带类 → fast 草案；exit_rsi（RSI 类）不进。"""
        now = datetime(2026, 11, 11, 10, 3, 23)
        closes = up_series(120, 13.60)                     # rsi=100（同时满足 exit_rsi）
        bars5 = minute_bars(closes, END_1000) + [FORMING_1005]
        info = info_002015_sideways()

        def run(sub, px, open_t):
            env = fast_env(tmp_path, {"002015": info}, {"002015": bars5},
                           {"002015": bars15_neutral()},
                           {"002015": {"price": px, "ts": "2026-11-11 10:03:20"}},
                           sub=sub)
            root = tts.tracker_dir(env["out_dir"])
            state = tts.SignalState({})
            state.set_open_t("002015", open_t)
            tts.save_state(root, DAY, state)
            return tts.run_fast_tick(now=now, **env), root

        # 目标带触线 → fast_target_band
        rep, _ = run("t1", 13.72, {"draft_id": "T_x", "entry_px": 13.2, "qty": 600,
                                   "target_band": [13.7, 13.9], "stop": 12.9,
                                   "ts": "2026-11-11 09:40:00"})
        d = rep["codes"]["002015"]["draft"]
        assert d["trigger"]["kind"] == "fast_target_band" and d["subtype"] == "exit"
        assert d["open_t_ref"] == "T_x"
        # RSI 类：px 不在带内（12.95 > stop 12.5、RSI 极高）→ 无 fast 草案
        # （R50-L2/A1 起 rsi5≥80 布防 L2 卖侧拐点，l2_* 拐点可发——它不是
        # 价带 fast 类；本断言只拦 exit_rsi/fast_* 泄漏进快车道）
        rep2, _ = run("t2", 12.95, {"draft_id": "T_y", "entry_px": 13.2, "qty": 600,
                                    "target_band": [13.7, 13.9], "stop": 12.5,
                                    "ts": "2026-11-11 09:40:00"})
        d2 = rep2["codes"]["002015"]["draft"]
        assert d2 is None or "l2_" in d2["trigger"]["kind"]
        # 止损线：px ≤ stop → fast_stop_break
        rep3, _ = run("t3", 12.88, {"draft_id": "T_z", "entry_px": 13.2, "qty": 600,
                                    "target_band": [13.7, 13.9], "stop": 12.9,
                                    "ts": "2026-11-11 09:40:00"})
        d3 = rep3["codes"]["002015"]["draft"]
        assert d3["trigger"]["kind"] == "fast_stop_break" and d3["subtype"] == "stop"

    def test_fast_up_buy_carries_r54_tier_and_settle_spec(self, tmp_path):
        """R54 快车道 intrabar 同步：300054 up 买带 30→35 后 fast 草案带 buy_tier，
        收线结算判据 rsi5_max=35.0（快慢车道共用 resolve_params）。"""
        closes = down_series(120, 60.20)                  # rsi=0 → conviction 档
        bars5 = minute_bars(closes, END_1000) + [
            {"ts": "2026-11-11 10:05", "close": 60.20, "volume": 500.0,
             "amount": 500.0 * 60.2 * 100}]
        quotes = {"300054": {"price": 60.15, "ts": "2026-11-11 10:03:20"}}
        holdings = {"holdings": [{"code": "300054", "name": "鼎龙股份", "shares": 9000}],
                    "updated_at": "2026-11-11 09:00:00", "holding_source": "test"}
        env = fast_env(tmp_path, {"300054": info_300054_up()},
                       {"300054": bars5}, {"300054": bars15_neutral(end=60.0)},
                       quotes, holdings=holdings, sub="r54a")
        rep = tts.run_fast_tick(now=datetime(2026, 11, 11, 10, 3, 23), **env)
        d = rep["codes"]["300054"]["draft"]
        assert d["trigger"]["kind"] == "fast_buy_band"
        assert d["buy_tier"] == "conviction" and d["conviction"] is True
        assert d["fast_settle_check"]["rsi5_max"] == 35.0
        # 结算判据实证：34.999 收线成立、35.001 瓦解（R54 新边界）
        assert tts._fast_settle_holds(d["fast_settle_check"], 60.15, 34.999, None)[0] is True
        assert tts._fast_settle_holds(d["fast_settle_check"], 60.15, 35.001, None)[0] is False

    def test_fast_up_buy_normal_tier_between_20_and_35(self, tmp_path):
        """R54：intrabar RSI 落 (20,35] → normal 档标签（不分层漏标）。
        15 根闭合 bar 精确构造 RSI=30（7 涨/7 跌），形成中 bar 差分=0 不改 RSI。"""
        tail = [60.50]
        for _ in range(7):
            tail.append(tail[-1] + 0.02)
        dn = 0.02 / (30.0 / 70.0)
        for _ in range(7):
            tail.append(tail[-1] - dn)
        px = tail[-1]                                      # ≈60.31，带内
        bars5 = minute_bars(tail, END_1000) + [
            {"ts": "2026-11-11 10:05", "close": px, "volume": 500.0,
             "amount": 500.0 * px * 100}]
        quotes = {"300054": {"price": px, "ts": "2026-11-11 10:03:20"}}
        holdings = {"holdings": [{"code": "300054", "name": "鼎龙股份", "shares": 9000}],
                    "updated_at": "2026-11-11 09:00:00", "holding_source": "test"}
        env = fast_env(tmp_path, {"300054": info_300054_up()},
                       {"300054": bars5}, {"300054": bars15_neutral(end=60.0)},
                       quotes, holdings=holdings, sub="r54b")
        rep = tts.run_fast_tick(now=datetime(2026, 11, 11, 10, 3, 23), **env)
        d = rep["codes"]["300054"]["draft"]
        assert d and d["trigger"]["kind"] == "fast_buy_band"
        assert abs(d["intrabar_rsi5"] - 30.0) < 0.1
        assert d["buy_tier"] == "normal" and "conviction" not in d

    def test_fast_lane_still_excludes_rsi_class_trim(self, tmp_path):
        """R54 回归：trim/exit_rsi 仍属 RSI 类 → 不进快车道（R46 语义不动）。
        open_t 未 trim、RSI=100（≥75 trim 档）、px 不在任何带 → 无 fast 草案。"""
        now = datetime(2026, 11, 11, 10, 3, 23)
        closes = up_series(120, 60.90)                    # rsi=100；px 60.90 在买带外
        bars5 = minute_bars(closes, END_1000) + [
            {"ts": "2026-11-11 10:05", "close": 60.90, "volume": 500.0,
             "amount": 500.0 * 60.9 * 100}]
        quotes = {"300054": {"price": 60.90, "ts": "2026-11-11 10:03:20"}}
        holdings = {"holdings": [{"code": "300054", "name": "鼎龙股份", "shares": 9000}],
                    "updated_at": "2026-11-11 09:00:00", "holding_source": "test"}
        env = fast_env(tmp_path, {"300054": info_300054_up()},
                       {"300054": bars5}, {"300054": bars15_neutral(end=60.0)},
                       quotes, holdings=holdings, sub="r54c")
        root = tts.tracker_dir(env["out_dir"])
        state = tts.SignalState({})
        state.set_open_t("300054", {"draft_id": "T_r54", "entry_px": 60.2, "qty": 3000,
                                    "target_band": [63.2, 64.5], "stop": 59.3,
                                    "ts": "2026-11-11 09:40:00"})
        tts.save_state(root, DAY, state)
        rep = tts.run_fast_tick(now=now, **env)
        assert rep["codes"]["300054"]["draft"] is None
        assert "trim" not in tts.FAST_TRIGGER_KINDS and "exit_rsi" not in tts.FAST_TRIGGER_KINDS

    def test_fast_precheck_gates_identical(self, tmp_path):
        """预检门与慢车道一致：时段窗外/日 2 次/1/3 整百。"""
        info = info_002015_sideways()
        now_early = datetime(2026, 11, 11, 9, 33, 23)     # 时段窗外（09:35 起）
        bars_early = minute_bars(up_series(120, 13.69),
                                 datetime(2026, 11, 11, 9, 30)) + [
            {"ts": "2026-11-11 09:35", "close": 13.9, "volume": 500.0,
             "amount": 500.0 * 13.8 * 100}]
        env = fast_env(tmp_path, {"002015": info}, {"002015": bars_early},
                       {"002015": bars15_neutral()}, SELL_QUOTES, sub="gates")
        rep = tts.run_fast_tick(now=now_early, **env)
        assert rep["codes"]["002015"]["suppressed"]["reason"] == "outside_time_window"

        # 日 2 次上限：预置 2 条 entry/zone 草案 → daily_t_cap
        root = tts.tracker_dir(env["out_dir"])
        df = tts.drafts_path(root, DAY)
        df.parent.mkdir(parents=True, exist_ok=True)
        for i in (1, 2):
            tts._append_jsonl(df, {"id": f"T{STAMP}_002015_sell_{i:02d}",
                                   "code": "002015", "subtype": "zone",
                                   "ts": f"2026-11-11 09:4{i}:00", "side": "sell-T"})
        rep2 = tts.run_fast_tick(now=datetime(2026, 11, 11, 10, 3, 23), **env)
        assert rep2["codes"]["002015"]["suppressed"]["reason"] == "daily_t_cap"

        # 底仓 <300 → t_lot_below_min（观察票 shares=0 同理）
        holdings_small = {"holdings": [{"code": "002015", "name": "x", "shares": 200}],
                          "updated_at": "2026-11-11 09:00:00", "holding_source": "test"}
        env3 = fast_env(tmp_path, {"002015": info}, {"002015": sell_bars([FORMING_1005])},
                        {"002015": bars15_neutral()}, SELL_QUOTES,
                        holdings=holdings_small, sub="gates3")
        rep3 = tts.run_fast_tick(now=datetime(2026, 11, 11, 10, 4, 23), **env3)
        assert rep3["codes"]["002015"]["suppressed"]["reason"] == "t_lot_below_min"


# ── 收线结算：confirmed_bar / expired_unconfirmed ────────────────

class TestFastSettlement:
    def _setup(self, tmp_path, settled_close):
        """10:03 fast 触发（forming 10:05），10:06 慢 beat 收线结算。"""
        now_fire = datetime(2026, 11, 11, 10, 3, 23)
        bars5_fire = sell_bars([FORMING_1005])
        info = info_002015_sideways()
        env_fast = fast_env(tmp_path, {"002015": info}, {"002015": bars5_fire},
                            {"002015": bars15_neutral()}, SELL_QUOTES)
        rep = tts.run_fast_tick(now=now_fire, **env_fast)
        draft_id = rep["new_draft_ids"][0]
        root = tts.tracker_dir(env_fast["out_dir"])

        bars5_settled = sell_bars([{"ts": "2026-11-11 10:05", "close": settled_close,
                                    "volume": 900.0,
                                    "amount": 900.0 * settled_close * 100}])
        env_beat = beat_env(tmp_path, {"002015": info}, {"002015": bars5_settled},
                            {"002015": bars15_neutral()})
        return env_beat, root, draft_id, bars5_settled

    def test_settle_confirmed(self, tmp_path):
        env_beat, root, draft_id, bars5 = self._setup(tmp_path, 13.82)
        now_beat = datetime(2026, 11, 11, 10, 6, 0)
        rep = tts.run_beat(now=now_beat, **env_beat)
        assert rep["fast_settled"] == [draft_id]
        # 慢车道自身不另发（同组仍布防解除）
        assert rep["new_draft_ids"] == []
        drafts = tts.read_jsonl(tts.drafts_path(root, DAY))
        d = next(x for x in drafts if x["id"] == draft_id)
        assert d["bar_status"] == "confirmed_bar"
        assert d["confirm_source"] == "slow_beat_settle"
        assert d["confirm_bar_ts"] == "2026-11-11 10:05"
        expected_rsi = tts.rsi_wilder([float(b["close"]) for b in bars5])
        assert d["confirm_rsi5"] == round(expected_rsi, 1)
        # pending_push 尚未消费 → 同步重写升级
        pp = root / "pending_push" / f"{draft_id}.json"
        assert json.loads(pp.read_text(encoding="utf-8"))["bar_status"] == "confirmed_bar"

    def test_settle_expired_kept_on_record(self, tmp_path):
        env_beat, root, draft_id, _ = self._setup(tmp_path, 13.40)   # 收线拉回出带
        now_beat = datetime(2026, 11, 11, 10, 6, 0)
        rep = tts.run_beat(now=now_beat, **env_beat)
        assert rep["fast_settled"] == [draft_id]
        drafts = tts.read_jsonl(tts.drafts_path(root, DAY))
        assert len(drafts) == 1                              # 留档不删除
        d = drafts[0]
        assert d["bar_status"] == "expired_unconfirmed"
        assert d["expire_reason"] == "px_out_of_band"
        assert d["expire_bar_ts"] == "2026-11-11 10:05"

    def test_settle_waits_until_forming_bar_closed(self, tmp_path):
        """forming bar 未收（10:04 拍）→ 保持 open 不结算。"""
        env_beat, root, draft_id, _ = self._setup(tmp_path, 13.82)
        now_beat = datetime(2026, 11, 11, 10, 4, 0)          # 10:05 未到
        rep = tts.run_beat(now=now_beat, **env_beat)
        assert rep["fast_settled"] == []
        d = tts.read_jsonl(tts.drafts_path(root, DAY))[0]
        assert d["bar_status"] == "open"


# ── 同波去重并入 ────────────────────────────────────────────────

class TestFastSlowMerge:
    def test_slow_draft_merges_into_open_fast(self, tmp_path):
        """慢车道草案与近窗 open fast 同 (code,side,band) → 并入不另发。

        场景：fast 触发于形成中 bar（forming 10:05）；慢 beat 10:06 时 5m 数据
        滞后（10:05 bar 未入已收序列）→ 结算无法推进保持 open，而快照价仍在
        带内 → 慢车道评估出新草案 → 同波并入升级，不另发。
        """
        now_fire = datetime(2026, 11, 11, 10, 3, 23)
        bars5 = sell_bars([FORMING_1005])
        info = info_002015_sideways()
        env_fast = fast_env(tmp_path, {"002015": info}, {"002015": bars5},
                            {"002015": bars15_neutral()}, SELL_QUOTES)
        rep = tts.run_fast_tick(now=now_fire, **env_fast)
        assert len(rep["new_draft_ids"]) == 1
        draft_id = rep["new_draft_ids"][0]
        root = tts.tracker_dir(env_fast["out_dir"])
        assert tts.read_jsonl(tts.drafts_path(root, DAY))[0]["forming_bar_ts"] == \
            "2026-11-11 10:05"

        # 布防丢失 + 数据滞后：慢车道以快照价重新评估出同波草案
        tts.save_state(root, DAY, tts.SignalState({}))
        bars5_lag = sell_bars()                              # 无 10:05 bar（滞后）
        env_beat = _env(tmp_path, "fast", {"002015": info}, {"002015": bars5_lag},
                        {"002015": bars15_neutral()},
                        snapshot={"price": 13.82, "snapshot_ts": "2026-11-11 10:05:30"})
        rep2 = tts.run_beat(now=datetime(2026, 11, 11, 10, 6, 0), **env_beat)
        assert rep2["fast_merged"] == [draft_id]
        assert rep2["new_draft_ids"] == []                    # 不另发
        drafts = tts.read_jsonl(tts.drafts_path(root, DAY))
        assert len(drafts) == 1                               # 台账仍一条
        d = drafts[0]
        assert d["bar_status"] == "confirmed_bar"
        assert d["confirm_source"] == "slow_beat_merge"
        # 冷却从并入时刻起算（保守）
        st = tts.load_state(root, DAY)
        assert st.side_state("002015", "sell_zone")["last_fire"] == "2026-11-11 10:06:00"


# ── 票池并集 ────────────────────────────────────────────────────

class TestTPool:
    def _write_store(self, tmp_path, items):
        store = tmp_path / "wl" / "holding_tick_watchlist.json"
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps({"version": 2, "items": items,
                                     "updated_at": "2026-11-11T09:30:00+08:00"},
                                    ensure_ascii=False), encoding="utf-8")
        return store

    def test_pool_union_dedup_and_qty(self, tmp_path, monkeypatch):
        store = self._write_store(tmp_path, [
            {"code": "002015.SZ", "name": "协鑫能科", "qty": 4000, "source": "ths"},
            {"code": "300037.SZ", "name": "新宙邦", "qty": None, "source": "manual"},
            {"code": "603881.SH", "name": "数据港", "qty": 3000, "source": "manual"},
        ])
        monkeypatch.setattr(tts, "load_broker_holdings", lambda: {
            "holdings": [HOLDING_002015], "updated_at": "t", "holding_source": "test"})
        pool = tts.load_t_pool(watchlist_store=store)
        codes = [h["code"] for h in pool["holdings"]]
        assert codes == ["002015", "300037", "603881"]        # 持仓码去重不重复
        by = {h["code"]: h for h in pool["holdings"]}
        assert by["002015"]["shares"] == 2000                 # 持仓以 broker_state 为准
        assert by["300037"]["shares"] == 0                    # 未填 qty → 观察票
        assert by["603881"]["shares"] == 3000
        assert pool["pool_source"] == "broker_state+watchlist"
        assert pool["watchlist_added"] == ["300037", "603881"]

    def test_pool_watchlist_missing_degrades(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tts, "load_broker_holdings", lambda: {
            "holdings": [HOLDING_002015], "updated_at": "t", "holding_source": "test"})
        pool = tts.load_t_pool(watchlist_store=tmp_path / "nope.json")
        assert [h["code"] for h in pool["holdings"]] == ["002015"]
        assert "pool_source" not in pool

    def test_pool_broker_state_fail_closed(self, tmp_path, monkeypatch):
        def boom():
            raise RuntimeError("broker_state unreadable")
        monkeypatch.setattr(tts, "load_broker_holdings", boom)
        with pytest.raises(RuntimeError):
            tts.load_t_pool(watchlist_store=tmp_path / "nope.json")

    def test_watchlist_observer_suppressed_by_lot(self, tmp_path):
        """观察票（shares=0）参与判定但被 1/3 整百门拒发——绝不猜底仓。"""
        now = datetime(2026, 11, 11, 10, 3, 23)
        bars5 = sell_bars([FORMING_1005])
        quotes = {"300037": {"price": 13.80, "ts": "2026-11-11 10:03:20"}}
        holdings = {"holdings": [{"code": "300037", "name": "新宙邦", "shares": 0,
                                  "source": "watchlist:manual"}],
                    "updated_at": "t", "pool_source": "broker_state+watchlist",
                    "watchlist_added": ["300037"]}
        info = info_002015_sideways()                          # 同带参数复用
        env = fast_env(tmp_path, {"300037": info}, {"300037": bars5},
                       {"300037": bars15_neutral()}, quotes, holdings=holdings)
        rep = tts.run_fast_tick(now=now, **env)
        res = rep["codes"]["300037"]
        assert res["draft"] is None
        # 现行 not_in_pool 门（shares=0）先行拦截——与慢车道判定完全一致
        assert res["suppressed"]["reason"] == "not_in_pool"
        assert res["rsi5"] is not None                          # 已参与判定
        assert rep["pool"]["watchlist_added"] == ["300037"]


# ── ulist 解析 / TTL 限频缓存 / 对账 fast 统计 ───────────────────

class TestUlistAndCache:
    def _fake_open(self, payloads):
        calls = []

        class _Resp:
            def __init__(self, body):
                self._body = body.encode("utf-8")

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _open(req, timeout=10.0):
            calls.append(req.full_url)
            return _Resp(json.dumps(payloads[len(calls) - 1]))
        return _open, calls

    def test_fetch_east_ulist_quotes_parse(self):
        f124 = int(datetime(2026, 11, 11, 10, 3, 20,
                            tzinfo=tts.BEIJING_TZ).timestamp())
        payload = {"data": {"diff": [
            {"f12": "002015", "f2": 13.84, "f13": 0, "f124": f124},
            {"f12": "300054", "f2": "-", "f13": 0, "f124": f124},
        ]}}
        open_, calls = self._fake_open([payload])
        out = tts.fetch_east_ulist_quotes(["002015", "300054"], url_open=open_)
        assert set(out) == {"002015"}                      # 停牌 '-' 跳过
        assert out["002015"]["price"] == 13.84
        assert out["002015"]["ts"] == "2026-11-11 10:03:20"  # f124 → 北京时间
        assert len(calls) == 1

    def test_fetch_east_ulist_chunks_over_ten(self):
        codes = [f"3000{i:02d}" for i in range(12)]
        open_, calls = self._fake_open([{"data": {"diff": []}}] * 2)
        tts.fetch_east_ulist_quotes(codes, url_open=open_)
        assert len(calls) == 2                              # >10 码分块
        assert "300000" in calls[0] and "300011" in calls[1]

    def test_klt_ttl_cache_limits_fetch(self):
        tts.reset_klt_cache()
        fetches = []

        def fetch(code, klt):
            fetches.append((code, klt))
            return [{"ts": "2026-11-11 09:35", "close": 13.0, "volume": 1, "amount": 1}]

        bars1 = tts.cached_east_klines("002015", 5, now_ts=1000.0, fetch=fetch)
        bars2 = tts.cached_east_klines("002015", 5, now_ts=1030.0, fetch=fetch)  # TTL 内
        assert bars1 == bars2 and len(fetches) == 1
        tts.cached_east_klines("002015", 5, now_ts=1061.0, fetch=fetch)          # TTL 过
        assert len(fetches) == 2
        tts.reset_klt_cache()

    def test_fetch_east_trends_1m_parse(self):
        rows = [
            "2026-11-10 09:30,0.00,13.53,13.53,13.53,3591,4858623.00,13.530",
            "2026-11-10 09:31,13.53,13.55,13.56,13.52,1200,1626600.00,13.540",
            "bad,row",
        ]

        class _Resp:
            def read(self):
                return json.dumps(
                    {"data": {"trends": rows}}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        out = tts.fetch_east_trends_1m("002015", url_open=lambda req, timeout=10: _Resp())
        assert len(out) == 2
        assert out[0] == {"ts": "2026-11-10 09:30", "open": 13.53, "close": 13.53,
                          "volume": 3591.0, "amount": 4858623.0}
        assert out[1]["open"] == 13.53 and out[1]["close"] == 13.55

    def test_reconcile_fast_lane_stats(self, tmp_path):
        root = tmp_path / "t"
        df = tts.drafts_path(root, DAY)
        df.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"id": "a", "lane": "fast", "bar_status": "confirmed_bar", "code": "002015",
             "side": "sell-T", "ts": "2026-11-11 10:02:00", "px": 13.8, "qty": 600,
             "trigger": {"band": [13.7, 13.9]}, "confirm_source": "slow_beat_settle"},
            {"id": "b", "lane": "fast", "bar_status": "confirmed_bar", "code": "x",
             "confirm_source": "slow_beat_merge", "side": "sell-T",
             "ts": "2026-11-11 11:00:00", "px": 13.8, "qty": 600,
             "trigger": {"band": [13.7, 13.9]}},
            {"id": "c", "lane": "fast", "bar_status": "expired_unconfirmed", "code": "y",
             "side": "sell-T", "ts": "2026-11-11 13:00:00", "px": 13.8, "qty": 600,
             "trigger": {"band": [13.7, 13.9]}},
            {"id": "d", "lane": "fast", "bar_status": "open", "code": "z",
             "side": "sell-T", "ts": "2026-11-11 14:58:00", "px": 13.8, "qty": 600,
             "trigger": {"band": [13.7, 13.9]}},
        ]
        for r in rows:
            tts._append_jsonl(df, r)
        summary = tts.run_reconcile(
            for_date=DAY, now=datetime(2026, 11, 11, 15, 10), out_dir=root,
            fills_loader=lambda d: ([], "none"))
        fl = summary["fast_lane"]
        assert fl == {"drafts": 4, "confirmed_bar": 2, "expired_unconfirmed": 1,
                      "open_unresolved": 1, "merged_from_slow": 1,
                      "hit_rate": round(2 / 3, 4)}
