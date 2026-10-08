"""End-to-end backtest tests over a deterministic synthetic price path."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pandas as pd
import pytest

from engine.backtest.providers import (
    CandlePriceProvider,
    FallbackPriceProvider,
    ModelPriceProvider,
    PriceRequest,
    Quote,
)
from engine.backtest.runner import (
    Backtest,
    BacktestParams,
    discover_requirements,
    weekly_expiry_resolver,
)
from engine.config import IST
from engine.pricing.costs import ZERO_COST, CostModel
from engine.pricing.iv_surface import IVSurface
from engine.strategy.condor import CondorStatus, PriceSource, StrategyConfig

EXPIRY = dt.date(2026, 3, 26)
START = dt.datetime(2026, 3, 23, 9, 15, tzinfo=IST)

SURFACE = IVSurface(atm_vol=0.14)


def cfg(**kw) -> StrategyConfig:
    return StrategyConfig(**{"lots": 1, "lot_size": 75, **kw})


def model_provider(**kw) -> ModelPriceProvider:
    return ModelPriceProvider(surface=SURFACE, **kw)


def spot_path(prices: list[float], start: dt.datetime = START, minutes: int = 5):
    return [(start + dt.timedelta(minutes=minutes * i), p) for i, p in enumerate(prices)]


def run(prices, params=None, provider=None, expiries=(EXPIRY,), minutes=5, to_expiry=True):
    """Replay `prices`, then -- unless `to_expiry` is False -- a bar at the
    last expiry's close at the last price, so a position held to expiry is
    settled rather than left open when the range ends."""
    params = params or BacktestParams(strategy=cfg(), costs=ZERO_COST)
    provider = provider or model_provider()
    engine = Backtest(params, provider, weekly_expiry_resolver(list(expiries)))
    path = spot_path(prices, minutes=minutes)
    if to_expiry:
        path.append((dt.datetime.combine(max(expiries), dt.time(15, 30), tzinfo=IST), prices[-1]))
    return engine.run(path)


# ============================================================== pass 1


def test_pass_one_discovers_only_the_legs_the_ladder_touches():
    params = BacktestParams(strategy=cfg())
    triggers, requirements = discover_requirements(
        spot_path([24_000, 23_900, 23_800]), params, weekly_expiry_resolver([EXPIRY])
    )
    assert len(triggers) == 3
    # 3 rungs x 4 legs = 12, minus the strikes shared between rungs.
    assert len(requirements) == 10
    strikes = {(r.strike, r.right) for r in requirements}
    assert (23_600, "PE") in strikes   # long wing of rung 24000, short of rung 23800
    assert (24_400, "CE") in strikes
    assert all(r.expiry == EXPIRY for r in requirements)


def test_pass_one_is_far_cheaper_than_fetching_the_whole_chain():
    """The point of the two-pass design: fetch ~dozens of legs, not thousands."""
    params = BacktestParams(strategy=cfg(max_condors=20))
    _, requirements = discover_requirements(
        spot_path([24_000 - 25 * i for i in range(200)]), params, weekly_expiry_resolver([EXPIRY])
    )
    assert len(requirements) < 60


# ============================================================== the run


def test_flat_market_holds_the_anchor_condor_to_expiry_and_keeps_the_credit():
    result = run([24_000] * 40)
    assert len(result.condors) == 1
    condor = result.condors[0]
    assert condor.status is CondorStatus.EXPIRED
    # Spot pinned between the shorts, so every option expires worthless.
    assert condor.realised_pnl() == pytest.approx(condor.credit)
    assert result.metrics.net_pnl > 0
    assert result.metrics.win_rate == 1.0


def test_declining_market_builds_the_ladder():
    result = run([24_000, 23_900, 23_800, 23_700] + [23_700] * 20)
    assert [c.level for c in result.condors] == [24_000, 23_900, 23_800, 23_700]
    assert result.metrics.condors == 4
    assert result.metrics.max_concurrent == 4


def test_equity_curve_is_produced_for_every_bar():
    prices = [24_000] * 30
    result = run(prices, to_expiry=False)
    assert len(result.equity) == len(prices)
    assert all(p.drawdown <= 0 for p in result.equity)
    assert result.equity[0].spot == 24_000


def test_everything_open_is_settled_at_the_end_of_the_range():
    result = run([24_000, 23_900])          # range ends before expiry
    assert all(not c.is_open for c in result.condors)
    assert all(c.exit_reason for c in result.condors)


def test_a_crash_through_the_lower_wing_is_capped_at_max_loss():
    """The ladder's whole premise: losses are bounded by the long wings."""
    result = run([24_000] + [20_000] * 10, params=BacktestParams(strategy=cfg(max_condors=1), costs=ZERO_COST))
    condor = result.condors[0]
    assert condor.realised_pnl() == pytest.approx(-condor.max_loss)
    assert condor.realised_pnl() >= -(200 * 75)


