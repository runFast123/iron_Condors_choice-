"""India VIX before Choice's daily series begins (31 Jul 2020): each day's
close is its last hourly reading inside the session, from Choice itself."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from engine.config import IST
from engine.data.market import ChoiceMarketData


def bars(rows):
    return pd.DataFrame({"ts": pd.to_datetime([r[0] for r in rows]).tz_localize("Asia/Kolkata"),
                         "close": [r[1] for r in rows]})


def market(daily, hourly):
    m = object.__new__(ChoiceMarketData)
    asked = []

    def india_vix(start, end, resolution="D", *, strict=True):
        asked.append((resolution, start, end))
        return daily if resolution == "D" else hourly

    m.india_vix = india_vix
    return m, asked


def test_days_before_the_daily_series_come_from_hourly_bars():
    daily = bars([("2020-07-31 00:00", 24.0), ("2020-08-03 00:00", 25.0)])
    hourly = bars([("2020-07-29 14:29:59", 26.0), ("2020-07-29 15:29:59", 26.5),
                   ("2020-07-29 15:59:59", 99.0),           # after the close: not the day's close
                   ("2020-07-30 15:29:58", 27.0)])
    m, asked = market(daily, hourly)
    got = m.vix_by_date(dt.date(2020, 7, 27), dt.date(2020, 8, 3))
    assert got == {dt.date(2020, 7, 29): 26.5, dt.date(2020, 7, 30): 27.0,
                   dt.date(2020, 7, 31): 24.0, dt.date(2020, 8, 3): 25.0}
    # Hourly bars are asked only for the stretch before the daily series.
    assert asked[1] == ("60", dt.date(2020, 7, 27), dt.date(2020, 7, 30))


def test_a_range_the_daily_series_covers_asks_nothing_more():
    daily = bars([("2024-01-02 00:00", 14.0)])
    m, asked = market(daily, bars([]))
    assert m.vix_by_date(dt.date(2024, 1, 2), dt.date(2024, 1, 2)) == {dt.date(2024, 1, 2): 14.0}
    assert [a[0] for a in asked] == ["D"]
