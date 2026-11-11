# -*- coding: utf-8 -*-
"""R48 教练式复盘 + fills 精确覆盖：各分支单测（含 2026-11-11 真实数据黄金例）。

fixture 以 2026-11-11 实况为底：fills=上午两笔（10:04 买 2000@13.33 /
11:12 卖 2000@13.30）、草案=当日唯一 10:21 buy-T 13.2、regime=002015 sideways、
契约=hard_stop 12.90 / rally_trim 13.85。零网络零 THS。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from services import t_trade_coaching as coach

DAY = date(2026, 11, 11)

FILLS = [
    {"ts": datetime(2026, 11, 11, 10, 4, 31), "code": "002015", "side": "BUY",
     "shares": 2000, "price": 13.33},
    {"ts": datetime(2026, 11, 11, 11, 12, 47), "code": "002015", "side": "SELL",
     "shares": 2000, "price": 13.30},
]
DRAFT_1021 = {"id": "T20261111_002015_buy_01", "code": "002015",
              "side": "buy-T", "ts": "2026-11-11 10:21:00", "px": 13.2,
              "qty": 1300, "trigger": {"band": [13.0, 13.2]}}
REGIME = {"codes": {"002015": {"regime": "sideways"}}}
CONTRACT = {"rules": [
    {"code": "002015", "hard_stop": 12.90,
     "rally_trim": {"price": 13.85, "qty_per_wave": 1000}},
    {"code": "300054", "hard_stop": 59.30},
]}


def _build(fills=FILLS, matches=None, drafts=(DRAFT_1021,), regime=REGIME,
           contract=CONTRACT, rounds=(), knife_prior=0, day_hl=None,
           inferred=()):
    return coach.build_coaching(
        for_date=DAY, fills_today=fills,
        matches=matches if matches is not None else {i: None for i in range(len(fills))},
        drafts=list(drafts), rounds_today=list(rounds), regime_doc=regime,
        contract_doc=contract, knife_week_prior=knife_prior,
        day_hl=day_hl, inferred_rows=inferred)


# ── fills → inferred 覆盖（±10min 同码同向） ────────────────────────────

def _inf(ts, side, delta, price_ref=13.33, code="002015", account="acct1"):
    return {"ts": ts, "side": side, "delta_qty": delta, "code": code,
            "price_ref": price_ref, "account": account}


def test_override_match_within_10min():
    rows = coach.override_inferred_with_fills(
        [_inf("2026-11-11 10:14:00", "buy", 2000)], OWNER_FILLS_PROTO)
    ov = rows[0]["fill_override"]
    assert ov["status"] == "matched" and ov["sum_shares"] == 2000 \
        and ov["price_weighted"] == 13.33


OWNER_FILLS_PROTO = [
    {"trade_time": "2026-11-11 10:04:31", "side": "BUY", "code": "002015",
     "shares": 2000, "price": 13.33, "amount": 26660.0},
    {"trade_time": "2026-11-11 11:12:47", "side": "SELL", "code": "002015",
     "shares": 2000, "price": 13.30, "amount": 26600.0},
]


def test_override_rejects_wrong_direction_and_far_window():
    rows = coach.override_inferred_with_fills(
        [_inf("2026-11-11 10:04:40", "sell", 2000),          # 方向不符
         _inf("2026-11-11 10:20:00", "buy", 2000),           # >10min
         _inf("2026-11-11 11:22:00", "sell", 2000, price_ref=13.30)],
        OWNER_FILLS_PROTO)
    assert rows[0]["fill_override"]["status"] == "unmatched"
    assert rows[1]["fill_override"]["status"] == "unmatched"
    assert rows[2]["fill_override"]["status"] == "matched" \
        and rows[2]["fill_override"]["price_weighted"] == 13.30


def test_override_split_fills_aggregate():
    fills = [
        {"trade_time": "2026-11-11 11:12:40", "side": "SELL", "code": "002015",
         "shares": 100, "price": 13.29, "amount": 1329.0},
        {"trade_time": "2026-11-11 11:12:47", "side": "SELL", "code": "002015",
         "shares": 1900, "price": 13.30, "amount": 25270.0},
    ]
    rows = coach.override_inferred_with_fills(
        [_inf("2026-11-11 11:13:00", "sell", 2000, price_ref=13.30)], fills)
    ov = rows[0]["fill_override"]
    got = {(f["ts"], f["shares"]) for f in ov["fills"]}
    assert ov["status"] == "matched" and ov["sum_shares"] == 2000 \
        and got == {("2026-11-11 11:12:40", 100),
                    ("2026-11-11 11:12:47", 1900)} \
        and ov["price_weighted"] == pytest.approx(13.2995, abs=1e-3)


def test_override_idempotent_keeps_matched():
    once = coach.override_inferred_with_fills(
        [_inf("2026-11-11 10:10:00", "buy", 2000)], OWNER_FILLS_PROTO)
    twice = coach.override_inferred_with_fills(once, OWNER_FILLS_PROTO)
    assert once == twice


def test_override_account_isolation():
    fills = [dict(f, account="acct2") for f in OWNER_FILLS_PROTO]
    rows = coach.override_inferred_with_fills(
        [_inf("2026-11-11 10:05:00", "buy", 2000)], fills)
    assert rows[0]["fill_override"]["status"] == "unmatched"


# ── 信号对照：跟单/带外/计划外 ─────────────────────────────────────────

def test_follow_requires_draft_before_fill():
    # 引擎会把 10:04 买配到 10:21 同带草案（±40min）；但草案晚于成交 →
    # 同向命中而时间上不可能跟单 → 带外（注明"草案后至同向"）
    rep = _build(matches={0: DRAFT_1021, 1: None})
    t0 = rep["trades"][0]
    assert t0["signal"] == "带外"
    assert t0["violations"][0]["type"] == "off_plan"
    assert "草案后至" in (t0["violations"][0].get("note") or "")


def test_follow_when_draft_precedes():
    early = [dict(DRAFT_1021, id="d9", ts="2026-11-11 09:58:00")]
    rep = _build(matches={0: early[0], 1: None}, drafts=early)
    t0 = rep["trades"][0]
    assert t0["signal"] == "跟单" and t0["draft_id"] == "d9"
    assert t0["slippage_vs_draft_px"] == pytest.approx(13.33 - 13.2)


def test_contra_plan_when_opposite_draft_live():
    sell_draft = {"id": "d_sell", "code": "002015", "side": "sell-T",
                  "ts": "2026-11-11 10:10:00", "px": 13.4, "qty": 1000}
    rep = _build(drafts=(sell_draft,))
    t0 = rep["trades"][0]  # BUY 成交，窗口内只有反向（卖）草案
    assert t0["signal"] == "计划外"
    assert t0["violations"][0]["type"] == "contra_plan"
    assert t0["violations"][0]["opp_draft_ids"] == ["d_sell"]


# ── 规则违反分支 ───────────────────────────────────────────────────────

def test_knife_catch_down_regime_weekly_count():
    regime = {"codes": {"002015": {"regime": "down"}}}
    rep = _build(regime=regime, knife_prior=2)
    t0 = rep["trades"][0]
    knife = [v for v in t0["violations"] if v["type"] == "knife_catch"][0]
    assert knife["week_no"] == 3 and "本周第3次" in knife["label"]
    assert rep["knife_catch_count"] == 1 and rep["knife_week_total"] == 3
    # 接飞刀15 + 两笔带外(10:04草案后至不算跟单/11:12无草案)10×2 = 65
    assert rep["daily"]["纪律评分"] == 100 - 15 - 10 * 2
    assert "接飞刀" in rep["daily"]["明日提醒"]


def test_weekly_knife_count_reads_prior_summaries(tmp_path):
    ledger = tmp_path / "ledger"
    ledger.mkdir(parents=True)
    for stamp, cnt in (("20261109", 1), ("20261110", 2), ("20261106", 5)):
        (ledger / f"{stamp}_summary.json").write_text(
            json.dumps({"coaching": {"knife_catch_count": cnt}}), encoding="utf-8")
    # 2026-11-11 为周三；本周=11-09(一)/11-10(二)，11-06 上周不计
    assert coach.weekly_knife_count(tmp_path, DAY) == 3


def test_oversize_beyond_2x_draft():
    matched = dict(DRAFT_1021, ts="2026-11-11 09:58:00", qty=800)
    rep = _build(matches={0: matched, 1: None}, drafts=(matched,))
    over = [v for v in rep["trades"][0]["violations"] if v["type"] == "oversize"]
    assert over and over[0]["suggested"] == 800 and over[0]["actual"] == 2000


def test_late_entry_after_1430():
    late_fill = dict(FILLS[0], ts=datetime(2026, 11, 11, 14, 31, 5))
    rep = _build(fills=[late_fill])
    assert any(v["type"] == "late_entry" for v in rep["trades"][0]["violations"])
    # 隔离分支：跟单成交只留 late_entry → 提醒落 14:30 分支
    early = dict(DRAFT_1021, id="d_early", ts="2026-11-11 14:20:00", qty=3000)
    rep2 = _build(fills=[late_fill], matches={0: early}, drafts=(early,))
    assert [v["type"] for v in rep2["trades"][0]["violations"]] == ["late_entry"]
    assert "14:30" in rep2["daily"]["明日提醒"]


def test_contract_ignored_when_breach_no_sell():
    # 当日只有买入无卖出 + 日内破位 → 契约触发未执行
    rep = _build(fills=[FILLS[0]],
                 day_hl={"002015": {"low": 12.50, "high": 13.60},
                         "300054": {"low": 60.0, "high": 62.0}})
    ign = [v for v in rep["contract"] if v["type"] == "contract_ignored"]
    assert ign and ign[0]["code"] == "002015" and ign[0]["day_low"] == 12.50
    assert rep["daily"]["纪律评分"] == 100 - 10 - 25  # 一笔带外 + 契约未执行
    assert "止损契约" in rep["daily"]["明日提醒"]


def test_contract_breach_with_sell_not_flagged():
    # 卖腿在场（FILLS[1] 即当日 SELL 002015）→ 不判契约未执行
    rep = _build(day_hl={"002015": {"low": 12.50, "high": 13.60},
                         "300054": {"low": 60.0, "high": 62.0}})
    # 002015 有当日卖出；契约对象按码判定
    assert not [v for v in rep["contract"]
                if v.get("code") == "002015"
                and v.get("status") != "not_scored"]


def test_contract_day_low_unavailable_honest():
    rep = _build(day_hl={"002015": {"low": None, "high": None},
                         "300054": {"low": None, "high": None}})
    assert all(v.get("status") == "not_scored" for v in rep["contract"])
    assert rep["daily"]["纪律评分"] == 80  # 仅两笔带外，契约不扣分


def test_rally_trim_unexecuted():
    rep = _build(day_hl={"002015": {"low": 13.10, "high": 13.90},
                         "300054": {"low": 60.0, "high": 62.0}})
    trims = [v for v in rep["contract"] if v["type"] == "contract_trim_unexecuted"]
    # 002015 当日有卖出（11:12）→ 不判；300054 无 rally_trim 规则 → 无
    assert not trims


# ── 日终字段 ───────────────────────────────────────────────────────────

def test_daily_fields_shape_and_zero_trade_day():
    rep = _build()
    for key in ("纪律评分", "跟单率", "最差一笔归因", "明日提醒"):
        assert key in rep["daily"]
    empty = _build(fills=[])
    assert empty["daily"]["纪律评分"] == 100
    assert empty["daily"]["跟单率"] is None
    assert "无" in empty["fills_missing_note"]


def test_follow_rate():
    early = [dict(DRAFT_1021, ts="2026-11-11 09:58:00")]
    rep = _build(matches={0: early[0], 1: None}, drafts=early)
    assert rep["daily"]["跟单率"] == 0.5 and rep["followed"] == 1


def test_worst_round_attribution():
    rounds = [{"kind": "round_trip", "code": "002015", "net_pnl": -671.0,
               "buy": {"ts": "2026-11-10 14:55:13"},
               "sell": {"ts": "2026-11-11 11:12:47"}}]
    rep = _build(rounds=rounds)
    worst = rep["daily"]["最差一笔归因"]
    assert worst["code"] == "002015" and worst["net_pnl"] == -671.0
    assert "带外" in worst["归因"]


def test_score_floor_zero():
    regime = {"codes": {"002015": {"regime": "down"}}}
    fills = [dict(FILLS[0], ts=datetime(2026, 11, 11, 14, 3 * k + 31, 0))
             for k in range(3)]  # 3 笔下降态尾盘买入
    rep = _build(fills=fills, regime=regime, knife_prior=6,
                 day_hl={"002015": {"low": 12.5, "high": 13.9},
                         "300054": {"low": 60, "high": 62}})
    assert rep["daily"]["纪律评分"] == 0  # 45+30+15+25+10=125 → 触底 0


def test_inferred_unmatched_listed():
    inf = [{"ts": "2026-11-11 13:50:00", "code": "002015", "side": "sell",
            "delta_qty": -300, "price_ref": 13.4,
            "fill_override": {"status": "unmatched"}}]
    rep = _build(inferred=inf)
    assert rep["inferred_unmatched_by_fills"][0]["delta_qty"] == -300


def test_attach_to_summary_pm_face():
    rep = _build()
    summary: dict = {}
    coach.attach_to_summary(summary, rep)
    assert set(("纪律评分", "跟单率", "最差一笔归因", "明日提醒")) <= set(summary["coaching"])
    assert summary["coaching"]["knife_catch_count"] == 0


# ── 2026-11-11 实况黄金例（sideways + 唯一后至草案 + 无契约触发） ─────

def test_golden_today_no_hard_violations():
    """今日实况：两笔带外（草案后至不算跟单）、无接飞刀/超量/尾盘/契约
    （日内未破 12.90 未及 13.85）→ 评分 80，提醒落带外分支。"""
    rep = _build(day_hl={"002015": {"low": 13.10, "high": 13.62},
                         "300054": {"low": 60.1, "high": 62.0}})
    assert rep["daily"]["纪律评分"] == 80
    assert rep["daily"]["跟单率"] == 0.0
    assert rep["trades"][0]["signal"] == "带外" and rep["trades"][1]["signal"] == "带外"
    assert "带外" in rep["daily"]["明日提醒"]
    assert rep["violations_total"] == 2
