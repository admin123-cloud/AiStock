"""In-process scheduler telemetry. Reading this module never starts a job."""
import functools
import os
import threading
import time
from datetime import datetime
from uuid import uuid4
from contextlib import contextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_MISSED, EVENT_JOB_MAX_INSTANCES
from apscheduler.schedulers.base import STATE_PAUSED
from utils.trading_sessions import BUSINESS_TZ

JOB_NAMES = {
    'g3_state_alpha_shadow_monitor': 'G3候选刷新',
    'g3_state_alpha_shadow_exit_monitor': 'G3影子退出',
    'g3_state_alpha_daily_trend_exit_monitor': 'G3趋势退出',
    'g3_state_alpha_observation_snapshot': 'G3每日观察',
    'g3_state_alpha_observation_first_bar_retry': 'G3首根30m补验',
    'g3_state_alpha_broker_sync': '真实账户盘后同步',
    'g3_state_alpha_auto_order_30m': 'G3自动委托（现有开关）',
    'v4_manual_holdings_monitor': '持仓监控',
    'gen2_shadow_buy_monitor': 'G2影子买点监控',
    'gen2_strategy_refresh_30m': 'G2策略刷新',
}


def stamp():
    return datetime.now(BUSINESS_TZ).isoformat(timespec='seconds')


class SchedulerRegistry:
    def __init__(self):
        self.lock = threading.RLock()
        self.schedulers = []
        self.jobs = {}
        self.startups = []
        self.probes = {}
        self.workers = set()
        self.instance_id = uuid4().hex
        self.started_at = stamp()
        self.phase = 'not_started'

    def initialize(self, specs, *, enabled=True):
        self.phase = 'starting'
        for name, callback in specs:
            row = {'name': name, 'started_at': stamp(), 'status': 'starting'}
            self.startups.append(row)
            try:
                result = callback() if enabled else None
                disabled = not enabled or result is None or result is False or (
                    isinstance(result, dict) and result.get('enabled') is False)
                row['status'] = 'disabled' if disabled else 'ready'
            except Exception as exc:
                row.update(status='failed', error=f'{type(exc).__name__}: {exc}')
            row['finished_at'] = stamp()
        self.phase = 'ready'

    def snapshot(self):
        with self.lock:
            rows = []
            for key, entry in self.jobs.items():
                scheduler = entry['scheduler']
                job = scheduler.get_job(entry['job_id'])
                state = entry['status']
                if not scheduler.running:
                    state = 'stopped'
                elif job is None or getattr(job, 'next_run_time', None) is None:
                    state = 'disabled' if state != 'running' else state
                row = {k: v for k, v in entry.items() if k != 'scheduler'}
                row.update(id=key, status=state, executor='API', scheduler_online=scheduler.running,
                           scheduler_paused=scheduler.state == STATE_PAUSED,
                           next_run=job.next_run_time.isoformat() if job and getattr(job, 'next_run_time', None) else None,
                           window=str(job.trigger) if job else '任务已移除', business_status=entry.get('business_status', 'unverified'))
                if state == 'running':
                    row['running_seconds'] = round(time.monotonic()-entry['started_clock'], 1)
                row.pop('started_clock', None)
                probe = self.probes.get(entry['job_id'])
                if probe:
                    try:
                        business = probe() or {}
                        row.update(last_success=business.get('last_success_at'),
                                   error=business.get('last_error') or row.get('error'))
                        if business.get('last_error'):
                            row['business_status'] = 'blocked'
                            if state != 'running':
                                row['status'] = 'failed'
                        if business.get('active_task'):
                            active = business['active_task']
                            row.update(status='running', last_run=active.get('started_at') or active.get('created_at'),
                                       description=active.get('message'), progress=active.get('progress'))
                    except Exception as exc:
                        row.update(business_status='unknown', error=f'业务状态读取失败：{type(exc).__name__}')
                rows.append(row)
            blockers = [x['name'] for x in self.startups if x['status'] == 'failed']
            blockers += [x.owner for x in self.schedulers if (not x.running or x.state == STATE_PAUSED) and self.phase == 'ready']
            ready = self.phase == 'ready' and not blockers
            return {'generated_at': stamp(), 'instance_id': self.instance_id, 'pid': os.getpid(),
                    'started_at': self.started_at, 'phase': self.phase, 'ready': ready,
                    'status': 'ready' if ready else 'not_ready', 'blockers': blockers,
                    'startups': [dict(x) for x in self.startups], 'tasks': rows}

    def shutdown(self):
        self.phase = 'stopping'
        for scheduler in self.schedulers:
            if scheduler.running:
                scheduler.shutdown(wait=True)
        with self.lock:
            workers = list(self.workers)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join()
        self.phase = 'stopped'

    @contextmanager
    def worker(self):
        current = threading.current_thread()
        with self.lock:
            if self.phase in ('stopping', 'stopped'):
                raise RuntimeError('API is stopping; refresh rejected')
            self.workers.add(current)
        try:
            yield
        finally:
            with self.lock:
                self.workers.discard(current)


registry = SchedulerRegistry()


class ObservedScheduler(BackgroundScheduler):
    def __init__(self, *args, owner='API', telemetry=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.telemetry = telemetry or registry
        self.owner = owner
        self.telemetry.schedulers.append(self)
        self.add_listener(self._missed, EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES)

    def _missed(self, event):
        with self.telemetry.lock:
            row = self.telemetry.jobs.get(f'{self.owner}:{event.job_id}')
            if row:
                row['last_schedule_warning'] = '错过执行窗口' if event.code == EVENT_JOB_MISSED else '上次执行尚未结束，本次跳过'
                row['last_warning_at'] = stamp()
                row['missed_count'] = row.get('missed_count', 0) + 1

    def add_job(self, func, *args, **kwargs):
        if not callable(func):
            raise TypeError('Observed jobs must use a callable')
        job_id = kwargs.get('id') or uuid4().hex
        kwargs['id'] = job_id
        key = f'{self.owner}:{job_id}'
        with self.telemetry.lock:
            self.telemetry.jobs.setdefault(key, {'job_id': job_id, 'name': JOB_NAMES.get(job_id) or kwargs.get('name') or job_id,
                'owner': self.owner, 'scheduler': self, 'status': 'idle', 'runs': 0, 'failures': 0})

        @functools.wraps(func)
        def observed(*call_args, **call_kwargs):
            with self.telemetry.lock:
                row = self.telemetry.jobs[key]
                row.update(status='running', last_run=stamp(), started_clock=time.monotonic())
            try:
                result = func(*call_args, **call_kwargs)
                business_failed = isinstance(result, dict) and result.get('ok') is False
                with self.telemetry.lock:
                    row.update(status='failed' if business_failed else 'idle', error=(
                        str(result.get('reason') or result.get('error') or '业务结果ok=false')[:1000] if business_failed else None))
                    row['business_status'] = 'blocked' if business_failed else 'unverified'
                    if business_failed:
                        row['failures'] += 1
                    else:
                        row['last_success'] = stamp()
                return result
            except Exception as exc:
                with self.telemetry.lock:
                    row.update(status='failed', error=f'{type(exc).__name__}: {exc}'[:1000])
                    row['failures'] += 1
                raise
            finally:
                with self.telemetry.lock:
                    row['runs'] += 1
                    row['last_finished'] = stamp()
                    row['duration_seconds'] = round(time.monotonic()-row['started_clock'], 3)

        return super().add_job(observed, *args, **kwargs)
