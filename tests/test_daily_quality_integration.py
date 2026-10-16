"""Opt-in read-only regression against the installed historical quality view."""
import os
import math
import pytest

pytestmark=pytest.mark.skipif(os.getenv('AISTOCK_QUALITY_LIVE_TEST')!='1',reason='explicit live read-only test')

def test_shared_reads_and_aggregates_do_not_restore_zero():
 from utils.market_warehouse import clickhouse_query_df,clickhouse_scalar
 f=clickhouse_query_df("SELECT turnover_rate FROM kline_daily WHERE code='000001.SZ' AND trade_date='1991-04-03'")
 assert f['turnover_rate'].isna().all()
 for fn in ['avg','sum']:
  assert clickhouse_scalar(f"SELECT {fn}(turnover_rate) FROM kline_daily WHERE code='000001.SZ' AND trade_date='1991-04-03'") is None
 assert clickhouse_scalar("SELECT turnover_rate FROM kline_daily WHERE code='000001.SZ' AND trade_date='1991-04-06'")==0

def test_orm_reads_use_quality_view():
 from utils.database import db
 from sqlalchemy import text
 with db.engine.connect() as cx:
  assert cx.execute(text("SELECT kline_daily.turnover_rate FROM kline_daily WHERE code='000001.SZ' AND trade_date='1991-04-03'")).scalar() is None

def test_daily_api_retains_unknowns_and_precision_zero(monkeypatch):
 from api import stocks
 monkeypatch.setattr(stocks,'_build_intraday_daily_bar',lambda code:None)
 packet=stocks.get_stock_kline('000001.SZ','1d',12000)
 rows={r['date'][:10]:r for r in packet['data']}
 assert rows['1991-04-03']['turnover_rate'] is None
 assert 'turnover_rate' in rows['1991-04-03']['unknown_fields']
 assert rows['1991-04-06']['turnover_rate']==0
 assert rows['1991-04-03']['close'] is not None

def test_daily_api_does_not_rederive_unknown_reference(monkeypatch):
 from api import stocks
 monkeypatch.setattr(stocks,'_build_intraday_daily_bar',lambda code:None)
 p=stocks.get_stock_kline('000010.SH','1d',12000)
 r=next(r for r in p['data'] if r['date'].startswith('1996-06-26'))
 assert r['close'] is not None
 assert all(r[f] is None for f in ['amplitude','change_pct','change_amount'])

def test_daily_api_nontrade_bar_not_zero_candle(monkeypatch):
 from api import stocks
 monkeypatch.setattr(stocks,'_build_intraday_daily_bar',lambda code:None)
 p=stocks.get_stock_kline('000007.SZ','1d',12000)
 r=next(r for r in p['data'] if r['date'].startswith('2022-06-30'))
 assert all(r[f] is None for f in ['open','high','low','close','volume','amount'])

def test_quality_failure_does_not_fall_back_to_raw(monkeypatch):
 from api import stocks
 from fastapi import HTTPException
 monkeypatch.setattr(stocks,'clickhouse_query_df',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('view unavailable')))
 with pytest.raises(HTTPException) as e:stocks.get_stock_kline('000001.SZ','1d',2)
 assert e.value.status_code==503

def test_unknown_change_is_not_inferred_into_limit_up():
 from api.market_overview import _is_limit_up_day
 from types import SimpleNamespace
 stock=SimpleNamespace(code='600000.SH',name='测试',market='SH',st=False)
 assert not _is_limit_up_day(stock,SimpleNamespace(change_pct=None,open=1,close=2))

def test_capital_analysis_excludes_unknown_turnover(monkeypatch):
 from services import cls_news_service as news
 import pandas as pd
 frame=pd.DataFrame([{'code':'000001.SZ','trade_date':'1991-04-03','amount':100000,'change_pct':1,'turnover_rate':None}])
 monkeypatch.setattr(news,'clickhouse_query_df',lambda *a,**k:frame.copy())
 result=news._stock_capital_context_map(['000001.SZ'])['000001.SZ']
 assert result['capital_proxy_score'] is None and result['quality_usable'] is False
