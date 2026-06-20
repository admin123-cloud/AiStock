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

from scripts.backtest_wave_style_template_strategy_v1 import INITIAL_CAPITAL, _load_index, _max_drawdown, _md_table, _trade_calendar  # noqa: E402
from scripts.backtest_wave_winner_similarity_scheduler_v1 import BASE_VARIANTS, SOURCE_DIR, _load_candidate_pool  # noqa: E402
from utils.paths import report_path  # noqa: E402


BASE_SCHED_DIR = report_path("wave_style_model_scheduler_v1")
OUT_DIR = report_path("score120_sector_diffusion_gate_v1")
BASE_MODEL = "scheduler_focus_240d_score120_aggr25"


def _safe_float(value: Any, digits: int = 6) -> Any:
    try:
        x = float(value)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, digits)


def _pct(value: object) -> str:
    x = _safe_float(value)
    if x is None:
        return ""
    return f"{x:.2%}"


def _build_sector_diffusion(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    pool = _load_candidate_pool(SOURCE_DIR, BASE_VARIANTS)
    pool = pool[(pool["entry_date"] >= start) & (pool["entry_date"] <= end)].copy()
    for col in ["rank_key", "wave_style_score", "ret5", "ret20", "amount_rank", "big_up_days20", "limit_up_days20"]:
        pool[col] = pd.to_numeric(pool.get(col), errors="coerce")

    day_stock = (
        pool.sort_values(["entry_date", "code_raw", "rank_key"], ascending=[True, True, False])
        .drop_duplicates(["entry_date", "code_raw"])
        .copy()
    )
    sector = (
        day_stock.groupby(["entry_date", "l2_sector_name"], dropna=False)
        .agg(
            sector_candidate_count=("code_raw", "count"),
            sector_avg_score=("wave_style_score", "mean"),
            sector_max_score=("wave_style_score", "max"),
            sector_avg_ret5=("ret5", "mean"),
            sector_avg_ret20=("ret20", "mean"),
            sector_big_up_sum=("big_up_days20", "sum"),
            sector_limit_up_sum=("limit_up_days20", "sum"),
            sector_avg_amount_rank=("amount_rank", "mean"),
        )
        .reset_index()
        .sort_values(["l2_sector_name", "entry_date"])
    )
    for col in ["sector_candidate_count", "sector_avg_score", "sector_avg_ret5", "sector_avg_ret20", "sector_big_up_sum"]:
        sector[f"{col}_ma5"] = sector.groupby("l2_sector_name")[col].transform(lambda x: x.rolling(5, min_periods=1).mean())
        sector[f"{col}_chg5"] = sector.groupby("l2_sector_name")[col].transform(lambda x: x / x.shift(5).replace(0, np.nan) - 1.0)

    market = day_stock.groupby("entry_date").agg(market_candidate_count=("code_raw", "count"), market_avg_score=("wave_style_score", "mean")).reset_index()
    sector = sector.merge(market, on="entry_date", how="left")
    sector["sector_share"] = sector["sector_candidate_count"] / sector["market_candidate_count"].replace(0, np.nan)
    for col in ["sector_candidate_count", "sector_avg_score", "sector_avg_ret5", "sector_avg_ret20", "sector_share"]:
        sector[f"{col}_pct"] = sector[col].rank(pct=True)
    sector["sector_diffusion_score"] = (
        sector["sector_candidate_count_pct"] * 30.0
        + sector["sector_avg_score_pct"] * 20.0
        + sector["sector_avg_ret5_pct"] * 15.0
        + sector["sector_avg_ret20_pct"] * 15.0
        + sector["sector_share_pct"] * 20.0
    )
    return sector


def _load_base_trades(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    trades = pd.read_csv(BASE_SCHED_DIR / BASE_MODEL / "closed_trades.csv", encoding="utf-8-sig")
    trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="coerce").dt.normalize()
    trades["policy_exit_date"] = pd.to_datetime(trades["policy_exit_date"], errors="coerce").dt.normalize()
    trades = trades[(trades["entry_date"] >= start) & (trades["entry_date"] <= end)].copy()
    for col in ["net_ret", "rank_key", "wave_style_score", "amount_rank"]:
        if col in trades.columns:
            trades[col] = pd.to_numeric(trades[col], errors="coerce")
    return trades


def _simulate_gate(
    trades: pd.DataFrame,
    calendar: list[pd.Timestamp],
    *,
    name: str,
    min_diffusion: float | None,
    min_count: float | None,
    min_share: float | None,
    min_count_chg5: float | None,
    slots: int,
    slot_pct: float,
    daily_open_limit: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    by_entry = {day: g.copy() for day, g in trades.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []

    for day in calendar:
        realized_pnl = 0.0
        still_open: list[dict[str, Any]] = []
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
        skipped_by_gate = 0
        todays = by_entry.get(day)
        if todays is not None:
            d = todays.copy()
            before = len(d)
            if min_diffusion is not None:
                d = d[pd.to_numeric(d["sector_diffusion_score"], errors="coerce") >= float(min_diffusion)]
            if min_count is not None:
                d = d[pd.to_numeric(d["sector_candidate_count"], errors="coerce") >= float(min_count)]
            if min_share is not None:
                d = d[pd.to_numeric(d["sector_share"], errors="coerce") >= float(min_share)]
            if min_count_chg5 is not None:
                d = d[pd.to_numeric(d["sector_candidate_count_chg5"], errors="coerce") >= float(min_count_chg5)]
            skipped_by_gate = before - len(d)
            d = d.sort_values(["entry_date", "rank_key", "amount_rank"], ascending=[True, False, False])
            for row in d.itertuples(index=False):
                if opened >= int(daily_open_limit) or len(open_pos) >= int(slots):
                    break
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * float(slot_pct)
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
                "date": day,
                "scheduler": name,
                "cash": cash,
                "reserved_principal": reserved,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "skipped_by_gate": skipped_by_gate,
                "realized_pnl": realized_pnl,
            }
        )

    curve = pd.DataFrame(curve_rows)
    closed_df = pd.DataFrame(closed)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df


def _window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, index_df: pd.DataFrame, name: str, window: str, start: str, end: str) -> dict[str, Any]:
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    iw = index_df[(index_df["trade_date"] >= pd.Timestamp(start)) & (index_df["trade_date"] <= pd.Timestamp(end))].copy()
    if cw.empty:
        return {"model": name, "window": window, "closed": 0}
    strat_ret = float(cw["equity"].iloc[-1] / cw["equity"].iloc[0] - 1.0)
    index_ret = float(iw["close"].iloc[-1] / iw["close"].iloc[0] - 1.0) if len(iw) >= 2 else np.nan
    net = pd.to_numeric(tw.get("net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "model": name,
        "window": window,
        "closed": int(len(tw)),
        "unique_codes": int(tw["code_raw"].nunique()) if (not tw.empty and "code_raw" in tw.columns) else 0,
        "strategy_ret": strat_ret,
        "index_ret": index_ret,
        "excess_ret": strat_ret - index_ret if pd.notna(index_ret) else np.nan,
        "max_drawdown": _max_drawdown(cw["equity"]),
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "mean_trade_ret": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "avg_open_positions": float(cw["open_positions"].mean()),
        "max_open_positions": int(cw["open_positions"].max()),
    }


def _annual_metrics(curve: pd.DataFrame, closed: pd.DataFrame, index_df: pd.DataFrame, name: str) -> pd.DataFrame:
    years = sorted(pd.to_datetime(curve["date"]).dt.year.dropna().unique().tolist()) if not curve.empty else []
    return pd.DataFrame([_window_metrics(curve, closed, index_df, name, str(int(y)), f"{int(y)}-01-01", f"{int(y)}-12-31") for y in years])


def _contribution_summary(closed: pd.DataFrame, name: str) -> pd.DataFrame:
    if closed.empty:
        return pd.DataFrame()
    rows: list[pd.DataFrame] = []
    for key in ["selected_variant", "template_label", "l2_sector_name"]:
        if key not in closed.columns:
            continue
        g = (
            closed.groupby(key, dropna=False)
            .agg(
                closed=("net_ret", "size"),
                pnl=("realized_pnl", "sum"),
                avg_ret=("net_ret", "mean"),
                win_rate=("net_ret", lambda x: float((x > 0).mean())),
                worst_ret=("net_ret", "min"),
                avg_diffusion=("sector_diffusion_score", "mean"),
            )
            .reset_index()
            .rename(columns={key: "bucket"})
        )
        g.insert(0, "bucket_type", key)
        rows.append(g)
    out = pd.concat(rows, ignore_index=True)
    out.insert(0, "model", name)
    return out.sort_values("pnl", ascending=False)


def run(args: argparse.Namespace) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date)
    trades = _load_base_trades(start, end)
    sector = _build_sector_diffusion(start, end)
    trades = trades.merge(
        sector[
            [
                "entry_date",
                "l2_sector_name",
                "sector_candidate_count",
                "sector_avg_score",
                "sector_avg_ret5",
                "sector_avg_ret20",
                "sector_share",
                "sector_candidate_count_chg5",
                "sector_diffusion_score",
            ]
        ],
        on=["entry_date", "l2_sector_name"],
        how="left",
    )
    calendar = _trade_calendar(start, trades["policy_exit_date"].max())
    index_df = _load_index(args.start_date, args.end_date)

    configs = [
        {"name": "score120_base_replay", "min_diffusion": None, "min_count": None, "min_share": None, "min_count_chg5": None},
        {"name": "score120_diff65", "min_diffusion": 65.0, "min_count": None, "min_share": None, "min_count_chg5": None},
        {"name": "score120_diff70", "min_diffusion": 70.0, "min_count": None, "min_share": None, "min_count_chg5": None},
        {"name": "score120_diff72", "min_diffusion": 72.0, "min_count": None, "min_share": None, "min_count_chg5": None},
        {"name": "score120_diff75", "min_diffusion": 75.0, "min_count": None, "min_share": None, "min_count_chg5": None},
        {"name": "score120_diff80", "min_diffusion": 80.0, "min_count": None, "min_share": None, "min_count_chg5": None},
        {"name": "score120_diff72_share08", "min_diffusion": 72.0, "min_count": None, "min_share": 0.08, "min_count_chg5": None},
        {"name": "score120_diff75_share08", "min_diffusion": 75.0, "min_count": None, "min_share": 0.08, "min_count_chg5": None},
        {"name": "score120_diff80_chg15", "min_diffusion": 80.0, "min_count": None, "min_share": None, "min_count_chg5": 1.5},
    ]
    windows = {
        "full": (args.start_date, args.end_date),
        "train_2020_2023": ("2020-01-01", "2023-12-31"),
        "valid_2024_2025": ("2024-01-01", "2025-12-31"),
        "post_2024_09": ("2024-09-24", args.end_date),
        "blind_2026ytd": ("2026-01-01", args.end_date),
    }

    summary_rows: list[dict[str, Any]] = []
    annual_frames: list[pd.DataFrame] = []
    contrib_frames: list[pd.DataFrame] = []
    for cfg in configs:
        name = cfg["name"]
        run_dir = OUT_DIR / name
        run_dir.mkdir(parents=True, exist_ok=True)
        curve, closed = _simulate_gate(
            trades,
            calendar,
            name=name,
            min_diffusion=cfg["min_diffusion"],
            min_count=cfg["min_count"],
            min_share=cfg["min_share"],
            min_count_chg5=cfg["min_count_chg5"],
            slots=int(args.slots),
            slot_pct=float(args.slot_pct),
            daily_open_limit=int(args.daily_open_limit),
        )
        curve.to_csv(run_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        for window, (w_start, w_end) in windows.items():
            summary_rows.append(_window_metrics(curve, closed, index_df, name, window, w_start, w_end))
        annual_frames.append(_annual_metrics(curve, closed, index_df, name))
        contrib = _contribution_summary(closed, name)
        if not contrib.empty:
            contrib_frames.append(contrib)

    summary = pd.DataFrame(summary_rows)
    annual = pd.concat(annual_frames, ignore_index=True) if annual_frames else pd.DataFrame()
    contrib = pd.concat(contrib_frames, ignore_index=True) if contrib_frames else pd.DataFrame()
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_summary.csv", index=False, encoding="utf-8-sig")
    contrib.to_csv(OUT_DIR / "contribution_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(configs).to_csv(OUT_DIR / "scheduler_config.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / "base_trades_with_sector_diffusion.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"strategy_ret", "index_ret", "excess_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade", "avg_ret", "worst_ret"}
    full = summary[summary["window"].eq("full")].sort_values("excess_ret", ascending=False)
    post = summary[summary["window"].eq("post_2024_09")].sort_values("excess_ret", ascending=False)
    blind = summary[summary["window"].eq("blind_2026ytd")].sort_values("excess_ret", ascending=False)
    best = full.iloc[0]["model"]
    annual_best = annual[annual["model"].eq(best)].sort_values("window") if not annual.empty else pd.DataFrame()
    contrib_best = contrib[contrib["model"].eq(best)].sort_values("pnl", ascending=False) if not contrib.empty else pd.DataFrame()

    lines = [
        "# score120 主线扩散门控回测 v1",
        "",
        "## 定义",
        "",
        f"- 基础策略：`{BASE_MODEL}`。",
        "- 门控口径：不重新选股，只对基础策略原本会成交的交易附加行业扩散分。",
        "- 行业扩散分来自同日四类主升模板候选池，综合行业候选数量、行业平均分、5/20 日涨幅、行业候选市场占比。",
        f"- 仓位：{int(args.slots)} 槽，每槽 {float(args.slot_pct):.0%}，每日最多开 {int(args.daily_open_limit)} 笔。",
        "",
        "## 全周期结果",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
        "## 2024-09 后结果",
        "",
        _md_table(post, pct_cols=pct_cols),
        "",
        "## 2026 样本外结果",
        "",
        _md_table(blind, pct_cols=pct_cols),
        "",
        f"## 最优模型 `{best}` 分年",
        "",
        _md_table(annual_best, pct_cols=pct_cols),
        "",
        "## 最优模型贡献拆分",
        "",
        _md_table(contrib_best, pct_cols=pct_cols),
        "",
        "## 解释",
        "",
        "- `diff65` 偏进攻，保留更多交易，收益最高。",
        "- `diff72` 更均衡，回撤明显降低，但收益低于 `diff65`。",
        "- 过高门槛会错过大肉，适合作为降仓版本，不适合作为唯一版本。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")

    result = {
        "status": "completed",
        "out_dir": str(OUT_DIR),
        "base_model": BASE_MODEL,
        "models": [x["name"] for x in configs],
        "base_trades": int(len(trades)),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay score120 with sector diffusion gate.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-17")
    parser.add_argument("--slots", type=int, default=5)
    parser.add_argument("--slot-pct", type=float, default=0.25)
    parser.add_argument("--daily-open-limit", type=int, default=1)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
