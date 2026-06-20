from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_style_template_strategy_v1 import _trade_calendar  # noqa: E402
from scripts.gen3_market_mode_router_v1 import metrics, simulate, window_metrics  # noqa: E402
from utils.paths import report_path  # noqa: E402


SRC_DIR = report_path("gen3_market_mode_router_v1")
OUT_DIR = report_path("gen3_market_state_router_v1")
TRADE_LIBRARY = SRC_DIR / "mode_trade_library.csv"
MARKET_CONTEXT = report_path("gen3_four_path_independent_candidates", "market_context.csv")

MODE_PRIORITY = [
    "institutional_mainwave",
    "panic_repair",
    "strong_volume5",
    "range_weak_rebound",
    "old_g3_route_v3",
    "old_g3_guarded",
]


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return ""
    if not np.isfinite(x):
        return ""
    return f"{x:.2%}"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, max_rows: int = 80) -> str:
    if df.empty:
        return "_无数据_"
    d = df.head(max_rows).copy()
    for col in pct_cols or set():
        if col in d.columns:
            d[col] = d[col].map(_pct)
    return d.to_markdown(index=False)


def _safe_num(row: pd.Series, key: str, default: float = 0.0) -> float:
    value = row.get(key, default)
    try:
        x = float(value)
    except Exception:
        return default
    return x if np.isfinite(x) else default


def load_trade_library() -> pd.DataFrame:
    if not TRADE_LIBRARY.exists():
        raise FileNotFoundError(f"missing trade library: {TRADE_LIBRARY}")
    trades = pd.read_csv(TRADE_LIBRARY, low_memory=False, encoding="utf-8-sig")
    trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="coerce").dt.normalize()
    trades["policy_exit_date"] = pd.to_datetime(trades["policy_exit_date"], errors="coerce").dt.normalize()
    for col in ["score", "net_ret", "index_mom20", "index_mom60", "up_rate", "big_down_rate"]:
        if col in trades.columns:
            trades[col] = pd.to_numeric(trades[col], errors="coerce")
    trades = trades.dropna(subset=["entry_date", "policy_exit_date", "net_ret"]).copy()
    trades["mode_rank"] = trades["mode"].map({m: i for i, m in enumerate(MODE_PRIORITY)}).fillna(99)
    return trades


def load_market_context() -> pd.DataFrame:
    if not MARKET_CONTEXT.exists():
        raise FileNotFoundError(f"missing market context: {MARKET_CONTEXT}")
    mc = pd.read_csv(MARKET_CONTEXT, low_memory=False)
    mc["context_date"] = pd.to_datetime(mc["trade_date"], errors="coerce").dt.normalize()
    keep = [
        "context_date",
        "market_style",
        "ma_skeleton",
        "volume_price_layer",
        "adx_layer",
        "up_rate",
        "big_down_rate",
        "limit_down_proxy_rate",
        "mom20",
        "mom60",
        "index_amount_ratio20",
        "turnover20_vs_60",
        "breadth_ma20",
        "breadth_ma60",
        "median_mom20",
    ]
    mc = mc[[c for c in keep if c in mc.columns]].dropna(subset=["context_date"]).copy()
    for col in mc.columns:
        if col not in {"context_date", "market_style", "ma_skeleton", "volume_price_layer", "adx_layer"}:
            mc[col] = pd.to_numeric(mc[col], errors="coerce")
    return mc.sort_values("context_date").reset_index(drop=True)


def attach_previous_context(trades: pd.DataFrame, mc: pd.DataFrame) -> pd.DataFrame:
    left = trades.sort_values("entry_date").reset_index(drop=True)
    right = mc.sort_values("context_date").reset_index(drop=True)
    out = pd.merge_asof(
        left,
        right,
        left_on="entry_date",
        right_on="context_date",
        direction="backward",
        allow_exact_matches=False,
        suffixes=("", "_prev"),
    )
    out["decision_date"] = out["context_date"]
    return out


