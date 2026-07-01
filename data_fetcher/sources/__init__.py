"""
Data source modules.
"""

__all__ = [
    "QmtMiniMarketClient",
    "QmtMiniDataSource",
    "QmtMiniTradingClient",
]


def __getattr__(name):
    if name in {"QmtMiniMarketClient", "QmtMiniTradingClient"}:
        from .qmtmini_client import QmtMiniMarketClient, QmtMiniTradingClient

        return {
            "QmtMiniMarketClient": QmtMiniMarketClient,
            "QmtMiniTradingClient": QmtMiniTradingClient,
        }[name]
    if name == "QmtMiniDataSource":
        from .qmtmini import QmtMiniDataSource

        return QmtMiniDataSource
    raise AttributeError(name)
