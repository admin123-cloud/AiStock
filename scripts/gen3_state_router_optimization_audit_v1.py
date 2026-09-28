from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_market_mode_router_v1 import metrics, simulate, window_metrics  # noqa: E402
from scripts.gen3_market_state_router_v1 import (  # noqa: E402
    attach_previous_context,
    load_market_context,
    load_trade_library,
)
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("gen3_state_router_optimization_audit_v1")
STATE_DIR = report_path("gen3_market_state_router_v1")
SHADOW_DIR = report_path("gen3_state_router_shadow_daily_v1")
BASE_CURVE = STATE_DIR / "state_router_equity_curve.csv"


def _safe_float(value: Any, digits: int = 6) -> Any:
    try:
        x = float(value)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, digits)


def _pct(value: Any) -> str:
    x = _safe_float(value)
    return "" if x is None else f"{x:.2%}"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, max_rows: int = 60) -> str:
    if df.empty:
        return "_无数据_"
    d = df.head(max_rows).copy()
    for col in pct_cols or set():
        if col in d.columns:
            d[col] = d[col].map(_pct)
    for col in d.columns:
        if pd.api.types.is_float_dtype(d[col]) and col not in (pct_cols or set()):
            d[col] = d[col].map(lambda x: "" if pd.isna(x) else f"{float(x):.4f}")
    return d.to_markdown(index=False)


def _summarize_trades(name: str, trades: pd.DataFrame, calendar: list[pd.Timestamp]) -> dict[str, Any]:
    if trades.empty:
        return {
            "model": name,
            "trades": 0,
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "avg_trade_return": 0.0,
            "worst_trade": 0.0,
            "best_trade": 0.0,
        }
    curve, closed = simulate(trades, name, calendar)
    item = metrics(name, curve, closed)
    return item


