from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default, _pct  # noqa: E402
from scripts.gen2_backtest_position_staging import run_one  # noqa: E402
from scripts.gen2_backtest_risk_cool_dynamic_circuit import _run_dynamic  # noqa: E402


SOURCE = ROOT / "reports" / "gen2_v2_complete_strategy" / "sources" / "g2_v2_complete.parquet"
OUT = ROOT / "reports" / "gen2_v2_attack_sizing_probe"
START_DATE = "2024-07-09"
END_DATE = "2026-05-28"
POLICY = "stop_cd3_skip"


VARIANTS = [
    {
        "name": "base_official_50x2_day1",
        "engine": "official",
        "note": "当前正式口径：最多2只，每日最多1只，每只目标约50%。",
    },
    {
        "name": "single_60x2_day1",
        "engine": "position",
        "position_model": "single",
        "tranche_weight": 0.60,
        "max_position_weight": 0.60,
        "max_codes": 2,
        "max_new_codes_per_day": 1,
        "note": "A类信号单票上限提高到60%，最多2只。",
    },
    {
        "name": "single_70x2_day1",
        "engine": "position",
        "position_model": "single",
        "tranche_weight": 0.70,
        "max_position_weight": 0.70,
        "max_codes": 2,
        "max_new_codes_per_day": 1,
        "note": "A类信号单票上限提高到70%，最多2只。",
    },
    {
        "name": "single_50x3_day1",
        "engine": "position",
        "position_model": "single",
        "tranche_weight": 0.50,
        "max_position_weight": 0.50,
        "max_codes": 3,
        "max_new_codes_per_day": 1,
        "note": "不提高单票仓位，只允许最多3只。",
    },
    {
        "name": "single_50x3_day2",
        "engine": "position",
        "position_model": "single",
        "tranche_weight": 0.50,
        "max_position_weight": 0.50,
        "max_codes": 3,
        "max_new_codes_per_day": 2,
        "note": "最多3只，强信号簇状出现时同日最多2只。",
    },
    {
        "name": "staged_25_add25_cap50",
        "engine": "position",
        "position_model": "staged",
        "tranche_weight": 0.25,
        "max_position_weight": 0.50,
        "max_codes": 2,
        "max_new_codes_per_day": 1,
        "note": "先25%，次日满足强/弱确认再加25%，单票上限50%。",
    },
    {
        "name": "staged_35_add35_cap70",
        "engine": "position",
        "position_model": "staged",
        "tranche_weight": 0.35,
        "max_position_weight": 0.70,
        "max_codes": 2,
        "max_new_codes_per_day": 1,
        "note": "先35%，次日满足确认再加35%，单票上限70%。",
    },
    {
        "name": "staged_40_add40_cap80",
        "engine": "position",
        "position_model": "staged",
        "tranche_weight": 0.40,
        "max_position_weight": 0.80,
        "max_codes": 2,
        "max_new_codes_per_day": 1,
        "note": "先40%，次日满足确认再加40%，单票上限80%。",
    },
]


def _lot_summary(run_dir: Path) -> dict[str, Any]:
    trades_path = run_dir / "trades.csv"
    curve_path = run_dir / "equity_curve.csv"
    out: dict[str, Any] = {}
    if trades_path.exists():
        trades = pd.read_csv(trades_path)
        if not trades.empty:
            lot = trades.groupby(["buy_date", "code", "name"], dropna=False).agg(pnl=("pnl", "sum"), ret=("return", "sum"))
            wins = pd.to_numeric(trades.loc[pd.to_numeric(trades["return"], errors="coerce") > 0, "return"], errors="coerce")
            losses = pd.to_numeric(trades.loc[pd.to_numeric(trades["return"], errors="coerce") <= 0, "return"], errors="coerce")
            out.update(
                {
                    "lot_count": int(len(lot)),
                    "lot_win_rate": float((lot["pnl"] > 0).mean()) if len(lot) else None,
                    "lot_avg_pnl": float(lot["pnl"].mean()) if len(lot) else None,
                    "avg_win_trade": float(wins.mean()) if len(wins) else None,
                    "avg_loss_trade": float(losses.mean()) if len(losses) else None,
                    "payoff_trade": float(wins.mean() / abs(losses.mean()))
                    if len(wins) and len(losses) and float(losses.mean()) != 0
                    else None,
                }
            )
    if curve_path.exists():
        curve = pd.read_csv(curve_path)
        if not curve.empty:
            equity = pd.to_numeric(curve.get("equity"), errors="coerce")
            market = pd.to_numeric(curve.get("market_value"), errors="coerce")
            exposure = (market / equity).replace([np.inf, -np.inf], np.nan).fillna(0.0)
            out.update(
                {
                    "avg_exposure": float(exposure.mean()),
                    "invested_days_ratio": float((exposure > 0).mean()),
                    "max_exposure": float(exposure.max()),
                }
            )
    return out


