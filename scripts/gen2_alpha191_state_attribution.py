from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1]))

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
DEFAULT_AUDIT_DIR = ROOT / "reports" / "gen2_alpha191_contribution_audit"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_alpha191_state_attribution"
DEFAULT_VARIANTS = ["baseline_trigger_time", "volume5_rank", "volume5_keep80"]


from research.common.reporting import percent_text as _pct


def _money(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):,.0f}"


def _bin_numeric(value: Any, cuts: list[float], labels: list[str]) -> str:
    v = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(v):
        return "missing"
    for cut, label in zip(cuts, labels, strict=False):
        if float(v) <= float(cut):
            return label
    return labels[-1]


def _load_signals(matrix_dir: Path, variant: str) -> pd.DataFrame:
    path = matrix_dir / "backtests" / variant / "signals.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    d = pd.read_csv(path)
    d["variant"] = variant
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d["code"] = d["code"].astype(str)
    return d


def _load_lots(audit_dir: Path, variants: list[str]) -> pd.DataFrame:
    path = audit_dir / "lot_level_trades.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    d = pd.read_csv(path)
    d = d[d["variant"].isin(variants)].copy()
    d["buy_datetime"] = pd.to_datetime(d["buy_datetime"], errors="coerce")
    d["code"] = d["code"].astype(str)
    for col in ["pnl", "capital", "return_", "v4_rank", "v4_score"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    return d


def _enrich_lots(matrix_dir: Path, audit_dir: Path, variants: list[str]) -> pd.DataFrame:
    lots = _load_lots(audit_dir, variants)
    signals = pd.concat([_load_signals(matrix_dir, v) for v in variants], ignore_index=True)
    keep_cols = [
        "variant",
        "code",
        "confirm_datetime",
        "index_close_ge_ma20",
        "index_mom20",
        "market_breadth",
        "cap_bucket",
        "float_market_cap_yi",
        "overhead_pressure_days",
        "overhead_big_pressure_days",
        "overhead_pressure_amount_share",
        "prior_max_high_ratio",
        "runup_from_60d_low",
        "recent60_return",
        "mom5",
        "mom10",
        "mom20",
        "vol_ratio",
        "rt_return_from_d1_close",
        "rt_30m_amount_ratio",
        "alpha191_overlay_score",
        "alpha191_original_v4_rank",
        "alpha191_original_v4_score",
        "rank_change_status",
        "rank_change",
        "score_change",
    ]
    merged = lots.merge(
        signals[[c for c in keep_cols if c in signals.columns]],
        left_on=["variant", "code", "buy_datetime"],
        right_on=["variant", "code", "confirm_datetime"],
        how="left",
    )
    merged["market_ma20_state"] = np.where(merged["index_close_ge_ma20"].fillna(False), "index_ge_ma20", "index_lt_ma20")
    merged["index_mom20_bin"] = merged["index_mom20"].map(
        lambda x: _bin_numeric(x, [-0.001, 0.05, 0.10, 9.0], ["mom20_le_0", "mom20_0_5", "mom20_5_10", "mom20_gt_10"])
    )
    merged["breadth_bin"] = merged["market_breadth"].map(
        lambda x: _bin_numeric(x, [0.50, 0.80, 9.0], ["breadth_le_50", "breadth_50_80", "breadth_gt_80"])
    )
    merged["overhead_big_bin"] = merged["overhead_big_pressure_days"].map(
        lambda x: _bin_numeric(x, [0, 2, 999], ["big_pressure_0", "big_pressure_1_2", "big_pressure_ge_3"])
    )
    merged["overhead_share_bin"] = merged["overhead_pressure_amount_share"].map(
        lambda x: _bin_numeric(x, [0, 0.05, 9.0], ["overhead_share_0", "overhead_share_0_5", "overhead_share_gt_5"])
    )
    merged["runup_bin"] = merged["runup_from_60d_low"].map(
        lambda x: _bin_numeric(x, [0.30, 0.60, 1.00, 99.0], ["runup_le_30", "runup_30_60", "runup_60_100", "runup_gt_100"])
    )
    merged["recent60_bin"] = merged["recent60_return"].map(
        lambda x: _bin_numeric(x, [-0.001, 0.20, 0.50, 99.0], ["recent60_le_0", "recent60_0_20", "recent60_20_50", "recent60_gt_50"])
    )
    merged["rt_return_bin"] = merged["rt_return_from_d1_close"].map(
        lambda x: _bin_numeric(x, [0.03, 0.06, 0.09, 99.0], ["rt_le_3", "rt_3_6", "rt_6_9", "rt_gt_9"])
    )
    merged["rt_amount_bin"] = merged["rt_30m_amount_ratio"].map(
        lambda x: _bin_numeric(x, [2.0, 4.0, 6.0, 99.0], ["amt_le_2", "amt_2_4", "amt_4_6", "amt_gt_6"])
    )
    merged["alpha_score_bin"] = merged["alpha191_overlay_score"].map(
        lambda x: _bin_numeric(x, [0.40, 0.60, 0.80, 1.01], ["alpha_le_40", "alpha_40_60", "alpha_60_80", "alpha_gt_80"])
    )
    merged["alpha_rank_bin"] = merged["v4_rank"].map(lambda x: "rank1" if pd.notna(x) and float(x) <= 1 else "rank_gt1")
    merged["original_v4_rank_bin"] = merged["alpha191_original_v4_rank"].map(
        lambda x: _bin_numeric(x, [50, 100, 150, 999], ["orig_rank_1_50", "orig_rank_51_100", "orig_rank_101_150", "orig_rank_gt_150"])
    )
    return merged


def _summarize_group(d: pd.DataFrame, group_col: str) -> pd.DataFrame:
    rows = []
    for variant, vdf in d.groupby("variant", sort=False):
        total = float(vdf["pnl"].sum())
        g = (
            vdf.groupby(group_col, dropna=False)
            .agg(
                lot_count=("pnl", "size"),
                pnl=("pnl", "sum"),
                win_rate=("pnl", lambda s: float((s > 0).mean())),
                avg_return=("return_", "mean"),
                median_return=("return_", "median"),
                avg_alpha_score=("alpha191_overlay_score", "mean"),
            )
            .reset_index()
            .rename(columns={group_col: "bucket"})
        )
        g["variant"] = variant
        g["dimension"] = group_col
        g["pnl_share"] = g["pnl"].map(lambda x: float(x) / total if total else np.nan)
        rows.append(g)
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if out.empty:
        return out
    return out[["variant", "dimension", "bucket", "lot_count", "pnl", "pnl_share", "win_rate", "avg_return", "median_return", "avg_alpha_score"]]


def _write_report(output_dir: Path, rows: pd.DataFrame, lots: pd.DataFrame) -> None:
    lines = [
        "# G2 Alpha191 State Attribution",
        "",
        "This report attributes lot-level PnL by market, pressure, shape and Alpha191 score buckets.",
        "",
    ]
    for dim in ["segment", "market_ma20_state", "index_mom20_bin", "breadth_bin", "cap_bucket", "overhead_big_bin", "runup_bin", "rt_return_bin", "alpha_score_bin"]:
        sub = rows[rows["dimension"].eq(dim)].copy()
        if sub.empty:
            continue
        lines.extend([f"## {dim}", "", "| variant | bucket | lots | pnl | share | win | avg_ret |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"])
        sub = sub.sort_values(["variant", "pnl"], ascending=[True, False])
        for row in sub.itertuples(index=False):
            lines.append(f"| {row.variant} | {row.bucket} | {row.lot_count} | {_money(row.pnl)} | {_pct(row.pnl_share)} | {_pct(row.win_rate)} | {_pct(row.avg_return)} |")
        lines.append("")

    inc = lots[lots["variant"].isin(["volume5_rank", "volume5_keep80"])].copy()
    lines.extend(["## 2026 Blind Winners", "", "| variant | code | name | buy | pnl | return | alpha | state |", "| --- | --- | --- | --- | ---: | ---: | ---: | --- |"])
    top = inc[inc["segment"].eq("blind_2026ytd")].sort_values("pnl", ascending=False).head(15)
    for row in top.itertuples(index=False):
        lines.append(
            f"| {row.variant} | {row.code} | {row.name} | {pd.Timestamp(row.buy_datetime).strftime('%Y-%m-%d %H:%M')} | "
            f"{_money(row.pnl)} | {_pct(row.return_)} | {_pct(row.alpha191_overlay_score)} | {row.market_ma20_state}/{row.overhead_big_bin}/{row.rt_return_bin} |"
        )
    (output_dir / "state_attribution.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    matrix_dir = Path(args.matrix_dir)
    audit_dir = Path(args.audit_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    variants = [x.strip() for x in str(args.variants).split(",") if x.strip()]
    lots = _enrich_lots(matrix_dir, audit_dir, variants)
    dimensions = [
        "segment",
        "market_ma20_state",
        "index_mom20_bin",
        "breadth_bin",
        "cap_bucket",
        "overhead_big_bin",
        "overhead_share_bin",
        "runup_bin",
        "recent60_bin",
        "rt_return_bin",
        "rt_amount_bin",
        "alpha_score_bin",
        "alpha_rank_bin",
        "original_v4_rank_bin",
        "rank_change_status",
    ]
    rows = pd.concat([_summarize_group(lots, dim) for dim in dimensions], ignore_index=True)
    lots.to_csv(output_dir / "attributed_lots.csv", index=False, encoding="utf-8-sig")
    rows.to_csv(output_dir / "state_attribution_summary.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, rows, lots)
    payload = {
        "schema_version": 1,
        "variants": variants,
        "outputs": {
            "lots": "attributed_lots.csv",
            "summary": "state_attribution_summary.csv",
            "report": "state_attribution.md",
        },
        "top_rows": rows.sort_values("pnl", ascending=False).head(30).where(
            pd.notna(rows.sort_values("pnl", ascending=False).head(30)),
            None,
        ).to_dict("records"),
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Attribute Alpha191 overlay trades by market and candidate states.")
    parser.add_argument("--matrix-dir", default=str(DEFAULT_MATRIX_DIR))
    parser.add_argument("--audit-dir", default=str(DEFAULT_AUDIT_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--variants", default=",".join(DEFAULT_VARIANTS))
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
