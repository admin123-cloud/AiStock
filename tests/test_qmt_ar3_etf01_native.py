# -*- coding: utf-8 -*-
"""国金标准 QMT 纯内置 AR3 #01 原生策略离线单元测试（含零 IO/零外连合规审计）。"""
from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import qmt_ar3_etf01_native as qmt_strat

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "qmt_ar3_etf01_native.py"


def test_zero_file_io_and_zero_network_compliance() -> None:
    """硬合规门禁：策略源码绝不允许任何文件 IO、外部网络库或 MiniQMT 依赖。"""
    src = SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)

    forbidden_imports = {
        "os", "pathlib", "io", "urllib", "urllib3", "requests", "http",
        "socket", "subprocess", "shutil", "xtquant", "sqlite3", "pymongo", "redis",
    }
    imported: set[str] = set()
    called_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)

    assert imported.isdisjoint(forbidden_imports), f"发现违规导入: {imported & forbidden_imports}"
    assert "open" not in called_names, "发现违规文件 IO 调用 open()"
    assert qmt_strat.DRY_RUN is True, "默认配置必须开启 DRY_RUN=True 仿真护栏"


def test_regime_and_etf01_targets_across_all_phases() -> None:
    # 1. 构造上升确认行情 -> 进攻期选 A 股宽基 Top2
    up_sh = [3000.0 + i * 8.0 for i in range(80)]
    reg_up = qmt_strat.classify_sh_regime(up_sh, breadth_pct=65.0)
    assert reg_up["is_offensive"] is True
    assert reg_up["regime"] == "offensive"

    close_map = {code: [1.0 + i * 0.001 for i in range(40)] for code in qmt_strat.ETF_CODES}
    # 让创业板 ETF 与科创50 ETF 动量最强
    close_map["159915.SZ"] = [1.0 + i * 0.015 for i in range(40)]
    close_map["588000.SH"] = [1.0 + i * 0.012 for i in range(40)]
    close_map["518880.SH"] = [1.0 + i * 0.020 for i in range(40)]  # 非A股黄金更强，但进攻期应优先配A股宽基Top2

    rows = qmt_strat.compute_etf_metrics(close_map)
    tgt_off, _ = qmt_strat.decide_ar3_etf01_targets(reg_up, rows, panic_active=False)
    assert set(tgt_off.keys()) == {"159915.SZ", "588000.SH"}
    assert abs(sum(tgt_off.values()) - 1.0) < 1e-6

    # 2. 构造下跌确认行情 + 低宽度 -> 极端冰点未触发时 100% 切非 A 股最强单核(黄金ETF)
    down_sh = [3500.0 - i * 8.0 for i in range(80)]
    reg_panic = qmt_strat.classify_sh_regime(down_sh, breadth_pct=12.0)
    assert reg_panic["is_offensive"] is False
    assert reg_panic["regime"] == "extreme_panic"

    tgt_def, _ = qmt_strat.decide_ar3_etf01_targets(reg_panic, rows, panic_active=False)
    assert tgt_def == {"518880.SH": 1.0}

    # 3. 极端冰点右侧阳线触发 -> 30% 创业板 + 30% 中证1000 + 40% 非A股避险
    tgt_pan, _ = qmt_strat.decide_ar3_etf01_targets(reg_panic, rows, panic_active=True)
    assert tgt_pan["159915.SZ"] == 0.30
    assert tgt_pan["512100.SH"] == 0.30
    assert abs(sum(tgt_pan.values()) - 1.0) < 1e-6

    # 4. 全部 ETF 跌破 MA20 -> 自动切 100% 十年国债 511260.SH
    bear_map = {code: [2.0 - i * 0.02 for i in range(40)] for code in qmt_strat.ETF_CODES}
    bear_rows = qmt_strat.compute_etf_metrics(bear_map)
    tgt_bond, _ = qmt_strat.decide_ar3_etf01_targets(reg_panic, bear_rows, panic_active=False)
    assert tgt_bond == {"511260.SH": 1.0}


def test_rebalance_orders_sell_first_and_only_manages_etf_whitelist() -> None:
    holdings = {
        "002015.SZ": {"volume": 5000, "can_use": 5000, "open_price": 12.0, "market_value": 60000.0},  # 非ETF个股，绝不误卖
        "510300.SH": {"volume": 10000, "can_use": 10000, "open_price": 4.0, "market_value": 40000.0}, # 非目标ETF，需清仓
    }
    price_map = {"002015.SZ": 12.0, "510300.SH": 4.0, "512890.SH": 1.25}
    sells, buys = qmt_strat.build_rebalance_orders(
        total_assets=100000.0,
        available_cash=20000.0,
        holdings=holdings,
        target_weights={"512890.SH": 1.0},
        price_map=price_map,
        only_manage_etf=True,
    )
    assert [o["code"] for o in sells] == ["510300.SH"]
    assert sells[0]["volume"] == 10000
    assert [o["code"] for o in buys] == ["512890.SH"]
    assert buys[0]["volume"] % 100 == 0
    assert buys[0]["est_yuan"] <= 20000.0


def test_dry_run_never_calls_passorder_and_handles_full_cycle(monkeypatch) -> None:
    class _FakeCtx:
        def is_last_bar(self):
            return True

        def get_stock_list_in_sector(self, name):
            return []

        def get_market_data_ex(self, fields, stock_code, **kwargs):
            out = {}
            for code in stock_code:
                if code == "000001.SH":
                    vals = [3400.0 - i * 5.0 for i in range(85)]
                elif code == "512890.SH":
                    vals = [1.10 + i * 0.005 for i in range(85)]
                else:
                    vals = [1.50 - i * 0.004 for i in range(85)]
                out[code] = pd.DataFrame({"close": vals})
            return out

        def get_full_tick(self, stock_code):
            return {}

    passorder_calls = []
    monkeypatch.setattr(qmt_strat, "DRY_RUN", True)
    monkeypatch.setattr(qmt_strat, "get_trade_detail_data", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(qmt_strat, "passorder", lambda *a, **k: passorder_calls.append(a), raising=False)

    res = qmt_strat._run_ar3_cycle(_FakeCtx(), trigger_source="unit_test", allow_trade=True)
    assert res["targets"] == {"512890.SH": 1.0}
    assert len(res["buys"]) == 1
    assert res["buys"][0]["code"] == "512890.SH"
    assert len(passorder_calls) == 0, "DRY_RUN=True 时绝不允许调用 passorder"
