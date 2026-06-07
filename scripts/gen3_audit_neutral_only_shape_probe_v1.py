from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reports" / "gen3_range_floor10_reclaim60_emotion_split_v1" / "neutral_only__cost30" / "closed_trades.csv"
OUT_DIR = ROOT / "reports" / "gen3_range_neutral_only_shape_probe_v1"


FEATURES = [
    "policy_net_ret",
    "range_pos60",
    "runup_from_60d_low",
    "close_position",
    "amount_ratio3",
    "amount_ratio20",
    "gap_open",
    "index_mom20",
    "limit_up_count",
    "limit_down_count",
    "bar_ret",
    "bar_close_pos",
    "d0_confirm_to_close",
    "d1_open_ret",
    "d1_close_ret",
    "d2_close_ret",
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, max_rows: int | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    view = df.copy()
    if max_rows is not None:
        view = view.head(max_rows)
    rows: list[dict[str, Any]] = []
    for _, row in view.iterrows():
        item: dict[str, Any] = {}
        for col in view.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load() -> pd.DataFrame:
    d = pd.read_csv(SOURCE)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce")
    d["year"] = d["entry_date"].dt.year
    for col in FEATURES:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.sort_values("policy_net_ret", ascending=False).reset_index(drop=True)
    d["shape_bucket"] = "loss"
    d.loc[d["policy_net_ret"].gt(0), "shape_bucket"] = "ordinary_win"
    d.loc[d.index < 3, "shape_bucket"] = "top3_win"
    return d


def bucket_summary(d: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    names = [
        ("top3_win", "前三大盈利样本"),
        ("ordinary_win", "普通盈利样本"),
        ("loss", "亏损样本"),
    ]
    for bucket, desc in names:
        g = d[d["shape_bucket"].eq(bucket)]
        item: dict[str, Any] = {
            "bucket": bucket,
            "中文解释": desc,
            "trade_count": int(len(g)),
            "sum_ret": float(g["policy_net_ret"].sum()) if len(g) else 0.0,
            "avg_ret": float(g["policy_net_ret"].mean()) if len(g) else 0.0,
            "win_rate": float((g["policy_net_ret"] > 0).mean()) if len(g) else 0.0,
        }
        for col in FEATURES:
            if col == "policy_net_ret":
                continue
            item[f"avg_{col}"] = float(g[col].mean()) if len(g) and col in g.columns else 0.0
        rows.append(item)
    return pd.DataFrame(rows)


def rule_probes(d: pd.DataFrame) -> pd.DataFrame:
    probes = [
        ("amount3_ge2", "30m承接量比>=2.0", d["amount_ratio3"].ge(2.0)),
        ("amount3_ge18", "30m承接量比>=1.8", d["amount_ratio3"].ge(1.8)),
        ("index_mom20_lt0", "指数20日动量<0，偏下跌/修复环境", d["index_mom20"].lt(0)),
        ("index_mom20_ge0", "指数20日动量>=0，偏反弹/偏强环境", d["index_mom20"].ge(0)),
        ("close_pos_60_75", "日线修复在60%-75%，不是过度修复", d["close_position"].between(0.60, 0.75, inclusive="both")),
        ("close_pos_ge75", "日线修复>=75%，收盘较高", d["close_position"].ge(0.75)),
        ("floor_le3", "箱体位置<=3%，更贴近箱体底部", d["range_pos60"].le(0.03)),
        ("floor_3_10", "箱体位置3%-10%，仍在底部但不极端", d["range_pos60"].between(0.03, 0.10, inclusive="both")),
        ("amount20_lt1", "日线20日量能<1，缩量修复", d["amount_ratio20"].lt(1.0)),
        ("amount20_ge1", "日线20日量能>=1，放量修复", d["amount_ratio20"].ge(1.0)),
        ("weak_d1_expost", "事后观察：D1收盘未转弱", d["d1_close_ret"].gt(-0.03)),
    ]
    rows: list[dict[str, Any]] = []
    for name, desc, mask in probes:
        g = d[mask].copy()
        rows.append(
            {
                "probe": name,
                "中文解释": desc,
                "trade_count": int(len(g)),
                "sum_ret": float(g["policy_net_ret"].sum()) if len(g) else 0.0,
                "avg_ret": float(g["policy_net_ret"].mean()) if len(g) else 0.0,
                "win_rate": float((g["policy_net_ret"] > 0).mean()) if len(g) else 0.0,
                "top3_count": int(g["shape_bucket"].eq("top3_win").sum()) if len(g) else 0,
                "loss_count": int(g["shape_bucket"].eq("loss").sum()) if len(g) else 0,
                "trade_2026": int(g["year"].eq(2026).sum()) if len(g) else 0,
                "sum_ret_2026": float(g[g["year"].eq(2026)]["policy_net_ret"].sum()) if len(g) else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values(["sum_ret", "trade_count"], ascending=[False, False])


def trade_table(d: pd.DataFrame) -> pd.DataFrame:
    keep = [
        "shape_bucket",
        "entry_date",
        "code",
        "name",
        "policy_net_ret",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "amount_ratio20",
        "index_mom20",
        "limit_up_count",
        "limit_down_count",
        "d1_close_ret",
        "d2_close_ret",
    ]
    out = d[keep].copy()
    out["entry_date"] = out["entry_date"].dt.strftime("%Y-%m-%d")
    return out


def feature_gap(summary: pd.DataFrame) -> pd.DataFrame:
    top = summary[summary["bucket"].eq("top3_win")].iloc[0]
    loss = summary[summary["bucket"].eq("loss")].iloc[0]
    rows: list[dict[str, Any]] = []
    for col in FEATURES:
        if col == "policy_net_ret":
            continue
        k = f"avg_{col}"
        rows.append(
            {
                "feature": col,
                "中文解释": {
                    "range_pos60": "60日箱体位置",
                    "runup_from_60d_low": "距离60日低点反弹幅度",
                    "close_position": "日线收盘修复位置",
                    "amount_ratio3": "30m成交额相对前三根均量",
                    "amount_ratio20": "日线成交额相对20日均量",
                    "gap_open": "入场日跳空",
                    "index_mom20": "指数20日动量",
                    "limit_up_count": "前一日涨停家数",
                    "limit_down_count": "前一日跌停家数",
                    "bar_ret": "确认30m涨跌幅",
                    "bar_close_pos": "确认30m收盘位置",
                    "d0_confirm_to_close": "入场日至收盘收益",
                    "d1_open_ret": "D1开盘收益",
                    "d1_close_ret": "D1收盘收益",
                    "d2_close_ret": "D2收盘收益",
                }.get(col, col),
                "top3_avg": float(top[k]),
                "loss_avg": float(loss[k]),
                "gap_top3_minus_loss": float(top[k] - loss[k]),
            }
        )
    return pd.DataFrame(rows).sort_values("gap_top3_minus_loss", ascending=False)


def write_report(summary: pd.DataFrame, gaps: pd.DataFrame, probes: pd.DataFrame, trades: pd.DataFrame) -> None:
    pct_cols = {
        "sum_ret",
        "avg_ret",
        "win_rate",
        "avg_range_pos60",
        "avg_runup_from_60d_low",
        "avg_close_position",
        "avg_amount_ratio3",
        "avg_amount_ratio20",
        "avg_gap_open",
        "avg_index_mom20",
        "avg_bar_ret",
        "avg_bar_close_pos",
        "avg_d0_confirm_to_close",
        "avg_d1_open_ret",
        "avg_d1_close_ret",
        "avg_d2_close_ret",
        "top3_avg",
        "loss_avg",
        "gap_top3_minus_loss",
        "policy_net_ret",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "amount_ratio20",
        "index_mom20",
        "d1_close_ret",
        "d2_close_ret",
        "sum_ret_2026",
    }
    lines = [
        "# G3 neutral_only 盈亏形态探针 v1",
        "",
        "## 策略名解释",
        "",
        "- `neutral_only`：中性情绪横盘箱体底部修复源，不要求前一日出现冰点。",
        "- `top3_win`：前三大盈利样本，用来观察收益来源核心形态。",
        "- `ordinary_win`：除前三大盈利外的普通盈利样本。",
        "- `loss`：亏损样本。",
        "- `amount3_ge2`：确认30m成交额相对前三根均量至少2倍，只是探针，不是正式参数。",
        "",
        "## 本轮结论",
        "",
        "- `neutral_only` 的核心赢家更像“30m承接强、指数仍偏弱时的错杀修复”，不是简单的中性环境随便买。",
        "- 亏损样本并不都是30m弱，说明单纯提高 `amount_ratio3` 不能解决问题；但强30m承接可以解释一部分大赢家。",
        "- 2026 两笔亏损分别属于指数动量转正后的消费/防御修复失败，提示 neutral 需要增加“后续承接确认”或“板块扩散确认”，而不是仅靠入场日一次承接。",
        "- 本轮所有规则都是小样本探针，不作为正式策略参数。下一步应验证 30m 二次承接源。",
        "",
        "## 三类形态对照",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 前三大赢家 vs 亏损样本差异",
        "",
        md_table(gaps, pct_cols=pct_cols),
        "",
        "## 结构探针",
        "",
        md_table(probes, pct_cols=pct_cols),
        "",
        "## 逐笔形态表",
        "",
        md_table(trades, pct_cols=pct_cols),
        "",
        "## 下一步目标",
        "",
        "- 构造 `neutral_second_acceptance`：中性情绪下，第一次30m承接后，要求 D0 后半日或 D1 再出现一次可见放量承接。",
        "- 只验证方向，不扩大参数搜索：固定 1-2 个二次承接定义，做全周期、2024-2025、2026 和 2%冲击压力。",
        "- 如果二次承接仍不能改善 2026/冲击口径，就停止在 neutral 上加过滤，回到新横盘候选源重建。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d = load()
    summary = bucket_summary(d)
    gaps = feature_gap(summary)
    probes = rule_probes(d)
    trades = trade_table(d)
    summary.to_csv(OUT_DIR / "bucket_summary.csv", index=False, encoding="utf-8-sig")
    gaps.to_csv(OUT_DIR / "feature_gap_top3_vs_loss.csv", index=False, encoding="utf-8-sig")
    probes.to_csv(OUT_DIR / "rule_probes.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / "trade_shapes.csv", index=False, encoding="utf-8-sig")
    write_report(summary, gaps, probes, trades)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
