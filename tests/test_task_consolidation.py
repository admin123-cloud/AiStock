from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import pytest
from services.operations.task_catalog import catalog, describe_tasks
from services.operations.task_migration import merge_definitions, tag, NS
from scripts.run_operations_monitor import run_phases, phase_commands
from scripts.run_holding_t_service import phase_at, command_for


def definition(hour='09', command='pythonw', policy='IgnoreNew'):
    return f'<Task xmlns="{NS}"><RegistrationInfo/><Triggers><CalendarTrigger><StartBoundary>2026-09-09T{hour}:00:00</StartBoundary></CalendarTrigger></Triggers><Principals><Principal id="Author"><LogonType>S4U</LogonType></Principal></Principals><Settings><MultipleInstancesPolicy>{policy}</MultipleInstancesPolicy></Settings><Actions Context="Author"><Exec><Command>{command}</Command></Exec></Actions></Task>'


def test_merge_preserves_all_triggers_and_identity():
    first, second = definition(), definition('16')
    result = ET.fromstring(merge_definitions([first, second]))
    assert [x.find(tag('StartBoundary')).text for x in result.find(tag('Triggers'))] == ['2026-09-09T09:00:00','2026-09-09T16:00:00']
    assert ET.tostring(result.find(tag('Principals'))) == ET.tostring(ET.fromstring(first).find(tag('Principals')))
    assert result.find(tag('Actions')).attrib['Context'] == 'Author'


@pytest.mark.parametrize('other', [definition(command='other'), definition(policy='Parallel')])
def test_merge_rejects_silent_behavior_changes(other):
    with pytest.raises(ValueError):
        merge_definitions([definition(),other])


def test_explicit_dispatcher_does_not_relax_principal_policy():
    with pytest.raises(ValueError):
        merge_definitions([definition(),definition(policy='Parallel')],action={'Command':'dispatcher'})
    result=ET.fromstring(merge_definitions([definition(),definition(command='review')],action={'Command':'dispatcher','Arguments':'--source-root original'}))
    assert result.find(tag('Actions')).find(tag('Exec')).find(tag('Command')).text=='dispatcher'


def test_monitor_continues_delivery_after_health_failure_without_enabling_notifications():
    calls=[]
    def runner(command, **kwargs):
        calls.append(command)
        if len(calls)==1: raise TimeoutError('health timed out')
        return SimpleNamespace(returncode=0,stderr='')
    rows=run_phases(phase_commands(),runner)
    assert [x['ok'] for x in rows]==[False,True]
    assert all('--notify' not in c for c in calls)
    assert '--notify' in phase_commands(notify=True)[1][1]


@pytest.mark.parametrize('date,phase', [('2026-09-09T09:29:00',None),('2026-09-09T09:30:00','monitor'),('2026-09-09T15:30:00',None),('2026-09-09T16:00:00','review'),('2026-09-12T16:00:00',None)])
def test_holding_dispatch_window(date,phase):
    assert phase_at(datetime.fromisoformat(date))==phase


def test_holding_dispatch_preserves_original_source():
    assert command_for('review',Path('original'))[-1]==str(Path('original/scripts/run_g3_holding_t_daily_review.py'))


def test_catalog_has_purpose_and_failure_impact_for_every_entry():
    data=catalog()
    assert len([x for x in data['groups'] if x['id'] not in ('retired','external')])==6
    names=[n for x in data['tasks'] for n in x['names']]
    assert len(names)==len(set(names))
    for spec in data['tasks']:
        assert all(spec.get(k) for k in ('purpose','produces','failure_impact','schedule_description'))


def test_retirement_keeps_audit_but_fresh_registration_wins():
    transition={'retired':[{'name':'test','replacement':'new'}]}
    rows,_=describe_tasks([],transition)
    assert rows[0]['status']=='retired'
    rows,_=describe_tasks([{'name':'test','registered':True,'status':'running'}],transition)
    assert rows[0]['status']=='running'
    assert rows[0]['group']!='retired'
    assert '尚未登记' in rows[0]['purpose']
