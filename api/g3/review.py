"""G3 复盘与影子台账 API：实盘对账教练 + R40 影子做T 4周滚动 + 信号质量日报 + 日终复盘。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

from utils.common.paths import runtime_path

router = APIRouter()


def _read_json_safe(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


@router.get("/review/daily")
def get_g3_daily_review(limit: int = Query(10, ge=1, le=30)) -> dict[str, Any]:
    """读取最新日终复盘（runtime/daily_review）与实盘成交对账台账（t_tracker/eod & ledger）。"""
    review_dir = runtime_path("daily_review")
    tracker_dir = runtime_path("t_tracker")

    reviews: list[dict[str, Any]] = []
    if review_dir.exists():
        for f in sorted(review_dir.glob("review_*.md"), reverse=True)[:limit]:
            reviews.append({
                "file": f.name,
                "date": f.stem.replace("review_", ""),
                "content": f.read_text(encoding="utf-8", errors="ignore"),
            })

    eod_items: list[dict[str, Any]] = []
    eod_dir = tracker_dir / "eod"
    if eod_dir.exists():
        for f in sorted(eod_dir.glob("*.json"), reverse=True)[:limit]:
            data = _read_json_safe(f)
            if isinstance(data, dict):
                eod_items.append({"file": f.name, **data})

    ledger_items: list[dict[str, Any]] = []
    ledger_dir = tracker_dir / "ledger"
    if ledger_dir.exists():
        for f in sorted(ledger_dir.glob("*.json"), reverse=True)[:limit]:
            data = _read_json_safe(f)
            if isinstance(data, dict):
                ledger_items.append({"file": f.name, **data})

    return {
        "daily_reviews": reviews,
        "eod_summaries": eod_items,
        "ledger_entries": ledger_items,
    }


@router.get("/review/shadow")
def get_g3_shadow_review(limit: int = Query(20, ge=1, le=60)) -> dict[str, Any]:
    """读取 R40 影子做T日终固化记录与 4 周滚动统计。"""
    shadow_dir = runtime_path("t_tracker", "shadow")
    if not shadow_dir.exists():
        return {"rolling_summary": None, "daily_records": []}

    rolling = _read_json_safe(shadow_dir / "rolling_4w.json") or _read_json_safe(shadow_dir / "rolling_summary.json")
    daily_records: list[dict[str, Any]] = []
    for f in sorted(shadow_dir.glob("*.json"), reverse=True):
        if f.name in ("rolling_4w.json", "rolling_summary.json"):
            continue
        data = _read_json_safe(f)
        if isinstance(data, dict):
            daily_records.append({"file": f.name, **data})
        if len(daily_records) >= limit:
            break

    return {
        "rolling_summary": rolling,
        "daily_records": daily_records,
    }


@router.get("/review/signal-quality")
def get_g3_signal_quality(limit: int = Query(10, ge=1, le=30)) -> dict[str, Any]:
    """读取每日做T信号质量日报（runtime/signal_quality/）。"""
    sq_dir = runtime_path("signal_quality")
    if not sq_dir.exists():
        return {"reports": []}

    reports: list[dict[str, Any]] = []
    for f in sorted(sq_dir.glob("*.md"), reverse=True)[:limit]:
        json_pair = f.with_suffix(".json")
        reports.append({
            "date": f.stem,
            "file": f.name,
            "markdown": f.read_text(encoding="utf-8", errors="ignore"),
            "summary": _read_json_safe(json_pair),
        })
    return {"reports": reports}
