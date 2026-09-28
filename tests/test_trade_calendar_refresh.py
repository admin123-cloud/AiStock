import re
import sqlite3

from scripts import sync_trade_calendar as calendar


def test_history_only_refresh_keeps_future_calendar(monkeypatch):
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE trade_calendar(trade_date TEXT,market TEXT,is_trading INT)')
    db.executemany('INSERT INTO trade_calendar VALUES (?,?,?)',[
        ('2026-09-04','SH',1),('2026-09-05','SH',1),
        ('2026-09-10','SH',1),('2026-10-01','SH',0)])

    class Client:
        def command(self,sql,parameters=None):
            if 'CREATE TABLE IF NOT EXISTS' in sql:
                return
            if sql.startswith('RENAME TABLE'):
                for old,new in re.findall(r'(\w+) TO (\w+)',sql):
                    db.execute(f'ALTER TABLE {old} RENAME TO {new}')
                return
            sql=re.sub(r'CREATE TABLE (\w+) AS (\w+)',r'CREATE TABLE \1 AS SELECT * FROM \2 WHERE 0',sql)
            sql=sql.replace('{first:Date}',':first').replace('{last:Date}',':last')
            db.execute(sql,{k:str(v) for k,v in (parameters or {}).items()})

        def insert(self,table,rows,column_names):
            db.executemany(f'INSERT INTO {table} VALUES (?,?,?)',
                           [(str(day),market,flag) for day,market,flag in rows])

    monkeypatch.setattr(calendar,'clickhouse_client',lambda:Client())
    monkeypatch.setattr(calendar,'_load_qmt_trade_dates',lambda:['20260904','20260907','20260908','20260909'])
    assert calendar.sync_trade_calendar()==8
    result=db.execute('SELECT * FROM trade_calendar ORDER BY trade_date,market').fetchall()
    assert ('2026-09-10','SH',1) in result
    assert ('2026-10-01','SH',0) in result
    assert not any(day=='2026-09-05' for day,_,_ in result)
    assert len(result)==10
