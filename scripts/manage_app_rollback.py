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
CONTAINER_CANDIDATES = {
    'backend': ('aistock-backend-dev',),
    'frontend': ('aistock-frontend-dev', 'aistock-frontend-react-dev'),
}


def run(command, *, env=None, timeout=120):
    result = subprocess.run(command, check=True, capture_output=True, text=True,
                            encoding='utf-8', timeout=timeout, env=env,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    return result.stdout.strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))


def inspect_running(kind):
    errors = []
    for name in CONTAINER_CANDIDATES[kind]:
        try:
            return name, json.loads(run([DOCKER, 'inspect', name]))[0]
        except (subprocess.CalledProcessError, IndexError, json.JSONDecodeError) as exc:
            errors.append(f'{name}: {exc}')
    raise RuntimeError(f'No running {kind} container found; tried {CONTAINER_CANDIDATES[kind]} ({errors})')


def resolved_service(container):
    labels = container['Config'].get('Labels') or {}
    files = [item for item in labels.get('com.docker.compose.project.config_files', '').split(',') if item]
    if len(files) != 1:
        raise RuntimeError('Expected one actual Compose file per container')
    source = Path(files[0]).resolve(strict=True)
    environment = dict(item.split('=', 1) for item in container['Config'].get('Env', []) if '=' in item)
    for mount in container.get('Mounts', []):
        if mount.get('Destination') == '/app/config/settings.local.yaml':
            environment.setdefault('AISTOCK_SETTINGS_FILE', mount['Source'])
    resolved = json.loads(run([COMPOSE, '--project-directory', str(source.parent), '-f', str(source),
                               'config', '--format', 'json'], env={**os.environ, **environment}))
    service_name = labels.get('com.docker.compose.service')
    project = labels.get('com.docker.compose.project')
    if not service_name or service_name not in resolved.get('services', {}):
        raise RuntimeError(f'Running Compose service is absent from resolved config: {service_name}')
    if resolved.get('name') != project:
        raise RuntimeError('Resolved Compose project differs from running container label')
    return source, environment, resolved, service_name, project


def capture(output):
    output.mkdir(parents=True, exist_ok=False)
    containers = {}
    container_names = {}
    resolved_rows = {}
    projects = set()
    sources = {}
    for kind in ('backend', 'frontend'):
        name, container = inspect_running(kind)
        source, environment, resolved, service_name, project = resolved_service(container)
        containers[kind] = container
        container_names[kind] = name
        resolved_rows[kind] = (environment, resolved, service_name)
        projects.add(project)
        sources[kind] = str(source)
    if len(projects) != 1:
        raise RuntimeError(f'App containers belong to different Compose projects: {sorted(projects)}')
    project = projects.pop()

    # The current production topology may have been assembled from separate
    # release/dev Compose files. Build one immutable rollback Compose from the
    # exact resolved service definitions instead of assuming one source file.
    merged = {'name': project, 'services': {}, 'networks': {}, 'volumes': {}}
    service_names = {}
    for kind, (_environment, resolved, service_name) in resolved_rows.items():
        if service_name in merged['services']:
            raise RuntimeError(f'Duplicate service name while merging rollback Compose: {service_name}')
        merged['services'][service_name] = resolved['services'][service_name]
        service_names[kind] = service_name
        merged['networks'].update(resolved.get('networks') or {})
        merged['volumes'].update(resolved.get('volumes') or {})

    backend = containers['backend']
    backend_environment = resolved_rows['backend'][0]
    version = datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    images = {}
    for kind, container in containers.items():
        tag = f'aistock-rollback-{kind}:{version}'
        run([DOCKER, 'tag', container['Image'], tag])
        service_name = service_names[kind]
        images[kind] = {'tag': tag, 'id': container['Image'], 'service': service_name,
                        'container_name': container_names[kind]}
        merged['services'][service_name]['image'] = tag
        merged['services'][service_name].pop('build', None)

    checkout = Path(backend_environment['AISTOCK_HOST_REPO_ROOT']).resolve(strict=True)
    revision = run(['git', '-C', str(checkout), 'rev-parse', 'HEAD'])
    if run(['git', '-C', str(checkout), 'status', '--porcelain', '--untracked-files=no']):
        raise RuntimeError('Running checkout has tracked changes; preserve them before creating rollback')
    settings = next(Path(m['Source']) for m in backend['Mounts'] if m['Destination'] == '/app/config/settings.local.yaml')
    compose = output/'rollback.compose.json'
    save(compose, merged)
    run([COMPOSE, '-f', str(compose), 'config', '--quiet'])
    manifest = {'created_at': datetime.now().astimezone().isoformat(), 'project': project,
                'source_compose': sources, 'compose': str(compose), 'compose_sha256': digest(compose),
                'host_repo': str(checkout), 'host_revision': revision,
                'settings': str(settings), 'settings_sha256': digest(settings), 'images': images,
                'mounts': {k: v['Mounts'] for k, v in containers.items()}}
    save(output/'rollback.json', manifest)
    return manifest


def verify(record):
    manifest = json.loads(record.read_text(encoding='utf-8'))
    compose = Path(manifest['compose'])
    if digest(compose) != manifest['compose_sha256']:
        raise RuntimeError('Rollback Compose changed')
    if digest(Path(manifest['settings'])) != manifest['settings_sha256']:
        raise RuntimeError('Prior private configuration changed; review before rollback')
    checkout = manifest['host_repo']
    if run(['git', '-C', checkout, 'rev-parse', 'HEAD']) != manifest['host_revision']:
        raise RuntimeError('Prior host checkout revision changed')
    if run(['git', '-C', checkout, 'status', '--porcelain', '--untracked-files=no']):
        raise RuntimeError('Prior checkout has tracked changes')
    for item in manifest['images'].values():
        actual = run([DOCKER, 'image', 'inspect', '--format', '{{.Id}}', item['tag']])
        if actual != item['id']:
            raise RuntimeError('Pinned rollback image differs from captured image')
    run([COMPOSE, '-f', str(compose), 'config', '--quiet'])
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('capture', 'inspect', 'rollback'), default='inspect')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--record', type=Path)
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
            run([COMPOSE, '-f', value['compose'], 'up', '-d', '--no-build', '--wait', '--wait-timeout', '180'], timeout=240)
            for item in value['images'].values():
                if run([DOCKER, 'inspect', '--format', '{{.Image}}', item['container_name']]) != item['id']:
                    raise RuntimeError('Restored container image differs from captured image')
            value['restored_at'] = datetime.now().astimezone().isoformat()
            save(args.record, value)
    print(json.dumps({'mode': args.mode, 'project': value['project'], 'compose': value['compose'],
                      'host_revision': value['host_revision']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
