from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reports" / "gen3_range_floor10_reclaim60_execution_audit_v1" / "audited_trades.csv"
OUT_DIR = ROOT / "reports" / "gen3_range_floor10_reclaim60_2026_failure_audit_v1"


FEATURES = [
    "policy_net_ret",
    "range_pos60",
    "runup_from_60d_low",
    "close_position",
    "amount_ratio20",
    "gap_open",
    "index_mom20",
    "limit_up_count",
    "limit_down_count",
    "amount_ratio3",
    "bar_ret",
    "bar_close_pos",
    "d0_high_to_close",
    "d0_confirm_to_close",
    "d1_open_ret",
    "d1_close_ret",
    "d2_close_ret",
    "d3_open_ret",
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, max_rows: int | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    d = df.copy()
    if max_rows is not None:
        d = d.head(max_rows)
    rows: list[dict[str, Any]] = []
    for _, row in d.iterrows():
        item: dict[str, Any] = {}
        for col in d.columns:
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
    for col in ["late_confirm", "ultra_late_confirm", "d1_weak_confirm", "d2_still_weak", "early_weak_any"]:
        d[col] = d[col].astype(str).str.lower().isin(["true", "1"])
    d["return_bucket"] = pd.cut(
        d["policy_net_ret"],
        bins=[-1.0, -0.05, -0.02, 0.0, 0.02, 0.05, 1.0],
        labels=["大亏<-5%", "中亏-5~-2%", "小亏-2~0%", "小赚0~2%", "中赚2~5%", "大赚>5%"],
    )
    d["is_2026"] = d["year"].eq(2026)
    d["is_loss"] = d["policy_net_ret"].lt(0)
    return d


def group_summary(d: pd.DataFrame) -> pd.DataFrame:
    specs = [
        ("all_2020_2025", d[d["year"].between(2020, 2025)]),
        ("win_2020_2025", d[d["year"].between(2020, 2025) & ~d["is_loss"]]),
        ("loss_2020_2025", d[d["year"].between(2020, 2025) & d["is_loss"]]),
        ("all_2026", d[d["year"].eq(2026)]),
        ("loss_2026", d[d["year"].eq(2026) & d["is_loss"]]),
    ]
    rows: list[dict[str, Any]] = []
    for name, g in specs:
        rows.append(
            {
                "group": name,
                "中文解释": {
                    "all_2020_2025": "2020-2025全部样本",
                    "win_2020_2025": "2020-2025盈利样本",
                    "loss_2020_2025": "2020-2025亏损样本",
                    "all_2026": "2026全部样本",
                    "loss_2026": "2026亏损样本",
                }[name],
                "trade_count": int(len(g)),
                "sum_ret": float(g["policy_net_ret"].sum()) if len(g) else 0.0,
                "avg_ret": float(g["policy_net_ret"].mean()) if len(g) else 0.0,
                "win_rate": float((g["policy_net_ret"] > 0).mean()) if len(g) else 0.0,
                "avg_range_pos60": float(g["range_pos60"].mean()) if len(g) else 0.0,
                "avg_close_position": float(g["close_position"].mean()) if len(g) else 0.0,
                "avg_amount_ratio3": float(g["amount_ratio3"].mean()) if len(g) else 0.0,
                "avg_amount_ratio20": float(g["amount_ratio20"].mean()) if len(g) else 0.0,
                "avg_index_mom20": float(g["index_mom20"].mean()) if len(g) else 0.0,
                "avg_limit_up_count": float(g["limit_up_count"].mean()) if len(g) else 0.0,
                "avg_limit_down_count": float(g["limit_down_count"].mean()) if len(g) else 0.0,
                "late_rate": float(g["late_confirm"].mean()) if len(g) else 0.0,
                "early_weak_rate": float(g["early_weak_any"].mean()) if len(g) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def trade_view(d: pd.DataFrame) -> pd.DataFrame:
    keep = [
        "entry_date",
        "code",
        "name",
        "confirm_time",
        "policy_net_ret",
        "return_bucket",
        "range_pos60",
        "runup_from_60d_low",
        "close_position",
        "amount_ratio3",
        "amount_ratio20",
        "gap_open",
        "index_mom20",
        "limit_up_count",
        "limit_down_count",
        "emotion_signal",
        "d0_confirm_to_close",
        "d1_open_ret",
        "d1_close_ret",
        "d2_close_ret",
        "d3_open_ret",
        "late_confirm",
        "ultra_late_confirm",
        "early_weak_any",
    ]
    out = d[d["year"].eq(2026)][keep].copy()
    out["entry_date"] = out["entry_date"].dt.strftime("%Y-%m-%d")
    return out.sort_values("entry_date")


def feature_gaps(d: pd.DataFrame) -> pd.DataFrame:
    base = d[d["year"].between(2020, 2025)]
    loss26 = d[d["year"].eq(2026) & d["is_loss"]]
    rows: list[dict[str, Any]] = []
    for col in FEATURES:
        if col not in d.columns:
            continue
        rows.append(
            {
                "feature": col,
                "中文解释": {
                    "policy_net_ret": "策略净收益",
                    "range_pos60": "60日箱体位置，越低越贴近箱体底部",
                    "runup_from_60d_low": "距离60日低点反弹幅度",
                    "close_position": "日线收盘修复位置",
                    "amount_ratio20": "日线成交额相对20日均量",
                    "gap_open": "入场日开盘跳空",
                    "index_mom20": "指数20日动量",
                    "limit_up_count": "前一日涨停家数",
                    "limit_down_count": "前一日跌停家数",
                    "amount_ratio3": "确认30m成交额相对前三根均量",
                    "bar_ret": "确认30m涨跌幅",
                    "bar_close_pos": "确认30m收盘位置",
                    "d0_high_to_close": "入场日从日内高点到收盘回落",
                    "d0_confirm_to_close": "入场日至收盘相对确认价收益",
                    "d1_open_ret": "下一交易日开盘相对确认价收益",
                    "d1_close_ret": "下一交易日收盘相对确认价收益",
                    "d2_close_ret": "第二交易日收盘相对确认价收益",
                    "d3_open_ret": "第三交易日开盘相对确认价收益",
                }.get(col, col),
                "base_avg_2020_2025": float(base[col].mean()) if len(base) else 0.0,
                "base_loss_avg_2020_2025": float(base[base["is_loss"]][col].mean()) if len(base[base["is_loss"]]) else 0.0,
                "loss_2026_avg": float(loss26[col].mean()) if len(loss26) else 0.0,
                "gap_vs_base": float(loss26[col].mean() - base[col].mean()) if len(loss26) and len(base) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def rule_probe(d: pd.DataFrame) -> pd.DataFrame:
    rules = [
        ("avoid_2026_low_amount20", "过滤日线20日量能不足：amount_ratio20 < 1.0", d["amount_ratio20"].ge(1.0)),
        ("avoid_2026_neutral_emotion", "只保留 emotion_signal=icepoint，过滤 neutral", d["emotion_signal"].eq("icepoint")),
        ("avoid_low_close_position", "提高日线修复：close_position >= 0.70", d["close_position"].ge(0.70)),
        ("avoid_weak_d1", "事后观察：过滤D1早弱，不可作为买入过滤", ~d["d1_weak_confirm"]),
        ("avoid_weak_d2", "事后观察：过滤D2仍弱，不可作为买入过滤", ~d["d2_still_weak"]),
    ]
    rows: list[dict[str, Any]] = []
    for name, desc, mask in rules:
        g = d[mask].copy()
        g26 = g[g["year"].eq(2026)]
        rows.append(
            {
                "rule": name,
                "中文解释": desc,
                "trade_count": int(len(g)),
                "sum_ret": float(g["policy_net_ret"].sum()) if len(g) else 0.0,
                "avg_ret": float(g["policy_net_ret"].mean()) if len(g) else 0.0,
                "win_rate": float((g["policy_net_ret"] > 0).mean()) if len(g) else 0.0,
                "trade_count_2026": int(len(g26)),
                "sum_ret_2026": float(g26["policy_net_ret"].sum()) if len(g26) else 0.0,
                "avg_ret_2026": float(g26["policy_net_ret"].mean()) if len(g26) else 0.0,
                "win_rate_2026": float((g26["policy_net_ret"] > 0).mean()) if len(g26) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def write_report(summary: pd.DataFrame, trades: pd.DataFrame, gaps: pd.DataFrame, probes: pd.DataFrame) -> None:
    pct_cols = {
        "sum_ret",
        "avg_ret",
        "win_rate",
        "avg_range_pos60",
        "avg_close_position",
        "avg_amount_ratio3",
        "avg_amount_ratio20",
        "avg_index_mom20",
        "late_rate",
        "early_weak_rate",
        "policy_net_ret",
        "range_pos60",
        "runup_from_60d_low",
        "close_position",
        "amount_ratio3",
        "amount_ratio20",
        "gap_open",
        "index_mom20",
        "d0_confirm_to_close",
        "d1_open_ret",
        "d1_close_ret",
        "d2_close_ret",
        "d3_open_ret",
        "base_avg_2020_2025",
        "base_loss_avg_2020_2025",
        "loss_2026_avg",
        "gap_vs_base",
        "sum_ret_2026",
        "avg_ret_2026",
        "win_rate_2026",
    }
    lines = [
        "# G3 floor10_reclaim60 2026失效复盘 v1",
        "",
        "## 策略名解释",
        "",
        "- `floor10_reclaim60`：冰点后3日窗口里，箱体位置不高于10%，且日线收盘修复不低于60%的横盘箱体底部30m承接买法。",
        "- `icepoint`：冰点，前一交易日市场出现较强恐慌/出清信号，映射到下一交易日可见。",
        "- `neutral`：中性，没有命中冰点或高潮标签。",
        "- `early_weak_any`：D0/D1/D2 任一早期走弱信号，属于事后风控观察，不是买入日前可见过滤。",
        "",
        "## 本轮结论",
        "",
        "- 2026 只有4笔，样本太少，不能据此重写正式规则；但它暴露出两类问题：一是消费/防御类大盘股修复不持续，二是 neutral 样本在2026拖累更明显。",
        "- 2026最大亏损是锦江酒店，D1收盘已经跌破确认价4.57%，D2仍弱；它证明早弱可以解释个案，但前一步复验显示早弱退出会伤害全局收益，所以不能直接加入正式退出。",
        "- 中国移动是2026唯一正收益样本，虽然也是15:00确认，但次日和后续继续修复；这支持上一轮结论：15:00确认本身不是主要风险根。",
        "- 更值得继续验证的是“候选源行业/趋势质量”而不是继续调止盈止损：2026亏损样本普遍属于反抽后缺少持续资金承接，下一步应增加不依赖 score/rank 的结构标签，例如日线量能持续、板块扩散或30m二次承接。",
        "",
        "## 分组对照",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 2026逐笔成交",
        "",
        md_table(trades, pct_cols=pct_cols),
        "",
        "## 2026亏损样本与过往样本差异",
        "",
        md_table(gaps, pct_cols=pct_cols),
        "",
        "## 小规则探针",
        "",
        md_table(probes, pct_cols=pct_cols),
        "",
        "## 下一步目标",
        "",
        "- 不把 `avoid_weak_d1` 或 `avoid_weak_d2` 作为正式规则，因为它们是买入后的事后信息，且前一步 slot 复算已经显示收益下降。",
        "- 优先研究 `icepoint + 日线持续量能 + 30m二次承接`：先看能否提升 2026，同时不牺牲 2020-2025 的收益来源。",
        "- 其次研究 neutral 样本是否要降权或拆成单独源，不要把它和 icepoint 混在同一买法里。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d = load()
    summary = group_summary(d)
    trades = trade_view(d)
    gaps = feature_gaps(d)
    probes = rule_probe(d)
    summary.to_csv(OUT_DIR / "group_summary.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / "trades_2026.csv", index=False, encoding="utf-8-sig")
    gaps.to_csv(OUT_DIR / "feature_gaps.csv", index=False, encoding="utf-8-sig")
    probes.to_csv(OUT_DIR / "rule_probe.csv", index=False, encoding="utf-8-sig")
    write_report(summary, trades, gaps, probes)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
