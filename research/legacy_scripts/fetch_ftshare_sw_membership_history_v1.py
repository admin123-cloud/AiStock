"""Fetch FTShare public point-in-time-capable Shenwan membership history.

The public FTShare MCP endpoint exposes the history tool without an API key in
the current environment.  This script keeps the raw response and writes a
normalized interval parquet.  It never writes ClickHouse automatically.
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.paths import report_path

BASE_URL = "https://market.ft.tech/gateway/mcp"
REPORT_DIR = report_path("sector_mainwave_leader_rebuild_v2")


def _event_json(text: str, wanted_id: int) -> dict[str, Any]:
    """Parse FTShare's chunk-split SSE response."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("data: {"))
    chunks = [lines[start][6:]]
    for line in lines[start + 1 :]:
        if line.startswith("id:"):
            break
        chunks.append(line)
    payload = json.loads("".join(chunks))
    if payload.get("id") != wanted_id:
        raise RuntimeError(f"unexpected JSON-RPC id: {payload.get('id')}")
    if "error" in payload:
        raise RuntimeError(str(payload["error"]))
    result = payload.get("result") or {}
    if result.get("isError"):
        raise RuntimeError(str(result))
    text_block = (result.get("content") or [{}])[0].get("text")
    if not text_block:
        raise RuntimeError("missing FTShare content text")
    data = json.loads(text_block)
    if "error" in data:
        raise RuntimeError(str(data["error"]))
    return data


def _session() -> tuple[requests.Session, dict[str, str]]:
    s = requests.Session()
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    r = s.post(
        BASE_URL,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "aistock-membership-importer", "version": "1.0"},
            },
        },
        timeout=30,
    )
    r.raise_for_status()
    sid = r.headers.get("mcp-session-id")
    if not sid:
        raise RuntimeError("FTShare initialize did not return mcp-session-id")
    headers = dict(headers)
    headers.update({"Mcp-Session-Id": sid, "MCP-Protocol-Version": "2025-11-25"})
    s.post(BASE_URL, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=30).raise_for_status()
    return s, headers


def _call(s: requests.Session, headers: dict[str, str], ident: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    r = s.post(BASE_URL, headers=headers, json={"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, timeout=90)
    r.raise_for_status()
    return _event_json(r.text, ident)


def fetch_industry_codes() -> list[dict[str, Any]]:
    s, h = _session()
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        data = _call(s, h, 10 + page, "ft_v1_sw_industry_overview", {"date": "20260924", "level": 3, "page": page, "page_size": 200})
        out.extend(data.get("data") or [])
        meta = data.get("metadata") or {}
        pagination = meta.get("pagination") or {}
        if not pagination.get("has_more"):
            break
        page += 1
    return [{"industry_code": str(x["industryCode"]), "industry_name": str(x.get("industryName") or "")} for x in out if x.get("industryCode")]


def fetch_one(code: str, worker_id: int) -> tuple[str, list[dict[str, Any]], str | None]:
    for attempt in range(3):
        try:
            s, h = _session()
            data = _call(s, h, 100 + worker_id, "ft_v1_sw_industry_constituent_history", {"industry_code": code})
            return code, list(data.get("data") or []), None
        except Exception as exc:
            if attempt == 2:
                return code, [], f"{type(exc).__name__}: {exc}"
            time.sleep(1.0 * (attempt + 1))
    return code, [], "unknown"


def normalize(rows: list[dict[str, Any]]) -> pd.DataFrame:
    out: list[dict[str, Any]] = []
    for row in rows:
        stock = str(row.get("stockCode") or "").zfill(6)
        if not stock:
            continue
        for level, code_key, name_key in (
            (1, "swLevel1Code", "swLevel1Name"),
            (2, "swLevel2Code", "swLevel2Name"),
            (3, "swLevel3Code", "swLevel3Name"),
        ):
            sector = row.get(code_key)
            if not sector:
                continue
            out.append({
                "source": "ftshare",
                "sector_code": str(sector),
                "sector_name": str(row.get(name_key) or ""),
                "sector_level": level,
                "canonical_code": stock,
                "stock_name": str(row.get("stockName") or ""),
                "in_date": row.get("inDate"),
                "out_date": row.get("outDate"),
                "source_tool": "ft_v1_sw_industry_constituent_history",
            })
    df = pd.DataFrame(out)
    if df.empty:
        return df
    df["in_date"] = pd.to_datetime(df["in_date"], errors="coerce").dt.date
    df["out_date"] = pd.to_datetime(df["out_date"], errors="coerce").dt.date
    return df.drop_duplicates(["sector_code", "sector_level", "canonical_code", "in_date", "out_date"]).sort_values(["sector_level", "sector_code", "canonical_code", "in_date"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    codes = fetch_industry_codes()
    unique = {x["industry_code"]: x["industry_name"] for x in codes}
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(fetch_one, code, idx): code for idx, code in enumerate(sorted(unique), start=1)}
        for n, fut in enumerate(as_completed(futures), start=1):
            code, got, error = fut.result()
            if error:
                failures.append({"industry_code": code, "error": error})
            rows.extend(got)
            if n % 25 == 0 or n == len(futures):
                print(f"fetched {n}/{len(futures)} industries, rows={len(rows)}, failures={len(failures)}", flush=True)
    df = normalize(rows)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    parquet = REPORT_DIR / "ftshare_sw_membership_history.parquet"
    df.to_parquet(parquet, index=False)
    meta = {
        "started_at": started,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "endpoint": BASE_URL,
        "tool": "ft_v1_sw_industry_constituent_history",
        "industry_codes": len(unique),
        "raw_rows": len(rows),
        "normalized_rows": len(df),
        "sector_count": int(df.sector_code.nunique()) if not df.empty else 0,
        "stock_count": int(df.canonical_code.nunique()) if not df.empty else 0,
        "min_in_date": str(df.in_date.min()) if not df.empty else None,
        "max_in_date": str(df.in_date.max()) if not df.empty else None,
        "non_null_out_date": int(df.out_date.notna().sum()) if not df.empty else 0,
        "failures": failures,
        "research_only": True,
        "note": "Raw public endpoint response normalized to interval membership; no ClickHouse write performed.",
    }
    (REPORT_DIR / "ftshare_sw_membership_history.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
