"""Tests for deriving the expiry a historical bar should have traded.

The bug these exist to prevent was visible in the dashboard and invisible in
the code: every condor in a March backtest carried an expiry of 8 September,
six months out, because the scrip master only lists contracts that still exist.
Weekly condors were priced as half-year options, which is why credits came out
at roughly 80% of the wing width -- a number no iron condor can produce.
"""

from __future__ import annotations

import datetime as dt

import pytest

from engine.backtest.runner import weekly_expiry_resolver
from engine.data.expiry_calendar import (
    MAX_WEEKLY_DTE,
    expiry_calendar,
    infer_expiry_weekday,
    monthly_expiries,
    weekly_expiries,
)
from engine.data.market_calendar import MarketCalendar

TUESDAY = 1

# Expiries the exchange actually lists, read from the live scrip master on
# 8 Sep 2026. Two of them are rolled back off a holiday, which is what makes
# them useful as a fixture rather than just a list of Tuesdays.
LISTED = [
    dt.date(2026, 9, 8), dt.date(2026, 9, 15), dt.date(2026, 9, 22),
    dt.date(2026, 9, 29), dt.date(2026, 10, 6), dt.date(2026, 10, 27),
    dt.date(2026, 11, 23),                      # Monday — rolled back
    dt.date(2026, 12, 29), dt.date(2027, 3, 30), dt.date(2027, 6, 29),
    dt.date(2029, 12, 24),                      # Monday — rolled back off Christmas
]


# ============================================================ the rule itself


def test_the_expiry_weekday_is_read_from_listed_contracts_not_remembered():
    """NSE has moved NIFTY's expiry weekday before; the contracts say what it is."""
    assert infer_expiry_weekday(LISTED) == TUESDAY


def test_holiday_rolled_expiries_do_not_confuse_the_inference():
    """Two Mondays among the Tuesdays must not shift the answer."""
    assert infer_expiry_weekday(LISTED[:8]) == TUESDAY


def test_no_listed_contracts_falls_back_rather_than_crashing():
    assert infer_expiry_weekday([]) == TUESDAY


# ============================================================== derivation


def test_weekly_expiries_land_on_the_expiry_weekday():
    got = weekly_expiries(dt.date(2026, 3, 1), dt.date(2026, 3, 31), weekday=TUESDAY)
    assert got == [
        dt.date(2026, 3, 3), dt.date(2026, 3, 10), dt.date(2026, 3, 17),
        dt.date(2026, 3, 24), dt.date(2026, 3, 31),
    ]


def test_an_expiry_on_a_holiday_rolls_back_to_the_previous_trading_day():
    """Validated against a contract the exchange really lists.

    25 Dec 2029 is a Tuesday and Christmas; NSE lists 24 Dec 2029. Deriving
    that date from the rule alone is the check that the rule is right.
    """
    got = weekly_expiries(
        dt.date(2029, 12, 20), dt.date(2029, 12, 26),
        weekday=TUESDAY, calendar=MarketCalendar.load(),
    )
    assert dt.date(2029, 12, 24) in got
    assert dt.date(2029, 12, 25) not in got


def test_a_holiday_on_republic_day_also_rolls_back():
    # 26 Jan 2027 is a Tuesday and Republic Day.
    got = weekly_expiries(
        dt.date(2027, 1, 24), dt.date(2027, 1, 30),
        weekday=TUESDAY, calendar=MarketCalendar.load(),
    )
    assert dt.date(2027, 1, 25) in got          # rolled back to the Monday
    assert dt.date(2027, 1, 26) not in got


def test_monthly_expiries_are_the_last_weekly_of_each_month():
    got = monthly_expiries(dt.date(2026, 3, 1), dt.date(2026, 5, 31), weekday=TUESDAY)
    assert got == [dt.date(2026, 3, 31), dt.date(2026, 4, 28), dt.date(2026, 5, 26)]


# ========================================================= the full calendar


def test_a_historical_range_gets_expiries_from_its_own_era():
    """The actual bug: a March 2026 bar resolving to September 2026."""
    expiries, derived = expiry_calendar(
        dt.date(2026, 3, 1), dt.date(2026, 4, 30), LISTED
    )
    assert all(dt.date(2026, 3, 1) <= e <= dt.date(2026, 4, 30) for e in expiries)
    assert derived == set(expiries)             # none of these are still listed
    assert dt.date(2026, 9, 8) not in expiries


def test_listed_contracts_win_where_they_exist():
    expiries, derived = expiry_calendar(
        dt.date(2026, 9, 1), dt.date(2026, 9, 30), LISTED
    )
    for real in (dt.date(2026, 9, 8), dt.date(2026, 9, 15), dt.date(2026, 9, 22)):
        assert real in expiries
        assert real not in derived


def test_a_derived_date_next_to_a_listed_one_is_not_duplicated():
    """23 Nov 2026 is listed; the rule alone would say the 24th.

    An incomplete holiday file should not produce two contracts a day apart
    where the exchange lists one.
    """
    expiries, _ = expiry_calendar(
        dt.date(2026, 11, 1), dt.date(2026, 11, 30), LISTED
    )
    november = [e for e in expiries if e.month == 11]
    assert dt.date(2026, 11, 23) in november
    assert dt.date(2026, 11, 24) not in november


