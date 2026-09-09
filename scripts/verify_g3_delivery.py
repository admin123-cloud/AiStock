"""Read-only acceptance of freshly regenerated formal-contract observation batches."""
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from services.operations.batches import read_mainwave_batch
from services.operations.health import strategy_data_checks
from utils.paths import runtime_path


def verify(root, *, entry_date, decision_date, prior_batch_id=None):
    summary, rows, metadata = read_mainwave_batch(root,include_runtime=True)
    checks = [{'name':'atomic_batch','ok':metadata['ok'],'reason':metadata.get('reason')}]
    if metadata['ok']:
        checks.extend([{'name':'expected_dates','ok':summary.get('entry_date')==entry_date and summary.get('decision_date')==decision_date},
                       {'name':'regenerated','ok':not prior_batch_id or metadata['batch_id']!=prior_batch_id}])
        checks.extend(x for x in strategy_data_checks(summary,{}) if x['name']!='runtime_data_health')
        checks.extend(metadata.get('source_checks', []))
        checks.append({'name':'main_stage_conflicts', 'ok':all(x['ok'] for x in metadata.get('source_checks', [])),
                       'count':sum(x['failure_count'] for x in metadata.get('source_checks', []))})
    return {'ok':all(x['ok'] for x in checks),'checks':checks,'batch':metadata,
            'scope':'批次一致性与生产者来源证据；不执行委托、不证明收益，覆盖数据另验'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime-root',type=Path,default=runtime_path())
    parser.add_argument('--entry-date',required=True)
    parser.add_argument('--decision-date',required=True)
    parser.add_argument('--prior-batch-id')
    args=parser.parse_args()
    result=verify(args.runtime_root,entry_date=args.entry_date,decision_date=args.decision_date,prior_batch_id=args.prior_batch_id)
    print(json.dumps(result,ensure_ascii=False,default=str))
    return 0 if result['ok'] else 2

if __name__=='__main__': raise SystemExit(main())
