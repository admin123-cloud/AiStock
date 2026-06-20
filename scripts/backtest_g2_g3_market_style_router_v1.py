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

from scripts.backtest_wave_style_template_strategy_v1 import _trade_calendar  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("g2_g3_market_style_router_v1")
G2_TRADES = report_path("gen2_v2_complete_strategy", "runs", "official", "full", "trades.csv")
G2_SOURCE = report_path("gen2_v2_complete_strategy_2020", "sources", "g2_v2_complete.csv")
G3_CLOSED = report_path(
    "gen3_full_candidate_2slot_backtest_v1",
    "eligible_top2_sector_guard_mainwave_exempt_sector_for_distinct_closed_trades.csv",
)
MARKET_CONTEXT = report_path("gen3_four_path_independent_candidates", "market_context.csv")

INITIAL_CAPITAL = 1_000_000.0
MAX_SLOTS = 2
SLOT_PCT = 0.50
START_DATE = pd.Timestamp("2020-01-01")
END_FLOOR = pd.Timestamp("2026-06-18")


WINDOWS = {
    "full": ("2020-01-01", "2026-12-31"),
    "pre_2024_09": ("2020-01-01", "2024-09-30"),
    "post_2024_10": ("2024-10-01", "2026-12-31"),
    "2022_bear": ("2022-01-01", "2022-12-31"),
    "2024": ("2024-01-01", "2024-12-31"),
    "2025": ("2025-01-01", "2025-12-31"),
    "2026ytd": ("2026-01-01", "2026-12-31"),
}


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(str(path))
    return pd.read_csv(path, low_memory=False, encoding="utf-8-sig")


def _safe_float(value: Any, default: float = np.nan) -> float:
    try:
        x = float(value)
    except Exception:
        return default
    return x if math.isfinite(x) else default


def _pct(value: Any) -> str:
    x = _safe_float(value)
    if not math.isfinite(x):
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


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def load_g2_lots() -> pd.DataFrame:
    trades = _read_csv(G2_TRADES)
    trades["buy_datetime"] = pd.to_datetime(trades["buy_datetime"], errors="coerce")
    trades["sell_datetime"] = pd.to_datetime(trades["sell_datetime"], errors="coerce")
    trades["entry_date"] = pd.to_datetime(trades["buy_date"], errors="coerce").dt.normalize()
    trades["exit_date"] = pd.to_datetime(trades["sell_date"], errors="coerce").dt.normalize()
    for col in ["capital", "return", "pnl", "v4_rank", "v4_score", "buy_price"]:
        if col in trades.columns:
            trades[col] = pd.to_numeric(trades[col], errors="coerce")
    key_cols = ["code", "name", "entry_date", "buy_datetime", "buy_price", "v4_rank", "v4_score"]
    rows: list[dict[str, Any]] = []
    for key, group in trades.dropna(subset=["entry_date", "exit_date"]).groupby(key_cols, dropna=False, sort=False):
        item = dict(zip(key_cols, key))
        group = group.sort_values("sell_datetime").copy()
        capital = pd.to_numeric(group["capital"], errors="coerce").fillna(0.0)
        total_capital = float(capital.sum())
        if total_capital <= 0:
            total_capital = float(capital.iloc[0]) if len(capital) else 0.0
        if total_capital <= 0:
            continue
        returns = pd.to_numeric(group["return"], errors="coerce").fillna(0.0)
        net_ret = float((capital * returns).sum() / total_capital)
        exit_reasons = ",".join(group.get("exit_reason", pd.Series(dtype=str)).fillna("").astype(str).tolist())
        entry_dt = pd.Timestamp(item["buy_datetime"])
        rows.append(
            {
                "engine": "g2",
                "trade_key": f"g2|{item['code']}|{entry_dt.isoformat()}",
                "code": str(item["code"]),
                "name": str(item["name"]),
                "entry_date": pd.Timestamp(item["entry_date"]),
                "entry_datetime": entry_dt,
                "policy_exit_date": pd.Timestamp(group["exit_date"].max()),
                "net_ret": net_ret,
                "entry_price": _safe_float(item.get("buy_price"), np.nan),
                "score": _safe_float(item.get("v4_score"), 0.0),
                "rank_key": 10000.0 - _safe_float(item.get("v4_rank"), 9999.0),
                "route": "g2_buy_point",
                "mode": "g2_buy_point",
                "exit_reason": exit_reasons,
                "source_open_capital": total_capital,
            }
        )
    out = pd.DataFrame(rows)
    return enrich_g2_context(out)


