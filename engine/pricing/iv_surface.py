"""Implied-volatility surface for modeled premiums.

When Choice cannot serve historical candles for an option leg there is no
alternative *market* source — Choice is the only permitted vendor — so the
premium has to be modeled.  This module supplies the volatility that Black-76
needs, built from two things Choice does provide:

* **India VIX** (the ``INDIAVIX`` index, via Choice ChartData) for the
  at-the-money level, and
* a **strike skew**, fitted from whatever real Choice chain snapshots exist
  and falling back to a documented default NIFTY smile when none do.

Index options carry a pronounced put skew: downside strikes trade at higher
implied vol than equidistant upside strikes.  Ignoring it would systematically
under-price exactly the short puts this ladder sells, flattering the backtest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Sequence

# Default NIFTY smile, in normalised moneyness m = K/F - 1.
#   iv(m) = atm * (1 + slope*m + curvature*m^2)
# Slope is negative so that puts (m < 0) price above ATM.
DEFAULT_SLOPE = -3.0
DEFAULT_CURVATURE = 40.0

MIN_IV, MAX_IV = 0.03, 2.5

# Used only when there is nothing to fit and no level was supplied.
DEFAULT_ATM_VOL = 0.14

# India VIX is a 30-day measure. Shorter tenors trade richer, longer flatter.
VIX_TENOR_DAYS = 30.0


@dataclass(frozen=True)
class IVSurface:
    """A one-parameter-per-effect volatility surface.

    Deliberately simple and inspectable: three numbers a reader can sanity
    check, rather than an opaque fit that hides a bad extrapolation.
    """

    atm_vol: float
    slope: float = DEFAULT_SLOPE
    curvature: float = DEFAULT_CURVATURE
    term_exponent: float = 0.0
    fitted_from: int = 0          # number of real observations behind the fit

    @property
    def is_fitted(self) -> bool:
        return self.fitted_from > 0

    def atm_for_tenor(self, days: float) -> float:
        """Scale the 30-day ATM level to this tenor.

        ``term_exponent`` 0 means a flat term structure (use VIX as-is);
        a small negative value lifts short-dated vol, which is the usual
        shape for weeklies.
        """
        if self.term_exponent == 0.0 or days <= 0:
            return self.atm_vol
        return self.atm_vol * (max(days, 0.5) / VIX_TENOR_DAYS) ** self.term_exponent

    def vol(self, forward: float, strike: float, days: float) -> float:
        """Implied vol for one strike, clamped to a sane range."""
        if forward <= 0 or strike <= 0:
            return self.atm_vol
        m = strike / forward - 1.0
        atm = self.atm_for_tenor(days)
        smile = 1.0 + self.slope * m + self.curvature * m * m
        return min(MAX_IV, max(MIN_IV, atm * smile))

    def with_atm(self, atm_vol: float) -> "IVSurface":
        """Same shape, new level — the daily update from India VIX."""
        return replace(self, atm_vol=max(MIN_IV, min(MAX_IV, atm_vol)))


def from_vix(vix: float, **kw) -> IVSurface:
    """Build a surface from an India VIX reading (quoted in percent)."""
    if vix is None or not math.isfinite(vix) or vix <= 0:
        raise ValueError(f"Invalid India VIX value: {vix!r}")
    # VIX above 1.0 is being quoted in percent (typical range 10-25).
    atm = vix / 100.0 if vix > 1.0 else float(vix)
    return IVSurface(atm_vol=min(MAX_IV, max(MIN_IV, atm)), **kw)


@dataclass(frozen=True)
class VolPoint:
    """One observed (strike, implied vol) pair from a real chain snapshot."""

    forward: float
    strike: float
    days: float
    iv: float


def rms_error(surface: IVSurface, points: Sequence[VolPoint]) -> float:
    """How far a surface sits from the vols it claims to describe.

    Measured through `vol()` -- the way the surface is actually consumed --
    rather than against the fit's own objective. The two are not the same
    thing: the skew is solved on tenor-normalised points with a single level,
    while `vol()` applies `atm_for_tenor` on top, so a fit can minimise its own
    residual and still be wrong where it is used.
    """
    if not points:
        return float("inf")
    total = 0.0
    for p in points:
        total += (surface.vol(p.forward, p.strike, p.days) - p.iv) ** 2
    return math.sqrt(total / len(points))


def condor_credit(
    vol_at: "Callable[[float, str], float]",
    forward: float,
    days: float,
    short_offset: float,
    long_offset: float,
    rate: float = 0.065,
) -> float:
    """Per-share credit of the condor this platform trades, under some vols.

    The figure to judge a surface by. A condor's credit is a *difference* of
    four premiums, so equal-and-opposite errors on the sold and bought legs
    cancel in any per-leg score while destroying the thing the strategy
    actually earns. Measured on real Choice premiums, a fitted surface scored
    better than the default on RMS implied vol -- 0.016 against 0.035 -- and
    still priced the credit at 60 points where the market paid 110.
    """
    from engine.pricing.black76 import greeks     # local: avoids a cycle

    years = max(days, 0.5) / 365.0

    def px(strike: float, right: str) -> float:
        return greeks(forward, strike, years, vol_at(strike, right), rate, right).price

    return (
        px(forward - short_offset, "PE") + px(forward + short_offset, "CE")
        - px(forward - long_offset, "PE") - px(forward + long_offset, "CE")
    )


def credit_error(
    surface: IVSurface,
    points: Sequence[VolPoint],
    *,
    short_offset: float = 200.0,
    long_offset: float = 400.0,
) -> float | None:
    """Relative error in the condor credit this surface implies, against the
    credit the observed chain implies. None when the chain is too thin."""
    by_tenor: dict[float, list[VolPoint]] = {}
    for p in points:
        by_tenor.setdefault(p.days, []).append(p)

    errors: list[float] = []
    for days, group in by_tenor.items():
        forward = group[0].forward
        span = [p for p in group if abs(p.strike - forward) <= long_offset * 1.6]
        if len(span) < 4:
            continue

        def market_vol(strike: float, _right: str, _span=span) -> float:
            # Nearest observed strike. The chain is sampled every strike step,
            # so this is a short hop, and interpolating would invent a shape
            # between points that is exactly what is in question.
            return min(_span, key=lambda p: abs(p.strike - strike)).iv

        def model_vol(strike: float, _right: str, _d=days, _f=forward) -> float:
            return surface.vol(_f, strike, _d)

        real = condor_credit(market_vol, forward, days, short_offset, long_offset)
        model = condor_credit(model_vol, forward, days, short_offset, long_offset)
        if real > 0:
            errors.append(model / real - 1.0)

    if not errors:
        return None
    return sum(errors) / len(errors)


def better_of(fitted: IVSurface, points: Sequence[VolPoint]) -> IVSurface:
    """The fitted shape, or the default one if the fit prices worse.

    Judged on the condor credit rather than on per-leg implied vol, for the
    reason `condor_credit` gives: the credit is a difference, and a per-leg
    score cannot see an error that cancels within it.

    The old guard was `abs(slope) > 50 or abs(curvature) > 5000`, loose enough
    to admit a curvature of 923 where the default is 40.
    """
    if not points:
        return fitted
    plain = IVSurface(atm_vol=fitted.atm_vol, term_exponent=fitted.term_exponent)
    fit_err, plain_err = credit_error(fitted, points), credit_error(plain, points)
    if fit_err is None or plain_err is None:
        return fitted
    if abs(fit_err) <= abs(plain_err):
        return fitted
    return replace(plain, fitted_from=0)


def fit_skew(points: Sequence[VolPoint], *, atm_vol: float | None = None) -> IVSurface:
    """Least-squares fit of the smile to observed implied vols.

    Solves for slope and curvature in ``iv/atm = 1 + a*m + b*m^2`` by normal
    equations on a 2x2 system — small enough to do directly and keeps numpy
    out of the hot path.  Falls back to the documented default shape when the
    data is too thin or degenerate to support a fit.
    """
    usable = [
        p
        for p in points
        if p.forward > 0 and p.strike > 0 and p.iv and math.isfinite(p.iv) and MIN_IV <= p.iv <= MAX_IV
    ]
    def _level() -> float:
        """The at-the-money level, from the observation nearest the money."""
        nearest = min(usable, key=lambda p: abs(p.strike / p.forward - 1.0))
        return nearest.iv

    if len(usable) < 4:
        # `usable[0]` is whatever happened to come first in the input, which on
        # a chain ordered by strike is the furthest-out-of-the-money put -- the
        # single most skewed point in the set. Taking that as the ATM level and
        # then applying the skew on top double-counts it.
        if atm_vol is None:
            atm_vol = _level() if usable else DEFAULT_ATM_VOL
        return IVSurface(atm_vol=atm_vol if atm_vol > 0 else DEFAULT_ATM_VOL)

    if atm_vol is None:
        atm_vol = _level()
    # `atm_vol or ...` treated a caller-supplied 0.0 as "not supplied"; an
    # explicit zero is a bad level, not a missing one, and either way the fit
    # cannot proceed on it.
    if atm_vol <= 0:
        return IVSurface(atm_vol=DEFAULT_ATM_VOL)

    # Design matrix columns are m and m^2; target is iv/atm - 1.
    s11 = s12 = s22 = t1 = t2 = 0.0
    for p in usable:
        m = p.strike / p.forward - 1.0
        y = p.iv / atm_vol - 1.0
        m2 = m * m
        s11 += m2
        s12 += m * m2
        s22 += m2 * m2
        t1 += m * y
        t2 += m2 * y

    det = s11 * s22 - s12 * s12
    if abs(det) < 1e-18:
        return IVSurface(atm_vol=atm_vol, fitted_from=len(usable))

    slope = (t1 * s22 - t2 * s12) / det
    curvature = (s11 * t2 - s12 * t1) / det

    # Reject an implausible fit rather than letting it poison every premium.
    if not (math.isfinite(slope) and math.isfinite(curvature)) or abs(slope) > 50 or abs(curvature) > 5000:
        return IVSurface(atm_vol=atm_vol, fitted_from=len(usable))

    return IVSurface(atm_vol=atm_vol, slope=slope, curvature=curvature, fitted_from=len(usable))


def observations_from_chain(
    forward: float,
    days: float,
    quotes: Iterable[tuple[float, str, float]],
    rate: float = 0.065,
) -> list[VolPoint]:
    """Turn (strike, right, premium) quotes into implied-vol observations."""
    from engine.pricing.black76 import implied_vol  # local import avoids a cycle

    years = max(days, 0.0) / 365.0
    out: list[VolPoint] = []
    for strike, right, premium in quotes:
        iv = implied_vol(premium, forward, strike, years, rate, right)
        if iv is not None:
            out.append(VolPoint(forward=forward, strike=strike, days=days, iv=iv))
    return out
