"""The condor's arithmetic, pinned to literal numbers.

The unit model is about to be split so a two-leg debit spread can sit beside a
four-leg condor. That split moves most of `Condor` onto a shared base, and the
condor's maths is in live paper use behind a trade-for-trade parity gate.

The existing tests pin *relationships* -- `max_loss == 200 * 75 - 70 * 75` --
which is exactly what a refactor preserves while changing the answer. These pin
the answers themselves, computed from the code as it stood before the split.

`payoff_at_expiry` is the oracle. It sums each leg's intrinsic value against
what was paid for it, so it depends on nothing the split touches, and every
other figure is checked against it rather than against a restatement of its
own formula.
"""

from __future__ import annotations

import datetime as dt

import pytest

from engine.strategy.condor import (
    CALL,
    PUT,
    Condor,
    CondorStatus,
    FilledLeg,
    Side,
    StrategyConfig,
    build_legs,
)

WHEN = dt.datetime(2026, 3, 23, 9, 15)
EXPIRY = dt.date(2026, 3, 26)


def condor(
    level=24_000.0, *, lots=1, lot_size=75, prices=(70.0, 95.0, 130.0, 160.0),
    entry_costs=0.0, exit_costs=0.0, short_offset=200.0, long_offset=400.0,
) -> Condor:
    """A condor whose four legs are priced in `build_legs` order.

    That order is wings first -- long PE, long CE, short PE, short CE -- which
    is load-bearing elsewhere and is why the prices read oddly: the two cheap
    wings come first.
    """
    config = StrategyConfig(
        lots=lots, lot_size=lot_size,
        short_offset=short_offset, long_offset=long_offset,
    )
    legs = build_legs(level, config)
    return Condor(
        level=level, entry_time=WHEN, expiry=EXPIRY,
        legs=[FilledLeg(leg=leg, entry_price=p) for leg, p in zip(legs, prices)],
        config=config, entry_costs=entry_costs, exit_costs=exit_costs,
    )


# ============================== the numbers themselves


def test_a_plain_condor_reports_the_figures_it_always_has():
    c = condor()

    # SELL 130 + SELL 160 - BUY 70 - BUY 95 = 125/share, x75 shares.
    assert c.credit == pytest.approx(9_375.0)
    assert c.net_credit == pytest.approx(9_375.0)
    assert c.wing_width == pytest.approx(200.0)
    assert c.max_profit == pytest.approx(9_375.0)
    assert c.max_loss == pytest.approx(5_625.0)
    assert c.breakevens == pytest.approx((23_675.0, 24_325.0))


def test_costs_move_both_ends_the_way_they_always_have():
    c = condor(entry_costs=300.0, exit_costs=200.0)

    assert c.net_credit == pytest.approx(9_075.0)
    assert c.max_profit == pytest.approx(8_875.0)
    assert c.max_loss == pytest.approx(6_125.0)


def test_a_bigger_lot_scales_every_figure():
    c = condor(lots=4)

    assert c.credit == pytest.approx(37_500.0)
    assert c.max_loss == pytest.approx(22_500.0)
    assert c.breakevens == pytest.approx((23_675.0, 24_325.0)), "breakevens are per share"


def test_a_wider_wing_widens_the_loss_not_the_credit():
    c = condor(short_offset=200.0, long_offset=600.0)

    assert c.credit == pytest.approx(9_375.0)
    assert c.wing_width == pytest.approx(400.0)
    assert c.max_loss == pytest.approx(20_625.0)


@pytest.mark.parametrize(
    "spot,expected",
    [
        (24_000.0, 9_375.0),      # dead centre: every leg expires worthless
        (23_800.0, 9_375.0),      # at the short put, still whole
        (24_200.0, 9_375.0),      # at the short call
        (23_675.0, 0.0),          # the lower breakeven
        (24_325.0, 0.0),          # the upper
        (23_600.0, -5_625.0),     # at the long put: full loss
        (23_000.0, -5_625.0),     # beyond it: no worse
        (24_400.0, -5_625.0),     # the long call
        (25_000.0, -5_625.0),
    ],
)
def test_the_payoff_curve_is_where_it_has_always_been(spot, expected):
    assert condor().payoff_at_expiry(spot) == pytest.approx(expected)


# ============================== checked against the oracle


