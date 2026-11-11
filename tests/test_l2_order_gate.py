# -*- coding: utf-8 -*-
"""R50 L2 下单风控门单测（全矩阵，纯逻辑零 UI 零 THS 依赖）。

覆盖任务书第 3 项：单笔上限/日亏熔断/次数/时段/价格偏离/数据新鲜度
+ universe / 可卖 / kill_switch / 市场异常 / 14:30 后禁开新仓（T 平仓买豁免）
+ fail-closed（未知账号/快照缺失/票池不可判/可卖未知）
+ 零 UI 静态守卫 + 审计 journal + 真实配置集成。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import l2_order_gate as gate  # noqa: E402

D = Decimal
NOW = datetime(2026, 11, 12, 10, 0, 0)          # 周四盘中
SIG = datetime(2026, 11, 12, 9, 59, 55)

REAL_CONFIG = Path(r"F:\Aistock\data\data\runtime\t_tracker\l2_risk_config.json")


def synth_cfg() -> dict:
    """与 runtime/t_tracker/l2_risk_config.json 同构的合成配置。"""
    return {
        "schema": "l2_risk_config/1",
        "accounts": {
            "acct1_zeng_gf": {
                "capital_total": 100000,
                "per_order_value_cap": 25000,
                "per_order_share_rule": "min(25000元/信号价, 底仓1/3取整百)",
                "daily_loss_kill": 1500,
                "universe": "持仓票池（watchlist∪holdings）",
            },
            "acct2_wang_gf": {
                "capital_total": 20000,
                "per_order_share_cap": 300,
                "per_order_value_cap": 6500,
                "daily_loss_kill": 800,
                "universe": "仅 603881.SH 数据港（单票专用）",
            },
        },
        "global_breakers": {
            "price_deviation_reject_pct": 0.5,
            "order_count_per_code_per_day": 2,
            "no_new_position_after": "14:30",
            "stale_data_kill_sec": 120,
        },
    }


def snap(**over):
    s = {"reference_price": D("20.60"),
         "snapshot_ts": datetime(2026, 11, 12, 9, 59, 30)}   # age=30s
    s.update(over)
    return gate.MarketSnapshot(**s)


def day(**over):
    d = {"day_pnl": D(0), "orders_today": 0, "holdings_qty": 600,
         "sellable_qty": 600, "day_bought_qty": 0, "day_sold_qty": 0}
    d.update(over)
    return gate.DayState(**d)


def ctx(**over):
    from datetime import timedelta
    now = over.get("now", NOW)
    c = {"now": now,
         "snapshot": snap(snapshot_ts=now - timedelta(seconds=30)),
         "day": day(),
         "kill_switch": False, "code_in_universe": True}
    c.update(over)
    return gate.GateContext(**c)


def req(**over):
    r = {"account": "acct2", "code": "603881", "side": "buy",
         "price": D("20.60"), "qty": 300, "ts": SIG}
    r.update(over)
    return gate.OrderRequest(**r)


def decide(r=None, c=None, cfg=None):
    if isinstance(r, gate.GateContext):
        r, c = None, r
    if isinstance(c, dict):
        c, cfg = None, c
    return gate.check_order(r or req(), c or ctx(), cfg or synth_cfg())


def checks(res) -> set:
    return {x.check for x in res.rejections}


def reason(res, check: str) -> str:
    return ";".join(x.reason for x in res.rejections if x.check == check)


# ── 0) 静态守卫：零 UI 依赖 ─────────────────────────────────────────

def test_no_ui_imports():
    import ast
    tree = ast.parse(Path(gate.__file__).read_text(encoding="utf-8"))
    banned = ("pywinauto", "ctypes", "win32", "subprocess", "urllib",
              "requests", "socket", "shutil")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for n in names:
            root = (n or "").split(".")[0]
            assert root not in banned, f"gate 禁止依赖 {n}（纯逻辑铁律）"


# ── 1) 配置加载 fail-closed ────────────────────────────────────────

def test_config_missing(tmp_path):
    with pytest.raises(gate.GateConfigError):
        gate.load_config(tmp_path / "nope.json")


def test_config_schema_mismatch(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"schema": "l2_risk_config/9"}), encoding="utf-8")
    with pytest.raises(gate.GateConfigError):
        gate.load_config(p)


def test_config_bad_schema_rejects_order():
    res = decide(cfg={"schema": "other/1"})
    assert not res.approved and res.fill is None
    assert "config" in checks(res)


# ── 2) 放行路径 ────────────────────────────────────────────────────

def test_approve_acct2_happy_path():
    res = decide()
    assert res.approved and res.fill is not None
    f = res.fill
    assert f.action == "fill" and f.account == "acct2_wang_gf"
    assert f.code == "603881" and f.side == "buy"
    assert f.price == "20.60" and f.qty == 300
    assert f.config_schema == gate.SCHEMA
    # 全部检查项通过留痕
    assert {"kill_switch", "account_known", "request_valid", "universe",
            "data_freshness", "price_deviation", "market_halt",
            "per_order_cap", "sell_available", "order_count",
            "daily_loss_kill", "time_window",
            "no_new_position_after"} <= set(f.checks_passed)


def acct1_ctx(**over):
    """acct1 用例专用 ctx：基准价 13.50（与订单价一致，隔离偏离检查）。"""
    over.setdefault("snapshot", snap(reference_price=D("13.50")))
    return ctx(**over)


def test_approve_acct1_alias_and_code_suffix():
    res = decide(req(account="acct1", code="002015.SZ", price=D("13.50"),
                     qty=1300),
                 acct1_ctx(day=day(holdings_qty=4000)))
    assert res.approved
    assert res.fill.account == "acct1_zeng_gf" and res.fill.code == "002015"


def test_sell_odd_lot_allowed():
    """零股卖出合法（配股/送股残股）；买入必须整百。"""
    res = decide(req(side="sell", qty=37))
    assert res.approved


# ── 3) 请求合法性 ──────────────────────────────────────────────────

@pytest.mark.parametrize("over,expect_chk", [
    ({"side": "hold"}, "request_valid"),
    ({"qty": 0}, "request_valid"),
    ({"qty": -100}, "request_valid"),
    ({"qty": 300.5}, "request_valid"),
    ({"price": 0}, "request_valid"),
    ({"price": "abc"}, "request_valid"),
    ({"code": "60388"}, "request_valid"),
    ({"code": "ABCDEFG"}, "request_valid"),
    ({"side": "buy", "qty": 150}, "request_valid"),
])
def test_bad_request_rejected(over, expect_chk):
    res = decide(req(**over))
    assert not res.approved
    assert expect_chk in checks(res)


def test_unknown_account_rejected():
    res = decide(req(account="acct9"))
    assert not res.approved and "account_known" in checks(res)


def test_rejections_not_short_circuit():
    """多拒因并存：审计画面完整（未知账号 + 快照缺失 + 票池不可判）。"""
    res = decide(req(account="acct9"), ctx(snapshot=None))
    cs = checks(res)
    assert {"account_known", "data_freshness"} <= cs


# ── 4) universe 票池 ───────────────────────────────────────────────

def test_acct2_only_603881_enforced_by_config():
    res = decide(req(code="002015"))
    assert not res.approved and "universe" in checks(res)
    assert reason(res, "universe") == "code_not_in_universe"


def test_acct2_config_overrides_ctx_true():
    """配置"仅 603881"与 ctx 票池判定取 AND：ctx 说 True 也不能绕配置。"""
    res = decide(req(code="002015"), ctx(code_in_universe=True))
    assert not res.approved and "universe" in checks(res)


def test_acct1_universe_undeterminable_failclosed():
    res = decide(req(account="acct1", code="002015", price=D("13.5"), qty=1300),
                 ctx(code_in_universe=None))
    assert not res.approved and "universe" in checks(res)
    assert "undeterminable" in reason(res, "universe")


def test_acct1_universe_pass_with_ctx_true():
    res = decide(req(account="acct1", code="002015", price=D("13.5"), qty=1300),
                 acct1_ctx(day=day(holdings_qty=4000)))
    assert res.approved


# ── 5) 数据新鲜度 ──────────────────────────────────────────────────

def test_snapshot_missing_failclosed():
    res = decide(ctx(snapshot=None))
    assert not res.approved and "data_freshness" in checks(res)


def test_snapshot_missing_ts_failclosed():
    res = decide(ctx(snapshot=gate.MarketSnapshot(reference_price=D("20.6"))))
    assert not res.approved and "data_freshness" in checks(res)


def test_snapshot_stale_121s_rejected():
    res = decide(ctx(snapshot=snap(snapshot_ts=datetime(2026, 11, 12, 9, 57, 59))))
    assert not res.approved and reason(res, "data_freshness").startswith("snapshot_stale")


def test_snapshot_stale_boundary_120s_pass():
    res = decide(ctx(snapshot=snap(snapshot_ts=datetime(2026, 11, 12, 9, 58, 0))))
    assert res.approved


def test_snapshot_future_rejected():
    res = decide(ctx(snapshot=snap(snapshot_ts=datetime(2026, 11, 12, 10, 0, 30))))
    assert not res.approved and "future" in reason(res, "data_freshness")


# ── 6) 价格偏离 ────────────────────────────────────────────────────

def test_price_deviation_within_pass():
    res = decide(req(price=D("20.70")))     # 0.485% < 0.5%
    assert res.approved


def test_price_deviation_exceeded_reject():
    res = decide(req(price=D("20.71")))     # 0.534% > 0.5%
    assert not res.approved and "price_deviation" in checks(res)


def test_price_deviation_boundary_exact_pass():
    c = ctx(snapshot=snap(reference_price=D("20.00")))
    res = decide(req(price=D("20.10")), c)  # 恰 0.5% → 不大于 → 过
    assert res.approved


def test_price_deviation_sell_side_also_guarded():
    res = decide(req(side="sell", price=D("20.49"), qty=300))  # -0.534%
    assert not res.approved and "price_deviation" in checks(res)


def test_reference_missing_failclosed():
    res = decide(ctx(snapshot=gate.MarketSnapshot(
        snapshot_ts=datetime(2026, 11, 12, 9, 59, 30))))
    assert not res.approved and "price_deviation" in checks(res)


# ── 7) 市场异常 ────────────────────────────────────────────────────

def test_halt_suspended():
    res = decide(ctx(snapshot=snap(halted=True)))
    assert not res.approved and "suspended" in reason(res, "market_halt")


def test_halt_limit_touched():
    res = decide(ctx(snapshot=snap(limit_touched=True)))
    assert not res.approved and "limit" in reason(res, "market_halt")


def test_halt_feed_stale_11min():
    res = decide(ctx(snapshot=snap(feed_stale_min=11)))
    assert not res.approved and "feed_stale" in reason(res, "market_halt")


def test_feed_stale_10min_pass():
    res = decide(ctx(snapshot=snap(feed_stale_min=10)))
    assert res.approved


# ── 8) 单笔上限 ────────────────────────────────────────────────────

def test_acct2_share_cap_301_rejected():
    # acct2 share cap=300：qty 400 必拒（票池仍 True，只看 share cap）
    res = decide(req(qty=400))
    assert not res.approved and "per_order_cap" in checks(res)
    assert "share_cap" in reason(res, "per_order_cap")


def test_acct2_value_cap_6600_rejected():
    res = decide(req(price=D("22.00"), qty=300),
                 ctx(snapshot=snap(reference_price=D("22.00"))))
    assert not res.approved and "notional" in reason(res, "per_order_cap")


def test_acct2_value_6480_pass():
    res = decide(req(price=D("21.60"), qty=300),
                 ctx(snapshot=snap(reference_price=D("21.60"))))
    assert res.approved


def test_acct1_base_third_rule_cap1300():
    """min(25000/13.50=1851, 4000//3//100*100=1300)=1300：1300 过 1400 拒。"""
    c = acct1_ctx(day=day(holdings_qty=4000))
    assert decide(req(account="acct1", code="002015", price=D("13.50"), qty=1300), c).approved
    res = decide(req(account="acct1", code="002015", price=D("13.50"), qty=1400), c)
    assert not res.approved and "per_order_cap" in checks(res)


def test_acct1_value_branch_min_wins():
    """低价时金额分支仍受限（price 5 → by_value 5000 > by_base 1300 → cap 1300）。"""
    c = ctx(day=day(holdings_qty=4000),
            snapshot=snap(reference_price=D("5.00")))
    res = decide(req(account="acct1", code="002015", price=D("5.00"), qty=2000), c)
    assert not res.approved and "per_order_cap" in checks(res)


def test_acct1_tiny_base_holdings_zero_cap():
    """底仓 200 → 200//3//100*100=0：任何整百买入被拒（fail-closed 而非放行）。"""
    c = acct1_ctx(day=day(holdings_qty=200))
    res = decide(req(account="acct1", code="002015", price=D("13.50"), qty=100), c)
    assert not res.approved and "per_order_cap" in checks(res)


# ── 9) 可卖（T+1） ─────────────────────────────────────────────────

def test_sell_over_sellable_rejected():
    res = decide(req(side="sell", qty=700), ctx(day=day(sellable_qty=600)))
    assert not res.approved and "sell_available" in checks(res)


def test_sell_sellable_unknown_failclosed():
    res = decide(req(side="sell", qty=300), ctx(day=day(sellable_qty=None)))
    assert not res.approved
    assert reason(res, "sell_available") == "sellable_unknown_failclosed"


# ── 10) 同码每日次数 ───────────────────────────────────────────────

def test_order_count_2_rejected_1_pass():
    assert not decide(ctx(day=day(orders_today=2))).approved
    assert decide(ctx(day=day(orders_today=1))).approved


# ── 11) 日亏熔断 ───────────────────────────────────────────────────

def test_daily_loss_kill_acct2_boundary():
    assert not decide(ctx(day=day(day_pnl=D("-800")))).approved
    assert decide(ctx(day=day(day_pnl=D("-799.99")))).approved


def test_daily_loss_kill_acct1_1500():
    c = acct1_ctx(day=day(day_pnl=D("-1500.00"), holdings_qty=4000))
    r = req(account="acct1", code="002015", price=D("13.50"), qty=1300)
    assert not decide(r, c).approved


def test_daily_loss_kill_unconfigured_failclosed():
    cfg = synth_cfg()
    del cfg["accounts"]["acct2_wang_gf"]["daily_loss_kill"]
    res = decide(cfg=cfg)
    assert not res.approved and "daily_loss_kill" in checks(res)


# ── 12) 时段 ───────────────────────────────────────────────────────

@pytest.mark.parametrize("hms,ok_expected", [
    ((9, 29, 59), False), ((9, 30, 0), True), ((11, 30, 0), True),
    ((11, 30, 1), False), ((12, 30, 0), False), ((13, 0, 0), True),
    ((15, 0, 0), True), ((15, 0, 1), False),
])
def test_time_window(hms, ok_expected):
    # 用 sell 侧隔离时段逻辑（15:00 边界不受 14:30 禁开新仓干扰）
    c = ctx(now=datetime(2026, 11, 12, *hms))
    res = decide(req(side="sell", qty=300), c)
    assert res.approved is ok_expected
    if not ok_expected:
        assert "time_window" in checks(res)


# ── 13) 14:30 后禁开新仓（T 平仓买豁免） ──────────────────────────

def _afternoon(**day_over):
    return ctx(now=datetime(2026, 11, 12, 14, 31, 0), day=day(**day_over))


def test_buy_after_1430_new_position_rejected():
    res = decide(req(qty=200), _afternoon())
    assert not res.approved and "no_new_position_after" in checks(res)


def test_sell_after_1430_allowed():
    res = decide(req(side="sell", qty=300), _afternoon())
    assert res.approved


def test_buy_after_1430_t_close_exempt():
    """日卖 500 买 100 → 平今卖腿，豁免 14:30 禁令。"""
    c = _afternoon(day_sold_qty=500, day_bought_qty=100)   # capacity=400
    assert decide(req(qty=200), c).approved


def test_buy_after_1430_over_t_close_capacity_rejected():
    c = _afternoon(day_sold_qty=500, day_bought_qty=100)   # capacity=400
    res = decide(req(qty=500), c)
    assert not res.approved and "no_new_position_after" in checks(res)


def test_buy_before_1430_unrestricted():
    assert decide(ctx(now=datetime(2026, 11, 12, 14, 29, 0))).approved


# ── 14) kill_switch ────────────────────────────────────────────────

def test_kill_switch_rejects_everything():
    res = decide(ctx(kill_switch=True))
    assert not res.approved and "kill_switch" in checks(res)


# ── 15) 审计 journal ───────────────────────────────────────────────

def test_journal_approve_and_reject_rows(tmp_path):
    jp = tmp_path / "gate_journal" / "20261112.jsonl"
    d_ok = decide()
    d_no = decide(ctx(kill_switch=True))
    gate.journal_decision(d_ok, req(), ctx(), jp)
    gate.journal_decision(d_no, req(), ctx(kill_switch=True), jp)
    rows = [json.loads(x) for x in jp.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["approved"] is True and rows[0]["fill"]["qty"] == 300
    assert rows[1]["approved"] is False and rows[1]["fill"] is None
    assert rows[1]["rejections"][0]["check"] == "kill_switch"
    assert rows[0]["schema"] == gate.SCHEMA


# ── 16) 真实配置集成（配置在场时） ─────────────────────────────────

@pytest.mark.skipif(not REAL_CONFIG.exists(), reason="runtime config 不在本机")
def test_real_config_acct2_approve():
    cfg = gate.load_config(REAL_CONFIG)
    assert cfg["schema"] == gate.SCHEMA
    res = gate.check_order(
        req(account="acct2", code="603881.SH", price=D("20.60"), qty=300),
        ctx(day=day(holdings_qty=600, sellable_qty=600)), cfg)
    assert res.approved and res.fill.qty == 300


@pytest.mark.skipif(not REAL_CONFIG.exists(), reason="runtime config 不在本机")
def test_real_config_acct1_rule_matches_r47_cap():
    cfg = gate.load_config(REAL_CONFIG)
    c = ctx(day=day(holdings_qty=4000),
            snapshot=snap(reference_price=D("13.50")))
    r1300 = gate.check_order(
        req(account="acct1", code="002015", price=D("13.50"), qty=1300), c, cfg)
    r1400 = gate.check_order(
        req(account="acct1", code="002015", price=D("13.50"), qty=1400), c, cfg)
    assert r1300.approved and not r1400.approved
