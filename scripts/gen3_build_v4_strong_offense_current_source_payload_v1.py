from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path, runtime_path  # noqa: E402

OUT_DIR = report_path("gen3_v4_strong_offense_current_source_payload_v1")
RUNTIME_DIR = runtime_path("gen3_v4_strong_offense_current_source")

SELECTED = report_path("gen3_v4_strong_offense_candidate_v1", "g3_v4_strong_offense_closed_trades.csv")
PANIC_FINAL = report_path("gen3_panic_v2_research", "final_candidate_v1", "m30_close5_full_nextopen_cost30_closed_trades.csv")
PANIC_POOL = report_path("gen3_panic_v2_research", "panic_v2_candidates.csv")
RANGE_POOL = report_path("gen3_range_v3_gap_candidate_source_v1", "range_v3_gap_candidate_source_no_future.csv")
STRONG_PLUSWEAK = report_path("gen3_strong_v2_independent_source_v1", "strong_v2_main_up_plus_weak04_hold5_closed_trades.csv")

FORBIDDEN_PATTERNS = [
    r"^fwd_ret",
    r"^outcome",
    r"^mfe_",
    r"^mae_",
    r"^gross_ret$",
    r"^net_ret$",
    r"^policy_net_ret$",
    r"^baseline_net_ret$",
    r"^exit_ret_net$",
    r"^realized_pnl$",
    r"^exit_value$",
    r"^stake$",
    r"^policy_exit_date$",
    r"^exit_date$",
    r"^exit_price$",
    r"^trigger_close_ret$",
]

BASE_COLUMNS = [
    "schema_version",
    "strategy_id",
    "mode",
    "route",
    "route_source",
    "route_priority",
    "code",
    "name",
    "signal_date",
    "entry_date",
    "decision_source_date",
    "confirm_datetime",
    "entry_price",
    "score",
    "source_lineage_key",
    "current_source_layer",
    "live_visibility_status",
    "env_proxy_required",
    "ranking_proxy_required",
    "live_ready",
    "shadow_action",
    "auto_order_allowed",
    "formal_buy_signal",
    "order_path_enabled",
    "block_reason",
]

EXTRA_COLUMNS = [
    "trade_date",
    "factor_date",
    "g3_chain",
    "market_style",
    "candidate_score",
    "chain_rank",
    "range_v3_score",
    "range_v3_variant",
    "range_v3_family",
    "rank_in_day",
    "entry_open",
    "bar_time",
    "confirm_rule",
    "bar_close_pos",
    "bar_ret",
    "amount_ratio3",
    "market_breadth",
    "g3_strong_score",
    "score_volume5",
    "runup_from_60d_low",
    "l3_s3",
    "l2_s3",
    "l2_rt_rise_ratio",
    "v4_rank",
    "v4_score",
    "source_family",
    "l2_sector_name",
    "l3_sector_name",
    "strong_day_rank",
]


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path, low_memory=False)


