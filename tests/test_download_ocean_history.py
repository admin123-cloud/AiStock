import hashlib
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from scripts import download_ocean_history as mod


DATA = b'archive-payload-for-resume'


def make_queue(tmp_path):
    dest, state = tmp_path / 'dest', tmp_path / 'state'
    dest.mkdir()
    state.mkdir()
    return mod.Queue(dest, state)


def remote(command, **kwargs):
    if command.startswith('stat '):
        return f'{len(DATA)} 123456'
    if command.startswith('head '):
        count = int(command.split()[2])
        return hashlib.sha256(DATA[:count]).hexdigest()
    return hashlib.sha256(DATA).hexdigest()


def transfer(argv, stdout, **kwargs):
    command = argv[-1]
    offset = int(command.split('skip=')[1].split()[0])
    count = int(command.split('count=')[1].split()[0])
    stdout.write(DATA[offset:offset + count])
    return subprocess.CompletedProcess(argv, 0, b'', b'')


def test_resume_preserves_prefix_and_marks_verified(tmp_path):
    queue = make_queue(tmp_path)
    target = queue.dest / '2000_5min.zip'
    target.write_bytes(DATA[:7])
    with patch.object(mod, 'remote_text', side_effect=remote), patch.object(mod.subprocess, 'run', side_effect=transfer):
        queue.download(target.name, len(DATA))
    assert target.read_bytes() == DATA
    marker = json.loads(target.with_suffix('.zip.verified.json').read_text())
    assert marker['sha256'] == hashlib.sha256(DATA).hexdigest()
    assert queue.files[target.name]['state'] == 'verified'


def test_corrupt_prefix_is_not_overwritten_or_marked(tmp_path):
    queue = make_queue(tmp_path)
    target = queue.dest / '2001_5min.zip'
    target.write_bytes(b'wrong')
    with patch.object(mod, 'remote_text', side_effect=remote), patch.object(mod.subprocess, 'run') as run:
        queue.download(target.name, len(DATA))
    run.assert_not_called()
    assert target.read_bytes() == b'wrong'
    assert not target.with_suffix('.zip.verified.json').exists()
    assert queue.files[target.name]['state'] == 'failed'


def test_final_hash_mismatch_never_ready(tmp_path):
    queue = make_queue(tmp_path)
    def broken(argv, stdout, **kwargs):
        stdout.write(b'x' * len(DATA))
        return subprocess.CompletedProcess(argv, 0, b'', b'')
    with patch.object(mod, 'remote_text', side_effect=remote), patch.object(mod.subprocess, 'run', side_effect=broken):
        queue.download('2000_5min.zip', len(DATA))
    assert queue.files['2000_5min.zip']['state'] == 'failed'
    assert not (queue.dest / '2000_5min.zip.verified.json').exists()


def test_partial_transfer_retries_at_exact_offset(tmp_path):
    queue = make_queue(tmp_path)
    calls = []
    def partial(argv, stdout, **kwargs):
        calls.append(argv[-1])
        if len(calls) == 1:
            stdout.write(DATA[:5])
            return subprocess.CompletedProcess(argv, 255, b'', b'disconnected')
        return transfer(argv, stdout, **kwargs)
    with patch.object(mod, 'remote_text', side_effect=remote), patch.object(mod.subprocess, 'run', side_effect=partial), patch.object(mod.time, 'sleep'):
        queue.download('2000_5min.zip', len(DATA))
    assert 'skip=5 ' in calls[1]
    assert (queue.dest / '2000_5min.zip').read_bytes() == DATA
    assert queue.files['2000_5min.zip']['state'] == 'verified'
