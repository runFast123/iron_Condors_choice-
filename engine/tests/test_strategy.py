"""Tests for the ladder trigger engine and condor construction/netting."""

from __future__ import annotations

import datetime as dt

import pytest

from engine.strategy.condor import (
    CALL,
    PUT,
    Condor,
    CondorStatus,
    FilledLeg,
    PriceSource,
    Side,
    StrategyConfig,
    build_legs,
    net_positions,
    netting_summary,
    portfolio_payoff,
)
from engine.strategy.ladder import Ladder, simulate_levels

T0 = dt.datetime(2026, 3, 20, 9, 15)
EXPIRY = dt.date(2026, 3, 26)


def cfg(**kw) -> StrategyConfig:
    return StrategyConfig(**{"lots": 1, "lot_size": 75, **kw})


def feed(ladder: Ladder, prices: list[float]) -> list[float]:
    """Push a price series through the ladder; return the levels fired."""
    fired: list[float] = []
    for i, price in enumerate(prices):
        for trigger in ladder.on_price(price, T0 + dt.timedelta(minutes=i)):
            fired.append(trigger.level)
    return fired


# =========================================================== the spec's table


def test_reproduces_the_specified_entry_table():
    """Spot 24000 declining to 23700 must produce exactly the four documented rungs."""
    ladder = Ladder(config=cfg())
    fired = feed(ladder, [24_000, 23_900, 23_800, 23_700])
    assert fired == [24_000, 23_900, 23_800, 23_700]

    expected = {
        24_000: dict(short_pe=23_800, long_pe=23_600, short_ce=24_200, long_ce=24_400),
        23_900: dict(short_pe=23_700, long_pe=23_500, short_ce=24_100, long_ce=24_300),
        23_800: dict(short_pe=23_600, long_pe=23_400, short_ce=24_000, long_ce=24_200),
        23_700: dict(short_pe=23_500, long_pe=23_300, short_ce=23_900, long_ce=24_100),
    }
    for level, strikes in expected.items():
        assert ladder.planned_strikes(level) == pytest.approx(strikes), level


def test_leg_structure_matches_the_spec():
    legs = build_legs(24_000, cfg())
    as_set = {(leg.side, leg.right, leg.strike) for leg in legs}
    assert as_set == {
        (Side.SELL, PUT, 23_800),
        (Side.BUY, PUT, 23_600),
        (Side.SELL, CALL, 24_200),
        (Side.BUY, CALL, 24_400),
    }
    assert all(leg.qty == 75 for leg in legs)


def test_protective_wings_are_ordered_before_the_shorts():
    """Selling before the hedge exists spikes margin and invites rejection."""
    legs = build_legs(24_000, cfg())
    first_short = next(i for i, leg in enumerate(legs) if leg.side is Side.SELL)
    last_long = max(i for i, leg in enumerate(legs) if leg.side is Side.BUY)
    assert last_long < first_short


# ================================================================ ladder rules


def test_rallies_do_not_fire():
    ladder = Ladder(config=cfg())
    assert feed(ladder, [24_000, 24_100, 24_300, 24_500]) == [24_000]


def test_each_level_fires_at_most_once():
    """The confirmed rule: revisiting a level must not stack another condor."""
    ladder = Ladder(config=cfg())
    fired = feed(ladder, [24_000, 23_900, 24_100, 23_900, 23_800, 24_000, 23_800])
    assert fired == [24_000, 23_900, 23_800]


def test_partial_dip_below_a_step_does_not_fire():
    ladder = Ladder(config=cfg())
    assert feed(ladder, [24_000, 23_950, 23_910, 23_901]) == [24_000]


def test_crossing_the_step_fires_exactly_once():
    ladder = Ladder(config=cfg())
    assert feed(ladder, [24_000, 23_901, 23_899]) == [24_000, 23_900]


def test_gap_down_fills_every_skipped_level():
    # A drop to 23,650 has reached 23,700 but NOT 23,600.
    ladder = Ladder(config=cfg(fill_gaps=True))
    assert feed(ladder, [24_000, 23_650]) == [24_000, 23_900, 23_800, 23_700]


def test_gap_down_reaching_a_level_exactly_fires_it():
    ladder = Ladder(config=cfg(fill_gaps=True))
    assert feed(ladder, [24_000, 23_600]) == [24_000, 23_900, 23_800, 23_700, 23_600]


def test_gap_down_without_fill_gaps_fires_only_the_landing_level():
    ladder = Ladder(config=cfg(fill_gaps=False))
    assert feed(ladder, [24_000, 23_650]) == [24_000, 23_700]


def test_max_condors_caps_the_ladder():
    ladder = Ladder(config=cfg(max_condors=3))
    fired = feed(ladder, [24_000, 23_900, 23_800, 23_700, 23_600])
    assert fired == [24_000, 23_900, 23_800]
    assert ladder.next_trigger_level is None


