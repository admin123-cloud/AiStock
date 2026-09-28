from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _sql_literal  # noqa: E402
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table, simulate_scaled, summarize  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = (
    _report_path()
    / "gen3_v4_bigbull_four_state_gate_combo_v1"
    / "g3_plus_bigbull_uptrend_or_weak_rebound_quarter_d1_weak_full_exit_candidates.csv"
)
OUT_DIR = _report_path() / "gen3_v4_icepoint_climax_overlay_v1"
INDEX_CODE = "999999.SH"
BASE_COST_BPS = 30.0
ROLL_WINDOW = 252
QUANTILES = (90, 92, 97, 99)


PROFILES = [
    {"profile": "cost30", "cost_bps": 30.0, "all_shock": 0.0},
    {"profile": "cost100", "cost_bps": 100.0, "all_shock": 0.0},
    {"profile": "cost30_all_shock2", "cost_bps": 30.0, "all_shock": 0.02},
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


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


def load_trade_dates(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    sql = f"""
    SELECT trade_date, open, close
    FROM kline_daily
    WHERE code = {_sql_literal(INDEX_CODE)}
      AND trade_date BETWEEN toDate({_sql_literal(start.strftime('%Y-%m-%d'))})
                         AND toDate({_sql_literal(end.strftime('%Y-%m-%d'))})
    ORDER BY trade_date
    """
    d = clickhouse_query_df(sql)
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    return d.dropna(subset=["trade_date"]).drop_duplicates("trade_date").sort_values("trade_date").reset_index(drop=True)


def load_market_counts(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    stock_sql = """
    SELECT code
    FROM stocks
    WHERE type = 'stock'
      AND quit = 0
      AND (st = 0 OR st IS NULL)
    ORDER BY code
    """
    stock_df = clickhouse_query_df(stock_sql)
    codes = stock_df["code"].dropna().astype(str).drop_duplicates().tolist()
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 300):
        quoted = ",".join(_sql_literal(code) for code in codes[i : i + 300])
        sql = f"""
        WITH daily_prices AS (
            SELECT
                substring(code, 1, 6) AS plain_code,
                toDate(trade_date) AS trade_date,
                toFloat64(close) AS close,
                lagInFrame(toFloat64(close), 1) OVER (PARTITION BY code ORDER BY trade_date) AS close_prev
            FROM kline_daily
            WHERE code IN ({quoted})
              AND trade_date BETWEEN toDate({_sql_literal(start.strftime('%Y-%m-%d'))})
                                  AND toDate({_sql_literal(end.strftime('%Y-%m-%d'))})
        )
        SELECT
            trade_date,
            count() AS stock_cnt,
            sum((close / close_prev - 1.0) >= multiIf(
                startsWith(plain_code, '300') OR startsWith(plain_code, '688'), 0.199,
                substring(plain_code, 1, 2) IN ('43', '83', '87', '92'), 0.299,
                0.099
            )) AS limit_up_count,
            sum((close / close_prev - 1.0) <= -multiIf(
                startsWith(plain_code, '300') OR startsWith(plain_code, '688'), 0.199,
                substring(plain_code, 1, 2) IN ('43', '83', '87', '92'), 0.299,
                0.099
            )) AS limit_down_count
        FROM daily_prices
        WHERE close_prev > 0
        GROUP BY trade_date
        ORDER BY trade_date
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    d = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if d.empty:
        return d
    d = d.groupby("trade_date", as_index=False)[["stock_cnt", "limit_up_count", "limit_down_count"]].sum()
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    for col in ["stock_cnt", "limit_up_count", "limit_down_count"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["trade_date", "stock_cnt", "limit_up_count", "limit_down_count"]).copy()


def build_emotion_events(candidates: pd.DataFrame) -> pd.DataFrame:
    start = candidates["entry_date"].min() - pd.Timedelta(days=420)
    end = candidates["entry_date"].max()
    dates = load_trade_dates(start, end)
    market = load_market_counts(start, end)
    d = dates[["trade_date"]].merge(market, on="trade_date", how="left").sort_values("trade_date").reset_index(drop=True)
    for col in ["stock_cnt", "limit_up_count", "limit_down_count"]:
        d[col] = pd.to_numeric(d[col], errors="coerce").fillna(0.0)
    min_periods = max(30, int(ROLL_WINDOW * 0.35))
    for q in QUANTILES:
        qv = q / 100.0
        d[f"down_q{q}"] = d["limit_down_count"].rolling(ROLL_WINDOW, min_periods=min_periods).quantile(qv)
        d[f"up_q{q}"] = d["limit_up_count"].rolling(ROLL_WINDOW, min_periods=min_periods).quantile(qv)
    d["ice_score"] = 0.0
    d.loc[d["limit_down_count"].ge(d["down_q90"]), "ice_score"] = 0.6
    d.loc[d["limit_down_count"].ge(d["down_q92"]), "ice_score"] = 1.0
    d["climax_score"] = 0.0
    d.loc[d["limit_up_count"].ge(d["up_q97"]), "climax_score"] = 0.6
    d.loc[d["limit_up_count"].ge(d["up_q99"]), "climax_score"] = 1.0
    d["emotion_score"] = d["ice_score"] - d["climax_score"]
    d["emotion_signal"] = "neutral"
    d.loc[d["emotion_score"].gt(0), "emotion_signal"] = "icepoint"
    d.loc[d["emotion_score"].lt(0), "emotion_signal"] = "climax"
    d["signal_date"] = d["trade_date"]
    d["entry_date"] = d["trade_date"].shift(-1)
    cols = [
        "signal_date",
        "entry_date",
        "stock_cnt",
        "limit_up_count",
        "limit_down_count",
        "down_q90",
        "down_q92",
        "up_q97",
        "up_q99",
        "ice_score",
        "climax_score",
        "emotion_score",
        "emotion_signal",
    ]
    return d[cols].dropna(subset=["entry_date"]).copy()


def attach_emotion(candidates: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    d = candidates.merge(events, on="entry_date", how="left")
    d["emotion_signal"] = d["emotion_signal"].fillna("neutral")
    d["emotion_score"] = pd.to_numeric(d["emotion_score"], errors="coerce").fillna(0.0)
    d["ice_score"] = pd.to_numeric(d["ice_score"], errors="coerce").fillna(0.0)
    d["climax_score"] = pd.to_numeric(d["climax_score"], errors="coerce").fillna(0.0)
    return d


def apply_overlay(candidates: pd.DataFrame, variant: str) -> pd.DataFrame:
    d = candidates.copy()
    d["overlay_variant"] = variant
    d["overlay_note"] = "base"
    ice = d["emotion_signal"].eq("icepoint")
    climax = d["emotion_signal"].eq("climax")
    if variant == "base":
        return d
    if variant == "climax_half":
        d.loc[climax, "position_scale"] = d.loc[climax, "position_scale"] * 0.5
        d.loc[climax, "overlay_note"] = "climax_half"
        return d
    if variant == "climax_pause":
        d = d[~climax].copy()
        d.loc[:, "overlay_note"] = "climax_pause"
        return d
    if variant == "ice_boost_125":
        d.loc[ice, "position_scale"] = d.loc[ice, "position_scale"] * 1.25
        d.loc[ice, "overlay_note"] = "ice_boost_125"
        return d
    if variant == "ice_boost_150":
        d.loc[ice, "position_scale"] = d.loc[ice, "position_scale"] * 1.50
        d.loc[ice, "overlay_note"] = "ice_boost_150"
        return d
    if variant == "ice125_climax_half":
        d.loc[ice, "position_scale"] = d.loc[ice, "position_scale"] * 1.25
        d.loc[climax, "position_scale"] = d.loc[climax, "position_scale"] * 0.5
        d.loc[ice, "overlay_note"] = "ice_boost_125"
        d.loc[climax, "overlay_note"] = "climax_half"
        return d
    if variant == "ice125_climax_pause":
        d.loc[ice, "position_scale"] = d.loc[ice, "position_scale"] * 1.25
        d.loc[ice, "overlay_note"] = "ice_boost_125"
        d = d[~climax].copy()
        return d
    if variant == "ice_down_panic_125":
        mask = ice & d["route"].astype(str).eq("down_panic")
        d.loc[mask, "position_scale"] = d.loc[mask, "position_scale"] * 1.25
        d.loc[mask, "overlay_note"] = "ice_down_panic_125"
        return d
    if variant == "ice_down_panic_150":
        mask = ice & d["route"].astype(str).eq("down_panic")
        d.loc[mask, "position_scale"] = d.loc[mask, "position_scale"] * 1.50
        d.loc[mask, "overlay_note"] = "ice_down_panic_150"
        return d
    if variant == "ice_down_panic_125_climax_none":
        mask = ice & d["route"].astype(str).eq("down_panic")
        d.loc[mask, "position_scale"] = d.loc[mask, "position_scale"] * 1.25
        d.loc[mask, "overlay_note"] = "ice_down_panic_125"
        return d
    raise ValueError(variant)


def apply_stress(candidates: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    extra_cost = (float(profile["cost_bps"]) - BASE_COST_BPS) / 10000.0
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce") - extra_cost - float(profile.get("all_shock", 0.0))
    d["stress_profile"] = profile["profile"]
    return d


def emotion_trade_summary(closed: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for keys, g in closed.groupby(["route", "emotion_signal"], dropna=False):
        route, signal = keys
        ret = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        rows.append(
            {
                "route": route,
                "emotion_signal": signal,
                "trade_count": int(len(g)),
                "avg_ret": float(ret.mean()),
                "win_rate": float((ret > 0).mean()),
                "pnl": float(pd.to_numeric(g["realized_pnl"], errors="coerce").sum()),
                "avg_scale": float(pd.to_numeric(g.get("position_scale"), errors="coerce").mean()),
            }
        )
    return pd.DataFrame(rows).sort_values(["route", "emotion_signal"])


def annual_summary(closed: pd.DataFrame, variant: str, profile: str) -> pd.DataFrame:
    d = closed.copy()
    d["year"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.year
    rows: list[dict[str, Any]] = []
    for year, g in d.groupby("year"):
        ret = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        rows.append(
            {
                "variant": variant,
                "profile": profile,
                "year": int(year),
                "trade_count": int(len(g)),
                "avg_ret": float(ret.mean()),
                "win_rate": float((ret > 0).mean()),
                "pnl": float(pd.to_numeric(g["realized_pnl"], errors="coerce").sum()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_candidates()
    event_path = OUT_DIR / "emotion_events_d1_visible.csv"
    if event_path.exists():
        events = pd.read_csv(event_path, low_memory=False, encoding="utf-8-sig")
        for col in ["signal_date", "entry_date"]:
            events[col] = pd.to_datetime(events[col], errors="coerce").dt.normalize()
    else:
        events = build_emotion_events(base)
        events.to_csv(event_path, index=False, encoding="utf-8-sig")
    candidates = attach_emotion(base, events)
    candidates.to_csv(OUT_DIR / "base_candidates_with_emotion.csv", index=False, encoding="utf-8-sig")

    variants = [
        "base",
        "climax_half",
        "climax_pause",
        "ice_boost_125",
        "ice_boost_150",
        "ice125_climax_half",
        "ice125_climax_pause",
        "ice_down_panic_125",
        "ice_down_panic_150",
    ]
    summaries: list[dict[str, Any]] = []
    emotion_frames: list[pd.DataFrame] = []
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
            es = emotion_trade_summary(closed)
            es["variant"] = variant
            es["profile"] = str(profile["profile"])
            emotion_frames.append(es)
            annual_frames.append(annual_summary(closed, variant, str(profile["profile"])))

    summary = pd.DataFrame(summaries)
    emotion_summary = pd.concat(emotion_frames, ignore_index=True) if emotion_frames else pd.DataFrame()
    annual = pd.concat(annual_frames, ignore_index=True) if annual_frames else pd.DataFrame()
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    emotion_summary.to_csv(OUT_DIR / "emotion_route_summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_summary.csv", index=False, encoding="utf-8-sig")

    event_count = candidates.groupby(["emotion_signal", "route"]).size().reset_index(name="candidate_count")
    event_count.to_csv(OUT_DIR / "candidate_count_by_emotion_route.csv", index=False, encoding="utf-8-sig")

    focus = summary[summary["profile"].eq("cost30")].sort_values("total_return", ascending=False)
    stress = summary[summary["profile"].isin(["cost100", "cost30_all_shock2"])].sort_values(["profile", "total_return"], ascending=[True, False])
    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"}
    lines = [
        "# G3 V4 冰点/高潮择时 overlay 复验 v1",
        "",
        "## 复验边界",
        "",
        "- 复用 `icepoint_climax_reversal_session_summary.md` 的固定代表性分位：跌停 90/92、涨停 97/99、hold=5 思路。",
        "- 本次只使用 D-1 收盘后可见的涨跌停家数极值，信号映射到下一交易日 G3 开仓；不使用旧研究里的 `single` 未来修复过滤。",
        "- 不重新搜索参数，只验证冰点加仓/高潮减仓是否能改善当前 G3 V4 候选。",
        "",
        "## cost30 结果",
        "",
        md_table(focus, pct_cols=pct_cols),
        "",
        "## 压力结果",
        "",
        md_table(stress, pct_cols=pct_cols),
        "",
        "## 候选分布",
        "",
        md_table(event_count),
        "",
        "## 初步判断",
        "",
        "- 如果 `climax_half` 或 `climax_pause` 优于 base，说明高潮更适合作为减仓/暂停信号。",
        "- 如果 `ice_boost_*` 优于 base 且压力口径不恶化，说明冰点可作为加仓信号。",
        "- 若名义收益改善但 all_shock/100bps 明显恶化，则只能作为研究标签，不能纳入正式规则。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
