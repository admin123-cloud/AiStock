# -*- coding: utf-8 -*-
"""R47 fills 导出器多账号单测：分账号文件 / legacy 兼容 / 面板缺席跳过 / 逐账号隔离。"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.ths_fills_export as tfe  # noqa: E402


TODAY = date(2026, 11, 11)
NOW = datetime(2026, 11, 11, 15, 5, 0)
STAMP = "20261111"


def _capture_ok(today_rows=1):
    line = "撤单[F3]\t10:00:00\t002015\t协鑫能科\t证券买入\t100\t13.50\t1350.00\t1350\t12345"
    return {
        "today_lines": [
            "成交时间\t证券代码\t证券名称\t操作\t成交数量\t成交均价\t成交金额\t合同编号",
            line,
        ][:1 + today_rows],
        "history_rows": [],
        "history_header_seen": True,
        "window_state": {"minimized_before": False},
    }


def _capture_zero():
    return {
        "today_lines": ["成交时间\t证券代码\t证券名称\t操作\t成交数量\t成交均价\t成交金额\t合同编号"],
        "history_rows": [],
        "history_header_seen": True,
        "window_state": {},
    }


# ── 账号循环与文件命名 ────────────────────────────────────────

def test_multi_export_writes_per_account_and_legacy(tmp_path):
    plan = {
        "acct1": {"panel": None, "capture": lambda ev: _capture_ok()},
        "acct2": {"panel": {"panel_id": "acct2", "pid": 555}, "capture": lambda ev: _capture_zero()},
    }
    result = tfe.run_export_multi(for_date=TODAY, now=NOW, out_root=tmp_path, plan=plan)
    by = result["accounts"]
    assert by["acct1"]["status"] == "ok"
    assert by["acct2"]["status"] == "zero_trades"
    # 分账号文件 + acct1 legacy 镜像
    f1 = tmp_path / "fills" / f"{STAMP}_acct1.json"
    f2 = tmp_path / "fills" / f"{STAMP}_acct2.json"
    legacy = tmp_path / "fills" / f"{STAMP}.json"
    assert f1.is_file() and f2.is_file() and legacy.is_file()
    p1 = json.loads(f1.read_text(encoding="utf-8"))
    p2 = json.loads(f2.read_text(encoding="utf-8"))
    pl = json.loads(legacy.read_text(encoding="utf-8"))
    assert p1["account"] == "acct1" and p2["account"] == "acct2"
    assert pl == p1  # legacy = acct1 镜像
    assert all(r["account"] == "acct2" for r in p2["fills"]) or p2["fills"] == []
    # 行级 account 字段
    assert all(r.get("account") == "acct1" for r in p1["fills"])


def test_multi_export_acct2_absent_skips(tmp_path):
    # acct2 无面板（当前实况）→ not_configured 跳过，无 unavailable 噪音
    plan = {"acct1": {"panel": None, "capture": lambda ev: _capture_ok()}}
    result = tfe.run_export_multi(for_date=TODAY, now=NOW, out_root=tmp_path, plan=plan,
                                  known_accounts=["acct1", "acct2"])
    assert result["accounts"]["acct1"]["status"] == "ok"
    assert result["accounts"]["acct2"]["status"] == "panel_absent"
    assert not (tmp_path / "fills" / f"{STAMP}_acct2.unavailable.json").exists()
    assert result["rc"] == 0


def test_multi_export_acct2_unavailable_isolated(tmp_path):
    # acct2 面板在场但采集失败 → 仅 acct2 unavailable 标记，acct1 正常
    def boom(ev):
        raise RuntimeError("NO_PANEL_WINDOW")

    plan = {
        "acct1": {"panel": None, "capture": lambda ev: _capture_ok()},
        "acct2": {"panel": {"panel_id": "acct2", "pid": 555}, "capture": boom},
    }
    result = tfe.run_export_multi(for_date=TODAY, now=NOW, out_root=tmp_path, plan=plan)
    assert result["accounts"]["acct1"]["status"] == "ok"
    assert result["accounts"]["acct2"]["status"] == "unavailable"
    assert (tmp_path / "fills" / f"{STAMP}_acct2.unavailable.json").is_file()
    assert result["rc"] == 2  # 任一在场账号 unavailable → rc=2（PM watcher 语义保留）


def test_multi_export_acct1_failure_keeps_rc2(tmp_path):
    def boom(ev):
        raise RuntimeError("THS_UNREACHABLE")

    plan = {"acct1": {"panel": None, "capture": boom}}
    result = tfe.run_export_multi(for_date=TODAY, now=NOW, out_root=tmp_path, plan=plan)
    assert result["accounts"]["acct1"]["status"] == "unavailable"
    assert (tmp_path / "fills" / f"{STAMP}.unavailable.json").is_file()  # legacy 标记也在
    assert result["rc"] == 2


def test_multi_export_idempotent_skip_per_account(tmp_path):
    plan = {"acct1": {"panel": None, "capture": lambda ev: _capture_ok()}}
    first = tfe.run_export_multi(for_date=TODAY, now=NOW, out_root=tmp_path, plan=plan)
    assert first["accounts"]["acct1"]["status"] == "ok"
    again = tfe.run_export_multi(for_date=TODAY, now=NOW, out_root=tmp_path, plan=plan)
    assert again["accounts"]["acct1"]["status"] == "skip"


# ── 采集计划解析（面板 → 账号绑定）─────────────────────────────

def test_build_capture_plan_default_single(tmp_path, monkeypatch):
    from services import ths_account_bridge as bridge

    reg = bridge.PanelRegistry(tmp_path / "ths_panels.json")
    live = [
        {"pid": 37292, "process": "xiadan.exe", "title": "网上股票交易系统5.0",
         "hwnd": "0x1", "rect": [388, 210, 1381, 964]},
    ]
    plan = tfe.build_capture_plan(live_panels=live, registry=reg)
    assert plan["acct1"]["panel"]["pid"] == 37292
    assert "acct2" not in plan


def test_build_capture_plan_dual(tmp_path):
    from services import ths_account_bridge as bridge

    reg = bridge.PanelRegistry(tmp_path / "ths_panels.json")
    reg.bind(rule={"process": "xxbroker.exe", "title_contains": "XX证券"},
             account="acct2", broker_hint="XX")
    live = [
        {"pid": 37292, "process": "xiadan.exe", "title": "网上股票交易系统5.0",
         "hwnd": "0x1", "rect": [388, 210, 1381, 964]},
        {"pid": 555, "process": "xxbroker.exe", "title": "XX证券网上交易系统",
         "hwnd": "0x2", "rect": [0, 0, 900, 700]},
    ]
    plan = tfe.build_capture_plan(live_panels=live, registry=reg)
    assert plan["acct1"]["panel"]["pid"] == 37292
    assert plan["acct2"]["panel"]["pid"] == 555