def enrich_g2_context(lots: pd.DataFrame) -> pd.DataFrame:
    if lots.empty or not G2_SOURCE.exists():
        return lots
    src = _read_csv(G2_SOURCE)
    if "entry_date" not in src.columns:
        return lots
    src["entry_date"] = pd.to_datetime(src["entry_date"], errors="coerce").dt.normalize()
    keep = [
        "code",
        "entry_date",
        "source_family",
        "signal_family",
        "g2_v2_buy_logic",
        "l2_sector_name",
        "l3_sector_name",
    ]
    src = src[[c for c in keep if c in src.columns]].dropna(subset=["code", "entry_date"]).drop_duplicates(
        ["code", "entry_date"],
        keep="first",
    )
    out = lots.merge(src, on=["code", "entry_date"], how="left")
    out["sector_for_distinct"] = out.get("l2_sector_name", "").fillna("").astype(str)
    out.loc[out["sector_for_distinct"].eq(""), "sector_for_distinct"] = out.get("l3_sector_name", "").fillna("").astype(str)
    return out


def load_g3_final_lots() -> pd.DataFrame:
    d = _read_csv(G3_CLOSED)
    for col in ["entry_date", "policy_exit_date", "context_date", "decision_date"]:
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], errors="coerce").dt.normalize()
    for col in ["net_ret", "policy_net_ret", "score", "router_candidate_rank", "mode_pick_rank"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    if "net_ret" not in d.columns and "policy_net_ret" in d.columns:
        d["net_ret"] = d["policy_net_ret"]
    d = d.dropna(subset=["entry_date", "policy_exit_date", "net_ret"]).copy()
    d["engine"] = "g3_final"
    d["trade_key"] = d.get("trade_key", "").fillna("").astype(str)
    missing_key = d["trade_key"].eq("")
    d.loc[missing_key, "trade_key"] = (
        "g3|"
        + d.loc[missing_key, "entry_date"].dt.strftime("%Y-%m-%d")
        + "|"
        + d.loc[missing_key, "code"].astype(str)
        + "|"
        + d.loc[missing_key, "mode"].astype(str)
    )
    d["rank_key"] = -pd.to_numeric(d.get("mode_pick_rank", 99), errors="coerce").fillna(99) * 1000
    d["rank_key"] += -pd.to_numeric(d.get("router_candidate_rank", 999), errors="coerce").fillna(999)
    d["rank_key"] += pd.to_numeric(d.get("score", 0), errors="coerce").fillna(0)
    if "sector_for_distinct" not in d.columns:
        d["sector_for_distinct"] = ""
    return d[
        [
            "engine",
            "trade_key",
            "code",
            "name",
            "entry_date",
            "policy_exit_date",
            "net_ret",
            "entry_price",
            "score",
            "rank_key",
            "route",
            "mode",
            "exit_reason",
            "sector_for_distinct",
        ]
    ].copy()


def load_market_context() -> pd.DataFrame:
    d = _read_csv(MARKET_CONTEXT)
    d["context_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
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
    d = d[[c for c in keep if c in d.columns]].dropna(subset=["context_date"]).copy()
    for col in d.columns:
        if col not in {"context_date", "market_style", "ma_skeleton", "volume_price_layer", "adx_layer"}:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.sort_values("context_date").reset_index(drop=True)


def attach_previous_context(d: pd.DataFrame, mc: pd.DataFrame) -> pd.DataFrame:
    left = d.sort_values("entry_date").reset_index(drop=True)
    right = mc.sort_values("context_date").reset_index(drop=True)
    out = pd.merge_asof(
        left,
        right,
        left_on="entry_date",
        right_on="context_date",
        direction="backward",
        allow_exact_matches=False,
    )
    out["decision_date"] = out["context_date"]
    return out


def is_down_risk(row: pd.Series) -> bool:
    style = str(row.get("market_style") or "")
    up_rate = _safe_float(row.get("up_rate"), 0.5)
    big_down = _safe_float(row.get("big_down_rate"), 0.0)
    limit_down = _safe_float(row.get("limit_down_proxy_rate"), 0.0)
    mom20 = _safe_float(row.get("mom20"), 0.0)
    return bool((style == "standard_downtrend" and up_rate <= 0.35) or big_down >= 0.20 or limit_down >= 0.03 or mom20 <= -0.08)


def is_rotation_hotspot(row: pd.Series) -> bool:
    style = str(row.get("market_style") or "")
    ma = str(row.get("ma_skeleton") or "")
    volume = str(row.get("volume_price_layer") or "")
    up_rate = _safe_float(row.get("up_rate"), 0.5)
    big_down = _safe_float(row.get("big_down_rate"), 0.0)
    mom20 = _safe_float(row.get("mom20"), 0.0)
    mom60 = _safe_float(row.get("mom60"), 0.0)
    breadth20 = _safe_float(row.get("breadth_ma20"), 0.5)
    range_like = style in {"standard_range", "weak_rebound"} or ma in {"mixed", "weak_repair"}
    diffusion = 0.25 <= up_rate <= 0.62 and big_down < 0.18 and abs(mom20) <= 0.06 and mom60 <= 0.10
    short_cycle = volume in {"neutral_volume_price", "volume_price_repair"} and breadth20 <= 0.58
    return bool(range_like and diffusion and short_cycle)


def _rank_day(d: pd.DataFrame) -> pd.DataFrame:
    out = d.copy()
    out["rank_key"] = pd.to_numeric(out.get("rank_key"), errors="coerce").fillna(-9999)
    out["score"] = pd.to_numeric(out.get("score"), errors="coerce").fillna(-9999)
    return out.sort_values(["rank_key", "score", "code"], ascending=[False, False, True]).drop_duplicates("code", keep="first")


def build_model_candidates(all_lots: pd.DataFrame, model: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[pd.DataFrame] = []
    decisions: list[dict[str, Any]] = []
    by_day = {day: group.copy() for day, group in all_lots.groupby("entry_date")}
    for day, group in by_day.items():
        g2 = _rank_day(group[group["engine"].eq("g2")])
        g3 = _rank_day(group[group["engine"].eq("g3_final")])
        context = group.iloc[0]
        rotation = is_rotation_hotspot(context)
        down_risk = is_down_risk(context)
        selected = pd.DataFrame()
        decision = "NO_TRADE"
        reason = "no_candidate"
        if model == "g2_only":
            selected = g2.head(2)
            decision = "G2"
            reason = "pure_g2"
        elif model == "g3_final_only":
            selected = g3
            decision = "G3"
            reason = "pure_g3_final"
        elif model == "g3_final_with_g2_gap_supplement":
            g3_part = g3.copy()
            g2_part = g2.copy()
            if not g2_part.empty:
                g2_part["route"] = "g2_gap_supplement"
                g2_part["mode"] = "g2_gap_supplement"
                g2_part["route_label"] = "G2空档补位"
                g2_part["supplement_role"] = "g2_gap_fill_after_g3_primary"
                g2_part["confirm_rule"] = "g2_v2_complete_30m_buy_point"
            if not g3_part.empty:
                g3_part["rank_key"] = pd.to_numeric(g3_part.get("rank_key"), errors="coerce").fillna(-9999) + 1_000_000.0
            selected = pd.concat([g3_part, g2_part], ignore_index=True, sort=False)
            decision = "G3+G2补位"
            reason = "g3_primary_fill_unused_slots_with_g2_gap_supplement"
        elif model == "router_g3_priority":
            if down_risk:
                reason = "down_risk_pause"
            elif not g3.empty:
                selected = g3
                decision = "G3"
                reason = "g3_available_priority"
            elif not g2.empty:
                selected = g2.head(2)
                decision = "G2"
                reason = "g3_empty_use_g2"
        elif model == "router_rotation_force_g2":
            if down_risk:
                reason = "down_risk_pause"
            elif rotation and not g2.empty:
                selected = g2.head(2)
                decision = "G2"
                reason = "rotation_hotspot_use_g2"
            elif not g3.empty:
                selected = g3
                decision = "G3"
                reason = "trend_or_mainwave_use_g3"
            elif not g2.empty:
                selected = g2.head(2)
                decision = "G2"
                reason = "fallback_g2"
        elif model == "router_rotation_g2_no_pause":
            if rotation and not g2.empty:
                selected = g2.head(2)
                decision = "G2"
                reason = "rotation_hotspot_use_g2"
            elif not g3.empty:
                selected = g3
                decision = "G3"
                reason = "trend_or_mainwave_use_g3"
            elif not g2.empty:
                selected = g2.head(2)
                decision = "G2"
                reason = "fallback_g2"
        else:
            raise ValueError(model)

        if not selected.empty:
            selected = selected.copy()
            selected["model"] = model
            selected["router_decision"] = decision
            selected["router_reason"] = reason
            selected["rotation_hotspot"] = rotation
            selected["down_risk"] = down_risk
            rows.append(selected)
        decisions.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "model": model,
                "decision": decision,
                "reason": reason,
                "rotation_hotspot": rotation,
                "down_risk": down_risk,
                "g2_candidates": int(len(g2)),
                "g3_candidates": int(len(g3)),
                "selected": int(len(selected)),
                "market_style": str(context.get("market_style") or ""),
                "ma_skeleton": str(context.get("ma_skeleton") or ""),
                "up_rate": _safe_float(context.get("up_rate")),
                "big_down_rate": _safe_float(context.get("big_down_rate")),
                "limit_down_proxy_rate": _safe_float(context.get("limit_down_proxy_rate")),
                "mom20": _safe_float(context.get("mom20")),
                "mom60": _safe_float(context.get("mom60")),
            }
        )
    selected = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=all_lots.columns)
    return selected, pd.DataFrame(decisions)


