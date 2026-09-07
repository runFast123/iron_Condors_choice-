"""End-to-end backtest tests over a deterministic synthetic price path."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from engine.backtest.providers import (
    CandlePriceProvider,
    FallbackPriceProvider,
    ModelPriceProvider,
    PriceRequest,
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


def run(prices, params=None, provider=None, expiries=(EXPIRY,), minutes=5):
    params = params or BacktestParams(strategy=cfg(), costs=ZERO_COST)
    provider = provider or model_provider()
    engine = Backtest(params, provider, weekly_expiry_resolver(list(expiries)))
    return engine.run(spot_path(prices, minutes=minutes))


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
    result = run(prices)
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