def test_no_condor_can_lose_more_than_one_wing_width():
    result = run([24_000, 23_500, 23_000, 22_500] + [22_000] * 10)
    for condor in result.condors:
        assert condor.realised_pnl() >= -(200 * 75) - 1e-6


# ============================================================== exits


def test_take_profit_closes_early():
    params = BacktestParams(strategy=cfg(take_profit_pct=0.4), costs=ZERO_COST)
    # Hourly bars across the three days to expiry, so decay alone captures 40%.
    result = run([24_000] * 80, params=params, minutes=60)
    condor = result.condors[0]
    assert condor.status is CondorStatus.CLOSED_TARGET
    assert "take-profit" in (condor.exit_reason or "")
    assert condor.exit_time is not None and condor.exit_time < dt.datetime(
        2026, 3, 26, 15, 30, tzinfo=IST
    )


def test_stop_loss_closes_on_an_adverse_move():
    params = BacktestParams(strategy=cfg(stop_loss_mult=1.5), costs=ZERO_COST)
    result = run([24_000] + [23_300] * 10, params=params)
    condor = result.condors[0]
    assert condor.status is CondorStatus.CLOSED_STOP
    assert "stop-loss" in (condor.exit_reason or "")


def test_trailing_stop_closes_on_pullback_from_peak():
    # Decay gathers profit during flat hours, then adverse move triggers trailing stop
    params = BacktestParams(
        strategy=cfg(trailing_sl_mult=0.2, trailing_sl_trigger_pct=0.25),
        costs=ZERO_COST,
    )
    spots = [24_000] * 35 + [23_600] * 10
    result = run(spots, params=params, minutes=60)
    condor = result.condors[0]
    assert condor.status is CondorStatus.CLOSED_TRAILING_STOP
    assert "trailing-stop" in (condor.exit_reason or "")


def test_campaign_stop_loss_halts_entries_and_squares_off_campaign():
    params = BacktestParams(strategy=cfg(step=100.0), campaign_stop_loss=10_000.0, costs=ZERO_COST)
    spots = [24_000, 23_900, 23_800, 23_500, 23_400, 23_300]
    result = run(spots, params=params)
    closed_stops = [c for c in result.condors if c.status is CondorStatus.CLOSED_STOP]
    assert len(closed_stops) > 0
    assert any("campaign stop-loss" in (c.exit_reason or "") for c in closed_stops)


