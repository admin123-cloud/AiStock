from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _sql_literal  # noqa: E402
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table, simulate_scaled, summarize  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_v4_icepoint_climax_overlay_v1" / "base_candidates_with_emotion.csv"
OUT_DIR = ROOT / "reports" / "gen3_v4_ice_down_panic_30m_confirm_overlay_v1"
BASE_COST_BPS = 30.0


PROFILES = [
    {"profile": "cost30", "cost_bps": 30.0, "all_shock": 0.0},
    {"profile": "cost100", "cost_bps": 100.0, "all_shock": 0.0},
    {"profile": "cost30_all_shock2", "cost_bps": 30.0, "all_shock": 0.02},
]


def load_candidates() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False, encoding="utf-8-sig")
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["entry_price", "policy_net_ret", "position_scale", "score", "route_priority"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["position_scale"] = d.get("position_scale", 1.0).fillna(1.0)
    return d.dropna(subset=["entry_date", "policy_exit_date", "code", "entry_price", "policy_net_ret"]).copy()


def load_30m_bars(candidates: pd.DataFrame) -> pd.DataFrame:
    target = candidates[candidates["route"].astype(str).eq("down_panic") & candidates["emotion_signal"].astype(str).eq("icepoint")]
    if target.empty:
        return pd.DataFrame()
    codes = sorted(target["code"].dropna().astype(str).unique().tolist())
    start = target["entry_date"].min().strftime("%Y-%m-%d")
    end = target["entry_date"].max().strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 200):
        quoted = ",".join(_sql_literal(code) for code in codes[i : i + 200])
        sql = f"""
        SELECT code, datetime, open, high, low, close, amount
        FROM kline_minute_30
        WHERE code IN ({quoted})
          AND datetime >= toDateTime({_sql_literal(start + " 09:00:00")})
          AND datetime <= toDateTime({_sql_literal(end + " 15:30:00")})
        ORDER BY code, datetime
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if bars.empty:
        return bars
    bars["code"] = bars["code"].astype(str)
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["entry_date"] = bars["datetime"].dt.normalize()
    bars["bar_time"] = bars["datetime"].dt.strftime("%H:%M:%S")
    for col in ["open", "high", "low", "close", "amount"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars = bars.dropna(subset=["code", "datetime", "entry_date", "open", "high", "low", "close"]).sort_values(["code", "entry_date", "datetime"])
    g = bars.groupby(["code", "entry_date"], sort=False)
    bars["prev_bar_high"] = g["high"].shift(1)
    bars["intraday_low_so_far"] = g["low"].cummin()
    bars["prev_intraday_low"] = g["intraday_low_so_far"].shift(1)
    bars["amount_ma3_prev"] = g["amount"].transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
    bar_range = bars["high"] - bars["low"]
    bars["bar_close_pos"] = np.where(bar_range > 0, (bars["close"] - bars["low"]) / bar_range, np.nan)
    bars["bar_ret"] = bars["close"] / bars["open"] - 1.0
    bars["amount_ratio3"] = bars["amount"] / bars["amount_ma3_prev"]
    return bars.replace([np.inf, -np.inf], np.nan)


def build_confirm_tags(candidates: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    target = candidates[candidates["route"].astype(str).eq("down_panic") & candidates["emotion_signal"].astype(str).eq("icepoint")][
        ["entry_date", "code", "name", "policy_net_ret"]
    ].copy()
    if target.empty:
        return pd.DataFrame()
    joined = bars.merge(target[["entry_date", "code"]], on=["entry_date", "code"], how="inner")
    joined = joined[joined["bar_time"].ge("10:00:00")].copy()
    lower_reclaim = (joined["bar_close_pos"] >= 0.70) & (joined["bar_ret"] > -0.005)
    break_prev = joined["close"] > joined["prev_bar_high"]
    common_reversal = (
        (joined["close"] > joined["open"])
        & (joined["bar_close_pos"] >= 0.58)
        & (joined["amount_ratio3"].fillna(1.0) >= 0.85)
    )
    no_new_low = joined["low"] >= joined["prev_intraday_low"].fillna(joined["low"])
    joined["strict_1030"] = (
        joined["bar_time"].le("10:30:00")
        & ((lower_reclaim | break_prev) & (joined["amount_ratio3"].fillna(1.0) >= 1.0))
    )
    joined["loose_am"] = (
        joined["bar_time"].le("11:30:00")
        & ((lower_reclaim | break_prev | common_reversal | no_new_low) & (joined["amount_ratio3"].fillna(1.0) >= 0.85))
    )
    parts: list[pd.DataFrame] = []
    for rule in ["strict_1030", "loose_am"]:
        d = joined[joined[rule]].sort_values(["entry_date", "code", "datetime"]).groupby(["entry_date", "code"], as_index=False).first()
        d = d[["entry_date", "code", "datetime", "close", "bar_time", "bar_close_pos", "bar_ret", "amount_ratio3"]].copy()
        d = d.rename(columns={"datetime": f"{rule}_confirm_datetime", "close": f"{rule}_confirm_price"})
        d[f"{rule}_confirmed"] = True
        parts.append(d)
    out = target.copy()
    for d in parts:
        out = out.merge(d, on=["entry_date", "code"], how="left")
    for rule in ["strict_1030", "loose_am"]:
        out[f"{rule}_confirmed"] = out[f"{rule}_confirmed"].fillna(False).astype(bool)
    return out


def attach_confirm(candidates: pd.DataFrame, tags: pd.DataFrame) -> pd.DataFrame:
    d = candidates.merge(tags.drop(columns=["name", "policy_net_ret"], errors="ignore"), on=["entry_date", "code"], how="left")
    for rule in ["strict_1030", "loose_am"]:
        d[f"{rule}_confirmed"] = d[f"{rule}_confirmed"].fillna(False).astype(bool)
    return d


def apply_overlay(candidates: pd.DataFrame, variant: str) -> pd.DataFrame:
    d = candidates.copy()
    d["overlay_variant"] = variant
    d["overlay_note"] = "base"
    ice_down = d["route"].astype(str).eq("down_panic") & d["emotion_signal"].astype(str).eq("icepoint")
    if variant == "base":
        return d
    if variant == "ice_down_panic_150":
        d.loc[ice_down, "position_scale"] *= 1.50
        d.loc[ice_down, "overlay_note"] = "ice_down_panic_150"
        return d
    config = {
        "ice_down_strict1030_110": ("strict_1030_confirmed", 1.10),
        "ice_down_strict1030_125": ("strict_1030_confirmed", 1.25),
        "ice_down_strict1030_150": ("strict_1030_confirmed", 1.50),
        "ice_down_looseam_125": ("loose_am_confirmed", 1.25),
        "ice_down_looseam_150": ("loose_am_confirmed", 1.50),
    }
    if variant == "ice_down_strict1030_125_unconfirmed_half":
        confirmed = ice_down & d["strict_1030_confirmed"].fillna(False).astype(bool)
        unconfirmed = ice_down & ~d["strict_1030_confirmed"].fillna(False).astype(bool)
        d.loc[confirmed, "position_scale"] *= 1.25
        d.loc[unconfirmed, "position_scale"] *= 0.50
        d.loc[confirmed, "overlay_note"] = "strict1030_boost125"
        d.loc[unconfirmed, "overlay_note"] = "strict1030_unconfirmed_half"
        return d
    if variant not in config:
        raise ValueError(variant)
    confirm_col, scale = config[variant]
    mask = ice_down & d[confirm_col].fillna(False).astype(bool)
    d.loc[mask, "position_scale"] *= scale
    d.loc[mask, "overlay_note"] = variant
    return d


def apply_stress(candidates: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    extra_cost = (float(profile["cost_bps"]) - BASE_COST_BPS) / 10000.0
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce") - extra_cost - float(profile.get("all_shock", 0.0))
    d["stress_profile"] = profile["profile"]
    return d


def confirm_summary(candidates: pd.DataFrame) -> pd.DataFrame:
    target = candidates[candidates["route"].astype(str).eq("down_panic") & candidates["emotion_signal"].astype(str).eq("icepoint")].copy()
    rows: list[dict[str, Any]] = []
    for rule in ["strict_1030", "loose_am"]:
        for flag, g in target.groupby(f"{rule}_confirmed"):
            ret = pd.to_numeric(g["policy_net_ret"], errors="coerce")
            rows.append(
                {
                    "rule": rule,
                    "confirmed": bool(flag),
                    "candidate_count": int(len(g)),
                    "avg_policy_ret": float(ret.mean()),
                    "win_rate": float((ret > 0).mean()),
                }
            )
    return pd.DataFrame(rows)


def annual_summary(closed: pd.DataFrame, variant: str, profile: str) -> pd.DataFrame:
    d = closed.copy()
    d["year"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.year
    rows: list[dict[str, Any]] = []
    for year, g in d.groupby("year"):
        rows.append(
            {
                "variant": variant,
                "profile": profile,
                "year": int(year),
                "trade_count": int(len(g)),
                "pnl": float(pd.to_numeric(g["realized_pnl"], errors="coerce").sum()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_candidates()
    tag_path = OUT_DIR / "down_panic_ice_30m_confirm_tags.csv"
    if tag_path.exists():
        tags = pd.read_csv(tag_path, low_memory=False, encoding="utf-8-sig")
        tags["entry_date"] = pd.to_datetime(tags["entry_date"], errors="coerce").dt.normalize()
    else:
        bars = load_30m_bars(base)
        tags = build_confirm_tags(base, bars)
        tags.to_csv(tag_path, index=False, encoding="utf-8-sig")
    candidates = attach_confirm(base, tags)
    candidates.to_csv(OUT_DIR / "base_candidates_with_ice_30m_confirm.csv", index=False, encoding="utf-8-sig")
    conf_sum = confirm_summary(candidates)
    conf_sum.to_csv(OUT_DIR / "confirm_summary.csv", index=False, encoding="utf-8-sig")

    variants = [
        "base",
        "ice_down_panic_150",
        "ice_down_strict1030_110",
        "ice_down_strict1030_125",
        "ice_down_strict1030_150",
        "ice_down_strict1030_125_unconfirmed_half",
        "ice_down_looseam_125",
        "ice_down_looseam_150",
    ]
    summaries: list[dict[str, Any]] = []
    annual_frames: list[pd.DataFrame] = []
    for variant in variants:
        overlaid = apply_overlay(candidates, variant)
        overlaid.to_csv(OUT_DIR / f"{variant}_candidates.csv", index=False, encoding="utf-8-sig")
        for profile in PROFILES:
            stressed = apply_stress(overlaid, profile)
            curve, closed = simulate_scaled(stressed, float(profile["cost_bps"]))
            run_dir = OUT_DIR / f"{variant}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summaries.append(summarize(curve, closed, variant, str(profile["profile"])))
            annual_frames.append(annual_summary(closed, variant, str(profile["profile"])))
    summary = pd.DataFrame(summaries)
    annual = pd.concat(annual_frames, ignore_index=True) if annual_frames else pd.DataFrame()
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret", "avg_policy_ret"}
    lines = [
        "# G3 V4 down_panic 冰点 + 30m 修复确认 overlay v1",
        "",
        "## 边界",
        "",
        "- 只作用于 `emotion_signal=icepoint` 且 `route=down_panic` 的候选。",
        "- `strict_1030`：10:30 前出现下影收复或突破前 bar 高点，且 30m 成交额不弱于前 3 根均值。",
        "- `loose_am`：上午出现下影收复、突破前 bar、收红且收盘位置较高，或不再创新低，成交额不弱于前 3 根均值的 85%。",
        "- 不使用 score/rank，不改变 strong_main/bigbull/range_gap。",
        "",
        "## 30m 确认覆盖",
        "",
        md_table(conf_sum, pct_cols=pct_cols),
        "",
        "## 总体结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 初步判断",
        "",
        "- 如果确认后加仓在 cost30、100bps、all_shock2 三个口径都优于无确认加仓，说明 30m 修复确认有保护价值。",
        "- 如果 cost30 改善但 all_shock2 仍弱于 base，说明冰点加仓仍然只是增强标签，不能正式化。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
