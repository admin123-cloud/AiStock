from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path  # noqa: E402
DEFAULT_INPUT = report_path("gen3_market_state_router_strategy_v1", "g3_route_execution_mandate_candidate_closed_trades.csv")
DEFAULT_OUT = ROOT / "reports" / "gen3_live_safe_schema_v1"


FORBIDDEN_PATTERNS = [
    r"^fwd_ret",
    r"^outcome",
    r"^mfe_",
    r"^mae_",
    r"^exit_close_",
    r"^exit_date_h",
    r"^h\d+$",
    r"^gross_ret$",
    r"^net_ret$",
    r"^baseline_net_ret$",
    r"^policy_net_ret$",
    r"^fixed_net_ret$",
    r"^exit_ret_net$",
    r"^realized_pnl$",
    r"^exit_value$",
    r"^first_leg_ret$",
    r"^second_leg_ret$",
    r"^stop5_touch",
    r"^user_v2_pass$",
    r"^user_v2_reject_reason$",
    r"^daily_entry_close$",
    r"^price_scale_daily_to_minute$",
    r"^trade_idx$",
    r"^entry_start_idx$",
    r"^search_end_idx$",
]

LIVE_OK_FIELDS = {
    "code",
    "name",
    "chain",
    "g3_chain",
    "source_family",
    "entry_date",
    "entry_ts",
    "confirm_datetime",
    "bar_time",
    "confirm_open",
    "confirm_high",
    "confirm_low",
    "confirm_amount",
    "bar_close_pos",
    "bar_ret",
    "amount_ratio3",
    "minute_day_close",
    "entry_price",
    "entry_price_adjusted",
    "confirm_rule",
    "policy",
    "layer_policy",
    "profile",
    "strategy_id",
    "route",
    "route_label",
    "execution_variant",
    "execution_note",
    "candidate_key",
    "realtime_pool",
    "ctx_realtime_pool",
    "intraday_normal_datetime",
    "intraday_period",
    "intraday_state_note",
    "intraday_normal_mode",
    "rt_return_from_d1_close",
    "rt_confirm_vs_ma5",
    "rt_confirm_vs_ma10",
    "rt_fractal_rebound",
    "rt_30m_amount_ratio",
    "rt_confirm_hour",
    "volume_expand",
    "trigger_type",
    "pattern",
    "fractal_datetime",
    "fractal_low",
    "fractal_close",
    "confirm_ma5",
    "confirm_ma10",
    "confirm_amount_ma5_prev",
}

EXECUTION_ONLY_FIELDS = {
    "triggered",
    "executable",
    "exit_source",
    "exit_reason_proxy",
    "early_exit_fraction",
    "trigger_datetime",
    "trigger_close_ret",
    "exit_datetime",
    "exit_price",
    "policy_exit_date",
    "status",
    "stake",
    "exit_date",
    "first_leg_weight",
    "first_leg_date",
    "second_leg_weight",
    "second_leg_date",
}

