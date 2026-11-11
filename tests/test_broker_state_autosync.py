# -*- coding: utf-8 -*-
"""R38c broker_state 成交自动同步单测（services/broker_state_autosync + scheduler 注册）。

矩阵（任务书 task_briefs/20261110_broker_state_autosync_r38c.md）：
  - 多票同步（含当日新开仓票）；
  - 减仓清零（持仓行删除 + zeroed 记录）；
  - 当日无成交幂等（零字节改动）；
  - 失败模式 fail-closed（fills 缺失/unavailable/坏 JSON/坏行/透卖/锚点缺失/
    broker_state 不可读 → rc=1 + 告警 jsonl + 状态文件字节不变）；
  - 同日重跑/盘中分批应用幂等（applied 指纹去重，锚点全量重放）；
  - R38b 黄金回归：2026-11-10 真实 fills → 4000 股 @18.005 / 现金 54974.81；
  - pipeline_scheduler 15:15 注册与 rc 语义。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from services import broker_state_autosync as bsa  # noqa: E402
import pipeline_scheduler as ps  # noqa: E402

DAY = date(2026, 11, 10)
NOW = datetime(2026, 11, 10, 17, 30, 0)

# ---- 2026-11-10 真实成交（runtime/t_tracker/fills/20261110.json 当日 3 笔） ----
FILLS_TODAY = [
    {"trade_time": "2026-11-10 09:38:52", "side": "SELL", "code": "002015",
     "name": "协鑫能科", "shares": 100, "price": 13.72, "amount": 1372.0,
     "source": "ths_today_trades_ocr"},
    {"trade_time": "2026-11-10 09:38:58", "side": "SELL", "code": "002015",
     "name": "协鑫能科", "shares": 1900, "price": 13.72, "amount": 26068.0,
     "source": "ths_today_trades_ocr"},
    {"trade_time": "2026-11-10 14:55:13", "side": "BUY", "code": "002015",
     "name": "协鑫能科", "shares": 2000, "price": 13.63, "amount": 27260.0,
     "source": "ths_today_trades_ocr"},
]
HISTORY_ROW = {"trade_time": "2026-11-09 14:55:17", "side": "BUY", "code": "002015",
               "name": "协鑫能科", "shares": 2000, "price": 13.44, "amount": 26880.0,
               "source": "ths_history_trades_ocr"}

# ---- R38b 锚点（期初=2026-11-09 收盘持仓与现金；见 COORDINATION_R38B） ----
INIT_BASE = {
    "base_as_of": "2026-11-09",
    "base": {
        "002015": {"shares": 4000, "cost_price": 22.377},
        "300054": {"shares": 100, "cost_price": 182.595},
    },
    "base_cash_available": 54818.15,
}


def _holding(code, **over):
    row = {"code": code, "name": "占位", "shares": 0, "available_shares": 0,
           "cost_price": 1.0, "current_price": 1.0, "market_value": 0.0,
           "pnl_ratio": 0.0, "position_pct": 0.0, "entry_price": 1.0,
           "reference_close": 1.0, "hard_stop": 1.0, "take_profit_1": 1.0,
           "route": "broker_real_position", "route_label": "同花顺真实持仓",
           "trade_status": "broker_open", "management_action": "按G3合同检查退出",
           "exit_contract": "真实持仓同步自同花顺", "source": "ths_ocr_snapshot"}
    row.update(over)
    return row


def _state_r38b_final() -> dict:
    """当前正式 broker_state 的测试镜像（R38b 17:14 手工刷新后的值）。"""
    return {
        "holdings": [
            _holding("002015", name="协鑫能科", shares=4000, available_shares=4000,
                     cost_price=18.005, current_price=13.64, market_value=54560.0,
                     pnl_ratio=-24.24, position_pct=0.4713, entry_price=18.005,
                     reference_close=13.64, hard_stop=15.844, take_profit_1=20.165,
                     source="ths_fills_r38b"),
            _holding("300054", name="鼎龙股份", shares=100, available_shares=100,
                     cost_price=182.595, current_price=61.33, market_value=6133.0,
                     pnl_ratio=-66.41, position_pct=0.053, entry_price=182.595,
                     reference_close=61.33, hard_stop=160.684, take_profit_1=204.506,
                     source="ths_ocr_snapshot"),
        ],
        "capital": {
            "fund_balance": 27653.63, "available_cash": 54974.81,
            "withdrawable_cash": 27553.63, "frozen_cash": 100.0,
            "market_value": 60693.0, "total_capital": 115767.81,
            "holding_market_value": 60693.0, "cost_value": None,
            "holdings_stale": False, "available_cash_derived": True,
            "available_with_holdings": 115667.81, "holding_pnl": -29585.57,
            "day_pnl": 1059.66, "day_pnl_ratio_pct": 0.92,
        },
        "broker_trades": [{"trade_time": "2026-06-18 13:11:36", "code": "300037"}],
        "updated_at": "2026-11-10 17:14:25",
        "trade_updated_at": "2026-09-14 17:30:01",
        "holding_source": "ths_fills_r38b",
        "fallback_cache": False,
        "sync_message": "R38b 手工刷新",
        "trade_source": "qmtmini_readonly_snapshot",
        "capital_only": False,
        "broker_account_id": "8890729932",
        "qmtmini_snapshot": None,
    }


class Env:
    """临时目录：t_tracker 根（fills/alerts/report）+ broker_state 文件。"""

    def __init__(self, tmp_path: Path, state: dict, fills_rows=None, status="ok",
                 fills_date="20261110", init_base=None):
        self.root = tmp_path
        self.tracker = tmp_path / "t_tracker"
        (self.tracker / "fills").mkdir(parents=True)
        self.state_file = tmp_path / "broker_state.json"
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        self.fills_file = None
        if fills_rows is not None:
            self.fills_file = self.tracker / "fills" / f"{fills_date}.json"
            self.fills_file.write_text(json.dumps(
                {"schema": "ths_fills_export/1", "status": status,
                 "trade_date": "2026-11-10", "fills": fills_rows},
                ensure_ascii=False, indent=1), encoding="utf-8")
        self.alerts_file = self.tracker / bsa.ALERTS_FILENAME
        self.report_file = self.tracker / bsa.REPORT_DIRNAME / "20261110.json"
        self.history_file = (self.tracker / bsa.REPORT_DIRNAME
                             / "history.jsonl")
        self.init_base_file = None
        if init_base is not None:
            self.init_base_file = tmp_path / "init_base.json"
            self.init_base_file.write_text(json.dumps(init_base, ensure_ascii=False),
                                           encoding="utf-8")

    def run(self, **kw):
        init = None
        if self.init_base_file is not None:
            init = json.loads(self.init_base_file.read_text(encoding="utf-8"))
            if not kw.pop("keep_init", False):
                self.init_base_file = None  # 只在首跑注入
        return bsa.run_sync(
            state_file=self.state_file, fills_file=self.fills_file,
            alerts_file=self.alerts_file, report_file=self.report_file,
            history_file=self.history_file, for_date=DAY, now=NOW,
            init_base=init, **kw)

    def state(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def sha(self) -> str:
        return hashlib.sha256(self.state_file.read_bytes()).hexdigest()

    def alerts(self) -> list:
        if not self.alerts_file.is_file():
            return []
        return [json.loads(x) for x in
                self.alerts_file.read_text(encoding="utf-8").splitlines() if x.strip()]

    def holding(self, code):
        for row in self.state()["holdings"]:
            if row["code"] == code:
                return row
        return None


# ---------------------------------------------------------------------------
# 1. 黄金回归：真实 11-10 fills → 与 R38b 手工值全字段一致
# ---------------------------------------------------------------------------

class TestGoldenR38b:
    def test_real_fills_reproduce_r38b_values(self, tmp_path):
        env = Env(tmp_path, _state_r38b_final(), fills_rows=FILLS_TODAY,
                  init_base=INIT_BASE)
        res = env.run()
        assert res["rc"] == 0 and res["action"] == "write"
        h = env.holding("002015")
        assert h["shares"] == 4000 and h["available_shares"] == 4000
        assert h["cost_price"] == 18.005 and h["entry_price"] == 18.005
        assert h["hard_stop"] == 15.844 and h["take_profit_1"] == 20.165
        assert h["market_value"] == 54560.0 and h["pnl_ratio"] == -24.24
        assert h["position_pct"] == 0.4713 and h["source"] == "ths_fills_autosync"
        h3 = env.holding("300054")   # 无成交票：仅派生字段随总资产重算
        assert h3["shares"] == 100 and h3["cost_price"] == 182.595
        assert h3["source"] == "ths_ocr_snapshot" and h3["pnl_ratio"] == -66.41
        cap = env.state()["capital"]
        assert cap["available_cash"] == 54974.81
        assert cap["market_value"] == 60693.0
        assert cap["total_capital"] == 115767.81
        assert cap["available_with_holdings"] == 115667.81
        assert cap["holding_pnl"] == -29585.57
        assert cap["available_cash_derived"] is True
        assert cap["fund_balance"] == 27653.63      # 非派生键原样保留
        st = env.state()
        assert st["holding_source"] == "ths_fills_autosync"
        assert st["updated_at"] == "2026-11-10 17:30:00"
        assert st["broker_account_id"] == "8890729932"   # 结构键保留
        assert st["broker_trades"][0]["code"] == "300037"
        fs = st["fills_sync"]
        assert fs["schema"] == "broker_fills_sync/1" and len(fs["applied"]) == 3
        assert fs["cash_delta"] == 156.66
        assert fs["base"]["002015"]["cost_price"] == 22.377   # 锚点不动

    def test_fees_match_r38b_estimate(self):
        # 买 27260 → 费 5.07（R38b 手工同值）；卖 1372/26068 → 0.91/17.36
        from services.t_trade_signal import _fee_estimate
        assert _fee_estimate("BUY", 27260.0) == 5.07
        assert round(_fee_estimate("SELL", 1372.0), 2) == 0.91
        assert round(_fee_estimate("SELL", 26068.0), 2) == 17.36


# ---------------------------------------------------------------------------
# 2. 多票（含当日新开仓）
# ---------------------------------------------------------------------------

class TestMultiCode:
    def test_second_code_new_position(self, tmp_path):
        rows = FILLS_TODAY + [
            {"trade_time": "2026-11-10 10:00:00", "side": "BUY", "code": "600000",
             "name": "浦发银行", "shares": 1000, "price": 10.0, "amount": 10000.0,
             "source": "ths_today_trades_ocr"},
        ]
        env = Env(tmp_path, _state_r38b_final(), fills_rows=rows, init_base=INIT_BASE)
        res = env.run()
        assert res["rc"] == 0
        h = env.holding("600000")
        assert h is not None and h["name"] == "浦发银行"
        assert h["shares"] == 1000
        assert h["cost_price"] == 10.005          # (10000+max(5,1.86))/1000
        assert h["source"] == "ths_fills_autosync"
        assert env.holding("002015")["shares"] == 4000

    def test_second_code_reduce(self, tmp_path):
        base = json.loads(json.dumps(INIT_BASE))
        base["base"]["600000"] = {"shares": 500, "cost_price": 9.9}
        rows = FILLS_TODAY + [
            {"trade_time": "2026-11-10 10:00:00", "side": "SELL", "code": "600000",
             "name": "浦发银行", "shares": 200, "price": 10.2, "amount": 2040.0,
             "source": "ths_today_trades_ocr"},
        ]
        state = _state_r38b_final()
        state["holdings"].append(_holding("600000", name="浦发银行", shares=500,
                                          cost_price=9.9, current_price=10.2,
                                          market_value=5100.0))
        env = Env(tmp_path, state, fills_rows=rows, init_base=base)
        assert env.run()["rc"] == 0
        h = env.holding("600000")
        assert h["shares"] == 300 and h["cost_price"] == 9.9   # 卖出不改成本
        assert h["market_value"] == 3060.0


# ---------------------------------------------------------------------------
# 3. 减仓清零
# ---------------------------------------------------------------------------

class TestSellToZero:
    def test_zeroed_holding_dropped(self, tmp_path):
        base = {"base_as_of": "2026-11-09",
                "base": {"002015": {"shares": 2000, "cost_price": 22.377},
                         "300054": {"shares": 100, "cost_price": 182.595}},
                "base_cash_available": 54818.15}
        rows = [{"trade_time": "2026-11-10 14:00:00", "side": "SELL", "code": "002015",
                 "name": "协鑫能科", "shares": 2000, "price": 13.72, "amount": 27440.0,
                 "source": "ths_today_trades_ocr"}]
        env = Env(tmp_path, _state_r38b_final(), fills_rows=rows, init_base=base)
        res = env.run()
        assert res["rc"] == 0
        assert env.holding("002015") is None            # 清零 → 移除持仓行
        assert env.holding("300054") is not None
        # 54818.15 + (27440 − 18.28) = 82239.87
        assert env.state()["capital"]["available_cash"] == 82239.87
        assert env.state()["fills_sync"]["zeroed"] == ["002015"]
        assert env.state()["fills_sync"]["base"]["002015"]["shares"] == 2000  # 锚点不动


# ---------------------------------------------------------------------------
# 4. 当日无成交 / 同日重跑：幂等零改动
# ---------------------------------------------------------------------------

class TestIdempotent:
    def _env_prepared(self, tmp_path):
        env = Env(tmp_path, _state_r38b_final(), fills_rows=FILLS_TODAY,
                  init_base=INIT_BASE)
        assert env.run()["rc"] == 0
        return env

    def test_no_trade_day_zero_byte_change(self, tmp_path):
        # 真实无成交晨间态：持仓=锚点值（4000@22.377），fills 文件仅含历史行
        # R49 语义更新：首次接入当日无成交 → 锚点基线物化落盘（write），
        # 其后无成交轮次 noop 零字节改动（幂等不重放不双计）。
        state = _state_r38b_final()
        state["holdings"][0].update({"shares": 4000, "cost_price": 22.377,
                                     "entry_price": 22.377})
        state["capital"]["available_cash"] = 54818.15
        env = Env(tmp_path, state, fills_rows=[HISTORY_ROW], init_base=INIT_BASE)
        res1 = env.run()                            # init 首跑一次性注入
        assert res1["rc"] == 0 and res1["action"] == "write"  # 基线落盘
        sha1 = env.sha()
        res2 = env.run()
        assert res2["rc"] == 0 and res2["action"] == "noop"
        assert env.sha() == sha1                      # 零字节改动
        res3 = env.run()
        assert res3["rc"] == 0 and res3["action"] == "noop"
        assert env.sha() == sha1
        assert env.alerts() == []                     # 无成交不告警

    def test_rerun_same_day_no_double_count(self, tmp_path):
        env = self._env_prepared(tmp_path)
        sha1 = env.sha()
        res = env.run()                               # 同一份 fills 再跑
        assert res["rc"] == 0 and res["action"] == "noop"
        assert env.sha() == sha1
        h = env.holding("002015")
        assert h["shares"] == 4000 and h["cost_price"] == 18.005

    def test_partial_then_full_fills_file(self, tmp_path):
        """盘中先只有上午卖（写中间态），收盘文件补全后只增量应用下午买。"""
        env = Env(tmp_path, _state_r38b_final(), fills_rows=FILLS_TODAY[:2],
                  init_base=INIT_BASE)
        res1 = env.run()
        assert res1["rc"] == 0
        h1 = env.holding("002015")
        assert h1["shares"] == 2000 and h1["cost_price"] == 22.377
        assert env.state()["capital"]["available_cash"] == 82239.88
        # 导出器 15:05 重产全日文件（补下午买）
        env.fills_file.write_text(json.dumps(
            {"schema": "ths_fills_export/1", "status": "ok", "fills": FILLS_TODAY},
            ensure_ascii=False), encoding="utf-8")
        res2 = env.run()
        assert res2["rc"] == 0 and res2["action"] == "write"
        h2 = env.holding("002015")
        assert h2["shares"] == 4000 and h2["cost_price"] == 18.005
        assert env.state()["capital"]["available_cash"] == 54974.81
        assert len(env.state()["fills_sync"]["applied"]) == 3

    def test_hand_edit_after_sync_reports_drift_but_no_write(self, tmp_path):
        env = self._env_prepared(tmp_path)
        state = env.state()
        state["holdings"][0]["cost_price"] = 19.999    # 手工改动
        env.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        sha = env.sha()
        res = env.run()                                # 无新 fills
        assert res["rc"] == 0 and res["action"] == "noop"
        assert env.sha() == sha                        # 手工值不被覆盖
        assert env.alerts()[-1]["reason"] == "state_drift"


# ---------------------------------------------------------------------------
# 5. 失败模式：fail-closed 零改动 + 告警
# ---------------------------------------------------------------------------

class TestFailClosed:
    def _assert_refused(self, env, reason):
        sha0 = env.sha()
        res = env.run()
        assert res["rc"] == 1, res
        assert env.sha() == sha0                       # 状态文件字节不变
        alerts = env.alerts()
        assert alerts and alerts[-1]["reason"] == reason
        return res

    def test_fills_missing(self, tmp_path):
        env = Env(tmp_path, _state_r38b_final(), fills_rows=None, init_base=INIT_BASE)
        self._assert_refused(env, "fills_missing")

    def test_fills_status_not_ok(self, tmp_path):
        env = Env(tmp_path, _state_r38b_final(), fills_rows=FILLS_TODAY,
                  status="unavailable", init_base=INIT_BASE)
        self._assert_refused(env, "fills_unavailable")

    def test_fills_bad_json(self, tmp_path):
        env = Env(tmp_path, _state_r38b_final(), fills_rows=FILLS_TODAY,
                  init_base=INIT_BASE)
        env.fills_file.write_text('{"schema": "ths_fills_export/1", "status": "ok", '
                                  '"fills": [ BROKEN', encoding="utf-8")
        self._assert_refused(env, "fills_parse_error")

    def test_fills_bad_row(self, tmp_path):
        rows = FILLS_TODAY + [{"trade_time": "2026-11-10 10:00:00", "side": "BUY",
                               "code": "600000", "shares": 0, "price": 10.0}]
        env = Env(tmp_path, _state_r38b_final(), fills_rows=rows, init_base=INIT_BASE)
        self._assert_refused(env, "fills_row_invalid")

    def test_oversell(self, tmp_path):
        rows = [{"trade_time": "2026-11-10 10:00:00", "side": "SELL", "code": "002015",
                 "shares": 99000, "price": 13.0, "amount": 1287000.0}]
        env = Env(tmp_path, _state_r38b_final(), fills_rows=rows, init_base=INIT_BASE)
        self._assert_refused(env, "oversell")

    def test_base_missing(self, tmp_path):
        env = Env(tmp_path, _state_r38b_final(), fills_rows=FILLS_TODAY)
        self._assert_refused(env, "base_missing")

    def test_state_unreadable(self, tmp_path):
        env = Env(tmp_path, _state_r38b_final(), fills_rows=FILLS_TODAY,
                  init_base=INIT_BASE)
        env.state_file.write_text("{ broken", encoding="utf-8")
        res = env.run()
        assert res["rc"] == 1 and res["reason"] == "broker_state_unreadable"
        assert env.state_file.read_text(encoding="utf-8") == "{ broken"

    def test_init_conflict_refused(self, tmp_path):
        env = Env(tmp_path, _state_r38b_final(), fills_rows=FILLS_TODAY,
                  init_base=INIT_BASE)
        assert env.run()["rc"] == 0                    # 首跑 init+sync
        env.init_base_file = env.root / "init2.json"
        env.init_base_file.write_text(json.dumps(INIT_BASE), encoding="utf-8")
        self._assert_refused(env, "init_conflict")

    def test_dry_run_check_no_write(self, tmp_path):
        env = Env(tmp_path, _state_r38b_final(), fills_rows=FILLS_TODAY,
                  init_base=INIT_BASE)
        sha0 = env.sha()
        res = env.run(dry_run=True)
        assert res["rc"] == 0 and res["action"] == "check"
        assert env.sha() == sha0
        assert res["plan"]["holdings"]["002015"]["shares"] == [4000, 4000]


# ---------------------------------------------------------------------------
# 6. scheduler 注册（pipeline_scheduler 15:15）
# ---------------------------------------------------------------------------

BJ = ps.BEIJING_TZ


class TestSchedulerRegistration:
    def test_broker_sync_job_registered_at_1515(self):
        from apscheduler.schedulers.blocking import BlockingScheduler
        sched = BlockingScheduler(timezone=BJ)
        try:
            ps.register_t_tracker_jobs(sched)
            job = sched.get_job(ps.TTRACKER_BROKER_SYNC_JOB_ID)
            assert job is not None and job.max_instances == 1 and job.coalesce is True
            nxt = job.trigger.get_next_fire_time(
                None, datetime(2026, 11, 11, 15, 0, tzinfo=BJ))
            assert nxt is not None
            assert nxt.astimezone(BJ).strftime("%H:%M") == "15:15"
        finally:
            sched.shutdown(wait=False) if sched.running else None

    def test_broker_sync_runs_script_with_sync_flag(self):
        calls = []

        def runner(cmd):
            calls.append(list(cmd))
            return {"returncode": 0, "stdout": "", "stderr": ""}

        report = ps.run_broker_state_sync(
            is_trading_day=lambda d: True, runner=runner,
            now_fn=lambda: datetime(2026, 11, 11, 15, 15, tzinfo=BJ))
        assert report["status"] == "success"
        assert calls[0][1].endswith("broker_state_sync.py")
        assert calls[0][-1] == "--sync"

    def test_broker_sync_non_trading_noop(self):
        report = ps.run_broker_state_sync(
            is_trading_day=lambda d: False, runner=lambda cmd: pytest.fail("不应执行"),
            now_fn=lambda: datetime(2026, 11, 11, 15, 15, tzinfo=BJ))
        assert report["status"] == "no_op_non_trading"

    def test_broker_sync_failure_propagates(self):
        report = ps.run_broker_state_sync(
            is_trading_day=lambda d: True,
            runner=lambda cmd: {"returncode": 1, "stdout": "", "stderr": "refused"},
            now_fn=lambda: datetime(2026, 11, 11, 15, 15, tzinfo=BJ))
        assert report["status"] == "failure" and report["rc"] == 1
