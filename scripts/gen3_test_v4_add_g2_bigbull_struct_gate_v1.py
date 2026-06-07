from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table, simulate_scaled, summarize  # noqa: E402


G3_BASE = ROOT / "reports" / "gen3_v4_strong_entry_warning_early_exit_v1" / "base_093_candidates.csv"
BIGBULL_CONTEXT = ROOT / "reports" / "gen3_v4_bigbull_structural_context_v1" / "bigbull_context_enriched.csv"
OUT_DIR = ROOT / "reports" / "gen3_v4_add_g2_bigbull_struct_gate_v1"
COST_BPS = 30.0


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def load_g3_base() -> pd.DataFrame:
    d = pd.read_csv(G3_BASE, low_memory=False, encoding="utf-8-sig")
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["entry_price", "policy_net_ret", "score", "route_priority", "position_scale"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["position_scale"] = d.get("position_scale", 1.0).fillna(1.0)
    return d.dropna(subset=["entry_date", "policy_exit_date", "code", "entry_price", "policy_net_ret"]).copy()


def exit_date_map(start: pd.Timestamp, end: pd.Timestamp, hold_days: int = 5) -> dict[pd.Timestamp, pd.Timestamp]:
    calendar = _trade_calendar(start, end)
    out: dict[pd.Timestamp, pd.Timestamp] = {}
    for idx, day in enumerate(calendar):
        exit_idx = idx + hold_days - 1
        if exit_idx < len(calendar):
            out[day] = calendar[exit_idx]
    return out


def load_struct_gated_bigbull() -> pd.DataFrame:
    d = pd.read_csv(BIGBULL_CONTEXT, low_memory=False, encoding="utf-8-sig")
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in [
        "entry_price",
        "calc_ret_5d",
        "v4_score",
        "v4_rank",
        "breadth_ma20",
        "l3_rt_strong3_ratio",
        "rt_return_from_d1_close",
        "rt_breakout_vs_box_top",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    gate = (
        d["index_ge_ma20"].fillna(False).astype(bool)
        & d["breadth_ma20"].ge(0.50)
        & d["l3_rt_strong3_ratio"].ge(0.10)
        & d["rt_return_from_d1_close"].ge(0.06)
        & d["rt_breakout_vs_box_top"].ge(0.025)
        & d["rt_breakout_vs_box_top"].le(0.08)
    )
    d = d[gate].dropna(subset=["entry_date", "code", "entry_price", "calc_ret_5d"]).copy()
    exits = exit_date_map(d["entry_date"].min(), d["entry_date"].max() + pd.Timedelta(days=45), 5)
    out = pd.DataFrame(
        {
            "entry_date": d["entry_date"],
            "policy_exit_date": d["entry_date"].map(exits),
            "code": d["code"],
            "name": d["name"],
            "route": "strong_bigbull_struct_gate",
            "route_source": "g2_big_bull_struct_gate_index_breadth_sector_intraday_breakout",
            "route_priority": 2,
            "score": d["v4_score"].fillna(0.0),
            "entry_price": d["entry_price"],
            "policy_net_ret": d["calc_ret_5d"] - COST_BPS / 10000.0,
            "position_scale": 1.0,
            "scale_note": "full",
            "g2_v4_rank": d.get("v4_rank"),
            "breadth_ma20": d.get("breadth_ma20"),
            "l3_rt_strong3_ratio": d.get("l3_rt_strong3_ratio"),
            "rt_return_from_d1_close": d.get("rt_return_from_d1_close"),
            "rt_breakout_vs_box_top": d.get("rt_breakout_vs_box_top"),
        }
    )
    return out.dropna(subset=["policy_exit_date", "policy_net_ret"]).copy()


def scale_route(d: pd.DataFrame, scale: float) -> pd.DataFrame:
    out = d.copy()
    out["position_scale"] = float(scale)
    out["scale_note"] = f"struct_bigbull_scale_{scale:.2f}"
    return out


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
    variants = {
        "g3_base": g3,
        "g3_plus_struct_bigbull_full": pd.concat([g3, big], ignore_index=True, sort=False),
        "g3_plus_struct_bigbull_half": pd.concat([g3, scale_route(big, 0.50)], ignore_index=True, sort=False),
        "g3_plus_struct_bigbull_quarter": pd.concat([g3, scale_route(big, 0.25)], ignore_index=True, sort=False),
    }
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
            item = summarize(curve, closed, variant, profile)
            item["struct_bigbull_candidate_count"] = int(len(big))
            summaries.append(item)
            rs = route_summary(closed)
            if not rs.empty:
                rs.insert(0, "profile", profile)
                rs.insert(0, "variant", variant)
                route_frames.append(rs)
    summary = pd.DataFrame(summaries)
    route_df = pd.concat(route_frames, ignore_index=True) if route_frames else pd.DataFrame()
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    route_df.to_csv(OUT_DIR / "route_summary.csv", index=False, encoding="utf-8-sig")
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
        "# G3 V4 增加结构门槛 big_bull 轻仓复算 v1",
        "",
        "## 本轮完成",
        "",
        "- 只使用固定结构门槛，不使用 `score/rank` 过滤。",
        "- 门槛：指数站 MA20、市场广度 >= 50%、L3 板块强3 >= 10%、盘中涨幅 >= 6%、箱体突破 2.5%-8%。",
        "- 只测试它是否适合作为强势市场补充入口，不改 G3 原有弱势/震荡/强势主入口。",
        "",
        "## 组合复算汇总",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 路由贡献",
        "",
        md_table(route_df, pct_cols={"avg_ret", "win_rate"}),
        "",
        "## 下一步目标",
        "",
        "- 如果 quarter/half 在正常口径和 haircut2 下都比 base 更稳，再进入完整 30m 执行与跌停不可卖压力。",
        "- 如果正常收益略升但 haircut2 明显下降，说明该入口只能保留研究，不能急着接入 G3 V4 页面或实盘。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"done: {OUT_DIR}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
