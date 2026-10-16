from decimal import Decimal
import unittest
from unittest.mock import patch
import pandas as pd
from utils.minute_volume import split_volume, prepare_min5_volumes, precise_minute_read_sql, quantity_json
from utils.daily_quality_read import quality_read_sql


class ExactMin5VolumeTests(unittest.TestCase):
    def test_full_int64_domain_and_null_stay_legacy(self):
        for value in [None, 0, 2**53+1, 2**63-1]:
            self.assertEqual(split_volume(value), (value, None))

    def test_float32_fraction_stays_exact(self):
        for value in [0.5, 1750375.375, 0.10000000149011612]:
            legacy, exact = split_volume(value)
            self.assertIsNone(legacy)
            self.assertEqual(exact, Decimal.from_float(value))
            self.assertEqual(quantity_json(exact), value)

    def test_no_rounding_on_precision_or_range_overflow(self):
        for value in [Decimal('1e-46'), Decimal('1e31'), -1, float('inf')]:
            with self.assertRaises(ValueError):split_volume(value)

    def test_dataframe_never_coerces_large_legacy_integer(self):
        source=pd.DataFrame({'volume':pd.Series([2**63-1, 0.5, None],dtype=object),'open':[1.,2.,3.]})
        frame=prepare_min5_volumes(source)
        self.assertEqual(frame.volume.tolist(),[2**63-1,None,None])
        self.assertEqual(frame.volume_exact.tolist(),[None,Decimal('0.5'),None])
        self.assertEqual(source.volume.iloc[1],0.5)

    def test_precise_read_rewrites_alias_cte_and_final(self):
        for sql in ['SELECT kline_minute_5.volume FROM stock.kline_minute_5 FINAL',
                    'WITH x AS (SELECT * FROM kline_minute_5 AS k FINAL) SELECT * FROM x']:
            result=quality_read_sql(sql)
            self.assertIn('kline_minute_5_precise',result)
            self.assertNotIn(' FINAL',result)

    def test_literals_other_periods_and_writes_not_redirected(self):
        for sql in ["SELECT 'FROM kline_minute_5 FINAL' FROM stocks",'SELECT * FROM kline_minute_15 FINAL',
                    'INSERT INTO kline_minute_5 SELECT * FROM kline_minute_5',
                    'WITH x AS (SELECT * FROM kline_minute_5) INSERT INTO kline_minute_5 SELECT * FROM x']:
            self.assertEqual(precise_minute_read_sql(sql),sql)

    def test_json_legacy_integer_does_not_pass_through_float(self):
        self.assertEqual(quantity_json(Decimal(2**63-1)),2**63-1)

    def test_api_5m_keeps_fraction_and_integer(self):
        from api import stocks
        from types import SimpleNamespace
        from contextlib import ExitStack
        for value in [Decimal('0.5'),Decimal(2**63-1)]:
            with ExitStack() as stack:
                stack.enter_context(patch.object(stocks.DatabaseSessionManager,'get_session'))
                stack.enter_context(patch.object(stocks,'_get_stock_by_code',return_value=SimpleNamespace(name='指数')))
                stack.enter_context(patch.object(stocks,'clickhouse_available',return_value=True))
                frame=pd.DataFrame([dict(date_value=pd.Timestamp('2012-01-12 09:35'),code='000016.SH',open=1.,high=1.,low=1.,close=1.,volume=value,amount=2.)])
                stack.enter_context(patch.object(stocks,'clickhouse_query_df',return_value=frame))
                self.assertEqual(stocks.get_stock_kline('000016.SH','5m')['data'][0]['volume'],value)

if __name__=='__main__':unittest.main()
