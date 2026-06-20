"""Read-only inspection for PTrade local strategy persistence DB."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict


DB_PATH = Path(r"C:\xczq\ptrade_yfz\Users\Default\Strategy\FLYSERVER_610007046_persist.db")
KEYWORDS = ("AiStockBridge", "ptrade_file_bridge_strategy", "分钟")


def main() -> int:
    if not DB_PATH.exists():
        print(json.dumps({"ok": False, "reason": "db_missing", "path": str(DB_PATH)}, ensure_ascii=False, indent=2))
        return 1

    out: Dict[str, Any] = {"ok": False, "path": str(DB_PATH), "tables": [], "keyword_hits": []}
    try:
        header = DB_PATH.read_bytes()[:64]
        out["size"] = DB_PATH.stat().st_size
        out["header_hex"] = header.hex(" ")
        out["sqlite_header"] = header.startswith(b"SQLite format 3\x00")
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        out["header_error"] = repr(exc)

    if not out.get("sqlite_header"):
        out["reason"] = "not_sqlite_database"
        out["message"] = "PTrade local strategy persistence file is not a plain SQLite database; do not write it directly."
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 2

    try:
        con = sqlite3.connect(str(DB_PATH))
    except sqlite3.DatabaseError as exc:
        out["reason"] = "sqlite_open_failed"
        out["message"] = str(exc)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 2
    cur = con.cursor()
    try:
        tables = cur.execute(
            "select name,type from sqlite_master where type in ('table','view') order by name"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        con.close()
        out["reason"] = "sqlite_schema_read_failed"
        out["message"] = str(exc)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 2
    out["ok"] = True
    for name, typ in tables:
        entry = {"name": name, "type": typ, "columns": [], "row_count": None}
        try:
            entry["columns"] = [row[1] for row in cur.execute(f"pragma table_info({name})").fetchall()]
            entry["row_count"] = cur.execute(f"select count(*) from {name}").fetchone()[0]
        except Exception as exc:  # noqa: BLE001 - diagnostic script
            entry["error"] = repr(exc)
        out["tables"].append(entry)

        try:
            rows = cur.execute(f"select * from {name} limit 100").fetchall()
            for idx, row in enumerate(rows):
                text = json.dumps(row, ensure_ascii=False, default=str)
                if any(keyword in text for keyword in KEYWORDS):
                    out["keyword_hits"].append({"table": name, "row_index": idx, "text": text[:1000]})
        except Exception:
            pass

    con.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
