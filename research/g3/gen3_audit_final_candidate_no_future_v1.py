from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path

import pandas as pd


ROOT = _PROJECT_ROOT
OUT_DIR = _report_path() / "gen3_final_candidate_no_future_audit_v1"
TRADES = _report_path() / "gen3_combo_panic_strong_execution_stress_v1" / "base_30bps_closed_trades.csv"


def _dt(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce")


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_无数据_"
    return df.to_markdown(index=False)


def _as_chain(df: pd.DataFrame) -> pd.Series:
    chain = df.get("chain", pd.Series("", index=df.index)).astype(str)
    g3 = df.get("g3_chain", pd.Series("", index=df.index)).astype(str)
    return chain.where(chain.ne("") & chain.ne("nan"), g3)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(TRADES, low_memory=False)
    df["audit_chain"] = _as_chain(df)
    df["entry_date_dt"] = _dt(df["entry_date"]).dt.normalize()
    df["confirm_dt"] = _dt(df.get("confirm_datetime"))
    df["entry_ts_dt"] = _dt(df.get("entry_ts")).dt.normalize()
    df["trade_date_dt"] = _dt(df.get("trade_date")).dt.normalize()
    df["factor_date_dt"] = _dt(df.get("factor_date")).dt.normalize()
    df["policy_exit_date_dt"] = _dt(df.get("policy_exit_date", df.get("exit_date"))).dt.normalize()
    df["exit_date_dt"] = _dt(df.get("exit_date")).dt.normalize()

    rows = []
    for chain, g in df.groupby("audit_chain", dropna=False):
        confirm_same_day = (g["confirm_dt"].dt.normalize() == g["entry_date_dt"]).mean() if g["confirm_dt"].notna().any() else 0.0
        confirm_after_entry_day = (g["confirm_dt"].dt.normalize() > g["entry_date_dt"]).sum() if g["confirm_dt"].notna().any() else 0
        factor_prev_or_same = (g["factor_date_dt"].notna() & (g["factor_date_dt"] <= g["entry_date_dt"])).mean() if g["factor_date_dt"].notna().any() else 0.0
        trade_prev_or_same = (g["trade_date_dt"].notna() & (g["trade_date_dt"] <= g["entry_date_dt"])).mean() if g["trade_date_dt"].notna().any() else 0.0
        rows.append(
            {
                "chain": chain,
                "rows": len(g),
                "confirm_rows": int(g["confirm_dt"].notna().sum()),
                "confirm_same_entry_day_rate": _pct(confirm_same_day),
                "confirm_after_entry_day_rows": int(confirm_after_entry_day),
                "factor_date_rows": int(g["factor_date_dt"].notna().sum()),
                "factor_date_le_entry_rate": _pct(factor_prev_or_same),
                "trade_date_rows": int(g["trade_date_dt"].notna().sum()),
                "trade_date_le_entry_rate": _pct(trade_prev_or_same),
                "policy_exit_ge_entry_rate": _pct((g["policy_exit_date_dt"] >= g["entry_date_dt"]).mean()),
            }
        )
    chain_summary = pd.DataFrame(rows)

    time_rows = []
    if df["confirm_dt"].notna().any():
        part = df[df["confirm_dt"].notna()].copy()
        part["confirm_time"] = part["confirm_dt"].dt.strftime("%H:%M:%S")
        time_rows = (
            part.groupby(["audit_chain", "confirm_time"])
            .size()
            .reset_index(name="rows")
            .sort_values(["audit_chain", "confirm_time"])
        )
    else:
        time_rows = pd.DataFrame()

    field_rows = []
    field_policy = [
        ("panic", "market_style/ma_skeleton/volume_price_layer/adx_layer", "主要来自信号日前或当日已确认日线状态；若用于 10:00/10:30 买入，必须落到 D-1 快照或盘中实时重算。", "需实盘代理"),
        ("panic", "breadth_ma20/up_rate/big_down_rate/index_mom20", "恐慌环境字段在历史回放里多为当日环境结果；盘中买入时不能直接使用收盘后全日统计。", "高风险"),
        ("panic", "confirm_datetime/bar_time/bar_close_pos/bar_ret/amount_ratio3", "30m bar 收盘后可见；10:00 信号表示 9:30-10:00 bar 完成后。", "可实盘"),
        ("panic", "m30_close5_full_nextopen", "触发后按下一可成交口径退出，已有 next-open/延迟压力测试。", "可实盘但需执行层"),
        ("strong", "factor_date/Alpha191/V4 score/volume5 score", "若 factor_date 为 D-1 或更早，可以作为次日候选；若同日盘中使用必须改为实时代理。", "需实盘代理"),
        ("strong", "confirm_datetime/intraday_normal_datetime/rt_*", "30m 确认字段在对应 bar 收盘后可见，适合盘中二次确认。", "可实盘"),
        ("strong", "outcome_fwd_ret_* / fwd_ret_*", "只允许用于研究标签和回测收益，不允许进入选股、排序、过滤或买卖判断。", "禁止上线"),
        ("strong", "next_open_second_leg / limit_down_delay", "属于执行压力近似，不是逐笔排队模拟；正式上线需要实际成交回报和涨跌停可卖判断。", "可实盘但需执行层"),
    ]
    for chain, fields, reason, verdict in field_policy:
        field_rows.append({"chain": chain, "fields": fields, "reason": reason, "verdict": verdict})
    field_audit = pd.DataFrame(field_rows)

    exit_counts = (
        df.groupby(["audit_chain", df.get("policy", pd.Series("", index=df.index)).astype(str), df.get("exit_source", pd.Series("", index=df.index)).astype(str), df.get("exit_reason_proxy", pd.Series("", index=df.index)).astype(str)])
        .size()
        .reset_index(name="rows")
        .rename(columns={"level_1": "policy", "level_2": "exit_source", "level_3": "exit_reason_proxy"})
    )
    if len(exit_counts.columns) >= 5:
        exit_counts.columns = ["chain", "policy", "exit_source", "exit_reason_proxy", "rows"]

    chain_summary.to_csv(OUT_DIR / "chain_visibility_summary.csv", index=False, encoding="utf-8-sig")
    time_rows.to_csv(OUT_DIR / "confirm_time_distribution.csv", index=False, encoding="utf-8-sig")
    field_audit.to_csv(OUT_DIR / "field_visibility_audit.csv", index=False, encoding="utf-8-sig")
    exit_counts.to_csv(OUT_DIR / "exit_policy_counts.csv", index=False, encoding="utf-8-sig")

    report = f"""# G3 正式候选未来函数与实盘可见性审计 V1

生成日期：2026-06-01

## 审计对象

- 候选版本：`g3_final_panic_strong_base_30bps`
- 明细文件：`reports/gen3_combo_panic_strong_execution_stress_v1/base_30bps_closed_trades.csv`
- 样本数：`{len(df)}` 笔闭合交易

## 总结论

当前 G3 正式候选可以继续作为“研究正式候选包”，但还不能直接等同于“可自动下单版本”。

原因不是收益曲线，而是可见性分层还没有完全产品化：

- `panic` 的 30m 确认本身是盘中可见的，但部分市场环境/恐慌字段需要从 D-1 快照或盘中实时重算替代历史全日字段。
- `strong` 的 30m 二次确认可见，但 `volume5 / Alpha191 / V4 score` 必须确认只来自 D-1 或盘中代理，不能把同日收盘后的排序当成盘中信号。
- `outcome_fwd_ret_* / fwd_ret_*` 这类未来收益字段只能做研究和标签，不得进入正式选股或过滤。
- 跌停延迟和次日开盘成交已经做了压力测试，但仍是日线级近似，不是逐笔成交模型。

## 链路可见性统计

{_md_table(chain_summary)}

## 确认时间分布

{_md_table(time_rows)}

## 字段级审计

{_md_table(field_audit)}

## 卖出与执行口径统计

{_md_table(exit_counts)}

## 可上线等级

### 可以保留为正式研究候选

- `panic final`
- `strong volume5 risk-layer`
- `panic + strong 50/50 base_30bps`

这些已经通过长周期、压力成本、跌停延迟、年度稳定性和基准超额初审。

### 进入自动交易前必须补齐

1. Panic 环境字段实盘化

将 `breadth_ma20`、`up_rate`、`big_down_rate`、`index_mom20`、`market_amount_ratio20` 等字段明确改成：

- D-1 已确认快照，或
- 截至当前 30m bar 的盘中重算。

不能使用信号日收盘后的全日统计来决定 10:00/10:30 买入。

2. Strong 排序字段实盘化

`volume5`、`V4 rank/score`、`Alpha191 overlay` 必须明确口径：

- 若是盘前候选，只能使用 `factor_date <= entry_date - 1 trading day`。
- 若是盘中候选，必须使用 `intraday_v4_proxy_rank` 或实时重算字段。

3. 未来收益字段隔离

所有 `outcome_fwd_ret_*`、`fwd_ret_*`、`mfe_*`、`mae_*` 字段只允许在训练、审计、复盘文件中出现，不能进入正式候选生成函数的排序、过滤或 gate。

4. 执行层落地

当前 `next_open_second_leg` 和 `limit_down_delay` 是压力测试口径。实盘版本还要补：

- 涨停/跌停真实可成交判断。
- 停牌和一字板处理。
- 委托失败重试。
- 成交回报同步。
- 次日低开冲击记录。

## 阶段判断

这一步没有发现“已经被证明存在的硬未来函数”，但发现了“如果直接把研究字段搬去实盘，就会产生未来函数”的高风险区域。

因此当前状态应定义为：

`研究候选通过，自动交易未通过。`

## 下一步目标

第50步应做“实盘候选字段白名单”：

- 给 panic 和 strong 分别列出允许上线字段。
- 生成禁止字段黑名单。
- 在 G3 候选生成脚本里增加字段白名单检查。
- 输出一版 `G3 live-safe candidate schema`，作为后续接入页面或影子实盘的前置条件。
"""
    (OUT_DIR / "g3_final_candidate_no_future_audit_report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(f"[OK] wrote audit to {OUT_DIR}")


if __name__ == "__main__":
    main()
