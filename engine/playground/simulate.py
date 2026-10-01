"""Plan a campaign: simulate where it can go from here.

Path model -- filtered historical simulation, the standard way risk desks
project a path-dependent book without assuming returns are normal:

* Every simulated session is a real NIFTY session of the last two years,
  drawn at random: its 15-minute bars, overnight gap included. That keeps
  NIFTY's own fat tails, its intraday swings and its gaps -- a normal model
  has none of them, and a ladder fires on exactly those.
* NIFTY and India VIX move together. A drawn session brings its own VIX
  change with it, so a sell-off day lifts the path's VIX the way it lifted
  the real one, and the next sessions are scaled to the path's VIX as it then
  stands: volatility clusters, as it does in the market.
* Each drawn session is rescaled from the India VIX it opened with to the
  path's VIX, so a calm July day drawn into a nervous October moves like an
  October day. Realised moves sit below implied on most days (about 85% here);
  that gap is kept, since it is what a premium seller earns.
* The history's trend is removed: two years of NIFTY drifting one way is not
  a forecast, and a short-horizon risk model assumes no drift.

On each path the run's own ladder decides, bar by bar, exactly as live: the
same anchor and levels already opened, the same caps, gap-fills, entry
filters, VIX rule and daily loss limit. A rung is priced with the engine's
model at that bar's spot, time to expiry and the path's VIX, filled half the
spread against the trader, and charged the rates in force. Everything settles
on expiry day against the average of its final half hour -- the exchange's
own method -- with STT on exercise. The answer is the spread of outcomes
across paths, not one number.

Every plan uses the same paths (a fixed seed), so the difference between two
plans is the settings', not luck's.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import random
import statistics
import threading
from dataclasses import dataclass
from typing import Any, Callable

from engine.backtest.providers import ModelPriceProvider, PriceRequest
from engine.backtest.runner import _kind_for, _legs_for
from engine.config import IST
from engine.data.market_calendar import MarketCalendar
from engine.forward.fills import FillModel
from engine.pricing.costs import CostModel
from engine.pricing.iv_surface import from_vix
from engine.strategy.condor import (
    Condor, FilledLeg, PositionUnit, StrategyConfig, UnitKind, entry_refusal, vix_allows_entries,
)
from engine.strategy.ladder import Ladder
from engine.strategy.vertical import VerticalSpread

log = logging.getLogger(__name__)

#: 15-minute bar closes in a session, 09:15 to 15:30.
BAR_CLOSES = [dt.time(9, 29, 59)] + [
    (dt.datetime(2000, 1, 1, 9, 44, 59) + dt.timedelta(minutes=15 * i)).time() for i in range(24)
]
#: How far a drawn session may be rescaled either way.
SCALE_LIMITS = (0.4, 2.5)
#: Sessions of history drawn from.
HISTORY_DAYS = 500
#: Where a path's India VIX is held (as a fraction).
VIX_LIMITS = (0.08, 0.60)
#: Expiry settles on the average of the final half hour; these bars span it.
SETTLEMENT_FROM = dt.time(15, 0)


@dataclass
class Session:
    """One real session: log returns of its 15-minute closes, the first one
    from the previous session's close (the overnight gap), and the India VIX
    it opened with (the previous close)."""

    day: dt.date
    returns: list[float]
    vix: float
    #: India VIX at this session's close; None when it is not on record.
    vix_close: float | None = None

    @property
    def vix_change(self) -> float:
        """The session's log change in India VIX, open to close."""
        if not self.vix_close or self.vix <= 0:
            return 0.0
        return math.log(self.vix_close / self.vix)


_history: dict[dt.date, list[Session]] = {}
_history_lock = threading.Lock()


