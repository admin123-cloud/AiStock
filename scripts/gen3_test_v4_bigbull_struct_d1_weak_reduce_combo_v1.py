from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_v4_add_g2_bigbull_struct_gate_v1 import (  # noqa: E402
    COST_BPS,
    load_g3_base,
    load_struct_gated_bigbull,
    scale_route,
)
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table, simulate_scaled, summarize  # noqa: E402


AUDIT_PATH = ROOT / "reports" / "gen3_v4_bigbull_entry_fail_d1d2_weak_v1" / "entry_fail_d1d2_enriched.csv"
OUT_DIR = ROOT / "reports" / "gen3_v4_bigbull_struct_d1_weak_reduce_combo_v1"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def load_audit() -> pd.DataFrame:
    d = pd.read_csv(AUDIT_PATH, low_memory=False, encoding="utf-8-sig")
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["base_policy_ret", "d1_1030_exit_ret", "d2_1030_exit_ret"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    for col in ["d1_1030_weak", "entry_fail_plus_d1_weak", "d1d2_persistent_weak"]:
        if col in d.columns:
            d[col] = d[col].fillna(False).astype(bool)
    return d


def apply_bigbull_policy(big: pd.DataFrame, policy: str) -> pd.DataFrame:
    audit = load_audit()
    keep = [
        "entry_date",
        "code",
        "base_policy_ret",
        "d1_1030_exit_ret",
        "d2_1030_exit_ret",
        "d1_1030_weak",
        "entry_fail_plus_d1_weak",
        "d1d2_persistent_weak",
    ]
    d = big.merge(audit[[c for c in keep if c in audit.columns]], on=["entry_date", "code"], how="left")
    d["original_policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    base = pd.to_numeric(d["base_policy_ret"], errors="coerce").fillna(d["original_policy_net_ret"])
    d1_exit = pd.to_numeric(d["d1_1030_exit_ret"], errors="coerce").fillna(base)
    d2_exit = pd.to_numeric(d["d2_1030_exit_ret"], errors="coerce").fillna(base)
    d1_weak = d["d1_1030_weak"].fillna(False).astype(bool)
    entry_fail_d1_weak = d["entry_fail_plus_d1_weak"].fillna(False).astype(bool)
    d1d2_weak = d["d1d2_persistent_weak"].fillna(False).astype(bool)
    d["policy_variant"] = policy
    if policy == "hold5":
        d["policy_net_ret"] = base
    elif policy == "d1_weak_half_exit":
        d["policy_net_ret"] = base.where(~d1_weak, 0.5 * base + 0.5 * d1_exit)
    elif policy == "d1_weak_full_exit":
        d["policy_net_ret"] = base.where(~d1_weak, d1_exit)
    elif policy == "entry_fail_d1_weak_half_exit":
        d["policy_net_ret"] = base.where(~entry_fail_d1_weak, 0.5 * base + 0.5 * d1_exit)
    elif policy == "entry_fail_d1_weak_full_exit":
        d["policy_net_ret"] = base.where(~entry_fail_d1_weak, d1_exit)
    elif policy == "d1d2_persistent_half_exit":
        d["policy_net_ret"] = base.where(~d1d2_weak, 0.5 * base + 0.5 * d2_exit)
    else:
        raise ValueError(policy)
    d["reduce_triggered"] = False
    if "d1_weak" in policy:
        d["reduce_triggered"] = d1_weak if "entry_fail" not in policy else entry_fail_d1_weak
    if "d1d2" in policy:
        d["reduce_triggered"] = d1d2_weak
    d["route_source"] = d["route_source"].astype(str) + "__" + policy
    return d


def apply_profile(candidates: pd.DataFrame, name: str) -> pd.DataFrame:
    d = candidates.copy()
    d["stress_profile"] = name
    if name == "30bps":
        return d
    if name == "30bps_haircut2":
        d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce") - 0.02
        return d
    raise ValueError(name)


def route_summary(closed: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if closed.empty or "route" not in closed.columns:
        return pd.DataFrame()
    for route, g in closed.groupby("route", dropna=False):
        ret = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        rows.append(
            {
                "route": str(route),
                "trade_count": int(len(g)),
                "avg_ret": float(ret.mean()),
                "win_rate": float((ret > 0).mean()),
                "pnl": float(pd.to_numeric(g.get("realized_pnl"), errors="coerce").sum()) if "realized_pnl" in g.columns else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values("pnl", ascending=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    g3 = load_g3_base()
    big = load_struct_gated_bigbull()
    policies = [
        "hold5",
        "d1_weak_half_exit",
        "d1_weak_full_exit",
        "entry_fail_d1_weak_half_exit",
        "entry_fail_d1_weak_full_exit",
        "d1d2_persistent_half_exit",
    ]
    variants: dict[str, pd.DataFrame] = {"g3_base": g3}
    for policy in policies:
        bp = apply_bigbull_policy(big, policy)
        variants[f"g3_plus_struct_bigbull_quarter__{policy}"] = pd.concat([g3, scale_route(bp, 0.25)], ignore_index=True, sort=False)
        variants[f"g3_plus_struct_bigbull_half__{policy}"] = pd.concat([g3, scale_route(bp, 0.50)], ignore_index=True, sort=False)

    summaries: list[dict[str, Any]] = []
    route_frames: list[pd.DataFrame] = []
    for variant, candidates in variants.items():
        candidates.to_csv(OUT_DIR / f"{variant}_candidates.csv", index=False, encoding="utf-8-sig")
        for profile in ["30bps", "30bps_haircut2"]:
            prof = apply_profile(candidates, profile)
            curve, closed = simulate_scaled(prof, COST_BPS)
            run_dir = OUT_DIR / f"{variant}__{profile}"
            run_dir.mkdir(parents=True, exist_ok=True)
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summaries.append(summarize(curve, closed, variant, profile))
            rs = route_summary(closed)
            if not rs.empty:
                rs.insert(0, "profile", profile)
                rs.insert(0, "variant", variant)
                route_frames.append(rs)

    summary = pd.DataFrame(summaries)
    route_df = pd.concat(route_frames, ignore_index=True) if route_frames else pd.DataFrame()
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    route_df.to_csv(OUT_DIR / "route_summary.csv", index=False, encoding="utf-8-sig")

    focus = summary[
        summary["variant"].astype(str).str.contains("quarter")
        | summary["variant"].astype(str).eq("g3_base")
    ].copy()
    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
        "strong_main_avg_ret",
        "strong_bigbull_struct_gate_avg_ret",
        "avg_ret",
    }
    lines = [
        "# G3 V4 结构 big_bull + D1 弱确认减仓组合复算 v1",
        "",
        "## 本轮完成",
        "",
        "- 使用结构门槛 big_bull 作为强势补充入口。",
        "- 不使用 `score/rank` 过滤。",
        "- 触发 D1 10:30 弱时，测试半仓退出、全退，以及入场失败+D1弱的更窄触发。",
        "- 半仓退出采用保守混合收益：半仓按 10:30 退出，半仓继续 5日持有，不提前释放资金复用。",
        "",
        "## quarter 重点对比",
        "",
        md_table(focus, pct_cols=pct_cols),
        "",
        "## 全部组合汇总",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 路由贡献",
        "",
        md_table(route_df, pct_cols={"avg_ret", "win_rate"}),
        "",
        "## 下一步目标",
        "",
        "- 若 quarter + D1 弱半仓退出能同时改善正常口径和 haircut2，再进入真实 30m 执行重放。",
        "- 若仍只改善局部单笔，不改善组合压力口径，则 big_bull 暂时保留为研究入口，不进入 G3 V4 页面正式候选。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"done: {OUT_DIR}")
    print(focus.to_string(index=False))


if __name__ == "__main__":
    main()
