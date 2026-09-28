"""Pure XML planning for reversible Windows task consolidation."""
from copy import deepcopy
import xml.etree.ElementTree as ET

NS = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
ET.register_namespace('', NS)


def tag(name):
    return f'{{{NS}}}{name}'


def merge_definitions(documents, *, action=None, description=None):
    roots = [ET.fromstring(value) for value in documents]
    merged = deepcopy(roots[0])
    for root in roots[1:]:
        for section in ('Principals','Settings'):
            if ET.tostring(root.find(tag(section))) != ET.tostring(roots[0].find(tag(section))):
                raise ValueError(f'Task {section} differ; cannot silently merge policy')
        if action is None and ET.tostring(root.find(tag('Actions'))) != ET.tostring(roots[0].find(tag('Actions'))):
            raise ValueError('Different actions require an explicit dispatcher')
    triggers = merged.find(tag('Triggers'))
    triggers.clear()
    for root in roots:
        for original in root.find(tag('Triggers')):
            trigger = deepcopy(original)
            if 'id' in trigger.attrib:
                trigger.set('id',f'merged-{len(triggers)}')
            triggers.append(trigger)
    if action:
        actions = merged.find(tag('Actions'))
        for node in list(actions):
            actions.remove(node)
        execute = ET.SubElement(actions,tag('Exec'))
        for field in ('Command','Arguments','WorkingDirectory'):
            if action.get(field):
                ET.SubElement(execute,tag(field)).text = action[field]
    if description:
        info = merged.find(tag('RegistrationInfo'))
        if info is None:
            info = ET.SubElement(merged,tag('RegistrationInfo'))
        node = info.find(tag('Description'))
        if node is None:
            node = ET.SubElement(info,tag('Description'))
        node.text = description
    return ET.tostring(merged,encoding='unicode')