def test_campaign_trailing_sl_pct_halts_upcoming_campaign():
    # Campaign 1 runs and finishes with positive profit (held flat to expiry)
    # Campaign 2 opens, market drops heavily, breaching the 15% TSL threshold
    params = BacktestParams(
        strategy=cfg(step=100.0),
        campaign_trailing_sl_pct=0.15,
        costs=ZERO_COST,
        roll_to_next_expiry=True,
    )
    engine = Backtest(params, model_provider(), weekly_expiry_resolver([EXPIRY, EXPIRY_2], min_dte=1))
    # Prices: flat at 24000 across first expiry (settles positive), then plunges in second expiry
    prices = [24_000] * 80 + [23_600, 23_500, 23_200, 23_000] + [22_800] * 20
    result = engine.run(spot_path(prices, minutes=60))

    # Campaign 1 settled
    first_camp = [c for c in result.condors if c.expiry == EXPIRY]
    assert len(first_camp) > 0
    # Campaign 2 was stopped by trailing SL
    second_camp = [c for c in result.condors if c.expiry == EXPIRY_2]
    assert len(second_camp) > 0
    tsl_closed = [c for c in second_camp if c.status is CondorStatus.CLOSED_TRAILING_STOP]
    assert len(tsl_closed) > 0
    assert any("campaign trailing-stop" in (c.exit_reason or "") for c in tsl_closed)
    assert any("campaign trailing-stop" in w for w in result.warnings)


def test_subsequent_campaign_trades_with_remaining_capital_after_stop():
    """When a monthly campaign hits TSL, that month halts, but the next campaign starts and trades with remaining capital."""
    exp3 = dt.date(2026, 4, 9)
    params = BacktestParams(
        strategy=cfg(step=100.0),
        campaign_trailing_sl_pct=0.15,
        costs=ZERO_COST,
        roll_to_next_expiry=True,
    )
    engine = Backtest(params, model_provider(), weekly_expiry_resolver([EXPIRY, EXPIRY_2, exp3], min_dte=1))
    # Prices: flat in expiry 1, drops in expiry 2, crosses into expiry 3
    prices = [24_000] * 80 + [23_600, 23_500, 23_200, 23_000, 22_800] * 20 + [24_000] * 250
    result = engine.run(spot_path(prices, minutes=60))

    # Campaign 2 was stopped by trailing SL
    second_camp = [c for c in result.condors if c.expiry == EXPIRY_2]
    assert len(second_camp) > 0
    tsl_closed = [c for c in second_camp if c.status is CondorStatus.CLOSED_TRAILING_STOP]
    assert len(tsl_closed) > 0

    # Campaign 3 started and traded with remaining capital!
    third_camp = [c for c in result.condors if c.expiry == exp3]
    assert len(third_camp) > 0, "System must open positions in next campaign with remaining capital"


def test_tsl_respects_activation_hurdle():
    """TSL remains dormant until peak capital reaches the activation hurdle."""
    params = BacktestParams(
        strategy=cfg(step=100.0),
        campaign_trailing_sl_pct=0.15,
        campaign_trailing_sl_trigger=50_000.0,  # Hurdle 50k > Campaign 1 peak (~5.2k)
        costs=ZERO_COST,
        roll_to_next_expiry=True,
    )
    engine = Backtest(params, model_provider(), weekly_expiry_resolver([EXPIRY, EXPIRY_2], min_dte=1))
    prices = [24_000] * 80 + [23_600, 23_500, 23_200, 23_000] + [22_800] * 20
    result = engine.run(spot_path(prices, minutes=60))

    # Campaign 2 opens and trades because 50k hurdle was not reached (early noise protected)
    second_camp = [c for c in result.condors if c.expiry == EXPIRY_2]
    assert len(second_camp) > 0
    # No condors closed by TSL because TSL was disarmed below the 50k hurdle
    tsl_closed = [c for c in second_camp if c.status is CondorStatus.CLOSED_TRAILING_STOP]
    assert len(tsl_closed) == 0


def test_dynamic_step_backtest_applies_vix_step():
    params = BacktestParams(
        strategy=cfg(step=100.0, dynamic_step=True, dynamic_step_condors=5),
        costs=ZERO_COST,
        roll_to_next_expiry=True,
    )
    engine = Backtest(params, model_provider(), weekly_expiry_resolver([EXPIRY, EXPIRY_2], min_dte=1))
    # At spot 24,000, vix = 11.10: expected move 769 pts / 5 = 153.8 -> 150 pts step!
    prices = [24_000] * 10 + [23_850, 23_700] + [23_700] * 10
    path = spot_path(prices, minutes=60)
    vix_series = [11.10] * len(path)
    result = engine.run(path, vix=vix_series)

    levels = [c.level for c in result.condors]
    assert 24000.0 in levels
    assert 23850.0 in levels
    assert 23700.0 in levels
    assert abs(levels[0] - levels[1]) == 150.0
    assert any("dynamic VIX step 150 pts applied" in w for w in result.warnings)


