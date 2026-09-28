"""Run the exported shared watchdog in a Python with no site packages or pip."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.build_external_watchdog_bundle import build


def test_watchdog_bundle_runs_without_financial_dependencies(tmp_path):
    output = tmp_path/'bundle'
    manifest = build(output)
    assert manifest['third_party_dependencies'] == []
    assert json.loads((output/'watchdog.json').read_text())['url'].endswith('/api/health/ready')
    environment = tmp_path/'empty-python'
    subprocess.run([sys.executable,'-m','venv','--without-pip',str(environment)],check=True)
    python = environment/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
    harness = tmp_path/'verify.py'
    harness.write_text('''import importlib.util, json, smtplib, socket, sys, threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
bundle=Path(sys.argv[1])
spec=importlib.util.spec_from_file_location('watchdog',bundle/'watchdog.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
cfg,url,state=m.load_settings(bundle/'watchdog.json')
assert state==bundle/'state/events.sqlite'
assert cfg['notify'] is False
assert not any(name in sys.modules for name in ('yaml','apscheduler','pandas','utils.paths'))
now=datetime(2026,9,9,18,tzinfo=m.BUSINESS_TZ)
# Real refused localhost connection, never an external network or SMTP.
with socket.socket() as probe:
    probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
url=f'http://127.0.0.1:{port}/api/health/ready'
sent=[]
class FakeSMTP:
    def __init__(self,*args,**kwargs):pass
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def send_message(self,msg):sent.append(msg['Subject']);return {}
smtplib.SMTP_SSL=FakeSMTP
m.config.email={'enabled':True,'smtp_server':'fake.invalid','smtp_port':465,
                'from_email':'from@example.invalid','to_emails':['to@example.invalid']}
for seconds in (0,1,2):
    result=m.poll(url,state,sender=m.send_digest,now=now+timedelta(seconds=seconds),grace_minutes=0)
    assert not result['reachable_and_ready']
assert len(sent)==1
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        assert self.path=='/api/health/ready'
        self.send_response(200);self.end_headers();self.wfile.write(b'{"ready":true}')
    def log_message(self,*args):pass
server=HTTPServer(('127.0.0.1',port),Handler)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
try:
    for seconds in (3,4):
        result=m.poll(url,state,sender=m.send_digest,now=now+timedelta(seconds=seconds),grace_minutes=0)
        assert result['reachable_and_ready']
finally:
    server.shutdown();server.server_close();thread.join()
assert len(sent)==2
event=m.read_incidents(state)[0]
assert event['status']=='resolved' and event['recovery_notification']=='smtp_accepted'
print(json.dumps({'dependencies':[], 'fake_smtp_count':len(sent), 'outage_recovery':'passed',
                  'state_relative_to_config':True}))
''',encoding='utf-8')
    result = subprocess.run([str(python),'-I',str(harness),str(output)],cwd=tmp_path,
                            capture_output=True,text=True,check=True,timeout=45)
    assert json.loads(result.stdout)['fake_smtp_count']==2
    help_result = subprocess.run([str(python),'-I',str(output/'watchdog.py'),'--help'],
                                 cwd=tmp_path,capture_output=True,text=True,check=True)
    assert '--notify' in help_result.stdout
    with pytest.raises(ValueError,match='immutable'):
        build(output)


def test_bundle_refuses_repository_output():
    from scripts.build_external_watchdog_bundle import ROOT
    with pytest.raises(ValueError,match='outside'):
        build(ROOT/'generated-watchdog-test')
