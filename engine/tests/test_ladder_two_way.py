"""The ladder going up, which it has never done before.

The whole risk of this change is that the up side is a mirror of the down
side, and a mirror is the easiest thing in the world to get backwards.
"""

from __future__ import annotations

import datetime as dt

import pytest

from engine.config import IST
from engine.strategy.condor import StrategyConfig, net_positions
from engine.strategy.ladder import Ladder

T0 = dt.datetime(2026, 9, 11, 9, 15, tzinfo=IST)


def t(i: int) -> dt.datetime:
    return T0 + dt.timedelta(minutes=i)


def cfg(**kw) -> StrategyConfig:
    return StrategyConfig(**{"lots": 1, "lot_size": 65, **kw})


def ladder(**kw) -> Ladder:
    """A ladder anchored at 23,400 unless told otherwise."""
    lad = Ladder(config=cfg(**kw), anchor_mode="explicit", explicit_anchor=23_400)
    return lad


def feed(lad: Ladder, spots: list[float]) -> list:
    out = []
    for i, spot in enumerate(spots):
        out += lad.on_price(spot, t(i))
    return out


# ============================== the rounding trap


def test_a_tick_just_above_the_anchor_does_not_fire_the_up_rung():
    """The mistake this guards against: copying the down side's ceil into the
    up side. That fires 23,500 on a one-point tick above 23,400 -- and with
    gap-fill, every rung above it too."""
    lad = ladder(direction="both")
    lad.on_price(23_400, t(0))

    assert feed(lad, [23_400.01, 23_450, 23_499.99]) == []
    assert [x.level for x in lad.on_price(23_500, t(4))] == [23_500]
    assert lad.high_level == 23_500


def test_an_exact_multiple_is_that_level_not_the_next_one():
    """Both sides round so that landing exactly on a rung fires that rung."""
    up = ladder(direction="both")
    up.on_price(23_400, t(0))
    assert [x.level for x in up.on_price(23_500, t(1))] == [23_500]

    down = ladder()
    down.on_price(23_400, t(0))
    assert [x.level for x in down.on_price(23_300, t(1))] == [23_300]


def test_a_dip_that_does_not_clear_a_step_still_does_nothing():
    """The down side's own guard, re-asserted now that it lives in a branch."""
    lad = ladder(direction="both")
    lad.on_price(23_400, t(0))

    assert feed(lad, [23_399.99, 23_350, 23_300.01]) == []


# ============================== direction


def test_down_mode_ignores_a_thousand_point_rally():
    """The default must behave exactly as it always has."""
    lad = ladder()
    lad.on_price(23_400, t(0))

    assert feed(lad, [23_600, 23_900, 24_400]) == []
    assert lad.levels() == [23_400]


def test_up_mode_ignores_every_decline():
    lad = ladder(direction="up")
    lad.on_price(23_400, t(0))

    assert feed(lad, [23_300, 23_000, 22_400]) == []
    assert lad.levels() == [23_400]


def test_both_mode_fires_on_either_side_of_the_anchor():
    lad = ladder(direction="both")
    lad.on_price(23_400, t(0))

    down = [x.level for x in lad.on_price(23_300, t(1))]
    up = [x.level for x in lad.on_price(23_500, t(2))]

    assert down == [23_300]
    assert up == [23_500]
    assert lad.down_count == 1 and lad.up_count == 1


def test_only_one_side_can_fire_on_a_single_observation():
    """A spot cannot be a full step below the low bound and a full step above
    the high bound at once, so the two scans can never both fire."""
    lad = ladder(direction="both")
    lad.on_price(23_400, t(0))

    for i, spot in enumerate([23_250, 23_680, 22_900, 24_100, 23_000], start=1):
        sides = {x.side for x in lad.on_price(spot, t(i))}
        assert len(sides) <= 1, f"both sides fired at {spot}: {sides}"


# ============================== gaps


def test_a_gap_up_fills_every_rung_it_skipped_in_order():
    lad = ladder(direction="both")
    lad.on_price(23_400, t(0))

    fired = lad.on_price(23_850, t(1))

    assert [x.level for x in fired] == [23_500, 23_600, 23_700, 23_800]
    assert [x.reason for x in fired] == ["rally", "gap-fill", "gap-fill", "gap-fill"]
    assert {x.side for x in fired} == {"up"}


def test_without_gap_fill_a_gap_up_opens_only_the_landing_rung():
    lad = ladder(direction="both", fill_gaps=False)
    lad.on_price(23_400, t(0))

    assert [x.level for x in lad.on_price(23_850, t(1))] == [23_800]


