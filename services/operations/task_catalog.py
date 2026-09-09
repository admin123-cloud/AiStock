"""Product-facing task identity, purpose and grouping, independent of telemetry."""
import json
from pathlib import Path


def catalog():
    return json.loads((Path(__file__).resolve().parents[2]/'config/task_catalog.json').read_text(encoding='utf-8'))


def describe_tasks(rows, transitions=None):
    specification = catalog()
    lookup = {name: item for item in specification['tasks'] for name in item['names']}
    retired = {x['name']: x for x in (transitions or {}).get('retired', [])}
    names = {row['name'] for row in rows}
    rows = [*rows, *({'name': name, 'executor':'Windows', 'status':'retired', 'business_status':'unverified',
                      'registered':False} for name in retired if name not in names)]
    result = []
    for row in rows:
        spec = lookup.get(row.get('job_id')) or lookup.get(row['name']) or {}
        group = spec.get('group', 'monitor')
        item = {**row, **{k:v for k,v in spec.items() if k != 'names'},
                'title':spec.get('title',row['name']), 'group':group,
                'purpose':spec.get('purpose') or row.get('description') or '新发现任务，尚未登记具体用途，请核对执行入口。',
                'failure_impact':spec.get('failure_impact','影响范围待核对，不能据退出码推断业务正常。'),
                'produces':spec.get('produces') or row.get('artifact') or '产物尚未登记',
                'schedule_description':spec.get('schedule_description') or row.get('window') or '以实际触发器为准'}
        if row['name'] in retired and not (row.get('registered') and not row.get('source_stale')):
            item.update(group='retired', lifecycle='retired', status='retired',
                        replacement=retired[row['name']].get('replacement'),
                        retired_at=retired[row['name']].get('retired_at'))
        result.append(item)
    groups = [{**group, 'count':sum(x['group']==group['id'] for x in result),
               'attention':sum(x['group']==group['id'] and (x.get('status') in ('failed','unknown','not_observed') or
                   x.get('business_status') in ('blocked','degraded','stale')) for x in result)} for group in specification['groups']]
    return result, groups
