import sqlite3
from datetime import datetime
import pandas as pd
from scripts import qmt_fullpush_intraday_aggregator as collector
from utils.qmt_universe import qmt_universe_filter_sql


def test_real_sql_or_does_not_bypass_quit_and_realtime_dates(monkeypatch):
    db=sqlite3.connect(':memory:')
    db.create_function('positionUTF8',2,lambda value,needle:value.find(needle)+1)
    db.create_function('toDate',1,lambda value:value)
    db.execute('CREATE TABLE stocks (code TEXT,name TEXT,type TEXT,quit INTEGER,list_date TEXT,delist_date TEXT)')
    day=datetime.now(collector.SH_TZ).date().isoformat()
    db.executemany('INSERT INTO stocks VALUES(?,?,?,?,?,?)',[
        ('600001.SH','active','stock',0,'2000-01-01',None),
        ('600002.SH','retired','stock',1,'2000-01-01','2020-01-01'),
        ('600003.SH','future','stock',0,'2099-01-01',None),
        ('600004.SH','expires_today','stock',0,'2000-01-01',day),
        ('000001.SH','active index','index',0,None,None),
        ('000002.SH','retired index','index',1,None,'2020-01-01'),
    ])
    monkeypatch.setattr(collector,'clickhouse_query_df',lambda sql:pd.read_sql_query(sql.replace(' FINAL',''),db))
    assert set(collector.load_codes('stock,index','',0,False,''))=={'600001.SH','000001.SH'}
    # The shared filter alone retains expired/future rows for historical callers.
    historical=db.execute('SELECT count(*) FROM stocks WHERE '+qmt_universe_filter_sql('stock,index')).fetchone()[0]
    assert historical==6
    current=db.execute('SELECT code FROM stocks WHERE '+qmt_universe_filter_sql('stock,index')+' AND quit=0').fetchall()
    assert ('600002.SH',) not in current
    db.close()
