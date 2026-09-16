"""The Hybrid Iron Condor's entry rule, against the strategy document.

The walk-throughs in the document are the specification, so they are here as
exact expected outputs rather than as prose. Where this departs from them it
says so, and why.
"""

from __future__ import annotations

import datetime as dt

import pytest

from engine.config import IST
from engine.strategy.condor import (
    CALL,
    PUT,
    Condor,
    FilledLeg,
    UnitKind,
    net_positions,
)
from engine.strategy.hic import (
    CALL_SPREAD,
    CORE,
    PUT_SPREAD,
    HicConfig,
    build_hic_legs,
    choose_anchor,
    steps_from_anchor,
    structure_kind,
    unit_kind_at,
)
from engine.strategy.ladder import Ladder
from engine.strategy.vertical import VerticalSpread

T0 = dt.datetime(2026, 9, 15, 9, 15, tzinfo=IST)
EXPIRY = dt.date(2026, 9, 29)
ANCHOR = 23_400.0


def cfg(**kw) -> HicConfig:
    return HicConfig(**{"lots": 1, "lot_size": 65, "direction": "both", **kw})


def legs_of(level: float, k: int, config: HicConfig) -> list[tuple[str, str, float]]:
    return [(leg.side.value, leg.right, leg.strike) for leg in build_hic_legs(level, k, config)]


# ============================== section 3.1, the table of units


def test_the_anchor_and_both_first_steps_open_full_condors():
    config = cfg()

    for k, level in ((0, 23_400.0), (-1, 23_300.0), (1, 23_500.0)):
        assert unit_kind_at(k, config) == CORE
        assert len(build_hic_legs(level, k, config)) == 4
        assert structure_kind(k, config) is UnitKind.CONDOR


def test_the_anchor_condor_is_the_one_the_document_specifies():
    assert legs_of(23_400.0, 0, cfg()) == [
        ("BUY", PUT, 23_000.0),
        ("BUY", CALL, 23_800.0),
        ("SELL", PUT, 23_200.0),
        ("SELL", CALL, 23_600.0),
    ]


def test_past_the_band_a_fall_buys_a_put_spread():
    """Section 3.1: at 23,200, BUY 23,000 PE and SELL 22,800 PE."""
    assert unit_kind_at(-2, cfg()) == PUT_SPREAD
    assert legs_of(23_200.0, -2, cfg()) == [
        ("BUY", PUT, 23_000.0),
        ("SELL", PUT, 22_800.0),
    ]


def test_past_the_band_a_rally_buys_a_call_spread():
    """Section 3.1: at 23,600, BUY 23,800 CE and SELL 24,000 CE."""
    assert unit_kind_at(2, cfg()) == CALL_SPREAD
    assert legs_of(23_600.0, 2, cfg()) == [
        ("BUY", CALL, 23_800.0),
        ("SELL", CALL, 24_000.0),
    ]


def test_the_bought_leg_always_comes_first():
    config = cfg()
    for k, level in ((0, 23_400.0), (-2, 23_200.0), (2, 23_600.0), (-3, 23_100.0)):
        sides = [side for side, _, _ in legs_of(level, k, config)]
        assert sides[0] == "BUY", (k, sides)


def test_a_band_of_zero_makes_only_the_anchor_a_condor():
    """Section 3.1's `full_band_steps: 0`."""
    config = cfg(full_band_steps=0)

    assert unit_kind_at(0, config) == CORE
    assert unit_kind_at(-1, config) == PUT_SPREAD
    assert unit_kind_at(1, config) == CALL_SPREAD


# ============================== section 3.4, the shifted variant


def test_the_shifted_spread_is_bought_at_the_level_itself():
    """Section 3.4: at 23,200 with shift 200, BUY 23,200 PE and SELL 23,000."""
    assert legs_of(23_200.0, -2, cfg(debit_shift=200.0)) == [
        ("BUY", PUT, 23_200.0),
        ("SELL", PUT, 23_000.0),
    ]


def test_the_shift_mirrors_on_the_call_side():
    assert legs_of(23_600.0, 2, cfg(debit_shift=200.0)) == [
        ("BUY", CALL, 23_600.0),
        ("SELL", CALL, 23_800.0),
    ]


