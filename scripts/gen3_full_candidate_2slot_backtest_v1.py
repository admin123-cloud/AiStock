from __future__ import annotations

import json
import math
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_style_template_strategy_v1 import _trade_calendar  # noqa: E402
from scripts.gen3_promotion_self_test_v1 import (  # noqa: E402
    CONTRACTS,
    DEFAULT_END,
    INITIAL_CAPITAL,
    _build_price_context,
    _prepare_contract_candidates,
)
from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists  # noqa: E402
from utils.paths import report_path  # noqa: E402


ROUTER_DIR = report_path("gen3_market_state_router_v1")
PROMOTION_DIR = report_path("gen3_promotion_self_test_v1")
OUT_DIR = report_path("gen3_full_candidate_2slot_backtest_v1")
ALL_CANDIDATES = ROUTER_DIR / "state_router_all_candidates.csv"
SELECTED_CANDIDATES = ROUTER_DIR / "state_router_selected_candidates.csv"
CONTRACT_NAME = "g3_2slot_50_default_stop12_take12_prevlow"
AUDIT_PROFILE = "g3_final_top2_mainwave_sector_exempt_v1"
AUDIT_SCOPE = "legacy_mainwave_only_candidate_audit"
FORMAL_G3_PROFILE = "g3_final_with_g2_gap_supplement"
FINAL_G3_VARIANT = "eligible_top2_sector_guard_mainwave_exempt_sector_for_distinct"


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, Path):
        return str(value)
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return str(value)


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return "--"
    if not math.isfinite(x):
        return "--"
    return f"{x:.1%}"


def _money(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return "--"
    if not math.isfinite(x):
        return "--"
    return f"{x:,.0f}"


def _read_candidates(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path, low_memory=False, encoding="utf-8-sig")
    for col in ["entry_date", "policy_exit_date", "decision_date", "context_date"]:
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], errors="coerce").dt.normalize()
    for col in ["score", "net_ret", "entry_price", "router_candidate_rank", "mode_pick_rank"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["entry_date", "policy_exit_date", "net_ret", "entry_price"]).copy()
    if "trade_key" not in d.columns:
        d["trade_key"] = d["entry_date"].dt.strftime("%Y-%m-%d") + "|" + d["code"].astype(str)
    return d


def _load_sector_map() -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    if clickhouse_table_exists("sector_stocks") and clickhouse_table_exists("sectors"):
        level2 = clickhouse_query_df(
            """
            SELECT
                ss.stock_code AS code,
                s.code AS l2_sector_code,
                s.name AS l2_sector_name
            FROM sector_stocks ss
            JOIN sectors s ON s.code = ss.sector_code
            WHERE s.type = 'industry'
              AND s.level = 2
            """
        )
        if not level2.empty:
            frames.append(level2)
    if frames:
        out = pd.concat(frames, ignore_index=True, sort=False)
    else:
        out = pd.DataFrame(columns=["code", "l2_sector_code", "l2_sector_name"])
    if clickhouse_table_exists("stocks"):
        stocks = clickhouse_query_df("SELECT code, industry AS stock_industry FROM stocks")
        if not stocks.empty:
            out = stocks.merge(out, on="code", how="left") if out.empty else out.merge(stocks, on="code", how="outer")
    for col in ["code", "l2_sector_code", "l2_sector_name", "stock_industry"]:
        if col not in out.columns:
            out[col] = ""
        out[col] = out[col].fillna("").astype(str)
    out["sector_for_distinct"] = out["l2_sector_name"].where(out["l2_sector_name"].str.len() > 0, out["stock_industry"])
    return out.drop_duplicates(subset=["code"], keep="first")


def _enrich_sector(candidates: pd.DataFrame, sector_map: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty or sector_map.empty:
        out = candidates.copy()
        if "sector_for_distinct" not in out.columns:
            out["sector_for_distinct"] = ""
        return out
    keep = ["code", "l2_sector_code", "l2_sector_name", "stock_industry", "sector_for_distinct"]
    out = candidates.drop(columns=[c for c in keep if c in candidates.columns and c != "code"], errors="ignore")
    out = out.merge(sector_map[keep], on="code", how="left")
    for col in ["l2_sector_code", "l2_sector_name", "stock_industry", "sector_for_distinct"]:
        out[col] = out[col].fillna("").astype(str)
    return out


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "ok"}


