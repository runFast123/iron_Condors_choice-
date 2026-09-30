"""Tests for Black-76 pricing, greeks and the cost model."""

from __future__ import annotations

import datetime as dt

import math

import pytest

from engine.pricing.black76 import (
    CALL,
    PUT,
    forward_price,
    greeks,
    implied_vol,
    price,
    year_fraction,
)
from engine.pricing.costs import ZERO_COST, CostModel
from engine.strategy.condor import Side

F, K, T, V, R = 24_000.0, 24_000.0, 7 / 365, 0.14, 0.065


# ------------------------------------------------------------------ black-76


def test_put_call_parity_holds():
    """C - P == discounted (F - K). The strongest single check on the model."""
    for strike in (23_000, 23_800, 24_000, 24_500):
        call = price(F, strike, T, V, R, CALL)
        put = price(F, strike, T, V, R, PUT)
        assert call - put == pytest.approx(math.exp(-R * T) * (F - strike), abs=1e-6)


def test_atm_premium_matches_the_closed_form_approximation():
    """ATM Black-76 is very close to 0.3989 * F * vol * sqrt(T), discounted."""
    approx = math.exp(-R * T) * 0.3989 * F * V * math.sqrt(T)
    assert price(F, F, T, V, R, CALL) == pytest.approx(approx, rel=0.01)


def test_premium_rises_with_volatility_and_time():
    assert price(F, K, T, 0.20, R, CALL) > price(F, K, T, 0.10, R, CALL)
    assert price(F, K, 30 / 365, V, R, CALL) > price(F, K, T, V, R, CALL)


def test_deep_otm_is_nearly_worthless_and_deep_itm_is_intrinsic():
    assert price(F, 30_000, T, V, R, CALL) < 1.0
    deep_itm = price(F, 20_000, T, V, R, CALL)
    assert deep_itm == pytest.approx(math.exp(-R * T) * (F - 20_000), rel=0.01)


def test_at_expiry_price_is_intrinsic():
    assert price(F, 23_800, 0.0, V, R, CALL) == pytest.approx(200.0)
    assert price(F, 23_800, 0.0, V, R, PUT) == pytest.approx(0.0)
    assert price(F, 24_200, 0.0, V, R, PUT) == pytest.approx(200.0)


def test_zero_vol_degenerates_without_raising():
    g = greeks(F, K, T, 0.0, R, CALL)
    assert g.gamma == 0.0 and g.vega == 0.0 and g.theta == 0.0


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        price(F, K, T, V, R, "XX")
    with pytest.raises(ValueError):
        price(-1, K, T, V, R, CALL)


# -------------------------------------------------------------------- greeks


def test_call_and_put_delta_have_the_right_signs_and_magnitudes():
    call = greeks(F, K, T, V, R, CALL)
    put = greeks(F, K, T, V, R, PUT)
    assert 0.0 < call.delta < 1.0
    assert -1.0 < put.delta < 0.0
    # Black-76 parity: delta_call - delta_put == discount factor.
    assert call.delta - put.delta == pytest.approx(math.exp(-R * T), abs=1e-9)


def test_atm_delta_is_about_half():
    assert greeks(F, F, T, V, R, CALL).delta == pytest.approx(0.5, abs=0.02)


def test_gamma_and_vega_are_shared_by_calls_and_puts():
    call, put = greeks(F, K, T, V, R, CALL), greeks(F, K, T, V, R, PUT)
    assert call.gamma == pytest.approx(put.gamma)
    assert call.vega == pytest.approx(put.vega)
    assert call.gamma > 0 and call.vega > 0


def test_theta_is_negative_for_a_long_option():
    """Long options decay, so the short side of the condor earns theta."""
    assert greeks(F, K, T, V, R, CALL).theta < 0
    assert greeks(F, K, T, V, R, PUT).theta < 0


def test_delta_matches_a_numeric_derivative():
    bump = 1.0
    numeric = (price(F + bump, K, T, V, R, CALL) - price(F - bump, K, T, V, R, CALL)) / (2 * bump)
    assert greeks(F, K, T, V, R, CALL).delta == pytest.approx(numeric, rel=1e-4)


def test_vega_matches_a_numeric_derivative():
    bump = 1e-4
    numeric = (price(F, K, T, V + bump, R, CALL) - price(F, K, T, V - bump, R, CALL)) / (2 * bump)
    assert greeks(F, K, T, V, R, CALL).vega == pytest.approx(numeric, rel=1e-4)


def test_vega_per_point_is_the_traders_convention():
    g = greeks(F, K, T, V, R, CALL)
    assert g.vega_per_point == pytest.approx(g.vega / 100.0)


# ---------------------------------------------------------------- implied vol