def test_the_comparison_variant_sells_the_condors_own_side():
    """`half_mode: sell`, the opposite reading, for run H4."""
    assert legs_of(23_200.0, -2, cfg(half_mode="sell")) == [
        ("BUY", PUT, 22_800.0),
        ("SELL", PUT, 23_000.0),
    ]


def test_a_shift_on_the_sold_variant_is_refused_rather_than_ignored():
    """The document's own appendix drops it silently on that branch. Silently
    is how H3 and H4 end up conflated in a results table."""
    with pytest.raises(ValueError, match="bought spreads only"):
        cfg(half_mode="sell", debit_shift=200.0)


def test_a_spread_that_collapses_to_one_strike_is_refused():
    """A grid coarser than the gap between the two legs leaves a structure
    that can neither win nor lose."""
    with pytest.raises(ValueError, match="collapses"):
        build_hic_legs(23_200.0, -2, cfg(strike_step=500.0))


# ============================== anchoring


def test_the_anchor_is_the_nearest_step_not_the_one_below():
    """A correction to section 7.5, which needs `floor` to produce the levels
    it lists. Sections 3.5 and 9 say nearest, and nearest is right: under floor
    the spot sits up to 99 points above the anchor, so at 23,499 the first rung
    up is one point away and the first rung down is 199. A structure symmetric
    by construction cannot be anchored asymmetrically."""
    config = cfg()

    assert choose_anchor(23_450.0, config) == 23_500.0
    assert choose_anchor(23_449.0, config) == 23_400.0
    assert choose_anchor(23_400.0, config) == 23_400.0


def test_k_is_an_exact_integer_however_long_the_campaign():
    """Counted rather than divided, for the reason the ladder counts in units:
    a campaign that drifts loses a rung without saying so."""
    for k in range(-40, 41):
        level = ANCHOR + k * 100
        assert steps_from_anchor(level, ANCHOR, 100.0) == k


# ============================== section 3.2, the walk-through


def _campaign(spots: list[float], config: HicConfig, anchor: float = ANCHOR):
    """Run the ladder and build what each level it fires would open.

    The trigger is the ladder's own -- two-way, gap-filling, fire-once -- so
    HIC cannot silently diverge from the entry rule the backtest proved.
    """
    ladder = Ladder(config=config, anchor_mode="explicit", explicit_anchor=anchor)
    units: list = []
    for i, spot in enumerate(spots):
        for trigger in ladder.on_price(spot, T0 + dt.timedelta(minutes=i)):
            k = steps_from_anchor(trigger.level, anchor, config.step)
            kind = structure_kind(k, config)
            legs = build_hic_legs(trigger.level, k, config)
            unit_type = Condor if kind is UnitKind.CONDOR else VerticalSpread
            units.append(
                unit_type(
                    level=trigger.level, entry_time=trigger.time, expiry=EXPIRY,
                    legs=[FilledLeg(leg=leg, entry_price=100.0) for leg in legs],
                    config=config, index=len(units), kind=kind, k=k,
                )
            )
    return units


def test_the_fall_from_23400_to_22800_opens_what_the_document_says():
    """Section 3.2, step by step: two core condors then five put spreads."""
    units = _campaign(
        [23_400, 23_300, 23_200, 23_100, 23_000, 22_900, 22_800], cfg()
    )

    assert [u.level for u in units] == [
        23_400, 23_300, 23_200, 23_100, 23_000, 22_900, 22_800
    ]
    assert [u.k for u in units] == [0, -1, -2, -3, -4, -5, -6]
    assert [u.kind for u in units] == [UnitKind.CONDOR, UnitKind.CONDOR] + [
        UnitKind.PUT_DEBIT_SPREAD
    ] * 5


def test_that_fall_leaves_the_six_strike_put_book_the_document_describes():
    """Section 3.2's headline claim, and the reason the strategy is worth
    running: however far a one-way move goes, the broker's put book stops
    growing. The two stacked longs are what caps it."""
    units = _campaign(
        [23_400, 23_300, 23_200, 23_100, 23_000, 22_900, 22_800], cfg()
    )
    qty = cfg().qty

    book = {(p.right, p.strike): p.net_qty for p in net_positions(units)}
    live_puts = {
        strike: net for (right, strike), net in book.items() if right == PUT and net
    }

    assert live_puts == {
        23_200.0: -qty,       # the core condors' shorts
        23_100.0: -qty,
        23_000.0: 2 * qty,    # stacked: the anchor's wing plus a spread's long
        22_900.0: 2 * qty,
        22_500.0: -qty,       # 300 and 400 below the lowest spread level
        22_400.0: -qty,
    }


