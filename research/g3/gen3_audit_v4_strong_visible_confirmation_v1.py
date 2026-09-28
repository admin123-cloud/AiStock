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
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SRC_SCALE_DIR = _report_path() / "gen3_v4_strong_position_scale_probe_v1"
SRC_STRESS_DIR = _report_path() / "gen3_v4_strong_second_093_execution_stress_v1"
OUT_DIR = _report_path() / "gen3_v4_strong_visible_confirmation_audit_v1"


def pct(v: Any) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def load_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    base_candidates = pd.read_csv(SRC_SCALE_DIR / "base_candidates.csv", low_memory=False)
    keep093 = pd.read_csv(SRC_SCALE_DIR / "strong_second_score_ge_093_candidates.csv", low_memory=False)
    close = pd.read_csv(SRC_STRESS_DIR / "close_30bps" / "closed_trades.csv", low_memory=False)
    nextopen = pd.read_csv(SRC_STRESS_DIR / "nextopen_30bps" / "closed_trades.csv", low_memory=False)
    for d in [base_candidates, keep093, close, nextopen]:
        d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
        d["code"] = d["code"].astype(str)
        for col in ["score", "strong_day_rank", "policy_net_ret", "realized_pnl", "stake"]:
            if col in d.columns:
                d[col] = pd.to_numeric(d[col], errors="coerce")
    return base_candidates, keep093, close, nextopen


