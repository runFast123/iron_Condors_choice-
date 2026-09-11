"""When a trade happened, as against when the engine noticed.

The index is served from candles rather than the live book, so the price that
fires a rung can have printed minutes before the tick that reads it. Stamping
fills with the engine's clock made the trade log disagree with the chart.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from engine.config import IST
from engine.data.market import Quote, _candle_time


def test_a_candle_row_gives_back_the_instant_it_printed():
    row = pd.Series({"ts": dt.datetime(2026, 9, 10, 15, 1, tzinfo=IST), "close": 23386.0})
    assert _candle_time(row) == dt.datetime(2026, 9, 10, 15, 1, tzinfo=IST)


def test_a_naive_candle_timestamp_is_read_as_ist_not_utc():
    """A 5.5 hour error here would put every fill in the wrong session."""
    row = pd.Series({"ts": dt.datetime(2026, 9, 10, 15, 1), "close": 23386.0})
    assert _candle_time(row) == dt.datetime(2026, 9, 10, 15, 1, tzinfo=IST)


@pytest.mark.parametrize(
    "value",
    [None, pd.NaT, "not a date", float("nan")],
    ids=["missing", "nat", "garbage", "nan"],
)
def test_an_unreadable_timestamp_degrades_to_unknown(value):
    """It must not take the price down with it: not knowing when a price
    traded is survivable, losing the price is not."""
    assert _candle_time(pd.Series({"ts": value, "close": 23386.0})) is None


def test_a_row_without_a_timestamp_column_is_unknown():
    assert _candle_time(pd.Series({"close": 23386.0})) is None


def test_a_live_touch_carries_no_separate_market_time():
    """A live quote is current by definition, so the clock is the honest
    answer and a second timestamp would only invite doubt."""
    assert Quote(token=1, ltp=100.0, bid=99.0, ask=101.0).as_of is None


def test_a_candle_quote_carries_when_it_printed():
    printed = dt.datetime(2026, 9, 10, 15, 1, tzinfo=IST)
    quote = Quote(token=1, ltp=23386.0, stale=True, as_of=printed)
    assert quote.stale is True
    assert quote.as_of == printed