def test_implied_vol_recovers_the_input_volatility():
    for strike in (23_400, 23_800, 24_000, 24_400):
        for right in (CALL, PUT):
            premium = price(F, strike, T, 0.17, R, right)
            assert implied_vol(premium, F, strike, T, R, right) == pytest.approx(0.17, abs=1e-3)


def test_implied_vol_returns_none_for_impossible_quotes():
    assert implied_vol(0.0, F, K, T, R, CALL) is None
    assert implied_vol(100.0, F, K, 0.0, R, CALL) is None
    assert implied_vol(F * 2, F, K, T, R, CALL) is None


# ------------------------------------------------------------------ forwards


def test_forward_is_above_spot_for_a_positive_rate():
    assert forward_price(24_000, 0.065, 7 / 365) > 24_000
    assert forward_price(24_000, 0.0, 7 / 365) == pytest.approx(24_000)


def test_year_fraction_is_never_negative():
    assert year_fraction(-5) == 0.0
    assert year_fraction(365) == pytest.approx(1.0)


# --------------------------------------------------------------------- costs


def test_stt_applies_only_to_the_sell_side():
    model = CostModel()
    on = dt.date(2025, 6, 2)
    sell = model.breakdown(Side.SELL, 60.0, 75, on)
    buy = model.breakdown(Side.BUY, 60.0, 75, on)
    assert sell["stt"] == pytest.approx(0.001 * 60 * 75)
    assert buy["stt"] == 0.0


@pytest.mark.parametrize("on, stt, exchange", [
    (dt.date(2020, 6, 1), 0.0005, 0.0005),
    (dt.date(2022, 6, 1), 0.0005, 0.00053),
    (dt.date(2023, 6, 1), 0.000625, 0.0005),
    (dt.date(2024, 6, 1), 0.000625, 0.000495),
    (dt.date(2025, 6, 2), 0.001, 0.0003503),
    (dt.date(2026, 6, 1), 0.0015, 0.0003503),
])
def test_the_rates_are_the_ones_in_force_on_the_trade_date(on, stt, exchange):
    """A backtest over 2019-2026 spans four STT rates and five exchange charges;
    today's for every year misstated each one."""
    parts = CostModel().breakdown(Side.SELL, 100.0, 65, on)
    assert parts["stt"] == pytest.approx(stt * 6500)
    assert parts["exchange"] == pytest.approx(exchange * 6500)


def test_expiry_settlement_charges_exercise_stt_to_the_long_only():
    """The holder of an exercised option pays STT on its settlement value; the
    writer pays none, and a cash settlement places no order to charge."""
    model = CostModel()
    assert model.settlement_cost(Side.SELL, 100.0, 65, dt.date(2025, 6, 2)) == 0.0
    assert model.settlement_cost(Side.BUY, 0.0, 65, dt.date(2025, 6, 2)) == 0.0
    assert model.settlement_cost(Side.BUY, 100.0, 65, dt.date(2025, 6, 2)) == pytest.approx(0.00125 * 6500)
    assert model.settlement_cost(Side.BUY, 100.0, 65, dt.date(2026, 6, 2)) == pytest.approx(0.0015 * 6500)


def test_an_explicit_rate_overrides_the_schedule():
    assert CostModel(stt_sell_rate=0.002).breakdown(Side.SELL, 100.0, 1, dt.date(2020, 1, 1))["stt"] == pytest.approx(0.2)


def test_stamp_duty_applies_only_to_the_buy_side():
    model = CostModel()
    assert model.breakdown(Side.BUY, 60.0, 75)["stamp_duty"] > 0
    assert model.breakdown(Side.SELL, 60.0, 75)["stamp_duty"] == 0.0


def test_gst_is_charged_on_brokerage_txn_and_sebi_only():
    model = CostModel()
    items = model.breakdown(Side.SELL, 60.0, 75)
    expected = 0.18 * (items["brokerage"] + items["exchange"] + items["sebi"])
    assert items["gst"] == pytest.approx(expected)


def test_breakdown_total_equals_leg_cost():
    model = CostModel()
    for side in (Side.BUY, Side.SELL):
        assert model.breakdown(side, 60.0, 75)["total"] == pytest.approx(model.leg_cost(side, 60.0, 75))


def test_a_four_leg_condor_costs_a_meaningful_fraction_of_its_credit():
    """Sanity check that ignoring costs would not be harmless."""
    model = CostModel()
    prices = {(Side.SELL, 60.0), (Side.BUY, 25.0), (Side.SELL, 55.0), (Side.BUY, 20.0)}
    total = sum(model.leg_cost(side, px, 75) for side, px in prices)
    credit = (60 - 25 + 55 - 20) * 75
    assert 0.005 < total / credit < 0.25
    assert total > 4 * model.brokerage_per_order  # brokerage alone is not the whole story


def test_zero_cost_model_is_free():
    assert ZERO_COST.leg_cost(Side.SELL, 60.0, 75) == 0.0