def _prepare_sort_cols(d: pd.DataFrame) -> pd.DataFrame:
    out = d.copy()
    if "mode_pick_rank" not in out.columns:
        out["mode_pick_rank"] = 99
    if "router_candidate_rank" not in out.columns:
        out["router_candidate_rank"] = 999
    out["mode_pick_rank"] = pd.to_numeric(out["mode_pick_rank"], errors="coerce").fillna(99)
    out["router_candidate_rank"] = pd.to_numeric(out["router_candidate_rank"], errors="coerce").fillna(999)
    out["score"] = pd.to_numeric(out.get("score"), errors="coerce").fillna(-999)
    out["code"] = out.get("code", "").astype(str)
    return out


def _dedup_day_candidates(day: pd.DataFrame) -> pd.DataFrame:
    d = _prepare_sort_cols(day)
    d = d.sort_values(["mode_pick_rank", "router_candidate_rank", "score", "code"], ascending=[True, True, False, True]).copy()
    return d.drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)


def _direction_value(row: dict[str, Any], direction_col: str | None) -> str:
    if not direction_col:
        return ""
    value = row.get(direction_col)
    return str(value or "").strip()


def _route_value(row: dict[str, Any]) -> str:
    return str(row.get("mode") or row.get("route") or "").strip()


def _same_direction_allowed(candidate: dict[str, Any], existing: dict[str, Any], direction_col: str | None) -> bool:
    if not direction_col:
        return True
    candidate_direction = _direction_value(candidate, direction_col)
    existing_direction = _direction_value(existing, direction_col)
    if not candidate_direction or candidate_direction != existing_direction:
        return True
    return _route_value(candidate) == "institutional_mainwave" and _route_value(existing) == "institutional_mainwave"


def _simulate_portfolio(
    trades: pd.DataFrame,
    *,
    variant: str,
    daily_open_limit: int,
    direction_col: str | None = None,
    allow_same_direction_mainwave: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    by_entry = {day: g.copy() for day, g in trades.groupby("entry_date")}
    start = pd.Timestamp("2020-01-01")
    end = max(DEFAULT_END, pd.to_datetime(trades["policy_exit_date"], errors="coerce").max())
    calendar = _trade_calendar(start, end)
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    for day in calendar:
        realized_pnl = 0.0
        still_open: list[dict[str, Any]] = []
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                pnl = exit_value - float(pos["stake"])
                cash += exit_value
                realized_pnl += pnl
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = pnl
                entry_equity = float(out.get("entry_equity") or INITIAL_CAPITAL)
                out["account_loss_pct"] = min(0.0, pnl / entry_equity) if entry_equity > 0 else 0.0
                closed.append(out)
            else:
                still_open.append(pos)
        open_pos = still_open

        opened = 0
        skipped_same_direction = 0
        skipped_slot = 0
        selected_keys: list[str] = []
        todays = by_entry.get(day)
        if todays is not None and not todays.empty:
            today = _dedup_day_candidates(todays)
            for row in today.to_dict("records"):
                if opened >= daily_open_limit:
                    break
                if len(open_pos) >= 2:
                    skipped_slot += 1
                    break
                direction = _direction_value(row, direction_col)
                if (
                    direction_col
                    and direction
                    and any(
                        _direction_value(pos, direction_col) == direction
                        and not (allow_same_direction_mainwave and _same_direction_allowed(row, pos, direction_col))
                        for pos in open_pos
                    )
                ):
                    skipped_same_direction += 1
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * 0.50
                if stake <= 0 or cash < stake:
                    break
                pos = row.copy()
                pos["stake"] = stake
                pos["entry_equity"] = equity_before
                pos["contract"] = variant
                pos["direction_col"] = direction_col or ""
                pos["direction_value"] = direction
                cash -= stake
                open_pos.append(pos)
                selected_keys.append(str(pos.get("trade_key") or ""))
                opened += 1
        reserved = sum(float(p["stake"]) for p in open_pos)
        equity = cash + reserved
        curve_rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "variant": variant,
                "cash": cash,
                "reserved_principal": reserved,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "realized_pnl": realized_pnl,
            }
        )
        decision_rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "variant": variant,
                "candidate_count": 0 if todays is None else int(len(todays)),
                "distinct_code_count": 0 if todays is None else int(len(_dedup_day_candidates(todays))),
                "opened": opened,
                "open_positions_after": len(open_pos),
                "selected_trade_keys": "|".join(selected_keys),
                "skipped_same_direction": skipped_same_direction,
                "skipped_slot": skipped_slot,
            }
        )
    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, pd.DataFrame(closed), pd.DataFrame(decision_rows)


