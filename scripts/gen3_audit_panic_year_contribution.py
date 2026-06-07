from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def _load_curve(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.normalize()
    for col in ["equity", "drawdown", "ret_from_start", "opened", "skipped", "open_positions"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["date", "equity"]).copy()


def _load_trades(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    for col in ["entry_date", "policy_exit_date", "exit_date"]:
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], errors="coerce").dt.normalize()
    for col in ["policy_net_ret", "realized_pnl", "stake", "candidate_score"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["entry_date", "code", "policy_net_ret"]).copy()


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    local = equity / float(equity.iloc[0])
    return float((local / local.cummax() - 1.0).min())


def _year_rows(curve: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    years = sorted(set(curve["date"].dt.year.tolist()) | set(trades["entry_date"].dt.year.tolist()))
    for year in years:
        cy = curve[curve["date"].dt.year.eq(year)].copy()
        ty = trades[trades["entry_date"].dt.year.eq(year)].copy()
        if cy.empty and ty.empty:
            continue
        start_equity = float(cy["equity"].iloc[0]) if not cy.empty else float("nan")
        end_equity = float(cy["equity"].iloc[-1]) if not cy.empty else float("nan")
        rows.append(
            {
                "year": int(year),
                "curve_days": int(len(cy)),
                "trades": int(len(ty)),
                "unique_codes": int(ty["code"].nunique()) if not ty.empty else 0,
                "start_equity": start_equity,
                "end_equity": end_equity,
                "year_equity_ret": end_equity / start_equity - 1.0 if start_equity and not pd.isna(start_equity) else 0.0,
                "year_max_drawdown": _max_drawdown(cy["equity"]) if not cy.empty else 0.0,
                "mean_trade_ret": float(ty["policy_net_ret"].mean()) if not ty.empty else 0.0,
                "median_trade_ret": float(ty["policy_net_ret"].median()) if not ty.empty else 0.0,
                "win_rate": float((ty["policy_net_ret"] > 0).mean()) if not ty.empty else 0.0,
                "worst_trade": float(ty["policy_net_ret"].min()) if not ty.empty else 0.0,
                "best_trade": float(ty["policy_net_ret"].max()) if not ty.empty else 0.0,
                "realized_pnl": float(ty["realized_pnl"].sum()) if "realized_pnl" in ty.columns and not ty.empty else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _display(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    for col in [
        "year_equity_ret",
        "year_max_drawdown",
        "mean_trade_ret",
        "median_trade_ret",
        "win_rate",
        "worst_trade",
        "best_trade",
    ]:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    for col in ["start_equity", "end_equity", "realized_pnl"]:
        if col in out.columns:
            out[col] = out[col].map(lambda v: "" if pd.isna(v) else f"{float(v):.2f}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit yearly contribution for G3 panic final candidate.")
    parser.add_argument(
        "--curve",
        default="reports/gen3_panic_v2_research/final_candidate_v1/m30_close5_full_nextopen_cost30_mtm_equity_curve.csv",
    )
    parser.add_argument(
        "--trades",
        default="reports/gen3_panic_v2_research/final_candidate_v1/m30_close5_full_nextopen_cost30_closed_trades.csv",
    )
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/year_contribution_v1")
    args = parser.parse_args()

    curve_path = Path(args.curve)
    trades_path = Path(args.trades)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    curve = _load_curve(curve_path)
    trades = _load_trades(trades_path)
    raw = _year_rows(curve, trades)
    raw.to_csv(out_dir / "year_contribution_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "year_contribution_summary_display.csv", index=False, encoding="utf-8-sig")

    total_pnl = raw["realized_pnl"].sum() if "realized_pnl" in raw.columns else 0.0
    raw["pnl_share"] = raw["realized_pnl"] / total_pnl if total_pnl else 0.0
    concentration = float(raw["pnl_share"].abs().max()) if not raw.empty else 0.0
    top_year = int(raw.iloc[raw["realized_pnl"].abs().idxmax()]["year"]) if not raw.empty else None

    lines = [
        "# G3 Panic 最终候选 V1 年度收益贡献审计",
        "",
        "## 口径",
        "",
        f"- 资金曲线：`{curve_path}`",
        f"- 成交明细：`{trades_path}`",
        "- 使用 30bps 成本、5 槽位、20% 单槽、30m -5% 风控退出版本。",
        "- 本审计只看年度分布，不改策略参数。",
        "",
        "## 年度结果",
        "",
        display.to_markdown(index=False),
        "",
        "## 集中度",
        "",
        f"- 绝对收益贡献最大年份：`{top_year}`",
        f"- 最大年度 PnL 绝对占比：`{concentration * 100:.2f}%`",
        "",
        "## 判断",
        "",
        "1. 如果单一年份贡献过高，说明策略仍可能依赖特定市场冲击，需要更长影子盘确认。",
        "2. 如果多数年份都有交易且没有单一年份独占收益，说明弱势恐慌修复逻辑的跨周期性更好。",
        "3. 年度交易数低于 10 的年份不能单独下结论，只能作为稳定性提示。",
    ]
    (out_dir / "year_contribution_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "curve": str(curve_path),
        "trades": str(trades_path),
        "output_dir": str(out_dir),
        "years": int(len(raw)),
        "top_abs_pnl_year": top_year,
        "top_abs_pnl_share": concentration,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
