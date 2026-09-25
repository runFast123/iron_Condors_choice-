"""Implied volatility read from the exchange's closing prices.

The India VIX model prices every contract off one number and an assumed skew.
Measured against the exchange's own closing prices for January to September
2026, that put a typical out-of-the-money NIFTY leg 15% from where it traded,
and 13% too high on average -- flattering every modelled credit.

This reads the smile the market actually traded instead: every contract's
implied volatility at the previous session's close, from the exchange's daily
record, with the forward the market implied that day. Carried to a moment the
next day by the two things that moved since -- NIFTY and India VIX -- it priced
the same legs within 6% typically and without the bias. Only the previous
session's figures are read, so nothing here knows anything a trader at that
moment did not.

How the smile is built, and why:

* Only contracts that traded at least ``min_volume`` lots count. A contract
  nobody traded carries yesterday's close forward, and a thinly traded one's
  close can sit ten volatility points off its neighbours -- enough, on 24 March,
  to price a condor at 425 where the market paid 159.
* Out-of-the-money strikes on each side, both rights near the money. Their
  prices are the liquid ones.
* The forward from put-call parity on the most traded strikes -- what the
  options themselves assumed -- not spot compounded at a guessed rate. On 27
  March the guess sat 15 points above the market's forward three days from
  expiry, enough to cheapen every put by 5%.
* A quadratic in log-moneyness, fitted by weighted least squares with points
  more than four robust deviations out dropped, so no single print can bend
  it. A contract's own volatility is kept where it sits within two points of
  the fit: the fit is a shape, and the contract's own figure carries what is
  particular to that strike.

Carried forward, a strike keeps its own volatility plus the fitted smile's
change for how far its moneyness moved, then scales with India VIX.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field
from typing import Mapping

import numpy as np

from engine.pricing.black76 import implied_vol
from engine.pricing.iv_surface import MAX_IV, MIN_IV

#: Lots a contract must have traded on the day for its close to shape the smile.
MIN_VOLUME = 100
#: How far from the fitted smile a contract's own volatility may sit and still
#: be used as its own, in volatility (0.02 = two points).
MAX_OWN_DEVIATION = 0.02
#: Log-moneyness the fit reaches: about 12% either side of the forward, which
#: covers every strike a ladder or HIC trades.
BAND = 0.12
#: Fewer strikes than this and there is no smile worth fitting.
MIN_POINTS = 6
#: Strikes within this of spot are used for the put-call-parity forward.
PARITY_WINDOW = 300.0

SESSION_CLOSE = dt.time(15, 30)


def years_to_expiry(when: dt.datetime, expiry: dt.date) -> float:
    """Calendar years from `when` to the 15:30 close on expiry day."""
    close = dt.datetime.combine(expiry, SESSION_CLOSE, tzinfo=when.tzinfo)
    return max(0.0, (close - when).total_seconds() / 86_400.0) / 365.0


def _weighted_quadratic(xs, ys, ws) -> tuple[float, float, float]:
    x = np.asarray(xs, dtype=float)
    design = np.column_stack([np.ones_like(x), x, x * x])
    root = np.sqrt(np.asarray(ws, dtype=float))
    coef, *_ = np.linalg.lstsq(design * root[:, None], np.asarray(ys, dtype=float) * root, rcond=None)
    return float(coef[0]), float(coef[1]), float(coef[2])


@dataclass(frozen=True)
class ExchangeSmile:
    """One expiry's volatility smile as it closed on one day."""

    day: dt.date
    expiry: dt.date
    spot: float                    # NIFTY's official close that day
    forward: float                 # the options' own forward for this expiry
    years: float                   # time to expiry from that close
    coef: tuple[float, float, float]
    x_lo: float                    # log-moneyness range the fit covers
    x_hi: float
    own: Mapping[float, float] = field(default_factory=dict)
    vix: float | None = None       # India VIX at that close
    points: int = 0                # strikes the fit rests on
    forward_source: str = "parity"

    @property
    def carry(self) -> float:
        """Annualised carry the forward implied, spot to forward."""
        if self.years <= 0 or self.spot <= 0:
            return 0.0
        return math.log(self.forward / self.spot) / self.years

    def fitted(self, strike: float, forward: float | None = None) -> float:
        """The fitted smile at a strike, for a forward (the anchor's if None).

        Flat beyond the strikes it was fitted on: a quadratic extrapolated past
        its data is exactly the bad guess this module exists to replace.
        """
        x = math.log(strike / (forward or self.forward))
        x = min(self.x_hi, max(self.x_lo, x))
        a, b, c = self.coef
        return a + b * x + c * x * x

    def vol(self, strike: float, forward_now: float) -> float:
        """This strike's volatility carried to a new forward, before VIX.

        Its own figure where it has a sound one, else the fit's; plus the fit's
        change between where the strike sat in the smile then and now.
        """
        own = self.own.get(float(strike))
        base = own if own is not None else self.fitted(strike)
        return base + self.fitted(strike, forward_now) - self.fitted(strike)


