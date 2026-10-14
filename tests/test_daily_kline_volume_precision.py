import unittest
import json
from contextlib import ExitStack
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
from api import stocks


class DailyVolumePrecisionTest(unittest.TestCase):
    def response(self, period, clickhouse, volume, overrides=None):
        with ExitStack() as stack:
            manager = stack.enter_context(patch.object(stocks, "DatabaseSessionManager"))
            session = MagicMock()
            manager.get_session.return_value = session
            stack.enter_context(patch.object(stocks, "_get_stock_by_code", return_value=SimpleNamespace(name="测试指数")))
            stack.enter_context(patch.object(stocks, "clickhouse_available", return_value=clickhouse))
            stack.enter_context(patch.object(stocks, "_build_intraday_daily_bar", return_value=None))
            values = dict(open=100., high=102., low=99., close=101., volume=volume, amount=123456., code="000001.SH")
            values.update(overrides or {})
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

    def test_nonfinite_legacy_bars_serialize_as_unknown_without_losing_date(self):
        for clickhouse in [True, False]:
            for bad_value in [float("nan"), float("inf"), float("-inf")]:
                with self.subTest(clickhouse=clickhouse, bad_value=bad_value):
                    result = self.response("1d", clickhouse, bad_value, {"amount": bad_value})
                    bar = json.loads(json.dumps(result, allow_nan=False))["data"][0]
                    self.assertIsNone(bar["volume"])
                    self.assertIsNone(bar["amount"])
                    self.assertEqual(bar["close"], 101.)
                    self.assertEqual(bar["date"], "2026-08-03")

    def test_missing_legacy_values_remain_unknown(self):
        for clickhouse in [True, False]:
            result = self.response("daily", clickhouse, None, {"amount": None})
            self.assertIsNone(result["data"][0]["volume"])
            self.assertIsNone(result["data"][0]["amount"])

    def test_nonfinite_price_is_not_reported_as_zero(self):
        for clickhouse in [True, False]:
            result = self.response("1d", clickhouse, 1.97, {"open": float("nan")})
            self.assertIsNone(result["data"][0]["open"])
            self.assertEqual(result["data"][0]["volume"], 1.97)
            json.dumps(result, allow_nan=False)

if __name__ == "__main__":
    unittest.main()
