"""Inventory the committed QMT interfaces and current host task ownership."""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.paths import runtime_path

MARKET = {
    "connect", "get_full_tick", "get_market_data", "get_market_data_tdx_shape",
    "get_sector_list", "get_stock_list_in_sector", "get_instrument_detail",
    "get_instrument_detail_list", "download_history_contracts", "download_history_data",
    "download_history_data2", "get_trading_dates", "get_market_last_trade_date",
    "download_sector_data", "subscribe_whole_quote", "unsubscribe_quote"}


def inventory():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    files = subprocess.check_output(["git", "ls-files", "*.py"], cwd=ROOT, text=True).splitlines()
    entries, errors = [], []
    for filename in files:
        if filename.split("/", 1)[0] not in {"data_fetcher", "services", "api", "core", "utils", "execution", "scheduler", "scripts"}:
            continue
        source = subprocess.check_output(["git", "show", f"HEAD:{filename}"], cwd=ROOT)
        try:
            text = source.decode("utf-8-sig")
            if not any(marker in text for marker in ["xtquant", "QmtMiniMarketClient", "qmtmini_client", "_ensure_client()"]):
                continue
            tree = ast.parse(text)
        except Exception as exc:
            errors.append({"file": filename, "error": str(exc)})
            continue
        market_module = "QmtMiniMarketClient" in text or "xtdata" in text or "_ensure_client()" in text
        trading_module = "QmtMiniTradingClient" in text or "xttrader" in text
        calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in MARKET:
                calls.append({"method": node.func.attr, "line": node.lineno,
                              "receiver": ast.unparse(node.func.value)})
        entries.append({"file": filename, "sha256": hashlib.sha256(source).hexdigest(),
                        "scope": "market" if market_module else ("account_or_orders" if trading_module else "indirect_or_labels"),
                        "contains_trading_client": trading_module,
                        "calls": sorted(calls, key=lambda item: item["line"])})
    methods = Counter(call["method"] for entry in entries if entry["scope"] == "market" for call in entry["calls"])
    return {"revision": revision, "root": str(ROOT), "basis": "committed baseline, static call sites",
            "methods": dict(sorted(methods.items())), "files": entries, "parse_errors": errors,
            "limitation": "Call sites do not prove runtime frequency; optional/legacy branches remain listed."}


if __name__ == "__main__":
    result = inventory()
    output = runtime_path("qmt_builtin_market", "baseline_interface_inventory.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "revision": result["revision"], "methods": result["methods"],
                      "market_files": sum(e["scope"] == "market" for e in result["files"]),
                      "account_files": sum(e["scope"] == "account_or_orders" for e in result["files"]),
                      "parse_errors": result["parse_errors"]}, ensure_ascii=False))
