from scripts.run_weekly_data_audit import summarize


def test_weekly_audit_is_healthy_only_when_every_delivery_cell_is_complete():
    result=summarize({'datasets':[{'id':'stock_5','label':'股票5分钟','cells':[{'date':'2026-09-09','status':'complete','missing':0}]}]})
    assert result['status']=='healthy' and result['problem_days']==0


def test_weekly_audit_keeps_missing_and_unverified_data_visible():
    result=summarize({'datasets':[{'id':'stock_daily','label':'股票日线','cells':[{'date':'2026-09-08','status':'missing','missing':4},{'date':'2026-09-09','status':'unverified','missing':0}]}]})
    assert result['status']=='degraded'
    assert result['datasets'][0]['problem_days']==2
    assert result['datasets'][0]['missing_keys']==4


def test_weekly_audit_keeps_query_errors_visible():
    result=summarize({'datasets':[{'id':'stock_15','label':'股票15分钟','error':'Timeout','cells':[{'date':'2026-09-09','status':'unknown'}]}]})
    assert result['status']=='degraded'
    assert result['datasets'][0]['error']=='Timeout'
