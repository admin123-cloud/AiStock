"""Local-only bridge exposing read-only QMT ticks and account snapshots.

It lets the Docker backend inspect the Windows-host QMT account without
shipping ``xtquant`` into the container.  It deliberately exposes no order
submission endpoint.
"""

from __future__ import annotations

import json
import os
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


HOST = os.environ.get("AISTOCK_QMT_TICK_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("AISTOCK_QMT_TICK_BRIDGE_PORT", "8766"))
QMT_HOST = os.environ.get("AISTOCK_QMT_TICK_HOST", "127.0.0.1")
QMT_PORT = int(os.environ.get("AISTOCK_QMT_TICK_PORT", "58610"))


def _normalize_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", text):
        return text
    if not re.fullmatch(r"\d{6}", text):
        return ""
    if text.startswith(("4", "8")):
        return f"{text}.BJ"
    return f"{text}.SH" if text.startswith(("5", "6", "9")) else f"{text}.SZ"


def _full_tick(codes: list[str]) -> dict[str, Any]:
    from xtquant import xtdata  # type: ignore

    xtdata.enable_hello = False
    # QMT Mini versions differ: connect() may return 0, None, or an empty
    # value on success.  The actual full-tick response is the useful gate.
    xtdata.connect(QMT_HOST, QMT_PORT)
    return xtdata.get_full_tick(codes) or {}


def _account_snapshot() -> dict[str, Any]:
    from data_fetcher.sources.qmtmini_client import QmtMiniTradingClient

    with QmtMiniTradingClient() as client:
        return client.account_snapshot(include_sensitive=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _write(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._write(HTTPStatus.OK, {"ok": True, "mode": "read_only_qmt_bridge", "qmt_port": QMT_PORT})
            return
        if parsed.path == "/account-snapshot":
            try:
                self._write(HTTPStatus.OK, {"ok": True, "snapshot": _account_snapshot()})
            except Exception as exc:
                self._write(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "message": f"{type(exc).__name__}: {exc}"})
            return
        if parsed.path != "/full-tick":
            self._write(HTTPStatus.NOT_FOUND, {"ok": False, "message": "not found"})
            return
        raw_codes = parse_qs(parsed.query).get("codes", [""])[0]
        codes = list(dict.fromkeys(_normalize_code(code) for code in raw_codes.split(",")))
        codes = [code for code in codes if code]
        if not codes:
            self._write(HTTPStatus.BAD_REQUEST, {"ok": False, "message": "codes is required"})
            return
        try:
            self._write(HTTPStatus.OK, {"ok": True, "ticks": _full_tick(codes), "qmt_port": QMT_PORT})
        except Exception as exc:
            self._write(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "message": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    print(f"QMT read-only tick bridge listening on http://{HOST}:{PORT}, QMT={QMT_HOST}:{QMT_PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
