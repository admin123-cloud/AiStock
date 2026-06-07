from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from gen3_live_safe_schema_v1 import classify


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "reports" / "gen3_combo_panic_strong_execution_stress_v1" / "base_30bps_closed_trades.csv"
DEFAULT_OUT = ROOT / "reports" / "gen3_live_payload_v1"

IDENTITY_FIELDS = {
    "code",
    "name",
    "chain",
    "g3_chain",
    "source_family",
    "entry_date",
    "entry_ts",
    "candidate_key",
}

INTRADAY_FIELDS = {
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

D1_OR_PROXY_FIELDS = {
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
    "candidate_score",
    "chain_rank",
    "entry_weight_hint",
}


def _source_for(field: str, category: str) -> tuple[str, str]:
    if category == "live_ok":
        if field in IDENTITY_FIELDS:
            return "identity", "pass"
        if field in INTRADAY_FIELDS:
            return "intraday_confirm_bar", "pass"
        return "live_safe_static", "pass"
    if category == "execution_only":
        return "execution_layer_only", "research_only"
    if category == "requires_d1_or_intraday_proxy":
        if field in D1_OR_PROXY_FIELDS or field.startswith("Alpha") or field.startswith("score_") or field.startswith("alpha191_"):
            return "requires_d1_snapshot_or_intraday_proxy", "needs_visibility_proof"
        return "requires_visibility_proof", "needs_visibility_proof"
    if category == "forbidden_research_only":
        return "research_future_or_post_trade", "blocked"
    return "unknown", "blocked"


def _dt(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.normalize()


def _validate_dates(payload: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if "entry_date" not in payload.columns:
        rows.append({"check": "entry_date_exists", "status": "FAIL", "detail": "missing entry_date"})
        return pd.DataFrame(rows)
    entry = _dt(payload["entry_date"])
    if "confirm_datetime" in payload.columns:
        confirm = pd.to_datetime(payload["confirm_datetime"], errors="coerce").dt.normalize()
        bad = int(((confirm.notna()) & (confirm > entry)).sum())
        rows.append({"check": "confirm_not_after_entry", "status": "PASS" if bad == 0 else "FAIL", "detail": f"bad_rows={bad}"})
    for field in ["factor_date", "trade_date", "date"]:
        if field in payload.columns:
            d = _dt(payload[field])
            non_null = int(d.notna().sum())
            bad = int(((d.notna()) & (d > entry)).sum())
            same = int(((d.notna()) & (d == entry)).sum())
            rows.append(
                {
                    "check": f"{field}_not_after_entry",
                    "status": "PASS" if bad == 0 else "FAIL",
                    "detail": f"non_null={non_null}, same_day={same}, bad_rows={bad}",
                }
            )
    return pd.DataFrame(rows)


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_无数据_"
    return df.to_markdown(index=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build G3 live-safe payload from research output.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--include-proxy-fields", action="store_true", help="Keep requires_d1_or_intraday_proxy fields with sidecar visibility metadata.")
    args = parser.parse_args()

    input_path = Path(args.input)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(input_path, low_memory=False)
    field_rows = []
    keep_cols = []
    blocked_cols = []
    proxy_cols = []
    execution_cols = []
    for col in df.columns:
        category, reason = classify(col)
        source, status = _source_for(col, category)
        include = category == "live_ok" or (args.include_proxy_fields and category == "requires_d1_or_intraday_proxy")
        if include:
            keep_cols.append(col)
        if category == "forbidden_research_only":
            blocked_cols.append(col)
        if category == "requires_d1_or_intraday_proxy":
            proxy_cols.append(col)
        if category == "execution_only":
            execution_cols.append(col)
        field_rows.append(
            {
                "field": col,
                "category": category,
                "visibility_source": source,
                "field_status": status,
                "included_in_live_payload": include,
                "reason": reason,
            }
        )

    payload = df[keep_cols].copy()
    payload.insert(0, "payload_mode", "live_payload_with_proxy_fields" if args.include_proxy_fields else "live_payload_core_only")
    payload.insert(1, "payload_status", "needs_visibility_proof" if args.include_proxy_fields and proxy_cols else "live_safe_core")
    payload.insert(2, "schema_version", "g3_live_safe_schema_v1")

    date_checks = _validate_dates(payload)
    if not date_checks.empty and date_checks["status"].eq("FAIL").any():
        payload["payload_status"] = "research_only_date_check_failed"

    fields = pd.DataFrame(field_rows)
    counts = fields.groupby(["category", "included_in_live_payload"]).size().reset_index(name="field_count")
    blocked = fields[fields["category"].eq("forbidden_research_only")].copy()
    proxy = fields[fields["category"].eq("requires_d1_or_intraday_proxy")].copy()

    payload.to_csv(out / "g3_live_payload.csv", index=False, encoding="utf-8-sig")
    fields.to_csv(out / "g3_live_payload_field_manifest.csv", index=False, encoding="utf-8-sig")
    counts.to_csv(out / "g3_live_payload_field_counts.csv", index=False, encoding="utf-8-sig")
    blocked.to_csv(out / "g3_live_payload_blocked_fields.csv", index=False, encoding="utf-8-sig")
    proxy.to_csv(out / "g3_live_payload_proxy_required_fields.csv", index=False, encoding="utf-8-sig")
    date_checks.to_csv(out / "g3_live_payload_date_checks.csv", index=False, encoding="utf-8-sig")

    verdict = "PASS_CORE_ONLY"
    if args.include_proxy_fields:
        verdict = "NEEDS_VISIBILITY_PROOF"
    if not date_checks.empty and date_checks["status"].eq("FAIL").any():
        verdict = "FAIL_DATE_CHECK"

    report = f"""# G3 Live Payload 生成器 V1

生成日期：2026-06-01

## 输入

`{input_path}`

## 输出结论

- 输出模式：`{"include_proxy_fields" if args.include_proxy_fields else "core_live_only"}`
- 结论：`{verdict}`
- 输入行数：`{len(df)}`
- 输出行数：`{len(payload)}`
- 输入字段数：`{len(df.columns)}`
- 输出字段数：`{len(payload.columns)}`
- 被阻断黑名单字段：`{len(blocked_cols)}`
- 灰名单字段：`{len(proxy_cols)}`
- 执行层字段：`{len(execution_cols)}`

## 字段处理统计

{_md_table(counts)}

## 日期可见性检查

{_md_table(date_checks)}

## 被阻断字段

{_md_table(blocked[['field', 'visibility_source', 'reason']].head(100))}

## 灰名单字段

{_md_table(proxy[['field', 'visibility_source', 'reason']].head(100))}

## 判断

当前默认输出的是 `core_live_only`，只包含 `live_ok` 字段，不包含未来收益字段，也不包含需要 D-1/盘中代理证明的灰名单字段。

如果后续要把 `v4_rank`、`Alpha*`、`score_*`、市场环境、板块强度等字段放回 live payload，必须使用 `--include-proxy-fields`，并在下游补充真实的 `visibility_source` 证明：

- `d1_snapshot`
- `intraday_proxy`
- `execution_layer`

没有证明的灰名单字段只能用于 research-only。

## 产物

- `g3_live_payload.csv`：默认 live-safe 核心 payload。
- `g3_live_payload_field_manifest.csv`：字段级 manifest。
- `g3_live_payload_blocked_fields.csv`：被硬阻断字段。
- `g3_live_payload_proxy_required_fields.csv`：灰名单字段。
- `g3_live_payload_date_checks.csv`：日期可见性检查。

## 下一步

第52步应做一个小型影子实盘入口：只读取 `g3_live_payload.csv`，按 `payload_status` 决定是否允许展示为 live candidate；任何 research-only 或 needs_visibility_proof 的记录都不能进入自动下单。
"""
    (out / "g3_live_payload_report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(
        {
            "out_dir": str(out),
            "verdict": verdict,
            "rows_in": len(df),
            "rows_out": len(payload),
            "cols_in": len(df.columns),
            "cols_out": len(payload.columns),
            "blocked_fields": len(blocked_cols),
            "proxy_required_fields": len(proxy_cols),
        }
    )


if __name__ == "__main__":
    main()
