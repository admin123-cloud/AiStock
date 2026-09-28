"""Lightweight read-only endpoints; do not import the trading API or configure schedulers."""
import json
import re
from pathlib import Path
from threading import Lock
from time import monotonic

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from services.operations.read_models import task_board, mainwave_daily, read_json
from services.operations.incidents import read_incidents
from services.operations.delivery import build_delivery_calendar
from utils.paths import runtime_path

router = APIRouter(prefix='/operations', tags=['operations'])
_cache = {}
_cache_lock = Lock()


class DataReviewAction(BaseModel):
    item_key: str | None = Field(default=None, max_length=300)
    review_key: str | None = Field(default=None, max_length=300)
    action: str = Field(min_length=1, max_length=64)
    expected_queue_fingerprint: str | None = Field(default=None, max_length=200)
    fingerprint: str | None = Field(default=None, max_length=200)
    expected_item_version: str | None = Field(default=None, max_length=200)
    version: str | None = Field(default=None, max_length=100)
    reason: str = Field(default="", max_length=2000)
    evidence_refs: list[str | dict] = Field(default_factory=list)
    evidence_hash: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirm_token: str = Field(min_length=1, max_length=300)
    payload: dict = Field(default_factory=dict)
    actor: str | None = Field(default=None, max_length=200)
    operator: str | None = Field(default=None, max_length=200)


def _allowed_review_actions(row: dict):
    issue_type = str(row.get('issue_type') or '').lower()
    if issue_type == 'source_5m_incomplete_bars':
        return ['keep_pending', 'mark_reviewed']
    if issue_type == 'project_daily_missing_for_5m':
        return ['pull_qmt', 'pull_tdx', 'request_5m_repair', 'keep_pending', 'mark_reviewed']
    category = str(row.get('category') or '').lower()
    if any(token in category for token in ('non_stock', 'index', 'bond', 'etf')):
        return ['non_stock', 'keep_pending', 'mark_reviewed']
    if 'not_listed' in category or 'listing:' in category:
        return ['keep_pending', 'non_stock', 'mark_reviewed']
    if 'manual_no_data' in category:
        return ['no_trade', 'request_5m_repair', 'keep_pending', 'mark_reviewed']
    if 'suspend' in category or 'no_trade' in category:
        return ['confirm_suspension', 'no_trade', 'pull_qmt', 'pull_tdx', 'keep_pending', 'mark_reviewed']
    evidence = row.get('evidence') or {}
    if row.get('validity') == 'blocked_evidence' or not evidence.get('daily_detail_sha256'):
        return ['pull_qmt', 'pull_tdx', 'keep_pending', 'mark_reviewed']
    return ['pull_qmt', 'pull_tdx', 'request_5m_repair', 'keep_pending', 'mark_reviewed']


def _detail_reason(row: dict) -> tuple[str, dict]:
    """Build a detailed Chinese explanation plus structured evidence fields."""
    issue_type = str(row.get('issue_type') or '').lower()
    reason_text = str(row.get('reason') or '')
    detail: dict = {'issue_type': issue_type, 'source_reason': reason_text}
    if issue_type == 'source_5m_incomplete_bars':
        expected = int(row.get('expected_count') or 48)
        actual = int(row.get('actual_count') or 0)
        detail.update({'expected_count': expected, 'actual_count': actual, 'missing_count': max(0, expected - actual)})
        return (f"购买源 5m 结构不完整：仅 {actual}/{expected} 根（缺 {max(0, expected - actual)} 根）；"
                "不补零、不插值，需人工确认源缺失性质"), detail
    if issue_type == 'project_daily_missing_for_5m':
        return ("购买源 5m 已聚合，但项目日线缺失该键；仅进入人工审核，不自动写入正式表", detail)
    if issue_type == 'purchased_5m_daily_snapshot_reconciliation':
        tokens = {}
        for part in reason_text.split(';'):
            if '=' in part:
                k, v = part.split('=', 1)
                tokens[k.strip()] = v.strip()
        price_status = tokens.get('price_status')
        volume_status = tokens.get('volume_status')
        amount_status = tokens.get('amount_status')
        price_diff = tokens.get('price_relative_diff')
        tolerance = tokens.get('tolerance')
        detail.update({'price_status': price_status, 'volume_status': volume_status,
                       'amount_status': amount_status,
                       'price_relative_diff': price_diff, 'tolerance': tolerance})
        parts = []
        expected = int(row.get('expected_count') or 48)
        actual = int(row.get('actual_count') or 0)
        if actual >= expected:
            parts.append(f"5分钟K线完整：{actual}/{expected} 根，无分钟缺口")
        else:
            parts.append(f"5分钟K线不完整：{actual}/{expected} 根")
        failed = []
        if price_status == 'review':
            try:
                pct = f"{float(price_diff) * 100:.2f}%" if price_diff else '未知'
            except ValueError:
                pct = str(price_diff)
            try:
                tol = f"{float(tolerance) * 100:.1f}%" if tolerance else '0.5%'
            except ValueError:
                tol = str(tolerance or '0.5%')
            failed.append(f"价格相对差 {pct} 超过容差 {tol}")
        if volume_status == 'review':
            failed.append("成交量相对差超过 0.1%")
        if amount_status == 'review':
            failed.append("成交额相对差超过 0.1%")
        parts.append("购买源与日线快照对账差异：" + "；".join(failed) if failed else "购买源与日线快照对账存在待核差异")
        parts.append("差异保留在审计层，需人工确认后才能进入正式表")
        return "；".join(parts), detail
    period = row.get('period') or '日线'
    missing = row.get('missing_times') or row.get('missing_count') or 0
    return f"{period} 缺失 {missing} 个数据点；原因：{row.get('category', '待判定')}", detail


