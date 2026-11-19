"""G3 做T实战台 API：打通双账号真实持仓 + 做T观察名单 + 09:25 周期与带位参数 + 快慢车道草案。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.broker.broker_accounts import get_accounts_overview
from services.trading.holding_t_watchlist import (
    read_watchlist,
    sync_watchlist_from_ths,
    upsert_watchlist,
)
from utils.common.paths import runtime_path

router = APIRouter()
BJ_TZ = timezone(timedelta(hours=8))


def _watchlist_paths() -> tuple[Path, Path]:
    store_path = runtime_path("gen3_state_alpha", "holding_t_watchlist", "holding_tick_watchlist.json")
    legacy_path = runtime_path("gen3_state_alpha", "holding_tick_watchlist.json")
    return store_path, legacy_path


def _read_json_safe(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _latest_dated_json(directory: Path, prefix: str) -> tuple[str | None, dict[str, Any]]:
    if not directory.exists():
        return None, {}
    files = sorted(directory.glob(f"{prefix}*.json"), reverse=True)
    for f in files:
        data = _read_json_safe(f)
        if data:
            return f.name, data
    return None, {}


def _load_today_drafts(tracker_dir: Path, trade_date: str) -> list[dict[str, Any]]:
    drafts_dir = tracker_dir / "drafts"
    if not drafts_dir.exists():
        return []
    ymd = trade_date.replace("-", "")
    items: list[dict[str, Any]] = []
    # 读取当日 jsonl 或 json 草案文件
    for f in sorted(drafts_dir.glob(f"*{ymd}*")):
        if f.suffix == ".jsonl":
            for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        items.append(obj)
                except Exception:
                    continue
        elif f.suffix == ".json":
            obj = _read_json_safe(f)
            if isinstance(obj.get("drafts"), list):
                items.extend(obj["drafts"])
            elif obj:
                items.append(obj)
    return items[-50:]


def _collect_live_holdings() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """优先从双账号远航采集（ths_read_account）读取最新真实持仓，回退 broker_state.json。"""
    overview = get_accounts_overview()
    merged_holdings: list[dict[str, Any]] = []
    for acct in overview.get("accounts") or []:
        acct_id = acct.get("account_id", "")
        acct_label = acct.get("label") or acct_id
        for h in acct.get("holdings") or []:
            code = str(h.get("code") or "").split(".")[0].zfill(6)
            if not code or code == "000000":
                continue
            qty = int(float(h.get("quantity") or h.get("shares") or h.get("qty") or 0))
            avail = int(float(h.get("available_quantity") or h.get("available_shares") or qty))
            cur_px = h.get("current_price") or h.get("price")
            mv = h.get("market_value")
            if mv is None and cur_px is not None and qty:
                mv = round(float(cur_px) * qty, 2)
            merged_holdings.append({
                "account_id": acct_id,
                "account_label": acct_label,
                "code": code,
                "name": h.get("name") or "",
                "shares": qty,
                "available_shares": avail,
                "cost_price": h.get("cost_price"),
                "current_price": cur_px,
                "market_value": mv,
                "profit_loss": h.get("floating_profit") if h.get("floating_profit") is not None else h.get("profit_loss"),
                "profit_pct": h.get("profit_ratio") if h.get("profit_ratio") is not None else h.get("profit_pct"),
                "daily_profit": h.get("daily_profit"),
                "daily_profit_pct": h.get("daily_profit_ratio"),
                "source": "ths_read_account",
            })

    if not merged_holdings:
        broker_state = _read_json_safe(runtime_path("gen3_state_alpha", "broker_state.json"))
        for h in broker_state.get("holdings") or []:
            code = str(h.get("code") or "").split(".")[0].zfill(6)
            if not code or code == "000000":
                continue
            merged_holdings.append({
                "account_id": "broker_state",
                "account_label": "历史快照",
                "code": code,
                "name": h.get("name") or "",
                "shares": h.get("qty") or h.get("shares") or 0,
                "available_shares": h.get("available_qty") or h.get("qty") or 0,
                "cost_price": h.get("cost_price"),
                "current_price": h.get("last_price"),
                "market_value": h.get("market_value"),
                "profit_loss": None,
                "profit_pct": None,
                "source": "broker_state_fallback",
            })
    return overview, merged_holdings


@router.get("/desk")
def get_g3_desk() -> dict[str, Any]:
    """G3 做T实战台一站式聚合：双账号持仓 + 观察名单 + 周期判定 + 实时布防态 + 当日草案。"""
    now_bj = datetime.now(BJ_TZ)
    tracker_dir = runtime_path("t_tracker")

    overview, live_holdings = _collect_live_holdings()
    store_path, legacy_path = _watchlist_paths()
    watchlist_doc = read_watchlist(store_path, legacy_path=legacy_path)
    watchlist_items = watchlist_doc.get("entries") or watchlist_doc.get("items") or []

    regime_file, regime_doc = _latest_dated_json(tracker_dir, "regime_")
    trade_date = str(regime_doc.get("date") or now_bj.strftime("%Y-%m-%d"))
    state_file, state_doc = _latest_dated_json(tracker_dir, "state_")
    contract_stop = _read_json_safe(tracker_dir / "contract_stop.json")
    drafts = _load_today_drafts(tracker_dir, trade_date)

    # 聚合每只股票的三层视图：持仓 + 观察名单 + 周期参数 + 实时布防
    holding_by_code: dict[str, list[dict[str, Any]]] = {}
    for h in live_holdings:
        holding_by_code.setdefault(h["code"], []).append(h)

    watch_by_code: dict[str, dict[str, Any]] = {}
    for w in watchlist_items:
        code = str(w.get("code") or "").split(".")[0].zfill(6)
        if code and code != "000000":
            watch_by_code[code] = w

    regime_codes = regime_doc.get("codes") or {}
    all_codes = sorted(set(holding_by_code.keys()) | set(watch_by_code.keys()) | set(regime_codes.keys()))

    cards: list[dict[str, Any]] = []
    for code in all_codes:
        h_list = holding_by_code.get(code, [])
        w_item = watch_by_code.get(code)
        r_info = regime_codes.get(code) or {}
        s_info = state_doc.get(code) or {}
        total_shares = sum(int(x.get("shares") or 0) for x in h_list)
        t_lot_cap = (total_shares // 3 // 100) * 100 if total_shares >= 300 else 0
        name = (
            r_info.get("name")
            or (h_list[0].get("name") if h_list else "")
            or (w_item.get("name") if w_item else "")
            or code
        )
        cards.append({
            "code": code,
            "name": name,
            "in_holdings": bool(h_list),
            "in_watchlist": bool(w_item),
            "total_shares": total_shares,
            "t_lot_cap": t_lot_cap,
            "holdings": h_list,
            "watchlist": w_item,
            "regime": r_info.get("regime") or "unknown",
            "ma5": r_info.get("ma5"),
            "ma10": r_info.get("ma10"),
            "ma20": r_info.get("ma20"),
            "pos20": r_info.get("pos20"),
            "hi20": r_info.get("hi20"),
            "lo20": r_info.get("lo20"),
            "prev_close": r_info.get("close"),
            "chg5d_pct": r_info.get("chg5d_pct"),
            "params": r_info.get("params") or {},
            "armed_sides": s_info.get("sides") or {},
            "hard_stop": contract_stop.get(code),
        })

    merged_funds = overview.get("merged") or {}
    bridge_info = overview.get("bridge") or {}
    return {
        "as_of": now_bj.isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "accounts_summary": {
            "total_assets": merged_funds.get("total_assets"),
            "total_market_value": merged_funds.get("market_value"),
            "total_cash": merged_funds.get("available_cash"),
            "total_floating_profit": merged_funds.get("floating_profit"),
            "account_count": overview.get("accounts_total") or len(overview.get("accounts") or []),
            "accounts_ready": overview.get("accounts_ready"),
            "bridge_online": bridge_info.get("online"),
            "updated_at": bridge_info.get("heartbeat_at"),
        },
        "regime_meta": {
            "file": regime_file,
            "generated_at": regime_doc.get("generated_at"),
            "version": regime_doc.get("version"),
        },
        "state_meta": {
            "file": state_file,
        },
        "cards": cards,
        "drafts": drafts,
        "watchlist_count": len(watchlist_items),
        "holding_count": len(holding_by_code),
    }


class WatchlistUpsertRequest(BaseModel):
    add: list[Any] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


@router.post("/watchlist/upsert")
def post_g3_watchlist_upsert(req: WatchlistUpsertRequest) -> dict[str, Any]:
    """Scoped-write：维护做T持仓观察名单（仅写 holding_t_watchlist 授权目录）。"""
    try:
        store_path, legacy_path = _watchlist_paths()
        return upsert_watchlist(
            store_path,
            add=req.add,
            remove=req.remove,
            legacy_path=legacy_path,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/watchlist/sync-ths")
def post_g3_watchlist_sync_ths() -> dict[str, Any]:
    """Scoped-write：从同花顺双账号真实持仓同步做T观察名单。"""
    try:
        store_path, legacy_path = _watchlist_paths()
        overview, live_holdings = _collect_live_holdings()
        synced_at = str(overview.get("updated_at") or datetime.now(BJ_TZ).isoformat(timespec="seconds"))
        return sync_watchlist_from_ths(
            store_path,
            holdings=live_holdings,
            synced_at=synced_at,
            legacy_path=legacy_path,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
