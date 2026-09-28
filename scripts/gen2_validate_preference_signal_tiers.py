from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_analyze_trade_preferences import (  # noqa: E402
    DEFAULT_RUN_DIR,
    _add_composite_scores,
    _load_daily,
    _num,
    _post_entry_features,
    _prior_features,
)


DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_preference_signal_tier_validation"


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if pd.isna(value):
        return None
    return str(value)


def _load_signals(path: Path) -> pd.DataFrame:
    signals = pd.read_csv(path)
    signals["code"] = signals["code"].astype(str)
    signals["entry_date"] = pd.to_datetime(signals["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    signals["confirm_datetime"] = pd.to_datetime(signals["confirm_datetime"], errors="coerce")
    signals["signal_id"] = signals["code"] + "|" + signals["confirm_datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
    for col in signals.columns:
        if col in {"code", "name", "entry_date", "g2_open_state", "pattern", "trigger_type", "signal_id"}:
            continue
        if "datetime" in col or "date" in col:
            continue
        signals[col] = pd.to_numeric(signals[col], errors="ignore")
    return signals.dropna(subset=["code", "entry_date", "entry_price"]).reset_index(drop=True)


def _add_signal_features(signals: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    by_code = {code: group.reset_index(drop=True) for code, group in daily.groupby("code", sort=False)}
    rows: list[dict[str, Any]] = []
    for _, row in signals.iterrows():
        history = by_code.get(str(row["code"]))
        if history is None:
            rows.append({})
            continue
        entry_date = str(row["entry_date"])
        entry_price = _num(row.get("entry_price"))
        rows.append(
            {
                **_prior_features(history, entry_date, entry_price),
                **_post_entry_features(history, entry_date, entry_price),
            }
        )
    features = pd.DataFrame(rows)
    out = pd.concat([signals.reset_index(drop=True), features.reset_index(drop=True)], axis=1)
    for col in ["v4_rank", "v4_score", "mom5", "mom10", "mom20", "vol_ratio", "vol10", "volume_ratio", "breadth_ma20"]:
        if col in out.columns:
            out[f"sig_{col}"] = pd.to_numeric(out[col], errors="coerce")
    return _add_composite_scores(out)


def _tier_reason(row: pd.Series) -> tuple[str, str]:
    overhead = _num(row.get("overhead_pressure_score"), 0.5)
    effective = _num(row.get("effective_pressure_score"), 0.5)
    late = _num(row.get("late_stage_risk_score"), 0.35)
    wave = _num(row.get("wave_late_score"), 0.0)
    downtrend = _num(row.get("downtrend_rebound_score"), 0.0)
    distribution = _num(row.get("volume_bear_distribution_score"), 0.0)
    chase = _num(row.get("ma_chase_risk_score"), 0.0)

    hard_rejects: list[str] = []
    if overhead >= 0.65:
        hard_rejects.append("pressure_dense")
    if effective >= 0.60:
        hard_rejects.append("effective_pressure_high")
    if chase >= 0.85:
        hard_rejects.append("ma_chase")
    if wave >= 0.70:
        hard_rejects.append("late_wave")
    if downtrend >= 0.70:
        hard_rejects.append("downtrend_rebound")
    if distribution >= 0.70:
        hard_rejects.append("distribution_bar")
    if hard_rejects:
        return "C", ",".join(hard_rejects)

    if (
        overhead <= 0.35
        and effective <= 0.40
        and late <= 0.55
        and wave <= 0.55
        and downtrend <= 0.55
        and distribution <= 0.55
        and chase <= 0.65
    ):
        return "A", "low_pressure_clean_risk"

    reasons: list[str] = []
    if overhead > 0.35:
        reasons.append("pressure_watch")
    if effective > 0.40:
        reasons.append("needs_absorption")
    if late > 0.55 or wave > 0.55:
        reasons.append("late_risk_watch")
    if downtrend > 0.55:
        reasons.append("downtrend_watch")
    if distribution > 0.55:
        reasons.append("distribution_watch")
    if chase > 0.65:
        reasons.append("chase_watch")
    return "B", ",".join(reasons) or "mixed_quality"


def _assign_tiers(signals: pd.DataFrame) -> pd.DataFrame:
    d = signals.copy()
    tier_info = d.apply(_tier_reason, axis=1, result_type="expand")
    d["preference_tier"] = tier_info[0]
    d["tier_reason"] = tier_info[1]
    d["decision_weight"] = d["preference_tier"].map({"A": 1.0, "B": 0.5, "C": 0.0}).fillna(0.0)
    c_info = d.apply(_c_subtier_reason, axis=1, result_type="expand")
    d["c_subtier"] = c_info[0]
    d["c_subtier_reason"] = c_info[1]
    d["decision_group"] = np.where(d["preference_tier"].eq("C"), d["c_subtier"], d["preference_tier"])
    return d


def _c_subtier_reason(row: pd.Series) -> tuple[str, str]:
    if str(row.get("preference_tier")) != "C":
        return str(row.get("preference_tier")), "not_c"

    reason = str(row.get("tier_reason") or "")
    mom20 = _num(row.get("mom20"), 0.0)
    volume_ratio = _num(row.get("volume_ratio"), 0.0)
    effective = _num(row.get("effective_pressure_score"), 0.5)
    preference = _num(row.get("preference_score_v2"), 0.0)

    if any(token in reason for token in ["distribution_bar", "late_wave", "downtrend_rebound"]):
        return "C-risk", reason

    pressure_only = reason == "pressure_dense"
    pressure_without_structural_risk = "pressure_dense" in reason and not any(
        token in reason for token in ["distribution_bar", "late_wave", "downtrend_rebound", "ma_chase"]
    )
    if pressure_only:
        return "C-aggressive", "dense_pressure_but_absorbed"
    if pressure_without_structural_risk and (mom20 >= 0.12 or volume_ratio >= 3.0 or preference >= 0.35):
        return "C-aggressive", "dense_pressure_with_momentum"
    if "ma_chase" in reason and (mom20 >= 0.30 or volume_ratio >= 4.0):
        return "C-review", "ma_chase_high_momentum"
    if effective >= 0.60 and mom20 < 0.12:
        return "C-review", "high_effective_pressure_low_momentum"
    return "C-review", reason or "unclassified_c"


def _segment(entry_date: str) -> str:
    date = pd.Timestamp(entry_date)
    if date <= pd.Timestamp("2025-03-31"):
        return "train_2024h2_2025q1"
    if date <= pd.Timestamp("2025-12-31"):
        return "validation_2025q2_q4"
    return "blind_2026ytd"


def _summarize(group: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    grouped = group.groupby(keys, dropna=False, observed=False) if keys else [((), group)]
    for key, g in grouped:
        key_values = key if isinstance(key, tuple) else (key,)
        row = {name: value for name, value in zip(keys, key_values)}
        fwd5 = pd.to_numeric(g.get("entry_fwd_ret_5d"), errors="coerce")
        fwd10 = pd.to_numeric(g.get("entry_fwd_ret_10d"), errors="coerce")
        fwd20 = pd.to_numeric(g.get("entry_fwd_ret_20d"), errors="coerce")
        row.update(
            {
                "signals": int(len(g)),
                "avg_fwd_5d": float(fwd5.mean()),
                "win_5d": float((fwd5 > 0).mean()),
                "avg_fwd_10d": float(fwd10.mean()),
                "win_10d": float((fwd10 > 0).mean()),
                "avg_fwd_20d": float(fwd20.mean()),
                "win_20d": float((fwd20 > 0).mean()),
                "avg_post10_max_ret": float(pd.to_numeric(g.get("post10_max_ret"), errors="coerce").mean()),
                "avg_post10_min_ret": float(pd.to_numeric(g.get("post10_min_ret"), errors="coerce").mean()),
                "post5_stop_touch_rate": float((pd.to_numeric(g.get("post5_stop_loss_touch"), errors="coerce") > 0).mean()),
                "secondary_breakout_rate": float((pd.to_numeric(g.get("post10_secondary_volume_breakout"), errors="coerce") > 0).mean()),
                "post_absorption_rate": float((pd.to_numeric(g.get("post_absorption_confirmed"), errors="coerce") > 0).mean()),
                "avg_preference_score_v2": float(pd.to_numeric(g.get("preference_score_v2"), errors="coerce").mean()),
                "avg_effective_pressure": float(pd.to_numeric(g.get("effective_pressure_score"), errors="coerce").mean()),
                "avg_overhead_pressure": float(pd.to_numeric(g.get("overhead_pressure_score"), errors="coerce").mean()),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows)


def _write_report(
    output_dir: Path,
    overall: pd.DataFrame,
    by_segment: pd.DataFrame,
    reason_summary: pd.DataFrame,
    c_subtier: pd.DataFrame,
    c_subtier_by_segment: pd.DataFrame,
) -> None:
    lines = [
        "# G2 Preference Tier Signal Validation",
        "",
        "This is signal-level validation only. Tiers use entry-time/prior features; post-entry fields are evaluation labels.",
        "",
        "## Overall Tier Performance",
        "",
        "| tier | signals | avg5 | win5 | avg10 | win10 | avg20 | win20 | max10 | min10 | stop5 | secondary | absorption |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, row in overall.iterrows():
        lines.append(
            f"| {row['preference_tier']} | {int(row['signals'])} | {row['avg_fwd_5d']:.2%} | {row['win_5d']:.2%} | "
            f"{row['avg_fwd_10d']:.2%} | {row['win_10d']:.2%} | {row['avg_fwd_20d']:.2%} | {row['win_20d']:.2%} | "
            f"{row['avg_post10_max_ret']:.2%} | {row['avg_post10_min_ret']:.2%} | {row['post5_stop_touch_rate']:.2%} | "
            f"{row['secondary_breakout_rate']:.2%} | {row['post_absorption_rate']:.2%} |"
        )
    lines.extend(["", "## Segment Tier Performance", ""])
    lines.append("| segment | tier | signals | avg10 | win10 | avg20 | win20 | stop5 | secondary |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for _, row in by_segment.iterrows():
        lines.append(
            f"| {row['segment']} | {row['preference_tier']} | {int(row['signals'])} | {row['avg_fwd_10d']:.2%} | "
            f"{row['win_10d']:.2%} | {row['avg_fwd_20d']:.2%} | {row['win_20d']:.2%} | "
            f"{row['post5_stop_touch_rate']:.2%} | {row['secondary_breakout_rate']:.2%} |"
        )
    lines.extend(["", "## Top Tier Reasons", ""])
    lines.append("| tier | reason | signals | avg10 | stop5 | secondary |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: |")
    for _, row in reason_summary.head(20).iterrows():
        lines.append(
            f"| {row['preference_tier']} | {row['tier_reason']} | {int(row['signals'])} | "
            f"{row['avg_fwd_10d']:.2%} | {row['post5_stop_touch_rate']:.2%} | {row['secondary_breakout_rate']:.2%} |"
        )
    lines.extend(["", "## C Subtier Performance", ""])
    lines.append("| c_subtier | signals | avg5 | avg10 | avg20 | stop5 | secondary |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for _, row in c_subtier.iterrows():
        lines.append(
            f"| {row['c_subtier']} | {int(row['signals'])} | {row['avg_fwd_5d']:.2%} | "
            f"{row['avg_fwd_10d']:.2%} | {row['avg_fwd_20d']:.2%} | "
            f"{row['post5_stop_touch_rate']:.2%} | {row['secondary_breakout_rate']:.2%} |"
        )
    lines.extend(["", "## C Subtier By Segment", ""])
    lines.append("| segment | c_subtier | signals | avg10 | avg20 | stop5 | secondary |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for _, row in c_subtier_by_segment.iterrows():
        lines.append(
            f"| {row['segment']} | {row['c_subtier']} | {int(row['signals'])} | "
            f"{row['avg_fwd_10d']:.2%} | {row['avg_fwd_20d']:.2%} | "
            f"{row['post5_stop_touch_rate']:.2%} | {row['secondary_breakout_rate']:.2%} |"
        )
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    signals = _load_signals(run_dir / "signals.csv")
    start_date = str(signals["entry_date"].min())
    last_entry = pd.Timestamp(str(signals["entry_date"].max()))
    end_date = (last_entry + pd.Timedelta(days=45)).strftime("%Y-%m-%d")
    daily = _load_daily(sorted(signals["code"].unique().tolist()), start_date, end_date)
    enriched = _assign_tiers(_add_signal_features(signals, daily))
    enriched["segment"] = enriched["entry_date"].map(_segment)

    tier_order = pd.CategoricalDtype(categories=["A", "B", "C"], ordered=True)
    enriched["preference_tier"] = enriched["preference_tier"].astype(tier_order)
    enriched = enriched.sort_values(["entry_date", "confirm_datetime", "code"]).reset_index(drop=True)

    overall = _summarize(enriched, ["preference_tier"]).sort_values("preference_tier").reset_index(drop=True)
    by_segment = _summarize(enriched, ["segment", "preference_tier"]).sort_values(["segment", "preference_tier"]).reset_index(drop=True)
    reason_summary = (
        _summarize(enriched, ["preference_tier", "tier_reason"])
        .sort_values(["preference_tier", "signals"], ascending=[True, False])
        .reset_index(drop=True)
    )
    c_only = enriched[enriched["preference_tier"].astype(str) == "C"].copy()
    c_subtier = _summarize(c_only, ["c_subtier"]).sort_values("c_subtier").reset_index(drop=True)
    c_subtier_by_segment = _summarize(c_only, ["segment", "c_subtier"]).sort_values(["segment", "c_subtier"]).reset_index(drop=True)

    enriched.to_csv(output_dir / "signals_with_preference_tiers.csv", index=False, encoding="utf-8-sig")
    overall.to_csv(output_dir / "tier_summary.csv", index=False, encoding="utf-8-sig")
    by_segment.to_csv(output_dir / "tier_by_segment.csv", index=False, encoding="utf-8-sig")
    reason_summary.to_csv(output_dir / "tier_reason_summary.csv", index=False, encoding="utf-8-sig")
    c_subtier.to_csv(output_dir / "c_subtier_summary.csv", index=False, encoding="utf-8-sig")
    c_subtier_by_segment.to_csv(output_dir / "c_subtier_by_segment.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, overall, by_segment, reason_summary, c_subtier, c_subtier_by_segment)

    summary = {
        "signals": int(len(enriched)),
        "start_date": start_date,
        "end_date": str(signals["entry_date"].max()),
        "daily_feature_end_date": end_date,
        "tier_counts": {str(k): int(v) for k, v in enriched["preference_tier"].value_counts().sort_index().to_dict().items()},
        "output_dir": str(output_dir),
        "overall": overall.to_dict("records"),
        "by_segment": by_segment.to_dict("records"),
        "c_subtier": c_subtier.to_dict("records"),
        "c_subtier_by_segment": c_subtier_by_segment.to_dict("records"),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate G2 preference A/B/C tiers at signal level.")
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
