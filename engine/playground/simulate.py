"""Plan a campaign: simulate where it can go from here.

Path model -- filtered historical simulation, the standard way risk desks
project a path-dependent book without assuming returns are normal:

* Every simulated session is a real NIFTY session of the last two years,
  drawn at random: its 15-minute bars, overnight gap included. That keeps
  NIFTY's own fat tails, its intraday swings and its gaps -- a normal model
  has none of them, and a ladder fires on exactly those.
* Each drawn session is rescaled from the India VIX it opened with to the
  VIX assumed now, so a calm July day drawn into a nervous October moves
  like an October day. Realised moves sit below implied on most days; that
  gap is kept, since it is what a premium seller earns.

On each path the run's own ladder decides, bar by bar, exactly as live: the
same anchor and levels already opened, the same caps, gap-fills, entry
filters and VIX rule. A rung is priced with the engine's model at that bar's
spot and time to expiry, filled half the spread against the trader, charged
the rates in force, and everything settles at the path's close on expiry day,
STT on exercise included. The answer is the spread of outcomes across paths,
not one number.

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
from dataclasses import dataclass, replace
from typing import Any, Callable

from engine.backtest.providers import ModelPriceProvider, PriceRequest
from engine.backtest.runner import _kind_for, _legs_for
from engine.config import IST
from engine.data.market_calendar import MarketCalendar
from engine.forward.fills import FillModel
from engine.pricing.costs import CostModel
from engine.pricing.iv_surface import from_vix
from engine.strategy.condor import Condor, FilledLeg, PositionUnit, Side, StrategyConfig, UnitKind, entry_refusal
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


@dataclass
class Session:
    """One real session: log returns of its 15-minute closes, the first one
    from the previous session's close (the overnight gap), and the India VIX
    it opened with (the previous close)."""

    day: dt.date
    returns: list[float]
    vix: float


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
            chain = [closes[prev][-1]] + bars
            sessions.append(Session(day, [math.log(b / a) for a, b in zip(chain, chain[1:])], vix))
        sessions = sessions[-HISTORY_DAYS:]
        _history.clear()
        _history[today] = sessions
        log.info("Playground history: %d sessions from %s", len(sessions), sessions[0].day if sessions else None)
        return sessions


@dataclass
class Book:
    """The campaign as it stands: open positions, what has been realised,
    and the ladder's state."""

    expiry: dt.date
    open_units: list[PositionUnit]
    realised: float
    ladder_state: dict[str, Any] | None   # None: a fresh campaign, anchored at the first bar


def _paths(sessions: list[Session], now: dt.datetime, spot: float, vix: float,
           trading_days: list[dt.date], n: int, seed: int) -> list[list[tuple[dt.datetime, float]]]:
    """`n` simulated paths of (bar close, spot) from now to expiry's close."""
    rng = random.Random(seed)
    lo, hi = SCALE_LIMITS
    out = []
    for _ in range(n):
        level = math.log(spot)
        path: list[tuple[dt.datetime, float]] = []
        for day in trading_days:
            drawn = sessions[rng.randrange(len(sessions))]
            scale = min(hi, max(lo, vix / drawn.vix)) if drawn.vix > 0 else 1.0
            returns = drawn.returns
            closes = BAR_CLOSES[-len(returns):] if len(returns) <= len(BAR_CLOSES) else BAR_CLOSES
            returns = returns[-len(closes):]
            for t, r in zip(closes, returns):
                when = dt.datetime.combine(day, t, tzinfo=IST)
                if when <= now:
                    continue                         # today's bars already behind us
                level += r * scale
                path.append((when, math.exp(level)))
        out.append(path)
    return out


def _straight(now: dt.datetime, spot: float, target: float, trading_days: list[dt.date]) -> list[tuple[dt.datetime, float]]:
    """A steady move from `spot` to `target` by expiry, for 'if it ends at X'."""
    stamps = [dt.datetime.combine(day, t, tzinfo=IST) for day in trading_days for t in BAR_CLOSES]
    stamps = [w for w in stamps if w > now]
    if not stamps:
        return []
    a, b = math.log(spot), math.log(target)
    return [(w, math.exp(a + (b - a) * (i + 1) / len(stamps))) for i, w in enumerate(stamps)]


