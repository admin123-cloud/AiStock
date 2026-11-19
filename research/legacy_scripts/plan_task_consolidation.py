"""Generate merged task XML from an exported source inventory; no task mutations."""
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from services.operations.task_migration import merge_definitions

GROUPS = [
    ['AiStock QMT xtquant Daily Coverage Repair','AiStock QMT xtquant Daily Coverage After Close Repair'],
    ['AiStock G3 Holding T Paper Monitor','AiStock G3 Holding T Daily Review'],
    ['AiStockMonitor-Preopen','AiStockMonitor-Intraday-AM','AiStockMonitor-Intraday-PM','AiStockMonitor-Afterclose','AiStockMonitor-Fallback'],
    ['AiStock Runtime Health Publisher'],
]
RETIRED=['AiStock QMT Mini Broker Snapshot Sync','AiStock QMT xtquant Daily Coverage Continuous Repair','AiStock TDX Gateway Memory Watchdog']


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('backup',type=Path)
    parser.add_argument('--source-root',type=Path,required=True)
    parser.add_argument('--pythonw',required=True)
    args=parser.parse_args()
    inventory=json.loads((args.backup/'inventory.json').read_text(encoding='utf-8-sig'))
    by_name={item['name']:item for item in inventory}
    result={'groups':[], 'retired':RETIRED, 'code_root':str(ROOT), 'source_root':str(args.source_root)}
    for index,names in enumerate(GROUPS):
        action=None
        if index in (1,3):
            script='run_holding_t_service.py' if index==1 else 'run_operations_monitor.py'
            arguments=f'"{ROOT / "scripts" / script}"'
            if index==1:
                arguments+=f' --source-root "{args.source_root}"'
            action={'Command':args.pythonw,'Arguments':arguments,'WorkingDirectory':str(ROOT)}
        xml=merge_definitions([(args.backup/by_name[name]['file']).read_text(encoding='utf-16') for name in names],
                             action=action,description='AiStock统一任务入口；原触发时间与业务数据保留。任务用途与阶段详见任务中心。')
        target=f'merged-{index}.xml'
        (args.backup/target).write_text(xml,encoding='utf-16')
        result['groups'].append({'primary':names[0],'sources':names,'file':target})
    (args.backup/'plan.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'groups':len(GROUPS),'retire_disabled':len(RETIRED),'expected_registered_after':9,'notifications_enabled_by_migration':False}))


if __name__=='__main__':
    main()
