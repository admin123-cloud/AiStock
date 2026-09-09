"""Index refresh must preserve historical listing evidence and retained instruments."""
import ast
from datetime import date, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

from services.operations.delivery import build_delivery_calendar
from services.operations.health import BUSINESS_TZ


def test_index_refresh_insert_payload_uses_qmt_dates_and_retains_absent_rows(monkeypatch):
    old = [
        {'code':'old','type':'index','list_date':date(1991,7,15),'name':'old'},
        {'code':'corrected','type':'index','list_date':date(2026,9,9),'name':'old'},
        {'code':'absent','type':'index','list_date':date(2000,1,1),'name':'retained','industry':'retain-extra-fields'},
        {'code':'collision','type':'stock','list_date':date(2001,1,1),'name':'stock'},
    ]
    fetched = [
        {'code':'old','name':'old refreshed','market':'SH','list_date':'0'},
        {'code':'corrected','name':'corrected','market':'SH','list_date':'19910101'},
        {'code':'open','name':'raw OpenDate','market':'SH','list_date':'bad','OpenDate':19920701},
        {'code':'unknown','name':'unknown','market':'SZ','list_date':'19700101'},
        {'code':'future','name':'future invalid','market':'SZ','list_date':'29990101'},
        {'code':'collision','name':'must retain stock','market':'SZ'},
        {'code':'old','name':'duplicate','market':'SH'},
        {'code':'absent','name':'','market':'SH'},
    ]
    class Client:
        def __init__(self):self.payload=[];self.current=old
        def query(self,sql):
            return SimpleNamespace(result_rows=[(r['code'],r['type'],r['list_date']) for r in self.current])
        def command(self,sql,parameters=None):
            if sql.strip().startswith('INSERT INTO'):
                updated=parameters['updated_codes']
                self.staged=[r.copy() for r in self.current if r['type']!='index' or r['code'] not in updated]
            elif sql.startswith('RENAME TABLE'):
                self.current=self.staged
        def insert(self,table,rows,column_names):
            self.payload=[dict(zip(column_names,r)) for r in rows]
            self.staged.extend(self.payload)
    client=Client();calls=[]
    class Manager:
        def call_with_failover(self,*args,**kwargs):calls.append((args,kwargs));return fetched
    source=ModuleType('data_fetcher.manager');source.DataSourceManager=Manager
    clickhouse=ModuleType('clickhouse_connect');clickhouse.get_client=lambda **kwargs:client
    system=ModuleType('api.system_config');system.task_manager=SimpleNamespace(update_progress=lambda *args:None)
    for name,value in [('data_fetcher.manager',source),('clickhouse_connect',clickhouse),('api.system_config',system)]:
        monkeypatch.setitem(sys.modules,name,value)
    tree=ast.parse(Path('api/stocks.py').read_text(encoding='utf-8'))
    functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('_index_listing_date','update_indices')]
    for fn in functions:fn.decorator_list=[]
    env={'datetime':datetime,'__name__':'index_test'}
    exec(compile(ast.Module(body=functions,type_ignores=[]),'<index-refresh>','exec'),env)
    result=env['update_indices']()
    assert result['success']==5 and not result.get('error')
    assert calls[0][1]['source_name']=='qmt_xtquant'
    payload={r['code']:r for r in client.payload}
    assert payload['old']['list_date']==date(1991,7,15)
    assert payload['corrected']['list_date']==date(1991,1,1)
    assert payload['open']['list_date']==date(1992,7,1)
    assert payload['unknown']['list_date'] is None and payload['future']['list_date'] is None
    current={r['code']:r for r in client.current}
    assert current['absent']==old[2] and current['collision']==old[3]
    assert len(current)==len(client.current)==7


def test_unknown_index_listing_cannot_pass_historical_delivery():
    class Client:
        def query(self,sql):
            rows=[]
            if 'FROM trade_calendar' in sql:rows=[('2026-09-08',)]
            elif 'FROM stocks' in sql:rows=[('index','index',None,None)]
            elif 'FROM kline_daily ' in sql:rows=[('index','2026-09-08',1)]
            return SimpleNamespace(result_rows=rows)
    result=build_delivery_calendar(Client(),days=1,now=datetime(2026,9,9,18,tzinfo=BUSINESS_TZ))
    dataset=next(x for x in result['datasets'] if x['id']=='index_daily')
    cell=dataset['cells'][0]
    assert cell['status']=='unverified'
    assert cell['expected']==1 and cell['actual']==1
    assert cell['missing_listing_metadata_count']==1