class _Campaign:
    """One path's campaign: the ladder deciding, the model pricing, the book
    settling. The same rules as the backtest and the live run."""

    def __init__(self, config: StrategyConfig, book: Book, provider: ModelPriceProvider,
                 costs: CostModel, fills: FillModel, entries_allowed: bool) -> None:
        self.config, self.book, self.provider = config, book, provider
        self.costs, self.fills, self.allowed = costs, fills, entries_allowed

    def run(self, path: list[tuple[dt.datetime, float]]) -> tuple[float, int]:
        ladder = Ladder(config=self.config)
        if self.book.ladder_state:
            ladder.load_state(self.book.ladder_state)
        opened: list[PositionUnit] = []
        expiry = self.book.expiry
        for when, spot in path:
            if when.date() >= expiry:
                continue                             # nothing opens on expiry day
            for trigger in ladder.on_price(spot, when, entries_allowed=self.allowed):
                unit = self._open(trigger, ladder, when, spot)
                if unit is not None:
                    opened.append(unit)
        settle = path[-1][1] if path else None
        if settle is None:
            return self.book.realised, 0
        total = self.book.realised
        for unit in list(self.book.open_units) + opened:
            gross = sum(fl.leg.payoff(settle, fl.entry_price) for fl in unit.legs)
            stt = sum(self.costs.settlement_cost(fl.leg.side, fl.leg.intrinsic(settle), fl.leg.qty, expiry)
                      for fl in unit.legs)
            total += gross - unit.entry_costs - stt
        return total, len(opened)

    def _open(self, trigger, ladder: Ladder, when: dt.datetime, spot: float) -> PositionUnit | None:
        level = trigger.level
        try:
            legs = _legs_for(level, ladder, self.config)
        except ValueError:
            return None
        filled = []
        for leg in legs:
            quote = self.provider.quote(PriceRequest(self.book.expiry, leg.strike, leg.right, when, spot))
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

    provider = ModelPriceProvider(surface=from_vix(vix * 100.0))
    limit = config.max_entry_vix
    allowed = limit is None or vix * 100.0 <= limit
    campaign = _Campaign(config, book, provider, CostModel(), FillModel(), allowed)

    progress(0.15, f"Simulating {paths:,} paths")
    simulated = _paths(sessions, now, spot, vix, trading_days, paths, seed)
    results, rungs, finals = [], [], []
    for i, path in enumerate(simulated, 1):
        pnl, opened = campaign.run(path)
        results.append(pnl)
        rungs.append(opened)
        finals.append(path[-1][1] if path else spot)
        if i % 100 == 0:
            progress(0.15 + 0.7 * i / paths, f"Simulated {i:,} of {paths:,} paths")

    progress(0.88, "Tracing steady moves")
    ends_at = []
    for pct in range(-10, 11):
        target = spot * (1 + pct / 100.0)
        path = _straight(now, spot, target, trading_days)
        pnl, _ = campaign.run(path)
        ends_at.append({"spot": round(target, 2), "pnl": round(pnl, 2)})

    finals_sorted = sorted(finals)
    return {
        "inputs": {
            "spot": round(spot, 2), "vix": round(vix * 100.0, 2), "expiry": book.expiry.isoformat(),
            "sessions_left": len(trading_days), "paths": paths, "seed": seed,
            "realised_so_far": round(book.realised, 2), "open_positions": len(book.open_units),
            "fresh_campaign": book.ladder_state is None, "entries_paused_by_vix": not allowed,
            "history_sessions": len(sessions),
            "history_from": sessions[0].day.isoformat(), "history_to": sessions[-1].day.isoformat(),
        },
        "pnl": _summary(results),
        "histogram": _histogram(results),
        "rungs": {"mean": round(statistics.fmean(rungs), 2), "max": max(rungs)},
        "nifty_at_expiry": {
            key: round(finals_sorted[min(len(finals) - 1, int(p * (len(finals) - 1)))], 2)
            for key, p in (("p5", .05), ("p25", .25), ("p50", .5), ("p75", .75), ("p95", .95))
        },
        "ends_at": ends_at,
    }
