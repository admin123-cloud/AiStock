# -*- coding: utf-8 -*-
"""同花顺远航单账号预填单前置门禁单测（#17 / #28：仅预填、绝不提交/撤单/切号）。"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ths_prefill_gate import PrefillPolicy, evaluate_prefill_gate  # noqa: E402

TD = date(2026, 11, 19)
GOOD_GUARD = {"ok": True, "owner_active": False, "idle_seconds": 120.0}
GOOD_IDENT = {"account": "acct1", "fingerprint": "fp-acct1-8899"}
GOOD_DRAFT = {
    "draft_id": "D-20261119-001",
    "account": "acct1",
    "trade_date": "2026-11-19",
    "code": "300054.SZ",
    "side": "BUY",
    "qty": 300,
    "price": 20.5,
}


class TestThsPrefillGate:
    def test_valid_draft_passes_prefill_only_never_submitted(self):
        res = evaluate_prefill_gate(
            draft=GOOD_DRAFT,
            target_account="acct1",
            trade_date=TD,
            guard_snapshot=GOOD_GUARD,
            identity_snapshot=GOOD_IDENT,
        )
        assert res["allowed"] is True
        assert res["status"] == "prefill_allowed"
        assert res["submitted"] is False
        assert res["broker_order_id"] is None
        assert res["ticket"]["mode"] == "prefill_only"
        assert res["ticket"]["submitted"] is False
        assert res["ticket"]["notional"] == 6150.0

    def test_submit_or_cancel_mode_strictly_forbidden(self):
        res1 = evaluate_prefill_gate(
            draft=GOOD_DRAFT,
            target_account="acct1",
            trade_date=TD,
            guard_snapshot=GOOD_GUARD,
            identity_snapshot=GOOD_IDENT,
            allow_submit=True,
        )
        assert res1["allowed"] is False
        assert "submit_forbidden" in res1["reasons"]
        assert res1["ticket"] is None

        res2 = evaluate_prefill_gate(
            draft=GOOD_DRAFT,
            target_account="acct1",
            trade_date=TD,
            guard_snapshot=GOOD_GUARD,
            identity_snapshot=GOOD_IDENT,
            mode="submit",
        )
        assert res2["allowed"] is False
        assert "submit_forbidden" in res2["reasons"]

    def test_owner_active_or_missing_guard_fails_closed(self):
        for bad_guard in (None, {"ok": True, "owner_active": True}, {"ok": False, "owner_active": False}):
            res = evaluate_prefill_gate(
                draft=GOOD_DRAFT,
                target_account="acct1",
                trade_date=TD,
                guard_snapshot=bad_guard,
                identity_snapshot=GOOD_IDENT,
            )
            assert res["allowed"] is False
            assert "owner_active_or_guard_invalid" in res["reasons"]

    def test_account_mismatch_or_missing_fingerprint_refuses_no_autoswitch(self):
        res_mismatch = evaluate_prefill_gate(
            draft=GOOD_DRAFT,
            target_account="acct1",
            trade_date=TD,
            guard_snapshot=GOOD_GUARD,
            identity_snapshot={"account": "acct2", "fingerprint": "fp-acct2"},
        )
        assert res_mismatch["allowed"] is False
        assert "account_mismatch_no_autoswitch" in res_mismatch["reasons"]

        res_no_fp = evaluate_prefill_gate(
            draft=GOOD_DRAFT,
            target_account="acct1",
            trade_date=TD,
            guard_snapshot=GOOD_GUARD,
            identity_snapshot={"account": "acct1", "fingerprint": ""},
        )
        assert res_no_fp["allowed"] is False
        assert "identity_unreadable" in res_no_fp["reasons"]

    def test_risk_limits_lot_size_stale_date_and_duplicate_id_rejected(self):
        bad_draft = {
            "draft_id": "D-DUP",
            "account": "acct1",
            "trade_date": "2026-11-18",  # 过期草案
            "code": "INVALID",
            "side": "SHORT",
            "qty": 150,  # 非 100 股整数倍
            "price": 10000.0,  # 超金额上限
        }
        res = evaluate_prefill_gate(
            draft=bad_draft,
            target_account="acct1",
            trade_date=TD,
            guard_snapshot=GOOD_GUARD,
            identity_snapshot=GOOD_IDENT,
            consumed_draft_ids=["D-DUP"],
            policy=PrefillPolicy(max_order_qty=500, max_order_notional=10_000.0),
        )
        assert res["allowed"] is False
        for expected in (
            "duplicate_draft_id",
            "draft_date_mismatch",
            "invalid_symbol",
            "invalid_side",
            "qty_not_lot_multiple",
            "notional_exceeds_limit",
        ):
            assert expected in res["reasons"]
