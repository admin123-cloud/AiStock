from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_RUNS = {
    "base": Path("reports/gen2_candidate_outcome_supervision/backtests/risk_cool_v2"),
    "mom20_0.35": Path("reports/gen2_risk_cool_param_sweep/backtests/mom20_0.35"),
    "amount_1.5_6.0": Path("reports/gen2_risk_cool_param_sweep/backtests/amount_1.5_6.0"),
}
DEFAULT_OUTPUT_DIR = Path("reports/gen2_risk_cool_contribution")
INITIAL_EQUITY = 150000.0


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _load_run(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = pd.read_csv(path / "trades.csv")
    equity = pd.read_csv(path / "equity_curve.csv")
    trades["buy_date"] = pd.to_datetime(trades["buy_date"], errors="coerce")
    trades["sell_date"] = pd.to_datetime(trades["sell_date"], errors="coerce")
    trades["pnl"] = pd.to_numeric(trades["pnl"], errors="coerce")
    trades["return"] = pd.to_numeric(trades["return"], errors="coerce")
    equity["date"] = pd.to_datetime(equity["date"], errors="coerce")
    equity["strategy_equity"] = pd.to_numeric(equity["strategy_equity"], errors="coerce")
    equity["drawdown"] = pd.to_numeric(equity["drawdown"], errors="coerce")
    return trades, equity


def _entry_level(trades: pd.DataFrame) -> pd.DataFrame:
    keys = ["code", "name", "buy_datetime", "buy_date"]
    return (
        trades.groupby(keys, dropna=False)
        .agg(
            pnl=("pnl", "sum"),
            first_sell_date=("sell_date", "min"),
            last_sell_date=("sell_date", "max"),
            exit_count=("exit_reason", "count"),
            exit_reasons=("exit_reason", lambda x: "|".join(sorted(set(map(str, x))))),
            v4_rank=("v4_rank", "first"),
            v4_score=("v4_score", "first"),
        )
        .reset_index()
        .sort_values("pnl", ascending=False)
    )


def _month_table(equity: pd.DataFrame) -> pd.DataFrame:
    e = equity.dropna(subset=["date", "strategy_equity"]).copy()
    e["month"] = e["date"].dt.strftime("%Y-%m")
    rows = []
    for month, g in e.groupby("month", sort=True):
        start = float(g.iloc[0]["strategy_equity"])
        end = float(g.iloc[-1]["strategy_equity"])
        rows.append(
            {
                "month": month,
                "return": end / start - 1.0 if start else None,
                "min_drawdown": float(g["drawdown"].min()),
            }
        )
    return pd.DataFrame(rows)


def _segment_table(equity: pd.DataFrame) -> pd.DataFrame:
    windows = [
        ("2024H2-2025Q1", "2024-07-09", "2025-03-31"),
        ("2025Q2-Q4", "2025-04-01", "2025-12-31"),
        ("2026YTD", "2026-01-01", "2026-05-21"),
    ]
    rows = []
    for name, start, end in windows:
        g = equity[(equity["date"] >= pd.Timestamp(start)) & (equity["date"] <= pd.Timestamp(end))].copy()
        if g.empty:
            continue
        first = float(g.iloc[0]["strategy_equity"])
        last = float(g.iloc[-1]["strategy_equity"])
        rows.append({"segment": name, "return": last / first - 1.0 if first else None, "min_drawdown": float(g["drawdown"].min())})
    return pd.DataFrame(rows)


def _analyze_one(name: str, path: Path, output_dir: Path) -> dict[str, Any]:
    trades, equity = _load_run(path)
    entries = _entry_level(trades)
    total_pnl = float(trades["pnl"].sum())
    top = entries.head(10).copy()
    bottom = entries.tail(10).copy().sort_values("pnl")
    by_code = entries.groupby(["code", "name"], dropna=False).agg(pnl=("pnl", "sum"), entries=("pnl", "count")).reset_index()
    by_code = by_code.sort_values("pnl", ascending=False)
    monthly = _month_table(equity)
    segments = _segment_table(equity)

    run_dir = output_dir / name
    run_dir.mkdir(parents=True, exist_ok=True)
    entries.to_csv(run_dir / "entry_contribution.csv", index=False, encoding="utf-8-sig")
    by_code.to_csv(run_dir / "code_contribution.csv", index=False, encoding="utf-8-sig")
    monthly.to_csv(run_dir / "monthly_returns.csv", index=False, encoding="utf-8-sig")
    segments.to_csv(run_dir / "segment_returns.csv", index=False, encoding="utf-8-sig")

    return {
        "variant": name,
        "entries": int(len(entries)),
        "trade_rows": int(len(trades)),
        "total_pnl": total_pnl,
        "final_return": float(equity.iloc[-1]["strategy_equity"] - 1.0),
        "max_drawdown": float(equity["drawdown"].min()),
        "top1_pnl_share": float(top.head(1)["pnl"].sum() / total_pnl) if total_pnl else None,
        "top5_pnl_share": float(top.head(5)["pnl"].sum() / total_pnl) if total_pnl else None,
        "top10_pnl_share": float(top["pnl"].sum() / total_pnl) if total_pnl else None,
        "return_without_top1_pnl": float((equity.iloc[-1]["strategy_equity"] * INITIAL_EQUITY - top.head(1)["pnl"].sum()) / INITIAL_EQUITY - 1.0),
        "return_without_top5_pnl": float((equity.iloc[-1]["strategy_equity"] * INITIAL_EQUITY - top.head(5)["pnl"].sum()) / INITIAL_EQUITY - 1.0),
        "return_without_top10_pnl": float((equity.iloc[-1]["strategy_equity"] * INITIAL_EQUITY - top["pnl"].sum()) / INITIAL_EQUITY - 1.0),
        "worst_entry_pnl": float(bottom.head(1)["pnl"].sum()) if len(bottom) else None,
        "positive_entry_rate": float((entries["pnl"] > 0).mean()) if len(entries) else None,
        "top_entries": top[["code", "name", "buy_datetime", "pnl", "exit_reasons"]].to_dict("records"),
        "bottom_entries": bottom[["code", "name", "buy_datetime", "pnl", "exit_reasons"]].head(10).to_dict("records"),
        "segments": segments.to_dict("records"),
    }


def _write_report(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 Risk Cool Contribution Analysis",
        "",
        "| variant | entries | final | max_dd | top1 pnl share | top5 pnl share | top10 pnl share | final ex-top5 pnl | positive entries |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['variant']} | {row['entries']} | {_pct(row['final_return'])} | {_pct(row['max_drawdown'])} | "
            f"{_pct(row['top1_pnl_share'])} | {_pct(row['top5_pnl_share'])} | {_pct(row['top10_pnl_share'])} | "
            f"{_pct(row['return_without_top5_pnl'])} | {_pct(row['positive_entry_rate'])} |"
        )
    lines.append("")
    for row in rows:
        lines.append(f"## {row['variant']}")
        lines.append("")
        lines.append("| segment | return | min_dd |")
        lines.append("| --- | ---: | ---: |")
        for seg in row["segments"]:
            lines.append(f"| {seg['segment']} | {_pct(seg['return'])} | {_pct(seg['min_drawdown'])} |")
        lines.append("")
        lines.append("Top contributors:")
        for item in row["top_entries"][:5]:
            lines.append(f"- {item['buy_datetime']} {item['code']} {item['name']}: {item['pnl']:.2f}")
        lines.append("")
        lines.append("Worst contributors:")
        for item in row["bottom_entries"][:5]:
            lines.append(f"- {item['buy_datetime']} {item['code']} {item['name']}: {item['pnl']:.2f}")
        lines.append("")
    (output_dir / "contribution_report.md").write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [_analyze_one(name, path, output_dir) for name, path in DEFAULT_RUNS.items()]
    pd.DataFrame([{k: v for k, v in row.items() if k not in {"top_entries", "bottom_entries", "segments"}} for row in rows]).to_csv(
        output_dir / "summary.csv", index=False, encoding="utf-8-sig"
    )
    _write_report(output_dir, rows)
    payload = {"schema_version": 1, "rows": rows, "outputs": {"summary": "summary.csv", "report": "contribution_report.md"}}
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Contribution analysis for G2 risk cool variants.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
