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

import numpy as np
import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SOURCE = _report_path() / "gen3_v4_strong_missing_g2_bigbull_v1" / "g2_source_with_g3_overlap.csv"
INDEX_FEATURES = _report_path() / "gen2_timing_research" / "index_features.csv"
OUT_DIR = _report_path() / "gen3_v4_bigbull_structural_context_v1"
INDEX_CODE = "999999.SH"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        item: dict[str, Any] = {}
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


def load_source() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False, encoding="utf-8-sig")
    d = d[d["source_family"].astype(str).eq("big_bull")].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    for col in [
        "calc_ret_5d",
        "calc_ret_10d",
        "calc_mfe_5d",
        "calc_mae_5d",
        "rt_return_from_d1_close",
        "rt_breakout_vs_box_top",
        "l3_rt_strong3_ratio",
        "l2_rt_strong3_ratio",
        "l1_rt_strong3_ratio",
        "v4_score",
        "v4_rank",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["entry_date", "code", "calc_ret_5d"]).copy()


def load_index_context() -> pd.DataFrame:
    idx = pd.read_csv(INDEX_FEATURES, low_memory=False, encoding="utf-8-sig")
    idx = idx[idx["code"].astype(str).eq(INDEX_CODE)].copy()
    idx["entry_date"] = pd.to_datetime(idx["trade_date"], errors="coerce").dt.normalize()
    for col in ["close", "ma20", "ma60", "ma120", "ma20_slope5", "mom20", "breadth_ma20"]:
        idx[col] = pd.to_numeric(idx[col], errors="coerce")
    idx["index_ge_ma20"] = idx["close"] >= idx["ma20"]
    idx["index_ge_ma60"] = idx["close"] >= idx["ma60"]
    idx["ma20_ge_ma60"] = idx["ma20"] >= idx["ma60"]
    idx["ma60_ge_ma120"] = idx["ma60"] >= idx["ma120"]
    idx["proxy_style"] = np.select(
        [
            idx["index_ge_ma20"] & idx["ma20_ge_ma60"] & idx["ma60_ge_ma120"] & idx["mom20"].gt(0),
            idx["index_ge_ma20"] & idx["ma20_ge_ma60"] & idx["mom20"].gt(0),
            idx["index_ge_ma20"] & idx["breadth_ma20"].ge(0.50),
            idx["close"].lt(idx["ma20"]) & idx["ma20"].lt(idx["ma60"]),
        ],
        ["proxy_main_up", "proxy_up_repair", "proxy_range_repair", "proxy_down_or_failed"],
        default="proxy_mixed_range",
    )
    return idx[
        [
            "entry_date",
            "close",
            "ma20",
            "ma60",
            "ma120",
            "mom20",
            "ma20_slope5",
            "breadth_ma20",
            "index_ge_ma20",
            "index_ge_ma60",
            "ma20_ge_ma60",
            "ma60_ge_ma120",
            "proxy_style",
        ]
    ].rename(
        columns={
            "close": "idx_close",
            "ma20": "idx_ma20",
            "ma60": "idx_ma60",
            "ma120": "idx_ma120",
            "mom20": "idx_mom20",
            "ma20_slope5": "idx_ma20_slope5",
        }
    )


def add_context(d: pd.DataFrame) -> pd.DataFrame:
    out = d.merge(load_index_context(), on="entry_date", how="left")
    out["window"] = np.select(
        [
            out["entry_date"].between(pd.Timestamp("2020-01-01"), pd.Timestamp("2023-12-31")),
            out["entry_date"].between(pd.Timestamp("2024-01-01"), pd.Timestamp("2025-12-31")),
            out["entry_date"].ge(pd.Timestamp("2026-01-01")),
        ],
        ["train_2020_2023", "valid_2024_2025", "blind_2026"],
        default="outside",
    )
    out["breadth_bucket"] = pd.cut(
        out["breadth_ma20"],
        bins=[-np.inf, 0.30, 0.50, 0.60, np.inf],
        labels=["breadth_lt30", "breadth_30_50", "breadth_50_60", "breadth_ge60"],
    ).astype(str)
    out["sector_s3_bucket"] = pd.cut(
        out["l3_rt_strong3_ratio"],
        bins=[-np.inf, 0.10, 0.20, 0.40, np.inf],
        labels=["sector_lt10", "sector_10_20", "sector_20_40", "sector_ge40"],
    ).astype(str)
    out["intraday_strength_bucket"] = pd.cut(
        out["rt_return_from_d1_close"],
        bins=[-np.inf, 0.06, 0.08, 0.10, np.inf],
        labels=["intraday_lt6", "intraday_6_8", "intraday_8_10", "intraday_ge10"],
    ).astype(str)
    out["box_breakout_bucket"] = pd.cut(
        out["rt_breakout_vs_box_top"],
        bins=[-np.inf, 0.025, 0.05, 0.08, np.inf],
        labels=["breakout_lt2p5", "breakout_2p5_5", "breakout_5_8", "breakout_ge8"],
    ).astype(str)
    return out


