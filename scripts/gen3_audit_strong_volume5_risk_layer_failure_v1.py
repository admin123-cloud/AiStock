from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _md_table
from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_risk_layer_failure_v1"
SOURCE = (
    ROOT
    / "reports"
    / "gen3_strong_volume5_risk_layer_execution_stress_v1"
    / "half_d2_le0_then_d3_next_open_second_leg_30bps_closed_trades.csv"
)


NUMERIC_FEATURES = [
    "net_ret",
    "v4_rank",
    "v4_score",
    "mom5",
    "mom10",
    "mom20",
    "vol_ratio",
    "rt_return_from_d1_close",
    "rt_confirm_vs_ma5",
    "rt_fractal_rebound",
    "rt_30m_amount_ratio",
    "rt_confirm_hour",
    "float_market_cap_yi",
    "cap_pressure_days",
    "cap_big_pressure_days",
    "cap_pressure_amount_share",
    "overhead_pressure_days",
    "overhead_big_pressure_days",
    "overhead_pressure_amount_share",
    "prior_max_high_ratio",
    "runup_from_60d_low",
    "recent60_return",
    "market_breadth",
    "index_mom20",
    "l3_s3",
    "l2_s3",
    "l3_rise",
    "l3_rt_avg_return",
    "l3_rt_rise_ratio",
    "l3_rt_strong3_ratio",
    "l2_rt_rise_ratio",
    "score_volume5",
    "score_main3",
    "score_repair2",
    "fwd_ret_1d",
    "fwd_ret_2d",
    "fwd_ret_3d",
    "fwd_ret_5d",
    "mae_close_5d",
]


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _load_daily_paths(trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for r in trades.itertuples(index=False):
        start = (pd.Timestamp(r.entry_date) - pd.Timedelta(days=8)).strftime("%Y-%m-%d")
        end = (pd.Timestamp(r.policy_exit_date) + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
        sql = f"""
        SELECT code, trade_date, open, high, low, close, volume, amount
        FROM kline_daily
        WHERE code = {_sql_literal(str(r.code))}
          AND trade_date BETWEEN toDate({_sql_literal(start)}) AND toDate({_sql_literal(end)})
        ORDER BY trade_date
        """
        d = clickhouse_query_df(sql)
        if d.empty:
            continue
        d["audit_code"] = str(r.code)
        d["audit_name"] = str(r.name)
        d["audit_entry_date"] = pd.Timestamp(r.entry_date).strftime("%Y-%m-%d")
        d["audit_exit_date"] = pd.Timestamp(r.policy_exit_date).strftime("%Y-%m-%d")
        d["audit_net_ret"] = float(r.net_ret)
        rows.append(d)
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if out.empty:
        return out
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["prev_close"] = out.groupby("audit_code")["close"].shift(1)
    out["daily_ret"] = out["close"] / out["prev_close"] - 1.0
    return out


def _risk_tags(row: pd.Series) -> str:
    tags: list[str] = []
    if str(row.get("exit_reason_proxy")) == "fixed_h5":
        tags.append("未触发半仓")
    else:
        tags.append("半仓后仍失败")
    if str(row.get("g3_market_style")) == "weak_recovery":
        tags.append("弱修复风格")
    if pd.to_numeric(row.get("sector_strong"), errors="coerce") == 0:
        tags.append("板块未加分")
    if pd.to_numeric(row.get("runup_from_60d_low"), errors="coerce") > 0.8:
        tags.append("60日涨幅偏高")
    if pd.to_numeric(row.get("overhead_pressure_amount_share"), errors="coerce") > 0.5:
        tags.append("上方压力偏重")
    if pd.to_numeric(row.get("cap_pressure_amount_share"), errors="coerce") > 0.5:
        tags.append("筹码压力偏重")
    if pd.to_numeric(row.get("l3_rt_rise_ratio"), errors="coerce") < 0.35:
        tags.append("三级行业偏弱")
    if pd.to_numeric(row.get("rt_30m_amount_ratio"), errors="coerce") > 4.0:
        tags.append("30m放量过热")
    if not tags:
        tags.append("未命中粗标签")
    return " / ".join(tags)


def _feature_compare(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["is_bad10"] = d["net_ret"].le(-0.10)
    rows = []
    for col in NUMERIC_FEATURES:
        if col not in d.columns:
            continue
        s = pd.to_numeric(d[col], errors="coerce")
        bad = s[d["is_bad10"]]
        rest = s[~d["is_bad10"]]
        if bad.dropna().empty or rest.dropna().empty:
            continue
        rows.append(
            {
                "feature": col,
                "bad10_median": float(bad.median()),
                "rest_median": float(rest.median()),
                "delta": float(bad.median() - rest.median()),
                "bad10_mean": float(bad.mean()),
                "rest_mean": float(rest.mean()),
            }
        )
    return pd.DataFrame(rows).sort_values("delta")


def _bucket_summary(df: pd.DataFrame, col: str) -> pd.DataFrame:
    if col not in df.columns:
        return pd.DataFrame()
    g = (
        df.groupby(col, dropna=False)
        .agg(
            trades=("code", "count"),
            mean_ret=("net_ret", "mean"),
            win_rate=("net_ret", lambda s: float((s > 0).mean())),
            bad10_rate=("net_ret", lambda s: float((s <= -0.10).mean())),
            worst=("net_ret", "min"),
        )
        .reset_index()
    )
    return g.sort_values(["mean_ret", "trades"], ascending=[True, False])


def _monthly_2024(df: pd.DataFrame) -> pd.DataFrame:
    d = df[pd.to_datetime(df["entry_date"], errors="coerce").dt.year.eq(2024)].copy()
    if d.empty:
        return d
    d["month"] = pd.to_datetime(d["entry_date"]).dt.strftime("%Y-%m")
    return (
        d.groupby("month")
        .agg(
            trades=("code", "count"),
            mean_ret=("net_ret", "mean"),
            total_ret_proxy=("net_ret", "sum"),
            win_rate=("net_ret", lambda s: float((s > 0).mean())),
            bad10_rate=("net_ret", lambda s: float((s <= -0.10).mean())),
            worst=("net_ret", "min"),
        )
        .reset_index()
        .sort_values("total_ret_proxy")
    )


def _write_report(
    worst: pd.DataFrame,
    year2024: pd.DataFrame,
    monthly: pd.DataFrame,
    by_reason: pd.DataFrame,
    by_style: pd.DataFrame,
    by_sector_strong: pd.DataFrame,
    feature_compare: pd.DataFrame,
) -> None:
    pct_cols = {
        "net_ret",
        "fwd_ret_1d",
        "fwd_ret_2d",
        "fwd_ret_3d",
        "fwd_ret_5d",
        "mae_close_5d",
        "mean_ret",
        "win_rate",
        "bad10_rate",
        "worst",
        "total_ret_proxy",
        "bad10_median",
        "rest_median",
        "delta",
        "bad10_mean",
        "rest_mean",
    }
    worst_view_cols = [
        "code",
        "name",
        "entry_date",
        "policy_exit_date",
        "net_ret",
        "exit_reason_proxy",
        "g3_market_style",
        "l3_sector_name",
        "sector_strong",
        "v4_rank",
        "runup_from_60d_low",
        "overhead_pressure_amount_share",
        "rt_30m_amount_ratio",
        "risk_tags",
    ]
    worst_view = worst[[c for c in worst_view_cols if c in worst.columns]].head(20)
    y2024_view_cols = [
        "code",
        "name",
        "entry_date",
        "policy_exit_date",
        "net_ret",
        "exit_reason_proxy",
        "g3_market_style",
        "l3_sector_name",
        "sector_strong",
        "v4_rank",
        "risk_tags",
    ]
    y2024_bad = year2024.sort_values("net_ret")[[c for c in y2024_view_cols if c in year2024.columns]].head(15)
    lines = [
        "# G3 强势链路风险分层失败归因 V1",
        "",
        "## 口径",
        "",
        "- 样本：`half_d2_le0_then_d3 + next_open_second_leg + 30bps` 的已成交账本。",
        "- 目标：解释剩余尾部亏损和 2024 小亏，不做参数优化。",
        "- 注意：这里的风险标签是粗归因标签，只用于决定下一步研究方向，不作为硬过滤规则。",
        "",
        "## 最坏交易",
        "",
        _md_table(worst_view, pct_cols=pct_cols),
        "",
        "## 2024 亏损样本",
        "",
        _md_table(y2024_bad, pct_cols=pct_cols),
        "",
        "## 2024 月度分布",
        "",
        _md_table(monthly, pct_cols=pct_cols),
        "",
        "## 按退出原因",
        "",
        _md_table(by_reason, pct_cols=pct_cols),
        "",
        "## 按市场风格",
        "",
        _md_table(by_style, pct_cols=pct_cols),
        "",
        "## 按板块加分",
        "",
        _md_table(by_sector_strong, pct_cols=pct_cols),
        "",
        "## Bad10 特征差异",
        "",
        _md_table(feature_compare.head(20), pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 最大尾部主要来自未触发半仓的 `fixed_h5` 交易，说明 D2 半仓能降低组合回撤，但覆盖不了快速单票崩塌。",
        "- 2024 的问题更像结构性弱年里的强势票延续不足，而不是单一半仓退出失效。",
        "- 下一步不宜用这些标签直接硬过滤；应先做少数、粗粒度的候选护栏回放，例如弱修复降权、板块不强降权、30m 放量过热降权，并要求 train/valid/blind 同时通过。",
        "",
    ]
    (OUT_DIR / "failure_attribution_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(SOURCE)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    df["policy_exit_date"] = pd.to_datetime(df["policy_exit_date"], errors="coerce").dt.normalize()
    for col in NUMERIC_FEATURES:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["risk_tags"] = df.apply(_risk_tags, axis=1)

    worst = df.sort_values("net_ret").head(30).copy()
    year2024 = df[df["entry_date"].dt.year.eq(2024)].copy()
    monthly = _monthly_2024(df)
    by_reason = _bucket_summary(df, "exit_reason_proxy")
    by_style = _bucket_summary(df, "g3_market_style")
    by_sector_strong = _bucket_summary(df, "sector_strong")
    feature_compare = _feature_compare(df)
    paths = _load_daily_paths(worst.head(10))

    worst.to_csv(OUT_DIR / "worst_trades_top30.csv", index=False, encoding="utf-8-sig")
    year2024.to_csv(OUT_DIR / "year2024_trades.csv", index=False, encoding="utf-8-sig")
    monthly.to_csv(OUT_DIR / "year2024_monthly.csv", index=False, encoding="utf-8-sig")
    by_reason.to_csv(OUT_DIR / "by_exit_reason.csv", index=False, encoding="utf-8-sig")
    by_style.to_csv(OUT_DIR / "by_market_style.csv", index=False, encoding="utf-8-sig")
    by_sector_strong.to_csv(OUT_DIR / "by_sector_strong.csv", index=False, encoding="utf-8-sig")
    feature_compare.to_csv(OUT_DIR / "bad10_feature_compare.csv", index=False, encoding="utf-8-sig")
    paths.to_csv(OUT_DIR / "worst_trades_daily_paths.csv", index=False, encoding="utf-8-sig")
    _write_report(worst, year2024, monthly, by_reason, by_style, by_sector_strong, feature_compare)

    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "rows": int(len(df)),
                "bad10": int((df["net_ret"] <= -0.10).sum()),
                "worst": worst[["code", "name", "entry_date", "net_ret", "exit_reason_proxy", "risk_tags"]]
                .head(10)
                .assign(entry_date=lambda x: x["entry_date"].dt.strftime("%Y-%m-%d"))
                .to_dict(orient="records"),
                "year2024": {
                    "trades": int(len(year2024)),
                    "mean_ret": float(year2024["net_ret"].mean()) if len(year2024) else 0.0,
                    "bad10": int((year2024["net_ret"] <= -0.10).sum()) if len(year2024) else 0,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
