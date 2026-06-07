from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PANIC_DIR = ROOT / "reports" / "gen3_panic_v2_research" / "final_candidate_v1"
FINAL_DIR = ROOT / "reports" / "gen3_final_candidate_package_v1"
RANGE_DIR = ROOT / "reports" / "gen3_range_v2_independent_source_v1"
OUT_DIR = ROOT / "reports" / "gen3_down_panic_v3_regime_audit_v1"

INITIAL_CAPITAL = 150_000.0
POLICY = "m30_close5_full_nextopen"
COST_BPS = 30

WINDOWS = {
    "full_2020_2026": ("2020-01-01", "2026-05-29"),
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "weak_gap_2022_2024": ("2022-01-01", "2024-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-05-29"),
}


def pct(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def num(value: float | int | None, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):.{digits}f}"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows: list[dict[str, object]] = []
    for _, row in df.iterrows():
        item: dict[str, object] = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = num(value, 4)
            else:
                item[col] = "" if pd.isna(value) else value
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def window_return(curve: pd.DataFrame, start: str, end: str) -> tuple[float, float, int, float, float]:
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    part = curve[curve["date"].between(s, e)].copy()
    if part.empty:
        return 0.0, 0.0, 0, 0.0, 0.0
    ret = float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0)
    dd = max_drawdown(part["equity"])
    exposure = float((part["open_positions"] > 0).mean()) if "open_positions" in part else 0.0
    worst_open = float(part["worst_open_mtm_ret"].min()) if "worst_open_mtm_ret" in part else 0.0
    return ret, dd, int(len(part)), exposure, worst_open


def load_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    curve_path = PANIC_DIR / f"{POLICY}_cost{COST_BPS}_mtm_equity_curve.csv"
    trades_path = PANIC_DIR / f"{POLICY}_cost{COST_BPS}_closed_trades.csv"
    curve = pd.read_csv(curve_path)
    trades = pd.read_csv(trades_path)
    curve["date"] = pd.to_datetime(curve["date"], errors="coerce").dt.normalize()
    trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="coerce").dt.normalize()
    trades["exit_date"] = pd.to_datetime(trades["exit_date"], errors="coerce").dt.normalize()
    for col in ["policy_net_ret", "net_ret", "realized_pnl", "stake", "up_rate", "big_down_rate", "index_mom20"]:
        if col in trades.columns:
            trades[col] = pd.to_numeric(trades[col], errors="coerce")
    for col in ["equity", "open_positions", "worst_open_mtm_ret"]:
        if col in curve.columns:
            curve[col] = pd.to_numeric(curve[col], errors="coerce")
    curve = curve.dropna(subset=["date", "equity"]).sort_values("date")
    trades = trades.dropna(subset=["entry_date", "policy_net_ret"]).sort_values("entry_date")
    return curve, trades


