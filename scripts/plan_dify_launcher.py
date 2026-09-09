"""Prepare a PS5-compatible, byte-preserving launcher copy and daily trigger XML."""
import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

NS = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
ET.register_namespace('', NS)


def planned_xml(xml, executable, original_script, compatible_script):
    root = ET.fromstring(xml)
    actions = root.find('{'+NS+'}Actions')
    if len(actions) != 1:
        raise ValueError('Expected a single Dify dispatcher action')
    action = actions[0]
    args = action.find('{'+NS+'}Arguments')
    if original_script not in args.text:
        raise ValueError('Dify dispatcher action differs from expected source')
    action.find('{'+NS+'}Command').text = executable
    args.text = args.text.replace(original_script, compatible_script)
    working = action.find('{'+NS+'}WorkingDirectory')
    if working is None:
        working = ET.SubElement(action, '{'+NS+'}WorkingDirectory')
    working.text = str(Path(original_script).parent)
    converted = 0
    triggers = root.find('{'+NS+'}Triggers')
    for trigger in triggers:
        if trigger.tag == '{'+NS+'}TimeTrigger':
            trigger.tag = '{'+NS+'}CalendarTrigger'
            by_day = ET.SubElement(trigger, '{'+NS+'}ScheduleByDay')
            ET.SubElement(by_day, '{'+NS+'}DaysInterval').text = '1'
            converted += 1
    settings = root.find('{'+NS+'}Settings')
    enabled = settings.find('{'+NS+'}Enabled')
    if enabled is None:
        enabled = ET.SubElement(settings, '{'+NS+'}Enabled')
    enabled.text = 'false'
    return ET.tostring(root, encoding='unicode'), len(triggers), converted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--script', type=Path, required=True)
    parser.add_argument('--powershell', required=True)
    args = parser.parse_args()
    source = args.script.read_bytes()
    source_sha = hashlib.sha256(source).hexdigest()
    compat_name = args.script.stem+'.ps5-'+source_sha[:12]+'.ps1'
    target = args.script.with_name(compat_name)
    compatible = args.directory/compat_name
    compatible.write_bytes(b'\xef\xbb\xbf'+source.removeprefix(b'\xef\xbb\xbf'))
    (args.directory/'dispatcher.original.ps1').write_bytes(source)
    original = args.directory/'task.original.xml'
    xml, trigger_count, converted = planned_xml(original.read_text(encoding='utf-16'), args.powershell,
                                               str(args.script), str(target))
    planned = args.directory/'task.planned.xml'
    planned.write_bytes(xml.encode('utf-16'))
    plan = {'task': 'AiStockMonitor-Preopen', 'script': str(args.script), 'source_sha256': source_sha,
            'original_sha256': hashlib.sha256(original.read_bytes()).hexdigest(),
            'planned_sha256': hashlib.sha256(planned.read_bytes()).hexdigest(),
            'compatible_file': compat_name, 'compatible_path': str(target),
            'compatible_sha256': hashlib.sha256(compatible.read_bytes()).hexdigest(),
            'trigger_count': trigger_count, 'expired_once_converted_to_daily': converted,
            'notifications_not_executed': True, 'remote_workflow_not_audited': True}
    (args.directory/'plan.json').write_bytes(json.dumps(plan, ensure_ascii=False, indent=2).encode('utf-8'))
    print(json.dumps({'plan': str(args.directory/'plan.json'), 'triggers': trigger_count, 'converted': converted}))


if __name__ == '__main__':
    main()
