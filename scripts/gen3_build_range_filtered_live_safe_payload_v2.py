from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path, runtime_path

OUT_DIR = report_path("gen3_range_filtered_live_safe_payload_v2")
RUNTIME_DIR = runtime_path("gen3_range_filtered_live_safe")

SOURCE_PAYLOAD = report_path("gen3_guarded_live_safe_payload_v1", "g3_guarded_live_safe_payload.csv")
SELECTED_TRADES = report_path("gen3_range_filtered_candidate_package_v2", "g3_range_filtered_candidate_closed_trades.csv")
RANGE_SOURCE = report_path("gen3_range_v3_mtm_pressure_v1", "range_v3_weak_low_not_chasing_h5_cost30_closed_trades.csv")

FORBIDDEN_FIELDS = {"policy_net_ret", "stake", "exit_value", "realized_pnl", "policy_exit_date", "exit_price", "exit_date"}


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.normalize()


def _key(df: pd.DataFrame) -> pd.Series:
    return df["route"].astype(str) + "|" + _date(df["entry_date"]).dt.strftime("%Y-%m-%d") + "|" + df["code"].astype(str)


def _augment_missing_range_rows(template: pd.DataFrame, selected: pd.DataFrame, missing_keys: set[str]) -> pd.DataFrame:
    if not missing_keys:
        return pd.DataFrame(columns=template.columns)
    source = pd.read_csv(RANGE_SOURCE, low_memory=False)
    source["entry_date"] = _date(source["entry_date"])
    source["trade_date"] = _date(source["trade_date"])
    source["code"] = source["code"].astype(str)
    source["route"] = "range_gap"
    source["_key"] = _key(source)
    source = source[source["_key"].isin(missing_keys)].copy()
    if source.empty:
        return pd.DataFrame(columns=template.columns)

    selected_keep = selected[["_key", "route_source", "route_priority", "score"]].drop_duplicates("_key", keep="first")
    source = source.merge(selected_keep, on="_key", how="left", suffixes=("", "_selected"))

    rows = pd.DataFrame(columns=template.columns, index=source.index)
    rows["schema_version"] = "g3_range_filtered_live_safe_payload_v2"
    rows["strategy_id"] = "g3_range_filtered_volume5_v2"
    rows["mode"] = "shadow_only_observe"
    rows["route"] = "range_gap"
    rows["route_source"] = source.get("route_source_selected", "range_conservative_combo_b")
    rows["route_priority"] = pd.to_numeric(source.get("route_priority_selected", source.get("route_priority", 1)), errors="coerce").fillna(1).astype(int)
    rows["code"] = source["code"]
    rows["name"] = source.get("name", "")
    rows["signal_date"] = source["trade_date"].dt.strftime("%Y-%m-%d")
    rows["entry_date"] = source["entry_date"].dt.strftime("%Y-%m-%d")
    rows["decision_source_date"] = rows["signal_date"]
    rows["confirm_datetime"] = pd.NA
    rows["entry_price"] = pd.to_numeric(source.get("entry_open"), errors="coerce")
    rows["score"] = pd.to_numeric(source.get("range_v3_score", source.get("candidate_score", 0.0)), errors="coerce")
    rows["source_lineage_key"] = source["_key"]
    pass_rule = source["trade_date"] < source["entry_date"]
    rows["live_visibility_status"] = pass_rule.map(lambda x: "pass_d1_open" if bool(x) else "blocked_trade_date_not_before_entry")
    rows["env_proxy_required"] = False
    rows["ranking_proxy_required"] = False
    rows["live_ready"] = pass_rule.fillna(False)
    rows["shadow_action"] = "observe_only"
    rows["auto_order_allowed"] = False
    rows["formal_buy_signal"] = False
    rows["order_path_enabled"] = False
    rows["block_reason"] = "shadow_only_not_auto_ordered"
    rows["range_filter_version"] = "exclude_adx_downtrend_and_small_positive_gap"
    for col in template.columns:
        if col in rows.columns:
            continue
        if col in source.columns:
            rows[col] = source[col]
    return rows


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    payload = pd.read_csv(SOURCE_PAYLOAD, low_memory=False)
    selected = pd.read_csv(SELECTED_TRADES, low_memory=False)
    payload["entry_date"] = _date(payload["entry_date"])
    selected["entry_date"] = _date(selected["entry_date"])
    payload["code"] = payload["code"].astype(str)
    selected["code"] = selected["code"].astype(str)
    payload["_key"] = _key(payload)
    selected["_key"] = _key(selected)

    selected_keys = set(selected["_key"])
    out = payload[payload["_key"].isin(selected_keys)].copy()
    missing_selected = selected_keys - set(payload["_key"])
    augment = _augment_missing_range_rows(out, selected, missing_selected)
    if not augment.empty:
        out = pd.concat([out, augment.dropna(axis=1, how="all")], ignore_index=True)
    out["schema_version"] = "g3_range_filtered_live_safe_payload_v2"
    out["strategy_id"] = "g3_range_filtered_volume5_v2"
    out["mode"] = "shadow_only_observe"
    out["shadow_action"] = "observe_only"
    out["auto_order_allowed"] = False
    out["formal_buy_signal"] = False
    out["order_path_enabled"] = False
    out["block_reason"] = "shadow_only_not_auto_ordered"
    out["range_filter_version"] = pd.NA
    out.loc[out["route"].eq("range_gap"), "range_filter_version"] = "exclude_adx_downtrend_and_small_positive_gap"
    out["entry_date_sort"] = out["entry_date"]
    out = out.sort_values(["entry_date_sort", "route_priority", "score"], ascending=[True, False, False]).drop(columns=["entry_date_sort", "_key"])
    out["entry_date"] = _date(out["entry_date"]).dt.strftime("%Y-%m-%d")

    forbidden = sorted(FORBIDDEN_FIELDS.intersection(out.columns))
    out["_key"] = _key(out)
    missing_selected = sorted(selected_keys - set(out["_key"]))
    route_summary = []
    for route, g in out.groupby("route", dropna=False):
        route_summary.append(
            {
                "route": route,
                "rows": int(len(g)),
                "live_ready_rows": int(g["live_ready"].fillna(False).sum()),
                "auto_order_allowed_rows": int(g["auto_order_allowed"].fillna(False).sum()),
                "formal_buy_signal_rows": int(g["formal_buy_signal"].fillna(False).sum()),
                "order_path_enabled_rows": int(g["order_path_enabled"].fillna(False).sum()),
                "latest_entry_date": str(pd.to_datetime(g["entry_date"], errors="coerce").max().date()),
            }
        )
    route_df = pd.DataFrame(route_summary)
    audit = pd.DataFrame(
        [
            {
                "payload_rows": int(len(out)),
                "selected_rows": int(len(selected)),
                "matched_rows": int(len(out)),
                "missing_selected_rows": int(len(missing_selected)),
                "forbidden_field_count": int(len(forbidden)),
                "forbidden_fields": ",".join(forbidden),
                "auto_order_allowed_rows": int(out["auto_order_allowed"].fillna(False).sum()),
                "formal_buy_signal_rows": int(out["formal_buy_signal"].fillna(False).sum()),
                "order_path_enabled_rows": int(out["order_path_enabled"].fillna(False).sum()),
                "verdict": "PASS" if not missing_selected and not forbidden else "FAIL",
            }
        ]
    )

    out.to_csv(OUT_DIR / "g3_range_filtered_live_safe_payload.csv", index=False, encoding="utf-8-sig")
    route_df.to_csv(OUT_DIR / "g3_range_filtered_live_safe_summary.csv", index=False, encoding="utf-8-sig")
    audit.to_csv(OUT_DIR / "g3_range_filtered_live_safe_audit.csv", index=False, encoding="utf-8-sig")
    out.to_csv(RUNTIME_DIR / "latest_payload.csv", index=False, encoding="utf-8-sig")
    route_df.to_csv(RUNTIME_DIR / "latest_summary.csv", index=False, encoding="utf-8-sig")

    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "candidate": "g3_range_filtered_volume5_v2",
        "payload_rows": int(len(out)),
        "selected_rows": int(len(selected)),
        "routes": route_summary,
        "missing_selected_rows": int(len(missing_selected)),
        "forbidden_field_count": int(len(forbidden)),
        "auto_order_allowed_rows": int(out["auto_order_allowed"].fillna(False).sum()),
        "formal_buy_signal_rows": int(out["formal_buy_signal"].fillna(False).sum()),
        "order_path_enabled_rows": int(out["order_path_enabled"].fillna(False).sum()),
        "live_readiness": "shadow_only_payload_v2_ready" if not missing_selected and not forbidden else "payload_v2_audit_failed",
        "next_step": "wire_v2_shadow_endpoint_or_compare_with_existing_guarded_shadow",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (RUNTIME_DIR / "latest_summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = f"""# G3 range-filtered live-safe payload V2

生成时间：{meta["generated_at"]}

## 定位

这是 `g3_range_filtered_volume5_v2` 的独立 shadow-only payload。它只从 V1 live-safe payload 中筛出 V2 候选包实际成交样本，不引入收益/退出字段，不打开自动交易。

## 路由汇总

{route_df.to_markdown(index=False)}

## 审计

{audit.to_markdown(index=False)}

## 判断

- `range_gap` 已使用过滤版本：`exclude_adx_downtrend_and_small_positive_gap`。
- `auto_order_allowed/formal_buy_signal/order_path_enabled` 仍全部为 0。
- 该 payload 可用于下一步页面/API shadow 对比，但不能下单。
"""
    (OUT_DIR / "g3_range_filtered_live_safe_payload_report_cn.md").write_text(report, encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
