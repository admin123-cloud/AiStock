from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_promotion_self_test_v1 import (  # noqa: E402
    Contract,
    _build_price_context,
    _events_for_trade,
)
from scripts.scan_current_wave_style_candidates_v1 import (  # noqa: E402
    _is_tdx_mainline_sector_name,
    _topic_family,
)
from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists  # noqa: E402
from utils.paths import report_path  # noqa: E402


SOURCE_TRADES = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
OUT_DIR = report_path("institutional_mainwave_history_topic_replay_v1")

CONTRACT = Contract(
    name="g3_2slot_50_stop12_take12_prevlow_daily_replay",
    label="G3 2槽50% + 12%硬止损 + 12%先减半 + 剩余仓前低保护（日线/可用30m复盘）",
    slots=2,
    slot_pct=0.50,
    hard_stop_pct=0.12,
    take_profit_pct=0.12,
    take_profit_sell_ratio=0.50,
    use_prev_low_after_take_profit=True,
    require_mainwave_gate=False,
)


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        if isinstance(value, float) and not math.isfinite(value):
            return None
    except Exception:
        pass
    return value


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return ""
    if not math.isfinite(x):
        return ""
    return f"{x:.2%}"


def _code6(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else text[-6:]


def _date_text(value: Any) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(ts) else ts.strftime("%Y-%m-%d")


def _load_source() -> pd.DataFrame:
    if not SOURCE_TRADES.exists():
        raise FileNotFoundError(f"missing source trades: {SOURCE_TRADES}")
    df = pd.read_csv(SOURCE_TRADES, encoding="utf-8-sig", low_memory=False)
    if "mode" in df.columns:
        df = df[df["mode"].fillna("").astype(str).eq("institutional_mainwave")].copy()
    if df.empty:
        raise RuntimeError("source has no institutional_mainwave rows")
    for col in ["entry_date", "policy_exit_date", "exit_date", "decision_date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")
    df["code"] = df["code"].fillna("").astype(str)
    df["code6"] = df["code"].map(_code6)
    return df.dropna(subset=["entry_date", "policy_exit_date", "code"]).reset_index(drop=True)


def _entry_price_from_context(row: dict[str, Any], price_context: dict[str, Any]) -> tuple[float | None, str, float | None, str]:
    code = str(row.get("code") or "")
    entry = pd.Timestamp(row["entry_date"]).normalize()
    source_price = pd.to_numeric(pd.Series([row.get("entry_price")]), errors="coerce").iloc[0]
    source_price = float(source_price) if pd.notna(source_price) and float(source_price) > 0 else None
    market_open: float | None = None
    market_open_source = ""
    minute = price_context["minute_groups"].get(code, pd.DataFrame())
    if not minute.empty:
        day = minute[minute["bar_date"].eq(entry)]
        if not day.empty and float(day.iloc[0]["open"]) > 0:
            market_open = float(day.iloc[0]["open"])
            market_open_source = "minute30_first_open"
    daily = price_context["daily_groups"].get(code, pd.DataFrame())
    if market_open is None and not daily.empty:
        hit = daily[daily["trade_date"].eq(entry)]
        if not hit.empty and float(hit.iloc[0]["open"]) > 0:
            market_open = float(hit.iloc[0]["open"])
            market_open_source = "daily_open"
    if source_price is not None:
        return source_price, "source_entry_price_scaled_to_current_bars", market_open, market_open_source
    if market_open is not None:
        return market_open, market_open_source, market_open, market_open_source
    return None, "missing_entry_price", None, ""


def _fallback_ret(row: dict[str, Any], entry_price: float, price_context: dict[str, Any]) -> float:
    code = str(row.get("code") or "")
    exit_date = pd.Timestamp(row["policy_exit_date"]).normalize()
    daily = price_context["daily_groups"].get(code, pd.DataFrame())
    if not daily.empty:
        hit = daily[daily["trade_date"].eq(exit_date)]
        if not hit.empty and float(hit.iloc[0]["close"]) > 0:
            return float(hit.iloc[0]["close"]) / float(entry_price) - 1.0
    old_ret = pd.to_numeric(pd.Series([row.get("net_ret")]), errors="coerce").iloc[0]
    return float(old_ret) if pd.notna(old_ret) else 0.0


def _load_tdx_topics(codes: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    topic_cols = ["code", "code6", "topic_name", "sector_family", "tdx_sector_code", "tdx_snapshot_date"]
    if not codes or not clickhouse_table_exists("source_sector_stocks"):
        return pd.DataFrame(columns=topic_cols), pd.DataFrame(columns=["code", "tdx_topic_names", "sector_families"])
    quoted = ",".join("'" + str(code).replace("'", "''") + "'" for code in sorted(set(codes)) if str(code).strip())
    raw = clickhouse_query_df(
        f"""
        SELECT canonical_code AS code, sector_code, sector_name, snapshot_date
        FROM source_sector_stocks
        WHERE source = 'tdx'
          AND alias_status = 'active'
          AND canonical_code IN ({quoted})
          AND snapshot_date = (
              SELECT max(snapshot_date)
              FROM source_sector_stocks
              WHERE source = 'tdx'
                AND alias_status = 'active'
                AND canonical_code != ''
          )
        """
    )
    if raw.empty:
        return pd.DataFrame(columns=topic_cols), pd.DataFrame(columns=["code", "tdx_topic_names", "sector_families"])
    raw["sector_name"] = raw["sector_name"].fillna("").astype(str).str.strip()
    raw = raw[raw["sector_name"].map(_is_tdx_mainline_sector_name)].copy()
    if raw.empty:
        return pd.DataFrame(columns=topic_cols), pd.DataFrame(columns=["code", "tdx_topic_names", "sector_families"])
    raw["code"] = raw["code"].fillna("").astype(str)
    raw["code6"] = raw["code"].map(_code6)
    raw["topic_name"] = raw["sector_name"]
    raw["sector_family"] = raw["topic_name"].map(lambda value: _topic_family(value, ""))
    raw["tdx_sector_code"] = raw["sector_code"].fillna("").astype(str)
    raw["tdx_snapshot_date"] = pd.to_datetime(raw["snapshot_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    topic = raw[topic_cols].drop_duplicates(["code", "topic_name"]).sort_values(["code", "topic_name"]).reset_index(drop=True)
    agg = (
        topic.groupby("code", as_index=False)
        .agg(
            tdx_topic_names=("topic_name", lambda s: ",".join(dict.fromkeys([str(x) for x in s if str(x)]))),
            sector_families=("sector_family", lambda s: ",".join(dict.fromkeys([str(x) for x in s if str(x)]))),
            tdx_topic_count=("topic_name", "nunique"),
        )
        .reset_index(drop=True)
    )
    return topic, agg


def _replay_trades(source: pd.DataFrame, price_context: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    trade_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    for record in source.to_dict("records"):
        entry_price, entry_source, market_open, market_open_source = _entry_price_from_context(record, price_context)
        if entry_price is None or entry_price <= 0:
            continue
        replay_record = record.copy()
        replay_record["entry_price"] = entry_price
        replay_record["net_ret"] = _fallback_ret(replay_record, entry_price, price_context)
        events = _events_for_trade(replay_record, CONTRACT, price_context)
        if not events:
            continue
        weighted_ret = sum(float(ev["ratio"]) * float(ev["ret"]) for ev in events)
        final_dt = max(pd.Timestamp(ev["dt"]) for ev in events)
        stake = pd.to_numeric(pd.Series([record.get("stake")]), errors="coerce").iloc[0]
        stake = float(stake) if pd.notna(stake) and float(stake) > 0 else 1.0
        out = record.copy()
        out["entry_price_source_replay"] = entry_source
        out["market_open_replay"] = market_open
        out["market_open_source_replay"] = market_open_source
        out["entry_price_to_market_open_ratio"] = entry_price / market_open if market_open and market_open > 0 else pd.NA
        out["entry_price_original"] = pd.to_numeric(pd.Series([record.get("entry_price")]), errors="coerce").iloc[0]
        out["entry_price"] = entry_price
        out["policy_exit_date_original"] = record.get("policy_exit_date")
        out["exit_reason_original"] = record.get("exit_reason")
        out["net_ret_original"] = pd.to_numeric(pd.Series([record.get("net_ret")]), errors="coerce").iloc[0]
        out["policy_exit_date"] = final_dt.strftime("%Y-%m-%d")
        out["exit_date"] = final_dt.strftime("%Y-%m-%d")
        out["exit_datetime"] = final_dt.strftime("%Y-%m-%d %H:%M:%S")
        out["policy_exit_datetime"] = final_dt.strftime("%Y-%m-%d %H:%M:%S")
        out["exit_reason"] = ",".join(str(ev["reason"]) for ev in events)
        out["net_ret"] = weighted_ret
        out["policy_net_ret"] = weighted_ret
        out["stake"] = stake
        out["exit_value"] = stake * (1.0 + weighted_ret)
        out["realized_pnl"] = stake * weighted_ret
        out["account_ret"] = weighted_ret * float(record.get("position_pct") or record.get("slot_pct") or 0.5)
        out["replay_contract"] = CONTRACT.name
        out["replay_price_granularity"] = "30m_if_available_else_daily"
        out["replay_source"] = "institutional_mainwave_history_topic_replay_v1"
        out["trade_key"] = f"institutional_mainwave_replay|{out['entry_date']}|{out['code']}"
        trade_rows.append(out)

        for idx, ev in enumerate(events, start=1):
            ev_ret = float(ev["ret"])
            event_rows.append(
                {
                    "trade_key": out["trade_key"],
                    "code": out["code"],
                    "name": out.get("name") or out.get("stock_name") or "",
                    "entry_date": out["entry_date"],
                    "entry_price": entry_price,
                    "event_seq": idx,
                    "sell_datetime": pd.Timestamp(ev["dt"]).strftime("%Y-%m-%d %H:%M:%S"),
                    "sell_date": pd.Timestamp(ev["dt"]).strftime("%Y-%m-%d"),
                    "sell_ratio": float(ev["ratio"]),
                    "sell_reason": str(ev["reason"]),
                    "sell_ret": ev_ret,
                    "sell_price_proxy": entry_price * (1.0 + ev_ret),
                    "stake": stake,
                    "realized_pnl_part": stake * float(ev["ratio"]) * ev_ret,
                    "replay_price_granularity": out["replay_price_granularity"],
                }
            )
    return pd.DataFrame(trade_rows), pd.DataFrame(event_rows)


def _summary_metrics(df: pd.DataFrame) -> dict[str, Any]:
    ret = pd.to_numeric(df.get("net_ret"), errors="coerce")
    pnl = pd.to_numeric(df.get("realized_pnl"), errors="coerce")
    return {
        "rows": int(len(df)),
        "entry_start": _date_text(df["entry_date"].min()) if len(df) and "entry_date" in df.columns else "",
        "entry_end": _date_text(df["entry_date"].max()) if len(df) and "entry_date" in df.columns else "",
        "sum_net_ret": float(ret.sum()) if ret.notna().any() else None,
        "avg_net_ret": float(ret.mean()) if ret.notna().any() else None,
        "win_rate": float((ret > 0).mean()) if ret.notna().any() else None,
        "sum_realized_pnl": float(pnl.sum()) if pnl.notna().any() else None,
    }


def _group_summary(df: pd.DataFrame, key: str) -> pd.DataFrame:
    if df.empty or key not in df.columns:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for value, group in df.groupby(key, dropna=False):
        ret = pd.to_numeric(group.get("net_ret"), errors="coerce")
        pnl = pd.to_numeric(group.get("realized_pnl"), errors="coerce")
        rows.append(
            {
                key: value,
                "rows": int(len(group)),
                "sum_realized_pnl": float(pnl.sum()) if pnl.notna().any() else None,
                "avg_net_ret": float(ret.mean()) if ret.notna().any() else None,
                "win_rate": float((ret > 0).mean()) if ret.notna().any() else None,
            }
        )
    return pd.DataFrame(rows).sort_values(["sum_realized_pnl", "rows"], ascending=[False, False])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source = _load_source()
    price_context = _build_price_context(source)
    replayed, events = _replay_trades(source, price_context)
    if replayed.empty:
        raise RuntimeError("replay produced no trades")

    topic_members, topic_agg = _load_tdx_topics(replayed["code"].dropna().astype(str).unique().tolist())
    if not topic_agg.empty:
        replayed = replayed.merge(topic_agg, on="code", how="left")
    replayed["historical_sector_for_distinct"] = replayed.get("sector_for_distinct", pd.Series("", index=replayed.index))
    replayed["sector_family"] = replayed["historical_sector_for_distinct"].map(lambda value: _topic_family(value, ""))
    replayed.loc[replayed["sector_family"].eq(replayed["historical_sector_for_distinct"]) & replayed["sector_families"].fillna("").str.contains("半导体链", regex=False), "sector_family"] = "半导体链"

    original_metrics = _summary_metrics(source)
    replay_metrics = _summary_metrics(replayed)
    diff = replayed[
        [
            "trade_key",
            "code",
            "name",
            "entry_date",
            "policy_exit_date_original",
            "policy_exit_date",
            "exit_reason_original",
            "exit_reason",
            "entry_price_original",
            "entry_price",
            "net_ret_original",
            "net_ret",
            "realized_pnl",
            "sector_family",
            "tdx_topic_names",
        ]
    ].copy()
    diff["net_ret_delta"] = pd.to_numeric(diff["net_ret"], errors="coerce") - pd.to_numeric(diff["net_ret_original"], errors="coerce")
    diff["entry_price_delta"] = pd.to_numeric(diff["entry_price"], errors="coerce") - pd.to_numeric(diff["entry_price_original"], errors="coerce")

    replayed.to_csv(OUT_DIR / "replayed_closed_trades.csv", index=False, encoding="utf-8-sig")
    events.to_csv(OUT_DIR / "sell_events.csv", index=False, encoding="utf-8-sig")
    diff.to_csv(OUT_DIR / "diff_vs_formal_institutional_source.csv", index=False, encoding="utf-8-sig")
    topic_members.to_csv(OUT_DIR / "tdx_topic_membership.csv", index=False, encoding="utf-8-sig")
    _group_summary(replayed, "sector_family").to_csv(OUT_DIR / "sector_family_summary.csv", index=False, encoding="utf-8-sig")
    exploded = replayed[["trade_key", "code", "name", "entry_date", "net_ret", "realized_pnl"]].merge(topic_members, on="code", how="left")
    _group_summary(exploded.dropna(subset=["topic_name"]), "topic_name").to_csv(OUT_DIR / "topic_summary.csv", index=False, encoding="utf-8-sig")

    changed_exit = int((diff["policy_exit_date_original"].astype(str) != diff["policy_exit_date"].astype(str)).sum())
    changed_ret = int(diff["net_ret_delta"].abs().gt(1e-9).sum())
    summary = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(SOURCE_TRADES),
        "out_dir": str(OUT_DIR),
        "contract": CONTRACT.name,
        "price_granularity": "30m_if_available_else_daily",
        "original_metrics": original_metrics,
        "replay_metrics": replay_metrics,
        "sell_event_rows": int(len(events)),
        "changed_exit_date_rows": changed_exit,
        "changed_return_rows": changed_ret,
        "tdx_topic_member_rows": int(len(topic_members)),
        "note": "独立复盘结果，未覆盖正式历史成交源；旧分钟不全时回落到日线。",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")

    lines = [
        "# 机构主升浪历史买卖点复盘 v1",
        "",
        "## 结论",
        "",
        f"- 样本：{replay_metrics['rows']} 笔，区间 {replay_metrics['entry_start']} 至 {replay_metrics['entry_end']}。",
        f"- 复盘后平均单笔收益：{_pct(replay_metrics['avg_net_ret'])}，胜率：{_pct(replay_metrics['win_rate'])}。",
        f"- 复盘后实现盈亏合计：{replay_metrics['sum_realized_pnl']:,.2f}。",
        f"- 相比原正式源，退出日期变化 {changed_exit} 笔，收益变化 {changed_ret} 笔。",
        "",
        "## 口径",
        "",
        "- 买点：entry_date 的 30m 首根开盘价可用则使用，否则使用日线开盘价。",
        "- 卖点：按 G3 2槽50%合同复盘，12%硬止损、12%先减半、剩余仓前低保护。",
        "- 历史 30m 不足时自动回退到日线高低价，不把日线复盘伪装成分钟真实成交。",
        "- TDX 多题材只用于解释每日主线和归因，`sector_family` 用于大类归因。",
        "",
        "## 产物",
        "",
        "- `replayed_closed_trades.csv`：重算后的历史成交。",
        "- `sell_events.csv`：每笔分批卖出事件。",
        "- `diff_vs_formal_institutional_source.csv`：与原正式机构主升历史源的差异。",
        "- `sector_family_summary.csv` / `topic_summary.csv`：产业链和题材收益复盘。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8-sig")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
