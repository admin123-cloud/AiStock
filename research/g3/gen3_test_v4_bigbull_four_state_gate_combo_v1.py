from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402
from scripts.gen3_test_v4_add_g2_bigbull_struct_gate_v1 import COST_BPS, load_g3_base, load_struct_gated_bigbull, scale_route  # noqa: E402
from scripts.gen3_test_v4_bigbull_struct_d1_weak_reduce_combo_v1 import apply_bigbull_policy  # noqa: E402
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table, simulate_scaled, summarize  # noqa: E402


MARKET_CONTEXT = _report_path() / "gen3_four_path_independent_candidates" / "market_context.csv"
OUT_DIR = _report_path() / "gen3_v4_bigbull_four_state_gate_combo_v1"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def load_d1_market_context(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    ctx = pd.read_csv(MARKET_CONTEXT, low_memory=False, encoding="utf-8-sig")
    ctx["trade_date"] = pd.to_datetime(ctx["trade_date"], errors="coerce").dt.normalize()
    calendar = _trade_calendar(start - pd.Timedelta(days=20), end + pd.Timedelta(days=5))
    next_map = {calendar[i]: calendar[i + 1] for i in range(len(calendar) - 1)}
    ctx["entry_date"] = ctx["trade_date"].map(next_map)
    keep = [
        "entry_date",
        "trade_date",
        "market_style",
        "ma_skeleton",
        "volume_price_layer",
        "adx_layer",
        "breadth_ma20",
        "breadth_ma60",
        "up_rate",
        "big_down_rate",
        "limit_down_proxy_rate",
        "market_amount_ratio20",
        "adx20",
        "plus_di20",
        "minus_di20",
        "mom20",
    ]
    out = ctx[[c for c in keep if c in ctx.columns]].dropna(subset=["entry_date"]).copy()
    return out.rename(
        columns={
            "trade_date": "market_d1_date",
            "market_style": "market_style_d1",
            "ma_skeleton": "ma_skeleton_d1",
            "volume_price_layer": "volume_price_layer_d1",
            "adx_layer": "adx_layer_d1",
            "mom20": "index_mom20_d1",
        }
    )


def attach_four_state(big: pd.DataFrame) -> pd.DataFrame:
    ctx = load_d1_market_context(big["entry_date"].min(), big["entry_date"].max())
    out = big.merge(ctx, on="entry_date", how="left")
    return out


def filter_style(big: pd.DataFrame, style_gate: str) -> pd.DataFrame:
    d = attach_four_state(big)
    style = d["market_style_d1"].astype(str)
    if style_gate == "all_four_state":
        return d.copy()
    if style_gate == "standard_uptrend_only":
        return d[style.eq("standard_uptrend")].copy()
    if style_gate == "uptrend_or_weak_rebound":
        return d[style.isin(["standard_uptrend", "weak_rebound"])].copy()
    raise ValueError(style_gate)


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


def annual_summary(base_dir: Path, variants: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for variant in variants:
        for profile in ["30bps", "30bps_haircut2"]:
            p = base_dir / f"{variant}__{profile}" / "closed_trades.csv"
            if not p.exists():
                continue
            d = pd.read_csv(p, low_memory=False, encoding="utf-8-sig")
            d["entry_year"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.year
            for year, g in d.groupby("entry_year"):
                ret = pd.to_numeric(g["policy_net_ret"], errors="coerce")
                bg = g[g["route"].astype(str).eq("strong_bigbull_struct_gate")]
                rows.append(
                    {
                        "variant": variant,
                        "profile": profile,
                        "year": int(year),
                        "trade_count": int(len(g)),
                        "avg_ret": float(ret.mean()),
                        "win_rate": float((ret > 0).mean()),
                        "realized_pnl": float(pd.to_numeric(g["realized_pnl"], errors="coerce").sum()),
                        "bigbull_trades": int(len(bg)),
                        "bigbull_pnl": float(pd.to_numeric(bg.get("realized_pnl"), errors="coerce").sum()) if not bg.empty else 0.0,
                    }
                )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    g3 = load_g3_base()
    raw_big = load_struct_gated_bigbull()
    style_gates = ["all_four_state", "standard_uptrend_only", "uptrend_or_weak_rebound"]
    policies = ["hold5", "d1_weak_full_exit", "entry_fail_d1_weak_full_exit"]
    scales = {"quarter": 0.25, "half": 0.50}

    variants: dict[str, pd.DataFrame] = {"g3_base": g3}
    candidate_meta: list[dict[str, Any]] = []
    for style_gate in style_gates:
        styled = filter_style(raw_big, style_gate)
        styled.to_csv(OUT_DIR / f"bigbull_{style_gate}_candidates_raw.csv", index=False, encoding="utf-8-sig")
        style_counts = styled["market_style_d1"].value_counts(dropna=False).to_dict() if "market_style_d1" in styled.columns else {}
        for policy in policies:
            bp = apply_bigbull_policy(styled, policy)
            for scale_name, scale in scales.items():
                variant = f"g3_plus_bigbull_{style_gate}_{scale_name}_{policy}"
                variants[variant] = pd.concat([g3, scale_route(bp, scale)], ignore_index=True, sort=False)
                candidate_meta.append(
                    {
                        "variant": variant,
                        "style_gate": style_gate,
                        "policy": policy,
                        "scale": scale,
                        "bigbull_candidates": int(len(bp)),
                        "style_counts": str(style_counts),
                    }
                )

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
    meta = pd.DataFrame(candidate_meta)
    annual = annual_summary(OUT_DIR, summary["variant"].astype(str).unique().tolist())
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    route_df.to_csv(OUT_DIR / "route_summary.csv", index=False, encoding="utf-8-sig")
    meta.to_csv(OUT_DIR / "candidate_meta.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_contribution.csv", index=False, encoding="utf-8-sig")

    focus_names = [
        "g3_base",
        "g3_plus_bigbull_all_four_state_quarter_d1_weak_full_exit",
        "g3_plus_bigbull_standard_uptrend_only_quarter_d1_weak_full_exit",
        "g3_plus_bigbull_uptrend_or_weak_rebound_quarter_d1_weak_full_exit",
        "g3_plus_bigbull_standard_uptrend_only_half_d1_weak_full_exit",
    ]
    focus = summary[summary["variant"].astype(str).isin(focus_names)].copy()
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
        "# G3 V4 big_bull 四态门禁组合复算 v1",
        "",
        "## 本轮完成",
        "",
        "- 使用 `gen3_four_path_independent_candidates/market_context.csv` 的固定三层四态作为市场门禁。",
        "- 门禁使用 D-1 市场状态映射到 entry_date，不使用入场日收盘后的状态，避免未来函数。",
        "- 测试 `standard_uptrend_only` 与 `uptrend_or_weak_rebound`，并叠加 D1 10:30 弱确认全退保护。",
        "",
        "## 候选数量",
        "",
        md_table(meta),
        "",
        "## 重点组合对比",
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
        "## 年度贡献",
        "",
        md_table(annual, pct_cols={"avg_ret", "win_rate"}),
        "",
        "## 下一步目标",
        "",
        "- 如果 `standard_uptrend_only` 能压住 2021/2023 且压力口径优于 base，才进入真实 30m 执行重放。",
        "- 如果收益主要来自 2024/2026、且 2021/2023 仍拖累，则 big_bull 保留研究，不进入正式 G3。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"done: {OUT_DIR}")
    print(focus.to_string(index=False))


if __name__ == "__main__":
    main()
