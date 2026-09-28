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

from utils.market_warehouse import clickhouse_query_df  # noqa: E402


G2_SOURCE = _report_path() / "gen2_v2_complete_strategy_2020" / "sources" / "g2_v2_complete.parquet"
G3_STRONG = _report_path() / "gen3_v4_strong_entry_quality_audit_v1" / "entry_quality_enriched.csv"
OUT_DIR = _report_path() / "gen3_v4_strong_missing_g2_bigbull_v1"


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def pct(v: Any) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_sources() -> tuple[pd.DataFrame, pd.DataFrame]:
    g2 = pd.read_parquet(G2_SOURCE)
    g2["entry_date"] = pd.to_datetime(g2["entry_date"], errors="coerce").dt.normalize()
    g2["code"] = g2["code"].astype(str)
    for col in [
        "entry_price",
        "fwd_ret_5d",
        "fwd_ret_10d",
        "mfe_close_5d",
        "mae_close_5d",
        "rt_return_from_d1_close",
        "rt_breakout_vs_box_top",
        "l3_rt_strong3_ratio",
        "v4_rank",
        "v4_score",
        "runup_from_60d_low",
        "volume_ratio",
    ]:
        if col in g2.columns:
            g2[col] = pd.to_numeric(g2[col], errors="coerce")
    g2["entry_year"] = g2["entry_date"].dt.year

    g3 = pd.read_csv(G3_STRONG, low_memory=False, encoding="utf-8-sig")
    g3["entry_date"] = pd.to_datetime(g3["entry_date"], errors="coerce").dt.normalize()
    g3["code"] = g3["code"].astype(str)
    g3 = g3[g3["route"].astype(str).eq("strong_main")].copy()
    return g2.dropna(subset=["entry_date", "code", "entry_price"]).copy(), g3


def attach_g3_overlap(g2: pd.DataFrame, g3: pd.DataFrame) -> pd.DataFrame:
    keys = set(zip(g3["entry_date"], g3["code"]))
    out = g2.copy()
    out["in_g3_strong"] = [(day, code) in keys for day, code in zip(out["entry_date"], out["code"])]
    return out


