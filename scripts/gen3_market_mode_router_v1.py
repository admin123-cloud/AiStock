from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_style_template_strategy_v1 import INITIAL_CAPITAL, _max_drawdown, _trade_calendar  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("gen3_market_mode_router_v1")
MARKET_CONTEXT = report_path("gen3_four_path_independent_candidates", "market_context.csv")

SOURCES = [
    {
        "mode": "institutional_mainwave",
        "label": "机构主升浪",
        "path": report_path("gen3_score120_core_strategy_v1", "g3_route_execution_mandate_candidate_closed_trades.csv"),
        "ret_cols": ["stress_net_ret", "policy_net_ret", "net_ret"],
        "date_cols": ["entry_date", "policy_exit_date"],
        "score_cols": ["score", "selected_score", "rank_key"],
    },
    {
        "mode": "panic_repair",
        "label": "恐慌修复",
        "path": report_path("gen3_panic_v2_research", "final_candidate_v1", "m30_close5_full_nextopen_cost30_closed_trades.csv"),
        "ret_cols": ["policy_net_ret", "net_ret"],
        "date_cols": ["entry_date", "policy_exit_date"],
        "score_cols": ["candidate_score", "score"],
    },
    {
        "mode": "range_weak_rebound",
        "label": "震荡/弱反弹",
        "path": report_path("gen3_range_v3_mtm_pressure_v1", "range_v3_weak_low_not_chasing_h5_cost30_closed_trades.csv"),
        "ret_cols": ["policy_net_ret", "net_ret"],
        "date_cols": ["entry_date", "policy_exit_date"],
        "score_cols": ["range_v3_score", "candidate_score"],
    },
    {
        "mode": "strong_volume5",
        "label": "题材/强势volume5",
        "path": report_path("gen3_strong_v2_independent_source_v1", "strong_v2_main_up_only_hold5_closed_trades.csv"),
        "ret_cols": ["policy_net_ret", "net_ret"],
        "date_cols": ["entry_date", "policy_exit_date"],
        "score_cols": ["g3_strong_score", "score_volume5", "candidate_score"],
    },
    {
        "mode": "old_g3_guarded",
        "label": "旧G3防守混合",
        "path": report_path("gen3_guarded_candidate_package_v1", "g3_guarded_candidate_closed_trades.csv"),
        "ret_cols": ["policy_net_ret", "net_ret"],
        "date_cols": ["entry_date", "policy_exit_date"],
        "score_cols": ["score"],
    },
    {
        "mode": "old_g3_route_v3",
        "label": "旧G3路由V3",
        "path": report_path("gen3_route_execution_mandate_candidate_package_v3", "g3_route_execution_mandate_candidate_closed_trades.csv"),
        "ret_cols": ["stress_net_ret", "policy_net_ret", "net_ret"],
        "date_cols": ["entry_date", "policy_exit_date"],
        "score_cols": ["score"],
    },
]


def _first_col(df: pd.DataFrame, cols: list[str]) -> str | None:
    return next((c for c in cols if c in df.columns), None)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
    except Exception:
        return default
    return x if math.isfinite(x) else default


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return ""
    if not math.isfinite(x):
        return ""
    return f"{x:.2%}"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, max_rows: int = 40) -> str:
    if df.empty:
        return "_无数据_"
    d = df.head(max_rows).copy()
    for col in pct_cols or set():
        if col in d.columns:
            d[col] = d[col].map(_pct)
    return d.to_markdown(index=False)


