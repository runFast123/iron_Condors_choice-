"""Tests for fitting the IV surface to Choice's live chain.

The strongest check available: build a synthetic market whose option prices are
generated from a *known* surface, then confirm the calibrator recovers that
surface from the prices alone. If it can invert its own model it is doing
arithmetic, not curve-drawing.
"""

from __future__ import annotations

import datetime as dt

import pytest

from engine.choice.errors import ChoiceError, ChoiceInstrumentError
from engine.choice.instruments import Contract
from engine.data.market import Quote
from engine.pricing.black76 import forward_price, price, year_fraction
from engine.pricing.calibrate import (
    MAX_TERM_EXPONENT,
    MIN_TERM_EXPONENT,
    calibrate,
    fit_term_exponent,
)
from engine.pricing.iv_surface import VIX_TENOR_DAYS, IVSurface

LOT = 65
SPOT = 24_000.0
RATE = 0.065
TODAY = dt.date.today()


class SyntheticMaster:
    def __init__(self, expiries, step=50.0, missing=frozenset()):
        self._expiries = list(expiries)
        self._step = step
        self._missing = missing
        self._tokens: dict[tuple, int] = {}

    def index(self, name):
        return Contract(token=26000, segment_id=1, symbol="NIFTY",
                        description="Nifty 50", lot_size=LOT)

    def expiries(self, underlying, *, after=None):
        return [e for e in self._expiries if after is None or e >= after]

    def strike_step(self, underlying, expiry):
        return self._step

    def option(self, underlying, expiry, strike, right):
        if (expiry, strike, right) in self._missing:
            raise ChoiceInstrumentError("delisted")
        key = (expiry, strike, right)
        self._tokens.setdefault(key, 700_000 + len(self._tokens))
        return Contract(token=self._tokens[key], segment_id=2, symbol="NIFTY",
                        description="", lot_size=LOT, expiry=expiry, strike=strike,
                        option_type=right, underlying="NIFTY")


class SyntheticMarket:
    """Prices every option off `truth`, so the fit has a right answer."""

    def __init__(self, truth: IVSurface, expiries, spot=SPOT, step=50.0,
                 missing=frozenset(), no_quotes=False):
        self.truth = truth
        self.master = SyntheticMaster(expiries, step=step, missing=missing)
        self.spot = spot
        self.no_quotes = no_quotes
        self.session = None
        self.fallback_requests = 0

    def quotes(self, contracts, *, allow_history_fallback=True):
        if allow_history_fallback:
            self.fallback_requests += 1
        if self.no_quotes:
            raise ChoiceError("MultipleTouchline returned no usable quotes")
        out = {}
        for c in contracts:
            if c.token == 26000:
                out[c.token] = Quote(token=c.token, ltp=self.spot)
                continue
            days = (c.expiry - TODAY).days + 0.5
            years = year_fraction(days)
            fwd = forward_price(self.spot, RATE, years)
            vol = self.truth.vol(fwd, c.strike, days)
            px = price(fwd, c.strike, years, vol, RATE, c.option_type)
            if px <= 0:
                continue
            out[c.token] = Quote(token=c.token, ltp=px, bid=px * 0.99, ask=px * 1.01)
        return out


def weekly_expiries(n=5):
    return [TODAY + dt.timedelta(days=7 * i + 1) for i in range(n)]


# ============================================== the term structure on its own


@pytest.mark.parametrize("true_beta", [-0.40, -0.25, -0.10, 0.0])
def test_the_term_exponent_is_recovered_from_a_known_power_law(true_beta):
    atm = {d: 0.14 * (d / VIX_TENOR_DAYS) ** true_beta for d in (1.5, 8.5, 15.5, 22.5, 29.5)}
    assert fit_term_exponent(atm) == pytest.approx(true_beta, abs=1e-6)


def test_one_tenor_cannot_produce_a_slope():
    """A fabricated term structure is worse than an honest flat one."""
    assert fit_term_exponent({7.0: 0.18}) is None
    assert fit_term_exponent({}) is None


def test_an_implausible_slope_is_rejected():
    assert fit_term_exponent({1.0: 0.03, 30.0: 2.4}) is None


def test_the_accepted_range_brackets_real_market_shapes():
    assert MIN_TERM_EXPONENT < -0.40 and MAX_TERM_EXPONENT > 0.0


# ================================================== the full round trip


