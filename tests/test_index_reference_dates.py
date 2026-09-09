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
    functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('_reference_listing_date','update_indices')]
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


def test_qmt_official_pool_keeps_missing_details_without_false_delist():
    from data_fetcher.sources.qmtmini import QmtMiniDataSource
    source=object.__new__(QmtMiniDataSource)
    codes=['600000.SH','821028.BJ','821029.BJ','821030.BJ','000004.SZ']
    client=SimpleNamespace(download_history_contracts=lambda **kw:None,
        get_stock_list_in_sector=lambda sector:codes,
        get_instrument_detail_list=lambda *args:{'600000.SH':{'InstrumentName':'known','OpenDate':'19991110'},
                                               '000004.SZ':{'InstrumentName':'retired','ExpireDate':'20260101'}})
    source._ensure_client=lambda:client
    source.mark_success=lambda:None
    rows=source.get_stock_list(market='ALL')
    assert {r['code'] for r in rows}==set(codes)-{'000004.SZ'}
    unknown=[r for r in rows if r['metadata_unknown']]
    assert len(unknown)==3
    assert source.last_stock_list_metadata['official_codes']==codes
    assert len(source.last_stock_list_metadata['returned_codes'])==4
    assert source.last_stock_list_metadata['missing_detail_codes']==codes[1:4]
    assert all(r['list_date']=='' and r['quit']==0 and r['name']==r['code'] for r in unknown)


def test_stock_insert_dates_are_nullable_and_batch_details_are_reused(tmp_path,monkeypatch):
    from zoneinfo import ZoneInfo
    import utils.paths
    monkeypatch.setattr(utils.paths,'runtime_path',lambda *parts:tmp_path.joinpath(*parts))
    existing=[('600000.SH','known','SH','stock','industry','region',date(1999,11,10),None,0,0),
              ('old.SZ','retained','SZ','stock','','',None,None,0,0)]
    calls=[];payload=[]
    items=[{'code':'600000.SH','name':'known','source':'qmt_xtquant','list_date':''},
           {'code':'821028.BJ','name':'821028.BJ','source':'qmt_xtquant','metadata_unknown':True}]
    class Manager:
        def get_stock_list(self,**kwargs):
            assert kwargs['source_name']=='qmt_xtquant'
            return items
        def call_with_failover(self,*args,**kwargs):
            calls.append((args,kwargs));return None
        def get_source(self,name):return SimpleNamespace(get_expired_stock_info=lambda codes:{})
    class Client:
        def query(self,sql):return SimpleNamespace(result_rows=existing)
        def command(self,*args,**kwargs):pass
        def insert(self,table,rows,column_names):payload.extend(dict(zip(column_names,row)) for row in rows)
    manager=ModuleType('data_fetcher.manager');manager.DataSourceManager=Manager
    clickhouse=ModuleType('clickhouse_connect');clickhouse.get_client=lambda **kwargs:Client()
    system=ModuleType('api.system_config');system.task_manager=SimpleNamespace(update_progress=lambda *args:None)
    for name,value in [('data_fetcher.manager',manager),('clickhouse_connect',clickhouse),('api.system_config',system)]:
        monkeypatch.setitem(sys.modules,name,value)
    tree=ast.parse(Path('api/stocks.py').read_text(encoding='utf-8'))
    functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('_reference_listing_date','update_stock_list')]
    for fn in functions:fn.decorator_list=[]
    logger=SimpleNamespace(info=lambda *a:None,error=lambda *a:None,warning=lambda *a:None)
    env={'datetime':datetime,'ZoneInfo':ZoneInfo,'__name__':'stock_test','get_logger':lambda *a:logger,
         'Any':object,'Dict':dict,'pd':SimpleNamespace(isna=lambda value:value is None)}
    exec(compile(ast.Module(body=functions,type_ignores=[]),'<stock-refresh>','exec'),env)
    result=env['update_stock_list']()
    assert result['success'],result
    rows={r['code']:r for r in payload}
    assert rows['600000.SH']['list_date']==date(1999,11,10)
    assert rows['821028.BJ']['list_date'] is None and rows['821028.BJ']['quit']==0
    assert rows['old.SZ']['list_date'] is None
    assert len(calls)==1 and calls[0][1]['source_name']=='qmt_xtquant'
    assert result['metadata']['metadata_unknown_codes']==['821028.BJ']
    assert not result['metadata']['metadata_verified']
    assert (tmp_path/'operations/reference_metadata.json').exists()


def test_reference_metadata_health_does_not_confuse_missing_details_with_quote_failure(tmp_path):
    from services.operations.health import ArtifactRule,build_snapshot,write_snapshot
    now=datetime(2026,9,9,18,tzinfo=BUSINESS_TZ)
    path=tmp_path/'reference.json'
    rule=ArtifactRule('reference_metadata',path,36*3600,require_payload_healthy=True)
    payload={'generated_at':now.isoformat(),'status':'healthy','metadata_verified':False,
             'official_pool_count':5596,'returned_pool_count':5596,'metadata_unknown_codes':['821028.BJ']}
    write_snapshot(payload,path)
    result=build_snapshot([rule],now=now)
    assert not result['strategy_actionable']
    component=result['components'][0]
    assert component['reason']=='reference_metadata_unverified'
    assert component['reference_metadata']['official_pool_count']==5596
    assert component['recommended_action']=='verify_qmt_instrument_details_and_listing_dates'
    payload.update(metadata_verified=True,metadata_unknown_codes=[])
    write_snapshot(payload,path)
    assert build_snapshot([rule],now=now)['strategy_actionable']