def same_direction_block(row: dict[str, Any], open_pos: list[dict[str, Any]]) -> bool:
    direction = str(row.get("sector_for_distinct") or "")
    if not direction:
        return False
    for pos in open_pos:
        if str(pos.get("sector_for_distinct") or "") != direction:
            continue
        if row.get("engine") == "g3_final" and pos.get("engine") == "g3_final":
            if str(row.get("mode") or "") == "institutional_mainwave" and str(pos.get("mode") or "") == "institutional_mainwave":
                continue
        return True
    return False


def simulate(candidates: pd.DataFrame, model: str, calendar: list[pd.Timestamp]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    candidates = candidates.copy()
    if not candidates.empty:
        candidates["entry_date"] = pd.to_datetime(candidates["entry_date"], errors="coerce").dt.normalize()
        candidates["policy_exit_date"] = pd.to_datetime(candidates["policy_exit_date"], errors="coerce").dt.normalize()
    by_day = {day: _rank_day(group) for day, group in candidates.groupby("entry_date")} if not candidates.empty else {}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []

    for day in calendar:
        realized_pnl = 0.0
        still_open: list[dict[str, Any]] = []
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                pnl = exit_value - float(pos["stake"])
                cash += exit_value
                realized_pnl += pnl
                out = pos.copy()
                out["exit_date"] = day
                out["exit_value"] = exit_value
                out["realized_pnl"] = pnl
                out["account_ret"] = pnl / float(pos["entry_equity"]) if float(pos["entry_equity"]) else 0.0
                closed.append(out)
            else:
                still_open.append(pos)
        open_pos = still_open

        selected_keys: list[str] = []
        skipped_slot = 0
        skipped_direction = 0
        todays = by_day.get(day)
        if todays is not None and not todays.empty:
            for row in todays.to_dict("records"):
                if len(open_pos) >= MAX_SLOTS:
                    skipped_slot += 1
                    break
                if same_direction_block(row, open_pos):
                    skipped_direction += 1
                    continue
                equity_before = cash + sum(float(pos["stake"]) for pos in open_pos)
                stake = equity_before * SLOT_PCT
                if stake <= 0 or cash + 1e-9 < stake:
                    break
                pos = row.copy()
                pos["stake"] = stake
                pos["entry_equity"] = equity_before
                cash -= stake
                open_pos.append(pos)
                selected_keys.append(str(row.get("trade_key") or ""))

        reserved = sum(float(pos["stake"]) for pos in open_pos)
        equity = cash + reserved
        curve_rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "model": model,
                "cash": cash,
                "reserved_principal": reserved,
                "equity": equity,
                "open_positions": len(open_pos),
                "realized_pnl": realized_pnl,
            }
        )
        decision_rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "model": model,
                "candidate_count": 0 if todays is None else int(len(todays)),
                "opened": len(selected_keys),
                "open_positions_after": len(open_pos),
                "selected_trade_keys": "|".join(selected_keys),
                "skipped_slot": skipped_slot,
                "skipped_direction": skipped_direction,
            }
        )

    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, pd.DataFrame(closed), pd.DataFrame(decision_rows)