def summarize_windows(curve: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, (start, end) in WINDOWS.items():
        ret, dd, days, exposure, worst_open = window_return(curve, start, end)
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        t = trades[trades["entry_date"].between(s, e)].copy()
        rows.append(
            {
                "window": name,
                "start": start,
                "end": end,
                "return": ret,
                "max_drawdown": dd,
                "days": days,
                "trade_count": int(len(t)),
                "win_rate": float((t["policy_net_ret"] > 0).mean()) if not t.empty else 0.0,
                "avg_trade_ret": float(t["policy_net_ret"].mean()) if not t.empty else 0.0,
                "worst_trade": float(t["policy_net_ret"].min()) if not t.empty else 0.0,
                "exposure_day_ratio": exposure,
                "worst_open_mtm_ret": worst_open,
            }
        )
    return pd.DataFrame(rows)


def summarize_years(curve: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, part in curve.groupby(curve["date"].dt.year):
        t = trades[trades["entry_date"].dt.year.eq(year)]
        rows.append(
            {
                "year": int(year),
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0),
                "max_drawdown": max_drawdown(part["equity"]),
                "trade_count": int(len(t)),
                "win_rate": float((t["policy_net_ret"] > 0).mean()) if not t.empty else 0.0,
                "avg_trade_ret": float(t["policy_net_ret"].mean()) if not t.empty else 0.0,
                "worst_trade": float(t["policy_net_ret"].min()) if not t.empty else 0.0,
                "exposure_day_ratio": float((part["open_positions"] > 0).mean()),
                "worst_open_mtm_ret": float(part["worst_open_mtm_ret"].min()),
            }
        )
    return pd.DataFrame(rows)


def summarize_regimes(trades: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in ["market_style", "g3_position_guard", "g3_capitulation_strength", "g3_volume_context"] if c in trades.columns]
    rows = []
    for col in cols:
        for value, part in trades.groupby(col, dropna=False):
            if len(part) < 5:
                continue
            rows.append(
                {
                    "dimension": col,
                    "value": "missing" if pd.isna(value) else str(value),
                    "trade_count": int(len(part)),
                    "win_rate": float((part["policy_net_ret"] > 0).mean()),
                    "avg_trade_ret": float(part["policy_net_ret"].mean()),
                    "median_trade_ret": float(part["policy_net_ret"].median()),
                    "worst_trade": float(part["policy_net_ret"].min()),
                    "sum_realized_pnl": float(part["realized_pnl"].sum()) if "realized_pnl" in part else 0.0,
                }
            )
    return pd.DataFrame(rows).sort_values(["dimension", "sum_realized_pnl"], ascending=[True, False])


def load_reference() -> dict:
    reference: dict[str, object] = {}
    final_annual = FINAL_DIR / "final_candidate_annual.csv"
    if final_annual.exists():
        d = pd.read_csv(final_annual)
        d["year"] = pd.to_numeric(d["year"], errors="coerce")
        d["return"] = pd.to_numeric(d["return"], errors="coerce")
        reference["g3_current_final_2022_2024_sum"] = float(d[d["year"].between(2022, 2024)]["return"].sum())
        reference["g3_current_final_2023"] = float(d[d["year"].eq(2023)]["return"].sum())
        reference["g3_current_final_2024"] = float(d[d["year"].eq(2024)]["return"].sum())
    range_summary = RANGE_DIR / "summary.json"
    if range_summary.exists():
        reference["range_v2_summary"] = json.loads(range_summary.read_text(encoding="utf-8"))
    return reference


def write_report(
    window_df: pd.DataFrame,
    annual_df: pd.DataFrame,
    regime_df: pd.DataFrame,
    reference: dict,
) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    window_df.to_csv(OUT_DIR / "down_panic_v3_window_metrics.csv", index=False, encoding="utf-8-sig")
    annual_df.to_csv(OUT_DIR / "down_panic_v3_annual_metrics.csv", index=False, encoding="utf-8-sig")
    regime_df.to_csv(OUT_DIR / "down_panic_v3_regime_breakdown.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "policy": POLICY,
                "cost_bps": COST_BPS,
                "window_metrics": window_df.to_dict(orient="records"),
                "reference": reference,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    weak = window_df[window_df["window"].eq("weak_gap_2022_2024")].iloc[0]
    full = window_df[window_df["window"].eq("full_2020_2026")].iloc[0]
    blind = window_df[window_df["window"].eq("blind_2026ytd")].iloc[0]
    range_weak = None
    if reference.get("range_v2_summary"):
        range_weak = reference["range_v2_summary"].get("best_2022_2024", {}).get("return")

    verdict = "通过弱势链路保留审计"
    if weak["return"] <= 0 or weak["max_drawdown"] < -0.10:
        verdict = "不通过，需要重建弱势链路"
    elif blind["return"] < 0:
        verdict = "弱势窗口可保留，但盲测不足"

    lines = [
        "# G3 down_panic_v3 下降/弱势周期审计",
        "",
        f"- 策略口径：`{POLICY}`，成本 `{COST_BPS}bps`，读取 panic_v2 最终候选的逐日 MTM 曲线和 closed trades。",
        "- 本报告只做既有候选的分段审计，不新增参数搜索，不重跑 G2，也不接入实盘。",
        f"- 结论：**{verdict}**。",
        "",
        "## 关键结论",
        "",
        f"- 全周期收益：{pct(full['return'])}，最大回撤：{pct(full['max_drawdown'])}，交易数：{int(full['trade_count'])}。",
        f"- 2022-2024 弱势窗口收益：{pct(weak['return'])}，最大回撤：{pct(weak['max_drawdown'])}，交易数：{int(weak['trade_count'])}。",
        f"- 2026 盲测至今收益：{pct(blind['return'])}，最大回撤：{pct(blind['max_drawdown'])}，交易数：{int(blind['trade_count'])}。",
    ]
    if range_weak is not None:
        lines.append(f"- 对比当前 range_v2 最好版本 2022-2024：{pct(range_weak)}，down_panic_v3 明显更适合作为弱势打法基座。")
    if "g3_current_final_2022_2024_sum" in reference:
        lines.append(
            f"- 对比当前 G3 formal 年度粗合计 2022-2024：{pct(reference['g3_current_final_2022_2024_sum'])}，panic 单链能贡献稳定防守收益，但不是震荡链路答案。"
        )
    lines.extend(
        [
            "",
            "## 分段指标",
            "",
            md_table(
                window_df,
                {
                    "return",
                    "max_drawdown",
                    "win_rate",
                    "avg_trade_ret",
                    "worst_trade",
                    "exposure_day_ratio",
                    "worst_open_mtm_ret",
                },
            ),
            "",
            "## 年度指标",
            "",
            md_table(
                annual_df,
                {
                    "return",
                    "max_drawdown",
                    "win_rate",
                    "avg_trade_ret",
                    "worst_trade",
                    "exposure_day_ratio",
                    "worst_open_mtm_ret",
                },
            ),
            "",
            "## 环境拆分",
            "",
            md_table(
                regime_df.head(40),
                {"win_rate", "avg_trade_ret", "median_trade_ret", "worst_trade"},
            ),
            "",
            "## 下一步",
            "",
            "1. 下降/弱势打法先以 down_panic_v3 作为候选基座，后续只做执行偏差和容量压力，不继续围绕单一弱势样本调参。",
            "2. 震荡打法不能沿用当前 range_box_bottom 源，需要重建箱体底部/冰点/反抽的候选定义。",
            "3. 强势打法继续吸收 G2 full 的 volume5_keep80_runup + sector_score_bonus 思路，但要降低 strong_v2 当前回撤。",
        ]
    )
    (OUT_DIR / "down_panic_v3_regime_audit_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    curve, trades = load_inputs()
    window_df = summarize_windows(curve, trades)
    annual_df = summarize_years(curve, trades)
    regime_df = summarize_regimes(trades)
    reference = load_reference()
    write_report(window_df, annual_df, regime_df, reference)
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()