REQUIRES_D1_OR_PROXY = {
    "trade_date",
    "factor_date",
    "date",
    "v4_rank",
    "v4_score",
    "entry_pass",
    "in_score_pool",
    "rank_change_status",
    "previous_signal_date",
    "previous_rank",
    "rank_change",
    "score_change",
    "pool_streak",
    "close",
    "mom5",
    "mom10",
    "vol_ratio",
    "amt20",
    "vol10",
    "r_mom5",
    "r_mom10",
    "r_vol_ratio",
    "r_amt20",
    "r_vol10_low",
    "index_close",
    "index_ma20",
    "index_ma60",
    "market_breadth",
    "index_close_ge_ma20",
    "legacy_regime",
    "market_style",
    "ma_skeleton",
    "volume_price_layer",
    "adx_layer",
    "breadth_ma20",
    "breadth_ma60",
    "up_rate",
    "big_down_rate",
    "limit_down_proxy_rate",
    "market_amount_ratio20",
    "adx20",
    "index_mom20",
    "mom20",
    "drawdown10",
    "drawdown20",
    "runup_from_60d_low",
    "range_pos20",
    "range_pos60",
    "amount20",
    "amount_ratio20",
    "turnover_rate",
    "lower_shadow_ratio",
    "close_position",
    "gap_open",
    "industry",
    "industry_top",
    "float_share",
    "total_share",
    "float_market_cap_yi",
    "float_mcap_proxy",
    "cap_bucket",
    "float_mcap_bucket",
    "cap_pressure_lookback_days",
    "cap_pressure_days",
    "cap_big_pressure_days",
    "cap_pressure_amount_share",
    "overhead_pressure_days",
    "overhead_big_pressure_days",
    "overhead_pressure_amount_share",
    "prior_max_high_ratio",
    "recent60_return",
    "cap_pressure_reject",
    "downtrend_rebound_reject",
    "downtrend_rebound_reason",
    "downtrend_rebound_into_pressure",
    "share_cap_missing",
    "g3_position_guard",
    "g3_capitulation_strength",
    "g3_volume_context",
    "g3_repair_env_label",
    "base_signal_count",
    "day_breadth_ma20",
    "day_index_mom20",
    "day_market_amount_ratio20",
    "day_up_rate",
    "g2_open_state",
    "g3_market_style",
    "year",
    "up_rate_bucket",
    "big_down_bucket",
    "range_pos60_bucket",
    "drawdown20_bucket",
    "liquidity_amount20_bucket",
    "entry_start_date",
    "search_end_date",
    "is_same_day_target",
    "candidate_score",
    "chain_rank",
    "entry_weight_hint",
    "v4_rank_raw",
    "v4_score_raw",
    "sector_strong",
    "sector_score_bonus",
    "g2_v2_family_priority",
    "g2_v2_buy_logic",
    "volume_ratio",
    "candidate_profile",
    "trigger_profile",
    "setup_type",
    "box_top",
    "box_low",
    "box_range",
    "breakout_pct",
    "big_bull_lag_days",
    "close_vs_prior20_high",
    "close_vs_prior60_high",
    "setup_big_bull_rebreak_2_5d_top",
    "setup_big_bull_rebreak_2_5d_low",
    "setup_big_bull_rebreak_2_5d_range",
    "rt_breakout_vs_box_top",
    "signal_family",
    "l1_sector_code",
    "l1_sector_name",
    "l1_sector_stock_count",
    "l1_rt_member_bars",
    "l1_rt_avg_return",
    "l1_rt_rise_ratio",
    "l1_rt_strong3_ratio",
    "l1_rt_strong5_ratio",
    "l1_rt_top_return",
    "l2_sector_code",
    "l2_sector_name",
    "l2_sector_stock_count",
    "l2_rt_member_bars",
    "l2_rt_avg_return",
    "l2_rt_rise_ratio",
    "l2_rt_strong3_ratio",
    "l2_rt_strong5_ratio",
    "l2_rt_top_return",
    "l3_sector_code",
    "l3_sector_name",
    "l3_sector_stock_count",
    "l3_rt_member_bars",
    "l3_rt_avg_return",
    "l3_rt_rise_ratio",
    "l3_rt_strong3_ratio",
    "l3_rt_strong5_ratio",
    "l3_rt_top_return",
    "l3_s3",
    "l2_s3",
    "l3_rise",
}


def _is_alpha_or_score(col: str) -> bool:
    return bool(
        re.match(r"^Alpha\d+", col)
        or re.match(r"^score_", col)
        or col.startswith("alpha191_")
    )


def _is_forbidden(col: str) -> bool:
    return any(re.search(pattern, col) for pattern in FORBIDDEN_PATTERNS)


