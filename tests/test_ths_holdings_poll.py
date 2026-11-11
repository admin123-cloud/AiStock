# -*- coding: utf-8 -*-
"""R48 盘中持仓巡读：diff/T+1 语义/前台跳过/锁忙跳过/连续失败告警/幂等 单测。

fixture 纯合成（含 Owner 2026-11-11 上午真实两笔的黄金形态：
10:04 买 2000@13.33 / 11:12 卖 2000@13.30）。零 THS 依赖、零真实路径写。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from services import ths_holdings_poll as poll

DAY = date(2026, 11, 11)


def _rows(**qty_by_code):
    return [{"code": c, "name": f"n{c}", "shares": q,
             "available_shares": q, "current_price": 13.5, "cost_price": 13.4}
            for c, q in qty_by_code.items()]


# ── 窗口守卫 / 错峰 ────────────────────────────────────────────────────

@pytest.mark.parametrize("hhmm,ok", [
    ("09:34", False), ("09:35", True), ("12:00", True), ("15:00", True),
    ("15:01", False),
])
def test_should_poll_window(hhmm, ok):
    now = datetime.strptime(f"2026-11-11 {hhmm}", "%Y-%m-%d %H:%M")
    got, _ = poll.should_poll(now)
    assert got is ok


def test_should_poll_weekend():
    got, reason = poll.should_poll(datetime(2026, 11, 14, 10, 0))  # 周六
    assert got is False and reason == "weekend"


def test_stagger_seconds_align_47s():
    assert poll.stagger_sleep_seconds(datetime(2026, 11, 11, 13, 0, 3)) == 44.0
    assert poll.stagger_sleep_seconds(datetime(2026, 11, 11, 13, 0, 47)) == 0.0
    assert poll.stagger_sleep_seconds(datetime(2026, 11, 11, 13, 0, 50)) == 57.0


# ── diff：增/减/清零/新码/消失/T+1/零变动 ─────────────────────────────

def test_diff_buy_sell_reduce_and_zero():
    prev = _rows(**{"002015": 4000, "300054": 100})
    cur = _rows(**{"002015": 2000, "300054": 100})
    out = poll.diff_holdings(prev, cur)
    assert out == [{**out[0], "code": "002015", "side": "sell",
                    "delta_qty": -2000, "prev_shares": 4000, "cur_shares": 2000}]

    cur2 = _rows(**{"300054": 100})  # 002015 清仓消失
    out2 = poll.diff_holdings(_rows(**{"002015": 2000, "300054": 100}), cur2)
    assert out2[0]["code"] == "002015" and out2[0]["delta_qty"] == -2000

    out3 = poll.diff_holdings(_rows(**{"002015": 4000, "300054": 100}),
                              _rows(**{"002015": 6000, "300054": 100,
                                       "603881": 200}))
    kinds = {(r["code"], r["side"], r["delta_qty"]) for r in out3}
    assert kinds == {("002015", "buy", 2000), ("603881", "buy", 200)}


def test_diff_t1_semantics_buy_frozen_sell_released():
    prev = _rows(**{"002015": 4000})
    cur = [{"code": "002015", "name": "n", "shares": 6000,
            "available_shares": 4000, "current_price": 13.33, "cost_price": 13.4}]
    (buy,) = poll.diff_holdings(prev, cur)
    assert buy["delta_qty"] == 2000 and buy["avail_delta"] == 0  # T+1 冻结

    cur2 = [{"code": "002015", "name": "n", "shares": 4000,
             "available_shares": 2000, "current_price": 13.30, "cost_price": 13.4}]
    (sell,) = poll.diff_holdings(cur, cur2)
    assert sell["delta_qty"] == -2000 and sell["avail_delta"] == -2000


def test_diff_no_change_no_rows():
    assert poll.diff_holdings(_rows(**{"002015": 4000}), _rows(**{"002015": 4000})) == []


def test_diff_price_ref_is_current_price():
    (row,) = poll.diff_holdings(_rows(**{"002015": 4000}),
                                [{"code": "002015", "shares": 6000,
                                  "available_shares": 4000,
                                  "current_price": 13.33}])
    assert row["price_ref"] == 13.33


# ── 轮次编排：基线/跳过/失败告警/幂等 ──────────────────────────────────

def _round(root, hhmmss, account, status, holdings=None):
    return poll.run_poll_round(
        root=root, now=datetime.strptime(f"2026-11-11 {hhmmss}", "%Y-%m-%d %H:%M:%S"),
        plan={"acct1": {"panel": {"pid": 123, "title": "t"}}},
        capture=lambda a, p, h=None: {"status": status,
                                      "holdings": holdings or [],
                                      "ocr_lines": ["证券代码\t股票余额"]})


def test_round_baseline_then_diff_writes_inferred(tmp_path):
    r1 = _round(tmp_path, "09:35:00", "acct1", "ok", _rows(**{"002015": 4000}))
    assert r1["accounts"]["acct1"]["inferred_added"] == 0  # 首轮=基线
    r2 = _round(tmp_path, "09:40:00", "acct1", "ok",
                [{"code": "002015", "shares": 6000, "available_shares": 4000,
                  "current_price": 13.33}])
    assert r2["accounts"]["acct1"]["inferred_added"] == 1
    rows = poll.read_jsonl(poll.inferred_path(tmp_path, DAY))
    assert len(rows) == 1 and rows[0]["delta_qty"] == 2000 \
        and rows[0]["side"] == "buy" and rows[0]["price_ref"] == 13.33 \
        and rows[0]["account"] == "acct1" and rows[0]["schema"] == poll.SCHEMA_INFERRED


def test_round_idempotent_same_timestamp(tmp_path):
    _round(tmp_path, "09:35:00", "acct1", "ok", _rows(**{"002015": 4000}))
    for _ in range(2):
        r = _round(tmp_path, "09:40:00", "acct1", "ok",
                   [{"code": "002015", "shares": 6000,
                     "available_shares": 4000, "current_price": 13.33}])
    rows = poll.read_jsonl(poll.inferred_path(tmp_path, DAY))
    assert len(rows) == 1


@pytest.mark.parametrize("status", ["skipped_foreground", "skipped_minimized",
                                    "skipped_lock", "view_not_holdings",
                                    "panel_absent"])
def test_round_skip_statuses_no_diff_no_failure(tmp_path, status):
    _round(tmp_path, "09:35:00", "acct1", "ok", _rows(**{"002015": 4000}))
    r = _round(tmp_path, "09:40:00", "acct1", status)
    assert r["accounts"]["acct1"]["status"] == status
    state = json.loads(poll.poller_state_path(tmp_path, DAY).read_text("utf-8"))
    a = state["accounts"]["acct1"]
    assert a["consecutive_failures"] == 0 and a["failures"] == 0
    assert a["skips"].get(status) == 1
    assert poll.read_jsonl(poll.inferred_path(tmp_path, DAY)) == []
    snap = poll.snapshots_dir(tmp_path, DAY) / "094000_acct1.json"
    assert snap.is_file()
    assert json.loads(snap.read_text("utf-8"))["status"] == status


def test_round_consecutive_failures_alert_at_3(tmp_path):
    for i, hhmm in enumerate(("09:35", "09:40", "09:45")):
        r = _round(tmp_path, f"{hhmm}:00", "acct1", "error")
        if i < 2:
            assert "alert" not in r["accounts"]["acct1"]
        else:
            assert r["accounts"]["acct1"]["alert"] == "consecutive_failures=3"
    _round(tmp_path, "09:50:00", "acct1", "ok", _rows(**{"002015": 4000}))
    state = json.loads(poll.poller_state_path(tmp_path, DAY).read_text("utf-8"))
    assert state["accounts"]["acct1"]["consecutive_failures"] == 0


def test_round_capture_exception_recorded_not_raised(tmp_path):
    def boom(a, p, h=None):
        raise RuntimeError("ocr crashed")
    r = poll.run_poll_round(
        root=tmp_path, now=datetime(2026, 11, 11, 10, 0),
        plan={"acct1": {"panel": None}}, capture=boom)
    assert r["accounts"]["acct1"]["status"] == "error"


def test_multi_account_isolation(tmp_path):
    caps = {"acct1": {"status": "ok", "holdings": _rows(**{"002015": 4000})},
            "acct2": {"status": "panel_absent"}}
    poll.run_poll_round(
        root=tmp_path, now=datetime(2026, 11, 11, 10, 0),
        plan={"acct1": {"panel": None}, "acct2": {"panel": {"pid": 9}}},
        capture=lambda a, p, h=None: caps[a])
    state = json.loads(poll.poller_state_path(tmp_path, DAY).read_text("utf-8"))
    assert state["accounts"]["acct2"]["skips"]["panel_absent"] == 1
    assert (poll.snapshots_dir(tmp_path, DAY) / "100000_acct2.json").is_file()


# ── R48 身份门禁：窗口换登（曾*→王*美）绝不写成 acct1 ────────────────

def test_round_identity_mismatch_skip_keeps_baseline():
    """换登轮=skip：快照留痕（identity_label）、无 diff、基线不动、非失败。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _round(root, "09:35:00", "acct1", "ok", _rows(**{"002015": 4000}))
        r = poll.run_poll_round(
            root=root, now=datetime(2026, 11, 11, 10, 0),
            plan={"acct1": {"panel": {"pid": 1}, "identity_hint": "曾"}},
            capture=lambda a, p, h=None: {
                "status": "identity_mismatch",
                "identity_label": "广发证券-王*美"})
        assert r["accounts"]["acct1"]["status"] == "identity_mismatch"
        snap = json.loads((poll.snapshots_dir(root, DAY) / "100000_acct1.json")
                          .read_text("utf-8"))
        assert snap["identity_label"] == "广发证券-王*美"
        assert snap["identity_hint"] == "曾"
        assert snap["holdings"] == []  # 拒绝采集：不落他人持仓
        state = json.loads(poll.poller_state_path(root, DAY).read_text("utf-8"))
        a = state["accounts"]["acct1"]
        assert a["consecutive_failures"] == 0
        assert a["last_ok_snapshot"].endswith("093500_acct1.json")  # 基线不动
        assert poll.read_jsonl(poll.inferred_path(root, DAY)) == []
        # 换回曾*后下一轮恢复 diff（基线仍是最初 4000 股）
        r2 = poll.run_poll_round(
            root=root, now=datetime(2026, 11, 11, 10, 5),
            plan={"acct1": {"panel": {"pid": 1}, "identity_hint": "曾"}},
            capture=lambda a, p, h=None: {
                "status": "ok", "identity_label": "广发证券-曾*",
                "holdings": [{"code": "002015", "shares": 6000,
                              "available_shares": 4000,
                              "current_price": 13.33}]})
        assert r2["accounts"]["acct1"]["inferred_added"] == 1


