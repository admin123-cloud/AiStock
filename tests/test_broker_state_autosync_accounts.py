# -*- coding: utf-8 -*-
"""R47 broker_state 多账号结构单测：accounts 包裹 + legacy 镜像 + acct2 接入/跳过/隔离。"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import broker_state_autosync as bsa  # noqa: E402

TODAY = date(2026, 11, 11)
NOW = datetime(2026, 11, 11, 15, 15, 0)
STAMP = "20261111"

LEGACY_STATE = {
    "holdings": [
        {"code": "002015", "name": "协鑫能科", "shares": 4000, "available_shares": 4000,
         "cost_price": 18.005, "current_price": 13.64, "market_value": 54560.0,
         "source": "ths_fills_autosync"},
    ],
    "capital": {"available_cash": 54974.81, "market_value": 60693.0,
                "total_capital": 115767.81},
    "updated_at": "2026-11-10 17:47:00",
    "holding_source": "ths_fills_autosync",
    "broker_account_id": "8890729932",
    "fills_sync": {
        "schema": "broker_fills_sync/1",
        "base_as_of": "2026-11-09",
        "base": {"002015": {"shares": 4000, "cost_price": 22.377}},
        "base_cash_available": 54818.15,
        "applied": [],
    },
}


def _write_legacy(tmp_path: Path) -> Path:
    f = tmp_path / "broker_state.json"
    f.write_text(json.dumps(LEGACY_STATE, ensure_ascii=False), encoding="utf-8")
    return f


def _fills(rows, account="acct1"):
    return {
        "schema": "ths_fills_export/1", "status": "ok", "account": account,
        "trade_date": TODAY.isoformat(), "captured_at": NOW.isoformat(sep=" "),
        "fills": rows,
    }


SELL_ROW = {"trade_time": f"{TODAY.isoformat()} 09:38:52", "side": "SELL",
            "code": "002015", "name": "协鑫能科", "shares": 100, "price": 13.72,
            "amount": 1372.0, "source": "ths_today_trades_ocr", "account": "acct1"}


# ── 包裹与镜像 ────────────────────────────────────────────────

def test_acct1_sync_wraps_with_legacy_mirror(tmp_path):
    state_file = _write_legacy(tmp_path)
    fills_file = tmp_path / f"{STAMP}.json"
    fills_file.write_text(json.dumps(_fills([SELL_ROW])), encoding="utf-8")
    result = bsa.run_sync(
        state_file=state_file, fills_file=fills_file,
        alerts_file=tmp_path / "alerts.jsonl",
        report_file=tmp_path / f"{STAMP}.json",
        history_file=tmp_path / "history.jsonl",
        for_date=TODAY, now=NOW, account="acct1")
    assert result["rc"] == 0 and result["action"] == "write"
    data = json.loads(state_file.read_text(encoding="utf-8"))
    # accounts 结构 + acct1 镜像（legacy 消费者零改动）
    assert "accounts" in data and "acct1" in data["accounts"]
    assert data["accounts"]["acct1"]["holdings"] == data["holdings"]
    assert data["accounts"]["acct1"]["capital"] == data["capital"]
    assert data["accounts"]["acct1"]["fills_sync"] == data["fills_sync"]
    assert data["active_view"]["accounts"] == ["acct1"]
    # 成交已应用：4000-100=3900
    row = next(h for h in data["holdings"] if h["code"] == "002015")
    assert row["shares"] == 3900


def test_legacy_state_idempotent_noop_stays_unwrapped(tmp_path):
    # 无新成交 noop 零写纪律：不因多账号包裹而写文件
    state_file = _write_legacy(tmp_path)
    fills_file = tmp_path / f"{STAMP}.json"
    fills_file.write_text(json.dumps(_fills([])), encoding="utf-8")
    before = state_file.read_bytes()
    result = bsa.run_sync(
        state_file=state_file, fills_file=fills_file,
        alerts_file=tmp_path / "alerts.jsonl",
        report_file=tmp_path / f"{STAMP}.json",
        history_file=tmp_path / "history.jsonl",
        for_date=TODAY, now=NOW, account="acct1")
    assert result["rc"] == 0 and result["action"] == "noop"
    assert state_file.read_bytes() == before  # 零写


def test_wrapped_state_roundtrip_acct1(tmp_path):
    # 已包裹状态再同步：accounts.acct1 原位更新，镜像同步
    state_file = _write_legacy(tmp_path)
    fills_file = tmp_path / f"{STAMP}.json"
    fills_file.write_text(json.dumps(_fills([SELL_ROW])), encoding="utf-8")
    args = dict(state_file=state_file, fills_file=fills_file,
                alerts_file=tmp_path / "alerts.jsonl",
                report_file=tmp_path / f"{STAMP}.json",
                history_file=tmp_path / "history.jsonl",
                for_date=TODAY, now=NOW, account="acct1")
    bsa.run_sync(**args)
    fills_file.write_text(json.dumps(_fills(
        [SELL_ROW, {**SELL_ROW, "shares": 1900, "amount": 26068.0,
                    "trade_time": f"{TODAY.isoformat()} 09:38:58"}])),
        encoding="utf-8")
    r2 = bsa.run_sync(**args)
    assert r2["rc"] == 0
    data = json.loads(state_file.read_text(encoding="utf-8"))
    row = next(h for h in data["holdings"] if h["code"] == "002015")
    assert row["shares"] == 2000  # 4000-100-1900


# ── acct2 ────────────────────────────────────────────────────

INIT_BASE_ACCT2 = {
    "base_as_of": "2026-11-10",
    "base": {"603881": {"shares": 3000, "cost_price": 21.5}},
    "base_cash_available": 20000.0,
}

BUY_ROW_ACCT2 = {"trade_time": f"{TODAY.isoformat()} 10:00:00", "side": "BUY",
                 "code": "603881", "name": "数据港", "shares": 500, "price": 20.10,
                 "amount": 10050.0, "source": "ths_today_trades_ocr",
                 "account": "acct2"}


def test_acct2_first_attach_via_init_base(tmp_path):
    state_file = _write_legacy(tmp_path)  # acct1 已有
    fills2 = tmp_path / f"{STAMP}_acct2.json"
    fills2.write_text(json.dumps(_fills([BUY_ROW_ACCT2], account="acct2")),
                      encoding="utf-8")
    result = bsa.run_sync(
        state_file=state_file, fills_file=fills2,
        alerts_file=tmp_path / "alerts.jsonl",
        report_file=tmp_path / f"{STAMP}_acct2.json",
        history_file=tmp_path / "history.jsonl",
        for_date=TODAY, now=NOW, account="acct2",
        init_base=INIT_BASE_ACCT2)
    assert result["rc"] == 0 and result["action"] == "write"
    data = json.loads(state_file.read_text(encoding="utf-8"))
    # acct2 进 accounts；顶层镜像仍是 acct1（002015 持仓不动）
    assert "acct2" in data["accounts"]
    acct2_holdings = data["accounts"]["acct2"]["holdings"]
    row = next(h for h in acct2_holdings if h["code"] == "603881")
    assert row["shares"] == 3500  # 3000+500
    top_codes = {h["code"] for h in data["holdings"]}
    assert top_codes == {"002015"}  # 镜像不混入 acct2
    assert set(data["active_view"]["accounts"]) == {"acct1", "acct2"}


def test_acct2_without_fills_and_unconfigured_skipped(tmp_path):
    # CLI 侧判定逻辑：无 fills 文件且 state 无 acct2 → not_configured（非拒绝）
    state_file = _write_legacy(tmp_path)
    decision = bsa.should_attempt_account(
        full_state=json.loads(state_file.read_text(encoding="utf-8")),
        fills_file=tmp_path / f"{STAMP}_acct2.json", account="acct2")
    assert decision is False


def test_acct2_unavailable_marker_is_refused(tmp_path):
    # 面板在场但采集失败（unavailable 标记）→ fail-closed 拒绝 + 告警
    state_file = _write_legacy(tmp_path)
    # 先把 acct2 挂上（有历史）
    fills2 = tmp_path / f"{STAMP}_acct2.json"
    fills2.write_text(json.dumps(_fills([BUY_ROW_ACCT2], account="acct2")),
                      encoding="utf-8")
    bsa.run_sync(state_file=state_file, fills_file=fills2,
                 alerts_file=tmp_path / "alerts.jsonl",
                 report_file=tmp_path / "r.json", history_file=tmp_path / "h.jsonl",
                 for_date=TODAY, now=NOW, account="acct2",
                 init_base=INIT_BASE_ACCT2)
    # 次日 unavailable
    fills2.unlink()
    (tmp_path / f"{STAMP}_acct2.unavailable.json").write_text(
        json.dumps({"status": "unavailable"}), encoding="utf-8")
    decision = bsa.should_attempt_account(
        full_state=json.loads(state_file.read_text(encoding="utf-8")),
        fills_file=fills2, account="acct2")
    assert decision is True  # 已配置账号 → 必须尝试（fail-closed）

    result = bsa.run_sync(
        state_file=state_file, fills_file=fills2,
        alerts_file=tmp_path / "alerts.jsonl",
        report_file=tmp_path / f"{STAMP}_acct2.json",
        history_file=tmp_path / "history.jsonl",
        for_date=TODAY, now=NOW, account="acct2")
    assert result["rc"] == 1 and result["action"] == "refused"
    assert (tmp_path / "alerts.jsonl").is_file()
    # acct1 数据不受影响
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["holdings"][0]["shares"] == 4000


def test_acct1_always_attempted(tmp_path):
    state_file = _write_legacy(tmp_path)
    decision = bsa.should_attempt_account(
        full_state=json.loads(state_file.read_text(encoding="utf-8")),
        fills_file=tmp_path / f"{STAMP}.json", account="acct1")
    assert decision is True


# ── 持仓并集（票池/观察名单共用口径）───────────────────────────

def test_union_holdings_legacy_passthrough():
    rows = bsa.union_holdings(LEGACY_STATE)
    codes = [r["code"] for r in rows]
    assert codes == ["002015"]
    assert rows[0]["shares"] == 4000
    assert rows[0]["account_detail"] == {"acct1": 4000}
    assert rows[0]["accounts"] == ["acct1"]


def test_union_holdings_dual_account_same_code():
    full = {
        "holdings": [{"code": "002015", "name": "协鑫能科", "shares": 4000}],
        "accounts": {
            "acct1": {"holdings": [{"code": "002015", "name": "协鑫能科", "shares": 4000}]},
            "acct2": {"holdings": [
                {"code": "002015", "name": "协鑫能科", "shares": 1000},
                {"code": "603881", "name": "数据港", "shares": 3000},
            ]},
        },
    }
    rows = bsa.union_holdings(full)
    by_code = {r["code"]: r for r in rows}
    assert by_code["002015"]["shares"] == 5000  # 同码两账号分别计
    assert by_code["002015"]["account_detail"] == {"acct1": 4000, "acct2": 1000}
    assert by_code["603881"]["shares"] == 3000  # 603881 qty 来自 acct2
    assert by_code["603881"]["account_detail"] == {"acct2": 3000}


def test_union_holdings_skips_zero_and_bad_rows():
    full = {
        "holdings": [{"code": "002015", "shares": 0},
                     {"code": "", "shares": 100},
                     {"shares": 100}],
        "accounts": {},
    }
    rows = bsa.union_holdings(full)
    assert rows == []  # 零股/空码/坏行不进并集
