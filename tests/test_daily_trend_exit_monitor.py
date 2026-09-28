import unittest

from services.daily_trend_exit_monitor import evaluate_daily_rising_trend_exit, evaluate_intraday_rising_trend_line


def _bars(closes: list[float], lows: list[float]) -> list[dict]:
    return [
        {"trade_date": f"2026-01-{index + 1:02d}", "close": close, "low": low}
        for index, (close, low) in enumerate(zip(closes, lows))
    ]


class DailyTrendExitMonitorTests(unittest.TestCase):
    def test_daily_close_below_line_requires_sell_next_open(self) -> None:
        lows = [10, 9, 8, 7, 8, 9, 10, 9, 8, 9, 10, 11, 12, 11, 10, 11, 12, 13, 14, 13, 12, 13, 14, 15]
        closes = [low + 1 for low in lows]
        closes[-1] = 10.0
        result = evaluate_daily_rising_trend_exit(_bars(closes, lows))
        self.assertTrue(result["trend_broken"])
        self.assertTrue(result["should_sell_next_open"])
        self.assertEqual(result["action"], "SELL_NEXT_OPEN")

    def test_intraday_low_is_not_an_input_to_daily_close_signal(self) -> None:
        lows = [10, 9, 8, 7, 8, 9, 10, 9, 8, 9, 10, 11, 12, 11, 10, 11, 12, 13, 14, 13, 12, 13, 14, 15]
        closes = [low + 1 for low in lows]
        result = evaluate_daily_rising_trend_exit(_bars(closes, lows))
        self.assertFalse(result["trend_broken"])
        self.assertEqual(result["action"], "HOLD")

    def test_intraday_price_breaks_projected_daily_support(self) -> None:
        lows = [10, 9, 8, 7, 8, 9, 10, 9, 8, 9, 10, 11, 12, 11, 10, 11, 12, 13, 14, 13, 12, 13, 14, 15]
        closes = [low + 1 for low in lows]
        result = evaluate_intraday_rising_trend_line(_bars(closes, lows), 10.0)
        self.assertTrue(result["intraday_trend_broken"])
        self.assertEqual(result["intraday_action"], "SELL_REVIEW")


if __name__ == "__main__":
    unittest.main()