def test_identity_label_parsing():
    from services.ths_account_bridge import (identity_matches,
                                              parse_identity_label)
    lines = ["资产:16436.82 可用:4076.82\t撤\t帮助",
             "买\t卖\t撤\t查\t广发证券-王*美\t一|回|添加\t退出\t登录"]
    assert parse_identity_label(lines) == "广发证券-王*美"
    assert parse_identity_label(["随便一行", "没有券商栏"]) == ""
    assert identity_matches("广发证券-王*美", "曾") is False
    assert identity_matches("广发证券-曾*", "曾") is True
    assert identity_matches("", "曾") is False
    assert identity_matches("广发证券-王*美", None) is None


def test_fills_export_identity_gate():
    from scripts.ths_fills_export import assert_panel_identity
    wang = ["买\t卖\t撤\t查\t广发证券-王*美\t退出"]
    zeng = ["买\t卖\t撤\t查\t广发证券-曾*\t退出"]
    assert assert_panel_identity(wang, None) is None      # 未配置不门禁
    assert assert_panel_identity(zeng, "曾") == "广发证券-曾*"
    with pytest.raises(RuntimeError, match="PANEL_IDENTITY_MISMATCH"):
        assert_panel_identity(wang, "曾")
    with pytest.raises(RuntimeError, match="PANEL_IDENTITY_MISMATCH"):
        assert_panel_identity(["顶栏 OCR 失效"], "曾")  # 标签缺失=拒绝


