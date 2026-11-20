"""Task package exports with fault-tolerant imports.

Some environments may contain incomplete task files. We avoid importing all
submodules unconditionally so one broken task does not block the whole package.
"""

from utils.logger import get_logger

logger = get_logger("scheduler.tasks")

__all__ = []


def _safe_export(module_name: str, symbol_name: str):
    try:
        module = __import__(f"{__name__}.{module_name}", fromlist=[symbol_name])
        symbol = getattr(module, symbol_name)
        globals()[symbol_name] = symbol
        __all__.append(symbol_name)
    except Exception as exc:
        logger.warning(f"Skip task export {module_name}.{symbol_name}: {exc}")


_safe_export("full_sync_kline_task", "FullSyncKlineTask")
_safe_export("realtime_quotes_task", "RealtimeQuotesTask")
_safe_export("realtime_indices_task", "RealtimeIndicesTask")
_safe_export("intraday_sentiment_task", "IntradaySentimentTask")
_safe_export("minute_kline_task", "MinuteKlineTask")