def test_slippage_is_charged_on_the_way_out_as_well_as_in():
    """Charging it only on entry understated the round trip by about half,
    which flatters exactly the configurations that trade most."""
    free = ZERO_COST
    charged = replace(free, slippage_points=2.0)
    spots = [24_000] * 80

    without = run(spots, params=BacktestParams(strategy=cfg(take_profit_pct=0.4), costs=free),
                  minutes=60).condors[0]
    with_slip = run(spots, params=BacktestParams(strategy=cfg(take_profit_pct=0.4), costs=charged),
                    minutes=60).condors[0]

    assert without.status is CondorStatus.CLOSED_TARGET
    assert with_slip.status is CondorStatus.CLOSED_TARGET
    per_leg = 2.0 * with_slip.legs[0].leg.qty
    assert with_slip.entry_costs == pytest.approx(4 * per_leg)
    assert with_slip.exit_costs == pytest.approx(4 * per_leg), "both halves, not one"


def test_a_cash_settlement_is_not_charged_slippage():
    """There is no spread to cross at expiry: the exchange settles at
    intrinsic, so only STT on in-the-money shorts applies."""
    charged = replace(ZERO_COST, slippage_points=2.0)
    result = run([24_000] * 10, params=BacktestParams(strategy=cfg(), costs=charged))
    condor = result.condors[0]

    assert condor.status is CondorStatus.EXPIRED
    assert condor.exit_costs == pytest.approx(0.0)


def test_without_exits_configured_the_condor_is_held_to_expiry():
    result = run([24_000] * 40)
    assert result.condors[0].status is CondorStatus.EXPIRED


# ============================================================== costs


def test_costs_reduce_net_pnl_versus_a_frictionless_run():
    free = run([24_000] * 40, params=BacktestParams(strategy=cfg(), costs=ZERO_COST))
    charged = run([24_000] * 40, params=BacktestParams(strategy=cfg(), costs=CostModel()))
    assert charged.metrics.net_pnl < free.metrics.net_pnl
    assert charged.metrics.total_costs > 0
    assert charged.metrics.gross_pnl == pytest.approx(
        charged.metrics.net_pnl + charged.metrics.total_costs
    )


# ============================================================== providers


def test_real_candles_are_preferred_over_the_model():
    candles = CandlePriceProvider()
    frame = pd.DataFrame({"ts": [START], "close": [123.0]})
    for strike, right in [(23_800, "PE"), (23_600, "PE"), (24_200, "CE"), (24_400, "CE")]:
        candles.add(EXPIRY, strike, right, frame)

    provider = FallbackPriceProvider(primary=candles, fallback=model_provider())
    result = run([24_000] * 3, provider=provider)

    assert provider.real_quotes > 0
    assert provider.real_fraction == 1.0
    assert all(fl.source is PriceSource.CHOICE for fl in result.condors[0].legs)
    assert all(fl.entry_price == 123.0 for fl in result.condors[0].legs)


def test_missing_candles_fall_back_to_the_model_and_are_flagged():
    provider = FallbackPriceProvider(primary=CandlePriceProvider(), fallback=model_provider())
    result = run([24_000] * 3, provider=provider)

    assert provider.real_quotes == 0
    assert provider.modeled_quotes > 0
    assert result.condors[0].uses_modeled_prices
    assert result.metrics.real_price_fraction == 0.0
    assert any("MODELED" in w for w in result.warnings)