# ── Owner 2026-11-11 上午真实两笔：快照序列黄金对拍（diff 面） ─────────

OWNER_MORNING_FILLS = [
    {"trade_time": "2026-11-11 10:04:31", "side": "BUY", "code": "002015",
     "shares": 2000, "price": 13.33, "amount": 26660.0},
    {"trade_time": "2026-11-11 11:12:47", "side": "SELL", "code": "002015",
     "shares": 2000, "price": 13.30, "amount": 26600.0},
]


def test_owner_morning_two_trades_golden_diff():
    """10:04 买 2000@13.33（T+1 冻结）/11:12 卖 2000@13.30 的快照 diff 形态。"""
    s1 = [{"code": "002015", "shares": 4000, "available_shares": 4000,
           "current_price": 13.30}]                       # 09:55 基线
    s2 = [{"code": "002015", "shares": 6000, "available_shares": 4000,
           "current_price": 13.33}]                       # 10:10（买后）
    s3 = [{"code": "002015", "shares": 4000, "available_shares": 2000,
           "current_price": 13.30}]                      # 11:20（卖后，T+1 冻结 2000）
    (buy,) = poll.diff_holdings(s1, s2)
    assert (buy["side"], buy["delta_qty"], buy["avail_delta"],
            buy["price_ref"]) == ("buy", 2000, 0, 13.33)
    (sell,) = poll.diff_holdings(s2, s3)
    assert (sell["side"], sell["delta_qty"], sell["avail_delta"],
            sell["price_ref"]) == ("sell", -2000, -2000, 13.30)
