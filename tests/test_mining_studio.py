# -*- coding: utf-8 -*-
"""AI 策略挖掘工坊核心单元测试：三段切分、涨跌停撮合、5道硬门禁与影子合约安全锁。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from services.mining.backtest_engine import RuleSpec, evaluate_rule, simulate_trade_returns, split_trade_dates
from services.mining.contract_exporter import export_candidate_contract
from services.mining.live_router import load_regime_router_config, scan_active_strategy_signals, update_regime_route
from services.mining.panic_rebound_monitor import evaluate_panic_rebound_signal
from services.mining.quality_gates import evaluate_mining_quality_gates


def _make_synthetic_panel(n_days: int = 120) -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = pd.date_range("2024-01-02", periods=n_days, freq="B").strftime("%Y-%m-%d").tolist()
    bench = pd.DataFrame(
        {
            "trade_date": dates,
            "idx_close": np.linspace(3000.0, 3300.0, n_days),
            "hs300_open": np.linspace(3500.0, 3700.0, n_days),
            "hs300_close": np.linspace(3500.0, 3700.0, n_days),
            "market_above_ma20_pct": np.full(n_days, 65.0),
            "market_regime": ["offensive"] * n_days,
        }
    )

    rows = []
    rng = np.random.default_rng(42)
    for d in dates:
        for i in range(6):
            code = f"60000{i}.SH"
            strong = i < 3
            entry_o = 10.0
            # 强势样本持续具备正期望，弱势样本为负期望
            drift = 0.012 if strong else -0.008
            r_step = float(rng.normal(drift, 0.01))
            row = {
                "trade_date": d,
                "code": code,
                "name": f"测试股{i}",
                "industry": "半导体" if strong else "传统",
                "market_regime": "offensive",
                "entry_tradable": True,
                "ld_thresh": 0.905,
                "ma_bull_align": 1.0 if strong else 0.0,
                "rs_20d_vs_hs300": 12.0 if strong else -5.0,
                "industry_ret_5d_rank": 0.85 if strong else 0.2,
                "next_open": entry_o,
                "next_high": entry_o * 1.01,
                "next_low": entry_o * 0.99,
                "next_close": entry_o * (1.0 + r_step),
            }
            px = row["next_close"]
            for k in range(2, 11):
                px_next = max(1.0, px * (1.0 + drift + float(rng.normal(0, 0.005))))
                row[f"fwd_open_{k}"] = px
                row[f"fwd_high_{k}"] = max(px, px_next) * 1.005
                row[f"fwd_low_{k}"] = min(px, px_next) * 0.995
                row[f"fwd_close_{k}"] = px_next
                px = px_next
            rows.append(row)
    return pd.DataFrame(rows), bench


def test_split_and_evaluate_rule_passes_all_gates_and_exports_shadow(tmp_path: Path) -> None:
    panel, bench = _make_synthetic_panel(n_days=150)
    sp = split_trade_dates(bench["trade_date"].tolist())
    assert sp["is"][1] < sp["val"][0]
    assert sp["val"][1] < sp["oos"][0]

    rule = RuleSpec(
        rule_id="test_strong_trend",
        name="合成强势主升规则",
        template_id="mainwave_offense",
        target_regimes=["offensive"],
        entry_conditions=[
            {"feature": "ma_bull_align", "op": "==", "value": 1.0},
            {"feature": "rs_20d_vs_hs300", "op": ">=", "value": 5.0},
        ],
        exit_policy={"hold_days": 5, "stop_loss_pct": -0.07, "take_profit_pct": 0.15},
        top_k_per_day=3,
    )
    ev = evaluate_rule(panel, bench, rule, splits=sp)
    gate = evaluate_mining_quality_gates(ev, min_oos_trades=30)

    assert ev["segments"]["oos"]["trades_count"] >= 30
    assert ev["segments"]["oos"]["avg_ret_pct"] > 0.5
    assert gate["all_passed"] is True
    assert gate["mining_score"] >= 70.0

    # 验证导出合约严格锁死零自动下单与 candidate_shadow 状态
    exp = export_candidate_contract({**ev, "quality_gates": gate}, run_id="run_test_001", base_dir=tmp_path)
    contract = exp["contract"]
    assert contract["status"] == "candidate_shadow"
    assert contract["guardrails"]["auto_order_allowed"] is False
    assert contract["guardrails"]["formal_buy_signal"] is False
    assert Path(exp["path"]).exists()


def test_limit_down_delays_exit() -> None:
    """验证卖出当日一字跌停时不能按当日价强行卖出，必须顺延到次日可成交日。"""
    row = {
        "next_open": 10.0,
        "next_close": 10.0,
        "ld_thresh": 0.905,
        # 第 2 天一字跌停（9.0），无法卖出
        "fwd_open_2": 9.0,
        "fwd_high_2": 9.0,
        "fwd_low_2": 9.0,
        "fwd_close_2": 9.0,
        # 第 3 天打开跌停，低开在 8.50 触发止损卖出
        "fwd_open_3": 8.5,
        "fwd_high_3": 8.7,
        "fwd_low_3": 8.4,
        "fwd_close_3": 8.6,
    }
    for k in range(4, 11):
        row[f"fwd_open_{k}"] = 8.6
        row[f"fwd_high_{k}"] = 8.8
        row[f"fwd_low_{k}"] = 8.5
        row[f"fwd_close_{k}"] = 8.7
    df = pd.DataFrame([row])
    rets, days = simulate_trade_returns(df, hold_days=2, stop_loss_pct=-0.05, take_profit_pct=0.15)
    assert int(days[0]) == 3
    # 买入 10.0 -> 第 3 天开盘 8.5 成交 = -15% 再扣 0.26% 摩擦
    assert abs(float(rets[0]) - (-15.26)) < 0.05


def test_regime_router_bind_and_live_scan(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    panel, bench = _make_synthetic_panel(n_days=90)
    sp = split_trade_dates(bench["trade_date"].tolist())
    rule = RuleSpec(
        rule_id="router_test_champ",
        name="路由测试冠军",
        template_id="mainwave_offense",
        target_regimes=["offensive"],
        entry_conditions=[{"feature": "ma_bull_align", "op": "==", "value": 1.0}],
        exit_policy={"hold_days": 5, "stop_loss_pct": -0.07, "take_profit_pct": 0.15},
    )
    ev = evaluate_rule(panel, bench, rule, splits=sp)
    gate = evaluate_mining_quality_gates(ev, min_oos_trades=10)
    c_dir = tmp_path / "contracts"
    exp = export_candidate_contract({**ev, "quality_gates": gate}, run_id="r1", base_dir=c_dir)
    sid = exp["strategy_id"]

    # 默认防守周期为 cash_defense
    cfg0 = load_regime_router_config(base_dir=c_dir)
    assert cfg0["routes"]["defensive"] == "cash_defense"

    # 绑定 offensive 到冠军合约
    cfg1 = update_regime_route("offensive", sid, base_dir=c_dir)
    assert cfg1["routes"]["offensive"] == sid

    # 准备最新缓存截面并执行实时选股扫描
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    panel["close"] = 10.0
    panel["ret_1d_pct"] = 2.0
    panel["ret_5d_pct"] = 5.0
    panel["industry_above_ma20_pct"] = 80.0
    pq.write_table(pa.Table.from_pandas(panel, preserve_index=False), cache_dir / "panel_test.parquet")

    sig = scan_active_strategy_signals(
        current_regime="offensive",
        regime_label="主升进攻",
        allow_new_mainwave=True,
        target_position_cap_pct=75.0,
        contracts_dir=c_dir,
        cache_dir=cache_dir,
    )
    assert sig["mode"] == "active_strategy"
    assert sig["active_strategy_id"] == sid
    assert sig["matched_total"] == 3
    assert len(sig["picks"]) == 3
    assert sig["picks"][0]["stop_loss_price"] == 9.3
    assert sig["picks"][0]["take_profit_price"] == 11.5


def test_panic_rebound_monitor_armed_and_triggered() -> None:
    gate_panic = {"sources": {"breadth": {"above_ma20_pct": 11.01}}}
    fake_quotes = lambda codes: {
        "159915": {"price": 2.100},
        "512100": {"price": 2.500},
    }

    # 1. 冰点但双指数仍在下跌 -> ARMED_WAITING_REVERSAL，严禁触发买入
    sig_wait = evaluate_panic_rebound_signal(
        market_gate=gate_panic,
        index_chg_fetcher=lambda: {
            "399006.SZ": {"date": "2026-11-20", "close": 2100.0, "change_pct": -1.8},
            "000852.SH": {"date": "2026-11-20", "close": 5800.0, "change_pct": -2.2},
        },
        etf_quote_fetcher=fake_quotes,
    )
    assert sig_wait["status"] == "ARMED_WAITING_REVERSAL"
    assert sig_wait["armed"] is True
    assert sig_wait["triggered"] is False
    assert sig_wait["metrics"]["pair_avg_change_pct"] == -2.0

    # 2. 冰点且创业板+中证1000 均涨 >= +0.50% -> TRIGGERED_BUY_ETF
    sig_go = evaluate_panic_rebound_signal(
        market_gate=gate_panic,
        index_chg_fetcher=lambda: {
            "399006.SZ": {"date": "2026-11-23", "close": 2130.0, "change_pct": 1.1},
            "000852.SH": {"date": "2026-11-23", "close": 5850.0, "change_pct": 0.9},
        },
        etf_quote_fetcher=fake_quotes,
    )
    assert sig_go["status"] == "TRIGGERED_BUY_ETF"
    assert sig_go["triggered"] is True
    assert len(sig_go["etf_plans"]) == 2
    assert sig_go["etf_plans"][0]["code"] == "159915"
    assert sig_go["etf_plans"][0]["stop_loss_price"] == round(2.100 * 0.94, 3)
    assert sig_go["etf_plans"][0]["take_profit_price"] == round(2.100 * 1.12, 3)


