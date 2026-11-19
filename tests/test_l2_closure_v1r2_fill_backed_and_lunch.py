# -*- coding: utf-8 -*-
"""L2 误卖/午休提醒修复 R2（任务书 l2_alert_trade_closure_v1_r2）RED 测试。

覆盖三件事（先 RED，PM 批准后才能运行；GREEN 另批）：
  A. 建议不等于成交：entry/trim/exit 草案不得建立/推进/清除 open_t；
     无 fill-backed open state 时 stop/exit/trim 必须被明确抑制
     （no_open_t / no_fill_backed_open_t），不得输出可执行卖出数量。
     锚定 2026-11-16 证据：10:11:23 买入建议 T20261116_002015_buy_45（RSI5=30.9）
     → 10:12:23 stop 草案 sell_46（fast_stop_break，stop=12.9）——根因是
     买入建议建立了 open_t。Owner 手动 1000 股补仓无当日 fills 佐证，
     不得被当成已证成交。
  C. L2 monitor 午休闸（北京时间）：11:35–12:55 静默；
     11:30–11:35 与 12:55–13:00 各至多一次可观察边界提示（不发交易信号）；
     非交易日 no-op；不得扩大 15:00 之后的旧窗口。

约束：stdlib unittest、内存夹具；不 import runtime/THS/Win32/UIA/网络/DB/客户端，
不读 qmtmini 路径；唯一被 mock 的是 pipeline_scheduler._run_l2_heartbeat 与
time.sleep（避免真实 30s×60 等待与真实行情链）。
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import t_trade_signal as tts          # noqa: E402
from scripts import pipeline_scheduler as ps        # noqa: E402

DAY = date(2026, 11, 16)      # 周一（证据日；合成交易日）
CODE = "002015"
NAME = "协鑫能科"


# ── 合成数据工厂（与 tests/test_t_trade_signal.py 同构，纯内存） ──────────

def series_for_rsi(target, base=13.0, n_up=7, up=0.02, n_dn=7):
    """构造 15 根收盘使 rsi_wilder == target（代数恒等，见既有测试）。"""
    ratio = target / (100.0 - target)
    dn = (n_up * up) / (n_dn * ratio)
    closes = [float(base)]
    for _ in range(n_up):
        closes.append(closes[-1] + up)
    for _ in range(n_dn):
        closes.append(closes[-1] - dn)
    return closes


def down_series(n, end, step=0.01):
    """严格递减序列（末端=end）：RSI=0。"""
    return [round(end + step * i, 3) for i in range(n - 1, -1, -1)]


def _bars(closes, now_dt, step_min=5):
    last_ts = now_dt - timedelta(minutes=1)
    first_ts = last_ts - timedelta(minutes=step_min * (len(closes) - 1))
    return [{"ts": (first_ts + timedelta(minutes=step_min * i)).strftime("%Y-%m-%d %H:%M"),
             "close": c, "volume": 1000.0, "amount": c * 100000.0}
            for i, c in enumerate(closes)]


def make_env(*, now="10:01", px=13.10, series5=None, series15=None,
             shares=2000, regime="sideways", state=None,
             holdings_updated_at="2026-11-11 15:15:01"):
    """evaluate_code 内存夹具：002015 校准参数（buy_band[13.0,13.2]/stop 12.9）。"""
    now_dt = datetime.combine(DAY, datetime.strptime(now, "%H:%M").time())
    closes5 = series5 if series5 is not None else down_series(20, px)
    closes15 = series15 if series15 is not None else down_series(20, px)
    info = {"regime": regime, "ma5": 13.0, "ma10": 12.8, "ma20": 13.0,
            "pos20": 0.5, "hi20": 14.0, "lo20": 12.0, "close": 13.0,
            "chg5d_pct": 1.0, "t1_date": "2026-11-13",
            "params": tts.resolve_params(CODE, regime, {"hi20": 14.0, "lo20": 12.0})}
    return {
        "code": CODE, "name": NAME, "regime_info": info,
        "params": info["params"],
        "bars5": _bars(closes5, now_dt), "bars15": _bars(closes15, now_dt),
        "px": px, "px_source": "test", "now": now_dt,
        "state": state if state is not None else tts.SignalState(),
        "drafts_today": [],
        "holding": {"code": CODE, "name": NAME, "shares": shares},
        "holdings_updated_at": holdings_updated_at,
    }


def legacy_advice_open_t(draft_id="T20261116_002015_buy_45", qty=600):
    """旧缺陷语义下由建议建立的 open_t（无成交佐证字段）——2026-11-16 实况形状。"""
    return {"draft_id": draft_id, "entry_px": 13.10, "qty": qty,
            "target_band": [13.70, 13.90], "stop": 12.90,
            "ts": "2026-11-16 10:11:23"}


# ── A. 建议不等于成交（services/t_trade_signal.py） ──────────────────────

class TestAdviceIsNotFill(unittest.TestCase):

    def test_entry_advice_does_not_create_open_t(self):
        """买入建议可以发（advice），但不得建立 open_t（那是成交事实位）。"""
        env = make_env(now="10:01", px=13.10)
        res = tts.evaluate_code(**env)
        d = res["draft"]
        self.assertIsNotNone(d, "entry 建议本身应照常产出")
        self.assertEqual(d["subtype"], "entry")
        self.assertIsNone(env["state"].open_t(CODE),
                          "entry 建议不得建立 open_t（建议≠成交）")

    def test_stop_break_after_entry_advice_yields_no_sellable_qty(self):
        """11-16 事故回放：10:11 买建议 → 10:12 跌破 stop，不得再出可执行卖出。"""
        env1 = make_env(now="10:01", px=13.10)
        res1 = tts.evaluate_code(**env1)
        self.assertIsNotNone(res1["draft"])
        env2 = make_env(now="10:30", px=12.85, state=env1["state"])  # 12.85 ≤ stop 12.9
        res2 = tts.evaluate_code(**env2)
        d2 = res2["draft"]
        self.assertTrue(d2 is None or d2.get("side") != tts.SIDE_SELL,
                        "无 fill-backed open_t 时不得产出卖出草案（含可执行数量）")
        self.assertIsNone(env1["state"].open_t(CODE))

    def test_rsi5_309_alone_is_not_sell_condition(self):
        """RSI5=30.9（证据值）不构成卖出条件；买建议照发但不建立 open_t。"""
        env = make_env(now="10:11", px=13.003,
                       series5=series_for_rsi(30.9, base=13.09, up=0.01),
                       series15=series_for_rsi(18.2, base=13.09, up=0.01))
        res = tts.evaluate_code(**env)
        d = res["draft"]
        if d is not None:
            self.assertNotEqual(d.get("side"), tts.SIDE_SELL,
                                "Owner 卖出原则不含 RSI5=30.9")
        self.assertIsNone(env["state"].open_t(CODE))

    def test_legacy_advice_open_t_blocks_stop_no_fill_backed(self):
        """旧状态（建议建立、无成交佐证）驱动 stop：必须抑制，原因明确。"""
        state = tts.SignalState()
        state.set_open_t(CODE, legacy_advice_open_t())
        env = make_env(now="10:30", px=12.85, state=state)
        res = tts.evaluate_code(**env)
        self.assertIsNone(res["draft"], "无成交佐证的 open_t 不得产出卖出草案")
        self.assertIsNotNone(res["suppressed"])
        self.assertEqual(res["suppressed"]["reason"], "no_fill_backed_open_t")

    def test_fill_backed_open_t_stop_still_fires(self):
        """真实成交建立的 open_t（source=fill + fill_id）：stop 照常工作（不过度阻断）。"""
        state = tts.SignalState()
        tts.record_fill_open_t(
            state, CODE,
            draft_id="T20261116_002015_buy_45", entry_px=13.10, qty=600,
            target_band=[13.70, 13.90], stop=12.90,
            ts="2026-11-16 10:11:23", fill_id="F20261116_0001",
            fill_ts="2026-11-16 10:11:40")
        env = make_env(now="10:30", px=12.85, state=state)
        res = tts.evaluate_code(**env)
        d = res["draft"]
        self.assertIsNotNone(d)
        self.assertEqual(d["subtype"], "stop")
        self.assertEqual(d["qty"], 600)
        self.assertEqual(d["open_t_ref"], "T20261116_002015_buy_45")

    def test_record_fill_open_t_requires_fill_id(self):
        """接线接口保留但不造成交：无 fill_id / 非法数量必须 ValueError。"""
        state = tts.SignalState()
        with self.assertRaises(ValueError):
            tts.record_fill_open_t(
                state, CODE, draft_id="T_x", entry_px=13.1, qty=600,
                target_band=[13.7, 13.9], stop=12.9,
                ts="2026-11-16 10:11:23", fill_id="")
        with self.assertRaises(ValueError):
            tts.record_fill_open_t(
                state, CODE, draft_id="T_x", entry_px=13.1, qty=0,
                target_band=[13.7, 13.9], stop=12.9,
                ts="2026-11-16 10:11:23", fill_id="F1")
        ot = tts.record_fill_open_t(
            state, CODE, draft_id="T_x", entry_px=13.1, qty=600,
            target_band=[13.7, 13.9], stop=12.9,
            ts="2026-11-16 10:11:23", fill_id="F20261116_0002")
        self.assertEqual(ot["source"], "fill")
        self.assertEqual(ot["fill_id"], "F20261116_0002")

    def test_manual_add_with_stale_holdings_is_not_a_fill_fact(self):
        """B：持仓快照停在 11-11、当日无 fills——买建议只是建议，绝无 1000 股已成交断言。"""
        env = make_env(now="10:11", px=13.10,
                       holdings_updated_at="2026-11-11 15:15:01")
        res = tts.evaluate_code(**env)
        self.assertIsNotNone(res["draft"])
        self.assertEqual(res["draft"]["subtype"], "entry")
        self.assertIsNone(env["state"].open_t(CODE),
                          "旧持仓快照+无 fills 不得制造成交事实（1000 股补仓保持待核实）")
        env2 = make_env(now="10:30", px=12.85, state=env["state"],
                        holdings_updated_at="2026-11-11 15:15:01")
        res2 = tts.evaluate_code(**env2)
        d2 = res2["draft"]
        self.assertTrue(d2 is None or d2.get("side") != tts.SIDE_SELL)


# ── C. L2 午休闸（scripts/pipeline_scheduler.py，北京时间） ───────────────

def bj(hh, mm, day=DAY):
    return datetime(day.year, day.month, day.day, hh, mm)


SATURDAY = date(2026, 11, 21)


class TestL2MiddayGate(unittest.TestCase):

    def test_trading_times_run_heartbeat(self):
        """交易时段（上午 11:20 / 下午 13:00 起）不静默。"""
        for t in (bj(10, 0), bj(11, 20), bj(13, 0), bj(14, 50)):
            g = ps.l2_midday_gate(t)
            self.assertEqual(g["status"], "trading", t)
            self.assertTrue(g["run_heartbeat"], t)

    def test_midday_silent_1135_1255(self):
        """11:35–12:55 全程静默（含两端）；窗口不外扩（13:00 恢复）。"""
        t_1254_59 = datetime(2026, 11, 16, 12, 54, 59)
        for t in (bj(11, 35), bj(11, 36), bj(12, 0), bj(12, 54), t_1254_59):
            g = ps.l2_midday_gate(t)
            self.assertEqual(g["status"], "midday_silent", t)
            self.assertFalse(g["run_heartbeat"], t)
        g13 = ps.l2_midday_gate(bj(13, 0))
        self.assertEqual(g13["status"], "trading")

    def test_boundary_windows_notice_at_most_once_each(self):
        """11:30–11:35 / 12:55–13:00：不发交易信号，每窗口至多一次边界提示。"""
        notified = {}
        g1 = ps.l2_midday_gate(bj(11, 30), notified=notified)
        self.assertEqual(g1["status"], "boundary_am")
        self.assertFalse(g1["run_heartbeat"])
        self.assertTrue(g1["notice"], "首次进入上午边界窗口应提示一次")
        g2 = ps.l2_midday_gate(bj(11, 33), notified=notified)
        self.assertEqual(g2["status"], "boundary_am")
        self.assertFalse(g2["notice"], "同窗口第二次不得再提示")
        g3 = ps.l2_midday_gate(bj(12, 55), notified=notified)
        self.assertEqual(g3["status"], "boundary_pm")
        self.assertFalse(g3["run_heartbeat"])
        self.assertTrue(g3["notice"])
        g4 = ps.l2_midday_gate(bj(12, 58), notified=notified)
        self.assertFalse(g4["notice"])
        # 次日同窗口重新允许（按日期隔离）
        g5 = ps.l2_midday_gate(bj(11, 30, date(2026, 11, 17)), notified=notified)
        self.assertTrue(g5["notice"])

    def test_non_trading_day_noop(self):
        g = ps.l2_midday_gate(bj(10, 0, SATURDAY))
        self.assertEqual(g["status"], "non_trading_noop")
        self.assertFalse(g["run_heartbeat"])

    def test_trading_day_check_failure_fail_closed(self):
        def boom(d):
            raise RuntimeError("calendar down")
        g = ps.l2_midday_gate(bj(10, 0), is_trading_day=boom)
        self.assertEqual(g["status"], "trading_day_check_failed")
        self.assertFalse(g["run_heartbeat"], "日历源故障必须 fail-closed 静默")


class TestMonitorLoopMiddayGuard(unittest.TestCase):
    """run_t_trade_monitor 主循环接闸：静默窗零心跳、交易时段心跳照跑。"""

    def _run(self, at, hb):
        with mock.patch.object(ps, "_run_l2_heartbeat", hb), \
                mock.patch("time.sleep", new=lambda s: None):
            return ps.run_t_trade_monitor(
                mode="test", is_trading_day=lambda d: True,
                now_fn=lambda: at)

    def test_loop_skips_heartbeat_during_midday_silence(self):
        hb = mock.MagicMock(return_value={"signals": []})
        rep = self._run(bj(11, 40), hb)
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(hb.call_count, 0, "11:35–12:55 每拍必须静默")
        self.assertIn("midday", rep, "静默须可观察（节拍记录带 midday 汇总）")

    def test_loop_runs_heartbeat_in_trading_hours(self):
        hb = mock.MagicMock(return_value={"signals": []})
        rep = self._run(bj(10, 0), hb)
        self.assertEqual(rep["status"], "ok")
        self.assertGreater(hb.call_count, 0, "交易时段不得被误伤静默")

    def test_loop_boundary_window_no_signal_single_notice(self):
        """11:31（上午边界窗）：不发信号（零心跳），恰好一次可观察边界提示。"""
        hb = mock.MagicMock(return_value={"signals": []})
        rep = self._run(bj(11, 31), hb)
        self.assertEqual(hb.call_count, 0, "边界窗口不得发交易信号/弹窗")
        notices = rep.get("midday", {}).get("boundary_notices", [])
        self.assertEqual(notices.count("am"), 1, "至多一次且恰好一次 am 边界提示")


if __name__ == "__main__":
    unittest.main(verbosity=2)