def test_stale_candles_are_refused_rather_than_marked_at_a_dead_price():
    candles = CandlePriceProvider(max_staleness=dt.timedelta(minutes=5))
    old = pd.DataFrame({"ts": [START - dt.timedelta(hours=3)], "close": [99.0]})
    candles.add(EXPIRY, 23_800, "PE", old)
    quote = candles.quote(
        PriceRequest(expiry=EXPIRY, strike=23_800, right="PE", when=START, spot=24_000)
    )
    assert quote is None


def test_candle_lookup_never_uses_a_future_bar():
    candles = CandlePriceProvider()
    future = pd.DataFrame({"ts": [START + dt.timedelta(hours=1)], "close": [50.0]})
    candles.add(EXPIRY, 23_800, "PE", future)
    quote = candles.quote(
        PriceRequest(expiry=EXPIRY, strike=23_800, right="PE", when=START, spot=24_000)
    )
    assert quote is None


def test_a_rung_with_an_unpriceable_leg_is_skipped_not_half_opened():
    """Three of four legs would leave a naked short in the book."""

    class OnlyPuts:
        def quote(self, request):
            if request.right != "PE":
                return None
            return model_provider().quote(request)

    result = run([24_000] * 3, provider=OnlyPuts())
    assert result.condors == []
    assert result.skipped
    assert any("skipped" in w for w in result.warnings)


# ============================================================== reporting


def test_strike_matrix_shows_the_offsetting_legs():
    result = run([24_000, 23_900, 23_800] + [23_800] * 10)
    rows = {(r["right"], r["strike"]): r for r in result.strike_matrix()}

    offset = rows[("PE", 23_600.0)]
    assert offset["is_flat"]
    assert offset["net_qty"] == 0
    assert set(offset["by_condor"]) == {0, 2}     # rungs 24000 and 23800
    assert sorted(offset["by_condor"].values()) == [-75, 75]


def test_netting_summary_is_reported_on_the_result():
    result = run([24_000, 23_900, 23_800, 23_700] + [23_700] * 5)
    summary = result.netting
    assert summary["offset_qty"] > 0
    assert summary["strikes_fully_offset"] >= 2


def test_empty_input_returns_a_warning_not_a_crash():
    engine = Backtest(BacktestParams(), model_provider(), weekly_expiry_resolver([EXPIRY]))
    result = engine.run([])
    assert result.condors == []
    assert any("No spot data" in w for w in result.warnings)


def test_expiry_resolver_rolls_past_the_front_week():
    resolve = weekly_expiry_resolver([EXPIRY, dt.date(2026, 4, 2)], min_dte=1)
    assert resolve(dt.date(2026, 3, 23)) == EXPIRY
    assert resolve(EXPIRY) == dt.date(2026, 4, 2)   # on expiry day, roll out


# ====================================================== expiry roll


EXPIRY_2 = dt.date(2026, 4, 2)


def two_expiry_run(prices, roll: bool, minutes=60):
    params = BacktestParams(strategy=cfg(), costs=ZERO_COST, roll_to_next_expiry=roll)
    engine = Backtest(params, model_provider(), weekly_expiry_resolver([EXPIRY, EXPIRY_2], min_dte=1))
    return engine.run(spot_path(prices, minutes=minutes))


def test_ladder_re_anchors_in_the_new_expiry():
    """After an expiry settles, a fresh ladder starts at the prevailing spot.

    Without this the ladder keeps its old reference level, sits waiting for a
    price far below the market, and simply stops trading.
    """
    # Fall to 23,800 before expiry, then trade flat at 24,500 well after it.
    prices = [24_000, 23_900, 23_800] + [24_500] * 120
    result = two_expiry_run(prices, roll=True)

    assert result.rolls, "expected a roll into the next expiry"
    expiries = {c.expiry for c in result.condors}
    assert expiries == {EXPIRY, EXPIRY_2}
    # The second campaign anchors near 24,500, not at the stale 23,800.
    second = [c for c in result.condors if c.expiry == EXPIRY_2]
    assert second and max(c.level for c in second) >= 24_400