def _envelope(c: Condor) -> tuple[float, float]:
    """Best and worst the payoff actually reaches.

    Piecewise-linear with kinks only at traded strikes, so evaluating at every
    strike plus a point either side of the outermost pair is exact. A uniform
    grid would round the corners off and understate the extremes.
    """
    strikes = sorted(fl.leg.strike for fl in c.legs)
    grid = [strikes[0] - 1_000, *strikes, strikes[-1] + 1_000]
    grid += [s + d for s in strikes for d in (-1.0, 1.0)]
    values = [c.payoff_at_expiry(s) for s in grid]
    return min(values), max(values)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"lots": 3},
        {"entry_costs": 250.0},
        {"entry_costs": 250.0, "exit_costs": 180.0},
        {"long_offset": 600.0},
        {"prices": (40.0, 45.0, 210.0, 190.0)},
    ],
    ids=["plain", "three-lots", "entry-cost", "both-costs", "wide-wing", "rich-credit"],
)
def test_max_loss_and_max_profit_are_exactly_the_payoff_extremes(kwargs):
    """Equality, not containment.

    The existing envelope test asserts the payoff stays *inside* max loss and
    max profit. A figure four times too large passes that trivially -- which is
    how the debit-spread misreport survived. Equality is the form that catches
    it, and it is the form the new unit types have to satisfy too.
    """
    c = condor(**kwargs)
    worst, best = _envelope(c)

    assert worst == pytest.approx(-c.max_loss, abs=0.01)
    assert best == pytest.approx(c.max_profit, abs=0.01)


@pytest.mark.parametrize(
    "kwargs", [{}, {"lots": 3}, {"entry_costs": 250.0}, {"long_offset": 600.0}],
    ids=["plain", "three-lots", "entry-cost", "wide-wing"],
)
def test_the_payoff_really_is_zero_at_both_breakevens(kwargs):
    c = condor(**kwargs)

    for point in c.breakevens:
        assert c.payoff_at_expiry(point) == pytest.approx(0.0, abs=0.01)


# ============================== structure


def test_the_legs_are_built_where_they_have_always_been_built():
    legs = build_legs(24_000.0, StrategyConfig(lots=1, lot_size=75))

    assert [(leg.right, leg.side.value, leg.strike) for leg in legs] == [
        (PUT, "BUY", 23_600.0),
        (CALL, "BUY", 24_400.0),
        (PUT, "SELL", 23_800.0),
        (CALL, "SELL", 24_200.0),
    ]


def test_every_leg_carries_the_same_quantity():
    assert {leg.qty for leg in build_legs(24_000.0, StrategyConfig(lots=2, lot_size=75))} == {150}


def test_a_closed_condor_reports_what_it_actually_made():
    c = condor()
    for fl, price in zip(c.legs, (10.0, 12.0, 40.0, 30.0)):
        fl.exit_price = price
    c.close(WHEN, "take-profit", CondorStatus.CLOSED_TARGET, costs=150.0)

    # Shorts bought back for 70, wings sold for 22: 125 - 48 = 77/share.
    assert c.realised_pnl() == pytest.approx(77.0 * 75 - 150.0)


def test_an_open_condor_has_realised_nothing():
    assert condor().realised_pnl() == 0.0


def test_a_partly_marked_condor_refuses_to_report_a_mark():
    """Three of four legs priced is not three quarters of a P&L, it is no
    P&L -- the missing leg could be the one that moved."""
    c = condor()
    partial = {fl.leg: 50.0 for fl in c.legs[:3]}

    assert c.mtm(partial) == 0.0


def test_a_mark_with_every_leg_present_is_the_sum_of_the_legs():
    c = condor()
    marks = {fl.leg: fl.entry_price for fl in c.legs}

    assert c.mtm(marks) == pytest.approx(0.0), "unchanged prices are a flat position"


# ============================== what the split must not break


def test_the_four_leg_shape_is_what_the_condor_maths_assumes():
    """`wing_width` takes the widest span within one right, which is only a
    safe reading because a condor's two wings are mutually exclusive: at
    expiry at most one of them can be in the money. Recorded here because the
    new unit types cannot borrow that assumption."""
    c = condor()
    puts = sorted(fl.leg.strike for fl in c.legs if fl.leg.right == PUT)
    calls = sorted(fl.leg.strike for fl in c.legs if fl.leg.right == CALL)

    assert len(puts) == 2 and len(calls) == 2
    assert puts[1] - puts[0] == pytest.approx(calls[1] - calls[0])
    assert c.wing_width == pytest.approx(puts[1] - puts[0])


def test_exit_signals_are_measured_against_the_credit_taken_in():
    c = condor()
    c.config = StrategyConfig(lots=1, lot_size=75, take_profit_pct=0.5)
    # Half the credit captured: every leg bought back at half what it sold for.
    marks = {fl.leg: fl.entry_price for fl in c.legs}
    for fl in c.legs:
        if fl.leg.side is Side.SELL:
            marks[fl.leg] = fl.entry_price / 2

    assert "take-profit" in (c.exit_signal(marks) or "")
