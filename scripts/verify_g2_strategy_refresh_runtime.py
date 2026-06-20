from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


def _get_json(url: str, timeout: int = 20) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as resp:
        data = resp.read().decode("utf-8", errors="replace")
    payload = json.loads(data)
    return payload if isinstance(payload, dict) else {"value": payload}


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify live G2 30m strategy refresh runtime endpoints.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Backend base URL")
    args = parser.parse_args()

    base_url = str(args.base_url).rstrip("/")
    errors: list[str] = []
    result: dict[str, Any] = {
        "ok": True,
        "base_url": base_url,
        "checks": {},
    }

    try:
        status = _get_json(f"{base_url}/api/trading/gen2/strategy-refresh/status")
        result["checks"]["strategy_refresh_status"] = status
        if not status.get("enabled"):
            errors.append("strategy refresh is not enabled")
        if not status.get("next_run_time"):
            errors.append("strategy refresh has no next_run_time")
    except HTTPError as exc:
        errors.append(f"strategy-refresh status endpoint returned HTTP {exc.code}")
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        errors.append(f"strategy-refresh status endpoint unavailable: {exc}")

    query = urlencode({"strategy_code": "g2_alpha191_volume5_keep80_runup"})
    try:
        backtest = _get_json(f"{base_url}/api/trading/gen2/backtest?{query}")
        metrics = backtest.get("metrics") if isinstance(backtest.get("metrics"), dict) else {}
        result["checks"]["backtest"] = {
            "available": bool(backtest.get("available")),
            "total_return_text": metrics.get("total_return_text"),
            "trade_count": metrics.get("trade_count"),
            "signal_count": metrics.get("signal_count"),
            "run_dir": backtest.get("run_dir"),
        }
        if metrics.get("total_return_text") != "331.32%":
            errors.append("G2 recovered 331.32% backtest is not the active runtime result")
    except HTTPError as exc:
        errors.append(f"backtest endpoint returned HTTP {exc.code}")
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        errors.append(f"backtest endpoint unavailable: {exc}")

    if errors:
        result["ok"] = False
        result["errors"] = errors

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
