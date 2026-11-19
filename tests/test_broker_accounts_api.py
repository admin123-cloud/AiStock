# -*- coding: utf-8 -*-
"""AiStock 资金账号管理服务与 API 单测（双账号合并、身份错配拒采、同步指令防重入）。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api import accounts as accounts_api  # noqa: E402
from services import broker_accounts as ba  # noqa: E402

BJ_TZ = timezone(timedelta(hours=8))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class TestBrokerAccountsServiceAndApi:
    def test_overview_merges_acct1_and_acct2_and_reads_bridge_heartbeat(self, tmp_path):
        now = datetime(2026, 11, 19, 20, 30, 0, tzinfo=BJ_TZ)
        _write_json(
            tmp_path / "latest_acct1.json",
            {
                "account": "acct1",
                "read_account": {
                    "command": "read-account",
                    "status": "partial",
                    "rc": 0,
                    "account": "acct1",
                    "generated_at": "2026-11-19T19:20:37+08:00",
                    "funds_read_ok": True,
                    "funds": {
                        "total_assets": 110177.68,
                        "market_value": 69243.0,
                        "balance": 40934.68,
                        "available": 40834.68,
                        "withdrawable": 40834.68,
                        "frozen": 100.0,
                        "holding_profit": -35175.33,
                        "daily_profit": -887.0,
                    },
                    "holdings": [{"code": "002015", "name": "协鑫能科", "quantity": 5100}],
                    "fills": {"status": "partial", "rows": [{"code": "002015", "quantity": 1000}]},
                    "identity": {"before": "acct1", "after": "acct1"},
                    "coverage": {"holdings_complete": False, "fills_complete": False},
                },
            },
        )
        _write_json(
            tmp_path / "latest_acct2.json",
            {
                "account": "acct2",
                "read_account": {
                    "command": "read-account",
                    "status": "partial",
                    "rc": 0,
                    "account": "acct2",
                    "generated_at": "2026-11-19T20:28:36+08:00",
                    "funds_read_ok": True,
                    "funds": {
                        "total_assets": 15326.82,
                        "market_value": 11250.0,
                        "balance": 4076.82,
                        "available": 4076.82,
                        "withdrawable": 4076.82,
                        "frozen": 0.0,
                        "holding_profit": -9146.0,
                        "daily_profit": -714.0,
                    },
                    "holdings": [{"code": "603881", "name": "数据港", "quantity": 600}],
                    "fills": {"status": "partial", "rows": []},
                    "identity": {"before": "acct2", "after": "acct2"},
                    "coverage": {"holdings_complete": False, "fills_complete": False},
                },
            },
        )
        _write_json(
            tmp_path / "bridge_heartbeat.json",
            {"heartbeat_at": "2026-11-19T20:29:30+08:00", "state": "idle"},
        )

        ov = ba.get_accounts_overview(root=tmp_path, now=now)
        assert ov["credentials_touched"] is False
        assert ov["accounts_total"] == 2
        assert ov["accounts_ready"] == 2
        assert ov["merged"]["total_assets"] == 125504.5
        assert ov["merged"]["market_value"] == 80493.0
        assert ov["merged"]["available"] == 44911.5
        assert ov["merged"]["daily_profit"] == -1601.0
        assert ov["merged"]["holdings_count"] == 2
        assert ov["bridge"]["online"] is True

    def test_identity_mismatch_fails_closed_no_cross_account_leak(self, tmp_path):
        _write_json(
            tmp_path / "latest_acct2.json",
            {
                "account": "acct2",
                "read_account": {
                    "command": "read-account",
                    "status": "ok",
                    "rc": 0,
                    "account": "acct1",  # 串账污染！
                    "funds_read_ok": True,
                    "funds": {"total_assets": 99999.0},
                    "holdings": [{"code": "002015"}],
                    "identity": {"before": "acct1", "after": "acct1"},
                },
            },
        )
        detail = ba.get_account_detail("acct2", root=tmp_path)
        assert detail["available"] is False
        assert detail["status"] == "identity_mismatch"
        assert detail["funds"] is None
        assert detail["holdings"] == []

    def test_enqueue_sync_request_deduplicates_and_api_routes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ba, "default_accounts_root", lambda: tmp_path)

        d1 = accounts_api.trigger_accounts_sync(
            accounts_api.AccountSyncRequest(accounts=["acct1", "acct2"], with_fills=True)
        )
        assert d1["status"] == "queued" and d1["credentials_touched"] is False

        # 重复点击防重入
        d2 = accounts_api.trigger_accounts_sync(
            accounts_api.AccountSyncRequest(accounts=["acct1", "acct2"])
        )
        assert d2["status"] == "already_queued"
        assert d2["request_id"] == d1["request_id"]

        # 未知账号 400 / 404
        with pytest.raises(HTTPException) as exc1:
            accounts_api.trigger_accounts_sync(accounts_api.AccountSyncRequest(accounts=["acct9"]))
        assert exc1.value.status_code == 400

        with pytest.raises(HTTPException) as exc2:
            accounts_api.account_detail("acct9")
        assert exc2.value.status_code == 404

        ov = accounts_api.accounts_overview()
        assert ov["accounts_total"] == 2 and ov["bridge"]["pending_count"] == 1