def history(market, today: dt.date | None = None) -> list[Session]:
    """The library of real sessions, built once a day from Choice."""
    today = today or dt.datetime.now(tz=IST).date()
    with _history_lock:
        if today in _history:
            return _history[today]
        start = today - dt.timedelta(days=int(HISTORY_DAYS * 1.5))
        frame = market.nifty(start, today, "15", strict=False)
        vix_by_day = market.vix_by_date(start - dt.timedelta(days=10), today)
        closes: dict[dt.date, list[float]] = {}
        for row in frame.itertuples():
            ts = row.ts.to_pydatetime() if hasattr(row.ts, "to_pydatetime") else row.ts
            if ts.date() < today and ts.time() <= dt.time(15, 30):
                closes.setdefault(ts.date(), []).append(float(row.close))
        days = sorted(closes)
        sessions: list[Session] = []
        vix_days = sorted(vix_by_day)
        for prev, day in zip(days, days[1:]):
            bars = closes[day]
            if len(bars) < 20:
                continue
            known = [d for d in vix_days if d < day]
            if not known:
                continue
            vix = float(vix_by_day[known[-1]]) / 100.0
            close = vix_by_day.get(day)
            chain = [closes[prev][-1]] + bars
            sessions.append(Session(day, [math.log(b / a) for a, b in zip(chain, chain[1:])], vix,
                                    float(close) / 100.0 if close else None))
        sessions = sessions[-HISTORY_DAYS:]
        _history.clear()
        _history[today] = sessions
        log.info("Playground history: %d sessions from %s", len(sessions), sessions[0].day if sessions else None)
        return sessions


@dataclass
class Book:
    """The campaign as it stands: open positions, what has been realised,
    the ladder's state, and where today's loss limit stands."""

    expiry: dt.date
    open_units: list[PositionUnit]
    realised: float
    ladder_state: dict[str, Any] | None   # None: a fresh campaign, anchored at the first bar
    #: Today's P&L so far on the live run, against which the daily limit runs.
    day_pnl: float = 0.0
    #: The live run has already stopped entries for today.
    halted_today: bool = False


@dataclass
class Path:
    """One simulated month: bar closes to expiry, and India VIX (a fraction)
    as each session opens."""

    bars: list[tuple[dt.datetime, float]]
    vix: dict[dt.date, float]


def _drifts(sessions: list[Session]) -> tuple[float, float]:
    """The history's average session return and VIX change, removed from every draw."""
    return (statistics.fmean(sum(x.returns) for x in sessions),
            statistics.fmean(x.vix_change for x in sessions))


def _paths(sessions: list[Session], now: dt.datetime, spot: float, vix: float,
           trading_days: list[dt.date], n: int, seed: int) -> list[Path]:
    """`n` simulated paths from now to expiry's close."""
    rng = random.Random(seed)
    lo, hi = SCALE_LIMITS
    vlo, vhi = VIX_LIMITS
    drift, vix_drift = _drifts(sessions)
    out = []
    for _ in range(n):
        level = math.log(spot)
        v = vix
        bars: list[tuple[dt.datetime, float]] = []
        vix_path: dict[dt.date, float] = {}
        for day in trading_days:
            drawn = sessions[rng.randrange(len(sessions))]
            vix_path[day] = v
            scale = min(hi, max(lo, v / drawn.vix)) if drawn.vix > 0 else 1.0
            returns = drawn.returns
            closes = BAR_CLOSES[-len(returns):] if len(returns) <= len(BAR_CLOSES) else BAR_CLOSES
            returns = returns[-len(closes):]
            per_bar = drift / len(returns)
            for t, r in zip(closes, returns):
                when = dt.datetime.combine(day, t, tzinfo=IST)
                if when <= now:
                    continue                         # today's bars already behind us
                level += (r - per_bar) * scale
                bars.append((when, math.exp(level)))
            v = min(vhi, max(vlo, v * math.exp(drawn.vix_change - vix_drift)))
        out.append(Path(bars, vix_path))
    return out


