"""The two-leg spread, and the misreport it exists to fix.

A real HIC put debit spread put through `Condor` came out at 17,225 of risk
against a true 4,225, and a best case of minus 4,225. Those figures feed
`capital_at_risk`, which every risk-adjusted metric divides by, so the error
would not have stayed in one column.

The existing envelope test could never have caught it: it asserts the payoff
stays *inside* max loss and max profit, which a figure four times too large
satisfies comfortably. Everything here asserts equality against
`payoff_at_expiry`, which is computed from the legs and knows nothing about
these formulas.
"""

from __future__ import annotations

import datetime as dt

import pytest

from engine.strategy.condor import (
    CALL,
    PUT,
    Condor,
    FilledLeg,
    Leg,
    Side,
    StrategyConfig,
    UnitKind,
)
from engine.strategy.vertical import VerticalSpread

WHEN = dt.datetime(2026, 9, 15, 9, 15)
EXPIRY = dt.date(2026, 9, 29)
QTY = 65


def cfg(**kw) -> StrategyConfig:
    return StrategyConfig(**{"lots": 1, "lot_size": QTY, **kw})


def vertical(
    right=PUT, *, long_strike, short_strike, long_price, short_price,
    entry_costs=0.0, exit_costs=0.0, config=None,
) -> VerticalSpread:
    config = config or cfg()
    kind = UnitKind.PUT_DEBIT_SPREAD if right == PUT else UnitKind.CALL_DEBIT_SPREAD
    return VerticalSpread(
        level=23_200.0, entry_time=WHEN, expiry=EXPIRY,
        legs=[
            FilledLeg(leg=Leg(right, Side.BUY, long_strike, config.qty), entry_price=long_price),
            FilledLeg(leg=Leg(right, Side.SELL, short_strike, config.qty), entry_price=short_price),
        ],
        config=config, entry_costs=entry_costs, exit_costs=exit_costs, kind=kind,
    )


def put_debit(**kw) -> VerticalSpread:
    """HIC's spread at level 23,200: buy the 23,000 put, sell the 22,800."""
    return vertical(PUT, long_strike=23_000.0, short_strike=22_800.0,
                    long_price=150.0, short_price=85.0, **kw)


def call_debit(**kw) -> VerticalSpread:
    return vertical(CALL, long_strike=23_800.0, short_strike=24_000.0,
                    long_price=150.0, short_price=85.0, **kw)


def put_credit(**kw) -> VerticalSpread:
    """The H4 comparison variant: the same strikes, sold instead of bought."""
    return vertical(PUT, long_strike=22_800.0, short_strike=23_000.0,
                    long_price=85.0, short_price=150.0, **kw)


def _envelope(unit) -> tuple[float, float]:
    """Best and worst the payoff actually reaches.

    Exact rather than sampled: the curve is piecewise-linear with kinks only at
    traded strikes, so those points plus one either side of the outermost pair
    capture every extreme. A uniform grid rounds the corners off.
    """
    strikes = sorted(fl.leg.strike for fl in unit.legs)
    grid = [strikes[0] - 2_000, *strikes, strikes[-1] + 2_000]
    grid += [s + d for s in strikes for d in (-1.0, 1.0)]
    values = [unit.payoff_at_expiry(s) for s in grid]
    return min(values), max(values)


# ============================== the number that was wrong


def test_a_debit_spread_risks_the_debit_and_nothing_more():
    """The headline. 65 a share paid, 65 shares: 4,225 at risk, not 17,225."""
    v = put_debit()

    assert v.credit == pytest.approx(-4_225.0), "a debit is negative cash"
    assert v.is_debit is True
    assert v.max_loss == pytest.approx(4_225.0)
    assert v.max_profit == pytest.approx(8_775.0)
    assert v.max_profit > 0, "the best case of a bought spread is a profit"


def test_the_old_formula_really_would_have_been_wrong():
    """Kept as evidence, not as documentation. If someone ever simplifies the
    two shapes back into one, this says what that costs."""
    v = put_debit()
    credit_spread_formula = v.width * v.config.qty - v.credit

    assert credit_spread_formula == pytest.approx(17_225.0)
    assert credit_spread_formula / v.max_loss == pytest.approx(4.08, abs=0.01)


