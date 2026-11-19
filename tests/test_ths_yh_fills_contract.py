# -*- coding: utf-8 -*-
"""远航版单账号链（read-account 新链）行为契约测试（离线合成 mock）。

覆盖（任务书 2026-11-15）：
  * 历史逐笔成交纯解析层：列名适配/表头映射/导航树与查询控件排除/
    真实重复成交保留（绝不静默去重）/委托页冒充守卫；
  * 日期与分页证据：查询界面日期令牌归一、共N条/页码/下一页证据、
    证据不足 → partial/unavailable（不得成功空结果）；
  * 裁决纯函数 yh_fills_verdict / yh_single_snapshot 与采集器
    _yh_collect_single_snapshot 的 mock 集成：身份前后核验、新链绝不切号、
    Owner 活跃零交互、GUI 串行锁忙 fail-closed、--fills 显式触发；
  * 旧能力兼容：run_read_account_switchable 保留自动切换；
    资金读取既有路径回归（防空 stub 复辟）。

SAFETY：全部真机交互面（进程/窗口/PrintWindow/视觉/UIA/点击/锁）打桩；
不触达 runtime、DB、网络、登录信息；数据全部合成。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ths_client as tc  # noqa: E402

MAIN_HWND = 111
PANEL_HWND = 222
PANEL_RECT = [10, 20, 810, 620]
MASKED_A1 = "A5****1851"
MASKED_A2 = "A6****5646"


def _panel(hwnd: int = PANEL_HWND) -> dict:
    return {"hwnd": hwnd, "pid": 34, "rect": list(PANEL_RECT), "width": 800,
            "height": 600, "title": "广发证券交易面板"}


def _main() -> dict:
    return {"hwnd": MAIN_HWND, "pid": 33, "rect": [0, 0, 1280, 800],
            "area": 1280 * 800, "title": "同花顺远航"}


def _it(text: str, x: float, y: float, aid: str = "") -> dict:
    return {"text": text, "x": x, "y": y, "score": 0.9, "aid": aid}


def _masked_items(masked: str = MASKED_A1) -> list[dict]:
    return [_it(masked, 400.0, 20.0)]


def _funds_uia_texts() -> list[dict]:
    def mk(rel_x: float, rel_y: float, text: str) -> dict:
        cx, cy = PANEL_RECT[0] + rel_x, PANEL_RECT[1] + rel_y
        return {"text": text, "aid": "",
                "rect": [int(cx - 40), int(cy - 8), int(cx + 40), int(cy + 8)]}

    return [
        mk(200, 160, "资金余额"), mk(300, 160, "50,000.00"),
        mk(200, 190, "可取"), mk(300, 190, "40,000.00"),
        mk(500, 160, "股票市值"), mk(620, 160, "62,842.68"),
        mk(500, 220, "总资产"), mk(620, 220, "112,842.68"),
    ]


def _fills_page_items() -> list[dict]:
    """历史成交页合成布局：查询表单日期对 + 表头 + 两笔同日成交 +
    共N条（导航树列 x<200 须被排除）。"""
    return [
        _it("资金股票", 77.0, 240.0), _it("当日委托", 70.8, 266.0),
        _it("历史成交", 70.5, 288.0),
        _it("20261101", 250.0, 320.0), _it("20261115", 380.0, 320.0),
        _it("成交日期", 230.0, 350.0), _it("成交时间", 285.0, 350.0),
        _it("业务名称", 340.0, 350.0),
        _it("证券代码", 400.0, 350.0), _it("证券名称", 470.0, 350.0),
        _it("成交数量", 540.0, 350.0), _it("成交价格", 610.0, 350.0),
        _it("成交金额", 680.0, 350.0), _it("手续费", 750.0, 350.0),
        _it("20261113", 230.0, 380.0), _it("09:30:00", 285.0, 380.0),
        _it("证券买入", 340.0, 380.0),
        _it("603881", 400.0, 380.0), _it("数据港", 470.0, 380.0),
        _it("600", 540.0, 380.0), _it("20.010", 610.0, 380.0),
        _it("12,006.00", 680.0, 380.0), _it("5.00", 750.0, 380.0),
        _it("20261113", 230.0, 404.0), _it("09:35:00", 285.0, 404.0),
        _it("证券卖出", 340.0, 404.0),
        _it("603881", 400.0, 404.0), _it("数据港", 470.0, 404.0),
        _it("200", 540.0, 404.0), _it("20.080", 610.0, 404.0),
        _it("4,016.00", 680.0, 404.0), _it("4.00", 750.0, 404.0),
        _it("第1页/共1页", 620.0, 590.0), _it("共2条", 700.0, 590.0),
    ]


def _fills_ev(rows=None, **over):
    """合成 fills 证据（对齐硬化版 yh_fills_verdict 契约）。"""
    ev = {"attempted": True, "reason": None, "tree_token": "历史成交",
          "tried_tokens": ["历史成交"], "wrong_page": False,
          "rows": rows if rows is not None else [], "row_count": 0,
          "count_total": None, "page_current": 1, "page_total": 1,
          "next_page_present": False,
          "header_columns": ["date", "time", "side_raw", "code",
                             "quantity", "price", "amount"],
          "date_tokens": [{"token": "20261101", "iso": "2026-11-01"},
                          {"token": "20261115", "iso": "2026-11-15"}],
          "date_range": {"start": "2026-11-01", "end": "2026-11-15"},
          "date_range_source": "query_controls_readback",
          "reader": "ocr_local", "collected_at": "2026-11-15T00:00:00"}
    ev.update(over)
    return ev


# ── 纯解析层：逐笔成交行 ────────────────────────────────────────────

def test_fills_record_alias_adaptation():
    rec = tc.yh_normalize_fill_record({
        "成交日期": "20261113", "业务名称": "证券买入", "证券代码": "603881",
        "证券名称": "数据港", "成交数量": "600", "成交价格": "20.010",
        "成交金额": "12,006.00", "手续费": "5.00"})
    assert rec["date"] == "20261113" and rec["code"] == "603881"
    assert rec["name"] == "数据港"
    assert rec["side"] == "buy"
    assert rec["quantity"] == 600.0 and rec["price"] == 20.01
    assert rec["amount"] == 12006.0 and rec["fee"] == 5.0


def test_fills_record_side_variants():
    assert tc.yh_normalize_fill_record({"业务名称": "证券卖出"})["side"] == "sell"
    assert tc.yh_normalize_fill_record({"业务名称": "银行证券转入"})["side"] is None


def test_fills_from_items_header_mapping_and_nav_exclusion():
    rows = tc.yh_fills_from_items(_fills_page_items())
    assert len(rows) == 2                       # 导航树/查询控件不成行
    first, second = rows
    assert first["code"] == "603881" and first["side"] == "buy"
    assert first["quantity"] == 600.0 and first["price"] == 20.01
    assert second["side"] == "sell" and second["quantity"] == 200.0
    assert second["price"] == 20.08 and second["fee"] == 4.0


def test_fills_duplicate_rows_never_deduped():
    """同一笔重复出现（翻页重叠/界面重复）必须原样保留，绝不静默去重。"""
    dup = _fills_page_items() + [
        _it("20261113", 260.0, 428.0), _it("证券买入", 330.0, 428.0),
        _it("603881", 400.0, 428.0), _it("数据港", 470.0, 428.0),
        _it("600", 540.0, 428.0), _it("20.010", 610.0, 428.0),
        _it("12,006.00", 680.0, 428.0), _it("5.00", 750.0, 428.0)]
    dup = [it for it in dup if it["text"] != "共2条"] + [_it("共3条", 700.0, 590.0)]
    rows = tc.yh_fills_from_items(dup)
    assert len(rows) == 3                        # 完全重复的第 3 行保留


def test_fills_rows_from_vision_records():
    rows = tc.yh_fills_rows_from_vision([
        {"成交日期": "20261113", "业务名称": "证券买入", "证券代码": "603881",
         "成交数量": "600", "成交价格": "20.010", "成交金额": "12006.00"},
        {"证券代码": "导航行", "成交数量": "x"},                    # 无代码行剔除
    ])
    assert len(rows) == 1 and rows[0]["quantity"] == 600.0


# ── 纯解析层：日期/分页证据与页面守卫 ────────────────────────────────

def test_fills_date_tokens_normalized():
    evd = tc.yh_fills_date_tokens([
        _it("20261101", 250.0, 320.0), _it("2026年11月15日", 380.0, 320.0),
        _it("603881", 400.0, 380.0), _it("20.010", 610.0, 380.0)])
    isos = [d["iso"] for d in evd]
    assert "2026-11-01" in isos and "2026-11-15" in isos
    assert all("603881" not in (d["token"] or "") for d in evd)


def test_fills_page_evidence_count_and_pagination():
    evd = tc.yh_fills_page_evidence([_it("共22条", 700.0, 590.0),
                                     _it("下一页", 760.0, 590.0)])
    assert evd["count_total"] == 22 and evd["next_page_present"] is True
    evd2 = tc.yh_fills_page_evidence([_it("第1页/共3页", 700.0, 590.0)])
    assert evd2["page_current"] == 1 and evd2["page_total"] == 3
    assert evd2["count_total"] is None


def test_fills_wrong_page_guard_orders_like_page_rejected():
    """历史委托页（委托时间/申报价格表头）不得冒充历史逐笔成交。"""
    evd = tc.yh_fills_page_evidence([_it("委托时间", 260.0, 350.0),
                                     _it("申报价格", 610.0, 350.0),
                                     _it("合同号", 680.0, 350.0),
                                     _it("603881", 400.0, 380.0)])
    assert evd["orders_like_page"] is True
    assert evd["trade_like_page"] is False
    verdict = tc.yh_fills_verdict(_fills_ev(wrong_page=True))
    assert verdict["status"] == "unavailable"
    assert verdict["reason"] == "wrong_page"


# ── 裁决纯函数 yh_fills_verdict ─────────────────────────────────────

def _row(qty="600"):
    return {"date": "20261113", "time": "09:30:00", "side": "buy", "code": "603881",
            "name": "数据港", "quantity": float(qty), "price": 20.01,
            "amount": 12006.0, "fee": 5.0}


def test_fills_verdict_ok_with_full_evidence():
    rows = [_row(), _row("200")]
    v = tc.yh_fills_verdict(_fills_ev(rows=rows, row_count=2, count_total=2))
    assert v["status"] == "ok" and v["coverage_complete"] is True
    assert v["date_range_proven"] is True and v["row_count"] == 2


def test_fills_verdict_partial_when_count_exceeds_rows():
    v = tc.yh_fills_verdict(_fills_ev(rows=[_row()], row_count=1, count_total=22))
    assert v["status"] == "partial"
    assert v["reason"] == "pagination_incomplete"


def test_fills_verdict_partial_when_coverage_unproven():
    v = tc.yh_fills_verdict(_fills_ev(rows=[_row()], row_count=1))
    assert v["status"] == "partial" and v["reason"] == "coverage_unproven"


def test_fills_verdict_partial_when_date_range_missing():
    v = tc.yh_fills_verdict(_fills_ev(rows=[_row()], row_count=1,
                                      count_total=1, date_tokens=[],
                                      date_range=None))
    assert v["status"] == "partial" and v["reason"] == "date_range_unproven"


def test_fills_verdict_empty_not_proven_is_unavailable():
    v = tc.yh_fills_verdict(_fills_ev(rows=[], row_count=0))
    assert v["status"] == "unavailable" and v["reason"] == "empty_not_proven"


def test_fills_verdict_empty_proven_ok_with_zero_count():
    v = tc.yh_fills_verdict(_fills_ev(rows=[], row_count=0, count_total=0))
    assert v["status"] == "ok" and v["coverage_complete"] is True


def test_fills_verdict_not_attempted_unavailable():
    for reason in ("fills_not_requested", "owner_active_zero_input",
                   "fills_page_not_located"):
        v = tc.yh_fills_verdict(_fills_ev(attempted=False, reason=reason))
        assert v["status"] == "unavailable" and v["reason"] == reason


# ── 采集器与新链（mock 真机层） ─────────────────────────────────────

@pytest.fixture()
def yh_env(monkeypatch, tmp_path):
    calls = {"clicks": [], "f12": 0, "captures": [], "tree_selects": [],
             "switch_calls": [], "lock_acquire": 0, "lock_release": 0}
    tc._YH_ACCOUNT_STATE["current_used_account"] = None
    monkeypatch.setattr(tc, "_pids_of",
                        lambda name: [33] if name == "happ.exe"
                        else ([34] if name == "xiadan.exe" else []))
    monkeypatch.setattr(tc, "_yh_main_window", _main)
    monkeypatch.setattr(tc, "_yh_enum_panels", lambda: [_panel()])
    monkeypatch.setattr(tc, "_yh_capture_window",
                        lambda hwnd, prefix: (
                            calls["captures"].append(prefix) or {
                                "path": str(tmp_path / f"{prefix}.png"),
                                "width": 800, "height": 600}))
    monkeypatch.setattr(tc, "_yh_uia_texts", lambda hwnd: [])
    monkeypatch.setattr(tc, "_yh_uia_login_page", lambda hwnd: False)

    def fake_tree(hwnd, name="资金股票"):
        calls["tree_selects"].append(name)
        return {"ok": True, "method": "uia_selection", "item": name}

    monkeypatch.setattr(tc, "_yh_uia_select_tree_item", fake_tree)
    monkeypatch.setattr(tc, "_yh_owner_using", lambda hwnd: False)
    monkeypatch.setattr(tc, "_yh_send_f12",
                        lambda hwnd: (calls.__setitem__(
                            "f12", calls["f12"] + 1)
                            or {"foreground_ok": True, "via": "keybd_event"}))
    monkeypatch.setattr(tc, "_write_report",
                        lambda report, root=None, prefix="report":
                        tmp_path / f"{prefix}.json")

    def fake_switch(**kwargs):
        calls["switch_calls"].append(kwargs)
        return {"status": "switched", "rc": 0, "from_account": "acct1",
                "masked_id_after": MASKED_A2, "evidence": {"verdict": "verified"}}

    monkeypatch.setattr(tc, "run_switch_account", fake_switch)

    def fake_acquire(timeout_seconds=10.0, lock_path=None):
        calls["lock_acquire"] += 1
        return {"acquired": True, "path": "mock://yh.lock", "fh": None}

    def fake_release(lock):
        calls["lock_release"] += 1

    monkeypatch.setattr(tc, "_yh_acquire_gui_lock", fake_acquire)
    monkeypatch.setattr(tc, "_yh_release_gui_lock", fake_release)
    monkeypatch.setattr(tc, "_yh_input_idle_seconds", lambda: 9999.0)
    calls["_saved_read"] = tc._yh_read_image
    return calls


def _install_reads(monkeypatch, reads):
    queue = list(reads)

    def seq(_path):
        return queue.pop(0) if queue else {
            "items": [], "rows": [], "reader": None, "errors": []}

    monkeypatch.setattr(tc, "_yh_read_image", seq)


def _holdings_rows():
    return [{"code": "601238", "name": "协鑫科技", "quantity": "4100",
             "cost_price": "8.50", "current_price": "8.71",
             "floating_profit": "861.0"}]


def _ok_reads(fill_items=None):
    return [
        {"items": _masked_items(), "rows": [], "reader": "vision_model",
         "errors": []},
        {"items": _masked_items(), "rows": _holdings_rows(),
         "reader": "vision_model", "errors": []},
        {"items": _masked_items() + (fill_items or []),
         "rows": [], "reader": "ocr_local", "errors": []},
    ]


def _run_v1_fills_snapshot(*, account=None, with_fills=False, settle_sec=0, menu_wait_sec=0):
    """V1 单账号采集器+两级纯裁决测试入口（对应 _yh_collect_single_snapshot + yh_single_snapshot/yh_fills_verdict）。"""
    _ = settle_sec, menu_wait_sec
    lock = tc._yh_acquire_gui_lock(timeout_seconds=10.0)
    if not lock.get("acquired"):
        return {"rc": 4, "status": "lock_busy", "account": None, "funds": {},
                "holdings": [], "fills": {"status": "unavailable", "reason": "lock_busy"}}
    rep = {"steps": [], "captures": [], "capture_path": None, "input_idle_probes": []}
    try:
        ev = tc._yh_collect_single_snapshot(
            account=account, with_fills=with_fills, f12_wait=0.0, main_wait=0.0, report=rep)
    finally:
        tc._yh_release_gui_lock(lock)
    abort = ev.get("abort_status")
    if abort:
        return {
            "schema": tc.SCHEMA, "command": "read-account", "yuanhang": True,
            "credentials_touched": False, "rc": 4, "status": abort,
            "account": None, "funds": {}, "holdings": [],
            "fills": {"status": "unavailable", "reason": abort, "rows": []},
        }
    fills_ev = ev.get("fills") or {}
    if fills_ev.get("identity_reject"):
        return {
            "schema": tc.SCHEMA, "command": "read-account", "yuanhang": True,
            "credentials_touched": False, "rc": 4, "status": "identity_changed_fail_closed",
            "account": None, "funds": {}, "holdings": [],
            "fills": {"status": "unavailable", "reason": "identity_changed_fail_closed", "rows": []},
        }
    snap = tc.yh_single_snapshot(ev)
    if fills_ev.get("attempted"):
        fev_check = dict(fills_ev)
        if not fev_check.get("date_range") and fev_check.get("date_tokens"):
            isos = sorted({d["iso"] for d in fev_check["date_tokens"] if d.get("iso")})
            if len(isos) >= 2:
                fev_check["date_range"] = {"start": isos[0], "end": isos[-1]}
                fev_check["date_range_source"] = "query_controls_readback"
        verdict = tc.yh_fills_verdict(fev_check)
        fills_out = dict(fev_check)
        fills_out.update({
            "status": verdict.get("status"),
            "reason": verdict.get("reason"),
            "row_count": verdict.get("row_count"),
            "coverage_complete": verdict.get("coverage_complete"),
        })
    else:
        fills_out = {"status": "unavailable", "reason": fills_ev.get("reason", "fills_not_requested"), "rows": []}
    snap_ok = snap.get("status") == "ok"
    fills_ok = (not fills_ev.get("attempted")) or fills_out.get("status") == "ok"
    status = "ok" if snap_ok and fills_ok else "partial"
    return {
        "schema": tc.SCHEMA, "command": "read-account", "yuanhang": True,
        "credentials_touched": False, "rc": 0, "status": status,
        "account": ev.get("expected_account") or ev.get("identity_before"),
        "identity": {"before": ev.get("identity_before"), "after": ev.get("identity_after")},
        "funds": ev.get("funds") or {},
        "funds_labels": ev.get("funds_labels") or {},
        "funds_read_ok": bool(ev.get("funds")),
        "holdings": ev.get("holdings") or [],
        "holdings_source": ev.get("holdings_source"),
        "holdings_note": ev.get("holdings_note", ""),
        "page_switch": ev.get("page_switch"),
        "snapshot_decision": snap,
        "fills": fills_out,
        "input_idle_probes": rep.get("input_idle_probes", []),
    }


def test_route_ok_full_chain_with_fills(yh_env, monkeypatch):
    _install_reads(monkeypatch, _ok_reads(_fills_page_items()))
    monkeypatch.setattr(tc, "_yh_uia_texts", lambda hwnd: _funds_uia_texts())
    report = _run_v1_fills_snapshot(with_fills=True, settle_sec=0,
                                    menu_wait_sec=0)
    assert report["rc"] == 0 and report["status"] == "ok"
    assert report["account"] == "acct1"
    # 既有资金读取逻辑回归（防空 stub 复辟）
    assert report["funds"]["balance"] == 50000.0
    assert report["funds"]["total_assets"] == 112842.68
    assert report["funds_read_ok"] is True
    # 身份前后核验
    assert report["identity"]["before"] == "acct1"
    assert report["identity"]["after"] == "acct1"
    # 历史逐笔成交段
    fills = report["fills"]
    assert fills["status"] == "ok" and fills["row_count"] == 2
    assert fills["date_range"] == {"start": "2026-11-01", "end": "2026-11-15"}
    assert fills["coverage_complete"] is True
    assert fills["rows"][0]["code"] == "603881"
    assert fills["rows"][0]["side"] == "buy"
    assert yh_env["tree_selects"] == ["资金股票", "历史成交"]
    assert len(yh_env["captures"]) == 3
    # GUI 串行锁：核验与交互全程持锁
    assert yh_env["lock_acquire"] == 1 and yh_env["lock_release"] == 1
    # 新链绝不切号
    assert yh_env["switch_calls"] == []


def test_route_fills_default_off(yh_env, monkeypatch):
    _install_reads(monkeypatch, _ok_reads()[:2])
    monkeypatch.setattr(tc, "_yh_uia_texts", lambda hwnd: _funds_uia_texts())
    report = _run_v1_fills_snapshot(settle_sec=0, menu_wait_sec=0)
    assert report["rc"] == 0
    assert report["fills"]["status"] == "unavailable"
    assert report["fills"]["reason"] == "fills_not_requested"
    assert yh_env["tree_selects"] == ["资金股票"]


def test_route_wrong_account_never_switches_fail_closed(yh_env, monkeypatch):
    _install_reads(monkeypatch, _ok_reads()[:1])
    report = _run_v1_fills_snapshot(account="acct2", settle_sec=0,
                                    menu_wait_sec=0)
    assert report["rc"] == 4
    assert report["status"] == "wrong_account_fail_closed"
    assert yh_env["switch_calls"] == []          # 动态证明：零切换调用
    assert yh_env["tree_selects"] == []          # 不在他人面板上翻页


def test_route_owner_active_zero_input_and_fills_skipped(yh_env, monkeypatch):
    monkeypatch.setattr(tc, "_yh_owner_using", lambda hwnd: True)
    _install_reads(monkeypatch, [
        {"items": _masked_items(), "rows": [], "reader": "vision_model",
         "errors": []}])
    monkeypatch.setattr(tc, "_yh_uia_texts", lambda hwnd: _funds_uia_texts())
    report = _run_v1_fills_snapshot(with_fills=True, settle_sec=0,
                                    menu_wait_sec=0)
    assert report["rc"] == 0                     # 身份+资金可归属，部分降级
    assert report["status"] == "partial"
    assert yh_env["f12"] == 0 and yh_env["tree_selects"] == []
    assert report["page_switch"]["attempted"] is False
    assert report["fills"]["status"] == "unavailable"
    assert report["fills"]["reason"] == "owner_active_zero_input"


def test_route_lock_busy_fail_closed_zero_interaction(yh_env, monkeypatch):
    monkeypatch.setattr(tc, "_yh_acquire_gui_lock",
                        lambda timeout_seconds=10.0, lock_path=None: {
                            "acquired": False, "path": "mock://yh.lock",
                            "fh": None})
    _install_reads(monkeypatch, _ok_reads())
    report = _run_v1_fills_snapshot(settle_sec=0, menu_wait_sec=0)
    assert report["rc"] == 4 and report["status"] == "lock_busy"
    assert yh_env["captures"] == []              # 零截图零交互
    assert yh_env["tree_selects"] == []


def test_route_ocr_unavailable_partial_honest(yh_env, monkeypatch):
    _install_reads(monkeypatch, [
        {"items": _masked_items(), "rows": [], "reader": None,
         "errors": ["vision_offline"]},
        {"items": _masked_items(), "rows": [], "reader": None,
         "errors": []}])
    monkeypatch.setattr(tc, "_yh_uia_texts", lambda hwnd: _funds_uia_texts())
    report = _run_v1_fills_snapshot(settle_sec=0, menu_wait_sec=0)
    assert report["rc"] == 0 and report["status"] == "partial"
    assert report["holdings"] == []
    assert report["holdings_source"] == "ocr_unavailable"
    assert report["snapshot_decision"]["status"] != "ok"


def test_route_fills_unparseable_page_partial_not_fabricated(
        yh_env, monkeypatch):
    reads = _ok_reads(fill_items=[_it("无法解析的页面", 400.0, 350.0)])
    _install_reads(monkeypatch, reads)
    monkeypatch.setattr(tc, "_yh_uia_texts", lambda hwnd: _funds_uia_texts())
    report = _run_v1_fills_snapshot(with_fills=True, settle_sec=0,
                                    menu_wait_sec=0)
    assert report["rc"] == 0 and report["status"] == "partial"
    assert report["fills"]["status"] in ("partial", "unavailable")
    assert report["fills"]["rows"] == []         # 绝不造数


def test_route_identity_changed_mid_run_fail_closed(yh_env, monkeypatch):
    reads = _ok_reads(_fills_page_items())
    reads[2] = {"items": _masked_items(MASKED_A2), "rows": [],
                "reader": "ocr_local", "errors": []}   # fills 页身份变 acct2
    _install_reads(monkeypatch, reads)
    monkeypatch.setattr(tc, "_yh_uia_texts", lambda hwnd: _funds_uia_texts())
    report = _run_v1_fills_snapshot(with_fills=True, settle_sec=0,
                                    menu_wait_sec=0)
    assert report["rc"] == 4
    assert report["status"] == "identity_changed_fail_closed"
    assert report["fills"]["status"] == "unavailable"
    assert report["fills"]["rows"] == []         # 不归属任何账号


def test_route_owner_globally_active_no_f12_no_panel(yh_env, monkeypatch):
    """run3 实机教训回归：Owner 全局活跃（空闲<90s）时，即使面板缺席
    也绝不 F12 注入（不抢焦点），fail-closed 零交互退出。"""
    monkeypatch.setattr(tc, "_yh_input_idle_seconds", lambda: 3.4)
    monkeypatch.setattr(tc, "_yh_enum_panels", lambda: [])
    report = _run_v1_fills_snapshot(with_fills=True, settle_sec=0,
                                    menu_wait_sec=0)
    assert report["rc"] == 4
    assert report["status"] == "panel_unavailable_fail_closed"
    assert yh_env["f12"] == 0                   # 绝不注入 F12
    assert yh_env["tree_selects"] == []
    assert yh_env["captures"] == []             # 零截图（无面板零交互）


def test_route_owner_globally_active_zero_input_mode(yh_env, monkeypatch):
    """面板在场但 Owner 全局活跃 → 零输入模式：只读截图/UIA，不翻页、
    fills 跳过；资金+身份可归属时 rc0 partial。"""
    monkeypatch.setattr(tc, "_yh_input_idle_seconds", lambda: 5.0)
    _install_reads(monkeypatch, [
        {"items": _masked_items(), "rows": [], "reader": "vision_model",
         "errors": []}])
    monkeypatch.setattr(tc, "_yh_uia_texts", lambda hwnd: _funds_uia_texts())
    report = _run_v1_fills_snapshot(with_fills=True, settle_sec=0,
                                    menu_wait_sec=0)
    assert report["rc"] == 0 and report["status"] == "partial"
    assert yh_env["f12"] == 0 and yh_env["tree_selects"] == []
    assert report["page_switch"]["attempted"] is False
    assert report["fills"]["reason"] == "owner_active_zero_input"
    # 页面未确认 → 行数据空仓未证实
    assert report["holdings"] == []
    assert report["holdings_note"]
    assert report["input_idle_probes"]


def test_route_signature_keeps_allow_switch_compatible():
    import inspect

    sig = inspect.signature(tc.run_read_account)
    assert sig.parameters["allow_switch"].default is False


def test_route_switchable_legacy_auto_switch_preserved(yh_env, monkeypatch):
    monkeypatch.setattr(tc, "_yh_enum_panels", lambda: [_panel(hwnd=333)])
    reads = [
        {"items": _masked_items(), "rows": [], "reader": "vision_model",
         "errors": []},
        {"items": _masked_items(MASKED_A2), "rows": [],
         "reader": "vision_model", "errors": []},
    ]
    _install_reads(monkeypatch, reads)
    monkeypatch.setattr(tc, "_yh_uia_texts", lambda hwnd: _funds_uia_texts())
    report = tc.run_read_account_switchable(account="acct2", settle_sec=0,
                                            menu_wait_sec=0)
    assert report["rc"] == 0 and report["account"] == "acct2"
    assert report["masked_id"] == MASKED_A2
    assert yh_env["switch_calls"][0]["to"] == "acct2"
    assert report["switch"]["status"] == "switched"


def test_cli_read_account_fills_envelope(yh_env, monkeypatch, capsys):
    _install_reads(monkeypatch, _ok_reads(_fills_page_items()))
    monkeypatch.setattr(tc, "_yh_uia_texts", lambda hwnd: _funds_uia_texts())
    monkeypatch.setattr(tc, "run_read_account",
                        lambda **kw: _run_v1_fills_snapshot(account=kw.get("account"),
                                                            with_fills=kw.get("with_fills", False)))
    rc = tc.main(["read-account", "--fills"])
    out = capsys.readouterr().out
    assert rc == 0
    envelope = json.loads(out)
    assert envelope["schema"] == "ths_client/1"
    assert envelope["command"] == "read-account"
    assert envelope["yuanhang"] is True
    assert envelope["credentials_touched"] is False
    assert envelope["fills"]["status"] == "ok"
    assert envelope["identity"]["after"] == "acct1"


# ── 实机布局回归（2026-11-15 远航版历史成交页真实列头；值全合成） ───

def _real_fills_items():
    """真实坐标骨架（x 取自实机 OCR dump；数值合成）：工具条含「汇总」
    按钮行（无数值），底部真合计行——防误取按钮行的回归锚。"""
    def row(y, cells):
        return [_it(t, x, y) for t, x in cells]

    items = []
    items += row(97.0, [("当日", 241.5), ("近一周", 296.0), ("过滤", 761.5),
                        ("汇总", 836.5)])          # 工具条：含"汇总"无数值
    items += row(127.0, [("成交日期", 242.0), ("成交时间", 305.0),
                         ("证券代码", 368.5), ("证券名称", 430.5),
                         ("操作", 494.5), ("成交数量", 563.0),
                         ("成交均价", 635.5), ("成交金额", 715.0),
                         ("股票余额", 794.0), ("合同编号", 865.0),
                         ("成交编号", 936.5), ("印花税", 1005.0),
                         ("发生金额", 1090.5)])
    items += row(144.5, [("20261112", 241.0), ("09 : 42:29", 304.0),
                         ("002015", 361.0), ("协鑫能科", 429.0),
                         ("证券买入", 493.0), ("100", 584.0),
                         ("13. 370", 646.5), ("1337.000", 726.0),
                         ("4100", 811.0), ("14499", 848.5),
                         ("010400.", 927.0), ("0. 000", 1016.0),
                         ("-1342", 1099.0)])
    items += row(160.5, [("20261111", 240.0), ("11: 12:29", 304.5),
                         ("002015", 361.5), ("协鑫能科", 429.0),
                         ("证券卖出", 494.5), ("-2000", 578.0),
                         ("13. 300", 646.5), ("26600. 000", 722.5),
                         ("2100", 811.0), ("37671", 849.0),
                         ("010400.", 926.0), ("13. 300", 1013.5),
                         ("26581", 1098.0)])
    items += row(176.5, [("20261111", 240.0), ("10:04:30", 305.0),
                         ("002015", 361.5), ("协鑫能科", 429.0),
                         ("证券买入", 494.0), ("2000", 578.0),
                         ("13. 330", 646.5), ("26660.000", 722.5),
                         ("4100", 811.0), ("37672", 849.0),
                         ("010400.", 926.0), ("0. 000", 1014.0),
                         ("-26673", 1097.0)])
    # 合计行：数量 100-2000+2000=100；金额 1337+26600+26660=54597；
    # 印花税 0+13.3+0=13.3；发生金额 -1342+26581-26673=-1434
    items += row(589.5, [("汇总", 229.0), ("100", 578.0),
                         ("54597. 000", 719.5), ("13.300", 1010.0),
                         ("-1434", 1097.5)])
    return items


def test_fills_real_layout_columns_parse():
    rows = tc.yh_fills_from_items(_real_fills_items())
    assert len(rows) == 3
    first = rows[0]
    assert first["code"] == "002015" and first["side"] == "buy"
    assert first["date"] == "20261112"
    assert first["time"] == "09:42:29"        # OCR 冒号空格清洗
    assert first["quantity"] == 100.0
    assert first["price"] == 13.37            # 成交均价 → price
    assert first["amount"] == 1337.0
    assert first["stamp_tax"] == 0.0
    assert first["net_amount"] == -1342.0     # 发生金额 → net_amount
    assert first["balance_after"] == 4100.0   # 股票余额 → balance_after
    assert rows[1]["side"] == "sell"
    assert rows[1]["quantity"] == -2000.0     # 卖出负量原样保留
    assert rows[1]["stamp_tax"] == 13.3


def test_fills_summary_evidence_skips_toolbar_button_row():
    summary = tc.yh_fills_summary_evidence(_real_fills_items())
    assert summary["present"] is True
    assert summary["quantity"] == 100.0
    assert summary["amount"] == 54597.0
    assert summary["stamp_tax"] == 13.3
    assert summary["net_amount"] == -1434.0


def test_fills_verdict_ok_via_summary_reconciliation():
    rows = tc.yh_fills_from_items(_real_fills_items())
    date_tokens = [{"token": "20261111", "iso": "2026-11-11"},
                   {"token": "20261112", "iso": "2026-11-12"}]
    fev = _fills_ev(rows=rows, row_count=len(rows), count_total=len(rows),
                    date_tokens=date_tokens,
                    date_range={"start": "2026-11-11", "end": "2026-11-12"},
                    summary=tc.yh_fills_summary_evidence(_real_fills_items()))
    v = tc.yh_fills_verdict(fev)
    assert v["status"] == "ok" and v["coverage_complete"] is True
    assert v["coverage_evidence"] == "count_match"


def test_fills_verdict_partial_when_summary_mismatch():
    rows = tc.yh_fills_from_items(_real_fills_items())
    summary = tc.yh_fills_summary_evidence(_real_fills_items())
    summary["quantity"] = 999.0               # 对不上 → 不可宣称完整
    fev = _fills_ev(rows=rows, row_count=len(rows), count_total=len(rows),
                    date_range={"start": "2026-11-11", "end": "2026-11-12"},
                    summary=summary)
    v = tc.yh_fills_verdict(fev)
    assert v["status"] == "partial" and v["reason"] == "summary_mismatch"


def test_fills_summary_quantity_accepts_absolute_net_form():
    """实机事实：汇总行数量=净量绝对值（行集 −3900 ↔ 汇总 3900）。"""
    rows = [{"code": "002015", "quantity": -3900.0, "amount": 580472.0,
             "fee": None, "stamp_tax": None, "net_amount": None,
             "price": None},
            {"code": "002015", "quantity": 500.0, "amount": 0.0,
             "fee": None, "stamp_tax": None, "net_amount": None,
             "price": None}]
    fev = {"rows": rows[:1],
           "summary": {"present": True, "quantity": 3900.0,
                       "amount": 580472.0}}
    assert tc.yh_fills_summary_reconciled(fev) is True
    fev2 = {"rows": rows,
            "summary": {"present": True, "quantity": 3900.0,
                        "amount": 580472.0}}
    # 数量合计被第二行破坏（−3900+500=−3400 ≠ ±3900）→ 对不上
    assert tc.yh_fills_summary_reconciled(fev2) is False


# ── GUI 锁原语（tmp 目录内真实 msvcrt 互斥，不触项目 runtime） ──────

def test_gui_lock_mutual_exclusion(tmp_path):
    lock_path = tmp_path / "yh.lock"
    first = tc._yh_acquire_gui_lock(timeout_seconds=0.2,
                                    lock_path=lock_path)
    assert first["acquired"] is True
    second = tc._yh_acquire_gui_lock(timeout_seconds=0.2,
                                     lock_path=lock_path)
    assert second["acquired"] is False
    tc._yh_release_gui_lock(first)
    third = tc._yh_acquire_gui_lock(timeout_seconds=0.2,
                                    lock_path=lock_path)
    assert third["acquired"] is True
    tc._yh_release_gui_lock(third)