def test_floor_anchor_avoids_a_double_fire_on_the_first_bar():
    ladder = Ladder(config=cfg(), anchor_mode="floor")
    assert feed(ladder, [24_051]) == [24_000]


def test_round_anchor_snaps_to_the_nearest_level():
    ladder = Ladder(config=cfg(), anchor_mode="round")
    assert ladder.on_price(23_987, T0)[0].level == 24_000


def test_explicit_anchor_is_honoured():
    ladder = Ladder(config=cfg(), anchor_mode="explicit", explicit_anchor=24_000)
    assert feed(ladder, [23_970]) == [24_000]


def test_next_trigger_and_distance_are_reported():
    ladder = Ladder(config=cfg())
    ladder.on_price(24_000, T0)
    assert ladder.next_trigger_level == 23_900
    assert ladder.distance_to_next(23_940) == pytest.approx(40)


def test_long_campaign_does_not_drift_on_float_arithmetic():
    """Integer step units keep level 20 exact after a long decline."""
    ladder = Ladder(config=cfg(max_condors=100))
    fired = feed(ladder, [24_000 - 100 * i for i in range(40)])
    assert fired[-1] == pytest.approx(24_000 - 100 * 39)
    assert all(level == round(level) for level in fired)


def test_custom_step_and_offsets():
    ladder = Ladder(config=cfg(step=50, short_offset=100, long_offset=250))
    assert feed(ladder, [24_000, 23_950]) == [24_000, 23_950]
    assert ladder.planned_strikes(24_000) == pytest.approx(
        dict(long_pe=23_750, short_pe=23_900, short_ce=24_100, long_ce=24_250)
    )


def test_simulate_levels_is_pass_one_of_the_backtest():
    triggers = simulate_levels([24_000, 23_900, 23_800], cfg())
    assert [t.level for t in triggers] == [24_000, 23_900, 23_800]
    assert [t.reason for t in triggers] == ["anchor", "decline", "decline"]


def test_reset_clears_campaign_state():
    ladder = Ladder(config=cfg())
    feed(ladder, [24_000, 23_900])
    ladder.reset()
    assert feed(ladder, [23_900]) == [23_900]


# ============================================================ condor economics


def _condor(level: float, prices: dict[tuple[str, str], float], config=None, index=0) -> Condor:
    """Build a condor with explicit per-leg prices keyed by (side, right)."""
    config = config or cfg()
    legs = build_legs(level, config)
    filled = [
        FilledLeg(leg=leg, entry_price=prices[(leg.side.value, leg.right)], source=PriceSource.CHOICE)
        for leg in legs
    ]
    return Condor(level=level, entry_time=T0, expiry=EXPIRY, legs=filled, config=config, index=index)


PRICES = {("SELL", PUT): 60.0, ("BUY", PUT): 25.0, ("SELL", CALL): 55.0, ("BUY", CALL): 20.0}


def test_credit_is_shorts_minus_longs_times_quantity():
    condor = _condor(24_000, PRICES)
    # (60 - 25) + (55 - 20) = 70 per share, x75
    assert condor.credit == pytest.approx(70 * 75)


def test_max_loss_uses_one_wing_not_two():
    """Only one side can finish ITM, so exposure is one 200-point wing."""
    condor = _condor(24_000, PRICES)
    assert condor.max_loss == pytest.approx(200 * 75 - 70 * 75)
    assert condor.max_profit == pytest.approx(70 * 75)


def test_payoff_is_maximum_between_the_short_strikes():
    condor = _condor(24_000, PRICES)
    assert condor.payoff_at_expiry(24_000) == pytest.approx(condor.max_profit)
    assert condor.payoff_at_expiry(23_850) == pytest.approx(condor.max_profit)


def test_payoff_is_capped_at_max_loss_beyond_the_wings():
    condor = _condor(24_000, PRICES)
    for spot in (23_000, 23_400, 24_600, 25_500):
        assert condor.payoff_at_expiry(spot) == pytest.approx(-condor.max_loss)


def test_payoff_is_zero_at_the_breakevens():
    condor = _condor(24_000, PRICES)
    low, high = condor.breakevens
    assert low == pytest.approx(23_800 - 70)
    assert high == pytest.approx(24_200 + 70)
    assert condor.payoff_at_expiry(low) == pytest.approx(0.0, abs=1e-6)
    assert condor.payoff_at_expiry(high) == pytest.approx(0.0, abs=1e-6)


def test_entry_costs_reduce_credit_and_raise_max_loss():
    condor = _condor(24_000, PRICES)
    condor.entry_costs = 500.0
    assert condor.net_credit == pytest.approx(70 * 75 - 500)
    assert condor.max_loss == pytest.approx(200 * 75 - (70 * 75 - 500))