def test_calibration_recovers_the_surface_that_generated_the_prices():
    """The whole point: skew and term structure, inverted out of premiums."""
    truth = IVSurface(atm_vol=0.15, slope=-2.4, curvature=55.0, term_exponent=-0.30)
    market = SyntheticMarket(truth, weekly_expiries())

    result = calibrate(market, rate=RATE)  # type: ignore[arg-type]
    assert result is not None
    got = result.surface

    assert got.term_exponent == pytest.approx(truth.term_exponent, abs=0.03)
    assert got.atm_vol == pytest.approx(truth.atm_vol, rel=0.06)
    assert got.slope == pytest.approx(truth.slope, abs=0.8)
    assert got.fitted_from >= 8
    assert result.observations >= 8
    assert len(result.tenors) >= 4


def test_a_recovered_surface_reprices_the_chain_it_was_fitted_to():
    """Closer to what actually matters than any single parameter matching."""
    truth = IVSurface(atm_vol=0.15, slope=-2.4, curvature=55.0, term_exponent=-0.30)
    market = SyntheticMarket(truth, weekly_expiries())
    got = calibrate(market, rate=RATE).surface  # type: ignore[arg-type,union-attr]

    for days in (1.5, 8.5, 22.5):
        fwd = forward_price(SPOT, RATE, year_fraction(days))
        for strike in (23_600.0, 23_800.0, 24_000.0, 24_200.0, 24_400.0):
            right = "PE" if strike <= 24_000 else "CE"
            years = year_fraction(days)
            want = price(fwd, strike, years, truth.vol(fwd, strike, days), RATE, right)
            mine = price(fwd, strike, years, got.vol(fwd, strike, days), RATE, right)
            if want > 5.0:                      # a rupee option is all noise
                assert mine == pytest.approx(want, rel=0.30), (
                    f"{strike:g}{right} at {days}d: fitted {mine:.2f} vs true {want:.2f}"
                )


def test_a_flat_market_calibrates_to_a_flat_term_structure():
    truth = IVSurface(atm_vol=0.16, slope=-3.0, curvature=40.0, term_exponent=0.0)
    got = calibrate(SyntheticMarket(truth, weekly_expiries()), rate=RATE)  # type: ignore[arg-type]
    assert got is not None
    assert got.surface.term_exponent == pytest.approx(0.0, abs=0.03)


# ===================================================== refusing to guess


def test_no_quotes_means_no_calibration_rather_than_a_default():
    truth = IVSurface(atm_vol=0.15)
    got = calibrate(SyntheticMarket(truth, weekly_expiries(), no_quotes=True), rate=RATE)  # type: ignore[arg-type]
    assert got is None


def test_a_single_expiry_still_fits_a_skew_but_no_term_slope():
    truth = IVSurface(atm_vol=0.15, slope=-2.4, curvature=55.0, term_exponent=-0.30)
    got = calibrate(SyntheticMarket(truth, weekly_expiries(1)), rate=RATE)  # type: ignore[arg-type]
    assert got is not None
    assert got.surface.term_exponent == 0.0, "one tenor cannot imply a slope"
    assert got.surface.slope == pytest.approx(truth.slope, abs=0.8)


def test_delisted_strikes_are_skipped_without_failing_the_fit():
    expiries = weekly_expiries()
    missing = {(expiries[0], 24_000.0 + i * 50.0, "CE") for i in range(1, 6)}
    truth = IVSurface(atm_vol=0.15, slope=-2.4, curvature=55.0, term_exponent=-0.30)
    got = calibrate(SyntheticMarket(truth, expiries, missing=missing), rate=RATE)  # type: ignore[arg-type]
    assert got is not None
    assert got.observations >= 8


def test_calibration_never_falls_back_to_a_stale_candle():
    """A surface describes one instant; mixing in yesterday's close would make
    the fit describe no moment that ever existed."""
    truth = IVSurface(atm_vol=0.15, term_exponent=-0.25)
    market = SyntheticMarket(truth, weekly_expiries())
    calibrate(market, rate=RATE)  # type: ignore[arg-type]
    # Only the spot lookup may use the fallback; every chain request must not.
    assert market.fallback_requests <= 1


def test_the_summary_is_json_safe_and_says_what_it_rests_on():
    import json
    import math

    truth = IVSurface(atm_vol=0.15, slope=-2.4, curvature=55.0, term_exponent=-0.30)
    result = calibrate(SyntheticMarket(truth, weekly_expiries()), rate=RATE)  # type: ignore[arg-type]
    assert result is not None
    summary = result.summary()
    text = json.dumps(summary, allow_nan=False)
    assert "NaN" not in text and "Infinity" not in text
    assert summary["observations"] >= 8
    assert len(summary["atm_by_tenor"]) >= 4
    assert all(math.isfinite(v) for v in result.atm_by_tenor.values())
