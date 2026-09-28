"""Read-only release acceptance report. Exit 2 means remaining release gates."""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.operations.read_models import task_board, read_json, mainwave_daily
from services.operations.health import read_snapshot, BUSINESS_TZ
from utils.paths import runtime_path


def assess(root, *, now=None):
    now = now or datetime.now(BUSINESS_TZ)
    board = task_board(root, {}, now=now)
    publisher = board['operations_publisher']
    calendar = read_snapshot(root/'operations/delivery_calendar.json',now=now)
    bad = [{'dataset': dataset['id'], 'date': cell.get('date'), 'status': cell['status']}
           for dataset in calendar.get('datasets', []) for cell in dataset['cells']
           if cell['status'] not in ('complete','not_due')]
    daily = mainwave_daily(root,now=now)
    gates = [
        {'name':'api_schedulers', 'ok':board['api_runtime']['ready']},
        {'name':'host_inventory', 'ok':board['host_inventory_status']=='healthy'},
        {'name':'operations_publisher', 'ok':publisher.get('publisher_status')=='healthy'},
        {'name':'notification_takeover', 'ok':bool(publisher.get('notifications_enabled') and publisher.get('notification_transport_ok'))},
        {'name':'delivery_calendar', 'ok':calendar.get('publisher_status')=='healthy' and bool(calendar.get('datasets')) and not bad},
        {'name':'g3_batch', 'ok':daily['batch']['ok']},
        {'name':'g3_data_checks', 'ok':all(x['ok'] for x in daily['checks'])},
    ]
    return {'generated_at':now.isoformat(), 'ready':all(x['ok'] for x in gates), 'gates':gates,
            'delivery_blockers':bad, 'scope':'快照验收；不替代完整交易日观察、邮件实际送达与部署版本核验'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime-root',type=Path,default=runtime_path())
    args=parser.parse_args()
    result=assess(args.runtime_root)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['ready'] else 2


if __name__=='__main__':
    raise SystemExit(main())
