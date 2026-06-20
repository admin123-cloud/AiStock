from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path, runtime_path  # noqa: E402

OUT_DIR = report_path("gen3_v4_strong_offense_freshness_audit_v1")

SOURCES = {
    "current_source_payload": runtime_path("gen3_v4_strong_offense_current_source", "latest_payload.csv"),
    "shadow_payload": report_path("gen3_v4_strong_offense_shadow_only_v1", "g3_v4_strong_offense_shadow_payload.csv"),
    "panic_pool": report_path("gen3_panic_v2_research", "panic_v2_candidates.csv"),
    "panic_final": report_path("gen3_panic_v2_research", "final_candidate_v1", "m30_close5_full_nextopen_cost30_closed_trades.csv"),
    "range_pool": report_path("gen3_range_v3_gap_candidate_source_v1", "range_v3_gap_candidate_source_no_future.csv"),
    "strong_plusweak": report_path("gen3_strong_v2_independent_source_v1", "strong_v2_main_up_plus_weak04_hold5_closed_trades.csv"),
}

INDEX_CODE = "999999.SH"


def _date_span(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "rows": 0,
            "entry_date_min": "",
            "entry_date_max": "",
            "trade_date_min": "",
            "trade_date_max": "",
        }
    try:
        df = pd.read_csv(path, low_memory=False)
    except pd.errors.EmptyDataError:
        return {
            "exists": True,
            "rows": 0,
            "entry_date_min": "",
            "entry_date_max": "",
            "trade_date_min": "",
            "trade_date_max": "",
        }
    out = {"exists": True, "rows": int(len(df))}
    for col in ["entry_date", "trade_date"]:
        if col in df.columns and not df.empty:
            dates = pd.to_datetime(df[col], errors="coerce")
            out[f"{col}_min"] = "" if dates.dropna().empty else dates.min().strftime("%Y-%m-%d")
            out[f"{col}_max"] = "" if dates.dropna().empty else dates.max().strftime("%Y-%m-%d")
        else:
            out[f"{col}_min"] = ""
            out[f"{col}_max"] = ""
    return out


def _latest_index_date() -> str:
    try:
        df = clickhouse_query_df(
            """
            SELECT max(trade_date) AS trade_date
            FROM kline_daily
            WHERE code = %(index_code)s
            """,
            {"index_code": INDEX_CODE},
        )
    except Exception:
        return ""
    if df.empty or pd.isna(df.iloc[0].get("trade_date")):
        return ""
    return pd.Timestamp(df.iloc[0]["trade_date"]).strftime("%Y-%m-%d")


def _days_lag(max_date: str, ref_date: str) -> int | None:
    if not max_date or not ref_date:
        return None
    return int((pd.Timestamp(ref_date) - pd.Timestamp(max_date)).days)


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_无数据_"
    return df.astype(object).where(pd.notna(df), "").to_markdown(index=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    latest_index_date = _latest_index_date()
    today = pd.Timestamp(datetime.now()).strftime("%Y-%m-%d")
    reference_date = latest_index_date or today

    rows = []
    for name, path in SOURCES.items():
        span = _date_span(path)
        entry_max = str(span.get("entry_date_max") or "")
        trade_max = str(span.get("trade_date_max") or "")
        source_max = max([d for d in [entry_max, trade_max] if d] or [""])
        lag_days = _days_lag(source_max, reference_date)
        if not span["exists"]:
            status = "MISSING"
        elif lag_days is None:
            status = "NO_DATE"
        elif lag_days <= 1:
            status = "FRESH_OR_NEAR"
        elif lag_days <= 5:
            status = "STALE_SHORT"
        else:
            status = "STALE"
        rows.append(
            {
                "source": name,
                "path": str(path),
                **span,
                "source_max_date": source_max,
                "reference_date": reference_date,
                "lag_days": lag_days,
                "freshness_status": status,
            }
        )
    source_df = pd.DataFrame(rows)

    current_row = source_df[source_df["source"].eq("current_source_payload")]
    raw_current_lag = None if current_row.empty else current_row.iloc[0].get("lag_days")
    current_lag = None if raw_current_lag is None or pd.isna(raw_current_lag) else int(raw_current_lag)
    current_status = "" if current_row.empty else str(current_row.iloc[0].get("freshness_status"))
    verdict = "PASS_FRESHNESS" if current_status == "FRESH_OR_NEAR" else "NOT_PASS_STALE_SOURCE"
    blocker = (
        "current_source_payload is stale versus latest local index daily date"
        if verdict != "PASS_FRESHNESS"
        else ""
    )
    summary = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "audit": "gen3_v4_strong_offense_freshness_audit_v1",
        "today": today,
        "latest_index_date": latest_index_date,
        "reference_date": reference_date,
        "current_source_lag_days": current_lag,
        "current_source_status": current_status,
        "verdict": verdict,
        "blocker": blocker,
        "research_only": True,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "next_step": "refresh or rebuild the upstream panic/range/strong candidate sources to latest trade date before treating no-signal as actionable.",
    }

    source_df.to_csv(OUT_DIR / "g3_v4_strong_offense_source_freshness.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report = f"""# G3 V4 强进攻 freshness 审计 V1

生成时间：{summary["generated_at"]}

## 结论

- 本地指数日线最新交易日：`{latest_index_date or "未知"}`
- current-source payload 最新日期相对参考日滞后：`{summary["current_source_lag_days"]}` 天
- 结论：`{summary["verdict"]}`

## 源新鲜度

{_md_table(source_df)}

## 判断

如果 current-source payload 滞后，则“今天无 G3 V4 强进攻候选”只能解释为“当前已生成源中无候选”，不能解释为“最新交易日无候选”。下一步必须先刷新或重建 panic/range/strong 三条上游候选源到最新交易日。
"""
    (OUT_DIR / "g3_v4_strong_offense_freshness_audit_report_cn.md").write_text(report, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(source_df[["source", "rows", "source_max_date", "reference_date", "lag_days", "freshness_status"]].to_string(index=False))


if __name__ == "__main__":
    main()