def test_without_the_roll_the_ladder_stalls():
    """The old behaviour, kept available and shown to be the worse one."""
    prices = [24_000, 23_900, 23_800] + [24_500] * 120
    result = two_expiry_run(prices, roll=False)

    assert result.rolls == []
    assert {c.expiry for c in result.condors} == {EXPIRY}
    # Price spent the rest of the range far above the stale reference level,
    # so nothing else ever fired.
    assert len(result.condors) == 3


def test_each_campaign_gets_its_own_rung_budget():
    params = BacktestParams(strategy=cfg(max_condors=2), costs=ZERO_COST, roll_to_next_expiry=True)
    engine = Backtest(params, model_provider(), weekly_expiry_resolver([EXPIRY, EXPIRY_2], min_dte=1))
    result = engine.run(spot_path([24_000, 23_900, 23_800] + [24_500] * 120, minutes=60))

    for expiry in {c.expiry for c in result.condors}:
        assert len([c for c in result.condors if c.expiry == expiry]) <= 2


def test_offsetting_never_crosses_an_expiry():
    """The strategy's premise holds only within one expiry.

    A long 23,600 PE in March and a short 23,600 PE in April are different
    instruments; they must not be reported as cancelling.
    """
    result = two_expiry_run([24_000, 23_900, 23_800] + [24_500] * 120, roll=True)
    rows = result.strike_matrix()
    for row in rows:
        contributors = row["by_condor"]
        expiries = {c.expiry.isoformat() for c in result.condors if c.index in contributors}
        assert expiries <= {row["expiry"]}, f"row {row['strike']}{row['right']} mixes expiries"


def test_roll_is_reported_to_the_user():
    result = two_expiry_run([24_000, 23_900, 23_800] + [24_500] * 120, roll=True)
    assert result.campaigns == len(result.rolls) + 1
    assert any("expiry campaigns" in w for w in result.warnings)


def test_a_single_expiry_run_reports_one_campaign():
    result = run([24_000] * 40)
    assert result.rolls == []
    assert result.campaigns == 1


# ================================= P&L identities found by the verification run


def test_a_condor_settles_on_its_expiry_day_even_with_no_late_bar():
    """Daily candles are stamped at the session open, so "past 15:30 on expiry
    day" never fires and every condor used to settle the *next* day against the
    *next* day's spot -- the wrong settlement price for every trade in the run.
    """
    day_before = dt.datetime.combine(EXPIRY - dt.timedelta(days=1), dt.time(9, 15), tzinfo=IST)
    bars = [
        (day_before, 24_000.0),
        (dt.datetime.combine(EXPIRY, dt.time(9, 15), tzinfo=IST), 23_950.0),
        (dt.datetime.combine(EXPIRY + dt.timedelta(days=1), dt.time(9, 15), tzinfo=IST), 22_000.0),
    ]
    params = BacktestParams(strategy=cfg(), costs=ZERO_COST)
    result = Backtest(params, model_provider(), weekly_expiry_resolver([EXPIRY])).run(bars)

    settled = [c for c in result.condors if c.expiry == EXPIRY and not c.is_open]
    assert settled, "nothing settled"
    for condor in settled:
        assert condor.exit_time is not None
        assert condor.exit_time.date() <= EXPIRY, "settled after its own expiry"


def test_the_equity_curve_ends_where_the_reported_pnl_does():
    """Condors still open at the end settle after the final equity point was
    recorded, so the curve used to stop short of net P&L -- and drawdown,
    Sharpe and CAGR were all computed from that truncated curve.
    """
    result = run([24_000, 23_900, 23_800])          # range ends before expiry
    assert result.equity
    assert result.equity[-1].equity == pytest.approx(result.metrics.net_pnl, abs=0.01)
    assert result.equity[-1].open_condors == 0


def test_max_loss_is_never_smaller_than_the_worst_payoff():
    """A risk figure that understates risk is the wrong way round.

    `payoff_at_expiry` subtracts exit costs; `max_loss` did not, so a condor
    could report losing more than its stated maximum.
    """
    result = run([24_000, 23_900], params=BacktestParams(strategy=cfg(), costs=CostModel()))
    assert result.condors
    for condor in result.condors:
        for probe in (condor.level - 3_000, condor.level - 400, condor.level + 3_000):
            assert condor.payoff_at_expiry(probe) >= -condor.max_loss - 0.01


