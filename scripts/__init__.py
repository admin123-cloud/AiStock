"""scripts 兼容包。

生产管道脚本已实体迁入 pipelines.jobs，宿主机 GUI 桥接脚本已实体迁入 host_bridges，
API 依赖的计算/同步引擎已升格入 services.engines。
本模块通过 MetaPathFinder 透明兼容历史 scripts.<name> 导入路径。
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import sys
from types import ModuleType

_LEGACY_MODULE_MAP: dict[str, str] = {
    # pipelines.jobs (生产容器调度任务实体)
    "alert_local": "pipelines.jobs.alert_local",
    "broker_state_sync": "pipelines.jobs.broker_state_sync",
    "daily_kline_pipeline": "pipelines.jobs.daily_kline_pipeline",
    "holding_tick_qmt_alert": "pipelines.jobs.holding_tick_qmt_alert",
    "intraday_kline_upsert": "pipelines.jobs.intraday_kline_upsert",
    "intraday_snapshot_collect": "pipelines.jobs.intraday_snapshot_collect",
    "market_mood_update": "pipelines.jobs.market_mood_update",
    "minute_agg_backfill_window": "pipelines.jobs.minute_agg_backfill_window",
    "minute_collect_auto": "pipelines.jobs.minute_collect_auto",
    "run_weekly_data_audit": "pipelines.jobs.run_weekly_data_audit",
    "signal_quality_report": "pipelines.jobs.signal_quality_report",
    "t_trade_tracker": "pipelines.jobs.t_trade_tracker",
    # host_bridges (宿主机 Windows GUI 桥接实体)
    "l2_order_filler": "host_bridges.l2_order_filler",
    "tdx_postclose_client": "host_bridges.tdx_postclose_client",
    "ths_desktop_bridge": "host_bridges.ths_desktop_bridge",
    "ths_read_account_task": "host_bridges.ths_read_account_task",
    # services.engines (原被 API 反向依赖的引擎模块)
    "collect_intraday_snapshots": "services.engines.collect_intraday_snapshots",
    "generate_emotion_cycle": "services.engines.generate_emotion_cycle",
    "repair_kline_daily_metrics": "services.engines.repair_kline_daily_metrics",
    "sync_trade_calendar": "services.engines.sync_trade_calendar",
}


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target_name: str) -> None:
        self.target_name = target_name

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        mod = importlib.import_module(self.target_name)
        sys.modules[spec.name] = mod
        return mod

    def exec_module(self, module: ModuleType) -> None:
        return None


class _ScriptsAliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):
        if not fullname.startswith("scripts."):
            return None
        rest = fullname[len("scripts."):]
        if "." in rest:
            return None
        target_name = _LEGACY_MODULE_MAP.get(rest)
        if not target_name:
            return None
        return importlib.machinery.ModuleSpec(fullname, _AliasLoader(target_name))


if not any(isinstance(f, _ScriptsAliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _ScriptsAliasFinder())


def __getattr__(name: str) -> ModuleType:
    target_name = _LEGACY_MODULE_MAP.get(name)
    if target_name is not None:
        mod = importlib.import_module(target_name)
        sys.modules[f"scripts.{name}"] = mod
        globals()[name] = mod
        return mod
    raise AttributeError(f"module 'scripts' has no attribute {name!r}")
