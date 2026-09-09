"""Formal API lifecycle and single-owner lock; no producer work on import."""
import asyncio
import os
from pathlib import Path

from services.operations.schedulers import registry
from services.operations.health import write_snapshot


class InstanceLock:
    def __init__(self, path: Path):
        self.path, self.handle = path, None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            handle.close()
            raise RuntimeError('Another API/publisher instance owns this runtime lock')
        self.handle = handle

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None


def startup_specs():
    # Each initializer has its own failure boundary, including each G3 capability.
    from api import system_config as system, trading, gen3_state_alpha as g3
    registry.probes.update({
        g3._monitor_job_id: lambda: {**g3._load_monitor_state(), 'active_task': g3._running_refresh_task()},
        g3._exit_monitor_job_id: g3._load_exit_monitor_state,
        g3._daily_trend_exit_monitor_job_id: g3._load_daily_trend_exit_monitor_state,
        g3._observation_job_id: g3._load_observation_scheduler_state,
        g3._broker_sync_job_id: g3._load_broker_sync_state,
        g3._observation_retry_job_id: g3._load_observation_scheduler_state,
        trading._v4_monitor_job_id: trading._load_v4_monitor_state,
        trading._gen2_shadow_monitor_job_id: trading._load_gen2_shadow_monitor_state,
        trading._gen2_strategy_refresh_job_id: trading._load_gen2_strategy_refresh_state,
    })
    return [
        ('核心数据维护（默认由Windows负责）', system.start_core_data_maintenance_scheduler),
        ('持仓监控', trading.init_v4_manual_holdings_monitor_scheduler_from_config),
        ('G2影子买点监控', trading.init_gen2_shadow_buy_monitor_scheduler_from_config),
        ('G2策略刷新', trading.init_gen2_strategy_refresh_scheduler_from_config),
        ('G3候选刷新', g3.configure_shadow_monitor_scheduler),
        ('G3影子退出', g3.configure_shadow_exit_monitor_scheduler),
        ('G3趋势退出', g3.configure_daily_trend_exit_monitor_scheduler),
        ('G3每日观察', g3.configure_observation_scheduler),
        ('真实账户盘后同步', g3.configure_broker_sync_scheduler),
        ('G3自动委托（遵循现有开关）', g3.configure_auto_order_scheduler),
        ('启动参考资料同步', system.maybe_run_startup_reference_sync),
    ]


async def publish_heartbeat(path):
    while True:
        try:
            write_snapshot(registry.snapshot(), path)
        except OSError:
            # HTTP serves live telemetry; disk publication failure is exposed there.
            registry.phase = 'telemetry_write_failed'
        else:
            if registry.phase == 'telemetry_write_failed':
                registry.phase = 'ready'
        await asyncio.sleep(10)
