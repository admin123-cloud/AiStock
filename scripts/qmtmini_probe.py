from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_fetcher.sources.qmtmini_client import QmtMiniConfig, QmtMiniMarketClient, QmtMiniTradingClient
from execution.qmtmini_gateway import QmtMiniOrderGateway


def _json_default(value: Any) -> str:
    return str(value)


def _print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=_json_default))


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    config = QmtMiniConfig.from_env()
    summary: dict[str, Any] = {
        "ok": True,
        "config": {
            "qmt_root": str(config.qmt_root),
            "userdata_dir": str(config.userdata_dir),
            "quote_host": config.quote_host,
            "quote_port": config.quote_port,
        },
        "market": {},
        "trading": {},
        "dry_run_order": {},
    }

    try:
        market = QmtMiniMarketClient(config)
        summary["market"]["connect"] = market.connect()
        tick = market.get_full_tick([args.stock_code])
        quote = tick.get(args.stock_code) or {}
        summary["market"]["full_tick"] = {
            "ok": bool(quote),
            "stock_code": args.stock_code,
            "timetag": quote.get("timetag"),
            "lastPrice": quote.get("lastPrice"),
        }
        if args.download_history:
            summary["market"]["history"] = market.history_summary(
                args.stock_code,
                args.periods,
                args.start_time,
                args.end_time,
            )
    except Exception as exc:
        summary["ok"] = False
        summary["market"]["error"] = f"{type(exc).__name__}: {exc}"

    try:
        trading = QmtMiniTradingClient(config)
        connect_status = trading.connect()
        summary["trading"]["connect"] = connect_status
        if connect_status.get("ok"):
            summary["trading"]["accounts"] = trading.query_account_infos(masked=True)
            summary["trading"]["snapshot"] = trading.account_snapshot(include_sensitive=False)
        trading.close()
    except Exception as exc:
        summary["ok"] = False
        summary["trading"]["error"] = f"{type(exc).__name__}: {exc}"

    gateway = QmtMiniOrderGateway()
    summary["dry_run_order"] = gateway.dry_run_order(
        side="buy",
        stock_code=args.stock_code,
        volume=args.dry_run_volume,
        price=args.dry_run_price,
        order_remark="qmtmini_probe",
    )
    if not summary["dry_run_order"].get("ok"):
        summary["ok"] = False

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe local QMT Mini market/trading connectivity without real orders.")
    parser.add_argument("--stock-code", default="600519.SH")
    parser.add_argument("--start-time", default="20260601")
    parser.add_argument("--end-time", default="20260630")
    parser.add_argument("--periods", nargs="+", default=["1d", "5m", "15m", "30m", "60m"])
    parser.add_argument("--download-history", action="store_true")
    parser.add_argument("--dry-run-volume", type=int, default=100)
    parser.add_argument("--dry-run-price", type=float, default=1.0)
    args = parser.parse_args()

    summary = run_probe(args)
    _print_json(summary)
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
