"""Offline three-state calendar contract; all warehouse access is faked."""
from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import patch

from scheduler import trading_calendar as calendar


class CalendarThreeStateTests(TestCase):
    def setUp(self):
        calendar.TradingCalendar._LOOKUP_CACHE.clear()
        self.addCleanup(calendar.TradingCalendar._LOOKUP_CACHE.clear)

    def resolve(self, *, available=True, exists=True, row=None, bounds=(None, None), error=None,
                day=datetime(2026, 11, 10, 12)):
        def scalar(sql, params=None):
            if error:
                raise error
            if "SELECT is_trading" in sql:
                self.assertEqual(params, [day.date().isoformat()])
                return row
            if "min(trade_date)" in sql:
                return bounds[0]
            if "max(trade_date)" in sql:
                return bounds[1]
            self.fail("unexpected SQL")

        with patch.object(calendar, "clickhouse_available", return_value=available), \
             patch.object(calendar, "clickhouse_table_exists", return_value=exists), \
             patch.object(calendar, "clickhouse_scalar", side_effect=scalar):
            result = calendar.TradingCalendar.resolve_trading_day(day)
            return result, calendar.TradingCalendar.is_trading_day(day)

    def test_explicit_open_closed_and_weekend_override(self):
        for row, day, state, expected in (
            (1, datetime(2026, 11, 10, 12), "trading", True),
            (0, datetime(2026, 11, 10, 12), "closed", False),
            (1, datetime(2026, 11, 14, 12), "trading", True),
        ):
            with self.subTest(row=row, day=day):
                calendar.TradingCalendar._LOOKUP_CACHE.clear()
                result, value = self.resolve(row=row, day=day)
                self.assertEqual(result.state, state)
                self.assertEqual(result.source, "trade_calendar")
                self.assertTrue(result.evidence)
                self.assertIs(value, expected)

    def test_unknown_fail_closed(self):
        for inputs in (
            {"available": False},
            {"exists": False},
            {"error": RuntimeError("synthetic query failure")},
            {},
            {"bounds": (datetime(2026, 11, 1).date(), datetime(2026, 11, 9).date())},
        ):
            with self.subTest(inputs=inputs):
                calendar.TradingCalendar._LOOKUP_CACHE.clear()
                result, value = self.resolve(**inputs)
                self.assertEqual(result.state, "unknown")
                self.assertTrue(result.evidence)
                self.assertIs(value, False)

    def test_probe_exception_is_unknown(self):
        with patch.object(calendar, "clickhouse_available", side_effect=RuntimeError("probe")):
            result = calendar.TradingCalendar.resolve_trading_day(datetime(2026, 11, 10))
        self.assertEqual(result.state, "unknown")

    def test_sparse_in_bounds_keeps_existing_closed_behavior(self):
        result, value = self.resolve(bounds=(datetime(2026, 11, 1).date(), datetime(2026, 11, 30).date()))
        self.assertEqual(result.state, "closed")
        self.assertIs(value, False)

    def test_next_previous_unknown_and_datetime_bounds_terminate(self):
        for method, day in ((calendar.TradingCalendar.get_next_trading_day, datetime.max),
                            (calendar.TradingCalendar.get_previous_trading_day, datetime.min),
                            (calendar.TradingCalendar.get_next_trading_day, datetime(2026, 11, 10)),
                            (calendar.TradingCalendar.get_previous_trading_day, datetime(2026, 11, 10))):
            with self.subTest(method=method.__name__, day=day):
                calendar.TradingCalendar._LOOKUP_CACHE.clear()
                with patch.object(calendar, "clickhouse_available", return_value=False):
                    with self.assertRaises(calendar.CalendarUnknownError):
                        method(day)

    def test_next_previous_scan_fails_on_unknown_and_datetime_overflow(self):
        unknown = calendar.CalendarDayResult("unknown", "test", "synthetic")
        for method, day in ((calendar.TradingCalendar.get_next_trading_day, datetime.max),
                            (calendar.TradingCalendar.get_previous_trading_day, datetime.min),
                            (calendar.TradingCalendar.get_next_trading_day, datetime(2026, 11, 10)),
                            (calendar.TradingCalendar.get_previous_trading_day, datetime(2026, 11, 10))):
            with self.subTest(method=method.__name__, day=day):
                calendar.TradingCalendar._LOOKUP_CACHE.clear()
                with patch.object(calendar.TradingCalendar, "_db_calendar_available", return_value=True), \
                     patch.object(calendar, "clickhouse_scalar", return_value=None), \
                     patch.object(calendar.TradingCalendar, "resolve_trading_day", return_value=unknown) as resolve:
                    with self.assertRaises(calendar.CalendarUnknownError):
                        method(day)
                    self.assertLessEqual(resolve.call_count, 1)

    def test_adjacent_result_must_be_strictly_in_requested_direction(self):
        for method, returned in ((calendar.TradingCalendar.get_next_trading_day, datetime(2026, 11, 9).date()),
                                 (calendar.TradingCalendar.get_previous_trading_day, datetime(2026, 11, 11).date())):
            with self.subTest(method=method.__name__):
                calendar.TradingCalendar._LOOKUP_CACHE.clear()
                with patch.object(calendar.TradingCalendar, "_db_calendar_available", return_value=True), \
                     patch.object(calendar, "clickhouse_scalar", return_value=returned):
                    with self.assertRaises(calendar.CalendarUnknownError):
                        method(datetime(2026, 11, 10))

    def test_next_previous_scan_is_bounded_on_closed_days(self):
        result = calendar.CalendarDayResult("closed", "test", "synthetic")
        for method in (calendar.TradingCalendar.get_next_trading_day,
                       calendar.TradingCalendar.get_previous_trading_day):
            with self.subTest(method=method.__name__):
                with patch.object(calendar.TradingCalendar, "_db_calendar_available", return_value=True), \
                     patch.object(calendar, "clickhouse_scalar", return_value=None), \
                     patch.object(calendar.TradingCalendar, "resolve_trading_day", return_value=result) as resolve:
                    calendar.TradingCalendar._LOOKUP_CACHE.clear()
                    with self.assertRaises(calendar.CalendarUnknownError):
                        method(datetime(2026, 11, 10))
                    self.assertLessEqual(resolve.call_count, 30)

    def test_bounded_scan_finds_open_day(self):
        closed = calendar.CalendarDayResult("closed", "test", "synthetic")
        trading = calendar.CalendarDayResult("trading", "test", "synthetic")
        start = datetime(2026, 11, 10, 12)
        for method, delta in ((calendar.TradingCalendar.get_next_trading_day, 1),
                              (calendar.TradingCalendar.get_previous_trading_day, -1)):
            with self.subTest(method=method.__name__):
                calendar.TradingCalendar._LOOKUP_CACHE.clear()
                with patch.object(calendar.TradingCalendar, "_db_calendar_available", return_value=True), \
                     patch.object(calendar, "clickhouse_scalar", return_value=None), \
                     patch.object(calendar.TradingCalendar, "resolve_trading_day", side_effect=[closed, trading]):
                    self.assertEqual(method(start), start + timedelta(days=2 * delta))