def metrics(model: str, curve: pd.DataFrame, closed: pd.DataFrame) -> dict[str, Any]:
    rets = pd.to_numeric(closed.get("net_ret", pd.Series(dtype=float)), errors="coerce").dropna()
    active_days = int((pd.to_numeric(curve.get("open_positions"), errors="coerce").fillna(0) > 0).sum()) if not curve.empty else 0
    full_days = int((pd.to_numeric(curve.get("open_positions"), errors="coerce").fillna(0) >= MAX_SLOTS).sum()) if not curve.empty else 0
    return {
        "model": model,
        "trades": int(len(closed)),
        "return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0) if not curve.empty else 0.0,
        "max_drawdown": _max_drawdown(curve["equity"]) if not curve.empty else 0.0,
        "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
        "avg_trade_return": float(rets.mean()) if len(rets) else 0.0,
        "worst_trade": float(rets.min()) if len(rets) else 0.0,
        "best_trade": float(rets.max()) if len(rets) else 0.0,
        "sum_pnl": float(pd.to_numeric(closed.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum()) if not closed.empty else 0.0,
        "active_days": active_days,
        "full_days": full_days,
        "avg_open_positions_active": float(curve.loc[curve["open_positions"].gt(0), "open_positions"].mean()) if active_days else 0.0,
        "full_days_pct_active": full_days / active_days if active_days else 0.0,
    }


def window_metrics(model: str, curve: pd.DataFrame, closed: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    dcurve = curve.copy()
    dcurve["date_ts"] = pd.to_datetime(dcurve["date"], errors="coerce").dt.normalize()
    dclosed = closed.copy()
    if not dclosed.empty:
        dclosed["entry_date_ts"] = pd.to_datetime(dclosed["entry_date"], errors="coerce").dt.normalize()
    for name, (start, end) in WINDOWS.items():
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        c = dcurve[dcurve["date_ts"].between(s, e)].copy()
        t = dclosed[dclosed["entry_date_ts"].between(s, e)].copy() if not dclosed.empty else pd.DataFrame()
        if c.empty:
            continue
        base = float(c["equity"].iloc[0])
        finish = float(c["equity"].iloc[-1])
        rets = pd.to_numeric(t.get("net_ret", pd.Series(dtype=float)), errors="coerce").dropna()
        rows.append(
            {
                "model": model,
                "window": name,
                "start": c["date"].iloc[0],
                "end": c["date"].iloc[-1],
                "trades": int(len(t)),
                "return": finish / base - 1.0 if base else 0.0,
                "max_drawdown": _max_drawdown(c["equity"]),
                "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
                "avg_trade_return": float(rets.mean()) if len(rets) else 0.0,
                "worst_trade": float(rets.min()) if len(rets) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def write_report(summary: pd.DataFrame, windows: pd.DataFrame, router_decisions: pd.DataFrame, meta: dict[str, Any]) -> None:
    pct_cols = {
        "return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "best_trade",
        "full_days_pct_active",
    }
    rotation_counts = (
        router_decisions.groupby(["model", "decision", "reason"], dropna=False)
        .size()
        .reset_index(name="days")
        .sort_values(["model", "days"], ascending=[True, False])
    )
    lines = [
        "# G2/G3 行情风格路由回测 v1",
        "",
        "## 结论先看",
        "",
        "- 本报告把 G2 官方买点与最终版 G3 放到同一个二槽、单槽 50% 的组合回放框架中比较。",
        "- 路由只使用买入日前一交易日市场环境快照，避免使用入场日收盘后的未来信息。",
        "- `router_rotation_force_g2` 是用户提出的核心假设：震荡轮动、题材扩散、短周期热点时切到 G2 买点，其余优先最终版 G3。",
        "- 这是研究回测，不会改动当前 shadow/live 合同；如果要进入正式 G3，还需要把路由判断写入每日候选/页面/影子台账。",
        "",
        "## 全周期同口径对比",
        "",
        _md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口对比",
        "",
        _md_table(windows.sort_values(["window", "return"], ascending=[True, False]), pct_cols=pct_cols, max_rows=120),
        "",
        "## 路由触发分布",
        "",
        _md_table(rotation_counts, max_rows=120),
        "",
        "## 文件",
        "",
        f"- 输出目录：`{meta['out_dir']}`",
        "- `summary.csv`：全周期核心指标。",
        "- `window_summary.csv`：各窗口收益、回撤、胜率。",
        "- `all_router_decisions.csv`：每天每个路由模型的判断。",
        "- `*_closed_trades.csv` / `*_equity_curve.csv`：每个模型的成交与权益曲线。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    g2 = load_g2_lots()
    g3 = load_g3_final_lots()
    lots = pd.concat([g2, g3], ignore_index=True, sort=False)
    market_context = load_market_context()
    lots = attach_previous_context(lots, market_context)
    end = max(END_FLOOR, pd.to_datetime(lots["policy_exit_date"], errors="coerce").max())
    calendar = _trade_calendar(START_DATE, end)
    if not calendar:
        raise RuntimeError("empty trade calendar")

    models = [
        "g2_only",
        "g3_final_only",
        "g3_final_with_g2_gap_supplement",
        "router_g3_priority",
        "router_rotation_force_g2",
        "router_rotation_g2_no_pause",
    ]
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[pd.DataFrame] = []
    all_router_decisions: list[pd.DataFrame] = []
    all_daily_decisions: list[pd.DataFrame] = []
    for model in models:
        selected, router_decisions = build_model_candidates(lots, model)
        curve, closed, daily = simulate(selected, model, calendar)
        selected.to_csv(OUT_DIR / f"{model}_selected_candidates.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(OUT_DIR / f"{model}_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(OUT_DIR / f"{model}_closed_trades.csv", index=False, encoding="utf-8-sig")
        daily.to_csv(OUT_DIR / f"{model}_daily_decisions.csv", index=False, encoding="utf-8-sig")
        all_router_decisions.append(router_decisions)
        all_daily_decisions.append(daily)
        summary_rows.append(metrics(model, curve, closed))
        window_rows.append(window_metrics(model, curve, closed))

    summary = pd.DataFrame(summary_rows).sort_values("return", ascending=False)
    windows = pd.concat(window_rows, ignore_index=True).sort_values(["window", "return"], ascending=[True, False])
    router_decisions = pd.concat(all_router_decisions, ignore_index=True)
    daily_decisions = pd.concat(all_daily_decisions, ignore_index=True)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_summary.csv", index=False, encoding="utf-8-sig")
    router_decisions.to_csv(OUT_DIR / "all_router_decisions.csv", index=False, encoding="utf-8-sig")
    daily_decisions.to_csv(OUT_DIR / "all_daily_decisions.csv", index=False, encoding="utf-8-sig")
    lots.to_csv(OUT_DIR / "combined_trade_lots_with_prev_context.csv", index=False, encoding="utf-8-sig")
    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "out_dir": str(OUT_DIR),
        "g2_lots": int(len(g2)),
        "g3_lots": int(len(g3)),
        "combined_lots": int(len(lots)),
        "calendar_days": int(len(calendar)),
        "router_note": "route_health not used; previous-day market context only",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(summary, windows, router_decisions, meta)
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