def _pick_state_rows(
    enriched: pd.DataFrame,
    calendar: list[pd.Timestamp],
    *,
    inst_gate: Callable[[pd.Series], bool] | None = None,
    old_gate: Callable[[pd.Series], bool] | None = None,
    panic_gate: Callable[[pd.Series], bool] | None = None,
    bear_extra_modes: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    by_day = {day: g.copy() for day, g in enriched.groupby("entry_date")}
    selected: list[pd.DataFrame] = []
    decisions: list[dict[str, Any]] = []
    priority = ["panic_repair", "institutional_mainwave", "old_g3_route_v3"]
    for day in calendar:
        todays = by_day.get(day)
        if todays is None or todays.empty:
            continue
        d = todays.copy()
        if inst_gate is not None:
            mask = d["mode"].ne("institutional_mainwave") | d.apply(lambda r: bool(inst_gate(r)), axis=1)
            d = d[mask].copy()
        if old_gate is not None:
            mask = d["mode"].ne("old_g3_route_v3") | d.apply(lambda r: bool(old_gate(r)), axis=1)
            d = d[mask].copy()
        if panic_gate is not None:
            mask = d["mode"].ne("panic_repair") | d.apply(lambda r: bool(panic_gate(r)), axis=1)
            d = d[mask].copy()
        if d.empty:
            continue
        row = todays.iloc[0]
        style = str(row.get("market_style_prev") or row.get("market_style") or "")
        up_rate = float(row.get("up_rate_prev", row.get("up_rate", 0)) or 0)
        big_down = float(row.get("big_down_rate_prev", row.get("big_down_rate", 0)) or 0)
        limit_down = float(row.get("limit_down_proxy_rate", 0) or 0)
        panic = big_down >= 0.15 or limit_down >= 0.03 or (style == "standard_downtrend" and up_rate <= 0.35)
        bearish = _is_bearish_context(row)
        modes = []
        available = set(d["mode"].astype(str))
        if panic and "panic_repair" in available:
            modes = ["panic_repair"]
        elif "institutional_mainwave" in available:
            modes = ["institutional_mainwave"]
        else:
            if bearish and bear_extra_modes:
                modes.extend([m for m in bear_extra_modes if m in available])
            if "old_g3_route_v3" in available:
                modes.append("old_g3_route_v3")
        if not modes:
            continue
        picked = d[d["mode"].isin(modes)].copy()
        picked["mode_pick_rank"] = picked["mode"].map({m: i for i, m in enumerate(modes)}).fillna(99)
        picked = picked.sort_values(["mode_pick_rank", "score", "net_ret"], ascending=[True, False, False]).head(1)
        if not picked.empty:
            selected.append(picked)
            decisions.append(
                {
                    "date": day.strftime("%Y-%m-%d"),
                    "selected_mode": str(picked.iloc[0]["mode"]),
                    "code": str(picked.iloc[0].get("code", "")),
                    "net_ret": _safe_float(picked.iloc[0].get("net_ret")),
                }
            )
    out = pd.concat(selected, ignore_index=True) if selected else pd.DataFrame(columns=enriched.columns)
    return out, pd.DataFrame(decisions)


def _inst_context_audit(inst: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = inst.copy()
    d["entry_year"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.year
    d["is_loss"] = pd.to_numeric(d["net_ret"], errors="coerce") < 0
    d["is_big_loss"] = pd.to_numeric(d["net_ret"], errors="coerce") <= -0.12
    group_cols = ["market_style_prev", "ma_skeleton", "volume_price_layer", "adx_layer"]
    rows = []
    for col in group_cols:
        if col not in d.columns:
            continue
        g = (
            d.groupby(col, dropna=False)
            .agg(
                trades=("net_ret", "count"),
                avg_ret=("net_ret", "mean"),
                win_rate=("net_ret", lambda x: float((pd.to_numeric(x, errors="coerce") > 0).mean())),
                big_loss_rate=("is_big_loss", "mean"),
                worst_trade=("net_ret", "min"),
                best_trade=("net_ret", "max"),
            )
            .reset_index()
            .rename(columns={col: "bucket"})
        )
        g.insert(0, "feature", col)
        rows.append(g)
    context = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    worst = d.sort_values("net_ret").head(12)[
        [
            "entry_date",
            "policy_exit_date",
            "code",
            "name",
            "net_ret",
            "score",
            "market_style_prev",
            "ma_skeleton",
            "volume_price_layer",
            "adx_layer",
            "up_rate_prev",
            "big_down_rate_prev",
            "limit_down_proxy_rate",
            "mom20",
            "mom60",
            "breadth_ma20",
            "breadth_ma60",
            "median_mom20",
        ]
    ].copy()
    return context, worst


def _old_g3_efficiency(old: pd.DataFrame) -> pd.DataFrame:
    d = old.copy()
    if "route" not in d.columns:
        d["route"] = d.get("chain", "")
    return (
        d.groupby("route", dropna=False)
        .agg(
            trades=("net_ret", "count"),
            total_sum_ret=("net_ret", "sum"),
            avg_ret=("net_ret", "mean"),
            win_rate=("net_ret", lambda x: float((pd.to_numeric(x, errors="coerce") > 0).mean())),
            worst_trade=("net_ret", "min"),
            best_trade=("net_ret", "max"),
        )
        .reset_index()
        .sort_values(["avg_ret", "trades"], ascending=[False, False])
    )


def _panic_audit(panic: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = panic.copy()
    d["year"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.year
    annual = (
        d.groupby("year", dropna=False)
        .agg(
            trades=("net_ret", "count"),
            sum_ret=("net_ret", "sum"),
            avg_ret=("net_ret", "mean"),
            win_rate=("net_ret", lambda x: float((pd.to_numeric(x, errors="coerce") > 0).mean())),
            worst_trade=("net_ret", "min"),
            best_trade=("net_ret", "max"),
        )
        .reset_index()
        .sort_values("year")
    )
    by_context = (
        d.groupby(["market_style_prev", "ma_skeleton", "adx_layer"], dropna=False)
        .agg(
            trades=("net_ret", "count"),
            avg_ret=("net_ret", "mean"),
            win_rate=("net_ret", lambda x: float((pd.to_numeric(x, errors="coerce") > 0).mean())),
            worst_trade=("net_ret", "min"),
            best_trade=("net_ret", "max"),
        )
        .reset_index()
        .sort_values(["avg_ret", "trades"], ascending=[False, False])
    )
    return annual, by_context


def _shadow_consistency() -> pd.DataFrame:
    rows = []
    hist_path = STATE_DIR / "state_router_selected_candidates.csv"
    shadow_path = SHADOW_DIR / "g3_state_router_all_source_candidates.csv"
    if not hist_path.exists() or not shadow_path.exists():
        return pd.DataFrame()
    hist = pd.read_csv(hist_path, low_memory=False, encoding="utf-8-sig")
    shadow = pd.read_csv(shadow_path, low_memory=False, encoding="utf-8-sig")
    for col in ["entry_date", "decision_date"]:
        if col in hist.columns:
            hist[col] = pd.to_datetime(hist[col], errors="coerce").dt.strftime("%Y-%m-%d")
        if col in shadow.columns:
            shadow[col] = pd.to_datetime(shadow[col], errors="coerce").dt.strftime("%Y-%m-%d")
    if shadow.empty:
        return pd.DataFrame()
    entry_dates = sorted(set(shadow.get("entry_date", pd.Series(dtype=str)).dropna().astype(str)))
    for entry in entry_dates:
        h = hist[hist["entry_date"].eq(entry)].copy() if "entry_date" in hist.columns else pd.DataFrame()
        s = shadow[shadow["entry_date"].eq(entry)].copy()
        rows.append(
            {
                "entry_date": entry,
                "historical_rows": int(len(h)),
                "shadow_rows": int(len(s)),
                "historical_codes": ",".join(h.get("code", pd.Series(dtype=str)).astype(str).head(10)),
                "shadow_codes": ",".join(s.get("code", pd.Series(dtype=str)).astype(str).head(10)),
                "shadow_eligible_rows": int(s.get("router_eligible", pd.Series(False, index=s.index)).fillna(False).astype(bool).sum()) if not s.empty else 0,
                "note": "仅比较当前 shadow 产物日期；完整一致性需 ClickHouse 恢复后按历史日期批量回放。",
            }
        )
    return pd.DataFrame(rows)


def _offline_calendar(enriched: pd.DataFrame) -> list[pd.Timestamp]:
    if BASE_CURVE.exists():
        curve = pd.read_csv(BASE_CURVE, low_memory=False, encoding="utf-8-sig")
        for col in ["date", "trade_date"]:
            if col in curve.columns:
                dates = pd.to_datetime(curve[col], errors="coerce").dropna().dt.normalize().drop_duplicates().sort_values()
                if not dates.empty:
                    return list(dates)
    dates = pd.concat([enriched["entry_date"], enriched["policy_exit_date"]], ignore_index=True)
    dates = pd.to_datetime(dates, errors="coerce").dropna().dt.normalize().drop_duplicates().sort_values()
    return list(dates)


def _is_bearish_context(row: pd.Series) -> bool:
    style = str(row.get("market_style_prev") or row.get("market_style") or "")
    up_rate = float(row.get("up_rate_prev", row.get("up_rate", 0)) or 0)
    big_down = float(row.get("big_down_rate_prev", row.get("big_down_rate", 0)) or 0)
    mom20 = float(row.get("mom20", 0) or 0)
    breadth = float(row.get("breadth_ma20", 0) or 0)
    adx = str(row.get("adx_layer") or "")
    return (
        style == "standard_downtrend"
        or big_down >= 0.12
        or up_rate <= 0.35
        or breadth <= 0.35
        or (mom20 < 0 and adx == "downtrend_strength")
    )


def _attach_inst_rolling_health(enriched: pd.DataFrame) -> pd.DataFrame:
    out = enriched.copy()
    out["entry_date"] = pd.to_datetime(out["entry_date"], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(out["policy_exit_date"], errors="coerce").dt.normalize()
    inst = out[out["mode"].eq("institutional_mainwave")].copy()
    inst["net_ret"] = pd.to_numeric(inst["net_ret"], errors="coerce")
    inst = inst.dropna(subset=["policy_exit_date", "net_ret"]).sort_values("policy_exit_date")
    windows = [60, 90, 120, 180, 240, 360, 720]
    for days in windows:
        out[f"inst_roll{days}_count"] = 0
        out[f"inst_roll{days}_avg_ret"] = np.nan
        out[f"inst_roll{days}_win_rate"] = np.nan
        out[f"inst_roll{days}_big_loss_rate"] = np.nan
        out[f"inst_roll{days}_worst_ret"] = np.nan

    for idx, row in out.iterrows():
        entry = row.get("entry_date")
        if pd.isna(entry):
            continue
        past_base = inst[inst["policy_exit_date"].lt(entry)]
        for days in windows:
            start = entry - pd.Timedelta(days=days)
            past = past_base[past_base["policy_exit_date"].ge(start)]
            rets = pd.to_numeric(past["net_ret"], errors="coerce").dropna()
            if rets.empty:
                continue
            out.at[idx, f"inst_roll{days}_count"] = int(len(rets))
            out.at[idx, f"inst_roll{days}_avg_ret"] = float(rets.mean())
            out.at[idx, f"inst_roll{days}_win_rate"] = float((rets > 0).mean())
            out.at[idx, f"inst_roll{days}_big_loss_rate"] = float((rets <= -0.12).mean())
            out.at[idx, f"inst_roll{days}_worst_ret"] = float(rets.min())
    return out


def _apply_inst_loss_cap(selected: pd.DataFrame, floor: float) -> pd.DataFrame:
    out = selected.copy()
    if out.empty:
        return out
    mask = out["mode"].eq("institutional_mainwave")
    out.loc[mask, "net_ret"] = pd.to_numeric(out.loc[mask, "net_ret"], errors="coerce").clip(lower=floor)
    return out


def _mode_bear_library_audit(enriched: pd.DataFrame) -> pd.DataFrame:
    d = enriched.copy()
    d["bearish_context"] = d.apply(_is_bearish_context, axis=1)
    d = d[d["bearish_context"]].copy()
    if d.empty:
        return pd.DataFrame()
    d["year"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.year
    rows = []
    for mode, g in d.groupby("mode", dropna=False):
        rets = pd.to_numeric(g["net_ret"], errors="coerce").dropna()
        if rets.empty:
            continue
        rows.append(
            {
                "mode": mode,
                "bear_trades": int(len(rets)),
                "bear_sum_ret": float(rets.sum()),
                "bear_avg_ret": float(rets.mean()),
                "bear_win_rate": float((rets > 0).mean()),
                "bear_big_loss_rate": float((rets <= -0.12).mean()),
                "bear_worst_trade": float(rets.min()),
                "bear_best_trade": float(rets.max()),
                "years": ",".join(str(int(y)) for y in sorted(g["year"].dropna().unique())),
            }
        )
    return pd.DataFrame(rows).sort_values(["bear_avg_ret", "bear_trades"], ascending=[False, False])


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_trade_library()
    mc = load_market_context()
    enriched = attach_previous_context(trades, mc)
    enriched = _attach_inst_rolling_health(enriched)
    calendar = _offline_calendar(enriched)

    baseline_selected, _ = _pick_state_rows(enriched, calendar)
    variants: list[dict[str, Any]] = []

    def add_variant(name: str, **kwargs: Any) -> pd.DataFrame:
        selected, _dec = _pick_state_rows(enriched, calendar, **kwargs)
        item = _summarize_trades(name, selected, calendar)
        item["trade_delta_vs_baseline"] = int(item["trades"]) - int(baseline_metrics["trades"])
        item["return_delta_vs_baseline"] = _safe_float(float(item["total_return"]) - float(baseline_metrics["total_return"]))
        item["dd_delta_vs_baseline"] = _safe_float(float(item["max_drawdown"]) - float(baseline_metrics["max_drawdown"]))
        variants.append(item)
        return selected

    def add_selected_variant(name: str, selected: pd.DataFrame) -> None:
        item = _summarize_trades(name, selected, calendar)
        item["trade_delta_vs_baseline"] = int(item["trades"]) - int(baseline_metrics["trades"])
        item["return_delta_vs_baseline"] = _safe_float(float(item["total_return"]) - float(baseline_metrics["total_return"]))
        item["dd_delta_vs_baseline"] = _safe_float(float(item["max_drawdown"]) - float(baseline_metrics["max_drawdown"]))
        variants.append(item)

    baseline_metrics = _summarize_trades("baseline_state_router", baseline_selected, calendar)
    variants.append({**baseline_metrics, "trade_delta_vs_baseline": 0, "return_delta_vs_baseline": 0.0, "dd_delta_vs_baseline": 0.0})

    # Keep simple, interpretable gates. These are candidates, not final parameters.
    add_variant(
        "inst_gate_no_early_weak_range",
        inst_gate=lambda r: not (
            pd.Timestamp(r.get("entry_date")) < pd.Timestamp("2024-09-24")
            and str(r.get("market_style_prev")) in {"weak_rebound", "standard_range"}
        ),
    )
    add_variant(
        "inst_gate_require_breadth45_no_panic",
        inst_gate=lambda r: (float(r.get("breadth_ma20", 0) or 0) >= 0.45)
        and (float(r.get("big_down_rate_prev", 0) or 0) <= 0.12)
        and (float(r.get("limit_down_proxy_rate", 0) or 0) <= 0.025),
    )
    add_variant(
        "inst_gate_require_mom60_nonnegative",
        inst_gate=lambda r: float(r.get("mom60", 0) or 0) >= 0.0,
    )
    add_variant(
        "inst_regime_roll60_count1_avg_gt0",
        inst_gate=lambda r: int(r.get("inst_roll60_count", 0) or 0) >= 1
        and float(r.get("inst_roll60_avg_ret", -1) or -1) > 0,
    )
    add_variant(
        "inst_regime_roll90_count1_avg_gt0",
        inst_gate=lambda r: int(r.get("inst_roll90_count", 0) or 0) >= 1
        and float(r.get("inst_roll90_avg_ret", -1) or -1) > 0,
    )
    add_variant(
        "inst_regime_roll120_count1_avg_gt0",
        inst_gate=lambda r: int(r.get("inst_roll120_count", 0) or 0) >= 1
        and float(r.get("inst_roll120_avg_ret", -1) or -1) > 0,
    )
    add_variant(
        "inst_regime_roll180_count1_avg_gt0",
        inst_gate=lambda r: int(r.get("inst_roll180_count", 0) or 0) >= 1
        and float(r.get("inst_roll180_avg_ret", -1) or -1) > 0,
    )
    add_variant(
        "inst_regime_roll120_count2_avg_gt0",
        inst_gate=lambda r: int(r.get("inst_roll120_count", 0) or 0) >= 2
        and float(r.get("inst_roll120_avg_ret", -1) or -1) > 0,
    )
    add_variant(
        "inst_regime_roll180_count2_avg_gt0",
        inst_gate=lambda r: int(r.get("inst_roll180_count", 0) or 0) >= 2
        and float(r.get("inst_roll180_avg_ret", -1) or -1) > 0,
    )
    add_variant(
        "inst_regime_roll180_count2_health",
        inst_gate=lambda r: int(r.get("inst_roll180_count", 0) or 0) >= 2
        and float(r.get("inst_roll180_avg_ret", -1) or -1) > 0
        and float(r.get("inst_roll180_big_loss_rate", 1) or 1) <= 0.35,
    )
    add_variant(
        "inst_regime_roll240_count2_avg_gt0",
        inst_gate=lambda r: int(r.get("inst_roll240_count", 0) or 0) >= 2
        and float(r.get("inst_roll240_avg_ret", -1) or -1) > 0,
    )
    add_variant(
        "inst_regime_roll240_count2_health",
        inst_gate=lambda r: int(r.get("inst_roll240_count", 0) or 0) >= 2
        and float(r.get("inst_roll240_avg_ret", -1) or -1) > 0
        and float(r.get("inst_roll240_big_loss_rate", 1) or 1) <= 0.35,
    )
    add_variant(
        "inst_regime_roll360_count2_avg_gt0",
        inst_gate=lambda r: int(r.get("inst_roll360_count", 0) or 0) >= 2
        and float(r.get("inst_roll360_avg_ret", -1) or -1) > 0,
    )
    add_variant(
        "inst_regime_roll360_count2_health",
        inst_gate=lambda r: int(r.get("inst_roll360_count", 0) or 0) >= 2
        and float(r.get("inst_roll360_avg_ret", -1) or -1) > 0
        and float(r.get("inst_roll360_big_loss_rate", 1) or 1) <= 0.35,
    )
    add_variant(
        "inst_regime_roll720_count3_avg_gt0",
        inst_gate=lambda r: int(r.get("inst_roll720_count", 0) or 0) >= 3
        and float(r.get("inst_roll720_avg_ret", -1) or -1) > 0,
    )
    add_variant(
        "inst_regime_roll360_positive_or_weak_repair",
        inst_gate=lambda r: (
            int(r.get("inst_roll360_count", 0) or 0) >= 2
            and float(r.get("inst_roll360_avg_ret", -1) or -1) > 0
            and float(r.get("inst_roll360_big_loss_rate", 1) or 1) <= 0.35
        )
        or (
            str(r.get("ma_skeleton") or "") == "weak_repair"
            and float(r.get("breadth_ma20", 0) or 0) >= 0.45
            and float(r.get("mom20", 0) or 0) >= 0
        ),
    )
    add_variant(
        "old_g3_keep_positive_routes",
        old_gate=lambda r: str(r.get("route") or "") in {"v3_strong", "up_main", "down_panic", "breakout_pullback"},
    )
    add_variant(
        "panic_only_deep_or_downtrend",
        panic_gate=lambda r: (float(r.get("big_down_rate_prev", 0) or 0) >= 0.18)
        or (float(r.get("limit_down_proxy_rate", 0) or 0) >= 0.03)
        or str(r.get("market_style_prev")) == "standard_downtrend",
    )
    add_variant("bear_add_range_weak_rebound", bear_extra_modes=["range_weak_rebound"])
    add_variant("bear_add_old_g3_guarded", bear_extra_modes=["old_g3_guarded"])
    add_variant("bear_add_range_then_guarded", bear_extra_modes=["range_weak_rebound", "old_g3_guarded"])
    add_variant("bear_add_strong_volume5", bear_extra_modes=["strong_volume5"])

    for floor in [-0.20, -0.15, -0.12, -0.10]:
        add_selected_variant(f"inst_loss_cap_{abs(int(floor * 100))}pct_upper_bound", _apply_inst_loss_cap(baseline_selected, floor))

    variants_df = pd.DataFrame(variants)
    variants_df.to_csv(OUT_DIR / "optimization_variant_summary.csv", index=False, encoding="utf-8-sig")
    variants_df[variants_df["model"].astype(str).str.contains("inst_gate|inst_regime", regex=True)].to_csv(
        OUT_DIR / "institutional_regime_variants.csv", index=False, encoding="utf-8-sig"
    )
    variants_df[variants_df["model"].astype(str).str.contains("loss_cap", regex=False)].to_csv(
        OUT_DIR / "institutional_loss_cap_upper_bound.csv", index=False, encoding="utf-8-sig"
    )
    variants_df[variants_df["model"].astype(str).str.contains("bear_add", regex=False)].to_csv(
        OUT_DIR / "bear_extension_variants.csv", index=False, encoding="utf-8-sig"
    )

    inst = enriched[enriched["mode"].eq("institutional_mainwave")].copy()
    old = enriched[enriched["mode"].eq("old_g3_route_v3")].copy()
    panic = enriched[enriched["mode"].eq("panic_repair")].copy()
    inst_context, inst_worst = _inst_context_audit(inst)
    old_eff = _old_g3_efficiency(old)
    panic_annual, panic_context = _panic_audit(panic)
    bear_library = _mode_bear_library_audit(enriched)
    consistency = _shadow_consistency()

    inst_context.to_csv(OUT_DIR / "institutional_context_buckets.csv", index=False, encoding="utf-8-sig")
    inst_worst.to_csv(OUT_DIR / "institutional_worst_trades.csv", index=False, encoding="utf-8-sig")
    old_eff.to_csv(OUT_DIR / "old_g3_route_efficiency.csv", index=False, encoding="utf-8-sig")
    panic_annual.to_csv(OUT_DIR / "panic_repair_annual.csv", index=False, encoding="utf-8-sig")
    panic_context.to_csv(OUT_DIR / "panic_repair_context_buckets.csv", index=False, encoding="utf-8-sig")
    bear_library.to_csv(OUT_DIR / "bear_mode_library_audit.csv", index=False, encoding="utf-8-sig")
    consistency.to_csv(OUT_DIR / "shadow_consistency_current_check.csv", index=False, encoding="utf-8-sig")

    summary = {
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "baseline": baseline_metrics,
        "best_variant_by_return": variants_df.sort_values("total_return", ascending=False).head(1).to_dict("records"),
        "best_variant_by_drawdown": variants_df.sort_values("max_drawdown", ascending=False).head(1).to_dict("records"),
        "outputs": {
            "optimization_variant_summary": "optimization_variant_summary.csv",
            "institutional_regime_variants": "institutional_regime_variants.csv",
            "institutional_loss_cap_upper_bound": "institutional_loss_cap_upper_bound.csv",
            "bear_extension_variants": "bear_extension_variants.csv",
            "institutional_context_buckets": "institutional_context_buckets.csv",
            "institutional_worst_trades": "institutional_worst_trades.csv",
            "old_g3_route_efficiency": "old_g3_route_efficiency.csv",
            "panic_repair_annual": "panic_repair_annual.csv",
            "panic_repair_context_buckets": "panic_repair_context_buckets.csv",
            "bear_mode_library_audit": "bear_mode_library_audit.csv",
            "shadow_consistency_current_check": "shadow_consistency_current_check.csv",
        },
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# G3 状态路由优化审计 v1",
        "",
        "## 基准表现",
        "",
        _md_table(pd.DataFrame([baseline_metrics]), pct_cols={"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "best_trade"}),
        "",
        "## 优化候选对比",
        "",
        _md_table(
            variants_df[
                [
                    "model",
                    "trades",
                    "total_return",
                    "max_drawdown",
                    "win_rate",
                    "avg_trade_return",
                    "worst_trade",
                    "best_trade",
                    "trade_delta_vs_baseline",
                    "return_delta_vs_baseline",
                    "dd_delta_vs_baseline",
                ]
            ],
            pct_cols={"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "best_trade", "return_delta_vs_baseline", "dd_delta_vs_baseline"},
        ),
        "",
        "## 机构主升大亏样本",
        "",
        _md_table(inst_worst, pct_cols={"net_ret", "up_rate_prev", "big_down_rate_prev", "limit_down_proxy_rate", "mom20", "mom60", "breadth_ma20", "breadth_ma60", "median_mom20"}, max_rows=12),
        "",
        "## 机构主升上下文桶",
        "",
        _md_table(inst_context, pct_cols={"avg_ret", "win_rate", "big_loss_rate", "worst_trade", "best_trade"}, max_rows=80),
        "",
        "## 旧 G3 子路由效率",
        "",
        _md_table(old_eff, pct_cols={"avg_ret", "win_rate", "worst_trade", "best_trade", "total_sum_ret"}),
        "",
        "## panic_repair 年度拆分",
        "",
        _md_table(panic_annual, pct_cols={"sum_ret", "avg_ret", "win_rate", "worst_trade", "best_trade"}),
        "",
        "## panic_repair 场景拆分",
        "",
        _md_table(panic_context, pct_cols={"avg_ret", "win_rate", "worst_trade", "best_trade"}, max_rows=60),
        "",
        "## 当前 shadow 一致性检查",
        "",
        _md_table(consistency, max_rows=20),
        "",
        "## 初步结论",
        "",
        "- 本报告只做参数候选审计，不直接改正式 G3 历史包。",
        "- 机构主升降亏必须优先选择低损害规则；含日期的过滤只作为诊断上限，不作为实盘规则。",
        "- 旧 G3 应按子路由保留高效率分支，低效率分支降为观察。",
        "- panic_repair 的问题需要先分清交易数不足还是单笔收益不足，再决定扩样或提纯。",
    ]
    report_cols = [
        "model",
        "trades",
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "best_trade",
        "trade_delta_vs_baseline",
        "return_delta_vs_baseline",
        "dd_delta_vs_baseline",
    ]
    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "best_trade",
        "return_delta_vs_baseline",
        "dd_delta_vs_baseline",
    }
    lines = [
        "# G3 状态路由优化审计 v1",
        "",
        "## 基准表现",
        "",
        _md_table(pd.DataFrame([baseline_metrics]), pct_cols={"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "best_trade"}),
        "",
        "## 优化候选总览",
        "",
        _md_table(variants_df[report_cols].sort_values("total_return", ascending=False), pct_cols=pct_cols),
        "",
        "## 机构行情适配候选",
        "",
        _md_table(
            variants_df[variants_df["model"].astype(str).str.contains("inst_gate|inst_regime", regex=True)][report_cols].sort_values("total_return", ascending=False),
            pct_cols=pct_cols,
        ),
        "",
        "## 机构失败退出上限审计",
        "",
        "说明：当前离线样本缺少持仓期间路径，以下只代表“若能在该亏损附近失败退出”的收益上限，不等同于已经验证可执行的盘中止损。",
        "",
        _md_table(
            variants_df[variants_df["model"].astype(str).str.contains("loss_cap", regex=False)][report_cols].sort_values("total_return", ascending=False),
            pct_cols=pct_cols,
        ),
        "",
        "## 熊市模式扩展审计",
        "",
        _md_table(
            variants_df[variants_df["model"].astype(str).str.contains("bear_add", regex=False)][report_cols].sort_values("total_return", ascending=False),
            pct_cols=pct_cols,
        ),
        "",
        "## 熊市模式库单体表现",
        "",
        _md_table(bear_library, pct_cols={"bear_sum_ret", "bear_avg_ret", "bear_win_rate", "bear_big_loss_rate", "bear_worst_trade", "bear_best_trade"}),
        "",
        "## 机构主升大亏样本",
        "",
        _md_table(inst_worst, pct_cols={"net_ret", "up_rate_prev", "big_down_rate_prev", "limit_down_proxy_rate", "mom20", "mom60", "breadth_ma20", "breadth_ma60", "median_mom20"}, max_rows=12),
        "",
        "## 机构主升上下文桶",
        "",
        _md_table(inst_context, pct_cols={"avg_ret", "win_rate", "big_loss_rate", "worst_trade", "best_trade"}, max_rows=80),
        "",
        "## 旧 G3 子路由效率",
        "",
        _md_table(old_eff, pct_cols={"avg_ret", "win_rate", "worst_trade", "best_trade", "total_sum_ret"}),
        "",
        "## panic_repair 年度拆分",
        "",
        _md_table(panic_annual, pct_cols={"sum_ret", "avg_ret", "win_rate", "worst_trade", "best_trade"}),
        "",
        "## panic_repair 场景拆分",
        "",
        _md_table(panic_context, pct_cols={"avg_ret", "win_rate", "worst_trade", "best_trade"}, max_rows=60),
        "",
        "## 当前 shadow 一致性检查",
        "",
        _md_table(consistency, max_rows=20),
        "",
        "## 初步结论",
        "",
        "- 机构主升的无日期适配器以滚动 360/720 天历史收益健康度最有价值，既能学习近期赚钱模式，又不依赖 2024-09 这样的固定日期。",
        "- 机构主升最主要的优化空间不是删掉收益源，而是建立失败退出；-15% 附近的亏损截断上限已经能提高总收益并小幅降低回撤。",
        "- 熊市不是简单把 range_weak_rebound、old_g3_guarded 加回路由；本轮审计显示扩交易数会稀释收益，应继续做更窄的熊市赚钱模式，而不是泛化低吸。",
        "- panic_repair 的熊市单体仍有正收益，问题更像是样本数量不足和场景太窄，不是完全失效。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8-sig", newline="\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