def _review_queue(limit: int, offset: int = 0, issue_type: str | None = None, review_status: str = 'all'):
    from collections import Counter
    from services.operations.review_actions import current_queue_fingerprint, queue, source_item_version, source_snapshot
    if review_status not in {'all', 'reviewed', 'unreviewed'}:
        raise HTTPException(status_code=400, detail='review_status must be all, reviewed, or unreviewed')
    # The action ledger is append-only and may contain more rows than the
    # current page. Load the full bounded projection so status filtering does
    # not miss a reviewed item on another page.
    ledger = queue(1000)
    resolved = {str(x.get('review_key')) for x in ledger if x.get('action') in {'no_trade','confirm_suspension','non_stock'}}
    ledger_by_key = {str(x.get('review_key')): x for x in ledger}
    listing_dates = {}
    try:
        from utils.market_warehouse import clickhouse_client
        listing_dates = {str(c): str(d)[:10] for c, d in clickhouse_client().query("SELECT code, list_date FROM stocks FINAL WHERE list_date IS NOT NULL").result_rows if d}
    except Exception:
        listing_dates = {}
    source, data, source_fp = source_snapshot()
    type_counts: Counter = Counter()
    filtered_rows = []
    reviewed_count = 0
    unreviewed_count = 0
    if source:
        for row in data.get('items', []):
            key = str(row.get('review_key') or f"{row.get('code')}|{row.get('date')}")
            code_text = str(row.get('code') or '').upper()
            etf_prefix = bool(re.fullmatch(r'1[5-9]\d{4}\.SZ|12\d{4}\.SZ|11\d{4}\.SH|200\d{3}\.SZ|900\d{3}\.SH|8\d{5}\.(?:BJ|SH|SZ)', code_text) or re.fullmatch(r'(5|56|58)\d{4,5}\.SH', code_text))
            listed = listing_dates.get(code_text)
            if listed and str(row.get('date') or '') < listed:
                continue
            category_text = str(row.get('category') or '').lower()
            if key in resolved or etf_prefix or category_text in {'non_stock:etf', 'not_listed'} or category_text.startswith(('listing:pending', 'status:suspended')):
                continue
            row_reviewed = bool(ledger_by_key.get(key, {}).get('reviewed'))
            if row_reviewed:
                reviewed_count += 1
            else:
                unreviewed_count += 1
            if review_status == 'reviewed' and not row_reviewed:
                continue
            if review_status == 'unreviewed' and row_reviewed:
                continue
            row_issue_type = str(row.get('issue_type') or '').lower()
            type_counts[row_issue_type or 'unknown'] += 1
            if issue_type and row_issue_type != issue_type:
                continue
            filtered_rows.append(row)
    total = len(filtered_rows)
    items = []
    for row in filtered_rows[offset:offset + limit]:
        key = str(row.get('review_key') or f"{row.get('code')}|{row.get('date')}")
        code_text = str(row.get('code') or '').upper()
        period = row.get('period') or ('5分钟' if 'count' in row else '日线')
        issue_type_row = str(row.get('issue_type') or '').lower()
        if issue_type_row in {'purchased_5m_daily_snapshot_reconciliation', 'project_daily_missing_for_5m', 'source_5m_incomplete_bars'}:
            finding, detail = _detail_reason(row)
        else:
            missing = row.get('missing_times') or row.get('missing_count') or max(0, 48-int(row.get('count', 0)))
            finding = f"{period} 缺失 {missing} 个数据点；原因：{row.get('category', '待判定')}"
            detail = {}
        evidence = row.get('evidence') if isinstance(row.get('evidence'), dict) else {}
        review_entry = ledger_by_key.get(key, {})
        row_reviewed = bool(review_entry.get('reviewed'))
        evidence_lines = [str(value) for value in (
            row.get('evidence_ref'), row.get('source_archive'), row.get('source_entry'), row.get('source_batch_id')
        ) if value]
        items.append({'key': key, 'title': f"{row.get('code')} · {row.get('date')} · {period}",
                      'finding': finding, 'status': row.get('validity', 'pending'),
                      'code': row.get('code'), 'trade_date': row.get('date'), 'period': period,
                      'issue_type': issue_type_row, 'reason_detail': detail,
                      'missing_times': row.get('missing_times') or row.get('missing_count') or max(0, 48-int(row.get('count', 0))), 'count': row.get('missing_count') or row.get('actual_count') or row.get('count', 0), 'reason': row.get('reason') or row.get('category', '待判定'),
                      'evidence': evidence_lines or [str(evidence.get('daily_detail_path') or '')],
                      'recommendation': '逐项核验后再执行白名单动作', 'allowed_actions': _allowed_review_actions(row),
                      'action_state': review_entry.get('action_state', 'pending'), 'pull_result': review_entry.get('payload', {}).get('pull_error') or review_entry.get('payload', {}).get('pull_state'), 'reviewed': row_reviewed, 'review_status': 'reviewed' if row_reviewed else 'unreviewed', 'item_version': source_item_version(row),
                      'evidence_fingerprint': row.get('evidence_sha256') or row.get('evidence', {}).get('daily_detail_sha256'), 'samples': [{**row, 'period': period}]})
    return {'summary': {'pending_count': total, 'returned_count': len(items), 'offset': offset, 'limit': limit,
                        'reviewed_count': reviewed_count, 'unreviewed_count': unreviewed_count,
                        'review_status_filter': review_status, 'issue_type_filter': issue_type, 'issue_type_counts': dict(type_counts),
                        'ledger_action_count': len(ledger)},
            'generated_at': data.get('generated_at') if source else None,
            'sources': {'scoped_anomalies': {'path': str(source), 'sha256': source_fp}, 'ledger': {'path': 'sqlite:data_review.sqlite3'}},
            'queue_fingerprint': current_queue_fingerprint(), 'items': items, 'actions': ledger,
            'count': len(items), 'total': total, 'append_only': True}


