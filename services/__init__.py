"""Service package (按业务域组织：market / postclose / trading / broker / ops / operations / g3 / repositories).

Keep package initialization lightweight so individual services can be imported
without eagerly importing unrelated modules with optional dependencies.
同时通过 MetaPathFinder 透明兼容历史扁平导入路径（如 services.t_trade_signal -> services.trading.t_trade_signal）。
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import sys
from types import ModuleType

_LEGACY_MODULE_MAP: dict[str, str] = {
    # market
    "cls_news_service": "services.market.cls_news_service",
    "intraday_market_snapshot": "services.market.intraday_market_snapshot",
    "intraday_phase_gate": "services.market.intraday_phase_gate",
    "intraday_price_feed": "services.market.intraday_price_feed",
    "margin_sentiment": "services.market.margin_sentiment",
    "market_breadth": "services.market.market_breadth",
    "market_mood": "services.market.market_mood",
    "market_sentiment_cache": "services.market.market_sentiment_cache",
    "market_turnover_forecast": "services.market.market_turnover_forecast",
    "stock_service": "services.market.stock_service",
    # postclose
    "postclose_alerts": "services.postclose.postclose_alerts",
    "postclose_backup": "services.postclose.postclose_backup",
    "postclose_cleanup": "services.postclose.postclose_cleanup",
    "postclose_contracts": "services.postclose.postclose_contracts",
    "postclose_journal": "services.postclose.postclose_journal",
    "postclose_orchestrator": "services.postclose.postclose_orchestrator",
    "postclose_publication": "services.postclose.postclose_publication",
    "postclose_source_prepare": "services.postclose.postclose_source_prepare",
    # trading
    "daily_trend_exit_monitor": "services.trading.daily_trend_exit_monitor",
    "g3_holding_t_daily_review": "services.trading.g3_holding_t_daily_review",
    "g3_holding_t_paper_monitor": "services.trading.g3_holding_t_paper_monitor",
    "g3_holding_t_portfolio_state": "services.trading.g3_holding_t_portfolio_state",
    "holding_t_watchlist": "services.trading.holding_t_watchlist",
    "l2_fill_closure": "services.trading.l2_fill_closure",
    "l2_order_gate": "services.trading.l2_order_gate",
    "t_tracker_alerts": "services.trading.t_tracker_alerts",
    "t_trade_coaching": "services.trading.t_trade_coaching",
    "t_trade_shadow": "services.trading.t_trade_shadow",
    "t_trade_signal": "services.trading.t_trade_signal",
    # broker
    "broker_accounts": "services.broker.broker_accounts",
    "broker_state_autosync": "services.broker.broker_state_autosync",
    "tdx_gateway": "services.broker.tdx_gateway",
    "ths_account_bridge": "services.broker.ths_account_bridge",
    "ths_holdings_poll": "services.broker.ths_holdings_poll",
    "ths_hotswitch": "services.broker.ths_hotswitch",
    "ths_prefill_gate": "services.broker.ths_prefill_gate",
    # ops
    "cache": "services.ops.cache",
    "data_delivery": "services.ops.data_delivery",
    "data_storage": "services.ops.data_storage",
    "market_write_admission": "services.ops.market_write_admission",
    "monitoring": "services.ops.monitoring",
    "operations_incidents": "services.ops.operations_incidents",
    "runtime_health": "services.ops.runtime_health",
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


class _ServicesAliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):
        if not fullname.startswith("services."):
            return None
        rest = fullname[len("services."):]
        if "." in rest:
            return None
        target_name = _LEGACY_MODULE_MAP.get(rest)
        if not target_name:
            return None
        return importlib.machinery.ModuleSpec(fullname, _AliasLoader(target_name))


if not any(isinstance(f, _ServicesAliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _ServicesAliasFinder())


def __getattr__(name: str) -> ModuleType:
    target_name = _LEGACY_MODULE_MAP.get(name)
    if target_name is not None:
        mod = importlib.import_module(target_name)
        sys.modules[f"services.{name}"] = mod
        globals()[name] = mod
        return mod
    raise AttributeError(f"module 'services' has no attribute {name!r}")


__all__ = []
