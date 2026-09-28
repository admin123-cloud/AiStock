from datetime import date

import pandas as pd

from services import margin_sentiment


def test_load_margin_sentiment_returns_confirmed_daily_series(monkeypatch):
    frame = pd.DataFrame(
        [
            {
                "trade_date": date(2026, 8, 6),
                "financing_balance": 2_612_000_000_000,
                "securities_lending_balance": 25_500_000_000,
                "margin_balance": 2_637_500_000_000,
                "financing_buy": 236_000_000_000,
                "financing_net_buy": 12_000_000_000,
                "source": "test",
            },
            {
                "trade_date": date(2026, 8, 5),
                "financing_balance": 2_600_000_000_000,
                "securities_lending_balance": 25_000_000_000,
                "margin_balance": 2_625_000_000_000,
                "financing_buy": 240_000_000_000,
                "financing_net_buy": 8_000_000_000,
                "source": "test",
            },
        ]
    )
    monkeypatch.setattr(margin_sentiment, "clickhouse_available", lambda: True)
    monkeypatch.setattr(margin_sentiment, "clickhouse_query_df", lambda *args, **kwargs: frame)

    result = margin_sentiment.load_margin_sentiment(days=30)

    assert result["available"] is True
    assert result["latest"]["date"] == "2026-08-06"
    assert result["financing_net_buy"] == [8_000_000_000.0, 12_000_000_000.0]
    assert result["securities_lending_balance_change"] == [0.0, 500_000_000.0]