# ============================== caps


def test_the_up_side_has_its_own_cap():
    lad = ladder(direction="both", max_up=2)
    lad.on_price(23_400, t(0))

    feed(lad, [23_500, 23_600, 23_700, 23_800])

    assert lad.up_count == 2
    assert lad.next_up_level is None, "a capped side must not advertise a next rung"


def test_a_capped_up_side_does_not_stop_the_down_side():
    lad = ladder(direction="both", max_up=1)
    lad.on_price(23_400, t(0))
    feed(lad, [23_500, 23_600, 23_700])

    fired = lad.on_price(23_200, t(9))

    assert [x.level for x in fired] == [23_300, 23_200]
    assert lad.down_count == 2


def test_the_total_cap_still_binds_across_both_sides():
    lad = ladder(direction="both", max_condors=3)
    lad.on_price(23_400, t(0))

    feed(lad, [23_300, 23_500, 23_200, 23_600])

    assert lad.count == 3
    assert lad.next_trigger_level is None


def test_the_anchor_counts_toward_the_total_and_to_neither_side():
    lad = ladder(direction="both")
    lad.on_price(23_400, t(0))

    assert lad.count == 1
    assert lad.down_count == 0 and lad.up_count == 0


def test_a_rung_a_cap_skipped_never_fires_later():
    """The bound advances past a capped rung on purpose. Without that, raising
    a cap mid-run would retroactively open rungs the market left long ago."""
    lad = ladder(direction="both", max_up=1)
    lad.on_price(23_400, t(0))
    feed(lad, [23_700])                       # fires 23,500 only, then caps

    lad.config = cfg(direction="both", max_up=5)
    assert feed(lad, [23_650, 23_700]) == []


# ============================== the worked example from the plan


def test_the_worked_example_nets_two_strikes_to_zero():
    """Anchor 23,400, down to 23,300, up to 23,500. The long 23,100 PE of the
    23,500 condor cancels the short 23,100 PE of the 23,300 one, and the same
    on the call side at 23,700 -- which is the point of the whole strategy."""
    from engine.strategy.condor import Condor, FilledLeg, build_legs

    lad = ladder(direction="both")
    fired = feed(lad, [23_400, 23_300, 23_500])

    assert [x.level for x in fired] == [23_400, 23_300, 23_500]

    condors = [
        Condor(
            level=x.level, entry_time=x.time, expiry=dt.date(2026, 9, 29),
            legs=[
                FilledLeg(leg=leg, entry_price=100.0)
                for leg in build_legs(x.level, lad.config)
            ],
            config=lad.config, index=i,
        )
        for i, x in enumerate(fired)
    ]
    flat = {(p.right, p.strike) for p in net_positions(condors) if p.is_flat}
    assert ("PE", 23_100.0) in flat, "the put side did not cancel"
    assert ("CE", 23_700.0) in flat, "the call side did not cancel"


# ============================== anchoring


def test_a_two_way_ladder_anchors_to_the_nearest_level_not_the_floor():
    """Floor is lopsided for a two-way ladder: the spot would sit 0-99 points
    above the anchor, so the first up rung is far closer than the first down."""
    assert Ladder(config=cfg(direction="both"))._choose_anchor(23_460) == 23_500
    assert Ladder(config=cfg(direction="down"))._choose_anchor(23_460) == 23_400


def test_nearest_is_half_up_where_round_is_bankers():
    """Two modes that differ only at the exact half, kept apart deliberately."""
    two_way = Ladder(config=cfg(direction="both"))
    assert two_way._choose_anchor(23_450) == 23_500
    assert two_way._choose_anchor(23_550) == 23_600

    bankers = Ladder(config=cfg(), anchor_mode="round")
    assert bankers._choose_anchor(23_450) == 23_400        # ties-to-even
    assert bankers._choose_anchor(23_550) == 23_600


def test_an_explicit_anchor_mode_beats_the_direction_default():
    lad = Ladder(config=cfg(direction="both", anchor_mode="floor"))
    assert lad._choose_anchor(23_460) == 23_400


# ============================== persistence


def test_state_round_trips_both_bounds():
    lad = ladder(direction="both")
    feed(lad, [23_400, 23_200, 23_600])

    revived = Ladder(config=cfg(direction="both"))
    revived.load_state(lad.dump_state())

    assert revived.anchor == lad.anchor
    assert revived.last_level == lad.last_level
    assert revived.high_level == lad.high_level
    assert revived.fired_levels == lad.fired_levels
    assert feed(revived, [23_600, 23_200]) == [], "a resumed ladder re-fired a rung it holds"


