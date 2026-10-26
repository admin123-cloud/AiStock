"""Reconcile unresolved index keys with Sina's public historical K-line API.

The API is read-only and returns index OHLC plus raw volume.  It does not
return amount, so the ledger explicitly marks amount as unavailable rather
than treating an OHLCV match as a full six-field consensus.  Raw response
bytes are saved and hashed per code; no ClickHouse connection is opened.
Dates before 2000 are excluded from the active tracking output when the
caller enables the user's historical cutoff, while an exclusion manifest keeps
the exact keys that were removed from tracking.
"""

from __future__ import annotations

import argparse
from collections import Counter
import gzip
import hashlib
import json
import math
from pathlib import Path
from datetime import date
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TARGET_CODES = ("000680.SH", "000688.SH", "000852.SH")
FIELDS = ("open", "high", "low", "close", "volume", "amount")
OHLCV_FIELDS = ("open", "high", "low", "close", "volume")
PRICE_TOLERANCE = 0.01
VOLUME_TOLERANCE = 0.01
DEFAULT_API = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
DEFAULT_INPUT = Path(r"H:\Codex\Outputs\AiStock\daily_registry_scope_explanations_20261024\index_second_source_baostock_reconciliation.jsonl.gz")
DEFAULT_ROOT = Path(r"H:\Codex\Outputs\AiStock\daily_registry_scope_explanations_20261024\index_missing_keys_sina_20261024")


def _open_jsonl(path: Path, mode: str = "rt"):
    return gzip.open(path, mode, encoding="utf-8") if path.suffix == ".gz" else path.open(mode, encoding="utf-8")


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with _open_jsonl(path) as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _provider_code(code: str) -> str:
    return f"sh{code[:6]}"


def load_candidates(path: Path, cutoff: str = "2000-01-01", accepted_dispositions: tuple[str, ...] = ("baostock_missing_exact_key", "tencent_source_missing")) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    cutoff_date = date.fromisoformat(cutoff)
    seen: set[tuple[str, str]] = set()
    for row in _iter_jsonl(path):
        code = str(row.get("code") or "").strip().upper()
        trade_date = str(row.get("trade_date") or "")[:10]
        if row.get("disposition") not in accepted_dispositions or code not in TARGET_CODES or not trade_date:
            continue
        key = (code, trade_date)
        if key in seen:
            raise ValueError(f"duplicate missing exact key: {code}@{trade_date}")
        seen.add(key)
        try:
            before_cutoff = date.fromisoformat(trade_date) < cutoff_date
        except ValueError:
            before_cutoff = False
        if before_cutoff:
            excluded.append({"code": code, "trade_date": trade_date, "reason": "user_cutoff_before_2000"})
        else:
            active.append(row)
    return active, excluded


def _query_url(api_url: str, code: str) -> str:
    params = {"symbol": _provider_code(code), "scale": "240", "ma": "no", "datalen": "10000"}
    return api_url + "?" + urlencode(params)


