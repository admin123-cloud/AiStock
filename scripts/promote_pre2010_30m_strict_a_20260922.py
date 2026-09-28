import clickhouse_connect
import time

c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
selected='stock.pre2010_30m_strict_a_selected_20260922'; cand='stock.pre2010_30m_strict_a_candidate_20260922'; formal='stock.kline_minute_30'; backup='stock.kline_minute_30_repair_backup_strict_a_20260922'
c.command(f'DROP TABLE IF EXISTS {backup}')
c.command(f'CREATE TABLE {backup} AS {formal}')
c.command(f'INSERT INTO {backup} SELECT f.* FROM {formal} f INNER JOIN {selected} k ON f.code=k.code AND toDate(f.datetime)=k.trade_date')
old=int(c.query(f'SELECT count() FROM {backup}').result_rows[0][0])
c.command(f"ALTER TABLE {formal} DELETE WHERE (code,toDate(datetime)) IN (SELECT code,trade_date FROM {selected})")
pending=1
for _ in range(120):
    pending=int(c.query("SELECT count() FROM system.mutations WHERE database='stock' AND table='kline_minute_30' AND is_done=0").result_rows[0][0])
    if pending==0: break
    time.sleep(1)
if pending: raise RuntimeError('30m delete mutation still pending')
c.command(f'INSERT INTO {formal} SELECT * FROM {cand}')
new=int(c.query(f'SELECT count() FROM {cand}').result_rows[0][0])
dup=int(c.query(f'SELECT count() FROM (SELECT formal.code,formal.datetime,count() n FROM {formal} formal INNER JOIN {selected} k ON formal.code=k.code AND toDate(formal.datetime)=k.trade_date GROUP BY formal.code,formal.datetime HAVING n>1)').result_rows[0][0])
print({'selected_keys':int(c.query(f'SELECT count() FROM {selected}').result_rows[0][0]),'backup_rows':old,'inserted_rows':new,'duplicate_keys_after':dup})
