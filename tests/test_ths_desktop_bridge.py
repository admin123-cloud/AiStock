# -*- coding: utf-8 -*-
"""AiStock 宿主机常驻桌面桥单测（心跳刷新、指令消费、安全拒绝非只读指令、L2 闭环自动联动）。"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import ths_desktop_bridge as bridge  # noqa: E402
from services import broker_accounts as ba  # noqa: E402
from services.t_trade_signal import is_fill_backed_open_t, load_state  # noqa: E402

BJ_TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 19, 14, 30, 0, tzinfo=BJ_TZ)


class TestThsDesktopBridge:
    def test_bridge_processes_queued_request_and_syncs_l2_fill_closure(self, tmp_path):
        root = tmp_path / "ths"
        t_root = tmp_path / "t_tracker"
        bridge.write_heartbeat(root, state="idle", now=NOW)
        enq = ba.enqueue_sync_request(accounts=["acct1"], with_fills=True, root=root, now=NOW)
        assert enq["status"] == "queued"

        def fake_runner(**kw):
            return {
                "status": "ok",
                "rc": 0,
                "accounts": {
                    "acct1": {
                        "account": "acct1",
                        "read_account": {
                            "account": "acct1",
                            "fills": {
                                "rows": [
                                    {
                                        "code": "300054.SZ",
                                        "side": "BUY",
                                        "qty": 200,
                                        "px": 59.5,
                                        "fill_no": "F-BRIDGE-1",
                                        "date": "20261119",
                                        "time": "10:15:00",
                                    }
                                ]
                            },
                        },
                    }
                },
            }

        res = bridge.process_one_command(
            root,
            runner=fake_runner,
            t_tracker_root=t_root,
            wait_idle_after_bootstrap=0.0,
            now=NOW,
        )
        assert res is not None and res["rc"] == 0
        assert res["l2_closure_sync"]["acct1"]["applied_buys"] == 1
        assert is_fill_backed_open_t(load_state(t_root, date(2026, 11, 19)).open_t("300054.SZ"))
        # 指令已归档到 done/，队列清空
        assert list((root / "commands").glob("req_*.json")) == []
        assert len(list((root / "commands" / "done").glob("req_*.json"))) == 1

    def test_bridge_rejects_unsafe_order_command_fail_closed(self, tmp_path):
        root = tmp_path / "ths"
        cmd_dir = root / "commands"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        (cmd_dir / "req_bad.json").write_text(
            json.dumps({"request_id": "req_bad", "action": "place_order", "allow_order": True}),
            encoding="utf-8",
        )
        res = bridge.process_one_command(root, now=NOW)
        assert res is not None
        assert res["status"] == "rejected_unsafe_action"
        assert res["rc"] == 4
        assert res["credentials_touched"] is False
