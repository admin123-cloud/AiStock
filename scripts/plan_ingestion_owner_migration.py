"""Produce reversible XML action changes while retaining every existing trigger."""
import argparse
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

NS = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
ET.register_namespace('', NS)


def transform(xml, source, target, *, daily=False, holding=False, now=None):
    root = ET.fromstring(xml)
    actions = root.find('{'+NS+'}Actions')
    commands = list(actions) if actions is not None else []
    if len(commands) != 1:
        raise ValueError('Expected exactly one collector action')
    action = commands[0]
    arguments = action.find('{'+NS+'}Arguments')
    entry = 'run_holding_t_service.py' if holding else 'run_qmt_xtquant_collector.ps1'
    if arguments is None or entry not in (arguments.text or ''):
        raise ValueError('Unexpected collector entry point')
    if source.lower() not in arguments.text.lower():
        raise ValueError('Action no longer points at expected source checkout')
    arguments.text = re.sub(re.escape(source), lambda match: target, arguments.text, flags=re.I)
    directory = action.find('{'+NS+'}WorkingDirectory')
    if directory is None:
        directory = ET.SubElement(action, '{'+NS+'}WorkingDirectory')
    directory.text = target
    triggers = root.find('{'+NS+'}Triggers')
    original_triggers = [ET.tostring(child, encoding='unicode') for child in triggers]
    if daily:
        arguments.text, changed = re.subn(r'(-Mode\s+)"?daily-coverage-repair"?',
                                          lambda match: match[1]+'"daily-maintenance"', arguments.text, flags=re.I)
        if changed != 1:
            raise ValueError('Unexpected daily action mode')
        template = next((child for child in triggers if 'T16:10:' in ET.tostring(child, encoding='unicode')), None)
        if template is None:
            raise ValueError('Expected existing 16:10 daily trigger')
        added = deepcopy(template)
        now = now or datetime.now()
        next_run = now.replace(hour=18, minute=10, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        added.find('{'+NS+'}StartBoundary').text = next_run.isoformat()
        added.attrib.pop('id', None)
        triggers.append(added)
    if [ET.tostring(child, encoding='unicode') for child in triggers][:len(original_triggers)] != original_triggers:
        raise AssertionError('An existing trigger was changed')
    # Registration is staged disabled; activation is a separate guarded command.
    settings = root.find('{'+NS+'}Settings')
    enabled = settings.find('{'+NS+'}Enabled')
    if enabled is None:
        enabled = ET.SubElement(settings, '{'+NS+'}Enabled')
    enabled.text = 'false'
    return ET.tostring(root, encoding='unicode'), len(original_triggers), len(triggers)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--source', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    inventory = json.loads((args.directory/'inventory.json').read_text(encoding='utf-8-sig'))
    plans = []
    for row in inventory:
        original = args.directory/row['file']
        before = original.read_bytes()
        xml, old_count, new_count = transform(before.decode('utf-16'), args.source, args.target,
                                              daily=row['name'].endswith('Daily Coverage Repair'),
                                              holding=row['name'] == 'AiStock G3 Holding T Paper Monitor')
        planned = original.with_suffix('.planned.xml')
        planned.write_bytes(xml.encode('utf-16'))
        plans.append({**row, 'sha256': hashlib.sha256(before).hexdigest(), 'planned_file': planned.name,
                      'planned_sha256': hashlib.sha256(planned.read_bytes()).hexdigest(),
                      'original_triggers': old_count, 'planned_triggers': new_count})
    payload = {'version': args.version, 'source': args.source, 'target': args.target,
               'generated_at': datetime.now().isoformat(), 'tasks': plans,
               'activation_separate': True, 'legacy_sector_owner_proof_required': True}
    (args.directory/'plan.json').write_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8'))
    print(json.dumps({'plan': str(args.directory/'plan.json'), 'tasks': len(plans)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
