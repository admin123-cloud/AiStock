# -*- coding: utf-8 -*-
"""G3 Phase 3C 进阶单元测试：半自动预填队列 + 宿主机预填安全门禁 + 同花顺成交回执自动入账。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from host_bridges.g3_prefill_bridge import process_one_prefill_request
from services.g3 import fill_autosync
from services.trading import t_daily_plan

BJ_TZ = timezone(timedelta(hours=8))


def _write_drafts(root: Path, trade_date: str, drafts: list[dict[str, Any]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"{trade_date.replace('-', '')}.jsonl"
    p.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in drafts) + "\n", encoding="utf-8")


def test_dispatch_prefill_order_enforces_prefill_only_and_idempotency(tmp_path: Path) -> None:
    tdate = "2026-11-23"
    _write_drafts(
        tmp_path,
        tdate,
        [
            {
                "draft_id": "PF-OK-01",
                "approved": True,
                "summary": "L2 预检通过",
                "order": {
                    "account_id": "acct1",
                    "code": "002015",
                    "name": "协鑫能科",
                    "side": "SELL",
                    "order_type": "t_trade",
                    "price": 12.66,
                    "shares": 300,
                },
            },
            {
                "draft_id": "PF-BAD-02",
                "approved": False,
                "summary": "超过单笔上限",
                "order": {
                    "account_id": "acct1",
                    "code": "002015",
                    "name": "协鑫能科",
                    "side": "BUY",
                    "order_type": "t_trade",
                    "price": 12.00,
                    "shares": 90000,
                },
            },
        ],
    )

    # 1. 未通过 L2 预检的草案必须拒绝派发
    with pytest.raises(ValueError, match="未通过 L2 硬风控预检"):
        fill_autosync.dispatch_prefill_order(draft_id="PF-BAD-02", trade_date=tdate, base_dir=tmp_path)

    # 2. 通过预检的草案正常入队，且强制 prefill_only + allow_submit=False
    res1 = fill_autosync.dispatch_prefill_order(draft_id="PF-OK-01", trade_date=tdate, base_dir=tmp_path)
    assert res1["status"] == "queued"
    assert res1["mode"] == "prefill_only"
    assert res1["allow_submit"] is False

    req_file = tmp_path / "queue" / "req_PF-OK-01.json"
    assert req_file.exists()
    queued_payload = json.loads(req_file.read_text(encoding="utf-8"))
    assert queued_payload["mode"] == "prefill_only"
    assert queued_payload["allow_submit"] is False
    assert queued_payload["code"] == "002015"
    assert queued_payload["qty"] == 300

    # 3. 幂等保护：再次派发同一 draft_id 返回 already_queued
    res2 = fill_autosync.dispatch_prefill_order(draft_id="PF-OK-01", trade_date=tdate, base_dir=tmp_path)
    assert res2["status"] == "already_queued"


def test_prefill_bridge_rejects_submit_and_executes_prefill_only(tmp_path: Path) -> None:
    now_dt = datetime(2026, 11, 23, 10, 15, 0, tzinfo=BJ_TZ)
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)

    # 1. 企图开启 allow_submit=True 的请求必须被硬拦截
    (queue_dir / "req_PF-EVIL.json").write_text(
        json.dumps(
            {
                "draft_id": "PF-EVIL",
                "trade_date": "2026-11-23",
                "mode": "prefill_only",
                "allow_submit": True,
                "account": "acct1",
                "code": "002015",
                "side": "BUY",
                "price": 12.5,
                "qty": 200,
            }
        ),
        encoding="utf-8",
    )
    called: list[dict[str, Any]] = []

    def fake_filler(**kwargs: Any) -> dict[str, Any]:
        called.append(kwargs)
        return {"ok": True, "status": "prefilled_awaiting_human_confirm", "readback": kwargs}

    out_evil = process_one_prefill_request(tmp_path, filler_fn=fake_filler, now=now_dt)
    assert out_evil is not None
    assert out_evil["ok"] is False
    assert out_evil["status"] == "rejected_submit_forbidden"
    assert out_evil["submitted"] is False
    assert len(called) == 0

    # 2. 合法 prefill_only 请求通过门禁并调用 filler_fn（仅填单，submitted=False）
    (queue_dir / "req_PF-GOOD.json").write_text(
        json.dumps(
            {
                "draft_id": "PF-GOOD",
                "trade_date": "2026-11-23",
                "mode": "prefill_only",
                "allow_submit": False,
                "account": "acct1",
                "code": "002015",
                "side": "SELL",
                "price": 12.66,
                "qty": 300,
            }
        ),
        encoding="utf-8",
    )
    out_good = process_one_prefill_request(tmp_path, filler_fn=fake_filler, now=now_dt)
    assert out_good is not None
    assert out_good["ok"] is True
    assert out_good["submitted"] is False
    assert len(called) == 1
    assert called[0]["code"] == "002015"
    assert called[0]["qty"] == 300
    assert (tmp_path / "done" / "PF-GOOD.json").exists()

    # 3. 同一 draft_id 再次投递应被门禁幂等拒绝（duplicate_draft_id）
    (queue_dir / "req_PF-GOOD.json").write_text(
        json.dumps(
            {
                "draft_id": "PF-GOOD",
                "trade_date": "2026-11-23",
                "mode": "prefill_only",
                "allow_submit": False,
                "account": "acct1",
                "code": "002015",
                "side": "SELL",
                "price": 12.66,
                "qty": 300,
            }
        ),
        encoding="utf-8",
    )
    out_dup = process_one_prefill_request(tmp_path, filler_fn=fake_filler, now=now_dt)
    assert out_dup is not None
    assert out_dup["ok"] is False
    assert out_dup["status"] == "rejected_by_prefill_gate"
    assert len(called) == 1


def test_sync_broker_fills_inherits_order_type_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tdate = "2026-11-23"
    plan_dir = tmp_path / "t_daily_plan"
    prefill_dir = tmp_path / "prefill"
    ths_dir = tmp_path / "ths"
    ths_dir.mkdir(parents=True, exist_ok=True)

    # 隔离 t_daily_plan 的落盘目录
    plan_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        t_daily_plan,
        "_exec_log_path",
        lambda d: plan_dir / f"executions_{d.replace('-', '')}.jsonl",
    )

    # 准备一条 R51 水位调仓预填单
    _write_drafts(
        prefill_dir,
        tdate,
        [
            {
                "draft_id": "PF-REB-99",
                "approved": True,
                "order": {
                    "account_id": "acct1",
                    "code": "002015",
                    "name": "协鑫能科",
                    "side": "SELL",
                    "order_type": "base_rebalance",
                    "price": 12.80,
                    "shares": 500,
                },
            }
        ],
    )

    # 准备 mocked 同花顺双账号成交回执（1 条匹配 R51 调仓，1 条普通做T）
    (ths_dir / "latest_multi.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-11-23T14:30:00+08:00",
                "accounts": {
                    "acct1": {
                        "read_account": {
                            "generated_at": "2026-11-23T14:30:00+08:00",
                            "today_fills": {
                                "rows": [
                                    {
                                        "deal_id": "CJ001",
                                        "date": "2026-11-23",
                                        "code": "002015",
                                        "name": "协鑫能科",
                                        "side": "卖出",
                                        "qty": 500,
                                        "price": 12.82,
                                    },
                                    {
                                        "deal_id": "CJ002",
                                        "date": "2026-11-23",
                                        "code": "600588",
                                        "name": "用友网络",
                                        "side": "买入",
                                        "qty": 200,
                                        "price": 15.10,
                                    },
                                ]
                            },
                        }
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    res1 = fill_autosync.sync_broker_fills_to_desk_executions(
        trade_date=tdate, ths_dir=ths_dir, prefill_dir=prefill_dir
    )
    assert res1["broker_fills_found"] == 2
    assert res1["imported"] == 2
    assert res1["idempotent_skipped"] == 0

    execs = t_daily_plan.load_intraday_executions(tdate)
    assert len(execs) == 2
    by_code = {e["code"]: e for e in execs}
    assert by_code["002015"]["order_type"] == "base_rebalance"
    assert "PF-REB-99" in str(by_code["002015"]["note"])
    assert by_code["600588"]["order_type"] == "t_trade"

    # 第二次同步必须 100% 幂等跳过
    res2 = fill_autosync.sync_broker_fills_to_desk_executions(
        trade_date=tdate, ths_dir=ths_dir, prefill_dir=prefill_dir
    )
    assert res2["broker_fills_found"] == 2
    assert res2["imported"] == 0
    assert res2["idempotent_skipped"] == 2
    assert len(t_daily_plan.load_intraday_executions(tdate)) == 2