def test_take_profit_triggers_at_the_configured_fraction():
    config = cfg(take_profit_pct=0.5)
    condor = _condor(24_000, PRICES, config)
    halved = {fl.leg: fl.entry_price * 0.5 for fl in condor.legs}
    assert "take-profit" in (condor.exit_signal(halved) or "")


def test_stop_loss_triggers_at_the_configured_multiple():
    """A crash: the short put balloons far more than its protective wing."""
    config = cfg(stop_loss_mult=2.0)
    condor = _condor(24_000, PRICES, config)
    crash = {
        ("SELL", PUT): 400.0,   # was 60
        ("BUY", PUT): 150.0,    # was 25 - the wing gains far less
        ("SELL", CALL): 0.5,    # was 55 - calls expire worthless
        ("BUY", CALL): 0.1,     # was 20
    }
    prices = {fl.leg: crash[(fl.leg.side.value, fl.leg.right)] for fl in condor.legs}
    assert condor.mtm(prices) < -2 * condor.credit
    assert "stop-loss" in (condor.exit_signal(prices) or "")


def test_a_mere_parallel_shift_in_all_legs_is_not_a_loss():
    """Adding the same premium to every leg nets to zero: shorts offset longs."""
    condor = _condor(24_000, PRICES, cfg(stop_loss_mult=2.0))
    shifted = {fl.leg: fl.entry_price + 100 for fl in condor.legs}
    assert condor.mtm(shifted) == pytest.approx(0.0)
    assert condor.exit_signal(shifted) is None


def test_no_exit_signal_when_holding_to_expiry():
    condor = _condor(24_000, PRICES, cfg())  # no TP/SL configured
    halved = {fl.leg: fl.entry_price * 0.5 for fl in condor.legs}
    assert condor.exit_signal(halved) is None


def test_closed_condor_emits_no_further_signals():
    condor = _condor(24_000, PRICES, cfg(take_profit_pct=0.5))
    condor.close(T0, "done", CondorStatus.CLOSED_TARGET)
    assert condor.exit_signal({fl.leg: 0.0 for fl in condor.legs}) is None


def test_modeled_prices_are_flagged_on_the_condor():
    condor = _condor(24_000, PRICES)
    condor.legs[0].source = PriceSource.MODELED
    assert condor.uses_modeled_prices


# ================================================================== netting


def test_long_put_offsets_the_short_put_two_rungs_below():
    """The strategy's central claim, made explicit.

    Condor at 24000 buys the 23600 PE; condor at 23800 sells the 23600 PE.
    Same strike, opposite sides, so the pair nets flat.
    """
    ladder = [_condor(level, PRICES, index=i) for i, level in enumerate([24_000, 23_900, 23_800])]
    positions = {(p.right, p.strike): p for p in net_positions(ladder)}

    offset = positions[(PUT, 23_600)]
    assert offset.net_qty == 0
    assert offset.is_flat
    assert offset.gross_long == 75 and offset.gross_short == 75
    assert offset.contributors == (0, 2)  # rungs 24000 and 23800


def test_call_side_offsets_the_same_way():
    ladder = [_condor(level, PRICES, index=i) for i, level in enumerate([24_000, 23_900, 23_800])]
    positions = {(p.right, p.strike): p for p in net_positions(ladder)}
    # 24000 sells the 24200 CE; 23800 buys the 24200 CE.
    assert positions[(CALL, 24_200)].net_qty == 0


def test_netting_summary_quantifies_the_self_hedging():
    ladder = [_condor(level, PRICES, index=i) for i, level in enumerate([24_000, 23_900, 23_800, 23_700])]
    summary = netting_summary(ladder)
    assert summary["gross_qty"] == 4 * 4 * 75
    assert summary["offset_qty"] > 0
    assert 0 < summary["offset_ratio"] < 1
    assert summary["strikes_fully_offset"] >= 2


def test_a_single_condor_offsets_nothing():
    summary = netting_summary([_condor(24_000, PRICES)])
    assert summary["offset_qty"] == 0
    assert summary["strikes_fully_offset"] == 0


def test_closed_condors_leave_the_net_book():
    ladder = [_condor(level, PRICES, index=i) for i, level in enumerate([24_000, 23_800])]
    ladder[1].close(T0, "tp", CondorStatus.CLOSED_TARGET)
    positions = {(p.right, p.strike): p for p in net_positions(ladder, open_only=True)}
    assert positions[(PUT, 23_600)].net_qty == 75  # the offsetting short is gone


def test_portfolio_payoff_sums_across_rungs():
    ladder = [_condor(level, PRICES, index=i) for i, level in enumerate([24_000, 23_900])]
    curve = portfolio_payoff(ladder, [23_000, 24_000, 25_000])
    assert curve[1] > curve[0] and curve[1] > curve[2]
    assert curve[1] == pytest.approx(sum(c.payoff_at_expiry(24_000) for c in ladder))
