"""Minimal PTrade heartbeat smoke strategy.

This file intentionally avoids os/pathlib and order APIs. It only proves that
PTrade can execute a strategy and write to the AiStock bridge status file.
"""

import json
import time


STATUS_FILE = r"F:\Stock\AiStock\data\runtime\ptrade_bridge\status\latest.json"


def _write_status(event):
    payload = {
        "ok": True,
        "event": event,
        "strategy": "ptrade_heartbeat_smoke_strategy",
        "timestamp": time.time(),
        "message": "ptrade heartbeat smoke",
    }
    with open(STATUS_FILE, "w") as f:
        json.dump(payload, f)


def initialize(context):
    _write_status("initialize")


def handle_data(context, data):
    _write_status("handle_data")