def load_daily_for_signals(signals: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(signals["code"].dropna().astype(str).unique().tolist())
    start = pd.Timestamp(signals["entry_date"].min()).strftime("%Y-%m-%d")
    end = (pd.Timestamp(signals["entry_date"].max()) + pd.Timedelta(days=45)).strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 300):
        quoted = ",".join(sql_literal(c) for c in codes[i : i + 300])
        sql = f"""
        SELECT code, trade_date, close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN toDate({sql_literal(start)}) AND toDate({sql_literal(end)})
        ORDER BY code, trade_date
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    daily = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if daily.empty:
        return daily
    daily["code"] = daily["code"].astype(str)
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.normalize()
    daily["close"] = pd.to_numeric(daily["close"], errors="coerce")
    return daily.dropna(subset=["code", "trade_date", "close"]).sort_values(["code", "trade_date"]).reset_index(drop=True)


def attach_calculated_forward_returns(signals: pd.DataFrame) -> pd.DataFrame:
    daily = load_daily_for_signals(signals)
    out_rows: list[dict[str, Any]] = []
    by_code = {code: g.sort_values("trade_date").reset_index(drop=True) for code, g in daily.groupby("code")} if not daily.empty else {}
    for row in signals.itertuples(index=False):
        item = row._asdict()
        code = str(item["code"])
        entry_date = pd.Timestamp(item["entry_date"]).normalize()
        entry_price = float(item.get("entry_price") or 0.0)
        g = by_code.get(code)
        item["calc_ret_5d"] = pd.NA
        item["calc_ret_10d"] = pd.NA
        item["calc_mfe_5d"] = pd.NA
        item["calc_mae_5d"] = pd.NA
        if g is not None and not g.empty and entry_price > 0:
            idxs = g.index[g["trade_date"] >= entry_date].tolist()
            if idxs:
                start_idx = int(idxs[0])
                if start_idx + 4 < len(g):
                    item["calc_ret_5d"] = float(g.loc[start_idx + 4, "close"]) / entry_price - 1.0
                if start_idx + 9 < len(g):
                    item["calc_ret_10d"] = float(g.loc[start_idx + 9, "close"]) / entry_price - 1.0
                path5 = g.iloc[start_idx : min(start_idx + 5, len(g))]["close"]
                if not path5.empty:
                    item["calc_mfe_5d"] = float(path5.max()) / entry_price - 1.0
                    item["calc_mae_5d"] = float(path5.min()) / entry_price - 1.0
        out_rows.append(item)
    return pd.DataFrame(out_rows)


def summarize(df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, g in df.groupby(group_col, dropna=False):
        rows.append(
            {
                group_col: str(key),
                "信号数": int(len(g)),
                "5日均值": float(g["calc_ret_5d"].mean()),
                "5日胜率": float((g["calc_ret_5d"] > 0).mean()),
                "5日中位数": float(g["calc_ret_5d"].median()),
                "5日最差": float(g["calc_ret_5d"].min()),
                "10日均值": float(g["calc_ret_10d"].mean()),
                "10日胜率": float((g["calc_ret_10d"] > 0).mean()),
                "5日MFE": float(g["calc_mfe_5d"].mean()),
                "5日MAE": float(g["calc_mae_5d"].mean()),
                "盘中强度均值": float(g["rt_return_from_d1_close"].mean()),
                "板块强3均值": float(g["l3_rt_strong3_ratio"].mean()),
                "进入G3数": int(g["in_g3_strong"].sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("5日均值", ascending=False)


def summarize_year(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (family, year), g in df.groupby(["source_family", "entry_year"], dropna=False):
        rows.append(
            {
                "source_family": str(family),
                "year": int(year),
                "信号数": int(len(g)),
                "5日均值": float(g["calc_ret_5d"].mean()),
                "5日胜率": float((g["calc_ret_5d"] > 0).mean()),
                "10日均值": float(g["calc_ret_10d"].mean()),
                "进入G3数": int(g["in_g3_strong"].sum()),
            }
        )
    return pd.DataFrame(rows).sort_values(["year", "source_family"])


def top_table(df: pd.DataFrame, family: str, ascending: bool, n: int = 15) -> pd.DataFrame:
    cols = [
        "entry_date",
        "code",
        "name",
        "source_family",
        "calc_ret_5d",
        "calc_ret_10d",
        "calc_mfe_5d",
        "calc_mae_5d",
        "rt_return_from_d1_close",
        "rt_breakout_vs_box_top",
        "l3_rt_strong3_ratio",
        "v4_rank",
        "v4_score",
        "confirm_datetime",
    ]
    x = df[df["source_family"].astype(str).eq(family)].sort_values("calc_ret_5d", ascending=ascending).head(n).copy()
    x = x[[c for c in cols if c in x.columns]]
    x["entry_date"] = pd.to_datetime(x["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["calc_ret_5d", "calc_ret_10d", "calc_mfe_5d", "calc_mae_5d", "rt_return_from_d1_close", "rt_breakout_vs_box_top", "l3_rt_strong3_ratio"]:
        if col in x.columns:
            x[col] = x[col].map(pct)
    return x.rename(
        columns={
            "entry_date": "入场日",
            "code": "代码",
            "name": "名称",
            "source_family": "来源",
            "calc_ret_5d": "5日",
            "calc_ret_10d": "10日",
            "calc_mfe_5d": "5日MFE",
            "calc_mae_5d": "5日MAE",
            "rt_return_from_d1_close": "盘中强度",
            "rt_breakout_vs_box_top": "箱体突破",
            "l3_rt_strong3_ratio": "板块强3",
            "v4_rank": "V4排名",
            "v4_score": "V4分",
            "confirm_datetime": "确认时间",
        }
    )


def write_report(df: pd.DataFrame, by_family: pd.DataFrame, by_year: pd.DataFrame) -> None:
    pct_cols = {
        "5日均值",
        "5日胜率",
        "5日中位数",
        "5日最差",
        "10日均值",
        "10日胜率",
        "5日MFE",
        "5日MAE",
        "盘中强度均值",
        "板块强3均值",
    }
    big = df[df["source_family"].astype(str).eq("big_bull")]
    vol = df[df["source_family"].astype(str).eq("volume5")]
    lines = [
        "# G3 V4 strong_main 缺失 G2 big_bull 二次突破审计 v1",
        "",
        "## 边界",
        "",
        "- 本轮只审计 G2 source 中的结构信号，不把它们直接并入 G3。",
        "- 使用固定 G2 定义：`big_bull_rebreak_2_5d + intraday_strength + sector_strong`。",
        "- 因原 source 中 big_bull 缺少前向收益字段，本报告统一用 ClickHouse 日线按 `entry_price` 计算 5/10 个交易日后收益。",
        "- 这是入口质量诊断，不是完整 slot 组合复算。",
        "",
        "## 关键结论",
        "",
        f"- G2 source 中 volume5 信号 {len(vol)} 个，big_bull 信号 {len(big)} 个。",
        f"- big_bull 进入当前 G3 strong_main 的数量：{int(big['in_g3_strong'].sum())}。",
        f"- big_bull 5日均值 {pct(big['calc_ret_5d'].mean())}，5日胜率 {pct((big['calc_ret_5d'] > 0).mean())}；volume5 5日均值 {pct(vol['calc_ret_5d'].mean())}，胜率 {pct((vol['calc_ret_5d'] > 0).mean())}。",
        "",
        "## 按来源对比",
        "",
        md_table(by_family, pct_cols=pct_cols),
        "",
        "## 年度对比",
        "",
        md_table(by_year, pct_cols={"5日均值", "5日胜率", "10日均值"}),
        "",
        "## big_bull 最好样本",
        "",
        md_table(top_table(df, "big_bull", ascending=False)),
        "",
        "## big_bull 最差样本",
        "",
        md_table(top_table(df, "big_bull", ascending=True)),
        "",
        "## 下一步目标",
        "",
        "- 如果 big_bull 前向收益稳定高于 volume5，下一步应建立 G3 独立 `strong_bigbull_rebreak` 候选源，而不是继续围绕 volume5 出口补救。",
        "- 如果 big_bull 只在 2025/2026 有效，则必须作为强势市场专用入口，不能跨市场风格硬套。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    g2, g3 = load_sources()
    d = attach_g3_overlap(g2, g3)
    d = attach_calculated_forward_returns(d)
    by_family = summarize(d, "source_family")
    by_year = summarize_year(d)
    d.to_csv(OUT_DIR / "g2_source_with_g3_overlap.csv", index=False, encoding="utf-8-sig")
    by_family.to_csv(OUT_DIR / "summary_by_source_family.csv", index=False, encoding="utf-8-sig")
    by_year.to_csv(OUT_DIR / "summary_by_year.csv", index=False, encoding="utf-8-sig")
    write_report(d, by_family, by_year)
    print(f"done: {OUT_DIR}")
    print(by_family.to_string(index=False))


if __name__ == "__main__":
    main()
