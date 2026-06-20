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
from utils.paths import report_path  # noqa: E402


SOURCE_DIR = report_path("wave_style_template_strategy_backtest_v1")
OUT_DIR = report_path("wave_winner_similarity_scheduler_v1")

BASE_VARIANTS = {
    "learned_sector_h20",
    "learned_sector_strict_h20",
    "learned_sector_trend_h10",
    "learned_sector_trend_h20",
}

FEATURE_COLS = [
    "amount_rank",
    "wave_style_score",
    "trend_score",
    "position_score",
    "volume_score",
    "acceleration_score",
    "learned_sector_bonus",
    "mom20",
    "mom60",
    "range_pos120",
    "close_to_high60",
    "amount_ratio20_60",
    "amount5_20",
    "ret5",
    "ret20",
    "limit_up_days20",
    "big_up_days20",
    "max_dd20",
    "index_mom20",
    "index_mom60",
]


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


def _load_candidate_pool(source_dir: Path, variants: set[str] | None = None) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in sorted(source_dir.glob("*/candidates.csv")):
        variant = path.parent.name
        if variants is not None and variant not in variants:
            continue
        df = pd.read_csv(path, encoding="utf-8-sig")
        if df.empty:
            continue
        df["variant"] = variant
        frames.append(df)
    if not frames:
        raise RuntimeError(f"no candidates.csv found under {source_dir}")
    out = pd.concat(frames, ignore_index=True)
    for col in ["trade_date", "entry_date", "policy_exit_date"]:
        out[col] = pd.to_datetime(out[col], errors="coerce").dt.normalize()
    for col in ["net_ret", "rank_key", "amount_rank", "entry_price", *FEATURE_COLS]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["entry_date", "policy_exit_date", "net_ret", "code_raw"])
    out = out.sort_values(["entry_date", "rank_key", "amount_rank"], ascending=[True, False, False]).reset_index(drop=True)
    return out


