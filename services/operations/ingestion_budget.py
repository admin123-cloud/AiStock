"""Cooperative boundaries and bounded child execution for ingestion jobs."""
import os
import subprocess
import time


def yield_requested():
    deadline = float(os.environ.get('AISTOCK_INGESTION_YIELD_AT', '0'))
    return deadline > 0 and time.time() >= deadline


def deferred_result():
    return {'ok': False, 'deferred': True, 'reason': 'cooperative_budget_yield'}


def run_owned(command, timeout, *, cwd, env=None):
    """A deadline never certifies that a remote database write rolled back."""
    started = time.monotonic()
    worker_env = dict(os.environ if env is None else env)
    worker_env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    proc = subprocess.Popen(command, cwd=cwd, env=worker_env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                            start_new_session=os.name != 'nt')
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        termination_error = None
        try:
            if os.name == 'nt':
                stopped = subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True,
                                         timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                if stopped.returncode:
                    termination_error = f'taskkill exit {stopped.returncode}'
            else:
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
        except Exception as exc:
            termination_error = str(exc)
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return {'ok': False, 'uncertain': True, 'error': 'execution_deadline_uncertain: verify remote writes before retry',
                'termination_error': termination_error,
                'elapsed_sec': round(time.monotonic() - started, 3)}
    return {'ok': proc.returncode == 0, 'returncode': proc.returncode,
            'deferred': proc.returncode == 75,
            'elapsed_sec': round(time.monotonic() - started, 3),
            'stdout_tail': stdout[-4000:], 'stderr_tail': stderr[-4000:], 'cmd': command}