def _date(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.normalize()


def _key(df: pd.DataFrame) -> pd.Series:
    return _date(df["entry_date"]).dt.strftime("%Y-%m-%d") + "|" + df["code"].astype(str)


def _forbidden_fields(columns: list[str]) -> list[str]:
    return [col for col in columns if any(re.search(pattern, col) for pattern in FORBIDDEN_PATTERNS)]


def _prep_sources() -> dict[str, pd.DataFrame]:
    sources = {
        "panic_final": _read_csv(PANIC_FINAL),
        "panic_pool": _read_csv(PANIC_POOL),
        "range_pool": _read_csv(RANGE_POOL),
        "strong_plusweak": _read_csv(STRONG_PLUSWEAK),
    }
    for df in sources.values():
        df["code"] = df["code"].astype(str)
        df["_key"] = _key(df)
    return sources


def _empty_payload(index: pd.Index) -> pd.DataFrame:
    out = pd.DataFrame(index=index)
    out["schema_version"] = "g3_v4_strong_offense_current_source_payload_v1"
    out["strategy_id"] = "g3_v4_strong_offense_candidate_v1"
    out["mode"] = "shadow_only_observe"
    out["shadow_action"] = "observe_only"
    out["auto_order_allowed"] = False
    out["formal_buy_signal"] = False
    out["order_path_enabled"] = False
    return out


def _add_common_from_selected(out: pd.DataFrame, selected: pd.DataFrame) -> pd.DataFrame:
    out["route"] = selected["route"].astype(str).values
    out["route_source"] = selected["route_source"].astype(str).values
    out["route_priority"] = pd.to_numeric(selected["route_priority"], errors="coerce").fillna(0).astype(int).values
    out["code"] = selected["code"].astype(str).values
    out["name"] = selected.get("name", "").values
    out["entry_date"] = _date(selected["entry_date"]).dt.strftime("%Y-%m-%d").values
    out["score"] = pd.to_numeric(selected["score"], errors="coerce").values
    out["source_lineage_key"] = out["route"] + "|" + out["entry_date"] + "|" + out["code"]
    for col in [
        "signal_date",
        "decision_source_date",
        "confirm_datetime",
        "entry_price",
        "current_source_layer",
        "live_visibility_status",
        "block_reason",
        *EXTRA_COLUMNS,
    ]:
        if col not in out.columns:
            out[col] = pd.Series(pd.NA, index=out.index, dtype="object")
    for col in ["env_proxy_required", "ranking_proxy_required", "live_ready"]:
        out[col] = pd.Series(False, index=out.index, dtype="object")
    return out


def _first(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=object)
    return df.iloc[0]


def _build_payload() -> tuple[pd.DataFrame, pd.DataFrame]:
    selected = _read_csv(SELECTED)
    selected["entry_date"] = _date(selected["entry_date"])
    selected["code"] = selected["code"].astype(str)
    selected["_key"] = _key(selected)
    sources = _prep_sources()

    payload = _add_common_from_selected(_empty_payload(selected.index), selected)
    audit_rows: list[dict[str, Any]] = []

    for idx, row in selected.iterrows():
        route = str(row["route"])
        key = str(row["_key"])
        source_row = pd.Series(dtype=object)
        source_layer = ""

        if route == "down_panic":
            final_hit = sources["panic_final"][sources["panic_final"]["_key"].eq(key)]
            pool_hit = sources["panic_pool"][sources["panic_pool"]["_key"].eq(key)]
            source_row = _first(final_hit if not final_hit.empty else pool_hit)
            source_layer = "panic_final_intraday_confirm" if not final_hit.empty else "panic_daily_candidate_pool"
            trade_date = _first(pool_hit).get("trade_date", "") if not pool_hit.empty else ""
            payload.loc[idx, "signal_date"] = pd.Timestamp(trade_date).strftime("%Y-%m-%d") if trade_date else payload.loc[idx, "entry_date"]
            payload.loc[idx, "decision_source_date"] = payload.loc[idx, "signal_date"]
            payload.loc[idx, "confirm_datetime"] = source_row.get("confirm_datetime", "")
            payload.loc[idx, "entry_price"] = pd.to_numeric(source_row.get("entry_price_adjusted", source_row.get("entry_price", pd.NA)), errors="coerce")
            payload.loc[idx, "current_source_layer"] = source_layer
            ready = bool(str(payload.loc[idx, "confirm_datetime"] or ""))
            payload.loc[idx, "live_visibility_status"] = "pass_panic_daily_pool_and_intraday_confirm" if ready else "pending_panic_intraday_confirm"
            payload.loc[idx, "env_proxy_required"] = not ready
            payload.loc[idx, "ranking_proxy_required"] = False
            payload.loc[idx, "live_ready"] = ready
            payload.loc[idx, "block_reason"] = "shadow_only_not_auto_ordered" if ready else "panic_intraday_confirm_missing"
        elif route == "range_gap":
            hit = sources["range_pool"][
                sources["range_pool"]["_key"].eq(key)
                & sources["range_pool"]["range_v3_variant"].astype(str).eq("range_v3_weak_low_not_chasing_h10")
            ]
            source_row = _first(hit)
            payload.loc[idx, "signal_date"] = pd.Timestamp(source_row.get("trade_date")).strftime("%Y-%m-%d")
            payload.loc[idx, "decision_source_date"] = payload.loc[idx, "signal_date"]
            payload.loc[idx, "confirm_datetime"] = pd.NA
            payload.loc[idx, "entry_price"] = pd.to_numeric(source_row.get("entry_open", pd.NA), errors="coerce")
            payload.loc[idx, "current_source_layer"] = "range_no_future_candidate_pool"
            payload.loc[idx, "live_visibility_status"] = "pass_range_d1_pool_next_open_shadow_only"
            payload.loc[idx, "env_proxy_required"] = False
            payload.loc[idx, "ranking_proxy_required"] = False
            payload.loc[idx, "live_ready"] = True
            payload.loc[idx, "block_reason"] = "shadow_only_not_auto_ordered"
        elif route == "strong_main":
            hit = sources["strong_plusweak"][sources["strong_plusweak"]["_key"].eq(key)]
            source_row = _first(hit)
            payload.loc[idx, "signal_date"] = pd.Timestamp(source_row.get("trade_date")).strftime("%Y-%m-%d")
            payload.loc[idx, "decision_source_date"] = pd.Timestamp(source_row.get("factor_date")).strftime("%Y-%m-%d")
            payload.loc[idx, "confirm_datetime"] = source_row.get("confirm_datetime", "")
            payload.loc[idx, "entry_price"] = pd.to_numeric(source_row.get("entry_price", pd.NA), errors="coerce")
            payload.loc[idx, "current_source_layer"] = "strong_v2_plusweak_intraday_confirm_source"
            ready = bool(str(payload.loc[idx, "confirm_datetime"] or ""))
            payload.loc[idx, "live_visibility_status"] = "pass_strong_d1_rank_and_intraday_confirm" if ready else "pending_strong_intraday_confirm"
            payload.loc[idx, "env_proxy_required"] = False
            payload.loc[idx, "ranking_proxy_required"] = not ready
            payload.loc[idx, "live_ready"] = ready
            payload.loc[idx, "block_reason"] = "shadow_only_not_auto_ordered" if ready else "strong_intraday_confirm_missing"
        else:
            payload.loc[idx, "current_source_layer"] = "unknown"
            payload.loc[idx, "live_visibility_status"] = "unknown_route"
            payload.loc[idx, "env_proxy_required"] = True
            payload.loc[idx, "ranking_proxy_required"] = True
            payload.loc[idx, "live_ready"] = False
            payload.loc[idx, "block_reason"] = "unknown_route"

        for col in EXTRA_COLUMNS:
            if col in payload.columns and pd.notna(payload.loc[idx, col]):
                continue
            if col in source_row.index:
                payload.loc[idx, col] = source_row.get(col)
            elif col in selected.columns:
                payload.loc[idx, col] = row.get(col)

        audit_rows.append(
            {
                "source_lineage_key": payload.loc[idx, "source_lineage_key"],
                "route": route,
                "current_source_layer": payload.loc[idx, "current_source_layer"],
                "entry_price_selected": row.get("entry_price"),
                "entry_price_payload": payload.loc[idx, "entry_price"],
                "entry_price_delta": (
                    float(payload.loc[idx, "entry_price"]) - float(row.get("entry_price"))
                    if pd.notna(payload.loc[idx, "entry_price"]) and pd.notna(row.get("entry_price"))
                    else None
                ),
                "score_selected": row.get("score"),
                "score_payload": payload.loc[idx, "score"],
                "live_ready": bool(payload.loc[idx, "live_ready"]),
            }
        )

    payload = payload[BASE_COLUMNS + [col for col in EXTRA_COLUMNS if col in payload.columns]]
    forbidden = _forbidden_fields(list(payload.columns))
    if forbidden:
        raise RuntimeError(f"forbidden fields leaked into current-source payload: {forbidden}")
    payload = payload.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False]).reset_index(drop=True)
    return payload, pd.DataFrame(audit_rows)


