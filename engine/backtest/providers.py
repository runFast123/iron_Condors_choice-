"""Option price sources for the backtester.

Three implementations behind one interface, so the runner never has to know
where a premium came from — but every quote it returns carries its
:class:`PriceSource`, and that tag follows the fill all the way into the UI.

* :class:`CandlePriceProvider` — real Choice option candles (the good case).
* :class:`ModelPriceProvider`  — Black-76 from India VIX plus a skew, used
  only when Choice has no data for that leg.
* :class:`FallbackPriceProvider` — tries real data first, models second, and
  records how often it had to fall back.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Callable, Protocol

import pandas as pd

from engine.pricing.black76 import forward_price, greeks
from engine.pricing.iv_surface import IVSurface
from engine.strategy.condor import PriceSource

CALL, PUT = "CE", "PE"


@dataclass(frozen=True)
class PriceRequest:
    expiry: dt.date
    strike: float
    right: str
    when: dt.datetime
    spot: float

    @property
    def days_to_expiry(self) -> float:
        """Calendar days remaining, floored at zero on expiry day itself."""
        expiry_close = dt.datetime.combine(self.expiry, dt.time(15, 30), tzinfo=self.when.tzinfo)
        return max(0.0, (expiry_close - self.when).total_seconds() / 86_400.0)


@dataclass(frozen=True)
class Quote:
    price: float
    source: PriceSource
    iv: float | None = None
    delta: float | None = None


class PriceProvider(Protocol):
    def quote(self, request: PriceRequest) -> Quote | None: ...


# ------------------------------------------------------------ real candles


@dataclass
class CandlePriceProvider:
    """Serves premiums from Choice option candles held in memory.

    ``candles`` maps ``(expiry, strike, right)`` to a frame with ``ts`` and
    ``close`` columns, as returned by :class:`engine.choice.history.HistoryClient`.
    """

    candles: dict[tuple[dt.date, float, str], pd.DataFrame] = field(default_factory=dict)
    max_staleness: dt.timedelta = dt.timedelta(minutes=15)
    hits: int = 0
    misses: int = 0

    def add(self, expiry: dt.date, strike: float, right: str, frame: pd.DataFrame) -> None:
        if frame is None or frame.empty:
            return
        ordered = frame.sort_values("ts").reset_index(drop=True)
        self.candles[(expiry, float(strike), right)] = ordered

    def quote(self, request: PriceRequest) -> Quote | None:
        frame = self.candles.get((request.expiry, float(request.strike), request.right))
        if frame is None or frame.empty:
            self.misses += 1
            return None

        # As-of lookup: the last bar at or before the requested moment. Using a
        # later bar would leak future information into the fill.
        timestamps = frame["ts"]
        eligible = frame[timestamps <= request.when]
        if eligible.empty:
            self.misses += 1
            return None

        row = eligible.iloc[-1]
        if request.when - row["ts"] > self.max_staleness:
            # A stale print is worse than an honest miss: it silently marks the
            # book at a price that no longer existed.
            self.misses += 1
            return None

        price = float(row["close"])
        if price <= 0:
            self.misses += 1
            return None
        self.hits += 1
        return Quote(price=price, source=PriceSource.CHOICE)


# ------------------------------------------------------------------- model


@dataclass
class ModelPriceProvider:
    """Black-76 premiums driven by India VIX and a strike skew."""

    surface: IVSurface
    rate: float = 0.065
    vix_by_date: dict[dt.date, float] | Callable[[dt.date], float | None] | None = None
    min_price: float = 0.05          # NIFTY options do not trade below 5 paise
    calls: int = 0

    def _surface_for(self, day: dt.date) -> IVSurface:
        if self.vix_by_date is None:
            return self.surface
        vix = self.vix_by_date(day) if callable(self.vix_by_date) else self.vix_by_date.get(day)
        if vix is None:
            return self.surface
        return self.surface.with_atm(vix / 100.0 if vix > 1.0 else float(vix))

    def quote(self, request: PriceRequest) -> Quote | None:
        days = request.days_to_expiry
        years = days / 365.0
        surface = self._surface_for(request.when.date())
        forward = forward_price(request.spot, self.rate, years)
        vol = surface.vol(forward, request.strike, days)
        g = greeks(forward, request.strike, years, vol, self.rate, request.right)
        self.calls += 1
        return Quote(
            price=max(self.min_price, g.price),
            source=PriceSource.MODELED,
            iv=vol,
            delta=g.delta,
        )


# ---------------------------------------------------------------- fallback


@dataclass
class FallbackPriceProvider:
    """Real data where it exists, modeled where it does not.

    Keeps a count of each so the dashboard can state plainly what fraction of
    a backtest rests on real premiums.
    """

    primary: PriceProvider
    fallback: PriceProvider
    real_quotes: int = 0
    modeled_quotes: int = 0

    def quote(self, request: PriceRequest) -> Quote | None:
        found = self.primary.quote(request)
        if found is not None:
            self.real_quotes += 1
            return found
        modeled = self.fallback.quote(request)
        if modeled is not None:
            self.modeled_quotes += 1
        return modeled

    @property
    def total_quotes(self) -> int:
        return self.real_quotes + self.modeled_quotes

    @property
    def real_fraction(self) -> float:
        return self.real_quotes / self.total_quotes if self.total_quotes else 0.0

    def summary(self) -> dict[str, float | int]:
        return {
            "real_quotes": self.real_quotes,
            "modeled_quotes": self.modeled_quotes,
            "total_quotes": self.total_quotes,
            "real_fraction": self.real_fraction,
        }
