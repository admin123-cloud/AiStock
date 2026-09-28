from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_30m_volume_acceptance_v1 import (  # noqa: E402
    INITIAL_CAPITAL,
    PROFILES,
    exit_dates,
    simulate,
    summarize,
    window_metrics,
)
from scripts.gen3_test_range_box_stress_second_accept_d3_exit_v1 import attach_second_acceptance  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_range_box_structural_30m_accept_v1" / "box_first_panic_reclaim_30m_confirmed_one_per_day.csv"
OUT_DIR = ROOT / "reports" / "gen3_range_box_first_panic_distill_second_accept_v1"
SIGNAL_START = "2020-01-01"
SIGNAL_END = "2026-05-29"
CURVE_END = "2026-06-04"

VARIANTS = [
    {
        "variant": "first_panic_base_30m",
        "desc_cn": "箱体底部第一次恐慌修复，已有首个30m真实承接",
        "mode": "base",
    },
    {
        "variant": "first_panic_reclaim60",
        "desc_cn": "第一次恐慌修复，日线收盘修复>=60%",
        "mode": "reclaim60",
    },
    {
        "variant": "first_panic_pressure_release",
        "desc_cn": "第一次恐慌修复，市场压力释放：大跌比例在10%-35%之间",
        "mode": "pressure",
    },
    {
        "variant": "first_panic_second_accept",
        "desc_cn": "第一次恐慌修复，D0后半日或D1出现二次30m承接",
        "mode": "second_any",
    },
    {
        "variant": "first_panic_d1_second_accept",
        "desc_cn": "第一次恐慌修复，D1出现二次30m承接",
        "mode": "d1_second",
    },
    {
        "variant": "first_panic_reclaim60_second_accept",
        "desc_cn": "第一次恐慌修复，日线修复>=60%，且D0/D1二次30m承接",
        "mode": "reclaim60_second",
    },
    {
        "variant": "first_panic_pressure_reclaim60",
        "desc_cn": "第一次恐慌修复，市场压力释放，且日线修复>=60%",
        "mode": "pressure_reclaim60",
    },
    {
        "variant": "first_panic_pressure_reclaim60_second_accept",
        "desc_cn": "第一次恐慌修复，市场压力释放，日线修复>=60%，且D0/D1二次30m承接",
        "mode": "pressure_reclaim60_second",
    },
    {
        "variant": "first_panic_pressure_reclaim60_d1_second",
        "desc_cn": "第一次恐慌修复，市场压力释放，日线修复>=60%，且D1二次30m承接",
        "mode": "pressure_reclaim60_d1",
    },
]

POLICIES = [
    {"policy": "h5", "desc_cn": "固定持有到D5"},
    {"policy": "d3neg", "desc_cn": "若D3仍为负则D3退出，否则D5退出"},
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value) * 100:+.2f}%"


