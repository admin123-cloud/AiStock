from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default, _load_trade_dates  # noqa: E402
from scripts.gen2_build_risk_cool_shadow_ledger import _build_ledger, _summarize, _write_report  # noqa: E402
from scripts.gen2_candidate_outcome_supervision import FEATURES, _prepare_source  # noqa: E402
from scripts.gen2_filter_signals_by_intraday_normal import DEFAULT_STATE_DAILY, _build_intraday_state, _filter_with_state, _load_prev_daily_state_from_clickhouse  # noqa: E402
from scripts.gen2_preference_filters import apply_user_v2_filters, load_share_cap_cache, summarize_filter_result  # noqa: E402
from scripts.gen2_realtime_candidate_recall_study import _attach_realtime_features  # noqa: E402
from scripts.gen2_test_30m_fractal_restart import DEFAULT_EVENT_DATASET, _find_fractal_triggers, _load_minute_bars  # noqa: E402
from scripts.gen2_validate_v2_trend_continuation import _load_signal_bars as _load_breakout_signal_bars  # noqa: E402
from scripts.collect_intraday_snapshots import load_snapshot_minute_bars  # noqa: E402
from scripts.gen2_backtest_risk_cool_dynamic_circuit import _base_mask, _load_filter_daily  # noqa: E402
from api.gen2_factor import _add_factor, _estimate_prewarm_days, _load_daily_ohlcv, build_gen2_factor_registry  # noqa: E402
from utils.market_warehouse import clickhouse_client  # noqa: E402


DEFAULT_SOURCE_CSV = ROOT / "reports" / "gen2_risk_cool_dynamic_circuit_user_v2_cap_v2" / "sources" / "risk_cool_base.csv"
DEFAULT_SOURCE_PARQUET = DEFAULT_SOURCE_CSV.with_suffix(".parquet")
DEFAULT_RUN_DIR = ROOT / "reports" / "gen2_risk_cool_dynamic_circuit_user_v2_cap_v2" / "backtests" / "two_stop_cd3_skip"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_risk_cool_shadow_ledger"
DEFAULT_SHARE_CAP_CACHE = ROOT / "data" / "runtime" / "tdx_share_cap_history.parquet"
DEFAULT_ALPHA191_TRAIN_SOURCE = ROOT / "reports" / "gen2_alpha191_overlay_candidate_train_dirs" / "sources" / "risk_cool_base.parquet"
DEFAULT_ALPHA191_TRAIN_VALUES = ROOT / "reports" / "gen2_alpha191_candidate_core10_t1" / "alpha191_core10_t1_signal_values.parquet"
DEFAULT_MAINLINE_THEME_ADDON = ROOT / "reports" / "mainline_theme_strategy_overlay_v1" / "theme_addon.csv"
ALPHA191_VOLUME5_FACTORS = ["Alpha150", "Alpha070", "Alpha095", "Alpha132", "Alpha144"]
ALPHA191_VOLUME5_DIRECTIONS = {
    "Alpha150": "low",
    "Alpha070": "low",
    "Alpha095": "low",
    "Alpha132": "low",
    "Alpha144": "high",
}
ALPHA191_VOLUME5_GATES = {"volume5_keep80_runup"}
ALPHA191_ACTIVE_GATES = {"off", "volume5_keep80_runup", "g2_v2_complete"}
ALPHA191_MAIN_THRESHOLD_Q = 0.20
LIVE_OUTPUT_COLUMNS = [
    "entry_date",
    "code",
    "name",
    "confirm_datetime",
    "entry_price",
    "v4_rank",
    "v4_score",
    "mom5",
    "mom10",
    "mom20",
    "vol_ratio",
    "vol10",
    "rt_return_from_d1_close",
    "rt_30m_amount_ratio",
    "realtime_pool",
    "alpha191_volume5_score",
    "alpha191_volume5_rank_in_day",
    "alpha191_original_v4_rank",
    "runup_from_60d_low",
    "overhead_pressure_amount_share",
    "source_family",
    "signal_family",
    "g2_v2_buy_logic",
    "g2_v2_family_priority",
    "sector_strong",
    "sector_score_bonus",
    "mainline_theme_match",
    "mainline_theme_level",
    "mainline_theme_label",
    "mainline_theme_action",
    "mainline_theme_weight_hint",
    "mainline_theme_score",
    "mainline_theme_risk_label",
    "mainline_theme_position_cap_hint",
    "mainline_theme_usage",
    "l3_rt_strong3_ratio",
    "l3_s3",
    "rt_breakout_vs_box_top",
    "setup_big_bull_rebreak_2_5d_top",
]


def _latest_daily_date() -> str:
    ch = clickhouse_client()
    df = ch.query_df("SELECT max(trade_date) AS max_date FROM kline_daily")
    if df.empty or pd.isna(df.iloc[0]["max_date"]):
        return ""
    return pd.Timestamp(df.iloc[0]["max_date"]).strftime("%Y-%m-%d")