def eligible_modes_for_state(row: pd.Series, available: set[str]) -> tuple[list[str], str]:
    style = str(row.get("market_style_prev") or row.get("market_style") or "")
    up_rate = _safe_num(row, "up_rate_prev", _safe_num(row, "up_rate", 0.0))
    big_down = _safe_num(row, "big_down_rate_prev", _safe_num(row, "big_down_rate", 0.0))
    limit_down = _safe_num(row, "limit_down_proxy_rate", 0.0)

    panic = big_down >= 0.15 or limit_down >= 0.03 or (style == "standard_downtrend" and up_rate <= 0.35)

    if panic and "panic_repair" in available:
        return ["panic_repair"], "前日恐慌扩散，优先做恐慌修复"
    if "institutional_mainwave" in available:
        return ["institutional_mainwave"], "机构主升候选存在，保留当前大波段收益模式"
    if "old_g3_route_v3" in available:
        return ["old_g3_route_v3"], "无机构主升，回到跨周期旧G3路由"

    # 低吸和强势补位暂不进入当前路由：样本显示它们容易稀释 2022/2026 的收益。
    return [], "无可交易模式"


def state_router(enriched: pd.DataFrame, calendar: list[pd.Timestamp]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    by_day = {day: g.copy() for day, g in enriched.groupby("entry_date")}
    selected_rows: list[pd.DataFrame] = []
    all_candidate_rows: list[pd.DataFrame] = []
    daily_candidate_rows: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    for day in calendar:
        todays = by_day.get(day)
        if todays is None or todays.empty:
            decisions.append({"date": day.strftime("%Y-%m-%d"), "selected_mode": "", "opened": 0, "reason": "no_candidate"})
            continue

        context_row = todays.iloc[0]
        available = set(todays["mode"].astype(str))
        modes, reason = eligible_modes_for_state(context_row, available)
        picked = pd.DataFrame()
        selected_mode = ""
        selected_trade_key = ""
        day_candidates = todays.copy()
        day_candidates["router_date"] = day.strftime("%Y-%m-%d")
        day_candidates["router_available_modes"] = ",".join(sorted(available))
        day_candidates["router_eligible_modes"] = ",".join(modes)
        day_candidates["router_reason"] = reason
        day_candidates["router_eligible"] = False
        day_candidates["router_candidate_rank"] = pd.NA
        day_candidates["router_selected"] = False
        day_candidates["router_block_reason"] = "mode_not_selected_by_state_router"
        if modes:
            ranked = todays[todays["mode"].isin(modes)].copy()
            if not ranked.empty:
                ranked["mode_pick_rank"] = ranked["mode"].map({m: i for i, m in enumerate(modes)}).fillna(99)
                ranked = ranked.sort_values(["mode_pick_rank", "score", "code"], ascending=[True, False, True]).copy()
                ranked["router_candidate_rank"] = range(1, len(ranked) + 1)
                picked = ranked.head(1)
                selected_mode = str(picked.iloc[0]["mode"])
                selected_trade_key = str(picked.iloc[0].get("trade_key") or "")
                selected_rows.append(picked)
                rank_map = ranked.set_index("trade_key")["router_candidate_rank"].to_dict() if "trade_key" in ranked.columns else {}
                eligible_keys = set(ranked.get("trade_key", pd.Series(dtype=str)).astype(str))
                day_candidates["router_eligible"] = day_candidates.get("trade_key", pd.Series("", index=day_candidates.index)).astype(str).isin(eligible_keys)
                day_candidates["router_candidate_rank"] = day_candidates.get("trade_key", pd.Series("", index=day_candidates.index)).astype(str).map(rank_map)
                day_candidates.loc[day_candidates["router_eligible"], "router_block_reason"] = "ranked_but_not_top_pick"
                if selected_trade_key:
                    selected_mask = day_candidates.get("trade_key", pd.Series("", index=day_candidates.index)).astype(str).eq(selected_trade_key)
                    day_candidates.loc[selected_mask, "router_selected"] = True
                    day_candidates.loc[selected_mask, "router_block_reason"] = ""
        elif not modes:
            day_candidates["router_block_reason"] = "no_eligible_mode_for_state"
        all_candidate_rows.append(day_candidates)
        daily_candidate_rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "decision_date": "" if pd.isna(context_row.get("decision_date")) else pd.Timestamp(context_row.get("decision_date")).strftime("%Y-%m-%d"),
                "candidate_count": int(len(todays)),
                "router_eligible_count": int(day_candidates["router_eligible"].fillna(False).astype(bool).sum()),
                "selected_count": int(day_candidates["router_selected"].fillna(False).astype(bool).sum()),
                "selected_code": "" if picked.empty else str(picked.iloc[0].get("code") or ""),
                "selected_name": "" if picked.empty else str(picked.iloc[0].get("name") or ""),
                "selected_mode": selected_mode,
                "available_modes": ",".join(sorted(available)),
                "eligible_modes": ",".join(modes),
                "reason": reason,
            }
        )

        decisions.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "decision_date": "" if pd.isna(context_row.get("decision_date")) else pd.Timestamp(context_row.get("decision_date")).strftime("%Y-%m-%d"),
                "selected_mode": selected_mode,
                "opened": int(len(picked)),
                "reason": reason,
                "available_modes": ",".join(sorted(available)),
                "market_style": str(context_row.get("market_style_prev") or context_row.get("market_style") or ""),
                "ma_skeleton": str(context_row.get("ma_skeleton") or ""),
                "volume_price_layer": str(context_row.get("volume_price_layer") or ""),
                "adx_layer": str(context_row.get("adx_layer") or ""),
                "up_rate": _safe_num(context_row, "up_rate_prev", _safe_num(context_row, "up_rate", np.nan)),
                "big_down_rate": _safe_num(context_row, "big_down_rate_prev", _safe_num(context_row, "big_down_rate", np.nan)),
                "limit_down_proxy_rate": _safe_num(context_row, "limit_down_proxy_rate", np.nan),
                "mom20": _safe_num(context_row, "mom20", np.nan),
                "mom60": _safe_num(context_row, "mom60", np.nan),
            }
        )

    selected = pd.concat(selected_rows, ignore_index=True) if selected_rows else pd.DataFrame(columns=enriched.columns)
    all_candidates = pd.concat(all_candidate_rows, ignore_index=True) if all_candidate_rows else pd.DataFrame(columns=enriched.columns)
    daily_candidates = pd.DataFrame(daily_candidate_rows)
    return selected, pd.DataFrame(decisions), all_candidates, daily_candidates


