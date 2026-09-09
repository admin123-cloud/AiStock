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

from utils.paths import report_path

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _md_table as md_table  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SRC = report_path("gen3_v4_strong_failure_attribution_v1", "strong_main_path_attribution.csv")
OUT_DIR = report_path("gen3_v4_strong_entry_quality_audit_v1")


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def load_trades() -> pd.DataFrame:
    d = pd.read_csv(SRC, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in [
        "score",
        "strong_day_rank",
        "policy_net_ret",
        "realized_pnl",
        "entry_close_ret",
        "d1_close_ret",
        "d2_close_ret",
        "min_low_ret",
        "max_high_ret",
        "giveback_from_high",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["early_fail"] = (
        d["failure_tag"].astype(str).str.contains("个股早期失败", regex=False)
        | d["d1_close_ret"].le(-0.06)
        | d["d2_close_ret"].le(-0.08)
    )
    return d.dropna(subset=["entry_date", "code", "policy_net_ret"])


def load_daily(trades: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(trades["code"].dropna().astype(str).unique().tolist())
    start = (trades["entry_date"].min() - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
    end = (trades["policy_exit_date"].max() + pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 300):
        quoted = ",".join(sql_literal(c) for c in codes[i : i + 300])
        sql = f"""
        SELECT code, trade_date, open, high, low, close, volume, amount, turnover_rate
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN toDate({sql_literal(start)}) AND toDate({sql_literal(end)})
        ORDER BY code, trade_date
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    d = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if d.empty:
        return d
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["code", "trade_date", "open", "high", "low", "close"]).sort_values(["code", "trade_date"])
    g = d.groupby("code", sort=False)
    d["prev_close"] = g["close"].shift(1)
    d["ret1"] = g["close"].pct_change()
    d["ma5"] = g["close"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    d["ma10"] = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    d["ma20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    d["amount20"] = g["amount"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    d["high20_prev"] = g["high"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).max())
    d["low60_prev"] = g["low"].transform(lambda s: s.shift(1).rolling(60, min_periods=20).min())
    d["prior5_ret"] = g["close"].transform(lambda s: s.shift(1) / s.shift(6) - 1.0)
    d["prior20_ret"] = g["close"].transform(lambda s: s.shift(1) / s.shift(21) - 1.0)
    return d.reset_index(drop=True)


def enrich_entry(trades: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    entry = daily.copy()
    entry = entry.rename(columns={"trade_date": "entry_date"})
    keep = [
        "code",
        "entry_date",
        "open",
        "high",
        "low",
        "close",
        "prev_close",
        "ma5",
        "ma10",
        "ma20",
        "amount",
        "amount20",
        "high20_prev",
        "low60_prev",
        "prior5_ret",
        "prior20_ret",
        "turnover_rate",
    ]
    d = trades.merge(entry[keep], on=["code", "entry_date"], how="left", suffixes=("", "_entry"))
    d["entry_gap_ret"] = d["open"] / d["prev_close"] - 1.0
    d["entry_intraday_ret"] = d["close"] / d["open"] - 1.0
    d["entry_low_from_open"] = d["low"] / d["open"] - 1.0
    d["entry_upper_shadow"] = (d["high"] - d["close"]) / (d["high"] - d["low"]).replace(0, pd.NA)
    d["entry_range"] = d["high"] / d["low"] - 1.0
    d["entry_amount_ratio20"] = d["amount"] / d["amount20"]
    d["entry_vs_ma5"] = d["close"] / d["ma5"] - 1.0
    d["entry_vs_ma20"] = d["close"] / d["ma20"] - 1.0
    d["entry_break_high20"] = d["close"] / d["high20_prev"] - 1.0
    d["runup_from_60d_low"] = d["close"] / d["low60_prev"] - 1.0
    return d


def bucketize(d: pd.DataFrame) -> pd.DataFrame:
    out = d.copy()
    out["rank_bucket"] = pd.cut(pd.to_numeric(out["strong_day_rank"], errors="coerce"), bins=[0, 1, 2, 99], labels=["rank1", "rank2", "rank3plus"], include_lowest=True)
    out["score_bucket"] = pd.cut(pd.to_numeric(out["score"], errors="coerce"), bins=[0, 0.93, 1.0, 1.08, 99], labels=["score_lt093", "score_093_100", "score_100_108", "score_ge108"], include_lowest=True)
    out["gap_bucket"] = pd.cut(pd.to_numeric(out["entry_gap_ret"], errors="coerce"), bins=[-9, -0.03, 0.0, 0.03, 9], labels=["gap_down3", "gap_flat_down", "gap_up0_3", "gap_up3plus"])
    out["amount_bucket"] = pd.cut(pd.to_numeric(out["entry_amount_ratio20"], errors="coerce"), bins=[-9, 1.5, 3.0, 5.0, 99], labels=["amt_le1_5", "amt_1_5_3", "amt_3_5", "amt_gt5"])
    out["runup_bucket"] = pd.cut(pd.to_numeric(out["runup_from_60d_low"], errors="coerce"), bins=[-9, 0.3, 0.6, 1.0, 99], labels=["runup_le30", "runup_30_60", "runup_60_100", "runup_gt100"])
    out["upper_shadow_bucket"] = pd.cut(pd.to_numeric(out["entry_upper_shadow"], errors="coerce"), bins=[-1, 0.25, 0.5, 0.75, 99], labels=["shadow_le25", "shadow_25_50", "shadow_50_75", "shadow_gt75"])
    out["break20_bucket"] = pd.cut(pd.to_numeric(out["entry_break_high20"], errors="coerce"), bins=[-9, -0.02, 0.0, 0.05, 99], labels=["below_high20_2", "near_high20", "break0_5", "break_gt5"])
    out["d1_bucket"] = pd.cut(pd.to_numeric(out["d1_close_ret"], errors="coerce"), bins=[-9, -0.06, -0.03, 0.03, 99], labels=["d1_fail6", "d1_weak3_6", "d1_flat", "d1_strong"])
    out["d2_bucket"] = pd.cut(pd.to_numeric(out["d2_close_ret"], errors="coerce"), bins=[-9, -0.08, -0.03, 0.03, 99], labels=["d2_fail8", "d2_weak3_8", "d2_flat", "d2_strong"])
    return out


def bucket_summary(d: pd.DataFrame, col: str) -> pd.DataFrame:
    g = (
        d.groupby(col, dropna=False)
        .agg(
            trades=("code", "count"),
            early_fail_rate=("early_fail", "mean"),
            mean_ret=("policy_net_ret", "mean"),
            win_rate=("policy_net_ret", lambda s: float((s > 0).mean())),
            bad8_rate=("policy_net_ret", lambda s: float((s <= -0.08).mean())),
            pnl=("realized_pnl", "sum"),
        )
        .reset_index()
        .rename(columns={col: "bucket"})
    )
    g.insert(0, "feature", col)
    return g.sort_values(["early_fail_rate", "trades"], ascending=[False, False])


def feature_compare(d: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "score",
        "strong_day_rank",
        "entry_gap_ret",
        "entry_intraday_ret",
        "entry_low_from_open",
        "entry_upper_shadow",
        "entry_range",
        "entry_amount_ratio20",
        "entry_vs_ma5",
        "entry_vs_ma20",
        "entry_break_high20",
        "runup_from_60d_low",
        "prior5_ret",
        "prior20_ret",
        "d1_close_ret",
        "d2_close_ret",
        "min_low_ret",
        "giveback_from_high",
    ]
    rows = []
    for col in cols:
        if col not in d.columns:
            continue
        s = pd.to_numeric(d[col], errors="coerce")
        bad = s[d["early_fail"]]
        rest = s[~d["early_fail"]]
        if bad.dropna().empty or rest.dropna().empty:
            continue
        rows.append(
            {
                "feature": col,
                "early_median": float(bad.median()),
                "rest_median": float(rest.median()),
                "delta": float(bad.median() - rest.median()),
                "early_mean": float(bad.mean()),
                "rest_mean": float(rest.mean()),
            }
        )
    return pd.DataFrame(rows)


def write_report(enriched: pd.DataFrame, compares: pd.DataFrame, buckets: pd.DataFrame, early: pd.DataFrame) -> None:
    pct_cols = {
        "early_fail_rate",
        "mean_ret",
        "win_rate",
        "bad8_rate",
        "early_median",
        "rest_median",
        "delta",
        "early_mean",
        "rest_mean",
        "policy_net_ret",
        "d1_close_ret",
        "d2_close_ret",
        "entry_gap_ret",
        "entry_intraday_ret",
        "entry_amount_ratio20",
        "entry_upper_shadow",
        "runup_from_60d_low",
        "entry_break_high20",
    }
    summary = pd.DataFrame(
        [
            {
                "scope": "all_strong_main",
                "trades": len(enriched),
                "early_fail_rate": float(enriched["early_fail"].mean()),
                "mean_ret": float(enriched["policy_net_ret"].mean()),
                "bad8_rate": float((enriched["policy_net_ret"] <= -0.08).mean()),
                "pnl": float(enriched["realized_pnl"].sum()),
            },
            {
                "scope": "early_fail",
                "trades": int(enriched["early_fail"].sum()),
                "early_fail_rate": 1.0,
                "mean_ret": float(enriched.loc[enriched["early_fail"], "policy_net_ret"].mean()),
                "bad8_rate": float((enriched.loc[enriched["early_fail"], "policy_net_ret"] <= -0.08).mean()),
                "pnl": float(enriched.loc[enriched["early_fail"], "realized_pnl"].sum()),
            },
        ]
    )
    early_cols = [
        "entry_date",
        "code",
        "name",
        "policy_net_ret",
        "score",
        "strong_day_rank",
        "entry_gap_ret",
        "entry_intraday_ret",
        "entry_upper_shadow",
        "entry_amount_ratio20",
        "runup_from_60d_low",
        "entry_break_high20",
        "d1_close_ret",
        "d2_close_ret",
        "failure_tag",
    ]
    lines = [
        "# G3 V4 strong_main 入场质量审计 v1",
        "",
        "## 边界",
        "",
        "- 样本固定为 `strong_second_score_ge_093` 的 `strong_main`。",
        "- 本轮只审计入场质量和 D1/D2 早期弱确认，不调参、不生成正式规则。",
        "- 入场前指标只使用入场当日及之前日线；D1/D2 指标用于失败归因，不能直接当入场条件。",
        "",
        "## 总览",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 早期失败 vs 其他样本特征差异",
        "",
        md_table(compares.sort_values("delta").head(25), pct_cols=pct_cols),
        "",
        "## 分桶审计",
        "",
        md_table(buckets, pct_cols=pct_cols),
        "",
        "## 早期失败样本",
        "",
        md_table(early.sort_values("policy_net_ret").head(30)[early_cols], pct_cols=pct_cols),
        "",
        "## 初步判断",
        "",
        "- 如果 D1/D2 桶显著解释失败，下一步应研究“入场后早期确认/降仓”，不是继续做固定止盈。",
        "- 如果入场日上影、量能过热、runup 过高等入场前指标解释力强，下一步才考虑入场质量过滤。",
        "- 如果入场前指标区分度弱，则需要补板块退潮信号和分时强度，而不是硬做日线过滤。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_trades()
    daily = load_daily(trades)
    enriched = bucketize(enrich_entry(trades, daily))
    compares = feature_compare(enriched)
    bucket_cols = [
        "rank_bucket",
        "score_bucket",
        "gap_bucket",
        "amount_bucket",
        "runup_bucket",
        "upper_shadow_bucket",
        "break20_bucket",
        "d1_bucket",
        "d2_bucket",
    ]
    buckets = pd.concat([bucket_summary(enriched, c) for c in bucket_cols], ignore_index=True)
    early = enriched[enriched["early_fail"]].copy()
    enriched.to_csv(OUT_DIR / "entry_quality_enriched.csv", index=False, encoding="utf-8-sig")
    compares.to_csv(OUT_DIR / "feature_compare.csv", index=False, encoding="utf-8-sig")
    buckets.to_csv(OUT_DIR / "bucket_summary.csv", index=False, encoding="utf-8-sig")
    early.to_csv(OUT_DIR / "early_fail_samples.csv", index=False, encoding="utf-8-sig")
    write_report(enriched, compares, buckets, early)
    print(f"wrote {OUT_DIR}")
    print(compares.sort_values("delta").to_string(index=False))
    print(buckets.sort_values(["early_fail_rate", "trades"], ascending=[False, False]).head(20).to_string(index=False))


if __name__ == "__main__":
    main()
