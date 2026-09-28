"""Export a stdlib-only external watchdog from the audited shared implementations."""
import argparse
import ast
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def selected_source(path, names):
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    found = {node.name: node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names}
    if set(found) != set(names):
        raise ValueError('Shared watchdog symbols changed: '+str(set(names)-set(found)))
    class LocalConfig(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return None if node.module == 'utils.config' else node
    chunks = [ast.unparse(LocalConfig().visit(found[name])) for name in names]
    return '\n\n'.join(chunks), {'path':str(path.relative_to(ROOT)),
                                'sha256':hashlib.sha256(source.encode()).hexdigest(), 'symbols':names}


PRELUDE = '''import argparse
import hashlib
import json
import math
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

# New incident timestamps only. China business time is UTC+8; no historical conversion.
BUSINESS_TZ = timezone(timedelta(hours=8), 'UTC+08:00')

class EnvironmentConfig:
    def __init__(self): self.email = {}
    def get(self, key, default=None): return self.email if key == 'email' else default

config = EnvironmentConfig()
'''

LAUNCHER = '''
def load_settings(path):
    path = path.resolve()
    cfg = json.loads(path.read_text(encoding='utf-8'))
    url = os.environ.get('AISTOCK_WATCHDOG_URL') or cfg.get('url')
    if not url or not url.startswith(('http://', 'https://')):
        raise ValueError('HTTP(S) readiness URL required')
    state = Path(os.environ.get('AISTOCK_WATCHDOG_STATE') or cfg.get('state', 'state/events.sqlite'))
    if not state.is_absolute(): state = path.parent / state
    email = dict(cfg.get('email') or {})
    password_env = email.pop('smtp_password_env', 'AISTOCK_WATCHDOG_SMTP_PASSWORD')
    if email.get('smtp_password'):
        raise ValueError('Use smtp_password_env; do not put SMTP secrets in deployment JSON')
    email['smtp_password'] = os.environ.get(password_env, '')
    config.email = email
    return cfg, url, state.resolve()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('watchdog.json'))
    parser.add_argument('--notify', action='store_true', help='Explicitly enable configured SMTP')
    parser.add_argument('--loop', action='store_true', help='Poll continuously; otherwise one read-only remote probe')
    parser.add_argument('--interval', type=int, default=60)
    args = parser.parse_args()
    if args.interval < 10: parser.error('interval must be at least 10 seconds')
    cfg, url, state = load_settings(args.config)
    grace = int(cfg.get('grace_minutes', 10))
    if grace < 0: parser.error('grace_minutes cannot be negative')
    while True:
        result = poll(url, state, sender=send_digest if args.notify else None, grace_minutes=grace)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if not args.loop: return 0
        time.sleep(args.interval)

if __name__ == '__main__': raise SystemExit(main())
'''


def build(output, *, source_root=ROOT):
    output = Path(output).resolve()
    if output == source_root.resolve() or source_root.resolve() in output.parents:
        raise ValueError('Bundle output must be outside the code repository')
    if output.exists():
        raise ValueError('Use a new output directory; existing bundles are immutable')
    selections = [
        ('services/operations/lifecycle.py', ['InstanceLock']),
        ('services/operations/incidents.py', ['NotificationOutcomeUncertain', '_schema', '_checks',
         '_fingerprint', 'reconcile', 'read_incidents', '_dispatch_failures', 'send_digest', 'dispatch']),
        ('scripts/external_runtime_watchdog.py', ['poll']),
    ]
    bodies, evidence = [], []
    for name, symbols in selections:
        text, provenance = selected_source(source_root/name, symbols)
        bodies.append(text)
        evidence.append(provenance)
    executable = PRELUDE+'\n\n'+'\n\n'.join(bodies)+'\n'+LAUNCHER
    # Parse before creating an artifact so extraction changes fail before export.
    ast.parse(executable)
    cfg = json.loads((source_root/'config/external_watchdog.example.json').read_text(encoding='utf-8'))
    cfg['state'] = 'state/events.sqlite'
    cfg['email'] = {'enabled':False, 'smtp_server':'SMTP_HOST', 'smtp_port':465,
                    'use_ssl':True, 'smtp_user':'', 'smtp_password_env':'AISTOCK_WATCHDOG_SMTP_PASSWORD',
                    'from_email':'', 'to_emails':[]}
    commit = subprocess.check_output(['git','-C',str(source_root),'rev-parse','HEAD'], text=True).strip()
    output.mkdir(parents=True)
    (output/'watchdog.py').write_bytes(executable.encode('utf-8'))
    (output/'watchdog.json').write_bytes(json.dumps(cfg,ensure_ascii=False,indent=2).encode('utf-8'))
    manifest = {'schema_version':1, 'source_commit':commit, 'shared_sources':evidence,
                'python':'>=3.10', 'third_party_dependencies':[],
                'deployment_status':'not_deployed_independent_device_required',
                'state_resolution':'relative to config file, never the process working directory',
                'environment':['AISTOCK_WATCHDOG_URL','AISTOCK_WATCHDOG_STATE','AISTOCK_WATCHDOG_SMTP_PASSWORD'],
                'time_basis':'UTC+08:00 for new events; no historical timezone conversion',
                'knowledge_base':'F:/AiDevelop/ai-workspace/projects/AiStock/changes/2026-09-09-delivery-observability-final.md',
                'files':{name:hashlib.sha256((output/name).read_bytes()).hexdigest()
                         for name in ('watchdog.py','watchdog.json')}}
    (output/'manifest.json').write_bytes(json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.output),ensure_ascii=False,indent=2))


if __name__ == '__main__': main()