def _summary(variant: str, curve: pd.DataFrame, closed: pd.DataFrame) -> dict[str, Any]:
    if curve.empty:
        return {"variant": variant, "trades": 0, "return": 0.0, "max_drawdown": 0.0}
    rets = pd.to_numeric(closed.get("net_ret"), errors="coerce").dropna() if not closed.empty else pd.Series(dtype=float)
    account_loss = pd.to_numeric(closed.get("account_loss_pct"), errors="coerce").dropna() if not closed.empty else pd.Series(dtype=float)
    open_pos = pd.to_numeric(curve.get("open_positions"), errors="coerce").fillna(0)
    active = open_pos > 0
    full = open_pos >= 2
    return {
        "variant": variant,
        "trades": int(len(closed)),
        "return": float(curve.iloc[-1]["equity"] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": float(pd.to_numeric(curve["drawdown"], errors="coerce").min()),
        "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
        "avg_trade_return": float(rets.mean()) if len(rets) else 0.0,
        "worst_trade": float(rets.min()) if len(rets) else 0.0,
        "best_trade": float(rets.max()) if len(rets) else 0.0,
        "sum_pnl": float(pd.to_numeric(closed.get("realized_pnl"), errors="coerce").fillna(0).sum()) if not closed.empty else 0.0,
        "max_single_account_loss": float(account_loss.min()) if len(account_loss) else 0.0,
        "active_days": int(active.sum()),
        "full_days": int(full.sum()),
        "full_days_pct_active": float(full.sum() / active.sum()) if active.any() else 0.0,
        "avg_active_exposure": float((open_pos[active] * 0.50).mean()) if active.any() else 0.0,
        "avg_open_positions_active": float(open_pos[active].mean()) if active.any() else 0.0,
        "max_open_positions": int(open_pos.max()) if len(open_pos) else 0,
    }


def _write_report(payload: dict[str, Any]) -> None:
    lines = [
        "# G3 Full Candidate 2-Slot Backtest v1",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- All candidates: `{payload['source']['all_candidates']}`",
        "- Contract: 2 slots / 50% per slot / 12% hard stop / 12% half take-profit / previous-low protection",
        f"- Audit scope: `{AUDIT_SCOPE}`",
        f"- Audited legacy profile: `{AUDIT_PROFILE}`",
        f"- Formal G3 profile: `{FORMAL_G3_PROFILE}`",
        f"- Audited variant: `{FINAL_G3_VARIANT}`",
        "",
        "## Summary",
        "",
        "| Variant | Trades | Return | Max DD | Win Rate | Active Exposure | Full Days | PnL |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["summaries"]:
        lines.append(
            f"| {row['variant']} | {row['trades']} | {_pct(row['return'])} | {_pct(row['max_drawdown'])} | {_pct(row['win_rate'])} | {_pct(row['avg_active_exposure'])} | {row['full_days']} | {_money(row['sum_pnl'])} |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- `selected_top1_baseline` should match the current promotion result.",
            "- `eligible_top2_any_direction` uses all historically route-eligible candidates, deduplicated by code per day, and may buy two names on the same day if two slots are free.",
            "- `eligible_top2_distinct_sector_for_distinct` blocks duplicated sector exposure.",
            "- `eligible_top2_sector_guard_mainwave_exempt_sector_for_distinct` blocks duplicated sector exposure except when both candidates are institutional-mainwave names.",
            "- Sector fields are enriched from current ClickHouse sector membership, not point-in-time historical sector membership; use this as a routing-policy audit rather than a strict sector-history simulation.",
            "- This report is not a formal `g3_final_with_g2_gap_supplement` replay. It only audits the legacy/mainwave candidate lane.",
        ]
    )
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def run() -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    contract = next(c for c in CONTRACTS if c.name == CONTRACT_NAME)
    contract = replace(contract, max_single_loss_limit=0.065, max_mtm_drawdown_limit=0.19)
    selected = _read_candidates(SELECTED_CANDIDATES)
    all_candidates = _read_candidates(ALL_CANDIDATES)
    sector_map = _load_sector_map()
    selected = _enrich_sector(selected, sector_map)
    all_candidates = _enrich_sector(all_candidates, sector_map)
    eligible = all_candidates[all_candidates.get("router_eligible", pd.Series(False, index=all_candidates.index)).map(_truthy)].copy()

    price_context = _build_price_context(pd.concat([selected, eligible], ignore_index=True, sort=False))
    prepared_selected = _prepare_contract_candidates(selected, contract, price_context)
    prepared_eligible = _prepare_contract_candidates(eligible, contract, price_context)

    variants = [
        ("selected_top1_baseline", prepared_selected, 1, None, False),
        ("eligible_top1_from_full_pool", prepared_eligible, 1, None, False),
        ("eligible_top2_any_direction", prepared_eligible, 2, None, False),
        ("eligible_top2_distinct_mode_proxy", prepared_eligible, 2, "mode", False),
    ]
    sector_cols = [c for c in ["sector_for_distinct", "l2_sector_name", "stock_industry", "industry", "sector_name"] if c in prepared_eligible.columns and prepared_eligible[c].fillna("").astype(str).str.len().gt(0).any()]
    if sector_cols:
        variants.append((f"eligible_top2_distinct_{sector_cols[0]}", prepared_eligible, 2, sector_cols[0], False))
        variants.append((f"eligible_top2_sector_guard_mainwave_exempt_{sector_cols[0]}", prepared_eligible, 2, sector_cols[0], True))

    summaries: list[dict[str, Any]] = []
    for name, trades, daily_limit, direction_col, allow_same_direction_mainwave in variants:
        curve, closed, decisions = _simulate_portfolio(
            trades,
            variant=name,
            daily_open_limit=daily_limit,
            direction_col=direction_col,
            allow_same_direction_mainwave=allow_same_direction_mainwave,
        )
        summaries.append(_summary(name, curve, closed))
        trades.to_csv(OUT_DIR / f"{name}_prepared_candidates.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(OUT_DIR / f"{name}_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(OUT_DIR / f"{name}_closed_trades.csv", index=False, encoding="utf-8-sig")
        decisions.to_csv(OUT_DIR / f"{name}_daily_decisions.csv", index=False, encoding="utf-8-sig")

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": {
            "audit_scope": AUDIT_SCOPE,
            "audited_profile": AUDIT_PROFILE,
            "formal_profile": FORMAL_G3_PROFILE,
            "audited_variant": FINAL_G3_VARIANT,
            "all_candidates": str(ALL_CANDIDATES),
            "selected_candidates": str(SELECTED_CANDIDATES),
            "all_candidate_rows": int(len(all_candidates)),
            "route_eligible_rows": int(len(eligible)),
            "selected_rows": int(len(selected)),
            "sector_map_rows": int(len(sector_map)),
            "eligible_sector_coverage": float(prepared_eligible.get("sector_for_distinct", pd.Series("", index=prepared_eligible.index)).fillna("").astype(str).str.len().gt(0).mean()) if not prepared_eligible.empty else 0.0,
            "sector_columns_used": sector_cols,
        },
        "summaries": summaries,
    }
    pd.DataFrame(summaries).to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(payload)
    return payload


def main() -> None:
    payload = run()
    print(
        json.dumps(
            {
                "status": "completed",
                "out_dir": str(OUT_DIR),
                "summaries": payload["summaries"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
