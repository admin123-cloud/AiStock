from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _sql_literal  # noqa: E402
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table, simulate_scaled, summarize  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_v4_ice_down_panic_30m_confirm_overlay_v1" / "base_candidates_with_ice_30m_confirm.csv"
LOCK_TAGS = ROOT / "reports" / "gen3_v4_ice_down_panic_profit_lock_v1" / "profit_lock_tags.csv"
OUT_DIR = ROOT / "reports" / "gen3_v4_ice_down_panic_lock5_execution_combo_v1"
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


def load_lock_tags() -> pd.DataFrame:
    d = pd.read_csv(LOCK_TAGS, low_memory=False, encoding="utf-8-sig")
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["lock5_exit_datetime"] = pd.to_datetime(d["lock5_exit_datetime"], errors="coerce")
    d["code"] = d["code"].astype(str)
    for col in ["entry_price", "policy_net_ret", "lock5_net_ret"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["lock5_triggered"] = d["lock5_triggered"].fillna(False).astype(bool)
    return d


def load_bars(tags: pd.DataFrame) -> pd.DataFrame:
    trig = tags[tags["lock5_triggered"]].copy()
    codes = sorted(trig["code"].dropna().astype(str).unique().tolist())
    if not codes:
        return pd.DataFrame()
    start = trig["entry_date"].min().strftime("%Y-%m-%d")
    end = trig["policy_exit_date"].max().strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 200):
        quoted = ",".join(_sql_literal(code) for code in codes[i : i + 200])
        sql = f"""
        SELECT code, datetime, open, high, low, close
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
    for col in ["open", "high", "low", "close"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    return bars.dropna(subset=["code", "datetime", "open", "close"]).sort_values(["code", "datetime"]).copy()


def add_delay_exits(tags: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    out = tags.copy()
    out["lock5_next_open_net_ret"] = out["lock5_net_ret"]
    out["lock5_next_close_net_ret"] = out["lock5_net_ret"]
    out["lock5_delay_missing"] = False
    grouped = {code: g.sort_values("datetime").reset_index(drop=True) for code, g in bars.groupby("code")}
    for idx, row in out[out["lock5_triggered"]].iterrows():
        g = grouped.get(str(row["code"]), pd.DataFrame())
        later = g[g["datetime"].gt(row["lock5_exit_datetime"])].head(1)
        if later.empty:
            out.loc[idx, "lock5_delay_missing"] = True
            continue
        entry_price = float(row["entry_price"])
        out.loc[idx, "lock5_next_open_net_ret"] = float(later.iloc[0]["open"]) / entry_price - 1.0 - BASE_COST_BPS / 10000.0
        out.loc[idx, "lock5_next_close_net_ret"] = float(later.iloc[0]["close"]) / entry_price - 1.0 - BASE_COST_BPS / 10000.0
    return out


def apply_variant(candidates: pd.DataFrame, tags: pd.DataFrame, variant: str) -> pd.DataFrame:
    d = candidates.copy()
    d["variant"] = variant
    ice_down = d["route"].astype(str).eq("down_panic") & d["emotion_signal"].astype(str).eq("icepoint")
    if variant in {"ice150", "ice150_lock5", "ice150_lock5_next_open", "ice150_lock5_next_close", "ice150_lock5_no_tail"}:
        d.loc[ice_down, "position_scale"] *= 1.50
    ret_col = {
        "base": None,
        "lock5": "lock5_net_ret",
        "lock5_next_open": "lock5_next_open_net_ret",
        "lock5_next_close": "lock5_next_close_net_ret",
        "ice150": None,
        "ice150_lock5": "lock5_net_ret",
        "ice150_lock5_next_open": "lock5_next_open_net_ret",
        "ice150_lock5_next_close": "lock5_next_close_net_ret",
        "ice150_lock5_no_tail": "lock5_net_ret",
    }[variant]
    if ret_col is None:
        return d
    small = tags[["entry_date", "code", "lock5_triggered", ret_col, "lock5_exit_datetime", "lock5_delay_missing"]].copy()
    merged = d.merge(small, on=["entry_date", "code"], how="left")
    mask = (
        merged["route"].astype(str).eq("down_panic")
        & merged["emotion_signal"].astype(str).eq("icepoint")
        & merged["lock5_triggered"].fillna(False).astype(bool)
        & merged[ret_col].notna()
    )
    if variant == "ice150_lock5_no_tail":
        exit_time = pd.to_datetime(merged["lock5_exit_datetime"], errors="coerce").dt.strftime("%H:%M:%S")
        mask = mask & exit_time.lt("14:30:00")
    merged.loc[mask, "policy_net_ret"] = pd.to_numeric(merged.loc[mask, ret_col], errors="coerce")
    return merged


def apply_stress(candidates: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    extra_cost = (float(profile["cost_bps"]) - BASE_COST_BPS) / 10000.0
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce") - extra_cost - float(profile.get("all_shock", 0.0))
    d["stress_profile"] = profile["profile"]
    return d


def execution_summary(tags: pd.DataFrame) -> pd.DataFrame:
    trig = tags[tags["lock5_triggered"]].copy()
    if trig.empty:
        return pd.DataFrame()
    trig["exit_time"] = pd.to_datetime(trig["lock5_exit_datetime"], errors="coerce").dt.strftime("%H:%M:%S")
    rows: list[dict[str, Any]] = []
    for key, g in trig.groupby("exit_time", dropna=False):
        rows.append(
            {
                "exit_time": key,
                "triggered": int(len(g)),
                "avg_lock5_ret": float(pd.to_numeric(g["lock5_net_ret"], errors="coerce").mean()),
                "avg_next_open_ret": float(pd.to_numeric(g["lock5_next_open_net_ret"], errors="coerce").mean()),
                "avg_next_close_ret": float(pd.to_numeric(g["lock5_next_close_net_ret"], errors="coerce").mean()),
                "delay_missing": int(g["lock5_delay_missing"].fillna(False).astype(bool).sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("exit_time")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates = load_candidates()
    tags = load_lock_tags()
    bars = load_bars(tags)
    tags = add_delay_exits(tags, bars)
    tags.to_csv(OUT_DIR / "lock5_execution_tags.csv", index=False, encoding="utf-8-sig")
    exec_sum = execution_summary(tags)
    exec_sum.to_csv(OUT_DIR / "lock5_execution_summary.csv", index=False, encoding="utf-8-sig")

    variants = [
        "base",
        "lock5",
        "lock5_next_open",
        "lock5_next_close",
        "ice150",
        "ice150_lock5",
        "ice150_lock5_next_open",
        "ice150_lock5_next_close",
        "ice150_lock5_no_tail",
    ]
    summaries: list[dict[str, Any]] = []
    for variant in variants:
        v = apply_variant(candidates, tags, variant)
        v.to_csv(OUT_DIR / f"{variant}_candidates.csv", index=False, encoding="utf-8-sig")
        for profile in PROFILES:
            stressed = apply_stress(v, profile)
            curve, closed = simulate_scaled(stressed, float(profile["cost_bps"]))
            run_dir = OUT_DIR / f"{variant}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summaries.append(summarize(curve, closed, variant, str(profile["profile"])))
    summary = pd.DataFrame(summaries)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret", "avg_lock5_ret", "avg_next_open_ret", "avg_next_close_ret"}
    lines = [
        "# G3 V4 icepoint down_panic lock5 执行与加仓组合 v1",
        "",
        "## 执行分布",
        "",
        md_table(exec_sum, pct_cols=pct_cols),
        "",
        "## 组合复算",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 判断口径",
        "",
        "- `lock5_next_open/close` 用触发后下一根 30m 成交，测试是否吃理想 30m close。",
        "- `ice150_lock5*` 测试冰点 down_panic 加仓 1.5 倍后，lock5 是否能守住压力口径。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