def money(value: Any) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value):,.0f}"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, money_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    money_cols = money_cols or set()
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        item: dict[str, Any] = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif col in money_cols:
                item[col] = money(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_base() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d["code"] = d["code"].astype(str)
    for col in [
        "range_pos60",
        "drawdown5",
        "drawdown10",
        "close_position",
        "amount_ratio20",
        "big_down_rate",
        "up_rate",
        "breadth_ma20",
        "bar_close_pos",
        "bar_ret",
        "amount_ratio3",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
        "entry_price_adjusted",
        "entry_price",
        "struct_tiebreak",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["hold_days"] = 5
    d["rank_key"] = pd.to_numeric(d.get("struct_tiebreak", 0.0), errors="coerce").fillna(0.0)
    return d.dropna(
        subset=["entry_date", "code", "confirm_datetime", "entry_price_adjusted", "fwd_ret_confirm_to_close_3d", "fwd_ret_confirm_to_close_5d"]
    ).copy()


def select_variant(base: pd.DataFrame, spec: dict[str, str]) -> pd.DataFrame:
    reclaim60 = base["close_position"].ge(0.60)
    pressure = base["big_down_rate"].between(0.10, 0.35, inclusive="left")
    second_any = base["d0_second_accept"] | base["d1_second_accept"]
    d1_second = base["d1_second_accept"]
    mode = spec["mode"]
    if mode == "base":
        mask = pd.Series(True, index=base.index)
    elif mode == "reclaim60":
        mask = reclaim60
    elif mode == "pressure":
        mask = pressure
    elif mode == "second_any":
        mask = second_any
    elif mode == "d1_second":
        mask = d1_second
    elif mode == "reclaim60_second":
        mask = reclaim60 & second_any
    elif mode == "pressure_reclaim60":
        mask = pressure & reclaim60
    elif mode == "pressure_reclaim60_second":
        mask = pressure & reclaim60 & second_any
    elif mode == "pressure_reclaim60_d1":
        mask = pressure & reclaim60 & d1_second
    else:
        raise ValueError(mode)
    d = base[mask].copy()
    d["variant_base"] = spec["variant"]
    d["variant_base_desc"] = spec["desc_cn"]
    return d


def standardize_policy(signals: pd.DataFrame, profile: dict[str, Any], variant: str, desc: str, policy: str) -> pd.DataFrame:
    if signals.empty:
        return signals.copy()
    d = signals.copy()
    d["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    ret3 = pd.to_numeric(d["fwd_ret_confirm_to_close_3d"], errors="coerce")
    ret5 = pd.to_numeric(d["fwd_ret_confirm_to_close_5d"], errors="coerce")
    cost = float(profile["cost_bps"]) / 10000.0
    shock = float(profile["shock"])
    if policy == "h5":
        d["policy_net_ret"] = ret5 - cost - shock
        d["exit_reason"] = "hold_d5"
        d["policy_hold_days"] = 5
    elif policy == "d3neg":
        early = ret3.lt(0.0)
        d["policy_net_ret"] = ret5.where(~early, ret3) - cost - shock
        d["exit_reason"] = early.map({True: "d3_negative_exit", False: "hold_d5"})
        d["policy_hold_days"] = early.map({True: 3, False: 5}).astype(int)
    else:
        raise ValueError(policy)
    maps = {
        3: exit_dates(d["entry_date_ts"], 3),
        5: exit_dates(d["entry_date_ts"], 5),
    }
    d["policy_exit_date"] = [maps[int(h)].get(pd.Timestamp(day).normalize()) for day, h in zip(d["entry_date_ts"], d["policy_hold_days"])]
    d["entry_price_used"] = pd.to_numeric(d["entry_price_adjusted"], errors="coerce")
    d["variant"] = variant
    d["desc"] = desc
    return d.dropna(subset=["entry_date_ts", "policy_exit_date", "policy_net_ret", "entry_price_used"]).copy()


def coverage(base: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for spec in VARIANTS:
        d = select_variant(base, spec)
        raw5 = pd.to_numeric(d.get("fwd_ret_confirm_to_close_5d", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "variant_base": spec["variant"],
                "desc": spec["desc_cn"],
                "signal_count": int(len(d)),
                "signal_days": int(d["entry_date"].nunique()) if len(d) else 0,
                "d0_second": int(d["d0_second_accept"].sum()) if len(d) else 0,
                "d1_second": int(d["d1_second_accept"].sum()) if len(d) else 0,
                "raw_avg_5d": float(raw5.mean()) if len(raw5) else 0.0,
                "raw_win_5d": float((raw5 > 0).mean()) if len(raw5) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def group_year(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["year"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.year
    rows = []
    for year, g in d.groupby("year"):
        ret = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        rows.append(
            {
                "year": int(year),
                "trade_count": int(len(g)),
                "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
                "avg_return": float(ret.mean()) if len(ret) else 0.0,
                "worst_return": float(ret.min()) if len(ret) else 0.0,
                "best_return": float(ret.max()) if len(ret) else 0.0,
                "pnl": float(pd.to_numeric(g.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum()),
            }
        )
    return pd.DataFrame(rows)


def concentration(df: pd.DataFrame) -> pd.DataFrame:
    d = df.sort_values("realized_pnl", ascending=False).copy()
    total = float(pd.to_numeric(d["realized_pnl"], errors="coerce").sum())
    rows = []
    for n in [0, 1, 3, 5, 10]:
        rest = d.iloc[n:].copy()
        ret = pd.to_numeric(rest["policy_net_ret"], errors="coerce")
        pnl = float(pd.to_numeric(rest.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum())
        rows.append(
            {
                "exclude_top_n": n,
                "remaining_trades": int(len(rest)),
                "remaining_pnl": pnl,
                "remaining_pnl_share": pnl / total if total else 0.0,
                "remaining_avg_return": float(ret.mean()) if len(ret) else 0.0,
                "remaining_win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def compare_to_reference(summary: pd.DataFrame) -> pd.DataFrame:
    ref_path = ROOT / "reports" / "gen3_range_box_stress_second_accept_d3_exit_v1" / "summary.csv"
    ref = pd.read_csv(ref_path, low_memory=False)
    keep = ref[ref["variant"].isin(["box_stress_second_accept_h5", "box_stress_reclaim60_second_accept_d3neg"])].copy()
    keep["source_group"] = "上一轮box_stress参考"
    ours = summary.copy()
    ours["source_group"] = "本轮first_panic蒸馏"
    cols = ["source_group", "variant", "desc", "profile", "trade_count", "total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade"]
    return pd.concat([keep[cols], ours[cols]], ignore_index=True)


def write_report(
    cov: pd.DataFrame,
    summary: pd.DataFrame,
    windows: pd.DataFrame,
    comparison: pd.DataFrame,
    best_variant: str,
    best_desc: str,
    yearly: pd.DataFrame,
    conc: pd.DataFrame,
) -> None:
    pct_cols = {
        "raw_avg_5d",
        "raw_win_5d",
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "return",
        "avg_return",
        "worst_return",
        "best_return",
        "remaining_pnl_share",
        "remaining_avg_return",
        "remaining_win_rate",
    }
    best_summary = summary[summary["variant"].eq(best_variant)].copy()
    best_windows = windows[windows["variant"].eq(best_variant)].copy()
    lines = [
        "# G3 v4 箱体第一次恐慌源蒸馏：压力释放 + 日线修复 + 二次承接",
        "",
        "## 回测范围",
        f"- 信号入场窗口：{SIGNAL_START} 至 {SIGNAL_END}。",
        f"- 曲线结算窗口：{SIGNAL_START} 至 {CURVE_END}。",
        "- 初始资金：150000；5个槽位；单槽20%；同日最多开1笔。",
        "- 本轮对象是上一轮宽结构源中已完成首个30m真实承接的369笔，不新增score/rank过滤，只补充结构条件。",
        "",
        "## 英文策略名解释",
        "- `first_panic`：第一次恐慌修复。指箱体底部第一次急跌后出现修复，不是二次回踩。",
        "- `pressure_release`：市场压力释放。指信号日市场大跌比例在10%-35%之间，代表有一定出清，但不是极端崩盘。",
        "- `reclaim60`：日线修复>=60%。指信号日收盘位于当日振幅区间60%以上。",
        "- `second_accept`：二次30m承接。首个30m买入确认后，D0后半日或D1再次出现非阴、收高位、放量的30m K线。",
        "- `d1_second_accept`：只看D1二次承接。比`second_accept`更强调次日仍有资金承接。",
        "- `d3neg`：D3弱退出。若到第3个交易日仍为负收益，则D3退出，否则D5退出。",
        "",
        "## 覆盖率",
        md_table(cov, pct_cols=pct_cols),
        "",
        "## 与上一轮强样本对比",
        md_table(comparison, pct_cols=pct_cols),
        "",
        "## 本轮slot复算",
        md_table(summary, pct_cols=pct_cols),
        "",
        f"## 当前相对最好方案：`{best_variant}`",
        f"- 中文含义：{best_desc}",
        md_table(best_summary, pct_cols=pct_cols),
        "",
        "## 最好方案分窗口",
        md_table(best_windows, pct_cols=pct_cols),
        "",
        "## 最好方案年度拆分（cost30）",
        md_table(yearly, pct_cols=pct_cols, money_cols={"pnl"}),
        "",
        "## 最好方案集中度（cost30）",
        md_table(conc, pct_cols=pct_cols, money_cols={"remaining_pnl"}),
        "",
        "## 判断规则",
        "- 如果压力释放 + 修复 + 二次承接仍不能接近上一轮box_stress参考，说明上一轮优势不只是这些表层条件，候选源还缺少更精确的压力结构。",
        "- 如果样本数过低或验证段/压力口径失败，只保留为研究标签，不进入G3实盘。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = attach_second_acceptance(load_base())
    base.to_csv(OUT_DIR / "base_with_second_acceptance_flags.csv", index=False, encoding="utf-8-sig")
    cov = coverage(base)
    cov.to_csv(OUT_DIR / "coverage.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    closed_map: dict[tuple[str, str], pd.DataFrame] = {}
    best_variant = ""
    best_desc = ""
    best_key: tuple[float, float, int] | None = None

    for spec in VARIANTS:
        selected = select_variant(base, spec)
        selected.to_csv(OUT_DIR / f"{spec['variant']}_signals.csv", index=False, encoding="utf-8-sig")
        for policy in POLICIES:
            variant = f"{spec['variant']}__{policy['policy']}"
            desc = f"{spec['desc_cn']}；{policy['desc_cn']}"
            for profile in PROFILES:
                candidates = standardize_policy(selected, profile, variant, desc, policy["policy"])
                curve, closed = simulate(candidates)
                run_dir = OUT_DIR / f"{variant}__{profile['profile']}"
                run_dir.mkdir(parents=True, exist_ok=True)
                candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
                curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
                closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
                closed_map[(variant, str(profile["profile"]))] = closed
                summary_rows.append(summarize(curve, closed, variant, str(profile["profile"]), desc))
                window_rows.extend(window_metrics(curve, closed, variant, str(profile["profile"])))
            cost30 = [r for r in summary_rows if r.get("variant") == variant and r.get("profile") == "cost30"][-1]
            if int(cost30.get("trade_count", 0)) >= 20:
                key = (
                    float(cost30.get("total_return", 0.0)),
                    -abs(float(cost30.get("max_drawdown", 0.0))),
                    int(cost30.get("trade_count", 0)),
                )
                if best_key is None or key > best_key:
                    best_key = key
                    best_variant = variant
                    best_desc = desc

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    comparison = compare_to_reference(summary)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    comparison.to_csv(OUT_DIR / "comparison_to_box_stress_reference.csv", index=False, encoding="utf-8-sig")

    if not best_variant and len(summary):
        row = summary[summary["profile"].eq("cost30")].sort_values("total_return", ascending=False).head(1).iloc[0]
        best_variant = str(row["variant"])
        best_desc = str(row["desc"])

    best_closed = closed_map.get((best_variant, "cost30"), pd.DataFrame())
    yearly = group_year(best_closed)
    conc = concentration(best_closed)
    yearly.to_csv(OUT_DIR / "best_yearly_cost30.csv", index=False, encoding="utf-8-sig")
    conc.to_csv(OUT_DIR / "best_concentration_cost30.csv", index=False, encoding="utf-8-sig")

    keep_cols = [
        "entry_date",
        "policy_exit_date",
        "code",
        "name",
        "exit_reason",
        "policy_net_ret",
        "realized_pnl",
        "range_pos60",
        "drawdown5",
        "drawdown10",
        "close_position",
        "big_down_rate",
        "up_rate",
        "amount_ratio20",
        "bar_time",
        "bar_close_pos",
        "bar_ret",
        "amount_ratio3",
        "d0_second_accept",
        "d1_second_accept",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
    ]
    keep_cols = [c for c in keep_cols if c in best_closed.columns]
    best_closed.sort_values("realized_pnl", ascending=False).head(20)[keep_cols].to_csv(
        OUT_DIR / "best_top_trades_cost30.csv", index=False, encoding="utf-8-sig"
    )
    best_closed.sort_values("policy_net_ret").head(20)[keep_cols].to_csv(
        OUT_DIR / "best_worst_trades_cost30.csv", index=False, encoding="utf-8-sig"
    )
    valid = best_closed[
        pd.to_datetime(best_closed.get("entry_date", pd.Series(dtype=str)), errors="coerce").between(
            pd.Timestamp("2024-01-01"), pd.Timestamp("2025-12-31")
        )
    ].copy()
    valid.sort_values("entry_date")[keep_cols].to_csv(OUT_DIR / "best_valid_2024_2025_trades_cost30.csv", index=False, encoding="utf-8-sig")
    write_report(cov, summary, windows, comparison, best_variant, best_desc, yearly, conc)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
