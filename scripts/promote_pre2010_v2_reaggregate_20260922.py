import clickhouse_connect

ch = clickhouse_connect.get_client(host='localhost', port=8123, username='default')
ch.command('SET max_partitions_per_insert_block=1000')

for freq, bars, slot in [('30',8,6), ('60',4,12)]:
    suf = '20260922_formal5m_v2'
    audit = f'stock.pre2010_bc_reaggregate_{freq}m_audit_{suf}'
    raw = f'stock.pre2010_bc_reaggregate_{freq}m_raw_{suf}'
    cand = f'stock.pre2010_refit_confirmed_{freq}m_20260922_v2'
    akey = f'stock.pre2010_refit_a_keys_{freq}m_20260922_v2'
    bbase = f'stock.pre2010_refit_b_base_{freq}m_20260922_v2'
    bkey = f'stock.pre2010_refit_b_keys_{freq}m_20260922_v2'
    fagg = f'stock.pre2010_refit_formal_agg_{freq}m_20260922_v2'
    rbad = f'stock.pre2010_refit_raw_bad_{freq}m_20260922_v2'
    for t in [cand, akey, bbase, bkey, fagg, rbad]: ch.command(f'DROP TABLE IF EXISTS {t}')
    ch.command(f'''CREATE TABLE {cand} ENGINE=MergeTree ORDER BY (code,datetime) SETTINGS allow_nullable_key=1 AS
        SELECT * FROM stock.kline_minute_{freq} LIMIT 0''')
    ch.command(f'''CREATE TABLE {akey} ENGINE=MergeTree ORDER BY (code,trade_date) SETTINGS allow_nullable_key=1 AS
        SELECT code, trade_date FROM {audit} LIMIT 0''')
    ch.command(f'''INSERT INTO {akey}
        SELECT code, trade_date FROM {audit}
        WHERE grade='A' AND candidate_bars={bars}
          AND price_relative_diff <= 0.005
          AND volume_relative_diff <= 0.001
          AND amount_relative_diff <= 0.01''')
    ch.command(f'''INSERT INTO {cand}
        SELECT r.* FROM {raw} r INNER JOIN {akey} k
          ON r.code=k.code AND toDate(r.datetime)=k.trade_date
        WHERE r.code IS NOT NULL AND r.datetime IS NOT NULL''')
    ch.command(f'''CREATE TABLE {fagg} ENGINE=MergeTree ORDER BY (code,trade_date) SETTINGS allow_nullable_key=1 AS
        SELECT code, toDate(datetime) trade_date, argMax(close,datetime) close,
               sum(volume) volume, sum(amount) amount, count() cnt
        FROM stock.kline_minute_{freq} GROUP BY code, toDate(datetime)''')
    ch.command(f'''CREATE TABLE {rbad} ENGINE=MergeTree ORDER BY (code,trade_date) SETTINGS allow_nullable_key=1 AS
        SELECT code, toDate(datetime) trade_date,
               countIf(code IS NULL OR datetime IS NULL OR open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL OR volume IS NULL OR amount IS NULL OR high < greatest(open,close) OR low > least(open,close) OR low < 0 OR volume < 0 OR amount < 0) bad
        FROM {raw} GROUP BY code, toDate(datetime)''')
    ch.command(f'''CREATE TABLE {bbase} ENGINE=MergeTree ORDER BY (code,trade_date) SETTINGS allow_nullable_key=1 AS
        SELECT a.code, a.trade_date, a.candidate_close, a.candidate_volume, a.candidate_amount,
          a.price_relative_diff cp, a.volume_relative_diff cv, a.amount_relative_diff ca,
          if(f.cnt=0, 1.0, abs(a.candidate_close-f.close)/nullIf(f.close,0)) fp,
          if(f.cnt=0, 1.0, abs(a.candidate_volume-f.volume)/nullIf(f.volume,0)) fv,
          if(f.cnt=0, 1.0, abs(a.candidate_amount-f.amount)/nullIf(f.amount,0)) fa,
          if(r.bad=0,0,1) bad
        FROM {audit} a
        LEFT JOIN {fagg} f
          ON a.code=f.code AND a.trade_date=f.trade_date
        LEFT JOIN {rbad} r
          ON a.code=r.code AND a.trade_date=r.trade_date
        WHERE a.grade='B' AND a.candidate_bars={bars}''')
    ch.command(f'''CREATE TABLE {bkey} ENGINE=MergeTree ORDER BY (code,trade_date) SETTINGS allow_nullable_key=1 AS
        SELECT code, trade_date FROM {bbase}
        WHERE bad=0 AND fp <= 0.02 AND fv <= 0.15 AND fa <= 0.25
          AND (cp+cv+ca) < (fp+fv+fa)''')
    ch.command(f'''INSERT INTO {cand}
        SELECT r.* FROM {raw} r INNER JOIN {bkey} k
          ON r.code=k.code AND toDate(r.datetime)=k.trade_date
        WHERE r.code IS NOT NULL AND r.datetime IS NOT NULL''')
    print(freq, 'A keys', ch.query(f'SELECT count() FROM {akey}').result_rows[0][0],
          'B keys', ch.query(f'SELECT count() FROM {bkey}').result_rows[0][0],
          'candidate rows', ch.query(f'SELECT count() FROM {cand}').result_rows[0][0])
