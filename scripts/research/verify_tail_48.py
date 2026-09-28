"""Verify all code-days in a staged tail payload are now 48/48 in the formal table."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client

ap = argparse.ArgumentParser()
ap.add_argument('--staged', type=Path, required=True)
a = ap.parse_args()
d = json.loads(a.staged.read_text(encoding='utf-8'))
# accepted includes both newly staged rows and already-complete formal days.
# Verifying only staged_rows silently turns an idempotent rerun into a 0-key
# success, so the accepted code-day list is the authoritative gate.
pairs = sorted({(r['code'], r['trade_date'][:10]) for r in d.get('accepted', [])})
c = clickhouse_client()
bad = []
for code, day in pairs:
    n = c.query('SELECT count() FROM kline_minute_5 WHERE toDate(datetime)={d:Date} AND code={c:String}',
                parameters={'d': day, 'c': code}, settings={'max_threads': 1}).result_rows[0][0]
    if n != 48:
        bad.append((code, day, n))
print(json.dumps({'code_days': len(pairs), 'not_48': bad or 'none_all_48'}, ensure_ascii=False))
sys.exit(1 if bad else 0)