def _load_events(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    d = pd.read_parquet(path)
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = d[(d["trade_date"] >= start_date) & (d["trade_date"] <= end_date)].copy()
    for col in ["v4_rank", "v4_score", "close", "mom5", "mom10", "mom20", "vol_ratio", "vol10", "amt20"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.reset_index(drop=True)


def _build_live_contexts(events: pd.DataFrame, signal_date: str, trade_dates: list[str], pool_rank: int) -> pd.DataFrame:
    if signal_date not in trade_dates:
        return pd.DataFrame()
    idx = trade_dates.index(signal_date)
    if idx <= 0:
        return pd.DataFrame()
    prev_date = trade_dates[idx - 1]
    base = events[events["trade_date"].eq(prev_date)].copy()
    if base.empty:
        return base
    rank = pd.to_numeric(base.get("v4_rank"), errors="coerce")
    base = base[rank.le(int(pool_rank))].copy()
    if base.empty:
        return base
    base["entry_date"] = signal_date
    base["entry_start_date"] = signal_date
    base["search_end_date"] = signal_date
    base["entry_start_idx"] = idx
    base["search_end_idx"] = idx
    base["trade_idx"] = idx - 1
    base["g2_open_state"] = "D1_CONTEXT"
    base["ctx_pullback_restart_rank100"] = True
    return base.reset_index(drop=True)


def _make_live_candidates(event_dataset: Path, state_daily: Path, signal_date: str, pool_rank: int) -> pd.DataFrame:
    trade_dates = _load_trade_dates("2024-07-09", signal_date)
    if signal_date not in trade_dates:
        return pd.DataFrame()
    prev_date = trade_dates[trade_dates.index(signal_date) - 1]
    events = _load_events(event_dataset, prev_date, signal_date)
    contexts = _build_live_contexts(events, signal_date, trade_dates, pool_rank)
    if contexts.empty:
        return contexts
    codes = contexts["code"].dropna().astype(str).unique().tolist()
    minute_bars = _load_minute_bars(codes, prev_date, signal_date)
    triggers = _find_fractal_triggers(contexts, minute_bars)
    if triggers.empty:
        return triggers
    triggers = triggers[triggers["trigger_type"].eq("bottom_fractal_break_high_vol")].copy()
    triggers["entry_date"] = pd.to_datetime(triggers["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    triggers = triggers[triggers["entry_date"].eq(signal_date)].copy()
    if triggers.empty:
        return triggers
    state = _build_intraday_state(triggers, state_daily, 30)
    filtered = _filter_with_state(triggers, state, "before_confirm")
    if filtered.empty:
        return filtered
    filtered = filtered[filtered["trigger_type"].eq("bottom_fractal_break_high_vol")].copy()
    filtered["realtime_pool"] = f"d1_rank{int(pool_rank)}"
    filtered["is_same_day_target"] = False
    return _attach_realtime_features(filtered, set()).replace([np.inf, -np.inf], np.nan)


def _apply_live_filters(candidates: pd.DataFrame, start_date: str, end_date: str, share_cap_cache: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    if candidates.empty:
        return candidates.copy(), {"signals_before": 0, "signals_after": 0}
    d = candidates.copy()
    for col in FEATURES + ["entry_price"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    selected = d[_base_mask(d).fillna(False)].copy()
    summary: dict[str, Any] = {
        "live_candidates": int(len(d)),
        "after_risk_cool_base": int(len(selected)),
        "share_cap_cache": str(share_cap_cache),
    }
    if selected.empty:
        summary.update({"signals_before": 0, "signals_after": 0})
        return selected, summary
    codes = selected["code"].dropna().astype(str).unique().tolist()
    daily = _load_filter_daily(codes, start_date, end_date)
    share_cap = load_share_cap_cache(share_cap_cache)
    audited = apply_user_v2_filters(selected, daily=daily, share_cap=share_cap, require_cap_data=False)
    filter_summary = summarize_filter_result(audited)
    summary.update(filter_summary)
    passed = audited[audited["user_v2_pass"].fillna(False)].copy()
    return _prepare_source(passed), summary


def _alpha191_factor_meta(factors: list[str] | None = None) -> list[dict[str, Any]]:
    lookup = {str(item.get("id")): item for item in build_gen2_factor_registry().get("factors") or []}
    metas = []
    for fid in factors or ALPHA191_VOLUME5_FACTORS:
        meta = lookup.get(fid)
        if not meta or meta.get("backend_status") != "implemented":
            raise RuntimeError(f"Alpha191 gate factor unavailable: {fid}")
        metas.append(meta)
    return metas


def _train_percentile(values: pd.Series, train_values: pd.Series) -> pd.Series:
    base = pd.to_numeric(train_values, errors="coerce").dropna().sort_values().to_numpy(dtype=float)
    if len(base) == 0:
        return pd.Series(np.nan, index=values.index)
    raw = pd.to_numeric(values, errors="coerce")
    pct = np.searchsorted(base, raw.to_numpy(dtype=float), side="right") / float(len(base))
    out = pd.Series(pct, index=values.index, dtype=float)
    out.loc[raw.isna()] = np.nan
    return out


def _load_alpha191_train_frame(source_path: Path, values_path: Path, train_start: str, train_end: str) -> pd.DataFrame:
    if not source_path.exists() or not values_path.exists():
        raise RuntimeError("Alpha191 train source or T-1 values file is missing.")
    source = pd.read_parquet(source_path)
    values = pd.read_parquet(values_path)
    source["date"] = pd.to_datetime(source["entry_date"], errors="coerce")
    source["code6"] = source["code"].astype(str).str[:6]
    values["date"] = pd.to_datetime(values["date"], errors="coerce")
    values["code6"] = values["code6"].astype(str).str[:6]
    merged = source[["date", "code6"]].merge(values, on=["date", "code6"], how="left")
    return merged[(merged["date"] >= pd.Timestamp(train_start)) & (merged["date"] <= pd.Timestamp(train_end))].copy()


def _load_live_alpha191_values(candidates: pd.DataFrame, signal_date: str, factors: list[str] | None = None) -> tuple[pd.DataFrame, str]:
    if candidates.empty:
        return pd.DataFrame(), ""
    trade_dates = _load_trade_dates("2024-07-09", signal_date)
    if signal_date not in trade_dates:
        return pd.DataFrame(), ""
    idx = trade_dates.index(signal_date)
    if idx <= 0:
        return pd.DataFrame(), ""
    factor_date = trade_dates[idx - 1]
    metas = _alpha191_factor_meta(factors)
    prewarm = max(_estimate_prewarm_days(meta) for meta in metas)
    daily = _load_daily_ohlcv(factor_date, factor_date, horizon=1, prewarm_days=prewarm)
    if daily.empty:
        return pd.DataFrame(), factor_date
    codes = candidates["code"].dropna().astype(str).str[:6].unique().tolist()
    out = pd.DataFrame({"code6": codes})
    out["date"] = pd.Timestamp(signal_date)
    out["factor_date"] = pd.Timestamp(factor_date)
    for meta in metas:
        fid = str(meta.get("id"))
        factored = _add_factor(daily, meta)
        slim = factored[pd.to_datetime(factored["date"], errors="coerce").dt.strftime("%Y-%m-%d").eq(factor_date)].copy()
        slim["code6"] = slim["code"].astype(str).str[:6]
        slim = slim[["code6", "factor_value"]].rename(columns={"factor_value": fid})
        out = out.merge(slim, on="code6", how="left")
    return out, factor_date


def _add_alpha191_percentile_scores(
    d: pd.DataFrame,
    train: pd.DataFrame,
    factors: list[str],
    directions: dict[str, str],
    score_name: str,
) -> pd.DataFrame:
    out = d.copy()
    pct_cols: list[str] = []
    for fid in factors:
        if fid not in train.columns or fid not in out.columns:
            raise RuntimeError(f"Missing Alpha191 field for live score: {fid}")
        direction = directions.get(fid, "high")
        train_raw = pd.to_numeric(train.get(fid), errors="coerce")
        live_raw = pd.to_numeric(out.get(fid), errors="coerce")
        train_oriented = train_raw if direction == "high" else -train_raw
        live_oriented = live_raw if direction == "high" else -live_raw
        pct_col = f"{fid}_train_pct"
        value_col = f"alpha191_{fid.lower()}_value"
        out[value_col] = live_raw
        out[pct_col] = _train_percentile(live_oriented, train_oriented)
        pct_cols.append(pct_col)
    out[f"alpha191_{score_name}_score"] = out[pct_cols].mean(axis=1)
    out[f"alpha191_{score_name}_valid_count"] = out[pct_cols].notna().sum(axis=1)
    return out


def _train_composite_threshold(train: pd.DataFrame, factors: list[str], directions: dict[str, str], q: float) -> float:
    tmp = pd.DataFrame(index=train.index)
    for fid in factors:
        if fid not in train.columns:
            raise RuntimeError(f"Missing Alpha191 train field: {fid}")
        direction = directions.get(fid, "high")
        raw = pd.to_numeric(train.get(fid), errors="coerce")
        oriented = raw if direction == "high" else -raw
        tmp[f"{fid}_train_pct"] = _train_percentile(oriented, oriented)
    scores = tmp.mean(axis=1).dropna()
    return float(scores.quantile(q)) if not scores.empty else np.nan


def _apply_alpha191_gate(
    signals: pd.DataFrame,
    signal_date: str,
    gate: str,
    train_source: Path,
    train_values: Path,
    train_start: str,
    train_end: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    gate = str(gate or "off").strip().lower()
    if gate in {"", "off", "none", "false"}:
        return signals.copy(), {"enabled": False, "gate": "off", "signals_before": int(len(signals)), "signals_after": int(len(signals))}
    if gate not in ALPHA191_ACTIVE_GATES:
        raise RuntimeError(f"Unsupported Alpha191 gate: {gate}")
    if signals.empty:
        return signals.copy(), {"enabled": True, "gate": gate, "signals_before": 0, "signals_after": 0}

    train = _load_alpha191_train_frame(train_source, train_values, train_start, train_end)
    d = signals.copy()
    d["code6"] = d["code"].astype(str).str[:6]
    factors = ALPHA191_VOLUME5_FACTORS
    directions = ALPHA191_VOLUME5_DIRECTIONS
    live_values, factor_date = _load_live_alpha191_values(signals, signal_date, factors=factors)
    d = d.merge(live_values.drop(columns=["date"], errors="ignore"), on="code6", how="left")
    score_name = "volume5"
    d = _add_alpha191_percentile_scores(d, train, factors, directions, score_name)
    score_col = f"alpha191_{score_name}_score"
    d["alpha191_gate_score"] = d[score_col]
    threshold = _train_composite_threshold(train, factors, directions, ALPHA191_MAIN_THRESHOLD_Q)
    d["alpha191_gate_enabled"] = True
    d["alpha191_gate_variant"] = gate
    d["alpha191_factor_date"] = factor_date
    d["alpha191_gate_threshold"] = threshold
    d["alpha191_original_v4_rank"] = pd.to_numeric(d.get("alpha191_original_v4_rank", d.get("v4_rank")), errors="coerce")
    d["alpha191_original_v4_score"] = pd.to_numeric(d.get("alpha191_original_v4_score", d.get("v4_score")), errors="coerce")
    d["alpha191_volume5_rank_in_day"] = (
        d.groupby("entry_date")["alpha191_volume5_score"]
        .rank(method="first", ascending=False, na_option="bottom")
        .fillna(999)
        .astype(int)
    )
    d["v4_score"] = pd.to_numeric(d["alpha191_volume5_score"], errors="coerce").fillna(-1.0)
    d["v4_rank"] = d["alpha191_volume5_rank_in_day"]
    pass_mask = d["alpha191_volume5_score"].notna()
    pass_mask &= d["alpha191_volume5_score"].ge(threshold)
    pass_mask &= pd.to_numeric(d.get("runup_from_60d_low"), errors="coerce").fillna(99.0).le(1.00)
    d["alpha191_gate_pass"] = pass_mask
    d["alpha191_gate_reason"] = np.where(
        d["alpha191_gate_pass"],
        "Alpha191 T-1 gate pass",
        "Alpha191 T-1 gate filtered",
    )
    passed = d[d["alpha191_gate_pass"].fillna(False)].drop(columns=["code6"]).copy()
    summary = {
        "enabled": True,
        "gate": gate,
        "factors": factors,
        "directions": directions,
        "train_start": train_start,
        "train_end": train_end,
        "factor_date": factor_date,
        "threshold_q": ALPHA191_MAIN_THRESHOLD_Q,
        "threshold": threshold,
        "signals_before": int(len(signals)),
        "signals_after": int(len(passed)),
        "filtered": int(len(signals) - len(passed)),
    }
    return passed, summary


def _code6(value: Any) -> str:
    return str(value or "").split(".")[0].zfill(6)[-6:]


def _load_mainline_theme_addon(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        addon = pd.read_csv(path, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    if addon.empty or "code6" not in addon.columns:
        return pd.DataFrame()
    addon = addon.copy()
    addon["code6"] = addon["code6"].map(_code6)
    rename = {
        "theme_label": "mainline_theme_label",
        "observation_level": "mainline_theme_level",
        "theme_overlay_action": "mainline_theme_action",
        "theme_weight_hint": "mainline_theme_weight_hint",
        "theme_candidate_score": "mainline_theme_score",
        "risk_label": "mainline_theme_risk_label",
        "position_cap_hint": "mainline_theme_position_cap_hint",
        "suggested_usage": "mainline_theme_usage",
    }
    keep = ["code6"] + [col for col in rename if col in addon.columns]
    addon = addon[keep].rename(columns=rename)
    for col in ["mainline_theme_weight_hint", "mainline_theme_score"]:
        if col in addon.columns:
            addon[col] = pd.to_numeric(addon[col], errors="coerce")
    return addon.drop_duplicates("code6", keep="first")


def _attach_mainline_theme_overlay(signals: pd.DataFrame, addon_path: Path) -> pd.DataFrame:
    if signals.empty:
        out = signals.copy()
        for col in [
            "mainline_theme_match",
            "mainline_theme_level",
            "mainline_theme_label",
            "mainline_theme_action",
            "mainline_theme_weight_hint",
            "mainline_theme_score",
            "mainline_theme_risk_label",
            "mainline_theme_position_cap_hint",
            "mainline_theme_usage",
        ]:
            if col not in out.columns:
                out[col] = pd.Series(dtype=object)
        return out
    addon = _load_mainline_theme_addon(addon_path)
    out = signals.copy()
    out["code6"] = out["code"].map(_code6)
    if addon.empty:
        out["mainline_theme_match"] = False
        out["mainline_theme_weight_hint"] = 0.0
        return out.drop(columns=["code6"], errors="ignore")
    out = out.drop(
        columns=[
            "mainline_theme_match",
            "mainline_theme_level",
            "mainline_theme_label",
            "mainline_theme_action",
            "mainline_theme_weight_hint",
            "mainline_theme_score",
            "mainline_theme_risk_label",
            "mainline_theme_position_cap_hint",
            "mainline_theme_usage",
        ],
        errors="ignore",
    )
    out = out.merge(addon, on="code6", how="left")
    out["mainline_theme_match"] = out["mainline_theme_level"].notna()
    out["mainline_theme_weight_hint"] = pd.to_numeric(out.get("mainline_theme_weight_hint"), errors="coerce").fillna(0.0)
    return out.drop(columns=["code6"], errors="ignore")


def _sector_membership() -> pd.DataFrame:
    ch = clickhouse_client()
    df = ch.query_df(
        """
        SELECT ss.stock_code, s.code AS sector_code, s.name AS sector_name, s.level, s.stock_count
        FROM sector_stocks ss
        JOIN sectors s ON ss.sector_code = s.code
        WHERE s.type = 'industry'
        """
    )
    if df.empty:
        return df
    df["stock_code6"] = df["stock_code"].map(_code6)
    df["level"] = pd.to_numeric(df["level"], errors="coerce")
    df["stock_count"] = pd.to_numeric(df["stock_count"], errors="coerce")
    return df


def _sector_intraday_stats(members: pd.DataFrame, sector_code: str, trade_date: str, confirm_time: str) -> dict[str, Any]:
    codes = members.loc[members["sector_code"].astype(str).eq(str(sector_code)), "stock_code"].dropna().astype(str).unique().tolist()
    if not codes:
        return {}
    quoted = ", ".join(f"'{code}'" for code in sorted(set(codes)))
    dt = f"{trade_date} {confirm_time}:00"
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT m.code, m.close AS intraday_close, d.close AS prev_close
        FROM
        (
            SELECT code, close
            FROM kline_minute_30
            WHERE code IN ({quoted})
              AND datetime = toDateTime('{dt}')
        ) m
        INNER JOIN
        (
            SELECT code, close
            FROM kline_daily
            WHERE code IN ({quoted})
              AND trade_date = (
                  SELECT max(trade_date)
                  FROM kline_daily
                  WHERE code IN ({quoted}) AND trade_date < toDate('{trade_date}')
              )
        ) d ON m.code = d.code
        """
    )
    if df.empty:
        return {}
    df["intraday_close"] = pd.to_numeric(df["intraday_close"], errors="coerce")
    df["prev_close"] = pd.to_numeric(df["prev_close"], errors="coerce")
    df = df[(df["prev_close"] > 0) & df["intraday_close"].notna()].copy()
    if df.empty:
        return {}
    df["rt_ret"] = df["intraday_close"] / df["prev_close"] - 1.0
    return {
        "member_bars": int(len(df)),
        "avg_return": float(df["rt_ret"].mean()),
        "rise_ratio": float((df["rt_ret"] > 0).mean()),
        "strong3_ratio": float((df["rt_ret"] >= 0.03).mean()),
        "strong5_ratio": float((df["rt_ret"] >= 0.05).mean()),
        "top_return": float(df["rt_ret"].max()),
    }


def _attach_sector_context(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty:
        return signals.copy()
    members = _sector_membership()
    if members.empty:
        return signals.copy()
    m = members.sort_values(["stock_code6", "level", "stock_count"], ascending=[True, True, True])
    sector_map = {
        (str(row.stock_code6), int(row.level)): row
        for row in m.itertuples(index=False)
        if pd.notna(row.level)
    }
    rows: list[dict[str, Any]] = []
    work = signals.copy()
    work["code6"] = work["code"].map(_code6)
    work["entry_date"] = pd.to_datetime(work["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    work["confirm_datetime"] = pd.to_datetime(work["confirm_datetime"], errors="coerce")
    work["confirm_time"] = work["confirm_datetime"].dt.strftime("%H:%M")
    for row in work.itertuples(index=False):
        out = row._asdict()
        for level in [1, 2, 3]:
            sec = sector_map.get((out["code6"], level))
            prefix = f"l{level}"
            if sec is None:
                continue
            out[f"{prefix}_sector_code"] = sec.sector_code
            out[f"{prefix}_sector_name"] = sec.sector_name
            out[f"{prefix}_sector_stock_count"] = int(sec.stock_count) if pd.notna(sec.stock_count) else None
            stats = _sector_intraday_stats(members, str(sec.sector_code), out["entry_date"], out["confirm_time"])
            for key, value in stats.items():
                out[f"{prefix}_rt_{key}"] = value
        rows.append(out)
    out_df = pd.DataFrame(rows)
    out_df["confirm_datetime"] = pd.to_datetime(out_df["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    out_df["l3_s3"] = pd.to_numeric(out_df.get("l3_rt_strong3_ratio"), errors="coerce").fillna(-1.0)
    out_df["sector_strong"] = out_df["l3_s3"] >= 0.05
    return out_df


def _prepare_live_volume5_mainline(
    signals: pd.DataFrame,
    signal_date: str,
    train_source: Path,
    train_values: Path,
    train_start: str,
    train_end: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    passed, summary = _apply_alpha191_gate(
        signals,
        signal_date=signal_date,
        gate="volume5_keep80_runup",
        train_source=train_source,
        train_values=train_values,
        train_start=train_start,
        train_end=train_end,
    )
    if passed.empty:
        return passed, summary
    out = _attach_sector_context(passed)
    out["source_family"] = "volume5"
    out["signal_family"] = "volume5_keep80_runup_sector_bonus"
    out["g2_v2_family_priority"] = 0
    out["sector_score_bonus"] = np.where(out["sector_strong"].fillna(False), 0.05, 0.0)
    out["v4_score_raw"] = pd.to_numeric(out.get("v4_score"), errors="coerce")
    out["v4_score"] = out["v4_score_raw"].fillna(0.0) + out["sector_score_bonus"]
    out["g2_v2_buy_logic"] = np.where(
        out["sector_strong"].fillna(False),
        "volume5_keep80_runup + sector_score_bonus",
        "volume5_keep80_runup",
    )
    summary["signals_after_sector_bonus"] = int(len(out))
    summary["sector_strong_signals"] = int(out["sector_strong"].fillna(False).sum())
    return out, summary


def _load_daily_for_breakout(codes: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    quoted = ", ".join(f"'{code}'" for code in sorted(set(codes)))
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT code, trade_date, open, high, low, close, volume, amount
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY code, trade_date
        """
    )
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["code", "trade_date", "close"]).reset_index(drop=True)


def _load_g2_open_states(path: Path) -> pd.DataFrame:
    states = pd.read_csv(path)
    states["trade_date"] = pd.to_datetime(states["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return states[["trade_date", "g2_open_state"]].dropna(subset=["trade_date"]).copy()


def _prepare_live_signal_bars(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out["entry_date"] = out["datetime"].dt.strftime("%Y-%m-%d")
    out["bar_time"] = out["datetime"].dt.strftime("%H:%M:%S")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        out[col] = pd.to_numeric(out.get(col), errors="coerce")
    out["amount"] = out["amount"].where(out["amount"] > 0, out["volume"] * out["close"])
    out = out.dropna(subset=["code", "datetime", "open", "high", "low", "close", "amount"]).copy()
    if out.empty:
        return out
    out = out.sort_values(["code", "datetime"]).reset_index(drop=True)
    group = out.groupby(["code", "entry_date"], sort=False)
    out["prev_intraday_high"] = group["high"].cummax().groupby([out["code"], out["entry_date"]]).shift(1)
    out["prev_bar_high"] = group["high"].shift(1)
    out["amount_ma5_prev"] = out.groupby("code")["amount"].transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())
    first2 = group.cumcount() < 2
    open_range = out[first2].groupby(["code", "entry_date"])["high"].max().rename("open_range_high").reset_index()
    out = out.merge(open_range, on=["code", "entry_date"], how="left")
    return out.reset_index(drop=True)


def _load_live_breakout_signal_bars(codes: list[str], start_date: str, end_date: str, period: int) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    try:
        base = _load_breakout_signal_bars(codes, start_date, end_date, period)
        if base is not None and not base.empty:
            base = base.copy()
            base["bar_source"] = "kline_minute"
            frames.append(base)
    except Exception as exc:
        print(f"[WARN] load kline_minute_{period} breakout bars failed: {exc}", file=sys.stderr)
    try:
        snap = load_snapshot_minute_bars(codes, start_date, end_date, period, asset_type="stock")
        if snap is not None and not snap.empty:
            snap = snap.copy()
            snap["bar_source"] = "intraday_quote_snapshot"
            frames.append(snap)
    except Exception as exc:
        print(f"[WARN] load snapshot breakout bars failed: {exc}", file=sys.stderr)
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True, sort=False)
    merged["datetime"] = pd.to_datetime(merged["datetime"], errors="coerce")
    source_rank = {"kline_minute": 0, "intraday_quote_snapshot": 1}
    merged["_source_rank"] = merged.get("bar_source", "").map(source_rank).fillna(9)
    merged = merged.sort_values(["code", "datetime", "_source_rank"]).drop_duplicates(["code", "datetime"], keep="first")
    merged = merged.drop(columns=[col for col in ["_source_rank"] if col in merged.columns])
    return _prepare_live_signal_bars(merged)


def _apply_g2_open_state_fallback(events: pd.DataFrame) -> pd.DataFrame:
    out = events.copy()
    if "g2_open_state" not in out.columns:
        out["g2_open_state"] = pd.NA
    state_text = out["g2_open_state"].fillna("").astype(str).str.strip()
    missing = state_text.eq("") | state_text.str.lower().isin({"nan", "none"})
    if "legacy_regime" in out.columns:
        legacy_ok = out["legacy_regime"].fillna("").astype(str).isin({"trend_up", "range"})
    else:
        legacy_ok = pd.Series(False, index=out.index)
    if "index_close_ge_ma20" in out.columns:
        index_ok = out["index_close_ge_ma20"].fillna(False).astype(bool)
    else:
        index_ok = pd.Series(False, index=out.index)
    fallback = missing & legacy_ok & index_ok
    out.loc[fallback, "g2_open_state"] = "NORMAL"
    out["g2_open_state_source"] = np.where(fallback, "fallback_legacy_regime_index_ma20", "state_daily")
    return out


def _attach_g2_open_state(events: pd.DataFrame, state_daily: Path, signal_date: str) -> pd.DataFrame:
    out = events.merge(_load_g2_open_states(Path(state_daily)), on="trade_date", how="left")
    state_text = out.get("g2_open_state", pd.Series("", index=out.index)).fillna("").astype(str).str.strip()
    missing = state_text.eq("") | state_text.str.lower().isin({"nan", "none"})
    if missing.any():
        try:
            intraday_state = _load_prev_daily_state_from_clickhouse(signal_date, signal_date)
            if intraday_state is not None and not intraday_state.empty:
                fallback = intraday_state[["trade_date", "g2_open_state"]].dropna(subset=["trade_date"]).copy()
                fallback = fallback.rename(columns={"g2_open_state": "g2_open_state_live"})
                out = out.merge(fallback, on="trade_date", how="left")
                live_state = out.get("g2_open_state_live", pd.Series("", index=out.index)).fillna("").astype(str).str.strip()
                out.loc[missing & live_state.ne(""), "g2_open_state"] = live_state[missing & live_state.ne("")]
                out = out.drop(columns=[col for col in ["g2_open_state_live"] if col in out.columns])
        except Exception as exc:
            print(f"[WARN] load clickhouse g2 open state fallback failed: {exc}", file=sys.stderr)
    return _apply_g2_open_state_fallback(out)


def build_g2_promotion_snapshot_codes(
    signal_date: str,
    pool_rank: int = 200,
    event_dataset: Path = DEFAULT_EVENT_DATASET,
    limit: int = 300,
) -> list[dict[str, Any]]:
    trade_dates = _load_trade_dates("2024-07-09", signal_date)
    if signal_date not in trade_dates:
        return []
    idx = trade_dates.index(signal_date)
    if idx <= 0:
        return []
    prev_date = trade_dates[idx - 1]
    events = _load_events(Path(event_dataset), prev_date, prev_date)
    if events.empty:
        return []
    for col in ["entry_pass", "in_score_pool"]:
        if col in events.columns:
            events[col] = events[col].fillna(False).astype(bool)
    events = _attach_g2_open_state(events, DEFAULT_STATE_DAILY, signal_date)
    rank = pd.to_numeric(events.get("v4_rank"), errors="coerce")
    pool = events.get("in_score_pool", False).fillna(False).astype(bool)
    mask = (
        pool
        & rank.le(min(int(pool_rank), 200))
        & pd.to_numeric(events.get("mom10"), errors="coerce").ge(0.02)
        & pd.to_numeric(events.get("mom20"), errors="coerce").ge(0.03)
        & pd.to_numeric(events.get("mom5"), errors="coerce").le(0.12)
        & pd.to_numeric(events.get("vol_ratio"), errors="coerce").le(3.0)
        & pd.to_numeric(events.get("vol10"), errors="coerce").le(0.10)
    )
    cols = [col for col in ["code", "name", "v4_rank", "v4_score"] if col in events.columns]
    out = events.loc[mask, cols].copy()
    if out.empty:
        return []
    out["v4_rank"] = pd.to_numeric(out.get("v4_rank"), errors="coerce").fillna(9999)
    out = out.sort_values(["v4_rank", "code"]).drop_duplicates("code").head(int(limit))
    return [
        {"code": str(row.code), "name": str(getattr(row, "name", "") or row.code)}
        for row in out.itertuples(index=False)
        if str(row.code)
    ]


def _latest_breakout_setup(hist: pd.DataFrame, signal_date: str) -> Optional[dict[str, Any]]:
    d = hist[pd.to_datetime(hist["trade_date"], errors="coerce") < pd.Timestamp(signal_date)].copy()
    if len(d) < 25:
        return None
    d = d.sort_values("trade_date").reset_index(drop=True)
    d["prev_close"] = d["close"].shift(1)
    d["ret1"] = d["close"] / d["prev_close"] - 1.0
    d["amount_ma5_prev_daily"] = d["amount"].shift(1).rolling(5, min_periods=3).mean()
    d["prior20_high"] = d["high"].shift(1).rolling(20, min_periods=20).max()
    d["prior60_high"] = d["high"].shift(1).rolling(60, min_periods=60).max()
    d["close_vs_prior60_high"] = d["close"] / d["prior60_high"] - 1.0
    i = len(d) - 1
    row = d.iloc[i]
    if pd.isna(row.get("close_vs_prior60_high")) or float(row["close_vs_prior60_high"]) < -0.05:
        return None
    for lag in range(2, 6):
        j = i - lag
        if j < 20:
            continue
        big = d.iloc[j]
        big_prior20 = big.get("prior20_high")
        big_amt_ma5 = big.get("amount_ma5_prev_daily")
        big_break = (
            pd.notna(big_prior20)
            and pd.notna(big_amt_ma5)
            and float(big_amt_ma5) > 0
            and float(big["ret1"]) >= 0.055
            and float(big["close"]) >= float(big_prior20) * 0.995
            and float(big["amount"]) >= float(big_amt_ma5) * 1.5
        )
        if not big_break:
            continue
        rest = d.iloc[j + 1 : i + 1]
        if len(rest) < 2:
            continue
        top = float(d.iloc[j : i + 1]["high"].max())
        low = float(rest["low"].min())
        rng = top / low - 1.0 if low > 0 else np.nan
        low_ok = low >= float(big["close"]) * 0.90
        close_ok = float(row["close"]) >= float(big["close"]) * 0.96
        if pd.notna(rng) and rng <= 0.18 and low_ok and close_ok:
            return {
                "setup_big_bull_rebreak_2_5d_top": top,
                "setup_big_bull_rebreak_2_5d_low": low,
                "setup_big_bull_rebreak_2_5d_range": float(rng),
                "setup_big_bull_date": big["trade_date"],
                "setup_big_bull_amount_ratio": float(big["amount"] / big_amt_ma5),
                "big_bull_lag_days": lag,
                "close_vs_prior60_high": float(row["close_vs_prior60_high"]),
                "prev_close": float(row["close"]),
            }
    return None


def _make_live_breakout_candidates(event_dataset: Path, state_daily: Path, signal_date: str, pool_rank: int) -> pd.DataFrame:
    trade_dates = _load_trade_dates("2024-07-09", signal_date)
    if signal_date not in trade_dates:
        return pd.DataFrame()
    idx = trade_dates.index(signal_date)
    if idx <= 0:
        return pd.DataFrame()
    prev_date = trade_dates[idx - 1]
    events = _load_events(event_dataset, prev_date, prev_date)
    if events.empty:
        return pd.DataFrame()
    for col in ["entry_pass", "in_score_pool"]:
        if col in events.columns:
            events[col] = events[col].fillna(False).astype(bool)
    events = _attach_g2_open_state(events, Path(state_daily), signal_date)
    candidate_mask = (
        events.get("in_score_pool", False).fillna(False).astype(bool)
        & pd.to_numeric(events.get("v4_rank"), errors="coerce").le(min(int(pool_rank), 200))
        & events["g2_open_state"].isin(("NORMAL", "AGGRESSIVE"))
        & pd.to_numeric(events.get("mom10"), errors="coerce").ge(0.02)
        & pd.to_numeric(events.get("mom20"), errors="coerce").ge(0.03)
        & pd.to_numeric(events.get("mom5"), errors="coerce").le(0.12)
        & pd.to_numeric(events.get("vol_ratio"), errors="coerce").le(3.0)
        & pd.to_numeric(events.get("vol10"), errors="coerce").le(0.10)
    )
    contexts = events[candidate_mask].copy()
    if contexts.empty:
        return contexts
    contexts["entry_date"] = signal_date
    contexts["entry_start_date"] = signal_date
    contexts["search_end_date"] = signal_date
    contexts["entry_start_idx"] = idx
    contexts["search_end_idx"] = idx
    contexts["trade_idx"] = idx - 1
    contexts["candidate_profile"] = "pool_rank200_box"
    codes = contexts["code"].dropna().astype(str).unique().tolist()
    daily = _load_daily_for_breakout(codes, (pd.Timestamp(signal_date) - pd.Timedelta(days=100)).strftime("%Y-%m-%d"), prev_date)
    minute = _load_live_breakout_signal_bars(codes, prev_date, signal_date, 30)
    if daily.empty or minute.empty:
        return pd.DataFrame()
    ctx_map = {str(row.code): row._asdict() for row in contexts.itertuples(index=False)}
    rows: list[dict[str, Any]] = []
    for code, hist in daily.groupby("code", sort=False):
        setup = _latest_breakout_setup(hist, signal_date)
        if not setup:
            continue
        bars = minute[
            minute["code"].astype(str).eq(str(code))
            & pd.to_datetime(minute["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d").eq(signal_date)
        ].sort_values("datetime").copy()
        if bars.empty:
            continue
        prev_close = setup.get("prev_close")
        setup_top = setup.get("setup_big_bull_rebreak_2_5d_top")
        if not prev_close or not setup_top:
            continue
        bars["rt_return_from_d1_close"] = bars["close"] / float(prev_close) - 1.0
        bars["rt_breakout_vs_box_top"] = bars["close"] / float(setup_top) - 1.0
        price_ok = (
            (bars["bar_time"] >= "10:00:00")
            & (bars["close"] >= float(setup_top))
            & (bars["close"] <= float(setup_top) * 1.15)
            & ((bars["rt_return_from_d1_close"] >= 0.060) | (bars["rt_breakout_vs_box_top"] >= 0.025))
        )
        vol_ok = pd.to_numeric(bars.get("amount_ma5_prev"), errors="coerce").gt(0) & (
            pd.to_numeric(bars.get("amount"), errors="coerce") >= pd.to_numeric(bars.get("amount_ma5_prev"), errors="coerce") * 1.2
        )
        hit = bars[price_ok & vol_ok].head(1)
        if hit.empty:
            continue
        first = hit.iloc[0]
        ctx = ctx_map.get(str(code), {})
        rows.append(
            {
                **ctx,
                **setup,
                "trade_date": prev_date,
                "entry_date": signal_date,
                "code": code,
                "name": ctx.get("name", ""),
                "entry_price": float(first["close"]),
                "confirm_datetime": first["datetime"],
                "confirm_amount": float(first.get("amount") or np.nan),
                "confirm_amount_ma5_prev": float(first.get("amount_ma5_prev") or np.nan),
                "volume_ratio": float(first["amount"] / first["amount_ma5_prev"]) if pd.notna(first.get("amount_ma5_prev")) and float(first["amount_ma5_prev"]) > 0 else np.nan,
                "source_family": "big_bull",
                "signal_family": "breakout_big_bull_sector_strong",
                "source": "g2_v2_live_breakout",
                "pattern": "big_bull_rebreak_2_5d",
                "trigger_type": "big_bull_rebreak_2_5d",
                "g2_v2_family_priority": 1,
                "rt_return_from_d1_close": float(first["rt_return_from_d1_close"]),
                "rt_breakout_vs_box_top": float(first["rt_breakout_vs_box_top"]),
                "g2_v2_buy_logic": "big_bull_rebreak_2_5d + intraday_strength + sector_strong",
            }
        )
    if not rows:
        return pd.DataFrame()
    out = _attach_sector_context(pd.DataFrame(rows))
    out = out[out["sector_strong"].fillna(False)].copy()
    out["sector_score_bonus"] = 0.0
    out["v4_score_raw"] = pd.to_numeric(out.get("v4_score"), errors="coerce")
    return out.reset_index(drop=True)


def _apply_g2_v2_complete(
    filtered_before_alpha191: pd.DataFrame,
    event_dataset: Path,
    state_daily: Path,
    signal_date: str,
    pool_rank: int,
    train_source: Path,
    train_values: Path,
    train_start: str,
    train_end: str,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    volume5, alpha_summary = _prepare_live_volume5_mainline(
        filtered_before_alpha191,
        signal_date=signal_date,
        train_source=train_source,
        train_values=train_values,
        train_start=train_start,
        train_end=train_end,
    )
    breakout = _make_live_breakout_candidates(event_dataset, state_daily, signal_date, pool_rank)
    if not volume5.empty and not breakout.empty:
        keys = set(zip(volume5["entry_date"].astype(str), volume5["code"].astype(str)))
        breakout = breakout[[key not in keys for key in zip(breakout["entry_date"].astype(str), breakout["code"].astype(str))]].copy()
    final = pd.concat([volume5, breakout], ignore_index=True, sort=False)
    if not final.empty:
        final = final.sort_values(
            ["entry_date", "g2_v2_family_priority", "v4_score", "v4_rank", "confirm_datetime", "code"],
            ascending=[True, True, False, True, True, True],
            na_position="last",
        ).reset_index(drop=True)
    summary = {
        "enabled": True,
        "gate": "g2_v2_complete",
        "volume5": alpha_summary,
        "breakout_signals": int(len(breakout)),
        "signals_before": int(len(filtered_before_alpha191)),
        "signals_after": int(len(final)),
        "source_families": final["source_family"].fillna("").astype(str).value_counts().to_dict() if not final.empty else {},
    }
    pre_final = pd.concat([filtered_before_alpha191, breakout], ignore_index=True, sort=False)
    return final, pre_final, summary


def _load_existing_source(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _write_csv_atomic(df: pd.DataFrame, path: Path, encoding: str = "utf-8-sig") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=path.suffix or ".csv", delete=False) as tmp:
            tmp_name = tmp.name
        tmp_path = Path(tmp_name)
        df.to_csv(tmp_path, index=False, encoding=encoding)
        tmp_path.replace(path)
    finally:
        if tmp_name:
            tmp_path = Path(tmp_name)
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


def _write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=path.suffix or ".parquet", delete=False) as tmp:
            tmp_name = tmp.name
        tmp_path = Path(tmp_name)
        df.to_parquet(tmp_path, index=False)
        tmp_path.replace(path)
    finally:
        if tmp_name:
            tmp_path = Path(tmp_name)
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


def _update_source(source_csv: Path, source_parquet: Path, signal_date: str, new_rows: pd.DataFrame) -> pd.DataFrame:
    existing = _load_existing_source(source_csv)
    if not existing.empty and "entry_date" in existing.columns:
        existing["entry_date"] = pd.to_datetime(existing["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        existing = existing[~existing["entry_date"].eq(signal_date)].copy()
    merged = pd.concat([existing, new_rows], ignore_index=True, sort=False) if not new_rows.empty else existing
    if merged.empty:
        merged = _ensure_output_frame(merged)
    if not merged.empty:
        merged["entry_date"] = pd.to_datetime(merged["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        if "confirm_datetime" in merged.columns:
            merged["confirm_datetime"] = pd.to_datetime(merged["confirm_datetime"], errors="coerce")
        for col in [item for item in merged.columns if "datetime" in str(item).lower() and item != "confirm_datetime"]:
            merged[col] = pd.to_datetime(merged[col], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
        for col in [item for item in merged.columns if str(item).lower().endswith("_date") and item != "entry_date"]:
            merged[col] = pd.to_datetime(merged[col], errors="coerce").dt.strftime("%Y-%m-%d")
        preferred_sort = ["entry_date", "g2_v2_family_priority", "v4_score", "v4_rank", "confirm_datetime", "code"]
        if "g2_v2_family_priority" not in merged.columns:
            preferred_sort = ["entry_date", "confirm_datetime", "v4_rank", "v4_score", "code"]
        sort_cols = [col for col in preferred_sort if col in merged.columns]
        ascending_map = {
            "entry_date": True,
            "g2_v2_family_priority": True,
            "v4_score": False,
            "v4_rank": True,
            "confirm_datetime": True,
            "code": True,
        }
        ascending = [ascending_map.get(col, True) for col in sort_cols]
        merged = merged.sort_values(sort_cols, ascending=ascending).reset_index(drop=True)
    _write_csv_atomic(merged, source_csv, encoding="utf-8-sig")
    _write_parquet_atomic(merged, source_parquet)
    return merged


def _ensure_output_frame(df: pd.DataFrame) -> pd.DataFrame:
    if not df.empty:
        return df
    return pd.DataFrame(columns=LIVE_OUTPUT_COLUMNS)


def _normalize_output_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_output_frame(df).copy()
    if out.empty:
        return out
    if "entry_date" in out.columns:
        out["entry_date"] = pd.to_datetime(out["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "trade_date" in out.columns:
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "confirm_datetime" in out.columns:
        out["confirm_datetime"] = pd.to_datetime(out["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    for col in [c for c in out.columns if str(c).lower().endswith("_date") and c not in {"entry_date", "trade_date"}]:
        out[col] = pd.to_datetime(out[col], errors="coerce").dt.strftime("%Y-%m-%d")
    return out


def _refresh_shadow_ledger(source_csv: Path, run_dir: Path, output_dir: Path) -> pd.DataFrame:
    ledger = _build_ledger(source_csv, run_dir)
    summary = _summarize(ledger)
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger.to_csv(output_dir / "shadow_ledger.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, summary, ledger)
    payload = {
        "schema_version": 1,
        "source": str(source_csv),
        "run_dir": str(run_dir),
        "rows": summary.where(pd.notna(summary), None).to_dict("records"),
        "outputs": {"ledger": "shadow_ledger.csv", "summary": "summary.csv", "report": "shadow_ledger_report.md"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return ledger


def run(args: argparse.Namespace) -> dict[str, Any]:
    signal_date = str(args.signal_date or "").strip() or _latest_daily_date()
    if not signal_date:
        raise RuntimeError("No signal_date available.")
    source_csv = Path(args.source_csv).resolve()
    source_parquet = Path(args.source_parquet).resolve() if args.source_parquet else source_csv.with_suffix(".parquet")
    output_dir = Path(args.output_dir).resolve()
    live_dir = output_dir / "live_updates"
    live_dir.mkdir(parents=True, exist_ok=True)

    candidates = _make_live_candidates(Path(args.event_dataset), Path(args.state_daily), signal_date, int(args.pool_rank))
    filtered, filter_summary = _apply_live_filters(
        candidates,
        start_date=(pd.Timestamp(signal_date) - pd.Timedelta(days=430)).strftime("%Y-%m-%d"),
        end_date=signal_date,
        share_cap_cache=Path(args.share_cap_cache),
    )
    filtered_before_alpha191 = filtered.copy()
    gate = str(args.alpha191_gate or "off").strip().lower()
    if gate == "g2_v2_complete":
        filtered, filtered_before_alpha191, alpha191_summary = _apply_g2_v2_complete(
            filtered_before_alpha191,
            event_dataset=Path(args.event_dataset),
            state_daily=Path(args.state_daily),
            signal_date=signal_date,
            pool_rank=int(args.pool_rank),
            train_source=Path(args.alpha191_train_source),
            train_values=Path(args.alpha191_train_values),
            train_start=str(args.alpha191_train_start),
            train_end=str(args.alpha191_train_end),
        )
    else:
        filtered, alpha191_summary = _apply_alpha191_gate(
            filtered,
            signal_date=signal_date,
            gate=gate,
            train_source=Path(args.alpha191_train_source),
            train_values=Path(args.alpha191_train_values),
            train_start=str(args.alpha191_train_start),
            train_end=str(args.alpha191_train_end),
        )
    filtered = _attach_mainline_theme_overlay(filtered, Path(args.mainline_theme_addon))
    filtered_before_alpha191 = _attach_mainline_theme_overlay(filtered_before_alpha191, Path(args.mainline_theme_addon))
    live_raw = live_dir / f"{signal_date}_raw_candidates.parquet"
    live_pre_alpha191 = live_dir / f"{signal_date}_pre_alpha191_signals.parquet"
    live_filtered = live_dir / f"{signal_date}_filtered_signals.parquet"
    raw_output = _normalize_output_frame(candidates)
    pre_alpha_output = _normalize_output_frame(filtered_before_alpha191)
    filtered_output = _normalize_output_frame(filtered)
    raw_output.to_parquet(live_raw, index=False)
    raw_output.to_csv(live_raw.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    pre_alpha_output.to_parquet(live_pre_alpha191, index=False)
    pre_alpha_output.to_csv(live_pre_alpha191.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    filtered_output.to_parquet(live_filtered, index=False)
    filtered_output.to_csv(live_filtered.with_suffix(".csv"), index=False, encoding="utf-8-sig")

    merged = _update_source(source_csv, source_parquet, signal_date, filtered)
    ledger = _refresh_shadow_ledger(source_csv, Path(args.run_dir), output_dir)
    day_ledger = ledger[pd.to_datetime(ledger["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d").eq(signal_date)].copy()

    payload = {
        "schema_version": 1,
        "signal_date": signal_date,
        "pool_rank": int(args.pool_rank),
        "raw_candidates": int(len(candidates)),
        "filtered_signals_before_alpha191": int(len(filtered_before_alpha191)),
        "filtered_signals": int(len(filtered)),
        "source_rows": int(len(merged)),
        "shadow_rows_for_date": int(len(day_ledger)),
        "filter_summary": filter_summary,
        "alpha191_gate": alpha191_summary,
        "mainline_theme_overlay": {
            "enabled": True,
            "addon": str(Path(args.mainline_theme_addon)),
            "filtered_theme_matches": int(filtered.get("mainline_theme_match", pd.Series(dtype=bool)).fillna(False).sum()) if not filtered.empty else 0,
            "pre_alpha191_theme_matches": int(filtered_before_alpha191.get("mainline_theme_match", pd.Series(dtype=bool)).fillna(False).sum()) if not filtered_before_alpha191.empty else 0,
            "mode": "research_only_fields_no_buy_bypass",
        },
        "outputs": {
            "raw_candidates": str(live_raw.relative_to(ROOT)),
            "pre_alpha191_signals": str(live_pre_alpha191.relative_to(ROOT)),
            "filtered_signals": str(live_filtered.relative_to(ROOT)),
            "source_csv": str(source_csv.relative_to(ROOT)) if source_csv.is_relative_to(ROOT) else str(source_csv),
            "shadow_ledger": str((output_dir / "shadow_ledger.csv").relative_to(ROOT)),
        },
    }
    (live_dir / f"{signal_date}_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Update live G2 V3 shadow ledger for one signal date without future labels.")
    parser.add_argument("--signal-date", default="")
    parser.add_argument("--event-dataset", default=str(DEFAULT_EVENT_DATASET))
    parser.add_argument("--state-daily", default=str(DEFAULT_STATE_DAILY))
    parser.add_argument("--source-csv", default=str(DEFAULT_SOURCE_CSV))
    parser.add_argument("--source-parquet", default=str(DEFAULT_SOURCE_PARQUET))
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--share-cap-cache", default=str(DEFAULT_SHARE_CAP_CACHE))
    parser.add_argument("--pool-rank", type=int, default=200)
    parser.add_argument(
        "--alpha191-gate",
        choices=[
            "off",
            "volume5_keep80_runup",
            "g2_v2_complete",
        ],
        default="g2_v2_complete",
    )
    parser.add_argument("--alpha191-train-source", default=str(DEFAULT_ALPHA191_TRAIN_SOURCE))
    parser.add_argument("--alpha191-train-values", default=str(DEFAULT_ALPHA191_TRAIN_VALUES))
    parser.add_argument("--alpha191-train-start", default="2024-07-09")
    parser.add_argument("--alpha191-train-end", default="2025-03-31")
    parser.add_argument("--mainline-theme-addon", default=str(DEFAULT_MAINLINE_THEME_ADDON))
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