@pytest.mark.parametrize(
    "build,kwargs",
    [
        (put_debit, {}),
        (put_debit, {"entry_costs": 120.0}),
        (put_debit, {"entry_costs": 120.0, "exit_costs": 95.0}),
        (call_debit, {}),
        (call_debit, {"exit_costs": 80.0}),
        (put_credit, {}),
        (put_credit, {"entry_costs": 120.0}),
    ],
    ids=["put-debit", "put-debit+entry", "put-debit+both",
         "call-debit", "call-debit+exit", "put-credit", "put-credit+entry"],
)
def test_max_loss_and_max_profit_are_exactly_the_payoff_extremes(build, kwargs):
    v = build(**kwargs)
    worst, best = _envelope(v)

    assert worst == pytest.approx(-v.max_loss, abs=0.01)
    assert best == pytest.approx(v.max_profit, abs=0.01)


def test_one_formula_covers_bought_and_sold_without_asking_which():
    """A debit and the credit spread on the same strikes are mirror images:
    what one risks the other can make."""
    bought, sold = put_debit(), put_credit()

    assert bought.max_loss == pytest.approx(sold.max_profit)
    assert bought.max_profit == pytest.approx(sold.max_loss)


# ============================== one breakeven, not two


def test_a_vertical_breaks_even_at_exactly_one_point():
    v = put_debit()

    assert len(v.breakevens) == 1
    (point,) = v.breakevens
    assert point == pytest.approx(22_935.0)
    assert v.payoff_at_expiry(point) == pytest.approx(0.0, abs=0.01)


@pytest.mark.parametrize(
    "build", [put_debit, call_debit, put_credit], ids=["put-debit", "call-debit", "put-credit"]
)
def test_the_payoff_really_is_zero_at_the_breakeven(build):
    v = build()

    for point in v.breakevens:
        assert v.payoff_at_expiry(point) == pytest.approx(0.0, abs=0.01)
        assert min(v.long_strike, v.short_strike) <= point <= max(v.long_strike, v.short_strike)


def test_a_spread_that_can_never_break_even_reports_no_breakeven():
    """Paying the full width means the best case is zero before costs, so the
    curve touches zero without crossing. A number off the curve would be worse
    than saying there is none."""
    v = vertical(PUT, long_strike=23_000.0, short_strike=22_800.0,
                 long_price=250.0, short_price=50.0, entry_costs=500.0)

    assert v.max_profit < 0
    assert v.breakevens == ()


# ============================== exits


def test_take_profit_works_on_a_debit_rather_than_being_silently_off():
    """The old gate was `credit <= 0: return None`, which does not disable
    exits for a bought spread so much as quietly pretend it has none."""
    v = put_debit(config=cfg(take_profit_pct=0.5))
    # Worth half the debit more than it cost: the long leg up, short unchanged.
    marks = {v.legs[0].leg: 150.0 + 32.5, v.legs[1].leg: 85.0}

    assert "take-profit" in (v.exit_signal(marks) or "")


def test_a_losing_debit_spread_does_not_announce_a_take_profit():
    """The version above passed for the wrong reason, and hid a live bug.

    It marks the spread at a profit and asserts "take-profit", which a rule
    that fires on *everything* satisfies just as well. The thresholds were
    still measured against `credit`, which is negative on a bought spread, so
    `pnl >= tp * credit` compared against a negative number: every debit
    spread closed on its first mark, at whatever it was down, labelled a
    take-profit -- and the trade log showed "captured -0% of credit".
    """
    v = put_debit(config=cfg(take_profit_pct=0.5, stop_loss_mult=2.0))

    for loss, marks in [
        (0.0, {v.legs[0].leg: 150.0, v.legs[1].leg: 85.0}),        # flat
        (-975.0, {v.legs[0].leg: 135.0, v.legs[1].leg: 85.0}),     # down a little
        (-3_345.0, {v.legs[0].leg: 100.0, v.legs[1].leg: 85.6}),   # down a lot
    ]:
        assert v.mtm(marks) <= 0
        assert v.exit_signal(marks) is None, f"fired at {v.mtm(marks):+,.0f}"

    # And it still fires where it should: half the debit, in profit.
    won = {v.legs[0].leg: 150.0 + 32.5, v.legs[1].leg: 85.0}
    assert v.exit_signal(won) == "take-profit: captured 50% of the debit paid"