def build_smile(
    chain,
    day: dt.date,
    expiry: dt.date,
    spot: float | None,
    *,
    future: float | None = None,
    vix: float | None = None,
    rate: float = 0.065,
    min_volume: int = MIN_VOLUME,
) -> ExchangeSmile | None:
    """The smile of `expiry` at `day`'s close, or None when it cannot be read.

    `chain` is that day's contracts for the expiry, as (strike, right,
    ContractDay) -- ChainHistory.chain. `spot` is NIFTY's official close.
    """
    if not spot or spot <= 0:
        return None
    years = years_to_expiry(dt.datetime.combine(day, SESSION_CLOSE), expiry)
    if years <= 0:
        return None
    liquid = {
        (float(k), r): c for k, r, c in chain
        if c.traded and c.volume >= min_volume and c.close > 0
    }
    if not liquid:
        return None

    parity = [
        k + (c.close - liquid[(k, "PE")].close) * math.exp(rate * years)
        for (k, r), c in liquid.items()
        if r == "CE" and abs(k - spot) <= PARITY_WINDOW and (k, "PE") in liquid
    ]
    if len(parity) >= 3:
        forward, source = statistics.median(parity), "parity"
    elif future:
        forward, source = float(future), "future"
    else:
        forward, source = spot * math.exp(rate * years), "carry"

    points: dict[float, tuple[float, float]] = {}
    for (k, r), c in liquid.items():
        out_of_money = (r == "PE" and k <= forward + 100) or (r == "CE" and k >= forward - 100)
        if not out_of_money or abs(math.log(k / forward)) > BAND or c.close < 0.5:
            continue
        iv = implied_vol(c.close, forward, k, years, rate, r)
        if iv is None or not (MIN_IV < iv < MAX_IV):
            continue
        # Near the money both rights price the same volatility; weight by lots.
        if k in points:
            v0, w0 = points[k]
            points[k] = ((v0 * w0 + iv * c.volume) / (w0 + c.volume), w0 + c.volume)
        else:
            points[k] = (iv, float(c.volume))
    if len(points) < MIN_POINTS:
        return None

    strikes = sorted(points)
    xs = [math.log(k / forward) for k in strikes]
    ys = [points[k][0] for k in strikes]
    ws = [math.log1p(points[k][1]) for k in strikes]
    keep = list(range(len(strikes)))
    coef = _weighted_quadratic(xs, ys, ws)
    for _ in range(3):
        residual = [ys[i] - (coef[0] + coef[1] * xs[i] + coef[2] * xs[i] ** 2) for i in range(len(strikes))]
        spread = statistics.median(abs(residual[i]) for i in keep)
        limit = max(4.0 * 1.4826 * spread, 0.01)
        kept = [i for i in range(len(strikes)) if abs(residual[i]) <= limit]
        if len(kept) < MIN_POINTS or kept == keep:
            break
        keep = kept
        coef = _weighted_quadratic([xs[i] for i in keep], [ys[i] for i in keep], [ws[i] for i in keep])

    own = {}
    for i in keep:
        fit = coef[0] + coef[1] * xs[i] + coef[2] * xs[i] ** 2
        if abs(ys[i] - fit) <= MAX_OWN_DEVIATION:
            own[strikes[i]] = ys[i]
    used = [xs[i] for i in keep]
    return ExchangeSmile(
        day=day, expiry=expiry, spot=float(spot), forward=float(forward), years=years,
        coef=coef, x_lo=min(used), x_hi=max(used), own=own, vix=vix,
        points=len(keep), forward_source=source,
    )


def carried_vol(
    smile: ExchangeSmile, strike: float, forward_now: float, vix_now: float | None
) -> float:
    """The volatility to price `strike` at, now: the smile carried to today's
    forward and scaled by how far India VIX has moved since its close."""
    vol = smile.vol(strike, forward_now)
    if vix_now and smile.vix:
        vol *= vix_now / smile.vix
    return min(MAX_IV, max(MIN_IV, vol))
