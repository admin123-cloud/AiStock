"""Lightweight read-only endpoints; do not import the trading API or configure schedulers."""
import json
from pathlib import Path
from threading import Lock
from time import monotonic

from fastapi import APIRouter, HTTPException, Query

from services.operations import task_board, mainwave_daily
from services.operations_incidents import read_incidents
from services.data_delivery import build_delivery_calendar
from utils.paths import runtime_path

router = APIRouter(prefix='/operations', tags=['operations'])
_cache = {}
_cache_lock = Lock()


@router.get('/tasks')
def tasks():
    manifest = json.loads((Path(__file__).resolve().parents[1]/'config/runtime_orchestration_manifest.json').read_text(encoding='utf-8'))
    return task_board(runtime_path(), manifest)


@router.get('/incidents')
def incidents():
    return {'incidents':read_incidents(runtime_path('operations','incidents.sqlite3'))}


@router.get('/mainwave-daily')
def daily():
    return mainwave_daily(runtime_path())


@router.get('/data-calendar')
def data_calendar(days: int = Query(30, ge=1, le=60)):
    from services.runtime_health import read_snapshot
    published = read_snapshot(runtime_path('operations','delivery_calendar.json'))
    if published.get('publisher_status') == 'healthy' and len(published.get('dates', [])) >= days:
        return {**published, 'dates':published['dates'][-days:],
                'datasets':[{**row,'cells':row['cells'][-days:]} for row in published.get('datasets', [])]}
    with _cache_lock:
        if days in _cache and monotonic()-_cache[days][0] < 300:
            return _cache[days][1]
        try:
            from utils.market_warehouse import clickhouse_client
            result = build_delivery_calendar(clickhouse_client(), days=days)
        except Exception as exc:
            raise HTTPException(503, '数据验收暂不可用，请检查交易日历、证券池和业务豁免表；未将查询失败解释为零缺口。') from exc
        _cache[days] = (monotonic(), result)
        return result
