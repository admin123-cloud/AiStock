from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _ret_from_price  # noqa: E402
from scripts.gen3_test_v4_strong_entry_warning_d1d2_30m_weak_confirm_v1 import (  # noqa: E402
    COST_BPS,
    CUTOFF,
    WEAK_RET,
    add_d1_d2_dates,
    load_30m_bars,
    load_base,
)


OUT_DIR = ROOT / "reports" / "gen3_v4_strong_entry_warning_d1_repair_fail_v1"


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


def cutoff_context(g: pd.DataFrame, entry_price: float) -> dict[str, Any]:
    if g.empty or entry_price <= 0:
        return {"status": "missing"}
    usable = g[g["time_text"] <= CUTOFF].sort_values("datetime")
    if usable.empty:
        return {"status": "no_cutoff_bar", "bar_count": int(len(g))}
    bar = usable.iloc[-1]
    return {
        "status": "ok",
        "datetime": pd.Timestamp(bar["datetime"]),
        "open": float(bar["open"]),
        "high": float(usable["high"].max()),
        "low": float(usable["low"].min()),
        "close": float(bar["close"]),
        "close_ret": float(bar["close"]) / entry_price - 1.0,
        "high_ret": float(usable["high"].max()) / entry_price - 1.0,
        "low_ret": float(usable["low"].min()) / entry_price - 1.0,
    }


def classify(row: pd.Series) -> str:
    old = float(row["original_policy_net_ret"])
    new = float(row["policy_net_ret"])
    delta = new - old
    if old < 0 and delta > 0:
        return "救亏损"
    if old > 0 and delta < 0:
        return "误伤盈利"
    if old < 0 and delta < 0:
        return "加深亏损"
    if old > 0 and delta > 0:
        return "改善盈利"
    return "中性"


def apply_repair_fail_policy(candidates: pd.DataFrame, bars: pd.DataFrame, action: str) -> pd.DataFrame:
    d = candidates.copy()
    d["repair_fail_action"] = action
    d["repair_fail_note"] = "base_policy"
    d["repair_fail_exit_datetime"] = pd.NaT
    d["repair_fail_exit_ret_30bps"] = pd.NA
    d["d1_1030_ret"] = pd.NA
    d["d1_1030_high_ret"] = pd.NA
    d["d2_1030_ret"] = pd.NA
    d["d2_1030_high_ret"] = pd.NA
    d["d2_higher_close_vs_d1"] = False
    d["d2_higher_high_vs_d1"] = False
    by_key = {(str(code), pd.Timestamp(day).normalize()): g.copy() for (code, day), g in bars.groupby(["code", "trade_date"])}

    mask = d["route"].astype(str).eq("strong_main") & d["entry_warning_strict"].fillna(False)
    for idx, row in d[mask].iterrows():
        code = str(row["code"])
        entry_price = float(row["entry_price"])
        old_exit = pd.Timestamp(row["policy_exit_date"]).normalize()
        d1 = row.get("d1_date")
        d2 = row.get("d2_date")
        if pd.isna(d1) or pd.isna(d2):
            continue
        d1 = pd.Timestamp(d1).normalize()
        d2 = pd.Timestamp(d2).normalize()
        if d2 >= old_exit:
            continue

        d1_ctx = cutoff_context(by_key.get((code, d1), pd.DataFrame()), entry_price)
        d2_ctx = cutoff_context(by_key.get((code, d2), pd.DataFrame()), entry_price)
        if d1_ctx.get("status") == "ok":
            d.at[idx, "d1_1030_ret"] = d1_ctx["close_ret"]
            d.at[idx, "d1_1030_high_ret"] = d1_ctx["high_ret"]
        if d2_ctx.get("status") == "ok":
            d.at[idx, "d2_1030_ret"] = d2_ctx["close_ret"]
            d.at[idx, "d2_1030_high_ret"] = d2_ctx["high_ret"]
        if d1_ctx.get("status") != "ok" or d2_ctx.get("status") != "ok":
            continue
        if float(d1_ctx["close_ret"]) > WEAK_RET:
            continue

        higher_close = float(d2_ctx["close"]) > float(d1_ctx["close"])
        higher_high = float(d2_ctx["high"]) > float(d1_ctx["high"])
        d.at[idx, "d2_higher_close_vs_d1"] = bool(higher_close)
        d.at[idx, "d2_higher_high_vs_d1"] = bool(higher_high)
        if higher_close or higher_high:
            continue

        exit_ret = _ret_from_price(float(d2_ctx["close"]), entry_price, COST_BPS)
        if exit_ret is None:
            continue
        original = float(row["policy_net_ret"])
        d.at[idx, "repair_fail_note"] = "strict_warning_d1_weak_no_d2_1030_repair"
        d.at[idx, "repair_fail_exit_datetime"] = d2_ctx["datetime"]
        d.at[idx, "repair_fail_exit_ret_30bps"] = exit_ret
        if action == "fast_exit":
            d.at[idx, "policy_exit_date"] = pd.Timestamp(d2_ctx["datetime"]).normalize()
            d.at[idx, "policy_net_ret"] = exit_ret
        elif action == "half_proxy":
            d.at[idx, "policy_net_ret"] = 0.5 * exit_ret + 0.5 * original
        else:
            raise ValueError(action)

    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    d["delta_ret"] = d["policy_net_ret"] - d["original_policy_net_ret"]
    d["outcome"] = d.apply(classify, axis=1)
    return d


