"""Minute K-line sync task.

The strategy chain mainly depends on 15m/30m bars. 1m is intentionally not
pulled here because it is expensive and low-value for the current G2 flow.
"""

from __future__ import annotations

import asyncio
from datetime import date

from utils.logger import get_logger

logger = get_logger("MinuteKlineTask")


class MinuteKlineTask:
    """Sync recent intraday bars needed by strategy refresh."""

    def __init__(self):
        self.periods = ["15m", "30m"]

    async def execute(self) -> int:
        from scripts.sync_intraday_minutes_fast import main as sync_intraday_minutes_fast_main

        logger.info("Start minute K-line sync task, periods=%s", ",".join(self.periods))

        def _run() -> int:
            import sys

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "sync_intraday_minutes_fast.py",
                    "--target-date",
                    str(date.today()),
                    "--types",
                    "stock,index",
                    "--periods",
                    ",".join(self.periods),
                    "--batch-size",
                    "500",
                    "--min-complete-codes",
                    "3000",
                ]
                return int(sync_intraday_minutes_fast_main() or 0)
            finally:
                sys.argv = old_argv

        return await asyncio.to_thread(_run)
