"""Option price sources for the backtester.

Three implementations behind one interface, so the runner never has to know
where a premium came from — but every quote it returns carries its
:class:`PriceSource`, and that tag follows the fill all the way into the UI.

* :class:`CandlePriceProvider` — real option candles: Choice's (the good
  case), or the backup source's for a contract Choice has no history for.
* :class:`ModelPriceProvider`  — Black-76 from India VIX plus a skew, used
  only when neither has data for that moment.
* :class:`FallbackPriceProvider` — Choice first, the backup second, the model
  last, and a count of how often each answered.
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
    """Serves premiums from option candles held in memory.

    ``candles`` maps ``(expiry, strike, right)`` to a frame with ``ts`` and
    ``close`` columns, as returned by :class:`engine.choice.history.HistoryClient`.
    ``source`` is what every quote from this instance is tagged: Choice's
    candles and the backup's are kept in separate instances, so a price can
    never be credited to the wrong one.
    """

    candles: dict[tuple[dt.date, float, str], pd.DataFrame] = field(default_factory=dict)
    max_staleness: dt.timedelta = dt.timedelta(minutes=15)
    source: PriceSource = PriceSource.CHOICE
    hits: int = 0
    misses: int = 0
    # Per leg. A leg whose fetch returned bars is not a leg that was priced
    # from them: bars that never sit within `max_staleness` of a request are
    # fetched, counted and then never used, and the run falls back to the
    # model for every quote while the report says the data was real.
    hits_by_key: dict[tuple[dt.date, float, str], int] = field(default_factory=dict)

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
        key = (request.expiry, float(request.strike), request.right)
        self.hits_by_key[key] = self.hits_by_key.get(key, 0) + 1
        return Quote(price=price, source=self.source)


# ------------------------------------------------------------------- model


@dataclass
class ModelPriceProvider:
    """Black-76 premiums driven by India VIX and a strike skew.

    ``vix_at`` is the India VIX as it stood at a moment, and is preferred.
    ``vix_by_date`` is a day's close, kept for callers with no intraday series
    -- but a close is not known until 15:30, so pricing a 10:00 entry off it
    uses a number from five and a half hours in the future.
    """

    surface: IVSurface
    rate: float = 0.065
    vix_by_date: dict[dt.date, float] | Callable[[dt.date], float | None] | None = None
    min_price: float = 0.05          # NIFTY options do not trade below 5 paise
    calls: int = 0
    vix_at: Callable[[dt.datetime], float | None] | None = None

    def _surface_for(self, when: dt.datetime) -> IVSurface:
        if self.vix_at is not None:
            vix = self.vix_at(when)
        elif self.vix_by_date is None:
            return self.surface
        else:
            day = when.date()
            vix = self.vix_by_date(day) if callable(self.vix_by_date) else self.vix_by_date.get(day)
        if vix is None:
            return self.surface
        return self.surface.with_atm(vix / 100.0 if vix > 1.0 else float(vix))

    def quote(self, request: PriceRequest) -> Quote | None:
        days = request.days_to_expiry
        years = days / 365.0
        surface = self._surface_for(request.when)
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

    Choice first (`primary`), then the backup source's candles (`secondary`,
    optional), then the model. Keeps a count of each so the dashboard can
    state plainly what fraction of a backtest rests on real premiums and how
    much of that came from the backup.
    """

    primary: PriceProvider
    fallback: PriceProvider
    secondary: PriceProvider | None = None
    choice_quotes: int = 0
    backup_quotes: int = 0
    modeled_quotes: int = 0

    def quote(self, request: PriceRequest) -> Quote | None:
        found = self.primary.quote(request)
        if found is not None:
            self.choice_quotes += 1
            return found
        if self.secondary is not None:
            found = self.secondary.quote(request)
            if found is not None:
                self.backup_quotes += 1
                return found
        modeled = self.fallback.quote(request)
        if modeled is not None:
            self.modeled_quotes += 1
        return modeled

    @property
    def real_quotes(self) -> int:
        """Quotes from a real traded price, whichever source served it."""
        return self.choice_quotes + self.backup_quotes

    @property
    def total_quotes(self) -> int:
        return self.real_quotes + self.modeled_quotes

    @property
    def real_fraction(self) -> float:
        return self.real_quotes / self.total_quotes if self.total_quotes else 0.0

    def summary(self) -> dict[str, float | int]:
        return {
            "real_quotes": self.real_quotes,
            "choice_quotes": self.choice_quotes,
            "backup_quotes": self.backup_quotes,
            "modeled_quotes": self.modeled_quotes,
            "total_quotes": self.total_quotes,
            "real_fraction": self.real_fraction,
            "backup_fraction": self.backup_quotes / self.total_quotes if self.total_quotes else 0.0,
        }
