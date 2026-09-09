from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path

import pandas as pd


ROOT = _PROJECT_ROOT
SRC_DIR = _report_path() / "gen3_v4_strong_position_scale_probe_v1"
OUT_DIR = _report_path() / "gen3_v4_strong_position_scale_windows_v1"
INITIAL_CAPITAL = 150_000.0

WINDOWS = {
    "weak_gap_2022_2024": ("2022-01-01", "2024-12-31"),
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-05-29"),
    "full": ("2020-01-01", "2026-05-29"),
}

VARIANTS = [
    "base",
    "strong_daily_limit1",
    "strong_daily_limit1_q1q2_half",
    "strong_second_half",
    "strong_second_score_ge_070",
    "strong_second_score_ge_080",
    "strong_second_score_ge_090",
    "strong_second_score_ge_093",
    "strong_second_score_ge_095",
    "strong_second_q75",
]
PROFILES = ["cost30", "cost100", "cost30_all_shock2"]


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def pct(v: float | int | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows: list[dict[str, str]] = []
    for _, row in df.iterrows():
        item: dict[str, str] = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_run(variant: str, profile: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    run_dir = SRC_DIR / f"{variant}__{profile}"
    curve = pd.read_csv(run_dir / "mtm_equity_curve.csv")
    closed = pd.read_csv(run_dir / "closed_trades.csv")
    curve["date"] = pd.to_datetime(curve["date"], errors="coerce").dt.normalize()
    closed["entry_date"] = pd.to_datetime(closed["entry_date"], errors="coerce").dt.normalize()
    for col in ["equity", "worst_open_mtm_ret"]:
        curve[col] = pd.to_numeric(curve[col], errors="coerce")
    for col in ["policy_net_ret", "realized_pnl", "position_scale"]:
        if col in closed.columns:
            closed[col] = pd.to_numeric(closed[col], errors="coerce")
    return curve, closed


def window_metrics(variant: str, profile: str, curve: pd.DataFrame, closed: pd.DataFrame) -> list[dict]:
    rows = []
    for window, (start, end) in WINDOWS.items():
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        cw = curve[(curve["date"] >= start_ts) & (curve["date"] <= end_ts)].copy()
        tw = closed[(closed["entry_date"] >= start_ts) & (closed["entry_date"] <= end_ts)].copy()
        if cw.empty:
            continue
        start_eq = float(cw["equity"].iloc[0])
        end_eq = float(cw["equity"].iloc[-1])
        net = pd.to_numeric(tw.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "variant": variant,
                "profile": profile,
                "window": window,
                "return": end_eq / start_eq - 1.0,
                "max_drawdown": max_drawdown(cw["equity"] / start_eq),
                "trade_count": int(len(tw)),
                "strong_trades": int(tw["route"].astype(str).eq("strong_main").sum()) if not tw.empty else 0,
                "scaled_trades": int(pd.to_numeric(tw.get("position_scale", 1.0), errors="coerce").fillna(1.0).lt(1.0).sum()) if not tw.empty else 0,
                "win_rate": float((net > 0).mean()) if len(net) else 0.0,
                "avg_trade_return": float(net.mean()) if len(net) else 0.0,
                "worst_trade": float(net.min()) if len(net) else 0.0,
                "worst_open_mtm_ret": float(cw["worst_open_mtm_ret"].min()),
            }
        )
    return rows


def annual_metrics(variant: str, profile: str, curve: pd.DataFrame, closed: pd.DataFrame) -> list[dict]:
    rows = []
    for year, cw in curve.groupby(curve["date"].dt.year):
        cw = cw.sort_values("date")
        tw = closed[closed["entry_date"].dt.year.eq(year)].copy()
        if cw.empty:
            continue
        start_eq = float(cw["equity"].iloc[0])
        end_eq = float(cw["equity"].iloc[-1])
        net = pd.to_numeric(tw.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "variant": variant,
                "profile": profile,
                "year": int(year),
                "return": end_eq / start_eq - 1.0,
                "max_drawdown": max_drawdown(cw["equity"] / start_eq),
                "trade_count": int(len(tw)),
                "strong_trades": int(tw["route"].astype(str).eq("strong_main").sum()) if not tw.empty else 0,
                "scaled_trades": int(pd.to_numeric(tw.get("position_scale", 1.0), errors="coerce").fillna(1.0).lt(1.0).sum()) if not tw.empty else 0,
                "win_rate": float((net > 0).mean()) if len(net) else 0.0,
                "avg_trade_return": float(net.mean()) if len(net) else 0.0,
                "worst_trade": float(net.min()) if len(net) else 0.0,
            }
        )
    return rows


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    window_rows: list[dict] = []
    annual_rows: list[dict] = []
    for variant in VARIANTS:
        for profile in PROFILES:
            curve, closed = load_run(variant, profile)
            window_rows.extend(window_metrics(variant, profile, curve, closed))
            annual_rows.extend(annual_metrics(variant, profile, curve, closed))

    windows = pd.DataFrame(window_rows)
    annual = pd.DataFrame(annual_rows)
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_metrics.csv", index=False, encoding="utf-8-sig")

    key_profiles = windows[windows["profile"].isin(["cost30", "cost30_all_shock2"])].copy()
    annual_key = annual[annual["profile"].isin(["cost30", "cost30_all_shock2"])].copy()
    pct_cols = {"return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"}
    lines = [
        "# G3 V4 strong_main 仓位路径年度/窗口复核 v1",
        "",
        "## 窗口对照",
        "",
        md_table(key_profiles, pct_cols=pct_cols),
        "",
        "## 年度对照",
        "",
        md_table(annual_key, pct_cols=pct_cols),
        "",
        "## 观察重点",
        "",
        "- 看 `strong_daily_limit1` 是否在 train/valid/blind 都保留收益，而不是只改善 full。",
        "- 看 `strong_daily_limit1_q1q2_half` 是否过度牺牲 valid_2024_2025 与 blind_2026ytd 的进攻收益。",
        "- all-shock 只是压力测试，不作为调参目标单独优化；如果为了 all-shock 牺牲正常口径过大，应退回更轻的容量限制。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()
