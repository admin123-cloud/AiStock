import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('quality_sql',Path(__file__).resolve().parents[1]/'utils/daily_quality_read.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class QualityReadTests(unittest.TestCase):
 def test_orm_qualified_columns_and_final(self):
  q=m.quality_read_sql('SELECT kline_daily.close FROM kline_daily FINAL WHERE code=?')
  self.assertIn('AS kline_daily',q);self.assertNotIn(' FINAL',q);self.assertIn(m.QUALITY_VIEW,q)
 def test_join_alias_and_cte(self):
  q=m.quality_read_sql('WITH x AS (SELECT code FROM stock.kline_daily FINAL) SELECT k.close FROM kline_daily AS k FINAL JOIN x ON x.code=k.code')
  self.assertEqual(q.count(m.QUALITY_VIEW),2);self.assertIn('AS k ',q)
 def test_comments_and_literals_untouched(self):
  q="SELECT 'FROM kline_daily FINAL' AS label FROM stocks -- JOIN kline_daily"
  self.assertEqual(m.quality_read_sql(q),q)
 def test_mutations_never_target_quality_view(self):
  for q in ['INSERT INTO kline_daily SELECT * FROM kline_daily','ALTER TABLE kline_daily DELETE WHERE code=1','WITH x AS (SELECT * FROM kline_daily) INSERT INTO kline_daily SELECT * FROM x']:
   self.assertEqual(m.quality_read_sql(q),q)
 def test_other_periods_and_intraday_untouched(self):
  for name in ['kline_daily_intraday','kline_minute_15','kline_weekly']:
   q='SELECT * FROM '+name;self.assertEqual(m.quality_read_sql(q),q)
 def test_idempotent(self):
  q=m.quality_read_sql('SELECT * FROM kline_daily');self.assertEqual(m.quality_read_sql(q),q)
 def test_nullable_number_preserves_zero(self):
  for x in [None,float('nan'),float('inf'),'bad']:self.assertIsNone(m.finite_number(x))
  self.assertEqual(m.finite_number(0),0)

if __name__=='__main__':unittest.main()