def test_payoff_never_exceeds_the_stated_max_profit():
    result = run([24_000, 23_900], params=BacktestParams(strategy=cfg(), costs=CostModel()))
    for condor in result.condors:
        probe = condor.level - 3_000
        while probe <= condor.level + 3_000:
            assert condor.payoff_at_expiry(probe) <= condor.max_profit + 0.01
            probe += 50.0


# ============================================ each contract at its own lot size


def test_each_contract_trades_at_the_lot_size_of_its_expiry():
    """A backtest over years sized every trade at today's lot."""
    from engine.data.lot_sizes import nifty_lot_size

    assert [nifty_lot_size(dt.date(y, m, d)) for y, m, d in
            [(2020, 3, 26), (2021, 7, 22), (2021, 7, 29), (2024, 4, 25), (2024, 5, 2),
             (2024, 12, 26), (2025, 1, 2), (2025, 1, 30), (2025, 12, 30), (2026, 1, 6)]] == \
        [75, 75, 50, 50, 25, 25, 75, 25, 75, 65]
    far = dt.date.today() + dt.timedelta(days=30)
    assert nifty_lot_size(far, current=70) == 70, "a listed contract's own size wins"

    params = BacktestParams(strategy=cfg(lot_size=65), costs=ZERO_COST, lot_size_for=lambda e: 25)
    result = run([24_000] * 5, params=params)
    assert {fl.leg.qty for fl in result.condors[0].legs} == {25}
    assert result.condors[0].config.lot_size == 25


def test_unphysical_condor_credit_exceeding_wing_width_is_rejected():
    """An iron condor with net credit exceeding wing_width * qty (e.g. from
    corrupt recycled token prices) violates no-arbitrage bounds and must be rejected."""
    # Custom provider that prices legs with an impossible spread:
    # Sell legs at 7486.0 (like recycled Apollo Hospitals stock future),
    # Buy legs at 10.0.
    # Wing width is 200, qty is 65 -> max allowed credit is 13,000.
    # Credit would be (7486*2 - 10*2) * 65 = ~971,880!
    class CorruptProvider:
        def quote(self, request):
            if request.strike in (23800.0, 24200.0):
                # Short legs
                return Quote(price=1000.0, source=PriceSource.CHOICE)
            # Long legs
            return Quote(price=10.0, source=PriceSource.CHOICE)

    result = run([24_000] * 5, provider=CorruptProvider())
    assert len(result.condors) == 0, "Corrupt condor exceeding wing width must not be opened"
    assert any("invalid condor credit" in reason for _, _, reason in result.skipped)


def test_candle_price_provider_rejects_unphysical_frames_and_quotes():
    """CandlePriceProvider drops frames with price > 2500 or PE >= strike."""
    provider = CandlePriceProvider()
    expiry = dt.date(2025, 10, 28)
    frame = pd.DataFrame({
        "ts": [dt.datetime(2025, 10, 3, 10, 0, tzinfo=IST)],
        "close": [7486.35],
    })
    provider.add(expiry, 24700.0, "PE", frame)
    assert (expiry, 24700.0, "PE") not in provider.candles

    # Valid price is accepted
    valid_frame = pd.DataFrame({
        "ts": [dt.datetime(2025, 10, 3, 10, 0, tzinfo=IST)],
        "close": [125.45],
    })
    provider.add(expiry, 24700.0, "PE", valid_frame)
    assert (expiry, 24700.0, "PE") in provider.candles
    q = provider.quote(PriceRequest(expiry=expiry, strike=24700.0, right="PE",
                                    when=dt.datetime(2025, 10, 3, 10, 0, tzinfo=IST), spot=25000.0))
    assert q is not None and q.price == 125.45