def classify(col: str) -> tuple[str, str]:
    if _is_forbidden(col):
        return "forbidden_research_only", "未来收益、成交后结果或研究标签，禁止进入实盘候选和排序过滤。"
    if col in LIVE_OK_FIELDS:
        return "live_ok", "盘中确认时点可见，或为基础标识字段。"
    if col in EXECUTION_ONLY_FIELDS:
        return "execution_only", "只允许在持仓/卖出执行层出现，不应参与买入排序过滤。"
    if col in REQUIRES_D1_OR_PROXY or _is_alpha_or_score(col):
        return "requires_d1_or_intraday_proxy", "必须证明来自 D-1 快照或截至 confirm bar 的盘中代理。"
    return "unknown_requires_review", "未纳入白名单，接入实盘前必须人工复核。"


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_无数据_"
    return df.to_markdown(index=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and validate G3 live-safe schema.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    input_path = Path(args.input)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(input_path, nrows=2000, low_memory=False)
    schema_rows = []
    for col in df.columns:
        category, reason = classify(col)
        non_null = int(df[col].notna().sum())
        schema_rows.append(
            {
                "field": col,
                "category": category,
                "reason": reason,
                "sample_non_null_rows": non_null,
            }
        )
    schema = pd.DataFrame(schema_rows)
    counts = schema.groupby("category").size().reset_index(name="field_count").sort_values("category")

    forbidden = schema[schema["category"].eq("forbidden_research_only")].copy()
    unknown = schema[schema["category"].eq("unknown_requires_review")].copy()
    live_projection_cols = schema[
        schema["category"].isin(["live_ok", "requires_d1_or_intraday_proxy", "execution_only"])
    ]["field"].tolist()
    projection = df[live_projection_cols].copy()

    schema.to_csv(out / "g3_live_safe_field_schema.csv", index=False, encoding="utf-8-sig")
    counts.to_csv(out / "g3_live_safe_category_counts.csv", index=False, encoding="utf-8-sig")
    forbidden.to_csv(out / "g3_live_forbidden_fields.csv", index=False, encoding="utf-8-sig")
    unknown.to_csv(out / "g3_live_unknown_review_fields.csv", index=False, encoding="utf-8-sig")
    projection.to_csv(out / "g3_live_safe_projection_sample.csv", index=False, encoding="utf-8-sig")

    live_payload_status = "PASS" if forbidden.empty and unknown.empty else "FAIL"
    filtered_status = "PASS_WITH_PROXY_REQUIREMENTS" if unknown.empty else "NEEDS_REVIEW"

    report = f"""# G3 Live-Safe 字段白名单/黑名单 V1

生成日期：2026-06-01

## 目的

这份 schema 用来防止 G3 研究文件里的未来收益、成交后结果、复盘标签误接入实盘候选池。

审计输入：

`{input_path}`

## 总结论

- 原始研究明细作为 live payload：`{live_payload_status}`
- 过滤黑名单后的投影样本：`{filtered_status}`

这不是说研究文件有问题，而是说研究文件不能原样接入实盘。正式接入页面、影子实盘或自动下单前，必须按本 schema 做字段过滤。

## 字段分类统计

{_md_table(counts)}

## 分类定义

- `live_ok`：可直接进入实盘候选 payload 的字段，主要是代码、名称、确认时间、30m bar 内已知信息、入场价等。
- `requires_d1_or_intraday_proxy`：可以保留，但必须证明来自 D-1 快照，或截至 `confirm_datetime` 的盘中代理重算。
- `execution_only`：只允许出现在持仓、卖出和执行回报层，不允许参与买入排序过滤。
- `forbidden_research_only`：只能用于训练、收益评估、失败复盘，禁止进入实盘候选。
- `unknown_requires_review`：还没有归类，不能上线。

## 禁止上线字段

{_md_table(forbidden[['field', 'category', 'reason']].head(80))}

## 待人工复核字段

{_md_table(unknown[['field', 'category', 'reason']].head(80))}

## 白名单原则

### Panic

允许直接进入实盘候选：

- `code`
- `name`
- `g3_chain`
- `entry_date`
- `confirm_datetime`
- `bar_time`
- `confirm_open/high/low/amount`
- `bar_close_pos`
- `bar_ret`
- `amount_ratio3`
- `entry_price`
- `entry_price_adjusted`
- `confirm_rule`

必须 D-1 或盘中代理：

- `breadth_ma20`
- `up_rate`
- `big_down_rate`
- `index_mom20`
- `market_amount_ratio20`
- `market_style`
- `ma_skeleton`
- `volume_price_layer`
- `adx_layer`
- `candidate_score`
- `chain_rank`

### Strong

允许直接进入实盘候选：

- `code`
- `name`
- `chain`
- `entry_date`
- `confirm_datetime`
- `intraday_normal_datetime`
- `intraday_period`
- `rt_return_from_d1_close`
- `rt_confirm_vs_ma5`
- `rt_confirm_vs_ma10`
- `rt_fractal_rebound`
- `rt_30m_amount_ratio`
- `trigger_type`
- `pattern`
- `confirm_ma5`
- `confirm_ma10`
- `volume_expand`

必须 D-1 或盘中代理：

- `factor_date`
- `v4_rank`
- `v4_score`
- `Alpha*`
- `score_*`
- `source_family`
- `sector_*`
- `l1/l2/l3_rt_*`
- `g3_market_style`

## 硬黑名单规则

字段名命中以下模式时，默认禁止进入实盘：

```text
{chr(10).join(FORBIDDEN_PATTERNS)}
```

## 产物

- `g3_live_safe_field_schema.csv`：完整字段分类。
- `g3_live_safe_category_counts.csv`：分类统计。
- `g3_live_forbidden_fields.csv`：黑名单字段。
- `g3_live_unknown_review_fields.csv`：待人工复核字段。
- `g3_live_safe_projection_sample.csv`：去掉黑名单字段后的样本投影。

## 下一步

第51步应把这个 schema 检查接入 G3 候选生成流程：

1. 候选生成后先跑字段检查。
2. 若出现 `forbidden_research_only` 字段进入 live payload，直接阻断。
3. 若出现 `unknown_requires_review` 字段，降级为 research-only。
4. 若出现 `requires_d1_or_intraday_proxy` 字段，必须附带 `visibility_source`，标明 `d1_snapshot` 或 `intraday_proxy`。
"""
    (out / "g3_live_safe_schema_report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(
        {
            "out_dir": str(out),
            "live_payload_status": live_payload_status,
            "filtered_status": filtered_status,
            "categories": counts.to_dict(orient="records"),
            "forbidden_fields": len(forbidden),
            "unknown_fields": len(unknown),
        }
    )


if __name__ == "__main__":
    main()
