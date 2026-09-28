from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


RET_COLS = [
    "fwd_ret_confirm_to_close_1d",
    "fwd_ret_confirm_to_close_2d",
    "fwd_ret_confirm_to_close_3d",
    "fwd_ret_confirm_to_close_5d",
    "fwd_ret_confirm_to_close_10d",
    "fwd_ret_confirm_to_close_20d",
]


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{v * 100:.2f}%"


def _add_labels(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    big_down = pd.to_numeric(out["big_down_rate"], errors="coerce")
    amount = pd.to_numeric(out["market_amount_ratio20"], errors="coerce")
    range_pos60 = pd.to_numeric(out["range_pos60"], errors="coerce")

    out["g3_position_guard"] = np.select(
        [
            range_pos60 <= 0.15,
            (range_pos60 > 0.15) & (range_pos60 <= 0.30),
            range_pos60 > 0.30,
        ],
        ["low_safety_margin", "mid_low_position", "not_low_enough"],
        default="missing",
    )
    out["g3_capitulation_strength"] = np.select(
        [
            big_down <= 0.12,
            (big_down > 0.12) & (big_down <= 0.20),
            (big_down > 0.20) & (big_down <= 0.35),
            big_down > 0.35,
        ],
        ["low_intensity", "mid_intensity_risk", "strong_capitulation", "extreme_capitulation"],
        default="missing",
    )
    out["g3_volume_context"] = np.select(
        [
            amount <= 0.90,
            (amount > 0.90) & (amount <= 1.05),
            (amount > 1.05) & (amount <= 1.20),
            amount > 1.20,
        ],
        ["dry_volume", "neutral_volume", "healthy_release", "overheated_volume"],
        default="missing",
    )
    out["g3_repair_env_label"] = np.select(
        [
            (big_down > 0.35) & (amount > 0.90) & (amount <= 1.20),
            (big_down > 0.20) & (big_down <= 0.35) & (amount > 0.90) & (amount <= 1.20),
            (big_down > 0.12) & (big_down <= 0.20) & (amount > 0.90),
            (range_pos60 <= 0.15) & (big_down <= 0.12),
        ],
        [
            "extreme_clearance",
            "strong_clearance",
            "mid_panic_failure_risk",
            "low_position_wait_repair",
        ],
        default="neutral_or_unclassified",
    )
    return out


def _summarize(df: pd.DataFrame, group_cols: list[str], min_signals: int) -> pd.DataFrame:
    rows: list[dict] = []
    if df.empty:
        return pd.DataFrame()
    for keys, group in df.groupby(group_cols, dropna=False, sort=True):
        if not isinstance(keys, tuple):
            keys = (keys,)
        item = {col: val for col, val in zip(group_cols, keys)}
        item["signals"] = int(len(group))
        item["days"] = int(group["entry_date"].nunique())
        item["unique_codes"] = int(group["code"].nunique())
        item["status"] = "ok" if len(group) >= min_signals else "small_sample"
        for col in RET_COLS:
            label = col.replace("fwd_ret_confirm_to_close_", "")
            s = pd.to_numeric(group[col], errors="coerce").dropna()
            item[f"mean_{label}"] = float(s.mean()) if len(s) else np.nan
            item[f"median_{label}"] = float(s.median()) if len(s) else np.nan
            item[f"win_{label}"] = float((s > 0).mean()) if len(s) else np.nan
        rows.append(item)
    return pd.DataFrame(rows)


def _display(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    for col in out.columns:
        if col.startswith(("mean_", "median_", "win_")):
            out[col] = out[col].map(_pct)
    return out


def _policy_masks(df: pd.DataFrame) -> dict[str, pd.Series]:
    chain = df["g3_chain"].eq("panic_v2_deep_wash_repair")
    lowpos = df["g3_position_guard"].eq("low_safety_margin")
    midrisk = df["g3_repair_env_label"].eq("mid_panic_failure_risk")
    clearance = df["g3_repair_env_label"].isin(["strong_clearance", "extreme_clearance"])
    return {
        "deep_all": chain,
        "deep_lowpos": chain & lowpos,
        "deep_lowpos_avoid_midrisk": chain & lowpos & ~midrisk,
        "deep_lowpos_clearance_only": chain & lowpos & clearance,
    }


def _policy_table(df: pd.DataFrame, min_signals: int) -> pd.DataFrame:
    rows: list[dict] = []
    for name, mask in _policy_masks(df).items():
        d = df[mask].copy()
        if d.empty:
            continue
        d["year_label"] = pd.to_datetime(d["entry_date"]).dt.year.astype(str)
        groups = [("full", d)]
        groups.extend((str(year), g) for year, g in d.groupby("year_label", sort=True))
        for period, group in groups:
            item = {
                "policy": name,
                "period": period,
                "signals": int(len(group)),
                "days": int(group["entry_date"].nunique()),
                "unique_codes": int(group["code"].nunique()),
                "status": "ok" if len(group) >= min_signals else "small_sample",
            }
            for col in RET_COLS:
                label = col.replace("fwd_ret_confirm_to_close_", "")
                s = pd.to_numeric(group[col], errors="coerce").dropna()
                item[f"mean_{label}"] = float(s.mean()) if len(s) else np.nan
                item[f"win_{label}"] = float((s > 0).mean()) if len(s) else np.nan
            rows.append(item)
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit G3 panic failure environment labels.")
    parser.add_argument("--input", default="reports/gen3_panic_v2_research/layer_audit_v1/layer_labeled_signals.parquet")
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/failure_env_audit_v1")
    parser.add_argument("--min-signals", type=int, default=20)
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(args.input)
    df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
    labeled = _add_labels(df)
    labeled.to_parquet(out_dir / "failure_env_labeled_signals.parquet", index=False)
    labeled.to_csv(out_dir / "failure_env_labeled_signals.csv", index=False, encoding="utf-8-sig")

    specs = {
        "repair_env": ["g3_chain", "g3_repair_env_label"],
        "capitulation_strength": ["g3_chain", "g3_capitulation_strength"],
        "volume_context": ["g3_chain", "g3_volume_context"],
        "position_guard": ["g3_chain", "g3_position_guard"],
        "repair_env_by_year": ["g3_chain", "g3_repair_env_label", "year"],
    }
    labeled["year"] = pd.to_datetime(labeled["entry_date"]).dt.year
    raw_tables: dict[str, pd.DataFrame] = {}
    for name, cols in specs.items():
        raw = _summarize(labeled, cols, args.min_signals)
        raw_tables[name] = raw
        raw.to_csv(out_dir / f"{name}_summary_raw.csv", index=False, encoding="utf-8-sig")
        _display(raw).to_csv(out_dir / f"{name}_summary_display.csv", index=False, encoding="utf-8-sig")

    policy_raw = _policy_table(labeled, args.min_signals)
    policy_raw.to_csv(out_dir / "policy_probe_raw.csv", index=False, encoding="utf-8-sig")
    policy_display = _display(policy_raw)
    policy_display.to_csv(out_dir / "policy_probe_display.csv", index=False, encoding="utf-8-sig")

    full_policy = policy_display[policy_display["period"].eq("full")]
    repair_deep = _display(raw_tables["repair_env"][raw_tables["repair_env"]["g3_chain"].eq("panic_v2_deep_wash_repair")])
    cap_deep = _display(raw_tables["capitulation_strength"][raw_tables["capitulation_strength"]["g3_chain"].eq("panic_v2_deep_wash_repair")])

    lines = [
        "# G3 Panic 修复失败环境标签审计",
        "",
        "## 口径",
        "",
        f"- 输入：`{args.input}`",
        f"- 输出目录：`{out_dir}`",
        "- 目标：验证“假恐慌/修复失败环境”是否能被少量可解释标签识别；本轮不做参数搜索。",
        "",
        "## 标签定义",
        "",
        "- `low_safety_margin`：个股 `range_pos60 <= 15%`。",
        "- `mid_panic_failure_risk`：市场大跌率在 `12%-20%` 且成交不低于 20 日均量的中等恐慌，暂视为假恐慌风险。",
        "- `strong_clearance`：市场大跌率在 `20%-35%` 且成交适度释放。",
        "- `extreme_clearance`：市场大跌率 `>35%` 且成交适度释放。",
        "",
        "## deep_wash 按修复环境分层",
        "",
        repair_deep[
            [
                "g3_chain",
                "g3_repair_env_label",
                "signals",
                "days",
                "status",
                "mean_3d",
                "mean_5d",
                "win_5d",
                "mean_10d",
                "mean_20d",
            ]
        ].to_markdown(index=False),
        "",
        "## deep_wash 按恐慌强度分层",
        "",
        cap_deep[
            [
                "g3_chain",
                "g3_capitulation_strength",
                "signals",
                "days",
                "status",
                "mean_3d",
                "mean_5d",
                "win_5d",
                "mean_10d",
                "mean_20d",
            ]
        ].to_markdown(index=False),
        "",
        "## 少量策略口径验证",
        "",
        full_policy[
            [
                "policy",
                "signals",
                "days",
                "unique_codes",
                "status",
                "mean_3d",
                "mean_5d",
                "win_5d",
                "mean_10d",
                "mean_20d",
            ]
        ].to_markdown(index=False),
        "",
        "## 判断",
        "",
        "1. `deep_lowpos_avoid_midrisk` 是下一轮更稳的研究口径：保留低位安全边际，同时规避中等恐慌假信号。",
        "2. `deep_lowpos_clearance_only` 收益更高，但样本会明显变少，不能直接作为正式策略。",
        "3. 如果某年度仍然失效，说明还需要盘中承接质量或后续止损机制，而不是继续细切日线标签。",
    ]
    (out_dir / "failure_env_audit_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input": args.input,
        "output_dir": str(out_dir),
        "rows": int(len(labeled)),
        "deep_rows": int(labeled["g3_chain"].eq("panic_v2_deep_wash_repair").sum()),
        "min_signals": args.min_signals,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