def _load_one(src: dict[str, Any]) -> pd.DataFrame:
    path = Path(src["path"])
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, low_memory=False, encoding="utf-8-sig")
    if df.empty:
        return pd.DataFrame()
    ret_col = _first_col(df, src["ret_cols"])
    entry_col = _first_col(df, [src["date_cols"][0]])
    exit_col = _first_col(df, [src["date_cols"][1], "exit_date", "stress_exit_date"])
    score_col = _first_col(df, src["score_cols"])
    if not ret_col or not entry_col or not exit_col:
        return pd.DataFrame()
    out = pd.DataFrame(index=df.index)
    out["mode"] = src["mode"]
    out["mode_label"] = src["label"]
    out["source_path"] = str(path)
    out["entry_date"] = pd.to_datetime(df[entry_col], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(df[exit_col], errors="coerce").dt.normalize()
    out["code"] = df.get("code", "").astype(str)
    default_mode = pd.Series(src["mode"], index=df.index)
    out["name"] = df["name"].astype(str) if "name" in df.columns else df["stock_name"].astype(str) if "stock_name" in df.columns else ""
    out["route"] = df["route"].astype(str) if "route" in df.columns else default_mode.astype(str)
    out["score"] = pd.to_numeric(df[score_col], errors="coerce") if score_col else 0.0
    out["net_ret"] = pd.to_numeric(df[ret_col], errors="coerce")
    out["entry_price"] = pd.to_numeric(df.get("entry_price"), errors="coerce")
    out["market_style"] = df["market_style"] if "market_style" in df.columns else ""
    out["index_mom20"] = pd.to_numeric(df.get("index_mom20"), errors="coerce")
    out["index_mom60"] = pd.to_numeric(df.get("index_mom60", df.get("sig_index_mom60")), errors="coerce")
    out["up_rate"] = pd.to_numeric(df.get("up_rate", df.get("day_up_rate")), errors="coerce")
    out["big_down_rate"] = pd.to_numeric(df.get("big_down_rate"), errors="coerce")
    out["chain"] = df["g3_chain"].astype(str) if "g3_chain" in df.columns else df["chain"].astype(str) if "chain" in df.columns else default_mode.astype(str)
    out = out.dropna(subset=["entry_date", "policy_exit_date", "net_ret"])
    out = out[out["policy_exit_date"].ge(out["entry_date"])].copy()
    out["net_ret"] = out["net_ret"].clip(lower=-0.95, upper=2.0)
    return out


def load_trades() -> pd.DataFrame:
    frames = [_load_one(src) for src in SOURCES]
    frames = [f for f in frames if not f.empty]
    if not frames:
        raise RuntimeError("no mode source trades loaded")
    all_trades = pd.concat(frames, ignore_index=True)
    all_trades = all_trades.sort_values(["entry_date", "mode", "score"], ascending=[True, True, False])
    all_trades["trade_key"] = (
        all_trades["mode"]
        + "|"
        + all_trades["entry_date"].dt.strftime("%Y-%m-%d")
        + "|"
        + all_trades["code"].astype(str)
    )
    return all_trades.drop_duplicates("trade_key").reset_index(drop=True)


def simulate(trades: pd.DataFrame, name: str, calendar: list[pd.Timestamp], slots: int = 4, slot_pct: float = 0.25, daily_open_limit: int = 1) -> tuple[pd.DataFrame, pd.DataFrame]:
    by_entry = {day: g.copy() for day, g in trades.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    for day in calendar:
        realized_pnl = 0.0
        still_open = []
        for pos in open_pos:
            if pos["policy_exit_date"] <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                cash += exit_value
                realized_pnl += exit_value - float(pos["stake"])
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = exit_value - float(pos["stake"])
                closed.append(out)
            else:
                still_open.append(pos)
        open_pos = still_open
        opened = 0
        todays = by_entry.get(day)
        if todays is not None:
            todays = todays.sort_values(["score", "net_ret"], ascending=[False, False])
            for row in todays.itertuples(index=False):
                if opened >= daily_open_limit or len(open_pos) >= slots:
                    break
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * slot_pct
                if stake <= 0 or cash < stake:
                    break
                pos = row._asdict()
                pos["scheduler"] = name
                pos["stake"] = stake
                cash -= stake
                open_pos.append(pos)
                opened += 1
        reserved = sum(float(p["stake"]) for p in open_pos)
        equity = cash + reserved
        curve_rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "scheduler": name,
                "cash": cash,
                "reserved_principal": reserved,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "realized_pnl": realized_pnl,
            }
        )
    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, pd.DataFrame(closed)


def metrics(name: str, curve: pd.DataFrame, closed: pd.DataFrame) -> dict[str, Any]:
    rets = pd.to_numeric(closed.get("net_ret", pd.Series(dtype=float)), errors="coerce").dropna()
    return {
        "model": name,
        "trades": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / curve["equity"].iloc[0] - 1.0) if not curve.empty else 0.0,
        "max_drawdown": _max_drawdown(curve["equity"]) if not curve.empty else 0.0,
        "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
        "avg_trade_return": float(rets.mean()) if len(rets) else 0.0,
        "worst_trade": float(rets.min()) if len(rets) else 0.0,
        "best_trade": float(rets.max()) if len(rets) else 0.0,
    }