def _straight(now: dt.datetime, spot: float, target: float, trading_days: list[dt.date]) -> Path:
    """A steady move from `spot` to `target` by expiry, for 'if it ends at X'.
    India VIX is held where it is."""
    stamps = [dt.datetime.combine(day, t, tzinfo=IST) for day in trading_days for t in BAR_CLOSES]
    stamps = [w for w in stamps if w > now]
    if not stamps:
        return Path([], {})
    a, b = math.log(spot), math.log(target)
    return Path([(w, math.exp(a + (b - a) * (i + 1) / len(stamps))) for i, w in enumerate(stamps)], {})


def settlement_price(bars: list[tuple[dt.datetime, float]], expiry: dt.date) -> float | None:
    """What expiry settles against: the average of its final half hour, as the
    exchange computes NIFTY's closing price -- here from the bar closes in it."""
    last = [p for w, p in bars if w.date() == expiry and w.time() >= SETTLEMENT_FROM]
    if last:
        return statistics.fmean(last)
    return bars[-1][1] if bars else None


@dataclass
class Outcome:
    pnl: float
    opened: int
    #: Rungs the daily loss limit kept from opening.
    held_back: int


class _Campaign:
    """One path's campaign: the ladder deciding, the model pricing, the book
    settling. The same rules as the backtest and the live run."""

    def __init__(self, config: StrategyConfig, book: Book, vix: float, now: dt.datetime, spot: float,
                 costs: CostModel, fills: FillModel, daily_loss_limit: float | None) -> None:
        self.config, self.book, self.vix = config, book, vix
        self.costs, self.fills = costs, fills
        self.limit = abs(daily_loss_limit) if daily_loss_limit else None
        self.surface = from_vix(vix * 100.0)
        self.now = now
        # Where today's limit is measured from, in this model's own marks: the
        # book now, less what the live run has already made or lost today.
        self.today_base = (self._value(list(book.open_units), now, spot, self._provider({}))
                           - book.day_pnl) if self.limit else 0.0

    def _provider(self, vix_path: dict[dt.date, float]) -> ModelPriceProvider:
        return ModelPriceProvider(
            surface=self.surface,
            vix_at=lambda when: 100.0 * vix_path.get(when.date(), self.vix),
        )

    @staticmethod
    def _value(units: list[PositionUnit], when: dt.datetime, spot: float,
               provider: ModelPriceProvider) -> float:
        """The open book's mark-to-market at a moment, net of entry costs."""
        total = 0.0
        for unit in units:
            marks = {}
            for fl in unit.legs:
                quote = provider.quote(PriceRequest(unit.expiry, fl.leg.strike, fl.leg.right, when, spot))
                if quote is not None:
                    marks[fl.leg] = quote.price
            total += unit.mtm(marks)
        return total

    def run(self, path: Path) -> Outcome:
        ladder = Ladder(config=self.config)
        if self.book.ladder_state:
            ladder.load_state(self.book.ladder_state)
        provider = self._provider(path.vix)
        units: list[PositionUnit] = list(self.book.open_units)
        opened = held_back = 0
        expiry = self.book.expiry
        vix_limit = self.config.max_entry_vix
        halted_on = self.now.date() if self.book.halted_today else None
        # For the daily limit: the book as the previous session ended -- its
        # last bar and how many units it held then.
        day: dt.date | None = None
        prev_bar: tuple[dt.datetime, float, int] | None = None
        day_open: tuple[dt.datetime, float, int] | None = None
        base: float | None = None
        for when, spot in path.bars:
            if when.date() != day:
                day, day_open, base = when.date(), prev_bar, None
            if when.date() < expiry:                 # nothing opens on expiry day
                allowed = vix_allows_entries(100.0 * path.vix.get(day, self.vix), vix_limit, ladder.paused)
                for trigger in ladder.on_price(spot, when, entries_allowed=allowed):
                    if self.limit is not None and halted_on != day:
                        if base is None:
                            if day == self.now.date() or day_open is None:
                                base = self.today_base
                            else:
                                w0, s0, n0 = day_open
                                base = self._value(units[:n0], w0, s0, provider)
                        if self._value(units, when, spot, provider) - base <= -self.limit:
                            halted_on = day
                    if halted_on == day:
                        held_back += 1
                        continue                     # the limit was hit today: as live, not opened
                    unit = self._open(trigger, ladder, when, spot, provider)
                    if unit is not None:
                        units.append(unit)
                        opened += 1
            prev_bar = (when, spot, len(units))
        settle = settlement_price(path.bars, expiry)
        if settle is None:
            return Outcome(self.book.realised, 0, 0)
        total = self.book.realised
        for unit in units:
            gross = sum(fl.leg.payoff(settle, fl.entry_price) for fl in unit.legs)
            stt = sum(self.costs.settlement_cost(fl.leg.side, fl.leg.intrinsic(settle), fl.leg.qty, expiry)
                      for fl in unit.legs)
            total += gross - unit.entry_costs - stt
        return Outcome(total, opened, held_back)

    def _open(self, trigger, ladder: Ladder, when: dt.datetime, spot: float,
              provider: ModelPriceProvider) -> PositionUnit | None:
        level = trigger.level
        try:
            legs = _legs_for(level, ladder, self.config)
        except ValueError:
            return None
        filled = []
        for leg in legs:
            quote = provider.quote(PriceRequest(self.book.expiry, leg.strike, leg.right, when, spot))
            if quote is None:
                return None
            price = self.fills.trade_fill(quote.price, leg.side).price
            filled.append(FilledLeg(leg=leg, entry_price=price))
        costs = sum(self.costs.leg_cost(fl.leg.side, fl.entry_price, fl.leg.qty, when.date()) for fl in filled)
        kind, k = _kind_for(level, ladder, self.config)
        unit_type = Condor if kind is UnitKind.CONDOR else VerticalSpread
        unit = unit_type(level=level, entry_time=when, expiry=self.book.expiry, legs=filled,
                         config=self.config, entry_costs=costs, side=trigger.side, kind=kind, k=k)
        if entry_refusal(unit, when.date(), self.config):
            return None
        return unit