def test_the_put_book_stops_growing_however_far_the_move_runs():
    config = cfg(max_put_spreads=20, max_condors=40, max_down=25)
    books = []
    for depth in (6, 10, 14, 18):
        spots = [ANCHOR - i * 100 for i in range(depth + 1)]
        units = _campaign(spots, config)
        puts = [p for p in net_positions(units) if p.right == PUT and not p.is_flat]
        books.append(len(puts))

    assert books == [6, 6, 6, 6], books


def test_the_whole_book_is_ten_strikes_with_the_calls_counted():
    """Six puts, plus the core condors' four call legs."""
    units = _campaign(
        [23_400, 23_300, 23_200, 23_100, 23_000, 22_900, 22_800], cfg()
    )

    assert len([p for p in net_positions(units) if not p.is_flat]) == 10


# ============================== section 7.5, corrected


def test_the_gap_opens_a_core_condor_then_spreads_in_order():
    """Section 7.5, shifted one step by the anchor correction above.

    The document expects 23,300 to be the core and 23,200/23,100 the spreads,
    which needs a floor anchor at 23,400. Anchored to the nearest step the
    first spot gives 23,500, so the core lands at 23,400 and the spreads below
    it. The claim being tested is unchanged: a gap fires every level it
    skipped, each with the type its distance from the anchor calls for, and in
    order.
    """
    config = cfg()
    anchor = choose_anchor(23_450.0, config)
    assert anchor == 23_500.0

    units = _campaign([23_450, 23_050], config, anchor=anchor)

    assert [(u.level, u.kind) for u in units] == [
        (23_500.0, UnitKind.CONDOR),            # the anchor itself
        (23_400.0, UnitKind.CONDOR),            # k = -1, still inside the band
        (23_300.0, UnitKind.PUT_DEBIT_SPREAD),  # k = -2, past it
        (23_200.0, UnitKind.PUT_DEBIT_SPREAD),
        (23_100.0, UnitKind.PUT_DEBIT_SPREAD),
    ]


def test_a_level_never_fires_twice_even_when_the_market_returns_to_it():
    config = cfg()
    units = _campaign([23_400, 23_200, 23_400, 23_200, 23_100], config)

    assert len({u.level for u in units}) == len(units)


# ============================== risk, per kind


def test_each_spread_risks_only_what_it_paid():
    """The whole reason the unit model was split. Every spread in a campaign
    reports the debit it paid, not a wing's width."""
    units = _campaign(
        [23_400, 23_300, 23_200, 23_100, 23_000], cfg()
    )
    spreads = [u for u in units if u.kind is not UnitKind.CONDOR]
    assert spreads

    for spread in spreads:
        # Priced at 100 a leg in this harness, so every spread is flat: bought
        # and sold at the same price, nothing paid and nothing at stake.
        assert spread.max_loss == pytest.approx(0.0, abs=0.01)
        assert spread.max_profit == pytest.approx(200.0 * spread.config.qty, abs=0.01)


def _fell_to_22800():
    """Section 3.2's campaign: two core condors and five put spreads."""
    return _campaign([23_400, 23_300, 23_200, 23_100, 23_000, 22_900, 22_800], cfg())


def _book_pnl(units, spot: float) -> float:
    return sum(u.payoff_at_expiry(spot) for u in units)


def test_carrying_on_down_pays_more_than_the_core_condors_lose():
    """What HIC is for. Past the last spread the book is well in profit, where
    a ladder of condors would be at its full loss."""
    units = _fell_to_22800()

    assert _book_pnl(units, 22_400.0) > 0
    assert _book_pnl(units, 22_000.0) == pytest.approx(_book_pnl(units, 22_400.0)), (
        "past the lowest spread the payoff is flat"
    )


