import csv, json
from datetime import date, datetime
from pathlib import Path
import clickhouse_connect

CSV = Path(r'F:\AiStock\data\data\runtime\purchased_history\pre2010_60m_a_vs_formal_20260922.csv')
FORMAL = 'stock.kline_minute_60'
SOURCE = 'stock.pre2010_a_grade_60m_reaggregate_candidate_20260922_formal5m_v2_clean'
KEYS = 'stock.pre2010_60m_clear_better_keys_20260922'
CAND = 'stock.pre2010_60m_clear_better_candidate_20260922'
BACKUP = 'stock.kline_minute_60_repair_backup_refit_a2_20260922'
OLD_RUN = 'purchased_history_review_20260922_refit_a_b_confirmed_20260922_v3_no_trade_resolved'
NEW_RUN = 'purchased_history_review_20260922_refit_a_b_confirmed_20260922_v4_60m_a2'

c = clickhouse_connect.get_client(host='localhost', port=8123, username='default')
c.command('SET max_partitions_per_insert_block=1000')
prior = {(str(x[0]), str(x[1])) for x in c.query("SELECT code,toDate(datetime) FROM stock.pre2010_refit_b_confirmed_60m_20260922 GROUP BY code,toDate(datetime)").result_rows}
chosen = []
with CSV.open(encoding='utf-8-sig', newline='') as f:
    for r in csv.DictReader(f):
        key = (r['k.code'], r['k.trade_date'])
        if key in prior:
            continue
        cp, cv, ca, fp, fv, fa = [float(r[x]) for x in ('cp','cv','ca','fp','fv','fa')]
        if float(r['cnt']) >= 4 and fp <= 0.02 and fv <= 0.15 and fa <= 0.25 and cp + cv + ca < fp + fv + fa:
            chosen.append((key[0], date.fromisoformat(key[1])))
chosen = list(dict.fromkeys(chosen))
for t in [KEYS, CAND]:
    c.command(f'DROP TABLE IF EXISTS {t}')
c.command(f'CREATE TABLE {KEYS} (code String,trade_date Date) ENGINE=MergeTree ORDER BY (code,trade_date)')
if chosen:
    c.insert(KEYS, chosen, column_names=['code','trade_date'])
c.command(f'CREATE TABLE {CAND} ENGINE=MergeTree ORDER BY (code,datetime) SETTINGS allow_nullable_key=1 AS SELECT * FROM {FORMAL} LIMIT 0')
c.command(f'INSERT INTO {CAND} SELECT s.* FROM {SOURCE} s INNER JOIN {KEYS} k ON s.code=k.code AND toDate(s.datetime)=k.trade_date')
cand_rows, cand_unique = c.query(f'SELECT count(),uniqExact(tuple(code,datetime)) FROM {CAND}').result_rows[0]
if int(cand_rows) != int(cand_unique) or int(cand_rows) != len(chosen) * 4:
    raise RuntimeError(f'candidate gate failed {cand_rows} {cand_unique} keys={len(chosen)}')
c.command(f'DROP TABLE IF EXISTS {BACKUP}')
c.command(f'CREATE TABLE {BACKUP} AS {FORMAL}')
c.command(f'INSERT INTO {BACKUP} SELECT f.* FROM {FORMAL} f INNER JOIN {KEYS} k ON f.code=k.code AND toDate(f.datetime)=k.trade_date')
c.command(f'ALTER TABLE {FORMAL} DELETE WHERE (code,toDate(datetime)) IN (SELECT code,trade_date FROM {KEYS}) SETTINGS mutations_sync=1')
c.command(f'INSERT INTO {FORMAL} SELECT * FROM {CAND}')
post = c.query(f'SELECT count(),uniqExact(tuple(code,datetime)),countIf(open<=0 OR high<=0 OR low<=0 OR close<=0 OR high<greatest(open,close) OR low>least(open,close) OR volume<0 OR amount<0) FROM {FORMAL} WHERE (code,toDate(datetime)) IN (SELECT code,trade_date FROM {KEYS})').result_rows[0]
if int(post[0]) != int(cand_rows) or int(post[1]) != int(cand_unique) or int(post[2]) != 0:
    raise RuntimeError(f'post gate failed {post}')
cols = 'run_id,review_key,issue_type,source,code,trade_date,period,security_type,expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,evidence_ref,evidence_sha256,observed_at,reason'
c.command(f'''INSERT INTO stock.purchased_history_data_review_items ({cols})
SELECT '{NEW_RUN}',review_key,issue_type,source,code,trade_date,period,security_type,expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,evidence_ref,evidence_sha256,observed_at,reason
FROM stock.purchased_history_data_review_items WHERE run_id='{OLD_RUN}'
AND NOT (period='60m' AND (code,trade_date) IN (SELECT code,trade_date FROM {KEYS}))''')
counts = c.query(f"SELECT period,issue_type,count() FROM stock.purchased_history_data_review_items WHERE run_id='{NEW_RUN}' GROUP BY period,issue_type ORDER BY period,issue_type").result_rows
result = {'old_run': OLD_RUN, 'run_id': NEW_RUN, 'selected_code_days': len(chosen), 'candidate_rows': int(cand_rows), 'backup': BACKUP, 'formal_post': list(post), 'rows': sum(int(x[2]) for x in counts), 'counts': {f'{x[0]}:{x[1]}': int(x[2]) for x in counts}, 'completed_at': datetime.now().astimezone().isoformat()}
out = Path(r'F:\AiStock\data\data\runtime\purchased_history\60m_clear_better_promotion_20260922.json')
out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False, indent=2))
