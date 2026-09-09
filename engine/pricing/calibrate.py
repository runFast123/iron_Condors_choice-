"""Fit the IV surface to Choice's own live option chain.

Almost every option leg in a historical backtest is modelled, because the
scrip master lists only contracts that still exist: a weekly that expired last
month has been delisted, so there is no token to request candles for. That is
structural, not a gap to be papered over with a second vendor -- and no vendor
sells the missing thing anyway, since Yahoo and friends carry no NSE F&O
history.

What *is* available is the chain trading right now. Its shape -- how implied
vol varies with strike, and how it varies with tenor -- is the same shape the
model extrapolates backwards. Fitting to it replaces two guesses with two
measurements:

* the **skew**, which decides how much the short puts this ladder sells are
  worth relative to the calls, and
* the **term structure**, which decides what a 1-day option is worth relative
  to the 30-day India VIX. That one matters most: flat term structure prices a
  1 DTE condor at a quarter of what a realistic slope gives, and the whole P&L
  of a credit strategy is the credit.

Everything here uses Choice and only Choice.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from engine.choice.errors import ChoiceError
from engine.pricing.black76 import forward_price, year_fraction
from engine.pricing.iv_surface import (
    MAX_IV,
    MIN_IV,
    VIX_TENOR_DAYS,
    IVSurface,
    VolPoint,
    fit_skew,
    observations_from_chain,
)

if TYPE_CHECKING:  # pragma: no cover
    from engine.data.market import ChoiceMarketData

log = logging.getLogger(__name__)

# Strikes either side of the money to sample per expiry. Wide enough to see the
# skew the ladder actually trades (its wings sit 400 points out, eight strikes
# on a 50-point grid), narrow enough that the far tail -- where quotes are a
# tick wide and the implied vol is mostly noise -- does not dominate the fit.
STRIKES_EACH_SIDE = 10

# Expiries to sample. The term structure needs several tenors to have any
# slope to fit; one expiry can only ever produce a flat answer.
MAX_EXPIRIES = 5

# A term slope outside this is not a market shape, it is a bad fit.
MIN_TERM_EXPONENT, MAX_TERM_EXPONENT = -0.8, 0.2


@dataclass(frozen=True)
class Calibration:
    """A fitted surface plus the evidence behind it."""

    surface: IVSurface
    observations: int
    tenors: list[float]
    atm_by_tenor: dict[float, float]
    spot: float
    as_of: dt.datetime

    def summary(self) -> dict:
        return {
            "atm_vol": round(self.surface.atm_vol, 4),
            "slope": round(self.surface.slope, 3),
            "curvature": round(self.surface.curvature, 1),
            "term_exponent": round(self.surface.term_exponent, 4),
            "observations": self.observations,
            "tenors": [round(t, 1) for t in self.tenors],
            "atm_by_tenor": {str(round(k, 1)): round(v, 4) for k, v in self.atm_by_tenor.items()},
            "spot": round(self.spot, 2),
            "as_of": self.as_of.isoformat(),
        }


def fit_term_exponent(atm_by_tenor: dict[float, float]) -> float | None:
    """Fit ``atm(T) = atm_30 * (T/30)^beta`` by least squares in log-log space.

    Taking logs turns the power law into a straight line, so beta is an
    ordinary slope. Returns None when there is nothing to fit -- fewer than two
    distinct tenors, or a degenerate spread -- rather than inventing a number,
    because a fabricated term structure is worse than an honest flat one.
    """
    points = [
        (math.log(days / VIX_TENOR_DAYS), math.log(iv))
        for days, iv in atm_by_tenor.items()
        if days > 0 and MIN_IV <= iv <= MAX_IV
    ]
    if len({round(x, 6) for x, _ in points}) < 2:
        return None

    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    if sxx < 1e-12:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    beta = sxy / sxx

    if not math.isfinite(beta):
        return None
    if not (MIN_TERM_EXPONENT <= beta <= MAX_TERM_EXPONENT):
        log.warning("Term exponent %.3f is outside the plausible range; ignoring", beta)
        return None
    return beta


def _atm_level(points: list[VolPoint]) -> float | None:
    """The implied vol nearest the money for one tenor."""
    if not points:
        return None
    return min(points, key=lambda p: abs(p.strike / p.forward - 1.0)).iv


def calibrate(
    market: "ChoiceMarketData",
    underlying: str = "NIFTY",
    *,
    rate: float = 0.065,
    strikes_each_side: int = STRIKES_EACH_SIDE,
    max_expiries: int = MAX_EXPIRIES,
) -> Calibration | None:
    """Fit skew and term structure to the live chain. None if it cannot.

    Every failure here is survivable -- the caller keeps whatever surface it
    already had -- so this reports rather than raises.
    """
    now = dt.datetime.now(tz=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    try:
        index = market.master.index(underlying)
        spot_quote = market.quotes([index]).get(index.token)
    except ChoiceError as exc:
        log.warning("Cannot calibrate: no spot (%s)", exc)
        return None
    if spot_quote is None or spot_quote.ltp <= 0:
        log.warning("Cannot calibrate: no usable spot")
        return None
    spot = spot_quote.ltp

    try:
        expiries = market.master.expiries(underlying, after=now.date())[:max_expiries]
    except ChoiceError as exc:
        log.warning("Cannot calibrate: no expiries (%s)", exc)
        return None
    if not expiries:
        return None

    all_points: list[VolPoint] = []
    atm_by_tenor: dict[float, float] = {}

    for expiry in expiries:
        days = (expiry - now.date()).days + 0.5      # mid-session, not midnight
        if days <= 0:
            continue
        forward = forward_price(spot, rate, year_fraction(days))
        points = _sample_expiry(
            market, underlying, expiry, forward, days, rate, strikes_each_side
        )
        if len(points) < 4:
            log.info("Expiry %s gave only %d usable quotes; skipped", expiry, len(points))
            continue
        all_points.extend(points)
        level = _atm_level(points)
        if level is not None:
            atm_by_tenor[days] = level

    if len(all_points) < 8:
        log.warning("Only %d usable observations; not calibrating", len(all_points))
        return None

    # Fit the skew on tenor-normalised points.
    #
    # Pooling raw implied vols across tenors does not work: a 1-day point at
    # 30% and a 29-day point at 14% are describing the same *shape* at
    # different levels, and dividing both by one pooled level makes the short
    # tenor look like enormous skew. Measured on a synthetic chain, that
    # mispriced a 1 DTE 400-point-OTM put by 80%. Dividing each observation by
    # its own tenor's ATM level first leaves a pure skew multiplier, which is
    # the thing that is actually stable across expiries.
    reference = atm_by_tenor[min(atm_by_tenor)] if atm_by_tenor else None
    if reference and reference > 0:
        normalised = [
            replace(p, iv=p.iv / atm_by_tenor[p.days] * reference)
            for p in all_points
            if atm_by_tenor.get(p.days, 0.0) > 0
        ]
        surface = fit_skew(normalised or all_points, atm_vol=reference)
    else:
        surface = fit_skew(all_points)

    beta = fit_term_exponent(atm_by_tenor)
    if beta is not None:
        surface = replace(surface, term_exponent=beta)

    # The level is quoted at the 30-day tenor so `atm_for_tenor` can scale from
    # it; without this the fitted level would be whichever tenor happened to
    # sit nearest the money.
    if beta is not None and atm_by_tenor:
        nearest_days = min(atm_by_tenor)
        scale = (nearest_days / VIX_TENOR_DAYS) ** beta
        if scale > 0:
            surface = replace(surface, atm_vol=atm_by_tenor[nearest_days] / scale)

    log.info(
        "Calibrated from %d quotes across %d tenors: atm=%.1f%% slope=%.2f term=%s",
        len(all_points), len(atm_by_tenor), 100 * surface.atm_vol, surface.slope,
        f"{beta:+.3f}" if beta is not None else "flat",
    )
    return Calibration(
        surface=surface,
        observations=len(all_points),
        tenors=sorted(atm_by_tenor),
        atm_by_tenor=atm_by_tenor,
        spot=spot,
        as_of=now,
    )


def _sample_expiry(
    market: "ChoiceMarketData",
    underlying: str,
    expiry: dt.date,
    forward: float,
    days: float,
    rate: float,
    strikes_each_side: int,
) -> list[VolPoint]:
    """Implied vols for the strikes around the money on one expiry."""
    try:
        step = market.master.strike_step(underlying, expiry)
    except ChoiceError:
        return []
    if step <= 0:
        return []

    atm = round(forward / step) * step
    wanted: list[tuple[float, str]] = []
    for i in range(-strikes_each_side, strikes_each_side + 1):
        strike = atm + i * step
        if strike <= 0:
            continue
        # Out-of-the-money side only: an ITM premium is mostly intrinsic, so
        # its implied vol is a tiny number divided by a large one and the
        # inversion is numerically hopeless.
        wanted.append((strike, "PE" if strike <= atm else "CE"))

    contracts = []
    rights: dict[int, tuple[float, str]] = {}
    for strike, right in wanted:
        try:
            contract = market.master.option(underlying, expiry, strike, right)
        except ChoiceError:
            continue
        contracts.append(contract)
        rights[contract.token] = (strike, right)

    if not contracts:
        return []

    try:
        # The live book only. A fallback candle here would mix a stale price
        # into a snapshot fit, and a surface is only meaningful at one instant.
        quotes = market.quotes(contracts, allow_history_fallback=False)
    except ChoiceError as exc:
        log.info("No chain quotes for %s: %s", expiry, exc)
        return []

    chain: list[tuple[float, str, float]] = []
    for token, quote in quotes.items():
        if token not in rights:
            continue
        strike, right = rights[token]
        # Mid where there is a book, last trade otherwise. A one-sided touch
        # would bias every implied vol in the same direction.
        premium = quote.mid
        if premium and premium > 0:
            chain.append((strike, right, premium))

    return observations_from_chain(forward, days, chain, rate=rate)