def _run_variant(spec: dict[str, Any]) -> dict[str, Any]:
    name = str(spec["name"])
    run_dir = OUT / "runs" / name
    if spec["engine"] == "official":
        summary = _run_dynamic(SOURCE, run_dir, POLICY, START_DATE, END_DATE, sort_mode="g2_v2")
    else:
        summary = run_one(
            name=name,
            source=SOURCE,
            policy=POLICY,
            output_dir=run_dir,
            start_date=START_DATE,
            end_date=END_DATE,
            tranche_weight=float(spec["tranche_weight"]),
            max_position_weight=float(spec["max_position_weight"]),
            initial_cash=150000.0,
            max_codes=int(spec["max_codes"]),
            max_new_codes_per_day=int(spec["max_new_codes_per_day"]),
            hold_days=10,
            max_up_ret=0.06,
            position_model=str(spec["position_model"]),
        )
    return {
        **summary,
        **_lot_summary(run_dir),
        "variant": name,
        "note": spec.get("note", ""),
        "engine": spec["engine"],
        "run_dir": str(run_dir),
    }


def _write_report(rows: list[dict[str, Any]]) -> None:
    sorted_rows = sorted(rows, key=lambda r: (float(r.get("total_return") or 0.0), float(r.get("max_drawdown") or -9.0)), reverse=True)
    lines = [
        "# G2 攻击仓位探索",
        "",
        "本报告只探索仓位/持仓槽位，不放宽正式 G2 买点。信号源固定为 `g2_v2_complete`，卖出、止损、止盈和冷却规则沿用正式口径。",
        "",
        "| 方案 | 交易 | 批次 | 加仓 | 平均仓位 | 持仓天数占比 | 总收益 | 超额 | 最大回撤 | 胜率 | 批次胜率 | 盈亏比 | 说明 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in sorted_rows:
        payoff_text = "" if row.get("payoff_trade") is None else f"{float(row['payoff_trade']):.2f}"
        lines.append(
            f"| {row['variant']} | {row.get('trade_count', '')} | {row.get('lot_count', '')} | {row.get('addon_buy_count', 0)} | "
            f"{_pct(row.get('avg_exposure'))} | {_pct(row.get('invested_days_ratio'))} | {_pct(row.get('total_return'))} | "
            f"{_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | "
            f"{_pct(row.get('lot_win_rate'))} | {payoff_text} | "
            f"{row.get('note', '')} |"
        )
    best = sorted_rows[0] if sorted_rows else {}
    lines.extend(
        [
            "",
            "## 初步结论",
            "",
            f"- 收益最高方案：`{best.get('variant', '')}`，总收益 {_pct(best.get('total_return'))}，最大回撤 {_pct(best.get('max_drawdown'))}。",
            "- 该探索仍属于回测研究，不等于实盘直接放大仓位；下一步需要单独检查成交容量、连续亏损期和最近一年稳定性。",
        ]
    )
    (OUT / "attack_sizing_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)
    rows = [_run_variant(spec) for spec in VARIANTS]
    df = pd.DataFrame(rows)
    df = df.sort_values(["total_return", "max_drawdown"], ascending=[False, False])
    df.to_csv(OUT / "summary.csv", index=False, encoding="utf-8-sig")
    (OUT / "summary.json").write_text(
        json.dumps({"schema_version": 1, "source": str(SOURCE), "rows": df.where(pd.notna(df), None).to_dict("records")}, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    _write_report(rows)
    print(json.dumps({"output_dir": str(OUT), "rows": len(rows)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