def _summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    n = len(ordered)

    def pct(p: float) -> float:
        return ordered[min(n - 1, max(0, int(round(p * (n - 1)))))]

    mean = statistics.fmean(ordered)
    std = statistics.pstdev(ordered) if n > 1 else 0.0
    return {
        "mean": round(mean, 2), "median": round(pct(0.5), 2), "std": round(std, 2),
        "se": round(std / math.sqrt(n), 2) if n > 1 else 0.0,
        "p_loss": round(sum(1 for v in ordered if v < 0) / n, 4),
        "p5": round(pct(0.05), 2), "p25": round(pct(0.25), 2),
        "p75": round(pct(0.75), 2), "p95": round(pct(0.95), 2),
        "worst": round(ordered[0], 2), "best": round(ordered[-1], 2),
        # Expected shortfall: the average of the worst 5% -- what a bad month
        # costs on average, not just where bad begins.
        "es5": round(statistics.fmean(ordered[: max(1, n // 20)]), 2),
    }


def _histogram(values: list[float], bins: int = 24) -> list[dict[str, float]]:
    lo, hi = min(values), max(values)
    if hi <= lo:
        return [{"lo": lo, "hi": hi, "count": len(values)}]
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, int((v - lo) / width))] += 1
    return [{"lo": round(lo + i * width, 2), "hi": round(lo + (i + 1) * width, 2), "count": c}
            for i, c in enumerate(counts)]


def _quantiles(values: list[float], scale: float = 1.0) -> dict[str, float]:
    ordered = sorted(values)
    n = len(ordered)
    return {key: round(scale * ordered[min(n - 1, max(0, int(round(p * (n - 1)))))], 2)
            for key, p in (("p5", .05), ("p25", .25), ("p50", .5), ("p75", .75), ("p95", .95))}


def plan(
    *,
    market,
    config: StrategyConfig,
    book: Book,
    spot: float,
    vix: float,
    now: dt.datetime | None = None,
    paths: int = 1000,
    seed: int = 7,
    daily_loss_limit: float | None = None,
    progress: Callable[[float, str], None] = lambda *_: None,
) -> dict[str, Any]:
    """The spread of outcomes for this campaign under `config`."""
    now = now or dt.datetime.now(tz=IST)
    calendar = MarketCalendar.load()
    trading_days = [now.date() + dt.timedelta(days=i) for i in range((book.expiry - now.date()).days + 1)]
    trading_days = [d for d in trading_days if calendar.is_trading_day(d)]
    if not trading_days or trading_days[-1] != book.expiry:
        raise ValueError(f"{book.expiry} is not a trading day ahead of {now:%d %b}.")

    progress(0.05, "Reading two years of NIFTY sessions")
    sessions = history(market, now.date())
    if len(sessions) < 50:
        raise ValueError("Not enough NIFTY history to simulate from.")
    drift, _ = _drifts(sessions)

    campaign = _Campaign(config, book, vix, now, spot, CostModel(), FillModel(), daily_loss_limit)

    progress(0.15, f"Simulating {paths:,} paths")
    simulated = _paths(sessions, now, spot, vix, trading_days, paths, seed)
    results, rungs, finals, vix_end, held = [], [], [], [], []
    for i, path in enumerate(simulated, 1):
        outcome = campaign.run(path)
        results.append(outcome.pnl)
        rungs.append(outcome.opened)
        held.append(outcome.held_back)
        finals.append(settlement_price(path.bars, book.expiry) or spot)
        vix_end.append(path.vix.get(book.expiry, vix))
        if i % 100 == 0:
            progress(0.15 + 0.7 * i / paths, f"Simulated {i:,} of {paths:,} paths")

    progress(0.88, "Tracing steady moves")
    ends_at = []
    for pct in range(-10, 11):
        target = spot * (1 + pct / 100.0)
        outcome = campaign.run(_straight(now, spot, target, trading_days))
        ends_at.append({"spot": round(target, 2), "pnl": round(outcome.pnl, 2)})

    sessions_left = sum(1 for d in trading_days
                        if dt.datetime.combine(d, BAR_CLOSES[-1], tzinfo=IST) > now)
    return {
        "inputs": {
            "spot": round(spot, 2), "vix": round(vix * 100.0, 2), "expiry": book.expiry.isoformat(),
            "sessions_left": sessions_left, "paths": paths, "seed": seed,
            "realised_so_far": round(book.realised, 2), "open_positions": len(book.open_units),
            "fresh_campaign": book.ladder_state is None,
            "entries_paused_by_vix": not vix_allows_entries(vix * 100.0, config.max_entry_vix, False),
            "daily_loss_limit": abs(daily_loss_limit) if daily_loss_limit else None,
            "day_pnl": round(book.day_pnl, 2), "halted_today": book.halted_today,
            "history_sessions": len(sessions),
            "history_from": sessions[0].day.isoformat(), "history_to": sessions[-1].day.isoformat(),
            # The history's own trend, taken out: per month of sessions left.
            "drift_removed_pct": round(100.0 * drift * sessions_left, 2),
        },
        "pnl": _summary(results),
        # Every path's outcome, in path order: two plans share their paths, so
        # they can be compared path by path.
        "paths_pnl": [round(v) for v in results],
        "histogram": _histogram(results),
        "rungs": {"mean": round(statistics.fmean(rungs), 2), "max": max(rungs)},
        "loss_limit": {
            "share_of_paths": round(sum(1 for h in held if h) / len(held), 4),
            "rungs_held_back": round(statistics.fmean(held), 2),
        },
        "nifty_at_expiry": _quantiles(finals),
        "vix_at_expiry": _quantiles(vix_end, 100.0),
        "ends_at": ends_at,
    }