def test_a_stop_measured_against_the_debit_can_actually_trigger():
    """A fraction of the debit is reachable; a multiple of it is not, because
    the debit is the whole of what a bought spread can lose."""
    v = put_debit(config=cfg(stop_loss_mult=0.5))
    # Down 2,437.50, which is 0.58x the 4,225 debit.
    marks = {v.legs[0].leg: 150.0 - 37.5, v.legs[1].leg: 85.0}

    assert v.exit_signal(marks) == "stop-loss: lost 0.6x the debit paid"


def test_a_credit_spread_still_speaks_of_credit():
    v = put_credit(config=cfg(take_profit_pct=0.5))
    # Bought leg unmoved, sold leg back half the 65-point credit.
    marks = {v.legs[0].leg: 85.0, v.legs[1].leg: 150.0 - 32.5}

    assert v.exit_signal(marks) == "take-profit: captured 50% of credit"


def test_the_reference_an_exit_is_measured_against_is_always_positive():
    assert put_debit().risk_reference == pytest.approx(4_225.0)
    assert put_credit().risk_reference == pytest.approx(4_225.0)


def test_a_partly_marked_spread_reports_no_mark():
    v = put_debit()
    assert v.mtm({v.legs[0].leg: 160.0}) == 0.0


# ============================== construction is checked


@pytest.mark.parametrize(
    "legs,because",
    [
        ([], "no legs"),
        ([(PUT, Side.BUY, 23_000.0)], "one leg"),
        ([(PUT, Side.BUY, 23_000.0), (PUT, Side.BUY, 22_800.0)], "both bought"),
        ([(PUT, Side.BUY, 23_000.0), (CALL, Side.SELL, 23_800.0)], "two different rights"),
        ([(PUT, Side.BUY, 23_000.0), (PUT, Side.SELL, 23_000.0)], "one strike"),
    ],
)
def test_a_structure_that_is_not_a_vertical_is_refused(legs, because):
    """A wrong number is expensive to notice. A refused construction is not."""
    if not legs:
        pytest.skip("a bare shell is allowed, for restore paths")
    config = cfg()
    with pytest.raises(ValueError):
        VerticalSpread(
            level=23_200.0, entry_time=WHEN, expiry=EXPIRY,
            legs=[
                FilledLeg(leg=Leg(r, s, k, config.qty), entry_price=100.0)
                for r, s, k in legs
            ],
            config=config,
        )


def test_a_two_leg_structure_cannot_be_built_as_a_condor():
    """The construction that started all of this."""
    config = cfg()
    with pytest.raises(ValueError, match="four legs"):
        Condor(
            level=23_200.0, entry_time=WHEN, expiry=EXPIRY,
            legs=[
                FilledLeg(leg=Leg(PUT, Side.BUY, 23_000.0, config.qty), entry_price=150.0),
                FilledLeg(leg=Leg(PUT, Side.SELL, 22_800.0, config.qty), entry_price=85.0),
            ],
            config=config,
        )


def test_a_four_leg_structure_cannot_be_built_as_a_vertical():
    from engine.strategy.condor import build_legs

    config = cfg()
    with pytest.raises(ValueError, match="two legs"):
        VerticalSpread(
            level=23_400.0, entry_time=WHEN, expiry=EXPIRY,
            legs=[FilledLeg(leg=leg, entry_price=100.0) for leg in build_legs(23_400.0, config)],
            config=config,
        )


# ============================== it still nets like anything else


def test_a_vertical_nets_against_a_condor_on_a_shared_strike():
    """HIC stacks spreads onto the core condors' wings, so the netting code has
    to see both kinds. It only ever touched legs, expiry and index, so this is
    a check that nothing was lost rather than that anything was added."""
    from engine.strategy.condor import build_legs, net_positions

    config = cfg()
    core = Condor(
        level=23_400.0, entry_time=WHEN, expiry=EXPIRY,
        legs=[FilledLeg(leg=leg, entry_price=100.0) for leg in build_legs(23_400.0, config)],
        config=config, index=0,
    )
    spread = put_debit()          # buys the 23,000 put, which the core also buys
    spread.index = 1

    book = {(p.right, p.strike): p for p in net_positions([core, spread])}
    assert book[(PUT, 23_000.0)].net_qty == 2 * QTY, "the longs should stack, not cancel"
    assert book[(PUT, 22_800.0)].net_qty == -QTY
