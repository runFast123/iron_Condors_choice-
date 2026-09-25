"""Option price sources for the backtester.

Four implementations behind one interface, so the runner never has to know
where a premium came from — but every quote it returns carries its
:class:`PriceSource`, and that tag follows the fill all the way into the UI.

* :class:`CandlePriceProvider` — real option candles: Choice's (the good
  case), or the backup source's for a contract Choice has no history for.
* :class:`ExchangeAnchoredPriceProvider` — for a moment neither has a candle
  for: the smile the market traded at the previous session's close, from the
  exchange's daily record, carried forward by NIFTY and India VIX.
* :class:`ModelPriceProvider`  — Black-76 from India VIX plus a skew, used
  only when there is no exchange record to anchor to either.
* :class:`FallbackPriceProvider` — Choice first, the backup second, the model
  last, and a count of how often each answered.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

import pandas as pd

from engine.config import IST
from engine.data.nse_bhavcopy import ChainHistory, ContractDay
from engine.pricing.black76 import forward_price, greeks
from engine.pricing.exchange_smile import (
    SESSION_CLOSE,
    ExchangeSmile,
    TradingClock,
    build_smile,
    carried_vol,
    years_to_expiry,
)
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


# ------------------------------------------------------- exchange-anchored

#: From this time a contract that traded that day is priced at its real last
#: trade: the bar stamped 15:29 is the session's last, and its NIFTY close is
#: the 15:30 price the last trade belongs with.
LAST_TRADE_FROM = dt.time(15, 29)


@dataclass
class ExchangeAnchoredPriceProvider:
    """The model, anchored to the exchange's own prices for the same contract.

    For a moment on day D, for a contract Choice has no candle for:

    * from 15:29 -- or on a daily bar, which is the close -- a contract that
      traded on D in size (see ContractDay.liquid) is priced at its real
      closing trade from the exchange's record. Tagged EXCHANGE: a real price,
      not a model.
    * otherwise at the smile the market traded at the previous session's
      close, carried to this moment by NIFTY and India VIX. Only that
      session's figures are read (engine.pricing.exchange_smile). MODELED.
    * with no previous session to read -- a gap in the record, or an expiry
      not yet listed -- the India VIX model this wraps. MODELED.

    A modelled price for a contract that traded on D in size is then kept
    inside D's real low and high. Every price it traded at that day lies in
    that range, so the bound can only move a price towards the truth. Not for
    a contract that barely traded: one print hours old bounds nothing.

    Volatility is carried on a TradingClock -- sessions count a day, days the
    market is shut NON_TRADING_WEIGHT of one -- so Friday's smile is not
    applied to Monday as if the weekend had been two trading days.
    """

    history: ChainHistory
    fallback: ModelPriceProvider
    vix_at: Callable[[dt.datetime], float | None] | None = None
    rate: float = 0.065
    clamp: bool = True
    daily_bars: bool = False
    #: An anchor older than this says nothing about today.
    max_anchor_gap: dt.timedelta = dt.timedelta(days=7)
    anchored: int = 0
    vix_only: int = 0
    clamped: int = 0
    exchange_prices: int = 0
    _smiles: dict[tuple[dt.date, dt.date], ExchangeSmile | None] = field(default_factory=dict)
    clock: TradingClock | None = None

    def __post_init__(self) -> None:
        if self.clock is None:
            from engine.data.market_calendar import MarketCalendar

            calendar = MarketCalendar.load()
            days = self.history.days

            def is_session(day: dt.date) -> bool:
                # The record itself says which days traded wherever it reaches;
                # beyond it, the exchange's published calendar.
                if days and days[0] <= day <= days[-1]:
                    return self.history.has_day(day)
                return calendar.is_trading_day(day)

            self.clock = TradingClock(is_session)

    def smile_before(self, day: dt.date, expiry: dt.date) -> ExchangeSmile | None:
        """The smile `expiry` closed with in the last session before `day`."""
        anchor = self.history.previous_day(day)
        if anchor is None or day - anchor > self.max_anchor_gap:
            return None
        key = (anchor, expiry)
        if key not in self._smiles:
            vix = None
            if self.vix_at is not None:
                vix = self.vix_at(dt.datetime.combine(anchor, SESSION_CLOSE, tzinfo=IST))
            self._smiles[key] = build_smile(
                self.history.chain(anchor, expiry), anchor, expiry,
                self.history.underlying(anchor),
                future=self.history.future(anchor, expiry), vix=vix, rate=self.rate,
                clock=self.clock,
            )
        return self._smiles[key]

    def model_price(self, request: PriceRequest) -> tuple[float, float | None, float | None, bool] | None:
        """(price, iv, delta, anchored) from the models alone -- no day range,
        no closing trade. None when neither model can price the request."""
        smile = self.smile_before(request.when.date(), request.expiry)
        if smile is None:
            quote = self.fallback.quote(request)
            if quote is None:
                return None
            return quote.price, quote.iv, quote.delta, False
        years = years_to_expiry(request.when, request.expiry)
        forward = request.spot * math.exp(smile.carry * years)
        vix_now = self.vix_at(request.when) if self.vix_at is not None else None
        ratio_now = self.clock.ratio(request.when, request.expiry) if self.clock else None
        vol = carried_vol(smile, float(request.strike), forward, vix_now, ratio_now)
        g = greeks(forward, float(request.strike), years, vol, self.rate, request.right)
        return max(self.fallback.min_price, g.price), vol, g.delta, True

    def quote(self, request: PriceRequest) -> Quote | None:
        day = request.when.date()
        today: ContractDay | None = self.history.contract(
            day, request.expiry, float(request.strike), request.right
        )
        if today is not None and not today.liquid:
            today = None
        if today is not None:
            closing = today.close if self.daily_bars else today.last
            at_close = self.daily_bars or request.when.time() >= LAST_TRADE_FROM
            if at_close and closing:
                self.exchange_prices += 1
                return Quote(price=float(closing), source=PriceSource.EXCHANGE)

        priced = self.model_price(request)
        if priced is None:
            return None
        price, iv, delta, anchored = priced
        if anchored:
            self.anchored += 1
        else:
            self.vix_only += 1
        if self.clamp and today is not None and 0 < today.low <= today.high:
            bounded = min(max(price, today.low), today.high)
            if bounded != price:
                self.clamped += 1
                price = bounded
        return Quote(price=price, source=PriceSource.MODELED, iv=iv, delta=delta)

    def summary(self) -> dict[str, int]:
        return {
            "anchored_quotes": self.anchored,
            "vix_only_quotes": self.vix_only,
            "clamped_quotes": self.clamped,
            "exchange_quotes": self.exchange_prices,
            "smiles_read": sum(1 for s in self._smiles.values() if s is not None),
            "smiles_unreadable": sum(1 for s in self._smiles.values() if s is None),
        }

    def measure(
        self,
        legs: Iterable[tuple[dt.date, float, str, dt.date]],
        last_day: dt.date,
    ) -> dict | None:
        """How far the model sat from the exchange's close, on this run's legs.

        For every leg a run traded and every session it was held (entry day to
        the day before expiry), the model prices the leg at that day's close
        from the previous session alone -- exactly as it prices a bar -- and is
        compared with the close the exchange recorded. The India VIX model is
        scored on the same points, so the two can be read side by side.

        `legs` is (expiry, strike, right, first day needed). None when there
        was nothing to compare: no leg that traded on a day with an anchor.
        """
        anchored_errors: list[float] = []
        vix_errors: list[float] = []
        contracts: set[tuple[dt.date, float, str]] = set()
        for expiry, strike, right, first_day in legs:
            for day in self.history.days:
                if day < first_day or day >= expiry or day > last_day:
                    continue
                real = self.history.contract(day, expiry, float(strike), right)
                spot = self.history.underlying(day)
                if real is None or not real.liquid or not spot:
                    continue
                request = PriceRequest(
                    expiry=expiry, strike=float(strike), right=right,
                    when=dt.datetime.combine(day, SESSION_CLOSE, tzinfo=IST), spot=spot,
                )
                priced = self.model_price(request)
                if priced is None or not priced[3]:
                    continue
                plain = self.fallback.quote(request)
                if plain is None:
                    continue
                anchored_errors.append(priced[0] / real.close - 1.0)
                vix_errors.append(plain.price / real.close - 1.0)
                contracts.add((expiry, float(strike), right))
        if not anchored_errors:
            return None
        return {
            "contracts": len(contracts),
            "checks": len(anchored_errors),
            "anchored": _error_stats(anchored_errors),
            "vix_model": _error_stats(vix_errors),
        }


def _error_stats(errors: list[float]) -> dict[str, float]:
    """Typical, average and 90th-percentile size of relative errors, and their
    mean (the bias: positive means the model priced too high)."""
    sizes = sorted(abs(e) for e in errors)
    middle = len(sizes) // 2
    median = sizes[middle] if len(sizes) % 2 else 0.5 * (sizes[middle - 1] + sizes[middle])
    return {
        "median_abs": round(median, 4),
        "mean_abs": round(sum(sizes) / len(sizes), 4),
        "p90_abs": round(sizes[int(0.9 * (len(sizes) - 1))], 4),
        "bias": round(sum(errors) / len(errors), 4),
    }


# ---------------------------------------------------------------- fallback


@dataclass
class FallbackPriceProvider:
    """Real data where it exists, modeled where it does not.

    Choice first (`primary`), then the backup source's candles (`secondary`,
    optional), then the fallback -- the model, or the exchange-anchored
    provider, which can itself answer with a real closing trade. Keeps a count
    of each so the dashboard can state plainly what fraction of a backtest
    rests on real premiums and where they came from.
    """

    primary: PriceProvider
    fallback: PriceProvider
    secondary: PriceProvider | None = None
    choice_quotes: int = 0
    backup_quotes: int = 0
    exchange_quotes: int = 0
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
            # Counted by what the quote is, not by which slot answered: the
            # exchange-anchored fallback prices a closing bar at a real trade.
            if modeled.source is PriceSource.EXCHANGE:
                self.exchange_quotes += 1
            else:
                self.modeled_quotes += 1
        return modeled

    @property
    def real_quotes(self) -> int:
        """Quotes from a real traded price, whichever source served it."""
        return self.choice_quotes + self.backup_quotes + self.exchange_quotes

    @property
    def total_quotes(self) -> int:
        return self.real_quotes + self.modeled_quotes

    @property
    def real_fraction(self) -> float:
        return self.real_quotes / self.total_quotes if self.total_quotes else 0.0

    def summary(self) -> dict[str, float | int]:
        out: dict[str, float | int] = {
            "real_quotes": self.real_quotes,
            "choice_quotes": self.choice_quotes,
            "backup_quotes": self.backup_quotes,
            "exchange_quotes": self.exchange_quotes,
            "modeled_quotes": self.modeled_quotes,
            "total_quotes": self.total_quotes,
            "real_fraction": self.real_fraction,
            "backup_fraction": self.backup_quotes / self.total_quotes if self.total_quotes else 0.0,
            "exchange_fraction": self.exchange_quotes / self.total_quotes if self.total_quotes else 0.0,
        }
        detail = getattr(self.fallback, "summary", None)
        if callable(detail):
            anchored = detail()
            out["anchored_quotes"] = int(anchored.get("anchored_quotes", 0))
            out["vix_only_quotes"] = int(anchored.get("vix_only_quotes", 0))
            out["clamped_quotes"] = int(anchored.get("clamped_quotes", 0))
            out["anchored_fraction"] = (
                out["anchored_quotes"] / self.modeled_quotes if self.modeled_quotes else 0.0
            )
        return out
