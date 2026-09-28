from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from scripts.gen2_batch_alpha191_factor_tests import _fmt_num, _fmt_pct, _rating, _score_row


def _safe_float(value):
    try:
        value = float(value)
    except Exception:
        return None
    return value if pd.notna(value) else None


def _bucket_map(text):
    try:
        rows = json.loads(text) if isinstance(text, str) and text else []
    except Exception:
        rows = []
    out = {}
    for row in rows:
        try:
            out[int(row.get("bucket"))] = _safe_float(row.get("mean_return"))
        except Exception:
            pass
    return out


def rebuild(report_dir: Path, replacement_csv: Path | None = None) -> None:
    result_path = report_dir / "alpha191_recent_2y_factor_tests.csv"
    df = pd.read_csv(result_path)
    if replacement_csv and replacement_csv.exists():
        repl = pd.read_csv(replacement_csv)
        keys = set(zip(repl["factor_id"], repl["horizon"]))
        df = df[~df.apply(lambda row: (row.get("factor_id"), row.get("horizon")) in keys, axis=1)]
        df = pd.concat([df, repl], ignore_index=True)

    for idx, row in df.iterrows():
        if str(row.get("available")).lower() != "true":
            continue
        spread = _safe_float(row.get("top_bottom_spread"))
        if spread is None:
            continue
        direction = "high" if spread >= 0 else "low"
        buckets = _bucket_map(row.get("bucket_returns"))
        spread_pos = _safe_float(row.get("spread_positive_ratio"))
        df.at[idx, "direction"] = direction
        df.at[idx, "edge_mean"] = abs(spread)
        df.at[idx, "edge_positive_ratio"] = spread_pos if direction == "high" else (1.0 - spread_pos if spread_pos is not None else None)
        df.at[idx, "long_mean_return"] = buckets.get(5) if direction == "high" else buckets.get(1)
        scored = df.loc[idx].to_dict()
        df.at[idx, "effectiveness_score"] = _score_row(scored)
        df.at[idx, "rating"] = _rating(scored)

    df = df.sort_values(["factor_no", "horizon"], na_position="last")
    df.to_csv(result_path, index=False, encoding="utf-8-sig")
    available = df[df["available"] == True].copy()  # noqa: E712
    ranking = available.sort_values(["effectiveness_score", "edge_mean"], ascending=[False, False])
    ranking.to_csv(report_dir / "alpha191_recent_2y_factor_ranking.csv", index=False, encoding="utf-8-sig")
    best = ranking.groupby("factor_id", as_index=False).head(1)
    best.to_csv(report_dir / "alpha191_recent_2y_best_by_factor.csv", index=False, encoding="utf-8-sig")
    _write_findings(report_dir / "findings.md", df, ranking, best)


def _write_findings(path: Path, df: pd.DataFrame, ranking: pd.DataFrame, best: pd.DataFrame) -> None:
    lines = [
        "# G2 Alpha191 Recent 2Y Factor Tests",
        "",
        "- Window: 2024-05-25 to 2026-05-25",
        "- Loaded market rows: 3,983,057; trade days: 790; symbols: 5,266",
        "- Decision boundary: factors are generated after T close and tested on T+1/T+3/T+5 forward returns.",
        "- Coverage: 191 factors, 189 implemented by OHLCV/benchmark formulas; Alpha030 and Alpha143 need extra data sources.",
        "",
        "## Best Horizon Per Factor Top 30",
        "",
        "|Rank|Factor|Horizon|Direction|Rating|Edge|LongRet|EdgeWin|MeanIC|ICIR|Monotonicity|",
        "|---:|---|---:|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, (_, row) in enumerate(best.head(30).iterrows(), start=1):
        lines.append(
            f"|{rank}|{row.get('factor_id')}|T+{int(row.get('horizon') or 0)}|{row.get('direction')}|{row.get('rating')}|"
            f"{_fmt_pct(row.get('edge_mean'))}|{_fmt_pct(row.get('long_mean_return'))}|{_fmt_pct(row.get('edge_positive_ratio'))}|"
            f"{_fmt_num(row.get('mean_ic'), 4)}|{_fmt_num(row.get('ic_ir'), 2)}|{_fmt_num(row.get('bucket_monotonicity'), 2)}|"
        )
    lines.extend(["", "## All Factor-Horizon Top 30", ""])
    lines.extend(
        [
            "|Rank|Factor|Horizon|Direction|Rating|Edge|LongRet|EdgeWin|MeanIC|ICIR|ICDays|",
            "|---:|---|---:|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for rank, (_, row) in enumerate(ranking.head(30).iterrows(), start=1):
        lines.append(
            f"|{rank}|{row.get('factor_id')}|T+{int(row.get('horizon') or 0)}|{row.get('direction')}|{row.get('rating')}|"
            f"{_fmt_pct(row.get('edge_mean'))}|{_fmt_pct(row.get('long_mean_return'))}|{_fmt_pct(row.get('edge_positive_ratio'))}|"
            f"{_fmt_num(row.get('mean_ic'), 4)}|{_fmt_num(row.get('ic_ir'), 2)}|{int(row.get('ic_days') or 0)}|"
        )
    skipped = df[df["available"] != True][["factor_id", "message"]].drop_duplicates()
    if not skipped.empty:
        lines.extend(["", "## Needs Extra Data / Unavailable", ""])
        for _, row in skipped.iterrows():
            lines.append(f"- {row.get('factor_id')}: {row.get('message')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", default="reports/gen2_alpha191_factor_tests")
    parser.add_argument("--replacement-csv", default=None)
    args = parser.parse_args()
    rebuild(Path(args.report_dir), Path(args.replacement_csv) if args.replacement_csv else None)


if __name__ == "__main__":
    main()
