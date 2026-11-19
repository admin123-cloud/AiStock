"""工具模块（按基础设施域组织：db / kline / calendar / common / reconcile / market_sources）。

通过 MetaPathFinder 透明兼容历史扁平导入路径（如 utils.paths -> utils.common.paths），
同时保留原有顶级导出符号。
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import sys
from types import ModuleType

_LEGACY_MODULE_MAP: dict[str, str] = {
    # db
    "backtest_result_store": "utils.db.backtest_result_store",
    "ch_gateway": "utils.db.ch_gateway",
    "clickhouse_mutation_guard": "utils.db.clickhouse_mutation_guard",
    "clickhouse_schema": "utils.db.clickhouse_schema",
    "database": "utils.db.database",
    "db_write_lock": "utils.db.db_write_lock",
    "postclose_lock_context": "utils.db.postclose_lock_context",
    # kline
    "daily_closure_snapshot": "utils.kline.daily_closure_snapshot",
    "daily_coverage_audit": "utils.kline.daily_coverage_audit",
    "daily_quality_read": "utils.kline.daily_quality_read",
    "daily_write_guard": "utils.kline.daily_write_guard",
    "derived_minute_live": "utils.kline.derived_minute_live",
    "g3_mainwave_confirmation": "utils.kline.g3_mainwave_confirmation",
    "g3_minute_visibility": "utils.kline.g3_minute_visibility",
    "kline_history_policy": "utils.kline.kline_history_policy",
    "kline_store": "utils.kline.kline_store",
    "kline_units": "utils.kline.kline_units",
    "market_warehouse": "utils.kline.market_warehouse",
    "minute_daily_quality_gate": "utils.kline.minute_daily_quality_gate",
    "minute_table_ops": "utils.kline.minute_table_ops",
    "minute_volume": "utils.kline.minute_volume",
    "period_constants": "utils.kline.period_constants",
    "security_market_policy": "utils.kline.security_market_policy",
    "tdx_daily_backfill": "utils.kline.tdx_daily_backfill",
    "tdx_lc5": "utils.kline.tdx_lc5",
    # calendar
    "daily_date_universe": "utils.calendar.daily_date_universe",
    "market_phase_policy": "utils.calendar.market_phase_policy",
    "market_status": "utils.calendar.market_status",
    "trading_sessions": "utils.calendar.trading_sessions",
    # common
    "config": "utils.common.config",
    "decorators": "utils.common.decorators",
    "email_transport": "utils.common.email_transport",
    "exceptions": "utils.common.exceptions",
    "helpers": "utils.common.helpers",
    "logger": "utils.common.logger",
    "paths": "utils.common.paths",
    "strategy_contracts": "utils.common.strategy_contracts",
    "verified_zip": "utils.common.verified_zip",
    # reconcile
    "ocean_audit": "utils.reconcile.ocean_audit",
    "ocean_formal_units": "utils.reconcile.ocean_formal_units",
    "ocean_stage": "utils.reconcile.ocean_stage",
    "ocean_whitelist": "utils.reconcile.ocean_whitelist",
    "purchased_5m_reconciliation": "utils.reconcile.purchased_5m_reconciliation",
    "qmt_daily_prices": "utils.reconcile.qmt_daily_prices",
    "qmt_universe": "utils.reconcile.qmt_universe",
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


class _UtilsAliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):
        if not fullname.startswith("utils."):
            return None
        rest = fullname[len("utils."):]
        if "." in rest:
            return None
        target_name = _LEGACY_MODULE_MAP.get(rest)
        if not target_name:
            return None
        return importlib.machinery.ModuleSpec(fullname, _AliasLoader(target_name))


if not any(isinstance(f, _UtilsAliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _UtilsAliasFinder())


def __getattr__(name: str) -> ModuleType:
    target_name = _LEGACY_MODULE_MAP.get(name)
    if target_name is not None:
        mod = importlib.import_module(target_name)
        sys.modules[f"utils.{name}"] = mod
        globals()[name] = mod
        return mod
    raise AttributeError(f"module 'utils' has no attribute {name!r}")


from .common.logger import setup_logger, get_logger  # noqa: E402
from .common.config import config  # noqa: E402
from .db.database import DatabaseManager  # noqa: E402
from .common.helpers import (  # noqa: E402
    format_number,
    validate_stock_code,
    is_trading_day,
    calculate_change,
    format_stock_code,
    detect_market,
    parse_market_code,
    is_trading_time,
)
from .common.decorators import (  # noqa: E402
    retry,
    timer,
    log_execution,
    validate_args,
    cache_result,
    rate_limit,
    async_retry,
    async_timer,
    singleton,
    deprecated,
    catch_exceptions,
)
from .common.exceptions import (  # noqa: E402
    AiStockException,
    DataSourceException,
    DataSourceError,
    DataFetchError,
    DataParseError,
    DatabaseException,
    DatabaseError,
    ConnectionError,
    QueryError,
    ModelException,
    ModelError,
    ModelTrainingError,
    ModelPredictionError,
    StrategyError,
    BacktestError,
    ExecutionError,
    OrderError,
    RiskControlError,
    ConfigException,
    ConfigError,
    StockNotFoundError,
    InvalidStockCodeError,
    DataValidationError,
    TransactionError,
)

__all__ = [
    "setup_logger",
    "get_logger",
    "config",
    "DatabaseManager",
    "format_number",
    "validate_stock_code",
    "is_trading_day",
    "calculate_change",
    "format_stock_code",
    "detect_market",
    "parse_market_code",
    "is_trading_time",
    "retry",
    "timer",
    "log_execution",
    "validate_args",
    "cache_result",
    "rate_limit",
    "async_retry",
    "async_timer",
    "singleton",
    "deprecated",
    "catch_exceptions",
    "AiStockException",
    "DataSourceException",
    "DataSourceError",
    "DataFetchError",
    "DataParseError",
    "DatabaseException",
    "DatabaseError",
    "ConnectionError",
    "QueryError",
    "ModelException",
    "ModelError",
    "ModelTrainingError",
    "ModelPredictionError",
    "StrategyError",
    "BacktestError",
    "ExecutionError",
    "OrderError",
    "RiskControlError",
    "ConfigException",
    "ConfigError",
    "StockNotFoundError",
    "InvalidStockCodeError",
    "DataValidationError",
    "TransactionError",
]
