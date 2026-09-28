"""Attribute T+1 price-proxy losses; does not simulate executable orders."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path


def run(source: Path, output: Path) -> dict:
    positions = pd.read_parquet(source)
    positions['entry_date'] = pd.to_datetime(positions['entry_date'])
    codes = ','.join("'" + c.replace("'", "''") + "'" for c in sorted(positions.code.unique()))
    bars = clickhouse_query_df(
        f"SELECT code,trade_date,open,close FROM kline_daily WHERE code IN ({codes}) "
        "AND trade_date BETWEEN '2019-12-01' AND '2026-09-24' ORDER BY code,trade_date"
    )
    bars['trade_date'] = pd.to_datetime(bars.trade_date)
    if bars.duplicated(['code', 'trade_date']).any():
        raise ValueError('Duplicate daily keys: cannot attribute returns')
    bars['exit_open'] = bars.groupby('code').open.shift(-1)
    bars['exit_date'] = bars.groupby('code').trade_date.shift(-1)
    bars = bars.rename(columns={'trade_date': 'entry_date', 'open': 'audit_open', 'close': 'audit_close'})
    joined = positions.merge(bars, on=['code', 'entry_date'], how='left', validate='many_to_one')
    joined['intraday'] = joined.audit_close / joined.actual_entry_open - 1
    joined['overnight'] = joined.exit_open / joined.audit_close - 1
    joined['tplus1_gross'] = joined.exit_open / joined.actual_entry_open - 1
    calendar = clickhouse_query_df("SELECT trade_date FROM kline_daily WHERE code='000300.SH' ORDER BY trade_date")
    dates = pd.to_datetime(calendar.trade_date).drop_duplicates().sort_values().tolist()
    next_date = dict(zip(dates[:-1], dates[1:]))
    joined['expected_exit_date'] = joined.entry_date.map(next_date)
    joined['exit_calendar_mismatch'] = joined.exit_date.ne(joined.expected_exit_date)
    segments = []
    for name, group in [('all', joined), ('calendar_aligned', joined[~joined.exit_calendar_mismatch]),
                        ('2025_2026', joined[joined.entry_date >= '2025-01-01'])]:
        valid = group[(group.exit_open > 0) & (group.actual_entry_open > 0)]
        events = (valid.tplus1_gross - .01).groupby(valid.trade_date).mean().sort_index()
        equity = (1 + events).cumprod()
        peak = equity.cummax().clip(lower=1)
        segments.append({'segment': name, 'positions': len(valid), 'events': len(events),
                         'intraday_mean': float(valid.intraday.mean()),
                         'overnight_mean': float(valid.overnight.mean()),
                         'proxy_return_100bps': float(equity.iloc[-1] - 1) if len(equity) else None,
                         'proxy_drawdown': float((equity / peak - 1).min()) if len(equity) else None})
    summary = {'execution_verified': False, 'kind': 'existing_sample_attribution_only',
               'source': str(source), 'positions': len(joined),
               'entry_open_mismatches': int(((joined.audit_open / joined.actual_entry_open - 1).abs() > 1e-6).sum()),
               'exit_calendar_mismatches': int(joined.exit_calendar_mismatch.sum()), 'segments': segments}
    output.mkdir(parents=True, exist_ok=True)
    joined.to_parquet(output / 'position_attribution.parquet', index=False)
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', type=Path, default=report_path('panic_tplus1_attribution_20260926'))
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.output), ensure_ascii=False, indent=2))
