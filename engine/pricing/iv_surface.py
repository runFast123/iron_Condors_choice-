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
from typing import Iterable, Sequence

# Default NIFTY smile, in normalised moneyness m = K/F - 1.
#   iv(m) = atm * (1 + slope*m + curvature*m^2)
# Slope is negative so that puts (m < 0) price above ATM.
DEFAULT_SLOPE = -3.0
DEFAULT_CURVATURE = 40.0

MIN_IV, MAX_IV = 0.03, 2.5

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
    if len(usable) < 4:
        return IVSurface(atm_vol=atm_vol or (usable[0].iv if usable else 0.14))

    if atm_vol is None:
        # The observation closest to the money defines the level.
        nearest = min(usable, key=lambda p: abs(p.strike / p.forward - 1.0))
        atm_vol = nearest.iv
    if atm_vol <= 0:
        return IVSurface(atm_vol=0.14)

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