def summarize(candidates: pd.DataFrame, variant: str) -> dict[str, Any]:
    strong = candidates[candidates["route"].astype(str).eq("strong_main")]
    triggered = strong[strong["repair_fail_note"].astype(str).ne("base_policy")]
    return {
        "variant": variant,
        "rows": int(len(candidates)),
        "strong_rows": int(len(strong)),
        "triggered_rows": int(len(triggered)),
        "all_mean_ret": float(candidates["policy_net_ret"].mean()),
        "strong_mean_ret": float(strong["policy_net_ret"].mean()) if len(strong) else 0.0,
        "triggered_original_mean": float(triggered["original_policy_net_ret"].mean()) if len(triggered) else 0.0,
        "triggered_new_mean": float(triggered["policy_net_ret"].mean()) if len(triggered) else 0.0,
        "triggered_delta_mean": float(triggered["delta_ret"].mean()) if len(triggered) else 0.0,
        "triggered_worst_new": float(triggered["policy_net_ret"].min()) if len(triggered) else 0.0,
        "outcome_counts": {str(k): int(v) for k, v in triggered["outcome"].value_counts().to_dict().items()},
    }


def write_report(summary_df: pd.DataFrame, triggered: pd.DataFrame) -> None:
    pct_cols = {
        "all_mean_ret",
        "strong_mean_ret",
        "triggered_original_mean",
        "triggered_new_mean",
        "triggered_delta_mean",
        "triggered_worst_new",
        "原始均值",
        "新均值",
        "平均变化",
    }
    by_outcome = (
        triggered.groupby("outcome")
        .agg(
            笔数=("code", "count"),
            原始均值=("original_policy_net_ret", "mean"),
            新均值=("policy_net_ret", "mean"),
            平均变化=("delta_ret", "mean"),
            D1均值=("d1_1030_ret", "mean"),
            D2均值=("d2_1030_ret", "mean"),
        )
        .reset_index()
        if not triggered.empty
        else pd.DataFrame()
    )

    focus = triggered.sort_values("delta_ret").copy()
    for col in [
        "original_policy_net_ret",
        "policy_net_ret",
        "delta_ret",
        "repair_fail_exit_ret_30bps",
        "d1_1030_ret",
        "d1_1030_high_ret",
        "d2_1030_ret",
        "d2_1030_high_ret",
        "max_high_ret",
    ]:
        if col in focus.columns:
            focus[col] = focus[col].map(pct)
    focus["entry_date"] = pd.to_datetime(focus["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    focus_cols = [
        "entry_date",
        "code",
        "name",
        "original_policy_net_ret",
        "policy_net_ret",
        "delta_ret",
        "outcome",
        "repair_fail_exit_ret_30bps",
        "d1_1030_ret",
        "d1_1030_high_ret",
        "d2_1030_ret",
        "d2_1030_high_ret",
        "max_high_ret",
        "failure_tag",
    ]
    focus = focus[[c for c in focus_cols if c in focus.columns]].rename(
        columns={
            "entry_date": "入场日",
            "code": "代码",
            "name": "名称",
            "original_policy_net_ret": "原始收益",
            "policy_net_ret": "处理后收益",
            "delta_ret": "变化",
            "outcome": "结果",
            "repair_fail_exit_ret_30bps": "D2 10:30退出收益",
            "d1_1030_ret": "D1 10:30",
            "d1_1030_high_ret": "D1高点",
            "d2_1030_ret": "D2 10:30",
            "d2_1030_high_ret": "D2高点",
            "max_high_ret": "持仓最高",
            "failure_tag": "失败标签",
        }
    )

    lines = [
        "# G3 V4 strong_main 入场警戒 + D1弱后无法修复 v1",
        "",
        "## 边界",
        "",
        "- 不使用 score/rank 新增过滤。",
        "- 入场警戒固定为：入场日上影 >= 50% 且收盘低于 20 日高点 2% 以上。",
        "- D1 10:30 相对入场价 <= -3% 先定义为早弱。",
        "- 修复结构固定为：D2 10:30 前出现比 D1 10:30 更高的高点或更高的收盘，即视为有修复；没有则视为弱后无法修复。",
        "- 只验证结构，不搜索阈值；`half_proxy` 仍然只是半仓代理。",
        "",
        "## 汇总",
        "",
        md_table(summary_df, pct_cols=pct_cols),
        "",
        "## 触发结果分类",
        "",
        md_table(by_outcome, pct_cols=pct_cols),
        "",
        "## 触发明细",
        "",
        md_table(focus.head(40)),
        "",
        "## 判断",
        "",
        "- 如果这个结构仍不能优于 base，说明强势链路的核心问题不在退出时点微调，而在入场后的强弱分化需要更早、更贴近盘口的确认。",
        "- 如果它减少误伤但收益仍下降，只能作为风险标签，不应升级为正式减仓/退出。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = add_d1_d2_dates(load_base())
    bars = load_30m_bars(base)
    base_out = base.copy()
    base_out["repair_fail_note"] = "base_policy"
    base_out["delta_ret"] = 0.0
    base_out["outcome"] = "中性"
    variants = {"base": base_out}
    for action in ["fast_exit", "half_proxy"]:
        d = apply_repair_fail_policy(base, bars, action)
        variants[f"d1_weak_no_d2_repair_{action}"] = d
        d.to_csv(OUT_DIR / f"d1_weak_no_d2_repair_{action}_candidates.csv", index=False, encoding="utf-8-sig")
    summary_df = pd.DataFrame([summarize(d, name) for name, d in variants.items()])
    summary_df.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    triggered = variants["d1_weak_no_d2_repair_half_proxy"]
    triggered = triggered[triggered["route"].astype(str).eq("strong_main") & triggered["repair_fail_note"].astype(str).ne("base_policy")].copy()
    triggered.to_csv(OUT_DIR / "triggered_trades_half_proxy.csv", index=False, encoding="utf-8-sig")
    write_report(summary_df, triggered)
    print(f"done: {OUT_DIR}")
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()