def window_metrics(name: str, curve: pd.DataFrame, closed: pd.DataFrame) -> pd.DataFrame:
    windows = [
        ("full", "2020-01-01", "2026-06-17"),
        ("pre_2024_09", "2020-01-01", "2024-09-23"),
        ("post_2024_09", "2024-09-24", "2026-06-17"),
        ("weak_2022", "2022-01-01", "2022-12-31"),
        ("valid_2024", "2024-01-01", "2024-12-31"),
        ("blind_2026ytd", "2026-01-01", "2026-06-17"),
    ]
    rows = []
    cdate = pd.to_datetime(curve["date"], errors="coerce") if not curve.empty else pd.Series(dtype="datetime64[ns]")
    edate = pd.to_datetime(closed.get("entry_date"), errors="coerce") if not closed.empty else pd.Series(dtype="datetime64[ns]")
    for window, start, end in windows:
        s, e = pd.Timestamp(start), pd.Timestamp(end)
        cw = curve[(cdate >= s) & (cdate <= e)].copy()
        tw = closed[(edate >= s) & (edate <= e)].copy()
        rets = pd.to_numeric(tw.get("net_ret", pd.Series(dtype=float)), errors="coerce").dropna()
        rows.append(
            {
                "model": name,
                "window": window,
                "trades": int(len(tw)),
                "return": float(cw["equity"].iloc[-1] / cw["equity"].iloc[0] - 1.0) if not cw.empty else 0.0,
                "max_drawdown": _max_drawdown(cw["equity"]) if not cw.empty else 0.0,
                "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
                "avg_trade_return": float(rets.mean()) if len(rets) else 0.0,
                "worst_trade": float(rets.min()) if len(rets) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def rolling_router(all_trades: pd.DataFrame, calendar: list[pd.Timestamp], lookback_days: int = 360, min_trades: int = 6) -> tuple[pd.DataFrame, pd.DataFrame]:
    selected_rows: list[pd.DataFrame] = []
    decisions = []
    by_day = {day: g.copy() for day, g in all_trades.groupby("entry_date")}
    for day in calendar:
        hist = all_trades[(all_trades["policy_exit_date"] < day) & (all_trades["policy_exit_date"] >= day - pd.Timedelta(days=lookback_days))].copy()
        rows = []
        for mode, g in hist.groupby("mode"):
            rets = pd.to_numeric(g["net_ret"], errors="coerce").dropna()
            if len(rets) < min_trades:
                continue
            total = float(np.prod(1.0 + rets.clip(lower=-0.95)) - 1.0)
            win = float((rets > 0).mean())
            avg = float(rets.mean())
            worst = float(rets.min())
            bad = float((rets <= -0.12).mean())
            score = total * 0.45 + avg * 5.0 + (win - 0.5) * 0.5 + min(worst, 0) * 0.35 - bad * 0.25
            rows.append({"mode": mode, "score": score, "hist_trades": len(rets), "hist_total": total, "hist_win": win, "hist_avg": avg, "hist_worst": worst})
        choice = ""
        reason = "no_mode_with_enough_history"
        if rows:
            rank = pd.DataFrame(rows).sort_values("score", ascending=False)
            top = rank.iloc[0]
            if float(top["score"]) > 0:
                choice = str(top["mode"])
                reason = "rolling_best_positive"
            else:
                reason = "best_mode_score_not_positive"
        todays = by_day.get(day)
        opened = 0
        if choice and todays is not None:
            picked = todays[todays["mode"].eq(choice)].copy()
            if not picked.empty:
                picked = picked.sort_values(["score", "net_ret"], ascending=[False, False]).head(1)
                selected_rows.append(picked)
                opened = len(picked)
        decisions.append({"date": day.strftime("%Y-%m-%d"), "selected_mode": choice, "reason": reason, "opened": opened})
    selected = pd.concat(selected_rows, ignore_index=True) if selected_rows else pd.DataFrame(columns=all_trades.columns)
    return selected, pd.DataFrame(decisions)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_trades = load_trades()
    start = pd.Timestamp("2020-01-01")
    end = max(pd.Timestamp("2026-06-17"), all_trades["policy_exit_date"].max())
    calendar = _trade_calendar(start, end)

    all_trades.to_csv(OUT_DIR / "mode_trade_library.csv", index=False, encoding="utf-8-sig")
    simulated: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    summary_rows = []
    window_frames = []
    for mode, g in all_trades.groupby("mode"):
        curve, closed = simulate(g, mode, calendar)
        curve.to_csv(OUT_DIR / f"{mode}_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(OUT_DIR / f"{mode}_closed_trades.csv", index=False, encoding="utf-8-sig")
        simulated[mode] = (curve, closed)
        summary_rows.append(metrics(mode, curve, closed))
        window_frames.append(window_metrics(mode, curve, closed))

    router_trades, decisions = rolling_router(all_trades, calendar)
    router_curve, router_closed = simulate(router_trades, "rolling_mode_router_360d", calendar)
    router_curve.to_csv(OUT_DIR / "rolling_mode_router_360d_equity_curve.csv", index=False, encoding="utf-8-sig")
    router_closed.to_csv(OUT_DIR / "rolling_mode_router_360d_closed_trades.csv", index=False, encoding="utf-8-sig")
    decisions.to_csv(OUT_DIR / "rolling_mode_router_360d_decisions.csv", index=False, encoding="utf-8-sig")
    summary_rows.append(metrics("rolling_mode_router_360d", router_curve, router_closed))
    window_frames.append(window_metrics("rolling_mode_router_360d", router_curve, router_closed))

    summary = pd.DataFrame(summary_rows).sort_values("total_return", ascending=False)
    windows = pd.concat(window_frames, ignore_index=True)
    annual = []
    for name, (_, closed) in list(simulated.items()) + [("rolling_mode_router_360d", (router_curve, router_closed))]:
        if closed.empty:
            continue
        d = closed.copy()
        d["year"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.year
        for year, g in d.groupby("year"):
            rets = pd.to_numeric(g["net_ret"], errors="coerce").dropna()
            annual.append({"model": name, "year": int(year), "trades": len(g), "sum_ret": float(rets.sum()), "avg_ret": float(rets.mean()), "win_rate": float((rets > 0).mean())})
    annual_df = pd.DataFrame(annual)

    context_rows = []
    if MARKET_CONTEXT.exists():
        mc = pd.read_csv(MARKET_CONTEXT, low_memory=False)
        mc["entry_date"] = pd.to_datetime(mc["trade_date"], errors="coerce").dt.normalize()
        enriched = all_trades.merge(mc[["entry_date", "market_style", "ma_skeleton", "volume_price_layer", "adx_layer", "up_rate", "big_down_rate", "mom20", "mom60"]], on="entry_date", how="left", suffixes=("", "_ctx"))
        style_col = "market_style_ctx" if "market_style_ctx" in enriched.columns else "market_style"
        for (mode, style), g in enriched.groupby(["mode", style_col], dropna=False):
            rets = pd.to_numeric(g["net_ret"], errors="coerce").dropna()
            context_rows.append({"mode": mode, "market_style": style, "trades": len(g), "avg_ret": float(rets.mean()) if len(rets) else 0.0, "win_rate": float((rets > 0).mean()) if len(rets) else 0.0})
    context_df = pd.DataFrame(context_rows).sort_values(["mode", "avg_ret"], ascending=[True, False]) if context_rows else pd.DataFrame()

    summary.to_csv(OUT_DIR / "mode_summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "mode_window_summary.csv", index=False, encoding="utf-8-sig")
    annual_df.to_csv(OUT_DIR / "mode_annual_trade_summary.csv", index=False, encoding="utf-8-sig")
    context_df.to_csv(OUT_DIR / "mode_market_context_summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "best_trade", "return", "avg_ret"}
    report = "\n".join(
        [
            "# G3 市场模式路由器 v1",
            "",
            "## 核心结论",
            "",
            "- 当前 `institutional_mainwave` 只是一种赚钱模式，不能代表全周期。",
            "- v1 先把已有模式库统一回测，并用过去 360 天已结束交易表现做滚动选择，不使用未来收益选择当天模式。",
            "- 这版目标不是最终上线，而是判断历史中到底有哪些模式在不同市场状态下挣钱。",
            "",
            "## 模式库全周期表现",
            "",
            _md_table(summary, pct_cols=pct_cols),
            "",
            "## 分窗口表现",
            "",
            _md_table(windows.sort_values(["window", "return"], ascending=[True, False]), pct_cols=pct_cols, max_rows=80),
            "",
            "## 分年交易质量",
            "",
            _md_table(annual_df.sort_values(["year", "avg_ret"], ascending=[True, False]), pct_cols={"sum_ret", "avg_ret", "win_rate"}, max_rows=120),
            "",
            "## 市场状态归因",
            "",
            _md_table(context_df, pct_cols={"avg_ret", "win_rate"}, max_rows=120),
            "",
            "## 下一步",
            "",
            "1. 把滚动绩效选择器升级为市场状态识别器，不直接追逐上一段收益。",
            "2. 给每种模式建立开关：机构主升浪、题材加速、冰点修复、恐慌修复、震荡弱反弹、空仓。",
            "3. 对每个模式单独做无未来函数的当日候选生成，再进入 G3 shadow live。",
        ]
    )
    (OUT_DIR / "REPORT_CN.md").write_text(report, encoding="utf-8")
    meta = {
        "status": "completed",
        "out_dir": str(OUT_DIR),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode_count": int(all_trades["mode"].nunique()),
        "trade_rows": int(len(all_trades)),
        "router_trades": int(len(router_closed)),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
