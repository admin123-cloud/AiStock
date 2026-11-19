from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default  # noqa: E402


DEFAULT_MATRIX_DIR = ROOT / "reports" / "gen2_alpha191_portfolio_overlay_matrix"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_alpha191_contribution_audit"
DEFAULT_VARIANTS = ["baseline_trigger_time", "volume5_rank", "volume5_keep80"]

SEGMENTS = [
    ("train_2024h2_2025q1", "2024-07-09", "2025-03-31"),
    ("validation_2025q2_q4", "2025-04-01", "2025-12-31"),
    ("blind_2026ytd", "2026-01-01", "2026-05-21"),
]


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _money(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):,.0f}"


def _load_trades(matrix_dir: Path, variant: str) -> pd.DataFrame:
    path = matrix_dir / "backtests" / variant / "trades.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    d = pd.read_csv(path)
    for col in ["buy_datetime", "sell_datetime", "buy_date", "sell_date"]:
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], errors="coerce")
    for col in ["pnl", "capital", "return", "v4_rank", "v4_score", "sell_ratio"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["variant"] = variant
    return d


def _lot_level(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()
    group_cols = ["variant", "code", "name", "buy_date", "buy_datetime"]
    rows = []
    for key, g in trades.groupby(group_cols, dropna=False, sort=False):
        variant, code, name, buy_date, buy_datetime = key
        capital = float(pd.to_numeric(g["capital"], errors="coerce").sum())
        pnl = float(pd.to_numeric(g["pnl"], errors="coerce").sum())
        rows.append(
            {
                "variant": variant,
                "code": code,
                "name": name,
                "buy_date": buy_date,
                "buy_datetime": buy_datetime,
                "sell_date_last": g["sell_date"].max(),
                "sell_datetime_last": g["sell_datetime"].max(),
                "exit_reasons": ",".join(sorted(set(g["exit_reason"].dropna().astype(str)))),
                "exit_rows": int(len(g)),
                "capital": capital,
                "pnl": pnl,
                "return": pnl / capital if capital > 0 else np.nan,
                "v4_rank": int(pd.to_numeric(g["v4_rank"], errors="coerce").dropna().iloc[0]) if g["v4_rank"].notna().any() else None,
                "v4_score": float(pd.to_numeric(g["v4_score"], errors="coerce").dropna().iloc[0]) if g["v4_score"].notna().any() else None,
            }
        )
    lots = pd.DataFrame(rows)
    lots["buy_month"] = pd.to_datetime(lots["buy_date"], errors="coerce").dt.strftime("%Y-%m")
    lots["segment"] = lots["buy_date"].map(_segment_name)
    return lots


def _segment_name(date: Any) -> str:
    ts = pd.Timestamp(date) if pd.notna(date) else pd.NaT
    if pd.isna(ts):
        return "unknown"
    for name, start, end in SEGMENTS:
        if pd.Timestamp(start) <= ts <= pd.Timestamp(end):
            return name
    return "other"


def _share(numer: float, denom: float) -> float | None:
    if denom == 0 or not np.isfinite(denom):
        return None
    return float(numer / denom)


def _concentration(lots: pd.DataFrame, variant: str) -> dict[str, Any]:
    d = lots[lots["variant"].eq(variant)].copy()
    if d.empty:
        return {"variant": variant, "lot_count": 0}
    total_pnl = float(d["pnl"].sum())
    gross_profit = float(d.loc[d["pnl"] > 0, "pnl"].sum())
    gross_loss = float(d.loc[d["pnl"] < 0, "pnl"].sum())
    winners = d[d["pnl"] > 0].sort_values("pnl", ascending=False)

    def top_sum(n: int) -> float:
        return float(winners.head(n)["pnl"].sum())

    by_code = d.groupby(["code", "name"], dropna=False)["pnl"].sum().sort_values(ascending=False)
    by_month = d.groupby("buy_month", dropna=False)["pnl"].sum().sort_values(ascending=False)
    by_segment = d.groupby("segment", dropna=False)["pnl"].sum()
    return {
        "variant": variant,
        "lot_count": int(len(d)),
        "trade_exit_rows": int(d["exit_rows"].sum()),
        "total_pnl": total_pnl,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "win_lot_rate": float((d["pnl"] > 0).mean()),
        "avg_lot_return": float(d["return"].mean()),
        "median_lot_return": float(d["return"].median()),
        "top1_profit": top_sum(1),
        "top3_profit": top_sum(3),
        "top5_profit": top_sum(5),
        "top10_profit": top_sum(10),
        "top1_profit_share_gross": _share(top_sum(1), gross_profit),
        "top3_profit_share_gross": _share(top_sum(3), gross_profit),
        "top5_profit_share_gross": _share(top_sum(5), gross_profit),
        "top10_profit_share_gross": _share(top_sum(10), gross_profit),
        "top1_profit_share_net": _share(top_sum(1), total_pnl),
        "top3_profit_share_net": _share(top_sum(3), total_pnl),
        "top5_profit_share_net": _share(top_sum(5), total_pnl),
        "top10_profit_share_net": _share(top_sum(10), total_pnl),
        "top_code_pnl": float(by_code.iloc[0]) if len(by_code) else None,
        "top_code_share_net": _share(float(by_code.iloc[0]), total_pnl) if len(by_code) else None,
        "top_month_pnl": float(by_month.iloc[0]) if len(by_month) else None,
        "top_month_share_net": _share(float(by_month.iloc[0]), total_pnl) if len(by_month) else None,
        "train_pnl": float(by_segment.get("train_2024h2_2025q1", 0.0)),
        "validation_pnl": float(by_segment.get("validation_2025q2_q4", 0.0)),
        "blind_pnl": float(by_segment.get("blind_2026ytd", 0.0)),
    }


def _group_contribution(lots: pd.DataFrame, group_cols: list[str], output_col: str) -> pd.DataFrame:
    rows = []
    for variant, vdf in lots.groupby("variant", sort=False):
        total = float(vdf["pnl"].sum())
        g = (
            vdf.groupby(group_cols, dropna=False)
            .agg(lot_count=("pnl", "size"), pnl=("pnl", "sum"), avg_return=("return_", "mean"))
            .reset_index()
            .sort_values("pnl", ascending=False)
        )
        g["variant"] = variant
        g["pnl_share_net"] = g["pnl"].map(lambda x: _share(float(x), total))
        rows.append(g)
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    return out[["variant", *group_cols, "lot_count", "pnl", "pnl_share_net", "avg_return"]].rename(columns={group_cols[-1]: output_col})


def _incremental_lots(lots: pd.DataFrame, baseline: str, variant: str) -> pd.DataFrame:
    base = lots[lots["variant"].eq(baseline)].copy()
    other = lots[lots["variant"].eq(variant)].copy()
    key_cols = ["code", "buy_datetime"]
    base_keys = set(tuple(x) for x in base[key_cols].astype(str).to_numpy())
    other["_lot_key"] = [tuple(x) for x in other[key_cols].astype(str).to_numpy()]
    inc = other[~other["_lot_key"].isin(base_keys)].drop(columns=["_lot_key"]).copy()
    inc["compare_to"] = baseline
    return inc


def _delta_vs_baseline(lots: pd.DataFrame, baseline: str, variants: list[str]) -> pd.DataFrame:
    key_cols = ["code", "buy_datetime"]
    base = lots[lots["variant"].eq(baseline)].copy()
    base_keys = set(tuple(x) for x in base[key_cols].astype(str).to_numpy())
    rows = []
    for variant in variants:
        if variant == baseline:
            continue
        other = lots[lots["variant"].eq(variant)].copy()
        other_keys = set(tuple(x) for x in other[key_cols].astype(str).to_numpy())
        added = other[[tuple(x) not in base_keys for x in other[key_cols].astype(str).to_numpy()]]
        removed = base[[tuple(x) not in other_keys for x in base[key_cols].astype(str).to_numpy()]]
        common_other = other[[tuple(x) in base_keys for x in other[key_cols].astype(str).to_numpy()]]
        common_base = base[[tuple(x) in other_keys for x in base[key_cols].astype(str).to_numpy()]]
        rows.append(
            {
                "variant": variant,
                "compare_to": baseline,
                "total_pnl": float(other["pnl"].sum()),
                "delta_vs_base": float(other["pnl"].sum() - base["pnl"].sum()),
                "lot_count": int(len(other)),
                "common_lot_count": int(len(common_other)),
                "added_lot_count": int(len(added)),
                "added_pnl": float(added["pnl"].sum()),
                "added_win_rate": float((added["pnl"] > 0).mean()) if len(added) else None,
                "removed_lot_count": int(len(removed)),
                "removed_pnl_in_base": float(removed["pnl"].sum()),
                "removed_win_rate_in_base": float((removed["pnl"] > 0).mean()) if len(removed) else None,
                "common_pnl_delta": float(common_other["pnl"].sum() - common_base["pnl"].sum()),
            }
        )
    return pd.DataFrame(rows)


def _write_report(output_dir: Path, summary: pd.DataFrame, top_lots: pd.DataFrame, code_contrib: pd.DataFrame, month_contrib: pd.DataFrame) -> None:
    lines = [
        "# G2 Alpha191 Contribution Audit",
        "",
        "Lot-level audit groups partial exits by `code + buy_datetime` before calculating contribution concentration.",
        "",
        "## Summary",
        "",
        "| variant | lots | net_pnl | win | avg_ret | top1/gross | top3/gross | top5/gross | top10/gross | top_code/net | top_month/net |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary.itertuples(index=False):
        lines.append(
            f"| {row.variant} | {row.lot_count} | {_money(row.total_pnl)} | {_pct(row.win_lot_rate)} | {_pct(row.avg_lot_return)} | "
            f"{_pct(row.top1_profit_share_gross)} | {_pct(row.top3_profit_share_gross)} | {_pct(row.top5_profit_share_gross)} | "
            f"{_pct(row.top10_profit_share_gross)} | {_pct(row.top_code_share_net)} | {_pct(row.top_month_share_net)} |"
        )

    lines.extend(["", "## Top Lots", "", "| variant | buy | code | name | pnl | return | exits |", "| --- | --- | --- | --- | ---: | ---: | --- |"])
    for row in top_lots.head(20).itertuples(index=False):
        lines.append(
            f"| {row.variant} | {pd.Timestamp(row.buy_datetime).strftime('%Y-%m-%d %H:%M')} | {row.code} | {row.name} | "
            f"{_money(row.pnl)} | {_pct(row.return_)} | {row.exit_reasons} |"
        )

    lines.extend(["", "## Top Codes", "", "| variant | code | name | lots | pnl | net share |", "| --- | --- | --- | ---: | ---: | ---: |"])
    for row in code_contrib.groupby("variant", sort=False).head(5).itertuples(index=False):
        lines.append(f"| {row.variant} | {row.code} | {row.name} | {row.lot_count} | {_money(row.pnl)} | {_pct(row.pnl_share_net)} |")

    lines.extend(["", "## Top Months", "", "| variant | month | lots | pnl | net share |", "| --- | --- | ---: | ---: | ---: |"])
    for row in month_contrib.groupby("variant", sort=False).head(5).itertuples(index=False):
        lines.append(f"| {row.variant} | {row.buy_month} | {row.lot_count} | {_money(row.pnl)} | {_pct(row.pnl_share_net)} |")

    (output_dir / "contribution_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    matrix_dir = Path(args.matrix_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    variants = [x.strip() for x in str(args.variants).split(",") if x.strip()]
    trades = pd.concat([_load_trades(matrix_dir, v) for v in variants], ignore_index=True)
    lots = _lot_level(trades)
    lots = lots.rename(columns={"return": "return_"})

    summary = pd.DataFrame([_concentration(lots.rename(columns={"return_": "return"}), v) for v in variants])
    top_lots = lots.sort_values(["variant", "pnl"], ascending=[True, False]).copy()
    code_contrib = _group_contribution(lots, ["code", "name"], "name")
    month_contrib = _group_contribution(lots, ["buy_month"], "buy_month")
    segment_contrib = _group_contribution(lots, ["segment"], "segment")

    incrementals = []
    baseline = variants[0] if variants else ""
    for variant in variants[1:]:
        incrementals.append(_incremental_lots(lots, baseline, variant))
    incremental = pd.concat(incrementals, ignore_index=True) if incrementals else pd.DataFrame()
    delta = _delta_vs_baseline(lots, baseline, variants) if baseline else pd.DataFrame()

    summary.to_csv(output_dir / "concentration_summary.csv", index=False, encoding="utf-8-sig")
    lots.to_csv(output_dir / "lot_level_trades.csv", index=False, encoding="utf-8-sig")
    top_lots.to_csv(output_dir / "top_lots.csv", index=False, encoding="utf-8-sig")
    code_contrib.to_csv(output_dir / "code_contribution.csv", index=False, encoding="utf-8-sig")
    month_contrib.to_csv(output_dir / "month_contribution.csv", index=False, encoding="utf-8-sig")
    segment_contrib.to_csv(output_dir / "segment_contribution.csv", index=False, encoding="utf-8-sig")
    incremental.to_csv(output_dir / "incremental_lots_vs_baseline.csv", index=False, encoding="utf-8-sig")
    delta.to_csv(output_dir / "delta_vs_baseline.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, summary, top_lots, code_contrib, month_contrib)

    payload = {
        "schema_version": 1,
        "matrix_dir": str(matrix_dir),
        "variants": variants,
        "outputs": {
            "summary": "concentration_summary.csv",
            "lots": "lot_level_trades.csv",
            "top_lots": "top_lots.csv",
            "codes": "code_contribution.csv",
            "months": "month_contribution.csv",
            "segments": "segment_contribution.csv",
            "incremental": "incremental_lots_vs_baseline.csv",
            "delta": "delta_vs_baseline.csv",
            "report": "contribution_audit.md",
        },
        "summary": summary.where(pd.notna(summary), None).to_dict("records"),
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit profit contribution concentration for Alpha191 overlay portfolio variants.")
    parser.add_argument("--matrix-dir", default=str(DEFAULT_MATRIX_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--variants", default=",".join(DEFAULT_VARIANTS))
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
