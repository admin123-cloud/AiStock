"""Year-bounded diagnostic, with warmup and following-year exit prices."""
import sys
import json
import time
from pathlib import Path
from argparse import Namespace
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.backtest_sector_pullback_reclaim_fixed_v1 import run
from utils.paths import report_path


def main():
    directory = report_path('sector_mainwave_leader_rebuild_v2')
    all_rows = []
    for year in range(2020, 2027):
        started = time.perf_counter()
        args = Namespace(start=f'{year-1}-11-01', end=min(f'{year+1}-01-20', '2026-09-24'),
                         snapshot='2026-07-06', breadth=.55, min_amount=100_000_000,
                         min_ret15=.10, min_excess=.20, min_pullback=.02,
                         max_pullback=.12, slots=3, cost_bps=200,
                         output_name=f'history_relative20_{year}.json')
        run(args)
        rows = json.loads((directory / args.output_name).read_text(encoding='utf-8'))['rows']
        all_rows.extend(r for r in rows if r['signal_date'].startswith(str(year)))
        print(f'{year} completed in {time.perf_counter()-started:.1f}s', flush=True)
    df = pd.DataFrame(all_rows)
    df['year'] = df.signal_date.str[:4]
    summary = []
    for (year, variant), group in df.groupby(['year', 'variant']):
        for h in (3, 5):
            gross = pd.to_numeric(group[f'ret_{h}d'], errors='coerce').dropna() + .02
            for cost in (20, 100, 200, 300):
                net = gross - cost / 10000
                summary.append(dict(year=year, variant=variant, horizon=h, cost_bps=cost,
                                    n=len(net), mean=float(net.mean()), median=float(net.median()),
                                    positive_rate=float((net>0).mean())))
    out = dict(research_only=True, execution_valid=False, historical_membership_bias=True,
               independent_holdout=False, account_returns=False, rows=summary,
               limitations=['frozen_2026_membership_survivorship_and_lookahead',
                            'overlapping_events_not_portfolio', 'fills_price_basis_unverified',
                            '2026_incomplete_and_latest_6_sessions_excluded'])
    (directory/'relative_history_summary.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
    df.to_parquet(directory/'relative_history_events.parquet',index=False)
    assert not df.duplicated(['variant','signal_date','code']).any()
    assert (df.entry_date > df.signal_date).all()
    print('summary written; timing and uniqueness checks passed', flush=True)


if __name__ == '__main__':
    main()
