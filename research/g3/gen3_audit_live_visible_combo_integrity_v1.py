from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = _PROJECT_ROOT
LIVE_PAYLOAD = _report_path() / "gen3_guarded_live_safe_payload_v1" / "g3_guarded_live_safe_payload.csv"
CLOSED_TRADES = _report_path() / "gen3_guarded_candidate_package_v1" / "g3_guarded_candidate_closed_trades.csv"
PACKAGE_SUMMARY = _report_path() / "gen3_guarded_candidate_package_v1" / "summary.json"
OUT_DIR = _report_path() / "gen3_live_visible_combo_integrity_audit_v1"

FORBIDDEN_PAYLOAD_FIELDS = {"policy_net_ret", "stake", "exit_value", "realized_pnl", "policy_exit_date", "exit_price", "exit_date"}


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.normalize()


def _key(df: pd.DataFrame) -> pd.Series:
    return df["route"].astype(str) + "|" + _date(df["entry_date"]).dt.strftime("%Y-%m-%d") + "|" + df["code"].astype(str)


def _metrics(df: pd.DataFrame, label: str) -> dict:
    r = pd.to_numeric(df.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    return {
        "scope": label,
        "rows": int(len(df)),
        "unique_dates": int(df["entry_date"].nunique()) if not df.empty else 0,
        "win_rate": float((r > 0).mean()) if len(r) else 0.0,
        "avg_policy_net_ret": float(r.mean()) if len(r) else 0.0,
        "sum_policy_net_ret": float(r.sum()) if len(r) else 0.0,
        "worst_trade": float(r.min()) if len(r) else 0.0,
    }


def _fmt_pct(v: float) -> str:
    if pd.isna(v):
        return ""
    return f"{v:.2%}"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    live = pd.read_csv(LIVE_PAYLOAD, low_memory=False)
    closed = pd.read_csv(CLOSED_TRADES, low_memory=False)
    live["entry_date"] = _date(live["entry_date"])
    closed["entry_date"] = _date(closed["entry_date"])
    live["code"] = live["code"].astype(str)
    closed["code"] = closed["code"].astype(str)
    live["_key"] = _key(live)
    closed["_key"] = _key(closed)

    d = closed.merge(
        live[
            [
                "_key",
                "live_ready",
                "env_proxy_required",
                "ranking_proxy_required",
                "auto_order_allowed",
                "formal_buy_signal",
                "order_path_enabled",
                "signal_date",
                "decision_source_date",
                "confirm_datetime",
            ]
        ],
        on="_key",
        how="left",
        indicator=True,
    )

    route_rows = []
    for route, g in d.groupby("route", dropna=False):
        row = _metrics(g, route)
        row.update(
            {
                "matched_payload_rows": int(g["_merge"].eq("both").sum()),
                "live_ready_rows": int(g["live_ready"].fillna(False).sum()),
                "blocked_rows": int((~g["live_ready"].fillna(False)).sum()),
                "auto_order_allowed_rows": int(g["auto_order_allowed"].fillna(False).sum()),
                "formal_buy_signal_rows": int(g["formal_buy_signal"].fillna(False).sum()),
                "order_path_enabled_rows": int(g["order_path_enabled"].fillna(False).sum()),
            }
        )
        route_rows.append(row)

    route_metrics = pd.DataFrame(route_rows)
    overall = pd.DataFrame(
        [
            {
                "scope": "payload_columns",
                "payload_rows": int(len(live)),
                "closed_rows": int(len(closed)),
                "matched_rows": int(d["_merge"].eq("both").sum()),
                "missing_payload_rows": int(d["_merge"].ne("both").sum()),
                "live_ready_rows": int(live["live_ready"].fillna(False).sum()),
                "env_proxy_required_rows": int(live["env_proxy_required"].fillna(False).sum()),
                "ranking_proxy_required_rows": int(live["ranking_proxy_required"].fillna(False).sum()),
                "auto_order_allowed_rows": int(live["auto_order_allowed"].fillna(False).sum()),
                "formal_buy_signal_rows": int(live["formal_buy_signal"].fillna(False).sum()),
                "order_path_enabled_rows": int(live["order_path_enabled"].fillna(False).sum()),
                "forbidden_payload_fields": ",".join(sorted(FORBIDDEN_PAYLOAD_FIELDS.intersection(live.columns))),
            }
        ]
    )

    package = json.loads(PACKAGE_SUMMARY.read_text(encoding="utf-8")) if PACKAGE_SUMMARY.exists() else {}
    package_cmp = pd.DataFrame(
        [
            {
                "metric": "main_30bps_total_return",
                "package_value": package.get("main_30bps", {}).get("total_return"),
            },
            {
                "metric": "main_30bps_max_drawdown",
                "package_value": package.get("main_30bps", {}).get("max_drawdown"),
            },
            {
                "metric": "close_100bps_total_return",
                "package_value": package.get("close_100bps", {}).get("total_return"),
            },
            {
                "metric": "nextopen_limitdown_30bps_total_return",
                "package_value": package.get("nextopen_limitdown_30bps", {}).get("total_return"),
            },
        ]
    )

    d.to_csv(OUT_DIR / "live_visible_combo_joined_trades.csv", index=False, encoding="utf-8-sig")
    overall.to_csv(OUT_DIR / "live_visible_combo_overall_audit.csv", index=False, encoding="utf-8-sig")
    route_metrics.to_csv(OUT_DIR / "live_visible_combo_route_metrics.csv", index=False, encoding="utf-8-sig")
    package_cmp.to_csv(OUT_DIR / "live_visible_combo_package_reference.csv", index=False, encoding="utf-8-sig")

    display_routes = route_metrics.copy()
    for c in ["win_rate", "avg_policy_net_ret", "sum_policy_net_ret", "worst_trade"]:
        display_routes[c] = display_routes[c].map(_fmt_pct)
    display_pkg = package_cmp.copy()
    display_pkg["package_value"] = display_pkg["package_value"].map(lambda x: _fmt_pct(float(x)) if pd.notna(x) else "")

    verdict = (
        int(overall.iloc[0]["missing_payload_rows"]) == 0
        and int(overall.iloc[0]["live_ready_rows"]) == len(live)
        and int(overall.iloc[0]["auto_order_allowed_rows"]) == 0
        and not overall.iloc[0]["forbidden_payload_fields"]
    )
    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "payload_rows": int(len(live)),
        "closed_rows": int(len(closed)),
        "matched_rows": int(d["_merge"].eq("both").sum()),
        "live_ready_rows": int(live["live_ready"].fillna(False).sum()),
        "auto_order_allowed_rows": int(live["auto_order_allowed"].fillna(False).sum()),
        "forbidden_payload_fields": overall.iloc[0]["forbidden_payload_fields"],
        "verdict": "PASS" if verdict else "FAIL",
        "next_step": "run_year_window_and_execution_stress_review_under_live_visible_payload",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = f"""# G3 live-visible 组合完整性审计 V1

生成时间：{meta["generated_at"]}

## 目的

确认三条链路升级为 live-visible 后，页面/影子盘 payload 与研究 closed_trades 的组合样本仍一一对应，同时不放开自动交易。

## 总体审计

{overall.to_markdown(index=False)}

## 分链路研究收益引用

这些收益只来自研究 closed_trades，用于确认样本没有断链；live-safe payload 本身不包含收益字段。

{display_routes.to_markdown(index=False)}

## 候选包参考指标

{display_pkg.to_markdown(index=False)}

## 判断

审计结论：`{meta["verdict"]}`。

- 若为 PASS：说明三条链路在 live-safe 里均可见，且没有打开自动交易。
- 这仍不是实盘接入结论；下一步必须回到年度/窗口稳定性和执行压力，尤其检查 100bps、次日开盘、跌停延迟和极端低开冲击。
"""
    (OUT_DIR / "live_visible_combo_integrity_report_cn.md").write_text(report, encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
