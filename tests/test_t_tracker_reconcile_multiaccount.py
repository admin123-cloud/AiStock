# -*- coding: utf-8 -*-
"""R47 reconcile 多账号单测：分账号台账 + 合并日报 + 单账号输出不变。"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.t_trade_tracker as ttt  # noqa: E402

TODAY = date(2026, 11, 11)
STAMP = "20261111"


def _fills_payload(rows, account="acct1"):
    return {"schema": "ths_fills_export/1", "status": "ok", "account": account,
            "trade_date": TODAY.isoformat(),
            "captured_at": "2026-11-11 15:05:00", "fills": rows}


SELL1 = {"trade_time": "2026-11-11 09:38:52", "side": "SELL", "code": "002015",
         "name": "协鑫能科", "shares": 100, "price": 13.72, "amount": 1372.0,
         "source": "ths_today_trades_ocr", "account": "acct1"}
BUY1 = {"trade_time": "2026-11-11 10:30:00", "side": "BUY", "code": "002015",
        "name": "协鑫能科", "shares": 100, "price": 13.50, "amount": 1350.0,
        "source": "ths_today_trades_ocr", "account": "acct1"}
BUY2 = {"trade_time": "2026-11-11 10:00:00", "side": "BUY", "code": "603881",
        "name": "数据港", "shares": 500, "price": 20.10, "amount": 10050.0,
        "source": "ths_today_trades_ocr", "account": "acct2"}


def _make_root(tmp_path: Path, with_acct2: bool) -> Path:
    root = tmp_path / "t_tracker"
    (root / "fills").mkdir(parents=True)
    (root / "fills" / f"{STAMP}.json").write_text(
        json.dumps(_fills_payload([SELL1, BUY1])), encoding="utf-8")
    if with_acct2:
        (root / "fills" / f"{STAMP}_acct2.json").write_text(
            json.dumps(_fills_payload([BUY2], account="acct2")), encoding="utf-8")
    return root


def test_reconcile_multi_writes_per_account_and_merged(tmp_path):
    root = _make_root(tmp_path, with_acct2=True)
    merged = ttt.reconcile_all_accounts(root=root, for_date=TODAY)
    ledger = root / "ledger"
    assert (ledger / f"{STAMP}.jsonl").is_file()              # acct1 legacy
    assert (ledger / f"{STAMP}_summary.json").is_file()
    assert (ledger / f"{STAMP}_acct2.jsonl").is_file()        # acct2 台账
    assert (ledger / f"{STAMP}_acct2_summary.json").is_file()
    merged_file = ledger / f"{STAMP}_merged_summary.json"
    assert merged_file.is_file()
    data = json.loads(merged_file.read_text(encoding="utf-8"))
    assert set(data["accounts"]) == {"acct1", "acct2"}
    assert data["merged"]["t_round_trips"] == (
        data["accounts"]["acct1"]["t_round_trips"]
        + data["accounts"]["acct2"]["t_round_trips"])
    assert abs(data["merged"]["net_pnl"] - sum(
        a["net_pnl"] for a in data["accounts"].values())) < 0.01
    # acct2 台账只含 acct2 成交
    acct2_rows = [json.loads(ln) for ln in
                  (ledger / f"{STAMP}_acct2.jsonl").read_text(encoding="utf-8").splitlines() if ln]
    codes = {r.get("code") or (r.get("fill") or {}).get("code") for r in acct2_rows}
    assert codes == {"603881"}


def test_reconcile_single_account_legacy_shape(tmp_path):
    # acct2 无 fills → 输出与 legacy 完全同形（无 _acct2 / 无 merged 文件）
    root = _make_root(tmp_path, with_acct2=False)
    merged = ttt.reconcile_all_accounts(root=root, for_date=TODAY)
    ledger = root / "ledger"
    assert (ledger / f"{STAMP}.jsonl").is_file()
    assert not (ledger / f"{STAMP}_acct2.jsonl").exists()
    assert not (ledger / f"{STAMP}_merged_summary.json").exists()
    assert merged["accounts"] == ["acct1"]


def test_reconcile_acct1_output_unchanged_by_acct2(tmp_path):
    # acct1 台账内容不因 acct2 在场而变化（隔离）
    r_a = _make_root(tmp_path / "a", with_acct2=False)
    r_b = _make_root(tmp_path / "b", with_acct2=True)
    ttt.reconcile_all_accounts(root=r_a, for_date=TODAY)
    ttt.reconcile_all_accounts(root=r_b, for_date=TODAY)
    la = (r_a / "ledger" / f"{STAMP}.jsonl").read_text(encoding="utf-8")
    lb = (r_b / "ledger" / f"{STAMP}.jsonl").read_text(encoding="utf-8")
    assert la == lb
    sa = json.loads((r_a / "ledger" / f"{STAMP}_summary.json").read_text(encoding="utf-8"))
    sb = json.loads((r_b / "ledger" / f"{STAMP}_summary.json").read_text(encoding="utf-8"))
    sa.pop("generated_at"), sb.pop("generated_at")
    assert sa == sb
