"""
Sync V4/V4.2 trading artifacts into ClickHouse for API consumption.

Usage:
  python scripts/sync_trading_artifacts.py
  python scripts/sync_trading_artifacts.py --v42-variants 15m,30m
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime
from typing import Any, Dict, List

import pandas as pd

from utils.market_warehouse import clickhouse_client, clickhouse_query_df

TABLE_NAME = "trading_strategy_artifacts"
DEFAULT_V4_DIR = r"C:\Users\Administrator\Documents\Codex\2026-04-23-aistock-100000-a-3-a-t\output_v4"
DEFAULT_V42_DIR = r"C:\Users\Administrator\Documents\Codex\2026-04-23-aistock-100000-a-3-a-t\output_v42"


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _ensure_table() -> None:
    """Create the ClickHouse table if it doesn't exist (ReplacingMergeTree for upsert semantics)."""
    client = clickhouse_client()
    client.command(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            strategy_version String,
            artifact_key String,
            payload_json String,
            updated_at DateTime DEFAULT now()
        ) ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (strategy_version, artifact_key)
    """)


def _read_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_csv_records(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    df = pd.read_csv(path, dtype={"code": str})
    for col in ["date", "trade_date", "signal_date", "buy_time", "sell_time"]:
        if col in df.columns:
            df[col] = df[col].astype(str)
            df.loc[df[col].str.lower().isin(["nan", "nat", "none"]), col] = None
    df = df.where(pd.notna(df), None)
    return df.to_dict(orient="records")


def _upsert(strategy_version: str, artifact_key: str, payload: Any) -> None:
    """Insert or replace a trading artifact in ClickHouse."""
    payload_json = json.dumps(payload, ensure_ascii=False, default=_json_default)
    client = clickhouse_client()
    client.command(
        f"ALTER TABLE {TABLE_NAME} DELETE WHERE strategy_version = ? AND artifact_key = ?",
        [strategy_version, artifact_key],
    )
    client.insert(
        TABLE_NAME,
        [[strategy_version, artifact_key, payload_json]],
        column_names=["strategy_version", "artifact_key", "payload_json"],
    )


def _sync_one(strategy_version: str, output_dir: str, files: Dict[str, str], strict: bool = True) -> Dict[str, Any]:
    artifacts: Dict[str, Any] = {}
    missing: List[str] = []

    for key, filename in files.items():
        full_path = os.path.join(output_dir, filename)
        if not os.path.exists(full_path):
            missing.append(full_path)
            continue
        if filename.lower().endswith(".json"):
            artifacts[key] = _read_json(full_path)
        elif filename.lower().endswith(".csv"):
            artifacts[key] = _read_csv_records(full_path)
        else:
            with open(full_path, "r", encoding="utf-8") as f:
                artifacts[key] = f.read()

    if strict and missing:
        raise FileNotFoundError(f"Missing required artifacts for {strategy_version}: {missing}")

    # optional consistency
    artifacts.setdefault("consistency", {})

    artifacts["meta"] = {
        "output_dir": output_dir,
        "strategy_version": strategy_version,
        "source_files": files,
        "synced_at": datetime.now().isoformat(timespec="seconds"),
    }

    for key, payload in artifacts.items():
        _upsert(strategy_version=strategy_version, artifact_key=key, payload=payload)

    return {
        "strategy_version": strategy_version,
        "output_dir": output_dir,
        "artifact_keys": sorted(artifacts.keys()),
        "missing_files": missing,
        "rows": {
            "decisions": len(artifacts.get("decisions", []) if isinstance(artifacts.get("decisions"), list) else []),
            "holdings": len(artifacts.get("holdings", []) if isinstance(artifacts.get("holdings"), list) else []),
            "trades": len(artifacts.get("trades", []) if isinstance(artifacts.get("trades"), list) else []),
            "curve": len(artifacts.get("curve", []) if isinstance(artifacts.get("curve"), list) else []),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync V4/V4.2 trading artifacts to MySQL")
    parser.add_argument("--v4-dir", default=os.getenv("AISTOCK_V4_OUTPUT_DIR", DEFAULT_V4_DIR))
    parser.add_argument("--v42-dir", default=os.getenv("AISTOCK_V42_OUTPUT_DIR", DEFAULT_V42_DIR))
    parser.add_argument("--v42-variants", default="15m", help="comma-separated: 15m,30m")
    parser.add_argument("--skip-v4", action="store_true")
    parser.add_argument("--skip-v42", action="store_true")
    parser.add_argument("--allow-missing", action="store_true", help="continue even if files missing")
    args = parser.parse_args()

    _ensure_table()

    reports: List[Dict[str, Any]] = []

    if not args.skip_v4:
        v4_files = {
            "summary": "summary_v4.json",
            "consistency": "v4_consistency_stats.json",
            "decisions": "daily_decisions_v4.csv",
            "holdings": "daily_holdings_v4.csv",
            "trades": "daily_trades_v4.csv",
            "curve": "equity_curve_v4.csv",
        }
        reports.append(_sync_one("v4", args.v4_dir, v4_files, strict=not args.allow_missing))

    if not args.skip_v42:
        variants = [x.strip().lower() for x in str(args.v42_variants).split(",") if x.strip()]
        for variant in variants:
            if variant not in {"15m", "30m"}:
                raise ValueError(f"Unsupported v4.2 variant: {variant}")
            suffix = variant
            ver = f"v4.2_{suffix}"
            v42_files = {
                "summary": f"summary_v42_{suffix}.json",
                "decisions": f"daily_decisions_v42_{suffix}.csv",
                "holdings": f"daily_holdings_v42_{suffix}.csv",
                "trades": f"daily_trades_v42_{suffix}.csv",
                "curve": f"equity_curve_v42_{suffix}.csv",
            }
            reports.append(_sync_one(ver, args.v42_dir, v42_files, strict=not args.allow_missing))

    print(json.dumps({"ok": True, "reports": reports}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