def _winner_profile(
    pool: pd.DataFrame,
    day: pd.Timestamp,
    *,
    lookback_days: int,
    min_winners: int,
    winner_ret: float,
    top_n: int,
    recent_weight: bool,
) -> dict[str, Any] | None:
    start = day - pd.Timedelta(days=int(lookback_days))
    hist = pool[(pool["policy_exit_date"] < day) & (pool["policy_exit_date"] >= start) & (pool["net_ret"] >= float(winner_ret))].copy()
    if hist.empty:
        return None
    hist = hist.sort_values("net_ret", ascending=False).head(int(top_n)).copy()
    if len(hist) < int(min_winners):
        return None

    feats = hist[[c for c in FEATURE_COLS if c in hist.columns]].copy()
    if feats.empty:
        return None
    weights = None
    if recent_weight:
        age = (day - hist["policy_exit_date"]).dt.days.clip(lower=1)
        weights = (1.0 / np.sqrt(age)).to_numpy(dtype=float)
        weights = weights / weights.sum()

    center: dict[str, float] = {}
    scale: dict[str, float] = {}
    for col in feats.columns:
        x = pd.to_numeric(feats[col], errors="coerce")
        valid = x.dropna()
        if len(valid) < max(3, int(min_winners) // 2):
            continue
        if weights is not None:
            mask = x.notna().to_numpy()
            w = weights[mask]
            v = x[mask].to_numpy(dtype=float)
            if len(v) == 0 or w.sum() <= 0:
                continue
            c = float(np.average(v, weights=w / w.sum()))
        else:
            c = float(valid.median())
        mad = float((valid - c).abs().median())
        std = float(valid.std(ddof=0))
        s = max(mad * 1.4826, std * 0.5, 1e-6)
        center[col] = c
        scale[col] = s
    if not center:
        return None

    sector_freq = hist["l2_sector_name"].fillna("").value_counts(normalize=True).to_dict() if "l2_sector_name" in hist.columns else {}
    label_freq = hist["template_label"].fillna("").value_counts(normalize=True).to_dict() if "template_label" in hist.columns else {}
    variant_freq = hist["variant"].fillna("").value_counts(normalize=True).to_dict()
    return {
        "center": center,
        "scale": scale,
        "winner_count": int(len(hist)),
        "winner_mean_ret": float(hist["net_ret"].mean()),
        "winner_median_ret": float(hist["net_ret"].median()),
        "sector_freq": sector_freq,
        "label_freq": label_freq,
        "variant_freq": variant_freq,
    }


def _score_today(todays: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = todays.copy()
    distances: list[pd.Series] = []
    for col, center in profile["center"].items():
        if col not in d.columns:
            continue
        scale = float(profile["scale"].get(col, 1.0))
        distances.append((pd.to_numeric(d[col], errors="coerce") - float(center)).abs() / scale)
    if not distances:
        d["similarity_score"] = 0.0
    else:
        dist = pd.concat(distances, axis=1).clip(upper=5.0).mean(axis=1)
        d["similarity_score"] = np.exp(-dist / 1.35)
    sector_freq = profile.get("sector_freq", {})
    label_freq = profile.get("label_freq", {})
    variant_freq = profile.get("variant_freq", {})
    d["winner_sector_freq"] = d.get("l2_sector_name", "").fillna("").map(sector_freq).fillna(0.0)
    d["winner_label_freq"] = d.get("template_label", "").fillna("").map(label_freq).fillna(0.0)
    d["winner_variant_freq"] = d.get("variant", "").fillna("").map(variant_freq).fillna(0.0)
    d["similarity_rank_score"] = (
        d["similarity_score"] * 65.0
        + d["winner_sector_freq"] * 18.0
        + d["winner_label_freq"] * 9.0
        + d["winner_variant_freq"] * 8.0
        + (pd.to_numeric(d.get("wave_style_score", 0), errors="coerce").fillna(0.0).clip(0, 140) / 140.0) * 10.0
    )
    return d.sort_values(["similarity_rank_score", "rank_key", "amount_rank"], ascending=[False, False, False])


def _simulate_similarity(
    pool: pd.DataFrame,
    calendar: list[pd.Timestamp],
    *,
    name: str,
    lookback_days: int,
    min_winners: int,
    winner_ret: float,
    top_n: int,
    min_similarity_rank_score: float,
    recent_weight: bool,
    slots: int,
    slot_pct: float,
    daily_open_limit: int,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    by_entry = {day: g.copy() for day, g in pool.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []

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

        profile = _winner_profile(
            pool,
            day,
            lookback_days=lookback_days,
            min_winners=min_winners,
            winner_ret=winner_ret,
            top_n=top_n,
            recent_weight=recent_weight,
        )
        opened = 0
        selected_score = np.nan
        selected_similarity = np.nan
        winner_count = 0
        winner_mean_ret = np.nan
        selected_code = ""
        selected_variant = ""
        selected_label = ""
        if profile is not None:
            winner_count = int(profile["winner_count"])
            winner_mean_ret = float(profile["winner_mean_ret"])
            todays = by_entry.get(day)
            if todays is not None:
                ranked = _score_today(todays, profile)
                ranked = ranked[ranked["similarity_rank_score"] >= float(min_similarity_rank_score)].copy()
                seen_codes = {str(p["code_raw"]) for p in open_pos}
                for row in ranked.itertuples(index=False):
                    if opened >= int(daily_open_limit):
                        break
                    if len(open_pos) >= int(slots):
                        break
                    row_dict = row._asdict()
                    if str(row_dict.get("code_raw")) in seen_codes:
                        continue
                    equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                    stake = equity_before * float(slot_pct)
                    if stake <= 0 or cash < stake:
                        break
                    row_dict["scheduler"] = name
                    row_dict["selected_similarity_rank_score"] = float(row_dict.get("similarity_rank_score", np.nan))
                    row_dict["selected_similarity_score"] = float(row_dict.get("similarity_score", np.nan))
                    row_dict["profile_winner_count"] = winner_count
                    row_dict["profile_winner_mean_ret"] = winner_mean_ret
                    row_dict["stake"] = stake
                    cash -= stake
                    open_pos.append(row_dict)
                    seen_codes.add(str(row_dict.get("code_raw")))
                    opened += 1
                    if not selected_code:
                        selected_code = str(row_dict.get("code_raw", ""))
                        selected_variant = str(row_dict.get("variant", ""))
                        selected_label = str(row_dict.get("template_label", ""))
                        selected_score = float(row_dict.get("similarity_rank_score", np.nan))
                        selected_similarity = float(row_dict.get("similarity_score", np.nan))

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
                "realized_pnl": realized_pnl,
                "profile_winner_count": winner_count,
                "profile_winner_mean_ret": winner_mean_ret,
                "selected_code": selected_code,
                "selected_variant": selected_variant,
                "selected_label": selected_label,
                "selected_similarity_rank_score": selected_score,
                "selected_similarity_score": selected_similarity,
            }
        )
        decision_rows.append(curve_rows[-1].copy())

    curve = pd.DataFrame(curve_rows)
    closed_df = pd.DataFrame(closed)
    decisions = pd.DataFrame(decision_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df, decisions


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
    for key in ["variant", "template_label", "l2_sector_name"]:
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
            )
            .reset_index()
            .rename(columns={key: "bucket"})
        )
        g.insert(0, "bucket_type", key)
        rows.append(g)
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    out.insert(0, "model", name)
    return out.sort_values("pnl", ascending=False)


def run(args: argparse.Namespace) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pool = _load_candidate_pool(SOURCE_DIR, BASE_VARIANTS)
    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date)
    pool = pool[(pool["entry_date"] >= start) & (pool["entry_date"] <= end)].copy()
    calendar = _trade_calendar(start, pool["policy_exit_date"].max())
    index_df = _load_index(args.start_date, args.end_date)

    configs = [
        {
            "name": "sim240_win20_top60_score50",
            "lookback_days": 240,
            "min_winners": 12,
            "winner_ret": 0.20,
            "top_n": 60,
            "min_similarity_rank_score": 50.0,
            "recent_weight": False,
            "slot_pct": 0.25,
        },
        {
            "name": "sim240_win30_top40_score50",
            "lookback_days": 240,
            "min_winners": 8,
            "winner_ret": 0.30,
            "top_n": 40,
            "min_similarity_rank_score": 50.0,
            "recent_weight": False,
            "slot_pct": 0.25,
        },
        {
            "name": "sim180_win20_top40_score52_recent",
            "lookback_days": 180,
            "min_winners": 8,
            "winner_ret": 0.20,
            "top_n": 40,
            "min_similarity_rank_score": 52.0,
            "recent_weight": True,
            "slot_pct": 0.25,
        },
        {
            "name": "sim120_win25_top30_score52_recent",
            "lookback_days": 120,
            "min_winners": 6,
            "winner_ret": 0.25,
            "top_n": 30,
            "min_similarity_rank_score": 52.0,
            "recent_weight": True,
            "slot_pct": 0.25,
        },
        {
            "name": "sim240_win20_top60_score55",
            "lookback_days": 240,
            "min_winners": 12,
            "winner_ret": 0.20,
            "top_n": 60,
            "min_similarity_rank_score": 55.0,
            "recent_weight": False,
            "slot_pct": 0.25,
        },
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
        curve, closed, decisions = _simulate_similarity(
            pool,
            calendar,
            name=name,
            lookback_days=int(cfg["lookback_days"]),
            min_winners=int(cfg["min_winners"]),
            winner_ret=float(cfg["winner_ret"]),
            top_n=int(cfg["top_n"]),
            min_similarity_rank_score=float(cfg["min_similarity_rank_score"]),
            recent_weight=bool(cfg["recent_weight"]),
            slots=int(args.slots),
            slot_pct=float(cfg.get("slot_pct", args.slot_pct)),
            daily_open_limit=int(args.daily_open_limit),
        )
        curve.to_csv(run_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        decisions.to_csv(run_dir / "daily_decisions.csv", index=False, encoding="utf-8-sig")
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

    pct_cols = {"strategy_ret", "index_ret", "excess_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade", "avg_ret", "worst_ret"}
    full = summary[summary["window"].eq("full")].sort_values("excess_ret", ascending=False)
    post = summary[summary["window"].eq("post_2024_09")].sort_values("excess_ret", ascending=False)
    blind = summary[summary["window"].eq("blind_2026ytd")].sort_values("excess_ret", ascending=False)
    best_names = full.head(2)["model"].tolist()
    annual_focus = annual[annual["model"].isin(best_names)].sort_values(["model", "window"]) if not annual.empty else pd.DataFrame()
    contrib_focus = contrib[contrib["model"].isin(best_names)].sort_values(["model", "pnl"], ascending=[True, False]) if not contrib.empty else pd.DataFrame()

    lines = [
        "# 波段赢家相似度调度回测 v1",
        "",
        "## 定义",
        "",
        f"- 回测区间：{args.start_date} 至 {args.end_date}",
        "- 每天只使用当天以前已经退出且盈利的候选作为赢家样本，提取趋势、容量、动量、位置、量能和回撤特征。",
        "- 当天候选按与赢家画像的相似度、赢家行业频率、赢家标签频率、赢家模板频率重新排序。",
        f"- 仓位：{int(args.slots)} 槽，默认每槽 {float(args.slot_pct):.0%}，每日最多开 {int(args.daily_open_limit)} 笔。",
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
        "## 最优相似度模型分年",
        "",
        _md_table(annual_focus, pct_cols=pct_cols),
        "",
        "## 最优相似度模型贡献拆分",
        "",
        _md_table(contrib_focus, pct_cols=pct_cols),
        "",
        "## 初步判断",
        "",
        "- 如果相似度模型低于 score120 调度器，说明仅用静态日线特征距离还不够，需要加入“上一波赢家所处波段阶段”和“当前主线强度扩散”。",
        "- 如果相似度模型在 2026 明显更强但全周期较弱，说明它更适合机构大波段牛市，而不适合震荡/弱势低吸年份。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")

    result = {
        "status": "completed",
        "source_dir": str(SOURCE_DIR),
        "out_dir": str(OUT_DIR),
        "candidate_rows": int(len(pool)),
        "models": [x["name"] for x in configs],
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest winner-style similarity scheduler.")
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