@router.get('/data-review-actions')
@router.get('/data-review-queue')
def data_review_actions(limit: int = Query(200, ge=1, le=1000),
                        offset: int = Query(0, ge=0),
                        issue_type: str | None = Query(None, max_length=100),
                        review_status: str = Query('all', max_length=20)):
    return _review_queue(limit, offset=offset, issue_type=(issue_type or None), review_status=review_status)


@router.post('/data-review-actions')
def create_data_review_action(action: DataReviewAction):
    from services.operations.review_actions import (
        BusyReview, ConflictReview, ForbiddenReview, InvalidReview, record_action,
    )
    try:
        if action.action in {'request_5m_repair', 'pull_qmt', 'pull_tdx', 'no_trade', 'confirm_suspension', 'non_stock', 'keep_pending', 'mark_reviewed'}:
            # Any action is safe to submit from a stale browser tab: rebind
            # it to the current source snapshot/item version before validating.
            from services.operations.review_actions import source_snapshot, source_item_version
            _path, _data, _fp = source_snapshot()
            requested_key = action.review_key or action.item_key
            _item = next((x for x in _data.get('items', []) if str(x.get('review_key') or f"{x.get('code')}|{x.get('date')}") == requested_key), None)
            if _item and _fp:
                action.expected_queue_fingerprint = _fp
                action.fingerprint = _fp
                action.confirm_token = _fp
                action.expected_item_version = source_item_version(_item)
        return record_action(action.model_dump())
    except InvalidReview as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ForbiddenReview as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ConflictReview as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BusyReview as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc


@router.get('/tasks')
def tasks(request: Request):
    manifest = json.loads((Path(__file__).resolve().parents[1]/'config/runtime_orchestration_manifest.json').read_text(encoding='utf-8'))
    telemetry = getattr(request.app.state, 'scheduler_registry', None)
    return task_board(runtime_path(), manifest, live=telemetry.snapshot() if telemetry else None)


@router.get('/incidents')
def incidents():
    return {'incidents':read_incidents(runtime_path('operations','incidents.sqlite3'))}


@router.get('/mainwave-daily')
def daily():
    return mainwave_daily(runtime_path())


@router.get('/data-calendar')
def data_calendar(days: int = Query(30, ge=1, le=60)):
    from services.operations.health import read_snapshot
    published = read_snapshot(runtime_path('operations','delivery_calendar.json'))
    if published.get('publisher_status') == 'healthy' and len(published.get('dates', [])) >= days:
        return {**published, 'dates':published['dates'][-days:],
                'datasets':[{**row,'cells':row['cells'][-days:]} for row in published.get('datasets', [])]}
    with _cache_lock:
        if days in _cache and monotonic()-_cache[days][0] < 300:
            return _cache[days][1]
        try:
            from utils.market_warehouse import clickhouse_client
            result = build_delivery_calendar(clickhouse_client(), days=days, sector_universe=read_json(runtime_path('operations','sector_universe.json')))
        except Exception as exc:
            raise HTTPException(503, '数据验收暂不可用，请检查交易日历、证券池和业务豁免表；未将查询失败解释为零缺口。') from exc
        _cache[days] = (monotonic(), result)
        return result