def annual_by_mode(closed: pd.DataFrame) -> pd.DataFrame:
    if closed.empty:
        return pd.DataFrame()
    d = closed.copy()
    d["year"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.year
    rows = []
    for (year, mode), g in d.groupby(["year", "mode"]):
        rets = pd.to_numeric(g["net_ret"], errors="coerce").dropna()
        rows.append(
            {
                "year": int(year),
                "mode": mode,
                "trades": int(len(g)),
                "sum_ret": float(rets.sum()) if len(rets) else 0.0,
                "avg_ret": float(rets.mean()) if len(rets) else 0.0,
                "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values(["year", "sum_ret"], ascending=[True, False])


def compare_models(calendar: list[pd.Timestamp], trades: pd.DataFrame, state_trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, tuple[pd.DataFrame, pd.DataFrame]]]:
    models: dict[str, pd.DataFrame] = {
        "state_market_router_v1": state_trades,
        "institutional_mainwave": trades[trades["mode"].eq("institutional_mainwave")].copy(),
        "old_g3_route_v3": trades[trades["mode"].eq("old_g3_route_v3")].copy(),
    }
    rolling_path = SRC_DIR / "rolling_mode_router_360d_closed_trades.csv"
    if rolling_path.exists():
        rolling = pd.read_csv(rolling_path, low_memory=False, encoding="utf-8-sig")
        rolling["entry_date"] = pd.to_datetime(rolling["entry_date"], errors="coerce").dt.normalize()
        rolling["policy_exit_date"] = pd.to_datetime(rolling["policy_exit_date"], errors="coerce").dt.normalize()
        models["rolling_mode_router_360d"] = rolling

    simulated: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    summary_rows = []
    window_frames = []
    for name, model_trades in models.items():
        curve, closed = simulate(model_trades, name, calendar)
        simulated[name] = (curve, closed)
        summary_rows.append(metrics(name, curve, closed))
        window_frames.append(window_metrics(name, curve, closed))
    summary = pd.DataFrame(summary_rows).sort_values("total_return", ascending=False)
    windows = pd.concat(window_frames, ignore_index=True)
    return summary, windows, simulated


def write_report(
    summary: pd.DataFrame,
    windows: pd.DataFrame,
    mode_counts: pd.DataFrame,
    annual: pd.DataFrame,
    decisions: pd.DataFrame,
    meta: dict[str, Any],
) -> None:
    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "best_trade", "return", "avg_ret", "sum_ret"}
    opened = decisions[decisions["opened"].gt(0)].copy()
    state_window = windows[windows["model"].eq("state_market_router_v1")].copy()
    lines = [
        "# G3 市场状态路由器 v1",
        "",
        "## 核心结论",
        "",
        "- 这版不是用上一段收益排名追涨，而是用前一交易日市场状态决定今日使用哪种赚钱模式，避免使用入场日收盘信息。",
        "- 路由目标是把 2024-09 之后的机构主升浪模式保留下来，同时在 2024-09 之前切回旧G3、恐慌修复、弱修复等更适配的模式。",
        "- 目前仍是研究版：规则是显式经验路由，不是最终机器学习分类器；但它能作为下一步影子交易的模式开关原型。",
        "",
        "## 全周期对比",
        "",
        _md_table(summary, pct_cols=pct_cols),
        "",
        "## 状态路由分窗口",
        "",
        _md_table(state_window, pct_cols=pct_cols),
        "",
        "## 全模型分窗口对比",
        "",
        _md_table(windows.sort_values(["window", "return"], ascending=[True, False]), pct_cols=pct_cols, max_rows=80),
        "",
        "## 状态路由使用模式",
        "",
        _md_table(mode_counts, pct_cols={"share"}),
        "",
        "## 状态路由分年模式贡献",
        "",
        _md_table(annual, pct_cols={"sum_ret", "avg_ret", "win_rate"}, max_rows=120),
        "",
        "## 最近20次开仓决策",
        "",
        _md_table(opened.tail(20), pct_cols={"up_rate", "big_down_rate", "limit_down_proxy_rate", "mom20", "mom60"}, max_rows=20),
        "",
        "## 文件",
        "",
        f"- 输出目录：`{meta['out_dir']}`",
        "- `state_router_closed_trades.csv`：状态路由实际选中的交易",
        "- `state_router_decisions.csv`：每天的模式选择、前日市场状态、选择原因",
        "- `state_router_window_summary.csv`：与机构主升、旧G3、滚动收益路由的窗口对比",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_trade_library()
    market_context = load_market_context()
    enriched = attach_previous_context(trades, market_context)

    start = pd.Timestamp("2020-01-01")
    end = max(pd.Timestamp("2026-06-17"), enriched["policy_exit_date"].max())
    calendar = _trade_calendar(start, end)

    state_trades, decisions, all_candidates, daily_candidates = state_router(enriched, calendar)
    curve, closed = simulate(state_trades, "state_market_router_v1", calendar)
    summary, windows, simulated = compare_models(calendar, enriched, state_trades)

    for name, (model_curve, model_closed) in simulated.items():
        model_curve.to_csv(OUT_DIR / f"{name}_equity_curve.csv", index=False, encoding="utf-8-sig")
        model_closed.to_csv(OUT_DIR / f"{name}_closed_trades.csv", index=False, encoding="utf-8-sig")

    decisions.to_csv(OUT_DIR / "state_router_decisions.csv", index=False, encoding="utf-8-sig")
    all_candidates.to_csv(OUT_DIR / "state_router_all_candidates.csv", index=False, encoding="utf-8-sig")
    daily_candidates.to_csv(OUT_DIR / "state_router_daily_candidate_summary.csv", index=False, encoding="utf-8-sig")
    state_trades.to_csv(OUT_DIR / "state_router_selected_candidates.csv", index=False, encoding="utf-8-sig")
    closed.to_csv(OUT_DIR / "state_router_closed_trades.csv", index=False, encoding="utf-8-sig")
    curve.to_csv(OUT_DIR / "state_router_equity_curve.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "state_router_summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "state_router_window_summary.csv", index=False, encoding="utf-8-sig")

    mode_counts = closed["mode"].value_counts(dropna=False).rename_axis("mode").reset_index(name="trades") if not closed.empty else pd.DataFrame(columns=["mode", "trades"])
    if not mode_counts.empty:
        mode_counts["share"] = mode_counts["trades"] / mode_counts["trades"].sum()
    annual = annual_by_mode(closed)
    mode_counts.to_csv(OUT_DIR / "state_router_mode_counts.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "state_router_annual_by_mode.csv", index=False, encoding="utf-8-sig")

    meta = {
        "status": "completed",
        "out_dir": str(OUT_DIR),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_rows": int(len(trades)),
        "all_candidate_rows": int(len(all_candidates)),
        "candidate_days": int(len(daily_candidates)),
        "multi_candidate_days": int((pd.to_numeric(daily_candidates.get("candidate_count"), errors="coerce").fillna(0) >= 2).sum()) if not daily_candidates.empty else 0,
        "selected_rows": int(len(state_trades)),
        "closed_trades": int(len(closed)),
    }
    write_report(summary, windows, mode_counts, annual, decisions, meta)
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
