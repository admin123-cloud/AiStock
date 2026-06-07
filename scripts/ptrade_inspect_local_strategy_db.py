"""Read-only inspection for PTrade local strategy persistence DB."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


DB_PATH = Path(r"C:\xczq\ptrade_yfz\Users\Default\Strategy\FLYSERVER_610007046_persist.db")
KEYWORDS = ("AiStockBridge", "ptrade_file_bridge_strategy", "分钟")


def main() -> int:
    if not DB_PATH.exists():
        print(json.dumps({"ok": False, "reason": "db_missing", "path": str(DB_PATH)}, ensure_ascii=False, indent=2))
        return 1

    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()
    tables = cur.execute(
        "select name,type from sqlite_master where type in ('table','view') order by name"
    ).fetchall()
    out = {"ok": True, "path": str(DB_PATH), "tables": [], "keyword_hits": []}
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
