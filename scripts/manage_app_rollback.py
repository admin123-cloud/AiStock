"""Capture exact running app images and resolved Compose for a reversible release."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

DOCKER = r'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
COMPOSE = str(Path(DOCKER).parents[1] / 'cli-plugins/docker-compose.exe')


def run(command, *, env=None, timeout=120):
    result = subprocess.run(command, check=True, capture_output=True, text=True,
                            encoding='utf-8', timeout=timeout, env=env,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    return result.stdout.strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))


def capture(output):
    output.mkdir(parents=True, exist_ok=False)
    containers = {kind: json.loads(run([DOCKER,'inspect',f'aistock-{kind}-dev']))[0]
                  for kind in ('backend','frontend')}
    backend = containers['backend']
    labels = backend['Config'].get('Labels') or {}
    config_files = labels.get('com.docker.compose.project.config_files', '').split(',')
    if len(config_files) != 1 or not config_files[0]:
        raise RuntimeError('Expected one actual Compose file; cannot infer rollback configuration')
    source = Path(config_files[0]).resolve(strict=True)
    environment = dict(item.split('=',1) for item in backend['Config']['Env'] if '=' in item)
    resolved = json.loads(run([COMPOSE,'--project-directory',str(source.parent),'-f',str(source),
                               'config','--format','json'], env={**os.environ,**environment}))
    project = labels.get('com.docker.compose.project')
    if resolved.get('name') != project or set(resolved['services']) != {'backend','frontend'}:
        raise RuntimeError('Rollback must target only the actual two-service app project')
    version = datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    images = {}
    for kind, container in containers.items():
        tag = f'aistock-rollback-{kind}:{version}'
        run([DOCKER,'tag',container['Image'],tag])
        images[kind] = {'tag':tag,'id':container['Image']}
        resolved['services'][kind]['image'] = tag
        resolved['services'][kind].pop('build', None)
    checkout = Path(environment['AISTOCK_HOST_REPO_ROOT']).resolve(strict=True)
    revision = run(['git','-C',str(checkout),'rev-parse','HEAD'])
    if run(['git','-C',str(checkout),'status','--porcelain','--untracked-files=no']):
        raise RuntimeError('Running checkout has tracked changes; preserve them before creating rollback')
    settings = next(Path(m['Source']) for m in backend['Mounts'] if m['Destination']=='/app/config/settings.local.yaml')
    compose = output/'rollback.compose.json'
    save(compose,resolved)
    run([COMPOSE,'-f',str(compose),'config','--quiet'])
    manifest = {'created_at':datetime.now().astimezone().isoformat(),'project':project,
                'source_compose':str(source),'compose':str(compose),'compose_sha256':digest(compose),
                'host_repo':str(checkout),'host_revision':revision,
                'settings':str(settings),'settings_sha256':digest(settings),'images':images,
                'mounts':{k:v['Mounts'] for k,v in containers.items()}}
    save(output/'rollback.json',manifest)
    return manifest


def verify(record):
    manifest = json.loads(record.read_text(encoding='utf-8'))
    compose = Path(manifest['compose'])
    if digest(compose) != manifest['compose_sha256']:
        raise RuntimeError('Rollback Compose changed')
    if digest(Path(manifest['settings'])) != manifest['settings_sha256']:
        raise RuntimeError('Prior private configuration changed; review before rollback')
    checkout = manifest['host_repo']
    if run(['git','-C',checkout,'rev-parse','HEAD']) != manifest['host_revision']:
        raise RuntimeError('Prior host checkout revision changed')
    if run(['git','-C',checkout,'status','--porcelain','--untracked-files=no']):
        raise RuntimeError('Prior checkout has tracked changes')
    for item in manifest['images'].values():
        actual = run([DOCKER,'image','inspect','--format','{{.Id}}',item['tag']])
        if actual != item['id']:
            raise RuntimeError('Pinned rollback image differs from captured image')
    run([COMPOSE,'-f',str(compose),'config','--quiet'])
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('capture','inspect','rollback'), default='inspect')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--record',type=Path)
    args = parser.parse_args()
    if args.mode == 'capture':
        if not args.output:
            parser.error('--output required')
        value = capture(args.output.resolve())
    else:
        if not args.record:
            parser.error('--record required')
        value = verify(args.record)
        if args.mode == 'rollback':
            run([COMPOSE,'-f',value['compose'],'up','-d','--no-build','--wait','--wait-timeout','180'],timeout=240)
            for kind, item in value['images'].items():
                if run([DOCKER,'inspect','--format','{{.Image}}',f'aistock-{kind}-dev']) != item['id']:
                    raise RuntimeError('Restored container image differs from captured image')
            value['restored_at'] = datetime.now().astimezone().isoformat()
            save(args.record,value)
    print(json.dumps({'mode':args.mode,'project':value['project'],'compose':value['compose'],
                      'host_revision':value['host_revision']},ensure_ascii=False))


if __name__ == '__main__':
    main()