def fetch_code(code: str, *, api_url: str, raw_dir: Path, timeout: int = 30) -> dict[str, Any]:
    url = _query_url(api_url, code)
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{code.replace('.', '_')}.json"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = int(getattr(response, "status", 200))
        raw_path.write_bytes(raw)
        payload = json.loads(raw.decode("utf-8"))
        rows = payload if isinstance(payload, list) else []
        observations: dict[str, dict[str, Any]] = {}
        duplicate_dates: list[str] = []
        for row_number, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping):
                continue
            trade_date = str(row.get("day") or "")[:10]
            if not trade_date:
                continue
            if trade_date in observations:
                duplicate_dates.append(trade_date)
                continue
            observations[trade_date] = {
                "row_number": row_number,
                "raw": dict(row),
                "normalized": {
                    "open": _number(row.get("open")),
                    "high": _number(row.get("high")),
                    "low": _number(row.get("low")),
                    "close": _number(row.get("close")),
                    "volume": (_number(row.get("volume")) / 10000.0) if _number(row.get("volume")) is not None else None,
                    "amount": None,
                },
            }
        dates = sorted(observations)
        return {
            "provider": "Sina CN_MarketData.getKLineData",
            "code": code,
            "provider_symbol": _provider_code(code),
            "url": url,
            "status_code": status,
            "raw_path": str(raw_path),
            "raw_sha256": _sha256_bytes(raw),
            "raw_size_bytes": len(raw),
            "rows": len(observations),
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
            "duplicate_dates": duplicate_dates,
            "observations": observations,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - preserve provider failure in audit output
        return {
            "provider": "Sina CN_MarketData.getKLineData",
            "code": code,
            "provider_symbol": _provider_code(code),
            "url": url,
            "status_code": None,
            "raw_path": str(raw_path),
            "raw_sha256": _sha256(raw_path) if raw_path.exists() else None,
            "raw_size_bytes": raw_path.stat().st_size if raw_path.exists() else 0,
            "rows": 0,
            "first_date": None,
            "last_date": None,
            "duplicate_dates": [],
            "observations": {},
            "error": f"{type(exc).__name__}: {exc}",
        }


def _compare(formal: Mapping[str, Any], provider_values: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    provider_values = provider_values or {}
    comparison: dict[str, dict[str, Any]] = {}
    for field in FIELDS:
        left, right = _number(formal.get(field)), _number(provider_values.get(field))
        if field == "amount":
            comparison[field] = {
                "formal": left,
                "sina_normalized": None,
                "delta_sina_minus_formal": None,
                "tolerance": None,
                "within_tolerance": None,
                "status": "provider_field_unavailable",
            }
            continue
        tolerance = PRICE_TOLERANCE if field != "volume" else VOLUME_TOLERANCE
        comparison[field] = {
            "formal": left,
            "sina_normalized": right,
            "delta_sina_minus_formal": (right - left) if left is not None and right is not None else None,
            "tolerance": tolerance,
            "within_tolerance": left is not None and right is not None and abs(right - left) <= tolerance,
            "status": "compared",
        }
    return comparison


def reconcile(*, input_path: Path = DEFAULT_INPUT, output_jsonl: Path | None = None, output_summary: Path | None = None, exclusion_jsonl: Path | None = None, raw_dir: Path | None = None, api_url: str = DEFAULT_API, cutoff: str = "2000-01-01", offline: bool = False) -> dict[str, Any]:
    root = output_jsonl.parent if output_jsonl else DEFAULT_ROOT
    output_jsonl = output_jsonl or root / "index_missing_keys_sina_reconciliation.jsonl.gz"
    output_summary = output_summary or root / "index_missing_keys_sina_reconciliation_summary.json"
    exclusion_jsonl = exclusion_jsonl or root / "index_missing_keys_pre2000_excluded.jsonl.gz"
    raw_dir = raw_dir or root / "raw_responses"
    candidates, excluded = load_candidates(input_path, cutoff=cutoff)
    provider_by_code: dict[str, dict[str, Any]] = {}
    for code in TARGET_CODES:
        raw_path = raw_dir / f"{code.replace('.', '_')}.json"
        if offline and raw_path.exists():
            raw = raw_path.read_bytes()
            try:
                rows = json.loads(raw.decode("utf-8"))
                observations: dict[str, dict[str, Any]] = {}
                for row_number, row in enumerate(rows if isinstance(rows, list) else [], start=1):
                    trade_date = str(row.get("day") or "")[:10]
                    if trade_date:
                        observations[trade_date] = {"row_number": row_number, "raw": dict(row), "normalized": {"open": _number(row.get("open")), "high": _number(row.get("high")), "low": _number(row.get("low")), "close": _number(row.get("close")), "volume": (_number(row.get("volume")) / 10000.0) if _number(row.get("volume")) is not None else None, "amount": None}}
                dates = sorted(observations)
                provider_by_code[code] = {"provider": "Sina CN_MarketData.getKLineData", "code": code, "provider_symbol": _provider_code(code), "url": _query_url(api_url, code), "status_code": 200, "raw_path": str(raw_path), "raw_sha256": _sha256_bytes(raw), "raw_size_bytes": len(raw), "rows": len(observations), "first_date": dates[0] if dates else None, "last_date": dates[-1] if dates else None, "duplicate_dates": [], "observations": observations, "error": None}
            except Exception as exc:  # noqa: BLE001
                provider_by_code[code] = {"provider": "Sina CN_MarketData.getKLineData", "code": code, "provider_symbol": _provider_code(code), "url": _query_url(api_url, code), "status_code": None, "raw_path": str(raw_path), "raw_sha256": _sha256(raw_path), "raw_size_bytes": raw_path.stat().st_size, "rows": 0, "first_date": None, "last_date": None, "duplicate_dates": [], "observations": {}, "error": f"offline_parse:{type(exc).__name__}: {exc}"}
        elif offline:
            provider_by_code[code] = {"provider": "Sina CN_MarketData.getKLineData", "code": code, "provider_symbol": _provider_code(code), "url": _query_url(api_url, code), "status_code": None, "raw_path": str(raw_path), "raw_sha256": None, "raw_size_bytes": 0, "rows": 0, "first_date": None, "last_date": None, "duplicate_dates": [], "observations": {}, "error": "offline_raw_response_missing"}
        else:
            provider_by_code[code] = fetch_code(code, api_url=api_url, raw_dir=raw_dir)
    counts: Counter[str] = Counter()
    by_code: dict[str, Counter[str]] = {}
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with _open_jsonl(output_jsonl, "wt") as handle:
        for candidate in sorted(candidates, key=lambda row: (row["code"], row["trade_date"])):
            code, trade_date = candidate["code"], candidate["trade_date"]
            provider = provider_by_code[code]
            observation = provider["observations"].get(trade_date)
            formal = candidate.get("candidate_formal_values") or candidate.get("formal_values") or {}
            comparison = _compare(formal, observation["normalized"] if observation else None)
            if observation is None:
                disposition = "sina_source_missing"
            elif all(comparison[field]["within_tolerance"] for field in OHLCV_FIELDS):
                disposition = "sina_ohlcv_consensus_amount_unavailable"
            else:
                disposition = "sina_ohlcv_residual_review_amount_unavailable"
            row = {
                "code": code,
                "trade_date": trade_date,
                "upstream_disposition": candidate.get("disposition"),
                "formal_values": formal,
                "sina_source": {
                    "provider": provider["provider"],
                    "provider_symbol": provider["provider_symbol"],
                    "url": provider["url"],
                    "status_code": provider["status_code"],
                    "raw_path": provider["raw_path"],
                    "raw_sha256": provider["raw_sha256"],
                    "source_row": observation["row_number"] if observation else None,
                    "raw_values": observation["raw"] if observation else None,
                    "normalized_values": observation["normalized"] if observation else None,
                    "coverage": {"first_date": provider["first_date"], "last_date": provider["last_date"], "provider_rows": provider["rows"]},
                    "error": provider["error"],
                },
                "field_comparison": comparison,
                "disposition": disposition,
                "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
            }
            counts[disposition] += 1
            by_code.setdefault(code, Counter())[disposition] += 1
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    exclusion_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with _open_jsonl(exclusion_jsonl, "wt") as handle:
        for row in excluded:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "schema_version": 1,
        "readonly": True,
        "gate": "review_only",
        "cutoff": cutoff,
        "input": str(input_path),
        "accepted_input_dispositions": ["baostock_missing_exact_key", "tencent_source_missing"],
        "output_jsonl": str(output_jsonl),
        "exclusion_jsonl": str(exclusion_jsonl),
        "candidate_rows_after_cutoff": len(candidates),
        "pre2000_excluded_rows": len(excluded),
        "pre2000_excluded_sha256": _sha256(exclusion_jsonl),
        "dispositions": dict(sorted(counts.items())),
        "by_code": {code: dict(sorted(counter.items())) for code, counter in sorted(by_code.items())},
        "provider_inventory": [
            {k: value.get(k) for k in ("provider", "code", "provider_symbol", "url", "status_code", "raw_path", "raw_sha256", "raw_size_bytes", "rows", "first_date", "last_date", "duplicate_dates", "error")}
            for value in provider_by_code.values()
        ],
        "comparison_contract": {
            "exact_key": "code + trade_date",
            "sina_volume": "raw volume / 10000 -> formal lots (observed scale)",
            "sina_amount": "provider does not return amount; amount remains unavailable",
            "price_abs_tolerance": PRICE_TOLERANCE,
            "volume_abs_tolerance": VOLUME_TOLERANCE,
        },
        "policy": {
            "pre2000_not_tracked": True,
            "pre2000_raw_source_files_not_deleted": True,
            "formal_missing_rows_are_not_created": True,
            "source_consensus_is_review_only": True,
            "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
        },
    }
    output_summary.parent.mkdir(parents=True, exist_ok=True)
    output_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-jsonl", type=Path)
    parser.add_argument("--output-summary", type=Path)
    parser.add_argument("--exclusion-jsonl", type=Path)
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--api-url", default=DEFAULT_API)
    parser.add_argument("--cutoff", default="2000-01-01")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    summary = reconcile(input_path=args.input, output_jsonl=args.output_jsonl, output_summary=args.output_summary, exclusion_jsonl=args.exclusion_jsonl, raw_dir=args.raw_dir, api_url=args.api_url, cutoff=args.cutoff, offline=args.offline)
    print(json.dumps({"candidate_rows_after_cutoff": summary["candidate_rows_after_cutoff"], "pre2000_excluded_rows": summary["pre2000_excluded_rows"], "dispositions": summary["dispositions"], "gate": summary["gate"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