def build_study_sets(base_candidates: pd.DataFrame, keep093: pd.DataFrame, close: pd.DataFrame, nextopen: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    kept_keys = set(zip(keep093["entry_date"], keep093["code"]))
    strong = base_candidates[base_candidates["route"].astype(str).eq("strong_main")].copy()
    strong["key"] = list(zip(strong["entry_date"], strong["code"]))
    skipped = strong[
        strong["strong_day_rank"].ge(2)
        & strong["score"].lt(0.93)
        & ~strong["key"].isin(kept_keys)
    ].copy()
    skipped["group"] = "skipped_by_093"
    skipped["sample_ret"] = skipped["policy_net_ret"]
    skipped["sample_pnl"] = pd.NA
    skipped["entry_year"] = skipped["entry_date"].dt.year

    keys = ["entry_date", "code", "route"]
    damage = close[keys + ["name", "score", "strong_day_rank", "policy_net_ret", "realized_pnl", "stake"]].merge(
        nextopen[keys + ["policy_net_ret", "realized_pnl", "policy_exit_date"]],
        on=keys,
        suffixes=("_close", "_nextopen"),
    )
    damage = damage[damage["route"].astype(str).eq("strong_main")].copy()
    damage["ret_delta_nextopen"] = damage["policy_net_ret_nextopen"] - damage["policy_net_ret_close"]
    damage["pnl_delta_nextopen"] = damage["realized_pnl_nextopen"] - damage["realized_pnl_close"]
    damage["group"] = "selected_strong_nextopen_damage"
    damage["sample_ret"] = damage["policy_net_ret_nextopen"]
    damage["sample_pnl"] = damage["pnl_delta_nextopen"]
    damage["entry_year"] = damage["entry_date"].dt.year
    return skipped, damage


def load_daily_context(samples: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(samples["code"].dropna().astype(str).unique().tolist())
    start = (pd.Timestamp(samples["entry_date"].min()) - pd.Timedelta(days=150)).strftime("%Y-%m-%d")
    end = (pd.Timestamp(samples["entry_date"].max()) + pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 300):
        quoted = ",".join(_sql_literal(c) for c in codes[i : i + 300])
        sql = f"""
        SELECT code, trade_date, open, high, low, close, volume
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN toDate({_sql_literal(start)}) AND toDate({_sql_literal(end)})
        ORDER BY code, trade_date
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    d = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if d.empty:
        return d
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close", "volume"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["code", "trade_date", "open", "high", "low", "close"]).sort_values(["code", "trade_date"])
    g = d.groupby("code", group_keys=False)
    d["prev_close"] = g["close"].shift(1)
    d["prev_high20"] = g["high"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).max())
    d["prev_high60"] = g["high"].transform(lambda s: s.shift(1).rolling(60, min_periods=30).max())
    d["prev_low60"] = g["low"].transform(lambda s: s.shift(1).rolling(60, min_periods=30).min())
    d["prev_ma20"] = g["close"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    d["prev_ma60"] = g["close"].transform(lambda s: s.shift(1).rolling(60, min_periods=30).mean())
    d["prev_vol20"] = g["volume"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    d["prev_ret5"] = g["close"].shift(1) / g["close"].shift(6) - 1.0
    d["prev_ret10"] = g["close"].shift(1) / g["close"].shift(11) - 1.0
    d["prev_ret20"] = g["close"].shift(1) / g["close"].shift(21) - 1.0
    d["prev_close_to_high20"] = d["prev_close"] / d["prev_high20"] - 1.0
    d["prev_close_to_high60"] = d["prev_close"] / d["prev_high60"] - 1.0
    d["prev_runup_60low"] = d["prev_close"] / d["prev_low60"] - 1.0
    d["prev_above_ma20"] = d["prev_close"] >= d["prev_ma20"]
    d["prev_above_ma60"] = d["prev_close"] >= d["prev_ma60"]
    d["entry_gap_open"] = d["open"] / d["prev_close"] - 1.0
    d["entry_close_ret"] = d["close"] / d["prev_close"] - 1.0
    d["entry_high_ret"] = d["high"] / d["prev_close"] - 1.0
    d["entry_low_ret"] = d["low"] / d["prev_close"] - 1.0
    d["entry_vol_ratio20"] = d["volume"] / d["prev_vol20"]
    d["entry_close_near_high"] = d["close"] / d["high"] - 1.0
    d["proxy_entry_break20"] = d["high"] >= d["prev_high20"]
    d["proxy_entry_break60"] = d["high"] >= d["prev_high60"]
    d["proxy_entry_strong_close"] = (d["entry_close_ret"] >= 0.05) & (d["entry_close_near_high"] >= -0.03)
    return d


def enrich(samples: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "code",
        "trade_date",
        "prev_close",
        "prev_high20",
        "prev_high60",
        "prev_ret5",
        "prev_ret10",
        "prev_ret20",
        "prev_close_to_high20",
        "prev_close_to_high60",
        "prev_runup_60low",
        "prev_above_ma20",
        "prev_above_ma60",
        "entry_gap_open",
        "entry_close_ret",
        "entry_high_ret",
        "entry_low_ret",
        "entry_vol_ratio20",
        "entry_close_near_high",
        "proxy_entry_break20",
        "proxy_entry_break60",
        "proxy_entry_strong_close",
    ]
    out = samples.merge(
        daily[cols],
        left_on=["code", "entry_date"],
        right_on=["code", "trade_date"],
        how="left",
    )
    out = out.drop(columns=["trade_date"], errors="ignore")
    out["d1_ready_high_base"] = (
        out["prev_above_ma20"].fillna(False)
        & out["prev_above_ma60"].fillna(False)
        & out["prev_close_to_high20"].ge(-0.08)
        & out["prev_ret20"].ge(0.0)
    )
    out["d1_overheated"] = out["prev_runup_60low"].ge(1.0) | out["prev_ret20"].ge(0.45)
    out["proxy_intraday_strength"] = (
        out["proxy_entry_break20"].fillna(False)
        & out["entry_vol_ratio20"].ge(1.5)
        & out["proxy_entry_strong_close"].fillna(False)
    )
    out["visible_bucket"] = "other"
    out.loc[out["d1_ready_high_base"] & ~out["d1_overheated"], "visible_bucket"] = "d1_ready_not_overheated"
    out.loc[out["d1_overheated"], "visible_bucket"] = "d1_overheated"
    out.loc[out["proxy_intraday_strength"], "visible_bucket"] = "proxy_intraday_strength"
    return out


def aggregate(enriched: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    for group, g in enriched.groupby("group"):
        for bucket, b in g.groupby("visible_bucket"):
            rows.append(
                {
                    "group": group,
                    "bucket": bucket,
                    "rows": int(len(b)),
                    "mean_ret": float(pd.to_numeric(b["sample_ret"], errors="coerce").mean()),
                    "win_rate": float((pd.to_numeric(b["sample_ret"], errors="coerce") > 0).mean()),
                    "sum_pnl_delta_or_na": float(pd.to_numeric(b["sample_pnl"], errors="coerce").fillna(0).sum()),
                    "mean_score": float(pd.to_numeric(b.get("score", pd.Series(dtype=float)), errors="coerce").mean()),
                    "mean_prev_ret20": float(pd.to_numeric(b["prev_ret20"], errors="coerce").mean()),
                    "mean_entry_vol_ratio20": float(pd.to_numeric(b["entry_vol_ratio20"], errors="coerce").mean()),
                }
            )
    bucket_summary = pd.DataFrame(rows)

    year_summary = (
        enriched.groupby(["group", "entry_year"])
        .agg(
            rows=("code", "count"),
            mean_ret=("sample_ret", "mean"),
            win_rate=("sample_ret", lambda s: float((pd.to_numeric(s, errors="coerce") > 0).mean())),
            sum_pnl_delta_or_na=("sample_pnl", lambda s: float(pd.to_numeric(s, errors="coerce").fillna(0).sum())),
            d1_ready_rate=("d1_ready_high_base", "mean"),
            proxy_strength_rate=("proxy_intraday_strength", "mean"),
            overheated_rate=("d1_overheated", "mean"),
        )
        .reset_index()
    )

    top_cols = [
        "group",
        "entry_date",
        "code",
        "name",
        "score",
        "strong_day_rank",
        "sample_ret",
        "sample_pnl",
        "ret_delta_nextopen",
        "pnl_delta_nextopen",
        "prev_ret20",
        "prev_close_to_high20",
        "prev_runup_60low",
        "entry_gap_open",
        "entry_close_ret",
        "entry_vol_ratio20",
        "d1_ready_high_base",
        "d1_overheated",
        "proxy_intraday_strength",
        "visible_bucket",
    ]
    top = pd.concat(
        [
            enriched[enriched["group"].eq("skipped_by_093")].sort_values("sample_ret", ascending=False).head(30),
            enriched[enriched["group"].eq("selected_strong_nextopen_damage")].sort_values("pnl_delta_nextopen").head(30),
        ],
        ignore_index=True,
    )
    top = top[[c for c in top_cols if c in top.columns]]
    return bucket_summary, year_summary, top


def write_report(bucket_summary: pd.DataFrame, year_summary: pd.DataFrame, top: pd.DataFrame) -> None:
    pct_cols = {
        "mean_ret",
        "win_rate",
        "mean_prev_ret20",
        "prev_ret20",
        "prev_close_to_high20",
        "prev_runup_60low",
        "entry_gap_open",
        "entry_close_ret",
        "d1_ready_rate",
        "proxy_strength_rate",
        "overheated_rate",
    }
    lines = [
        "# G3 V4 strong_main 可见确认诊断 v1",
        "",
        "## 目的",
        "",
        "- 固定 `strong_second_score_ge_093`，不继续调阈值。",
        "- 审计两类样本：被 0.93 过滤掉但原始 strong rank2 中收益较好的票，以及已入选但次日开盘成交相对收盘退出受损的票。",
        "- `d1_*` 字段是入场前一日可见；`proxy_entry_*` 字段使用入场日日线 OHLC，只能作为研究代理，不能直接作为实盘规则。",
        "",
        "## 分桶结果",
        "",
        md_table(bucket_summary, pct_cols=pct_cols),
        "",
        "## 年度结果",
        "",
        md_table(year_summary, pct_cols=pct_cols),
        "",
        "## 重点样本",
        "",
        md_table(top, pct_cols=pct_cols),
        "",
        "## 初步结论",
        "",
        "- 如果漏掉的大赢家集中在 `proxy_intraday_strength`，下一步要用真实 30m/5m 数据重建可见强度，而不是用日线代理上线。",
        "- 如果受损票集中在 `d1_overheated`，说明强势链路需要过热降仓或等待二次承接，而不是简单扩大买入。",
        "- 如果 D-1 可见分桶区分度弱，下一步必须转向板块主线/盘口承接，而不是继续用日线趋势特征调参。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base_candidates, keep093, close, nextopen = load_inputs()
    skipped, damage = build_study_sets(base_candidates, keep093, close, nextopen)
    samples = pd.concat([skipped, damage], ignore_index=True)
    daily = load_daily_context(samples)
    enriched = enrich(samples, daily)
    bucket_summary, year_summary, top = aggregate(enriched)
    skipped.to_csv(OUT_DIR / "skipped_by_093_samples.csv", index=False, encoding="utf-8-sig")
    damage.to_csv(OUT_DIR / "selected_nextopen_damage_samples.csv", index=False, encoding="utf-8-sig")
    enriched.to_csv(OUT_DIR / "visible_confirmation_enriched.csv", index=False, encoding="utf-8-sig")
    bucket_summary.to_csv(OUT_DIR / "bucket_summary.csv", index=False, encoding="utf-8-sig")
    year_summary.to_csv(OUT_DIR / "year_summary.csv", index=False, encoding="utf-8-sig")
    top.to_csv(OUT_DIR / "top_focus_samples.csv", index=False, encoding="utf-8-sig")
    write_report(bucket_summary, year_summary, top)
    print(f"wrote {OUT_DIR}")
    print(bucket_summary.to_string(index=False))


if __name__ == "__main__":
    main()
