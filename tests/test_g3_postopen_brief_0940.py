# -*- coding: utf-8 -*-
"""G3 09:40 早盘持仓同步与微信确认简报脚本单测（纯 mock，零真机/零微信发送）。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import g3_postopen_brief_0940 as job  # noqa: E402

BJ_TZ = timezone(timedelta(hours=8))


def _sample_overview(*, generated_at: str, online: bool = True, latest_completed: dict | None = None) -> dict:
    return {
        "accounts": [
            {
                "account_id": "acct1",
                "label": "广发证券-曾*",
                "generated_at": generated_at,
                "funds": {"total_assets": 103605.74, "available": 520.14},
                "holdings": [
                    {"code": "510880", "name": "红利ETF", "quantity": 6200},
                    {"code": "512890", "name": "红利低波", "quantity": 21500},
                    {"code": "300054", "name": "鼎龙股份", "quantity": 0},
                ],
            }
        ],
        "bridge": {
            "online": online,
            "state": "idle",
            "pending_ids": [],
            "active_request_id": None,
            "latest_completed": latest_completed or {},
        },
    }


def test_non_trading_day_skips_silently(tmp_path):
    calls = []
    rc = job.run_postopen_brief(
        now=datetime(2026, 11, 22, 9, 40, tzinfo=BJ_TZ),
        trading_day_fn=lambda _d: (False, "非交易日"),
        http_fn=lambda path, **kw: calls.append(path) or {},
        brief_dir=tmp_path,
    )
    assert rc == 0
    assert calls == []


def test_already_pushed_today_is_idempotent(tmp_path):
    (tmp_path / "postopen_pushed_20261123.json").write_text('{"date": "2026-11-23"}', encoding="utf-8")
    calls = []
    rc = job.run_postopen_brief(
        now=datetime(2026, 11, 23, 9, 40, tzinfo=BJ_TZ),
        trading_day_fn=lambda _d: (True, "交易日"),
        http_fn=lambda path, **kw: calls.append(path) or {},
        brief_dir=tmp_path,
    )
    assert rc == 0
    assert calls == []


def test_fresh_snapshot_skips_sync_and_pushes_wechat(tmp_path):
    calls = []

    def fake_http(path, method="GET", body=None, timeout=60):
        calls.append((method, path, body))
        if path == "/api/accounts/overview":
            return _sample_overview(generated_at="2026-11-23T09:38:00+08:00")
        if path == "/api/g3/desk/sync-fills":
            return {"broker_fills_found": 5, "imported": 2, "idempotent_skipped": 3}
        if path == "/api/g3/watchlist/sync-ths":
            return {"summary": "新增 2 / 更新 1"}
        if path == "/api/g3/premarket-brief":
            return {"date": "2026-11-23", "overnight_notes": ["外盘平稳"], "agent_thesis": "早盘原研判"}
        if path == "/api/g3/premarket-brief/generate":
            assert body["overnight_notes"] == ["外盘平稳"]
            assert "红利ETF(510880) 6200股" in body["agent_thesis"]
            assert "鼎龙股份" not in body["agent_thesis"]
            return {"date": "2026-11-23", "market_stance": {"actual_pct": 99.4}, "ar3_summary": {"sell_count": 1, "buy_count": 2}}
        if path == "/api/g3/premarket-brief/push-wechat":
            return {"ok": True, "channel": "ilink_bridge", "detail": "已送达微信"}
        raise AssertionError(path)

    rc = job.run_postopen_brief(
        now=datetime(2026, 11, 23, 9, 40, tzinfo=BJ_TZ),
        trading_day_fn=lambda _d: (True, "交易日"),
        http_fn=fake_http,
        brief_dir=tmp_path,
    )
    assert rc == 0
    paths = [p for _, p, _ in calls]
    assert "/api/accounts/sync" not in paths
    assert "/api/g3/premarket-brief/generate" in paths
    assert "/api/g3/premarket-brief/push-wechat" in paths
    marker = tmp_path / "postopen_pushed_20261123.json"
    assert marker.exists()
    assert json.loads(marker.read_text(encoding="utf-8"))["date"] == "2026-11-23"


def test_stale_snapshot_triggers_sync_waits_and_pushes(tmp_path):
    overview_calls = {"n": 0}
    paths = []

    def fake_http(path, method="GET", body=None, timeout=60):
        paths.append(path)
        if path == "/api/accounts/overview":
            overview_calls["n"] += 1
            if overview_calls["n"] == 1:
                return _sample_overview(generated_at="2026-11-20T17:18:42+08:00", online=True)
            return _sample_overview(
                generated_at="2026-11-23T09:41:10+08:00",
                online=True,
                latest_completed={"request_id": "req_0940", "rc": 0, "status": "ok"},
            )
        if path == "/api/accounts/sync":
            assert body == {"with_fills": True}
            return {"status": "queued", "request_id": "req_0940"}
        if path == "/api/g3/desk/sync-fills":
            return {"broker_fills_found": 3, "imported": 3, "idempotent_skipped": 0}
        if path == "/api/g3/watchlist/sync-ths":
            return {"summary": "ok"}
        if path == "/api/g3/premarket-brief":
            return {"date": "2026-11-23", "overnight_notes": [], "agent_thesis": ""}
        if path == "/api/g3/premarket-brief/generate":
            assert "已实时同步(09:41)" in body["agent_thesis"]
            return {"date": "2026-11-23", "market_stance": {"actual_pct": 99.4}, "ar3_summary": {"sell_count": 0, "buy_count": 0}}
        if path == "/api/g3/premarket-brief/push-wechat":
            return {"ok": True, "channel": "ilink_bridge", "detail": "已送达微信"}
        raise AssertionError(path)

    rc = job.run_postopen_brief(
        now=datetime(2026, 11, 23, 9, 40, tzinfo=BJ_TZ),
        trading_day_fn=lambda _d: (True, "交易日"),
        http_fn=fake_http,
        sleep_fn=lambda _s: None,
        brief_dir=tmp_path,
    )
    assert rc == 0
    assert "/api/accounts/sync" in paths
    assert "/api/g3/premarket-brief/push-wechat" in paths
