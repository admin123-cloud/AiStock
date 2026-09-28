"""Read-only check: can the 4 project_daily_missing_for_5m keys be derived?

For each queued index code-day, report whether the isolated purchased 5m stage
holds a first bar at 09:35 and a last bar at 15:00 with all-positive OHLC.  If so
a daily OHLC can be derived from the first open, last close, max high and min low.

CRITICAL CAVEAT (verified 2026-09-18)
-------------------------------------
A derived daily bar is only as good as its coverage, and these code-days are
NOT clean trading days.  ``399107.SZ / 2003-07-03`` carries 30 of 48 bars with
an interior hole 10:50-13:45 where the vendor froze ``open`` at 429.2 and set
``close``/``low`` to 0.  The surviving first bar (09:35) and last bar (15:00)
do give a usable open/close, but ``high``/``low`` are computed over partial
coverage, so they understate the true intraday range.  Treat a derived daily bar
as "usable but range-degraded", and never as equivalent to a fully covered day.

Writes nothing to ClickHouse.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client

STAGE = "stock.purchased_history_minute_5_stage"
KEYS = [
    ("399107.SZ", "2003-07-03"),
    ("399108.SZ", "2003-07-03"),
    ("399004.SZ", "2004-03-03"),
    ("399352.SZ", "2005-09-15"),
]


def main() -> int:
    client = clickhouse_client()
    out = []
    for code, day in KEYS:
        rows = client.query(
            f"""
            SELECT substring(toString(datetime), 12, 5) AS t, open, high, low, close,
                   source_volume_lots, source_amount_yuan
            FROM {STAGE}
            WHERE code = %(code)s AND toDate(datetime) = toDate(%(day)s)
            ORDER BY datetime
            """,
            parameters={"code": code, "day": day},
        ).result_rows
        times = [str(r[0]) for r in rows]
        opens = [float(r[1]) for r in rows]
        highs = [float(r[2]) for r in rows]
        lows = [float(r[3]) for r in rows]
        closes = [float(r[4]) for r in rows]
        vols = [float(r[5] or 0) for r in rows]
        amts = [float(r[6] or 0) for r in rows]
        allpos = all(v > 0 for v in opens + highs + lows + closes)
        has_first = bool(times) and times[0] == "09:35"
        has_last = bool(times) and times[-1] == "15:00"
        # Find interior gap: minutes covered vs the canonical 48 labels.
        canonical = []
        for total in range(9 * 60 + 35, 11 * 60 + 30 + 1, 5):
            canonical.append(f"{total // 60:02d}:{total % 60:02d}")
        for total in range(13 * 60 + 5, 15 * 60 + 1, 5):
            canonical.append(f"{total // 60:02d}:{total % 60:02d}")
        present = set(times)
        missing = [t for t in canonical if t not in present]
        out.append(
            {
                "code": code,
                "trade_date": day,
                "stage_bars": len(rows),
                "first_label": times[0] if times else None,
                "last_label": times[-1] if times else None,
                "has_0935_open": has_first,
                "has_1500_close": has_last,
                "all_ohlc_positive": allpos,
                "missing_labels": missing,
                "missing_count": len(missing),
                "derivable_daily": {
                    "open": opens[0] if opens else None,
                    "high": max(highs) if highs else None,
                    "low": min(lows) if lows else None,
                    "close": closes[-1] if closes else None,
                    "volume": sum(vols),
                    "amount": sum(amts),
                }
                if (has_first and has_last and allpos)
                else None,
            }
        )

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