def _summarize(payload: pd.DataFrame, audit: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    route_rows = []
    for route, g in payload.groupby("route", dropna=False):
        route_rows.append(
            {
                "route": route,
                "rows": int(len(g)),
                "live_ready_rows": int(g["live_ready"].fillna(False).sum()),
                "latest_entry_date": str(pd.to_datetime(g["entry_date"], errors="coerce").max().date()),
            }
        )
    price = audit.copy()
    price["entry_price_delta_abs"] = pd.to_numeric(price["entry_price_delta"], errors="coerce").abs()
    consistency = pd.DataFrame(
        [
            {
                "payload_rows": int(len(payload)),
                "live_ready_rows": int(payload["live_ready"].fillna(False).sum()),
                "auto_order_allowed_rows": int(payload["auto_order_allowed"].fillna(False).sum()),
                "formal_buy_signal_rows": int(payload["formal_buy_signal"].fillna(False).sum()),
                "order_path_enabled_rows": int(payload["order_path_enabled"].fillna(False).sum()),
                "max_entry_price_delta_abs": float(price["entry_price_delta_abs"].max()) if not price.empty else 0.0,
                "entry_price_mismatch_rows": int(price["entry_price_delta_abs"].gt(1e-6).sum()) if not price.empty else 0,
            }
        ]
    )
    return pd.DataFrame(route_rows), consistency


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_无数据_"
    return df.astype(object).where(pd.notna(df), "").to_markdown(index=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    payload, audit = _build_payload()
    route_summary, consistency = _summarize(payload, audit)

    payload.to_csv(OUT_DIR / "g3_v4_strong_offense_current_source_payload.csv", index=False, encoding="utf-8-sig")
    audit.to_csv(OUT_DIR / "g3_v4_strong_offense_current_source_consistency.csv", index=False, encoding="utf-8-sig")
    route_summary.to_csv(OUT_DIR / "g3_v4_strong_offense_current_source_route_summary.csv", index=False, encoding="utf-8-sig")
    consistency.to_csv(OUT_DIR / "g3_v4_strong_offense_current_source_summary.csv", index=False, encoding="utf-8-sig")
    payload.to_csv(RUNTIME_DIR / "latest_payload.csv", index=False, encoding="utf-8-sig")
    route_summary.to_csv(RUNTIME_DIR / "latest_route_summary.csv", index=False, encoding="utf-8-sig")

    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "candidate": "g3_v4_strong_offense_candidate_v1",
        "schema_version": "g3_v4_strong_offense_current_source_payload_v1",
        "payload_rows": int(len(payload)),
        "routes": route_summary.to_dict(orient="records"),
        "summary": consistency.iloc[0].to_dict(),
        "research_only": True,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "live_readiness": "shadow_only_current_source_payload_ready",
        "next_step": "wire this payload into shadow endpoint after endpoint refresh policy is updated.",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (RUNTIME_DIR / "latest_summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = f"""# G3 V4 强进攻 current-source payload V1

生成时间：{meta["generated_at"]}

## 定位

这是从上游候选源重建的 sanitized shadow-only payload，不从 closed trades 直接暴露收益/退出/资金字段。

## 路由摘要

{_md_table(route_summary)}

## 一致性摘要

{_md_table(consistency)}

## 纪律

- `auto_order_allowed=false`
- `formal_buy_signal=false`
- `order_path_enabled=false`
- 当前仍是 research-only，只能作为 G3 进攻性观察源。
"""
    (OUT_DIR / "g3_v4_strong_offense_current_source_payload_report_cn.md").write_text(report, encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
