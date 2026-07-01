"""
Data fetching package.
"""

__all__ = [
    "BaseDataSource",
    "DataCleaner",
    "QmtMiniMarketClient",
    "QmtMiniTradingClient",
    "clean_dataframe",
]


def __getattr__(name):
    if name == "BaseDataSource":
        from .base_fetcher import BaseDataSource

        return BaseDataSource
    if name in {"QmtMiniMarketClient", "QmtMiniTradingClient"}:
        from .sources import QmtMiniMarketClient, QmtMiniTradingClient

        return {
            "QmtMiniMarketClient": QmtMiniMarketClient,
            "QmtMiniTradingClient": QmtMiniTradingClient,
        }[name]
    if name in {"DataCleaner", "clean_dataframe"}:
        from .data_cleaner import DataCleaner, clean_dataframe

        return {
            "DataCleaner": DataCleaner,
            "clean_dataframe": clean_dataframe,
        }[name]
    raise AttributeError(name)
