import unittest
from contextlib import ExitStack
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
from api import stocks


class DailyVolumePrecisionTest(unittest.TestCase):
    def response(self, period, clickhouse, volume):
        with ExitStack() as stack:
            manager = stack.enter_context(patch.object(stocks, "DatabaseSessionManager"))
            session = MagicMock()
            manager.get_session.return_value = session
            stack.enter_context(patch.object(stocks, "_get_stock_by_code", return_value=SimpleNamespace(name="测试指数")))
            stack.enter_context(patch.object(stocks, "clickhouse_available", return_value=clickhouse))
            stack.enter_context(patch.object(stocks, "_build_intraday_daily_bar", return_value=None))
            values = dict(open=100., high=102., low=99., close=101., volume=volume, amount=123456., code="000001.SH")
            df = pd.DataFrame([dict(date_value=date(2026, 8, 3), **values)])
            stack.enter_context(patch.object(stocks, "clickhouse_query_df", return_value=df))
            session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [SimpleNamespace(trade_date=date(2026, 8, 3), **values)]
            return stocks.get_stock_kline("000001.SH", period, limit=10)

    def test_clickhouse_daily_preserves_fractional_lots(self):
        result = self.response("1d", True, 29517.08)
        self.assertEqual(result["data"][0]["volume"], 29517.08)
        self.assertEqual(result["source"], "clickhouse")

    def test_daily_alias_preserves_small_historical_quantity(self):
        self.assertEqual(self.response("daily", True, 1.97)["data"][0]["volume"], 1.97)

    def test_sql_fallback_preserves_fractional_lots(self):
        self.assertEqual(self.response("1d", False, 34162.5)["data"][0]["volume"], 34162.5)

    def test_daily_zero_remains_zero(self):
        self.assertEqual(self.response("1d", True, 0.)["data"][0]["volume"], 0.)

if __name__ == "__main__":
    unittest.main()