def summarize_group(d: pd.DataFrame, group_col: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, g in d.groupby(group_col, dropna=False):
        rows.append(
            {
                group_col: str(key),
                "count": int(len(g)),
                "ret5_mean": float(g["calc_ret_5d"].mean()),
                "ret5_win": float((g["calc_ret_5d"] > 0).mean()),
                "ret5_median": float(g["calc_ret_5d"].median()),
                "ret10_mean": float(g["calc_ret_10d"].mean()),
                "mfe5_mean": float(g["calc_mfe_5d"].mean()),
                "mae5_mean": float(g["calc_mae_5d"].mean()),
                "intraday_mean": float(g["rt_return_from_d1_close"].mean()),
                "sector_s3_mean": float(g["l3_rt_strong3_ratio"].mean()),
            }
        )
    return pd.DataFrame(rows).sort_values(["ret5_mean", "count"], ascending=[False, False])


def gate_masks(d: pd.DataFrame) -> dict[str, pd.Series]:
    index_ok = d["index_ge_ma20"].fillna(False).astype(bool)
    trend_ok = index_ok & d["ma20_ge_ma60"].fillna(False).astype(bool)
    breadth50 = pd.to_numeric(d["breadth_ma20"], errors="coerce").ge(0.50)
    breadth60 = pd.to_numeric(d["breadth_ma20"], errors="coerce").ge(0.60)
    sector10 = pd.to_numeric(d["l3_rt_strong3_ratio"], errors="coerce").ge(0.10)
    sector20 = pd.to_numeric(d["l3_rt_strong3_ratio"], errors="coerce").ge(0.20)
    intraday6 = pd.to_numeric(d["rt_return_from_d1_close"], errors="coerce").ge(0.06)
    breakout25 = pd.to_numeric(d["rt_breakout_vs_box_top"], errors="coerce").ge(0.025)
    breakout80 = pd.to_numeric(d["rt_breakout_vs_box_top"], errors="coerce").le(0.08)
    return {
        "all_bigbull": pd.Series(True, index=d.index),
        "index_ge_ma20": index_ok,
        "index_ge_ma20_breadth50": index_ok & breadth50,
        "trend_ma20_ge_ma60_breadth50": trend_ok & breadth50,
        "index_breadth50_sector10": index_ok & breadth50 & sector10,
        "index_breadth50_sector20": index_ok & breadth50 & sector20,
        "index_breadth60_sector20": index_ok & breadth60 & sector20,
        "index_breadth50_sector10_intraday6": index_ok & breadth50 & sector10 & intraday6,
        "index_breadth50_sector10_intraday6_breakout25_80": index_ok & breadth50 & sector10 & intraday6 & breakout25 & breakout80,
    }


def summarize_gates(d: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for gate, mask in gate_masks(d).items():
        g = d[mask].copy()
        if g.empty:
            rows.append({"gate": gate, "count": 0})
            continue
        rows.append(
            {
                "gate": gate,
                "count": int(len(g)),
                "coverage": float(len(g) / len(d)),
                "ret5_mean": float(g["calc_ret_5d"].mean()),
                "ret5_win": float((g["calc_ret_5d"] > 0).mean()),
                "ret5_median": float(g["calc_ret_5d"].median()),
                "ret10_mean": float(g["calc_ret_10d"].mean()),
                "mfe5_mean": float(g["calc_mfe_5d"].mean()),
                "mae5_mean": float(g["calc_mae_5d"].mean()),
                "train_ret5": float(g.loc[g["window"].eq("train_2020_2023"), "calc_ret_5d"].mean()),
                "valid_ret5": float(g.loc[g["window"].eq("valid_2024_2025"), "calc_ret_5d"].mean()),
                "blind_ret5": float(g.loc[g["window"].eq("blind_2026"), "calc_ret_5d"].mean()),
            }
        )
    return pd.DataFrame(rows).sort_values(["ret5_mean", "count"], ascending=[False, False])


def worst_examples(d: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    cols = [
        "entry_date",
        "code",
        "name",
        "calc_ret_5d",
        "calc_ret_10d",
        "calc_mfe_5d",
        "calc_mae_5d",
        "proxy_style",
        "breadth_ma20",
        "l3_rt_strong3_ratio",
        "rt_return_from_d1_close",
        "rt_breakout_vs_box_top",
        "confirm_datetime",
    ]
    x = d.sort_values("calc_ret_5d", ascending=True).head(n).copy()
    x = x[[c for c in cols if c in x.columns]]
    x["entry_date"] = pd.to_datetime(x["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return x


def write_report(d: pd.DataFrame, gate_summary: pd.DataFrame, group_tables: dict[str, pd.DataFrame]) -> None:
    pct_cols = {
        "coverage",
        "ret5_mean",
        "ret5_win",
        "ret5_median",
        "ret10_mean",
        "mfe5_mean",
        "mae5_mean",
        "intraday_mean",
        "sector_s3_mean",
        "train_ret5",
        "valid_ret5",
        "blind_ret5",
    }
    best_gate = gate_summary[gate_summary["count"].fillna(0).gt(20)].head(1)
    best_line = ""
    if not best_gate.empty:
        r = best_gate.iloc[0]
        best_line = f"- 固定结构门槛里暂时最好的是 `{r['gate']}`：样本 {int(r['count'])}，5日均值 {pct(r['ret5_mean'])}，胜率 {pct(r['ret5_win'])}。"

    lines = [
        "# G3 V4 big_bull 结构环境审计 v1",
        "",
        "## 本轮完成",
        "",
        "- 没有使用 `score/rank` 做过滤。",
        "- 对 G2 full 的 `big_bull_rebreak` 独立强势入口，回填上证指数 MA20/MA60/MA120、20日动量、全市场 MA20 广度、板块强3扩散、盘中强度、箱体突破幅度。",
        "- 这些门槛是固定解释型切片，不按收益最高反推参数。",
        "",
        "## 关键结论",
        "",
        f"- big_bull 全样本 {len(d)} 笔，5日均值 {pct(d['calc_ret_5d'].mean())}，胜率 {pct((d['calc_ret_5d'] > 0).mean())}。",
        best_line,
        "- 如果一个门槛只改善正常口径、但 train/valid/blind 任一段明显断裂，后续不能进入正式 G3，只能保留研究标签。",
        "",
        "## 固定结构门槛",
        "",
        md_table(gate_summary, pct_cols=pct_cols),
        "",
    ]
    for name, table in group_tables.items():
        lines.extend([f"## {name}", "", md_table(table, pct_cols=pct_cols), ""])
    lines.extend(
        [
            "## 最差样本",
            "",
            md_table(worst_examples(d)),
            "",
            "## 下一步目标",
            "",
            "- 若存在跨 train/valid/blind 都不塌的结构门槛，再做轻量组合复算：big_bull 仅作为强势市场补充入口，先用 1/4 仓位，不进入弱势/震荡路由。",
            "- 若结构门槛仍无法压住亏损，G3 强势链路应转向重新定义二次突破执行，而不是继续把 big_bull 加仓或调止盈止损。",
        ]
    )
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d = add_context(load_source())
    group_cols = [
        "window",
        "proxy_style",
        "breadth_bucket",
        "sector_s3_bucket",
        "intraday_strength_bucket",
        "box_breakout_bucket",
    ]
    group_tables = {col: summarize_group(d, col) for col in group_cols}
    gate_summary = summarize_gates(d)
    d.to_csv(OUT_DIR / "bigbull_context_enriched.csv", index=False, encoding="utf-8-sig")
    gate_summary.to_csv(OUT_DIR / "gate_summary.csv", index=False, encoding="utf-8-sig")
    for col, table in group_tables.items():
        table.to_csv(OUT_DIR / f"summary_by_{col}.csv", index=False, encoding="utf-8-sig")
    write_report(d, gate_summary, group_tables)
    print(f"done: {OUT_DIR}")
    print(gate_summary.to_string(index=False))


if __name__ == "__main__":
    main()
