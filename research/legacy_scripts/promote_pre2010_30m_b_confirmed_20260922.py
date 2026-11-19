import clickhouse_connect
import time
c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
c.command('SET max_partitions_per_insert_block=1000')
keys='stock.pre2010_30m_b_selected_clean_keys_20260922'; cand='stock.pre2010_30m_b_selected_candidate_20260922_clean'; formal='stock.kline_minute_30'; backup='stock.kline_minute_30_repair_backup_b_confirmed_20260922'
c.command(f'DROP TABLE IF EXISTS {backup}')
c.command(f'CREATE TABLE {backup} AS {formal}')
c.command(f'INSERT INTO {backup} SELECT f.* FROM {formal} f INNER JOIN {keys} k ON f.code=k.code AND toDate(f.datetime)=k.trade_date')
old=int(c.query(f'SELECT count() FROM {backup}').result_rows[0][0])
c.command(f'ALTER TABLE {formal} DELETE WHERE (code,toDate(datetime)) IN (SELECT code,trade_date FROM {keys})')
pending=1
for _ in range(240):
    pending=int(c.query("SELECT count() FROM system.mutations WHERE database='stock' AND table='kline_minute_30' AND is_done=0").result_rows[0][0])
    if pending==0: break
    time.sleep(1)
if pending: raise RuntimeError('30m B delete mutation still pending')
c.command(f'INSERT INTO {formal} SELECT * FROM {cand}')
new=int(c.query(f'SELECT count() FROM {cand}').result_rows[0][0])
print({'selected_keys':int(c.query(f'SELECT count() FROM {keys}').result_rows[0][0]),'backup_rows':old,'inserted_rows':new})
