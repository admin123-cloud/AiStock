"""Fixed index-only daily repair entry; never expands into stocks or minutes."""
import argparse
from datetime import date, datetime
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def canonical_arguments(args):
    start, end = date.fromisoformat(args.start_date), date.fromisoformat(args.end_date)
    if start > end or (end-start).days > 31:
        raise ValueError('Index daily repair supports an ordered range of at most 32 calendar days')
    return ['--mode', 'date-repair', '--scenario', 'after-close', '--universe', 'index',
            '--start-date', start.isoformat(), '--end-date', end.isoformat(),
            '--daily-batch-size', str(max(1, min(args.batch_size, 80))), '--no-with-minutes']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start-date', required=True)
    parser.add_argument('--end-date', required=True)
    parser.add_argument('--batch-size', type=int, default=40)
    args = parser.parse_args()
    arguments = canonical_arguments(args)
    from services.operations.qmt_download_queue import protected_session
    if protected_session(datetime.now(ZoneInfo('Asia/Shanghai'))):
        if os.getenv('AISTOCK_BACKLOG_REPLAY') != '1':
            from services.operations.ingestion_backlog import enqueue
            enqueue(list(sys.argv[1:]), kind='index_daily')
        return 75
    from scripts import qmt_xtquant_data_source_task as collector
    sys.argv = [str(ROOT/'scripts/qmt_xtquant_data_source_task.py'), *arguments]
    return collector.main()


if __name__ == '__main__':
    raise SystemExit(main())
