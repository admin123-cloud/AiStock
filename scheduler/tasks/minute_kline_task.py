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
        from scripts.qmt_xtquant_minute_backfill_validate import main as qmt_xtquant_minute_main

        logger.info("Start minute K-line sync task, periods=%s", ",".join(self.periods))

        def _run() -> int:
            import sys

            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "qmt_xtquant_minute_backfill_validate.py",
                    "--phase",
                    "all",
                    "--start-date",
                    str(date.today()),
                    "--end-date",
                    str(date.today()),
                    "--periods",
                    ",".join(self.periods),
                    "--batch-size",
                    "30",
                    "--include-index",
                    "--reset-stage",
                ]
                return int(qmt_xtquant_minute_main() or 0)
            finally:
                sys.argv = old_argv

        return await asyncio.to_thread(_run)