def test_state_written_before_the_up_side_existed_uses_the_anchor_as_its_bound():
    """Two live runs are stored in exactly this shape. Nothing above their
    anchor has ever fired, so the anchor is the correct high bound."""
    revived = Ladder(config=cfg(direction="both"))
    revived.load_state({"anchor": 23_400.0, "last_level": 23_300.0, "fired_levels": [233, 234]})

    assert revived.high_level == 23_400.0
    assert [x.level for x in revived.on_price(23_500, t(1))] == [23_500]


def test_reset_clears_the_up_bound_too():
    """It is called on every expiry roll. A stale high bound would suppress
    the whole up side of the next campaign."""
    lad = ladder(direction="both")
    feed(lad, [23_400, 23_700])
    assert lad.high_level == 23_700

    lad.reset()
    assert lad.high_level is None


# ============================== reporting


def test_next_trigger_still_means_the_down_rung():
    """Every existing surface reads this field. It must not change meaning
    under a two-way ladder, or the live dashboard starts lying."""
    lad = ladder(direction="both")
    lad.on_price(23_400, t(0))

    assert lad.next_trigger_level == 23_300
    assert lad.next_down_level == 23_300
    assert lad.next_up_level == 23_500
    assert lad.distance_to_next(23_420) == pytest.approx(120)
    assert lad.distance_to_next_up(23_420) == pytest.approx(80)


def test_an_up_only_ladder_reports_the_up_rung_as_its_next_trigger():
    lad = ladder(direction="up")
    lad.on_price(23_400, t(0))

    assert lad.next_down_level is None
    assert lad.next_trigger_level == 23_500


# ============================== end to end, through the backtester


def _backtest(direction: str, prices: list[float], **kw):
    """A deterministic run, so the comparison below is about the strategy."""
    from engine.backtest.providers import ModelPriceProvider
    from engine.backtest.runner import Backtest, BacktestParams, weekly_expiry_resolver
    from engine.pricing.costs import CostModel
    from engine.pricing.iv_surface import IVSurface

    expiry = dt.date(2026, 3, 26)
    start = dt.datetime(2026, 3, 23, 9, 15, tzinfo=IST)
    config = StrategyConfig(lots=1, lot_size=75, direction=direction, **kw)
    engine = Backtest(
        BacktestParams(strategy=config, costs=CostModel()),
        ModelPriceProvider(surface=IVSurface(atm_vol=0.14)),
        weekly_expiry_resolver([expiry]),
    )
    return engine.run([(start + dt.timedelta(minutes=5 * i), p) for i, p in enumerate(prices)])


# Falls 200, then rallies 600 through the anchor and settles near the high --
# the month type the document says a down-only ladder handles worst.
FALL_THEN_RALLY = [24_000, 23_900, 23_800, 23_900, 24_100, 24_250, 24_400, 24_300]


def test_the_up_side_earns_its_keep_on_a_rally_through_the_anchor():
    """Not a claim that two-way is better -- that is what the backtest matrix
    is for. It is a check that the up side does the thing it exists to do:
    hold condors near where the market actually settled."""
    down = _backtest("down", FALL_THEN_RALLY)
    both = _backtest("both", FALL_THEN_RALLY)

    assert [t.side for t in down.triggers] == ["anchor", "down", "down"]
    assert [t.side for t in both.triggers][:3] == ["anchor", "down", "down"]
    assert {t.side for t in both.triggers[3:]} == {"up"}
    assert both.metrics.net_pnl > down.metrics.net_pnl


def test_the_up_side_offsets_the_same_way_the_down_side_does():
    """The netting thesis is direction-agnostic: cancellation needs two rungs
    two steps apart, not a particular order of arrival."""
    down = _backtest("down", FALL_THEN_RALLY)
    both = _backtest("both", FALL_THEN_RALLY)

    assert both.netting["offset_ratio"] > down.netting["offset_ratio"]
    assert both.netting["strikes_fully_offset"] > down.netting["strikes_fully_offset"]


def test_a_down_only_backtest_is_untouched_by_the_up_side_existing():
    """The parity gate in test_ladder_parity covers this exhaustively; this is
    the same claim stated where someone reading the two-way code will see it."""
    before = _backtest("down", FALL_THEN_RALLY)

    assert len(before.condors) == 3
    assert [int(t.level) for t in before.triggers] == [24_000, 23_900, 23_800]


def test_per_side_caps_reach_the_backtest():
    capped = _backtest("both", FALL_THEN_RALLY, max_up=1)

    assert [t.side for t in capped.triggers].count("up") == 1
