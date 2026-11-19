# -*- coding: utf-8 -*-
"""L2 误卖与午休提醒修复（l2_alert_trade_closure_v1）——RED 测试批次。

任务书：l2_alert_trade_closure_v1_brief.md（PM 2026-11-16 下发）。
冻结基线（任务书 hashes，开工前已核验一致）：
  services/t_trade_signal.py   62c7d816…0909
  scripts/pipeline_scheduler.py f8c193dd…f3d9
  scripts/alert_local.py        26f13ae0…f262
  scripts/t_trade_tracker.py    f6d11bba…eb662（本批不改，护栏锁）

契约（对应任务书 A/B/C/D）：
  A 取消"建议即在途T"：
    - entry 草案不得建立 open_t（state.open_t(code) 必须保持 None）；
    - 无 fill-backed open_t 时 stop/exit/trim/fallback 一律不发草案、不带数量，
      且返回可观察抑制原因（token ∈ {no_fill_backed_open_t, trade_fact_unavailable}）；
    - 存量/旧格式 open_t（无成交血统）同样拒绝；
    - fill-backed open_t（fill_backed=True + fill 证据键）仍是合法状态机通道，不得误伤。
  B 1000 股手动补仓的诚实行为：由 A 的抑制原因承载——买入建议 ≠ 成交，
    无成交事实即无卖出数量（不得用 4000 股旧快照或建议 1300 股生成卖出）。
  C 午休提醒闸（北京时间，非宿主本地时区）：
    - 正式交易窗口 09:35–11:30、13:00–14:55（两端含端点，镜像 L1 TIME_WINDOWS）；
    - 11:35–12:55 每拍静默（不发信号，抑制原因可观察）；
    - 11:30–11:35、12:55–13:00 边界窗口不发交易信号（休盘/开盘前提示≤1 次
      为可选增强，本批仅锁"不发交易信号"下界）；
    - 09:35 前 / 14:55 后（含 15:00 后）不得发信号，不得扩大旧窗口；
    - 周末/非交易日维持调度层 no-op。
  D 弹窗按钮只确认：tk.Button 的 command 与 root.after 回调只能绑定
    self._dismiss；模块内不得出现交易导航/提交调用面。

RED/GREEN 预期（供 PM 审查）：
  预期 RED（现行代码必然失败，证明缺陷在位）：
    FillBackedOpenTTests 1/2/3/4、FastLaneIncidentTests、
    LunchGateTests 8/9/10/11/12
  预期锁定通过（现行即绿，防 GREEN 过度收紧/回归）：
    FillBackedOpenTTests 5、L2RsiSemanticTests、LunchGateTests 7、
    SchedulerNoopLockTests、AlertButtonGuardTests
  11:30:00 与 11:35:00/12:55:00 恰好端点、13:00 前 1 秒等极端边界留待
  GREEN 实现定稿后按 PM 裁决补测；本批用窗口中段时刻断言相位语义。

隔离声明：stdlib unittest；不 import runtime/THS/Win32/UIA/网络/DB/客户端，
不读 qmtmini；唯一落盘 = fast 车道用例的 unittest TemporaryDirectory
（系统临时目录，用后即删），仓库与 runtime 零写入。

运行命令（待 PM 批准后才允许执行；tests/ 无 __init__.py，故直接执行文件）：
  F:/python3.10/python.exe tests/test_l2_alert_trade_closure_v1.py -v
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services import t_trade_signal as sig  # noqa: E402
import scripts.pipeline_scheduler as sched  # noqa: E402

BJ_TZ = sig.BEIJING_TZ
UTC = timezone.utc

CODE = "002015"
DAY_STR = "2026-11-16"
# 任务书指定的两个合法抑制 token（PM 例举，封闭集合）
NO_FILL_REASONS = {"no_fill_backed_open_t", "trade_fact_unavailable"}

# R34 校准参数（services/t_trade_signal.py R34_CALIB["002015"]["sideways"] 原样）
PARAMS = {
    "buy_band": [13.00, 13.20], "buy_rsi5_max": 35.0,
    "sell_band": [13.70, 13.90], "sell_rsi5_min": 70.0,
    "exit_rsi5_min": 70.0, "stop": 12.90,
}
REGIME_INFO = {"regime": "sideways", "params": PARAMS}
# 持仓快照镜像 2026-11-16 实况：broker_state 4000 股、updated_at 停在 11-11
HOLDING = {"code": "002015.SZ", "name": "协鑫能科", "shares": 4000,
           "account": "acct1"}
HOLDINGS_STALE = {"holdings": [HOLDING], "updated_at": "2026-11-11 15:15:01"}

INCIDENT_BUY_TS = "2026-11-16 10:11:23"   # T20261116_002015_buy_45 时刻
INCIDENT_SELL_TS = "2026-11-16 10:12:23"  # T20261116_002015_sell_46 时刻


def _mk_bars(day_str: str, hhmm_list, start_px: float, steps):
    bars, px = [], start_px
    for hhmm, step in zip(hhmm_list, steps):
        px = round(px + step, 3)
        bars.append({
            "ts": f"{day_str} {hhmm}:00",
            "open": round(px + max(0.0, -step), 3), "close": px,
            "high": round(px + 0.01, 3), "low": round(px - 0.01, 3),
            "volume": 1000.0, "amount": round(px * 1000 * 100, 2),
        })
    return bars


def _bars5():
    """5m 序列：前一日 20 根 + 当日 8 根（09:35..10:10 收线）。

    交替 -0.06/+0.03 → Wilder RSI5 稳态 ≈33（锚定 11-16 实况 30.9 量级；
    夹具自检断言 18<rsi5<40，entry 能否触发由引擎 buy_rsi5_max=35 门槛实证）。
    """
    d1 = ["09:35", "09:40", "09:45", "09:50", "09:55", "10:00", "10:05",
          "10:10", "10:15", "10:20", "10:25", "10:30", "10:35", "10:40",
          "10:45", "10:50", "10:55", "11:00", "11:05", "11:10"]
    d2 = ["09:35", "09:40", "09:45", "09:50", "09:55", "10:00", "10:05",
          "10:10"]
    steps = [-0.07, 0.03] * 14
    return (_mk_bars("2026-11-13", d1, 13.70, steps)
            + _mk_bars(DAY_STR, d2, 13.34, steps))


def _bars5_fast(px_float: float):
    """fast 车道原始序列：另附 10:15 形成中 bar（intrabar 以实时价覆写 close）。"""
    return _bars5() + [{
        "ts": f"{DAY_STR} 10:15:00", "open": px_float, "close": px_float,
        "high": px_float + 0.01, "low": px_float - 0.01,
        "volume": 400.0, "amount": round(px_float * 400 * 100, 2)}]


def _bars15():
    """15m 序列：前一日 16 根（含午休断档的真实节拍）+ 当日 2 根。

    对称 ±0.03 → Wilder RSI15 ≈50（中性带，避开 _l2_rsi_candidates 警戒区
    15min RSI≥65 / ≤30 的注入干扰；夹具自检断言 31<rsi15<64）。
    """
    d1 = ["09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15",
          "11:30", "13:15", "13:30", "13:45", "14:00", "14:15", "14:30",
          "14:45", "15:00"]
    d2 = ["09:45", "10:00"]
    steps = [0.03, -0.03] * 9
    return (_mk_bars("2026-11-13", d1, 13.30, steps)
            + _mk_bars(DAY_STR, d2, 13.30, steps))


def _fixture_selfcheck(testcase):
    """夹具自检：RSI 落在设计的语义带内（不达标=夹具坏，非引擎坏）。"""
    rsi5 = sig.rsi_wilder([float(b["close"]) for b in _bars5()], 14)
    rsi15 = sig.rsi_wilder([float(b["close"]) for b in _bars15()], 14)
    testcase.assertTrue(rsi5 is not None and 18.0 < rsi5 < 40.0,
                        f"夹具 rsi5 出带: {rsi5}")
    testcase.assertTrue(rsi15 is not None and 31.0 < rsi15 < 64.0,
                        f"夹具 rsi15 出带: {rsi15}")


def _ev(px, now, state, drafts_today=(), holding=HOLDING):
    return sig.evaluate_code(
        code=CODE, name="协鑫能科", regime_info=REGIME_INFO, params=PARAMS,
        bars5=_bars5(), bars15=_bars15(), px=px, px_source="unit_test",
        now=now, state=state, drafts_today=list(drafts_today),
        holding=holding, holdings_updated_at=HOLDINGS_STALE["updated_at"])


def _ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def _l2(now, rsi15=82.0, rsi5=75.0, shares=4000):
    """L2 过热夹具（15m≥80 且 5m≥70 → sell-T）。"""
    return sig.evaluate_l2_signal(
        code=CODE, name="协鑫能科", price=13.10, price_source="unit_test",
        shares=shares, rsi15=rsi15, rsi5=rsi5, now=now, last_l2_time=None)


def _legacy_open_t():
    """旧格式 open_t（建议路径写入的形状：无任何成交血统键）。"""
    return {"draft_id": "T20261116_002015_buy_45", "entry_px": 13.10,
            "qty": 1300, "target_band": [13.70, 13.90], "stop": 12.90,
            "ts": INCIDENT_BUY_TS}


def _fill_backed_open_t():
    """fill-backed open_t（成交事实接线后的合法形状：fill_backed + fill 证据）。"""
    return {**_legacy_open_t(), "fill_backed": True,
            "fill": {"source": "fills", "id": "F20261116_002015_01",
                     "side": "BUY", "qty": 1300, "px": 13.10,
                     "ts": INCIDENT_BUY_TS}}


# ─────────────────────────────────────────────────────────────────
# A/B：建议即在途T 取消 + 无成交不生成卖出数量（含 11-16 事故黄金场景）
# ─────────────────────────────────────────────────────────────────
class FillBackedOpenTTests(unittest.TestCase):

    def setUp(self):
        _fixture_selfcheck(self)

    def test_1_entry_advice_does_not_establish_open_t(self):
        """RED：entry 草案（买入建议）不得建立 open_t（任务书 A）。"""
        state = sig.SignalState()
        res = _ev(13.10, _ts(INCIDENT_BUY_TS), state)
        self.assertIsNotNone(res.get("draft"), "entry 草案应正常发出")
        self.assertEqual(res["draft"]["subtype"], "entry")
        self.assertEqual(res["draft"]["side"], "buy-T")
        self.assertIsNone(state.open_t(CODE),
                          "买入建议不得建立 open_t（建议≠成交）")

    def test_2_incident_stop_break_after_entry_advice_suppressed_no_qty(self):
        """RED 黄金场景（11-16 事故，任务书 A+B）：

        T45 买入建议（rsi5≈30.9）后一分钟 px=12.75 ≤ stop=12.90：
        不得发出 fast_stop_break/stop 卖出草案；必须给出无成交事实的
        可观察抑制原因；抑制载荷不得携带可执行卖出数量（不得以
        4000 股旧快照或建议 1300 股替代 1000 股补仓的未核实事实）。
        """
        state = sig.SignalState()
        res1 = _ev(13.10, _ts(INCIDENT_BUY_TS), state)
        entry_draft = res1["draft"]
        self.assertEqual(entry_draft["subtype"], "entry")
        rsi5_at_entry = entry_draft["trigger"]["rsi5"]
        self.assertGreater(rsi5_at_entry, 20.0)   # 量级锚：≈30.9
        self.assertLessEqual(rsi5_at_entry, 35.0)

        res2 = _ev(12.75, _ts(INCIDENT_SELL_TS), state,
                   drafts_today=[entry_draft])
        self.assertIsNone(res2.get("draft"),
                          "无成交事实不得生成卖出草案（stop/exit/trim/fallback）")
        sup = res2.get("suppressed")
        self.assertIsNotNone(sup, "必须返回可观察抑制原因")
        self.assertIn((sup or {}).get("reason"), NO_FILL_REASONS,
                      f"抑制原因: {sup}")
        self.assertFalse((sup or {}).get("qty"),
                         "抑制载荷不得携带可执行卖出数量")

    def test_3_legacy_open_t_without_fill_provenance_stop_suppressed(self):
        """RED：存量/旧格式 open_t（无成交血统）触发 stop 同样拒绝。"""
        state = sig.SignalState()
        state.set_open_t(CODE, _legacy_open_t())
        res = _ev(12.75, _ts(INCIDENT_SELL_TS), state)
        self.assertIsNone(res.get("draft"))
        sup = res.get("suppressed")
        self.assertIsNotNone(sup)
        self.assertIn((sup or {}).get("reason"), NO_FILL_REASONS)

    def test_4_legacy_open_t_exit_band_suppressed(self):
        """RED：旧格式 open_t 的目标带命中（exit）同样拒绝（sell 家族全覆盖）。"""
        state = sig.SignalState()
        state.set_open_t(CODE, _legacy_open_t())
        res = _ev(13.80, _ts("2026-11-16 10:30:00"), state)
        self.assertIsNone(res.get("draft"))
        sup = res.get("suppressed")
        self.assertIsNotNone(sup)
        self.assertIn((sup or {}).get("reason"), NO_FILL_REASONS)

    def test_5_fill_backed_open_t_stop_draft_allowed(self):
        """LOCK（现行应绿）：fill-backed open_t 的 stop 通道不得被误伤。

        状态机后续接线接口保留：fill_backed=True + fill 证据键的 open_t
        仍可产生 stop 草案，数量取自 open_t.qty（=成交事实），带 open_t_ref。
        """
        state = sig.SignalState()
        ot = _fill_backed_open_t()
        state.set_open_t(CODE, ot)
        res = _ev(12.75, _ts(INCIDENT_SELL_TS), state)
        self.assertIsNotNone(res.get("draft"), "fill-backed 通道必须可用")
        d = res["draft"]
        self.assertEqual(d["subtype"], "stop")
        self.assertEqual(d["qty"], 1300)
        self.assertEqual(d["open_t_ref"], ot["draft_id"])


# ─────────────────────────────────────────────────────────────────
# A（fast 车道集成）：T45→T46 事故链（fast_stop_break）必须断链
# ─────────────────────────────────────────────────────────────────
class FastLaneIncidentTests(unittest.TestCase):

    def test_fast_lane_incident_chain_t45_t46(self):
        """RED：run_fast_tick 两拍复现 11-16 事故链并断链。

        拍1 10:11:23 px=13.10（买带内）→ entry fast 草案（建议，bar_status=open）；
        拍2 10:12:23 px=12.75（≤stop 12.90）→ 现行代码发出 fast_stop_break
        卖出草案（即 T46）——修复后不得出现任何 sell 侧草案。
        唯一落盘：unittest TemporaryDirectory（仓库/runtime 零写入）。
        """
        with tempfile.TemporaryDirectory(prefix="l2closure_red_") as tmp:
            out_dir = Path(tmp)
            bars15 = _bars15()

            def regime_loader(_day):
                return {"codes": {CODE: REGIME_INFO}}

            def quotes_loader(codes):
                return {CODE: {"price": _px_holder[0],
                               "ts": _px_holder[1]}}

            _px_holder = [13.10, INCIDENT_BUY_TS]

            def snapshot_loader(_code):
                return None

            tick1 = sig.run_fast_tick(
                now=_ts(INCIDENT_BUY_TS), out_dir=out_dir,
                holdings_loader=lambda: HOLDINGS_STALE,
                regime_loader=regime_loader,
                bars5_loader=lambda code: _bars5_fast(_px_holder[0]),
                bars15_loader=lambda code: bars15,
                quotes_loader=quotes_loader, snapshot_loader=snapshot_loader,
                write_pending_push=False, log=sig.logger)
            self.assertEqual(tick1.get("status"), "ok")
            buys1 = [i for i in tick1.get("new_draft_ids", []) if "_buy_" in i]
            self.assertTrue(buys1, f"拍1 应产生 entry fast 草案: {tick1}")

            _px_holder = [12.75, INCIDENT_SELL_TS]
            tick2 = sig.run_fast_tick(
                now=_ts(INCIDENT_SELL_TS), out_dir=out_dir,
                holdings_loader=lambda: HOLDINGS_STALE,
                regime_loader=regime_loader,
                bars5_loader=lambda code: _bars5_fast(_px_holder[0]),
                bars15_loader=lambda code: bars15,
                quotes_loader=quotes_loader, snapshot_loader=snapshot_loader,
                write_pending_push=False, log=sig.logger)
            self.assertEqual(tick2.get("status"), "ok")
            sells2 = [i for i in tick2.get("new_draft_ids", [])
                      if "_sell_" in i]
            self.assertEqual(
                sells2, [],
                f"拍2（px≤stop）不得再发 fast_stop_break 卖出草案: {tick2}")
            # 草案台账终审：当日文件中 sell 侧草案只能为零（T46 链断）
            drafts_file = out_dir / "drafts" / f"{date(2026, 11, 16):%Y%m%d}.jsonl"
            if drafts_file.is_file():
                rows = sig.read_jsonl(drafts_file)
                sells_all = [r for r in rows
                             if str(r.get("side", "")).startswith("sell")]
                self.assertEqual(
                    sells_all, [], f"草案台账不得含 sell 侧草案: {sells_all}")


# ─────────────────────────────────────────────────────────────────
# rsi5=30.9 语义锁定（Owner 卖出原则不包含该条件）
# ─────────────────────────────────────────────────────────────────
class L2RsiSemanticTests(unittest.TestCase):

    def test_rsi5_309_is_not_sell_condition(self):
        """LOCK：rsi5=30.9 不构成任何卖出条件（L2 卖侧需 rsi15≥80 且 rsi5≥70）。

        11-16 实况：T45 买入建议 rsi5=30.9/rsi15=18.2；该 RSI 组合下
        L2 既不买（rsi5>30）也不卖。
        """
        res = _l2(_ts(INCIDENT_BUY_TS), rsi15=18.2, rsi5=30.9)
        self.assertIsNone(res.get("signal"), f"rsi5=30.9 不得触发信号: {res}")
        self.assertIsNone(res.get("draft"))
        # 即使 15m 过热，5m=30.9 也远低于卖侧确认线 70
        res2 = _l2(_ts(INCIDENT_BUY_TS), rsi15=82.0, rsi5=30.9)
        self.assertIsNone(res2.get("signal"), f"5m RSI 30.9 不得确认卖出: {res2}")


# ─────────────────────────────────────────────────────────────────
# C：午休提醒闸（evaluate_l2_signal 时间门，北京时间）
# ─────────────────────────────────────────────────────────────────
class LunchGateTests(unittest.TestCase):

    def test_7_overheat_signal_fires_in_trading_windows(self):
        """LOCK 对照：正式窗口内（09:35–11:30 / 13:00–14:55，含端点镜像 L1）
        过热信号照常发出（不得误伤正常交易时段）。"""
        for text in ("2026-11-16 10:00:00", "2026-11-16 11:29:59",
                     "2026-11-16 13:00:00", "2026-11-16 13:30:00"):
            res = _l2(_ts(text))
            self.assertEqual(res.get("signal"), "sell-T",
                             f"{text} 应正常发出过热卖出信号: {res}")

    def test_8_lunch_silence_suppresses_every_tick(self):
        """RED：11:35–12:55 每拍静默——不发信号、草案，抑制原因可观察。"""
        for text in ("2026-11-16 11:35:00", "2026-11-16 11:36:00",
                     "2026-11-16 11:45:00", "2026-11-16 12:30:00",
                     "2026-11-16 12:54:59"):
            res = _l2(_ts(text))
            self.assertIsNone(res.get("signal"), f"{text} 午休静默期不得发信号")
            self.assertIsNone(res.get("draft"), f"{text} 午休静默期不得发草案")
            sup = res.get("suppressed")
            self.assertIsNotNone(sup, f"{text} 必须有可观察抑制原因")
            self.assertTrue(str((sup or {}).get("reason") or "").strip(),
                            f"{text} 抑制原因不得为空")

    def test_9_boundary_windows_no_trading_signal(self):
        """RED：11:30–11:35 与 12:55–13:00 边界窗口不发交易信号。

        （休盘/开盘前提示≤1 次为可选增强，本批只锁"不发交易信号"下界。）
        """
        for text in ("2026-11-16 11:31:00", "2026-11-16 11:34:59",
                     "2026-11-16 12:55:00", "2026-11-16 12:59:00"):
            res = _l2(_ts(text))
            self.assertIsNone(res.get("signal"), f"{text} 边界窗口不得发交易信号")
            self.assertIsNone(res.get("draft"))
            self.assertIsNotNone(res.get("suppressed"))

    def test_10_lunch_gate_uses_beijing_time_not_host(self):
        """RED：时间门必须按北京时间判定，不得用宿主本地无时区语义替代。

        UTC 03:40 = 北京 11:40（静默）；UTC 02:00 = 北京 10:00（正常交易）。
        """
        res_silence = _l2(datetime(2026, 11, 16, 3, 40, 0, tzinfo=UTC))
        self.assertIsNone(res_silence.get("signal"),
                          "UTC 03:40（北京 11:40）必须处于午休静默")
        self.assertIsNotNone(res_silence.get("suppressed"))
        res_trading = _l2(datetime(2026, 11, 16, 2, 0, 0, tzinfo=UTC))
        self.assertEqual(res_trading.get("signal"), "sell-T",
                         "UTC 02:00（北京 10:00）应正常发出信号")

    def test_11_preopen_and_afterclose_suppressed(self):
        """RED：09:35 前与 14:55 后（含 15:00 后旧窗口）不得发信号。"""
        for text in ("2026-11-16 09:25:00", "2026-11-16 09:30:00",
                     "2026-11-16 14:58:00", "2026-11-16 15:30:00"):
            res = _l2(_ts(text))
            self.assertIsNone(res.get("signal"), f"{text} 正式窗口外不得发信号")
            self.assertIsNotNone(res.get("suppressed"))

    def test_12_lunch_buy_signal_also_suppressed(self):
        """RED：午休静默对买入信号同样生效（超卖组合 15m≤20 且 5m≤30）。"""
        res = _l2(_ts("2026-11-16 11:40:00"), rsi15=15.0, rsi5=25.0)
        self.assertIsNone(res.get("signal"), "午休静默期买入信号同样抑制")
        self.assertIsNotNone(res.get("suppressed"))


# ─────────────────────────────────────────────────────────────────
# C：调度层 no-op 锁定（周末/窗口外维持现状，守卫在循环前返回，零睡眠）
# ─────────────────────────────────────────────────────────────────
class SchedulerNoopLockTests(unittest.TestCase):

    def test_14_weekend_noop(self):
        """LOCK：周末（2026-11-14 周六）维持 no_op_non_trading。"""
        out = sched.run_t_trade_monitor(
            mode="unit_test",
            now_fn=lambda: datetime(2026, 11, 14, 10, 0, 0, tzinfo=BJ_TZ))
        self.assertEqual(out.get("status"), "no_op_non_trading")

    def test_15_after_window_noop(self):
        """LOCK：交易日 15:30（>15:00 监控窗）维持 no_op_window。"""
        out = sched.run_t_trade_monitor(
            mode="unit_test",
            now_fn=lambda: datetime(2026, 11, 16, 15, 30, 0, tzinfo=BJ_TZ))
        self.assertEqual(out.get("status"), "no_op_window")


# ─────────────────────────────────────────────────────────────────
# D：弹窗按钮只确认（AST 静态守卫，不实例化 tkinter）
# ─────────────────────────────────────────────────────────────────
ALERT_SRC_PATH = Path(__file__).resolve().parents[1] / "scripts" / "alert_local.py"

FORBIDDEN_MODULES = {
    "webbrowser", "subprocess", "socket", "urllib", "ctypes", "requests",
    "pywinauto", "comtypes", "win32api", "win32gui", "win32com",
}
FORBIDDEN_CALL_ATTRS = {"startfile", "system", "Popen", "Invoke",
                        "click_input", "open_new", "open_new_tab"}


def _load_alert_ast():
    return ast.parse(ALERT_SRC_PATH.read_text(encoding="utf-8"))


class AlertButtonGuardTests(unittest.TestCase):

    def test_16_all_button_commands_are_dismiss_only(self):
        """LOCK：所有 tk.Button 的 command 只能绑定 self._dismiss。"""
        tree = _load_alert_ast()
        buttons = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "Button"):
                buttons.append(node)
        self.assertTrue(buttons, "未找到 tk.Button（AST 解析异常）")
        for btn in buttons:
            cmd = next((kw for kw in btn.keywords if kw.arg == "command"), None)
            cmd_val = cmd.value if cmd is not None else None
            self.assertIsNotNone(cmd, "Button 必须显式绑定 command")
            self.assertTrue(
                isinstance(cmd_val, ast.Attribute)
                and isinstance(cmd_val.value, ast.Name)
                and cmd_val.value.id == "self"
                and cmd_val.attr == "_dismiss",
                "Button command 只能是 self._dismiss，实际: "
                f"{ast.dump(cmd_val) if isinstance(cmd_val, ast.AST) else cmd_val!r}")

    def test_17_after_callbacks_are_dismiss_only(self):
        """LOCK：root.after 自动关闭回调只能绑定 self._dismiss。"""
        tree = _load_alert_ast()
        found = 0
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "after"):
                cb = node.args[1] if len(node.args) >= 2 else next(
                    (kw.value for kw in node.keywords
                     if kw.arg in ("func", "callback")), None)
                self.assertIsNotNone(cb, "after 回调缺失")
                self.assertTrue(
                    isinstance(cb, ast.Attribute)
                    and isinstance(getattr(cb, "value", None), ast.Name)
                    and getattr(getattr(cb, "value", None), "id", "") == "self"
                    and getattr(cb, "attr", "") == "_dismiss",
                    "after 回调只能是 self._dismiss: "
                    f"{ast.dump(cb) if isinstance(cb, ast.AST) else cb!r}")
                found += 1
        self.assertGreaterEqual(found, 1, "未找到 root.after（AST 解析异常）")

    def test_18_no_trade_navigation_or_submission_surface(self):
        """LOCK：模块内不得出现交易导航/提交调用面（任务书 D）。

        禁：webbrowser/subprocess/socket/urllib/ctypes/UIA 族 import；
        禁：startfile/system/Popen/Invoke/click_input 调用；
        禁：涉 xiadan 或 交易 URL 的字符串字面量。winsound（提示音）不在禁区。
        """
        tree = _load_alert_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0],
                                     FORBIDDEN_MODULES,
                                     f"禁止 import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                self.assertNotIn(root, FORBIDDEN_MODULES,
                                 f"禁止 from {node.module} import")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, FORBIDDEN_CALL_ATTRS,
                                     f"禁止调用 {node.func.attr}")
                if (isinstance(node.func, ast.Name)
                        and node.func.id in FORBIDDEN_CALL_ATTRS):
                    self.fail(f"禁止调用 {node.func.id}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                low = node.value.lower()
                self.assertNotIn("xiadan", low, "禁止出现 xiadan 入口字面量")
                if "http" in low:
                    self.fail("禁止出现 URL 字面量（交易导航面）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