def test_an_empty_range_is_an_error_rather_than_an_empty_calendar():
    with pytest.raises(ValueError):
        expiry_calendar(dt.date(2026, 3, 5), dt.date(2026, 3, 6), [])


# ================================================================== the guard


def test_a_six_month_expiry_is_refused_instead_of_silently_used():
    """The exact failure, now loud.

    Given only today's listed contracts, a March bar has nothing near it. That
    used to return September and quietly price a weekly as a half-year option.
    """
    resolve = weekly_expiry_resolver(LISTED)
    with pytest.raises(ValueError) as exc:
        resolve(dt.date(2026, 3, 12))
    message = str(exc.value)
    assert "180 days out" in message
    assert "derive them" in message


def test_a_sensible_expiry_resolves_normally():
    resolve = weekly_expiry_resolver(LISTED)
    assert resolve(dt.date(2026, 9, 9)) == dt.date(2026, 9, 15)


def test_the_guard_can_be_lifted_for_a_deliberately_long_dated_campaign():
    resolve = weekly_expiry_resolver(LISTED, max_dte=None)
    assert resolve(dt.date(2026, 3, 12)) == dt.date(2026, 9, 8)


def test_a_derived_calendar_keeps_every_condor_inside_the_dte_limit():
    """The end-to-end property: with derivation, the guard never has to fire."""
    start, end = dt.date(2026, 3, 1), dt.date(2026, 4, 30)
    expiries, _ = expiry_calendar(start, end + dt.timedelta(days=MAX_WEEKLY_DTE), LISTED)
    resolve = weekly_expiry_resolver(expiries)
    day = start
    while day <= end:
        assert (resolve(day) - day).days <= MAX_WEEKLY_DTE
        day += dt.timedelta(days=1)


def test_no_expiries_at_all_is_refused_at_construction():
    with pytest.raises(ValueError):
        weekly_expiry_resolver([])


# ============================================= monthly cadence, end to end


def test_a_monthly_campaign_gets_month_end_contracts_not_weeklies():
    """The gap the user caught: `expiry_cadence` was read by the job runner
    but never sent by anything, so every run silently used weeklies."""
    start, end = dt.date(2026, 3, 1), dt.date(2026, 5, 31)
    weekly, _ = expiry_calendar(start, end, [], cadence="weekly")
    monthly, _ = expiry_calendar(start, end, [], cadence="monthly")

    assert len(weekly) > len(monthly) * 3, "monthly should be far sparser"
    assert monthly == [dt.date(2026, 3, 31), dt.date(2026, 4, 28), dt.date(2026, 5, 26)]
    for expiry in monthly:
        later_same_month = [
            w for w in weekly if (w.year, w.month) == (expiry.year, expiry.month) and w > expiry
        ]
        assert not later_same_month, f"{expiry} is not the last expiry of its month"


def test_a_monthly_ladder_holds_long_enough_for_offsetting_to_accumulate():
    """The reason the cadence matters. Offsetting needs several condors alive
    in ONE expiry, and a weekly settles before the ladder gets that deep."""
    start, end = dt.date(2026, 3, 1), dt.date(2026, 4, 30)
    weekly, _ = expiry_calendar(start, end, [], cadence="weekly")
    monthly, _ = expiry_calendar(start, end, [], cadence="monthly")

    def mean_life(expiries):
        gaps = [(b - a).days for a, b in zip(expiries, expiries[1:])]
        return sum(gaps) / len(gaps)

    assert mean_life(weekly) == pytest.approx(7.0, abs=1.0)
    assert mean_life(monthly) > 25


def test_the_live_resolver_never_substitutes_a_weekly_for_a_monthly():
    from engine.data.expiry_calendar import nearest_listed_expiry

    listed = [dt.date(2026, 9, 8), dt.date(2026, 9, 15), dt.date(2026, 9, 22),
              dt.date(2026, 9, 29), dt.date(2026, 10, 6), dt.date(2026, 10, 27)]
    on = dt.date(2026, 9, 9)
    assert nearest_listed_expiry(listed, on, cadence="weekly") == dt.date(2026, 9, 15)
    assert nearest_listed_expiry(listed, on, cadence="monthly") == dt.date(2026, 9, 29)


def test_a_monthly_run_rolls_to_the_next_month_once_this_one_has_gone():
    from engine.data.expiry_calendar import nearest_listed_expiry

    listed = [dt.date(2026, 9, 29), dt.date(2026, 10, 6), dt.date(2026, 10, 27)]
    assert nearest_listed_expiry(listed, dt.date(2026, 9, 30), cadence="monthly") == dt.date(2026, 10, 27)


def test_todays_expiry_is_never_opened():
    """0 DTE settles in hours: the wings are worthless and it is a condor in
    name only."""
    from engine.data.expiry_calendar import nearest_listed_expiry

    listed = [dt.date(2026, 9, 8), dt.date(2026, 9, 15)]
    assert nearest_listed_expiry(listed, dt.date(2026, 9, 8)) == dt.date(2026, 9, 15)
