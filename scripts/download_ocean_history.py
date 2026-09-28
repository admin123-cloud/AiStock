"""Resumable, SHA-256 verified annual archive queue over the Shanghai SSH relay."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.paths import runtime_path

SSH = [r'C:\Windows\System32\OpenSSH\ssh.exe', '-p', '2222',
       '-i', r'C:\Users\Administrator\.ssh\id_ed25519_ocean4090',
       '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=30',
       '-o', 'ServerAliveInterval=20', '-o', 'ServerAliveCountMax=3',
       '-o', 'HostKeyAlias=100.65.32.2', '-o', 'StrictHostKeyChecking=yes',
       '-o', 'Compression=no', 'asky@127.0.0.1']
REMOTE = '/home/asky/AShare_5min_2000_2026'
CHUNK = 32 * 1024 * 1024


def now():
    return datetime.now().astimezone().isoformat()


def atomic_json(path, value):
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def remote_text(command, timeout=180):
    result = subprocess.run(SSH + [command], capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace')[-1500:])
    return result.stdout.decode('utf-8').strip()


class Queue:
    def __init__(self, dest, state):
        self.dest, self.state = dest, state
        self.guard = threading.Lock()
        self.files = {}
        self.started = now()

    def update(self, name, **fields):
        with self.guard:
            self.files.setdefault(name, {}).update(fields, updated_at=now())
            atomic_json(self.state / 'status.json', {
                'started_at': self.started, 'updated_at': now(), 'pid': os.getpid(),
                'route': '127.0.0.1:2222 -> Shanghai VPS -> ocean4090',
                'files': self.files})

    def download(self, name, size):
        target = self.dest / name
        source = f"'{REMOTE}/{name}'"
        self.update(name, state='checking', size=size)
        try:
            original_stat = remote_text(f"stat -c '%s %Y' {source}")
            if int(original_stat.split()[0]) != size:
                raise ValueError('Remote size changed since manifest')
            expected = remote_text(f'sha256sum {source}').split()[0]
            if not re.fullmatch('[0-9a-f]{64}', expected):
                raise ValueError('Invalid remote checksum')
            offset = target.stat().st_size if target.exists() else 0
            if offset > size:
                raise ValueError('Existing local file larger than source; preserved')
            if offset:
                prefix = remote_text(f'head -c {offset} {source} | sha256sum').split()[0]
                if digest(target) != prefix:
                    raise ValueError('Existing prefix hash differs; preserved for review')
            self.update(name, state='downloading', downloaded=offset, sha256=expected)
            failures = 0
            while offset < size:
                if shutil.disk_usage(self.dest).free < CHUNK + 2 * 1024**3:
                    raise ValueError('Insufficient free disk; file preserved')
                count = min(CHUNK, size - offset)
                command = (f'dd if={source} iflag=skip_bytes,count_bytes '
                           f'skip={offset} count={count} status=none')
                try:
                    with target.open('ab', buffering=0) as output:
                        result = subprocess.run(SSH + [command], stdout=output,
                                                stderr=subprocess.PIPE, timeout=600)
                    current = target.stat().st_size
                    if current > offset + count:
                        raise ValueError('Unexpected excess bytes; manual review required')
                    if result.returncode or current != offset + count:
                        raise RuntimeError(result.stderr.decode(errors='replace')[-1000:] or 'Short read')
                    failures = 0
                except (RuntimeError, subprocess.TimeoutExpired) as exc:
                    failures += 1
                    self.update(name, state='retrying', error=str(exc), failures=failures)
                    if failures >= 12:
                        raise
                    time.sleep(min(60, failures * 5))
                offset = target.stat().st_size
                self.update(name, state='downloading', downloaded=offset)
            self.update(name, state='verifying', downloaded=offset)
            if remote_text(f"stat -c '%s %Y' {source}") != original_stat:
                raise ValueError('Remote file changed during transfer')
            if digest(target) != expected:
                raise ValueError('Final SHA-256 mismatch; preserved, not marked ready')
            atomic_json(target.with_suffix(target.suffix + '.verified.json'), {
                'name': name, 'size': size, 'sha256': expected,
                'remote': f'{REMOTE}/{name}', 'remote_stat': original_stat,
                'verified_at': now(), 'method': 'full_remote_and_local_sha256'})
            self.update(name, state='verified', downloaded=size, error=None)
            print(f'{now()} VERIFIED {name}', flush=True)
        except Exception as exc:
            self.update(name, state='failed', error=str(exc))
            print(f'{now()} FAILED {name}: {exc}', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', type=Path, default=Path(r'E:\AStockHistory\harry'))
    parser.add_argument('--workers', type=int, default=2, choices=range(1, 5))
    ns = parser.parse_args()
    state = runtime_path('ocean_vps_download')
    state.mkdir(parents=True, exist_ok=True)
    ns.destination.mkdir(parents=True, exist_ok=True)
    import msvcrt
    with (state / 'download.lock').open('a+b') as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        listing = remote_text(f'find {REMOTE} -maxdepth 1 -type f -name "*_5min.zip" -printf "%f %s\\n"')
        files = {}
        for line in listing.splitlines():
            name, size = line.split()
            if re.fullmatch(r'20(?:0[0-9]|1[0-9]|2[0-6])_5min\.zip', name):
                files[name] = int(size)
        if set(files) != {f'{year}_5min.zip' for year in range(2000, 2027)}:
            raise RuntimeError('Expected exactly all 27 annual archives, refusing incomplete manifest')
        atomic_json(state / 'manifest.json', {'created_at': now(), 'files': files})
        queue = Queue(ns.destination, state)
        for name, size in sorted(files.items()):
            queue.update(name, state='queued', size=size)
        with concurrent.futures.ThreadPoolExecutor(max_workers=ns.workers) as executor:
            list(executor.map(lambda item: queue.download(*item), sorted(files.items())))
        failed = [n for n, s in queue.files.items() if s['state'] != 'verified']
        atomic_json(state / 'result.json', {'finished_at': now(), 'failed': failed, 'complete': not failed})
        return bool(failed)


if __name__ == '__main__':
    sys.exit(main())