def test_the_worst_case_is_the_reversal_not_the_move():
    """Worth stating plainly, because the document does not.

    Section 3.3 calls the bad month "a swing through the anchor", which is
    directionally right and understates it. After a one-way fall every spread
    bought is a put, so a sharp reversal past the core band's call wing is
    met by nothing at all: the loss is the core condors' combined worst case,
    with no absorption. It is also *larger* than anything the fall itself can
    cost, which is the opposite of where attention naturally goes while a
    position is falling.
    """
    units = _fell_to_22800()
    cores = [u for u in units if u.kind is UnitKind.CONDOR]

    worst_falling = min(_book_pnl(units, s) for s in (23_000.0, 22_900.0, 22_800.0))
    on_reversal = _book_pnl(units, 23_900.0)

    assert on_reversal < worst_falling
    assert on_reversal == pytest.approx(-sum(c.max_loss for c in cores))


def test_the_netted_worst_case_never_exceeds_the_sum_of_the_parts():
    """Offsetting can only help. Reported separately from the gross sum
    because they are different questions, and on this book they happen to
    give the same answer -- which is itself the finding above."""
    units = _fell_to_22800()
    gross = sum(u.max_loss for u in units)

    strikes = sorted({fl.leg.strike for u in units for fl in u.legs})
    grid = [strikes[0] - 500, *strikes, strikes[-1] + 500]
    netted = -min(_book_pnl(units, s) for s in grid)

    assert netted <= gross + 0.01


# ============================== caps


def test_the_spread_caps_bound_each_side_separately():
    config = cfg(max_put_spreads=3, max_call_spreads=2)

    assert config.max_down_levels == 4      # the band's one rung, plus three
    assert config.max_up_levels == 3
    assert config.core_units == 3


# ============================== the caps have to agree


def test_hic_defaults_ask_for_more_rungs_than_the_engine_allows():
    """The two caps are set independently and the ladder obeys the tighter one
    without saying which, so a run silently loses the rungs furthest from the
    anchor -- the ones a large move depends on."""
    from dataclasses import replace

    from engine.api import _rungs_requested

    config = HicConfig(
        lots=1, lot_size=65, direction="both", max_condors=20,
        full_band_steps=1, max_put_spreads=10, max_call_spreads=10,
    )
    config = replace(config, max_down=config.max_down_levels, max_up=config.max_up_levels)

    assert _rungs_requested(config) == 23
    assert config.max_condors == 20, "so three rungs are dropped, and it is now said"


def test_an_unbounded_side_has_nothing_to_disagree_about():
    from engine.api import _rungs_requested
    from engine.strategy.condor import StrategyConfig

    assert _rungs_requested(StrategyConfig(lots=1, lot_size=65)) is None


def test_a_one_sided_ladder_counts_only_the_side_it_trades():
    from engine.api import _rungs_requested
    from engine.strategy.condor import StrategyConfig

    down = StrategyConfig(lots=1, lot_size=65, direction="down", max_down=5, max_up=99)
    assert _rungs_requested(down) == 6


def test_hic_left_to_itself_is_symmetric():
    """HIC is symmetric by construction, so its two sides must match.

    A live run came out with max_down 12 and max_up 10. The Direction control
    is hidden for HIC, so the form's `direction` state stayed at the ladder
    default "down" -- which hid the Max up box while the form went on sending
    its untouched default of 10, cutting the call side two rungs shorter than
    the put side with nothing on screen to show it.
    """
    from engine.api import StartForwardRequest, _strategy_config

    body = StartForwardRequest(
        strategy="hic", step=100.0, lots=1,
        full_band_steps=2, max_put_spreads=10, max_call_spreads=10,
    )
    config = _strategy_config(body, lot_size=65, strike_step=50.0)

    assert config.max_down == config.max_up == 12
    assert config.direction == "both"


def test_an_explicit_cap_still_narrows_hic_but_never_widens_it():
    """A cap is a limit, not an instruction: asking for more than the band and
    spreads allow cannot conjure rungs that the structure does not have."""
    from engine.api import StartForwardRequest, _strategy_config

    tighter = _strategy_config(
        StartForwardRequest(strategy="hic", step=100.0, lots=1, full_band_steps=2,
                            max_put_spreads=10, max_call_spreads=10, max_down=4, max_up=4),
        lot_size=65, strike_step=50.0,
    )
    assert tighter.max_down == tighter.max_up == 4

    wider = _strategy_config(
        StartForwardRequest(strategy="hic", step=100.0, lots=1, full_band_steps=2,
                            max_put_spreads=10, max_call_spreads=10, max_down=99, max_up=99),
        lot_size=65, strike_step=50.0,
    )
    assert wider.max_down == wider.max_up == 12
