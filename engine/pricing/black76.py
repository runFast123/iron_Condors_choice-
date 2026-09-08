"""Black-76 option pricing and greeks.

Used as the *fallback* premium source when Choice cannot serve historical
option candles for a leg.  Choice is the only permitted data source, so when it
has no premium for a contract there is no other vendor to ask — only a model.

Black-76 (rather than Black-Scholes on spot) is the right form here because
NIFTY options are settled against the index and quoted off the forward.
Everything produced through this module is tagged ``PriceSource.MODELED`` and
is excluded from the "verified" statistics in the dashboard.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

CALL, PUT = "CE", "PE"

SQRT_2PI = math.sqrt(2.0 * math.pi)
TRADING_DAYS = 252
CALENDAR_DAYS = 365.0


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function (exact to double precision)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    vega: float       # per 1.00 (100%) change in vol
    theta: float      # per calendar day
    rho: float

    @property
    def vega_per_point(self) -> float:
        """Vega per 1 volatility *point* (1%), which is how traders read it."""
        return self.vega / 100.0


def forward_price(spot: float, rate: float, years: float, dividend_yield: float = 0.0) -> float:
    """Forward for a broad index: continuous carry, no discrete dividends."""
    return spot * math.exp((rate - dividend_yield) * years)


def year_fraction(days: float) -> float:
    return max(days, 0.0) / CALENDAR_DAYS


def price(
    forward: float,
    strike: float,
    years: float,
    vol: float,
    rate: float = 0.065,
    right: str = CALL,
) -> float:
    """Black-76 premium. Degenerates to intrinsic at expiry or zero vol."""
    return greeks(forward, strike, years, vol, rate, right).price


def greeks(
    forward: float,
    strike: float,
    years: float,
    vol: float,
    rate: float = 0.065,
    right: str = CALL,
) -> Greeks:
    """Full Black-76 valuation.

    Handles the degenerate corners explicitly rather than letting ``log`` or a
    zero denominator raise: at expiry, or with no volatility, the option is
    worth its discounted intrinsic value and has no gamma, vega or theta.
    """
    right = right.upper()
    if right not in (CALL, PUT):
        raise ValueError(f"right must be {CALL!r} or {PUT!r}, got {right!r}")
    if forward <= 0 or strike <= 0:
        raise ValueError("forward and strike must be positive")

    discount = math.exp(-rate * max(years, 0.0))

    if years <= 0 or vol <= 0:
        intrinsic = max(0.0, forward - strike) if right == CALL else max(0.0, strike - forward)
        sign = 1.0 if right == CALL else -1.0
        in_the_money = (forward > strike) if right == CALL else (forward < strike)
        return Greeks(
            price=discount * intrinsic,
            delta=sign * discount if in_the_money else 0.0,
            gamma=0.0,
            vega=0.0,
            theta=0.0,
            rho=0.0,
        )

    sqrt_t = math.sqrt(years)
    d1 = (math.log(forward / strike) + 0.5 * vol * vol * years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t

    if right == CALL:
        premium = discount * (forward * _norm_cdf(d1) - strike * _norm_cdf(d2))
        delta = discount * _norm_cdf(d1)
        rho = -years * premium
    else:
        premium = discount * (strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1))
        delta = -discount * _norm_cdf(-d1)
        rho = -years * premium

    gamma = discount * _norm_pdf(d1) / (forward * vol * sqrt_t)
    vega = discount * forward * _norm_pdf(d1) * sqrt_t

    # Theta per calendar day: the decay term plus the drift of the discount
    # factor (Black-76 discounts the whole payoff, so -r*premium applies to
    # both rights).
    theta_annual = -discount * forward * _norm_pdf(d1) * vol / (2.0 * sqrt_t) - rate * premium

    return Greeks(
        price=max(premium, 0.0),
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta=theta_annual / CALENDAR_DAYS,
        rho=rho,
    )


def implied_vol(
    target: float,
    forward: float,
    strike: float,
    years: float,
    rate: float = 0.065,
    right: str = CALL,
    *,
    tol: float = 1e-6,
    max_iter: int = 100,
) -> float | None:
    """Back out volatility from a traded premium by bisection.

    Bisection rather than Newton: vega collapses for deep out-of-the-money
    weeklies, and a Newton step there diverges. Robustness matters more than
    the handful of iterations we save.
    """
    if years <= 0 or target <= 0:
        return None
    intrinsic = max(0.0, forward - strike) if right.upper() == CALL else max(0.0, strike - forward)
    if target < intrinsic * math.exp(-rate * years) - tol:
        return None  # arbitrage-violating quote

    low, high = 1e-6, 6.0
    if price(forward, strike, years, high, rate, right) < target:
        return None  # unattainable even at 600% vol

    for _ in range(max_iter):
        mid = 0.5 * (low + high)
        value = price(forward, strike, years, mid, rate, right)
        if abs(value - target) < tol:
            return mid
        if value < target:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)
