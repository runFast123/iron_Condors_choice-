"""India VIX lined up against the bars a backtest replays.

The VIX rule is decided bar by bar, so every NIFTY bar needs the India VIX that
was known when that bar closed -- and nothing later. Two details make that less
obvious than a lookup by timestamp.

Choice stamps a candle with the time of its last trade, so a NIFTY bar and a
VIX bar covering the same five minutes can be stamped seconds apart, either way
round. Comparing the raw stamps would hand some bars the VIX from one interval
earlier and, worse, some bars the VIX from one interval *later*. So both are
placed in the interval they cover -- counted from the 09:15 open -- and a bar
takes the VIX of its own interval, or failing that the latest earlier one.

A daily close is known only when the session ends. As a fallback it is
therefore available from 15:30 that day: it can decide the next morning's
bars, never its own day's.
"""

from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from engine.config import IST

#: Minutes per bar, for the intraday sizes.
INTRADAY_MINUTES = {"1": 1, "5": 5, "10": 10, "15": 15, "30": 30, "60": 60}
SESSION_OPEN = dt.time(9, 15)
SESSION_CLOSE = dt.time(15, 30)

#: Older than this and a reading is not evidence of anything about today. Four
#: days covers a weekend and a holiday; past it the bar has no VIX, and the
#: rule pauses entries rather than guess.
MAX_AGE = dt.timedelta(days=4)


def interval_start(ts: dt.datetime, resolution: str) -> dt.datetime:
    """The start of the bar interval `ts` falls in.

    Counted from 09:15, which is how NSE bars are cut: a 60-minute bar runs
    09:15-10:15, not 09:00-10:00. Daily and longer bars are keyed by their
    day; Choice stamps those at midnight already.
    """
    minutes = INTRADAY_MINUTES.get(resolution)
    if minutes is None:
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    anchor = ts.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute, second=0, microsecond=0)
    return ts - (ts - anchor) % dt.timedelta(minutes=minutes)


@dataclass
class AlignedVix:
    """One VIX reading per replayed bar, and where each came from."""

    values: list[float | None]
    sources: list[str | None]
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def missing(self) -> int:
        return sum(1 for v in self.values if v is None)


class VixAsOf:
    """India VIX as it stood at any moment, from bars and daily closes.

    One lookup for everything in a backtest that reads VIX -- the entry rule
    and the premium model -- so the two can never disagree about what VIX was
    at 10:00 on a given day.

    `bars` are VIX candles at the run's own bar size, as (stamp, close,
    source). `daily` are daily closes, as (day, close, source): the fallback
    for a day with no intraday bars, usable from that day's close onwards.
    """

    def __init__(
        self,
        resolution: str,
        bars: Iterable[tuple[dt.datetime, float, str]],
        daily: Iterable[tuple[dt.date, float, str]] = (),
        *,
        max_age: dt.timedelta = MAX_AGE,
    ) -> None:
        self.resolution = resolution
        self.max_age = max_age
        points: list[tuple[dt.datetime, float, str]] = []
        for ts, value, source in bars:
            if value is None or not value > 0:
                continue
            points.append((interval_start(ts, resolution), float(value), source))
        for day, value, source in daily:
            if value is None or not value > 0:
                continue
            points.append(
                (dt.datetime.combine(day, SESSION_CLOSE, tzinfo=IST), float(value), f"{source}:close")
            )
        # Stable, so where an interval has two readings the later-listed one --
        # a daily close after the intraday bars -- is the one kept.
        points.sort(key=lambda p: p[0])
        self._points = points
        self._keys = [p[0] for p in points]

    def reading(self, ts: dt.datetime) -> tuple[float, str] | None:
        """(value, source) known when the bar containing `ts` closed, or None."""
        key = interval_start(ts, self.resolution)
        i = bisect.bisect_right(self._keys, key) - 1
        if i < 0 or key - self._keys[i] > self.max_age:
            return None
        _, value, source = self._points[i]
        return value, source

    def at(self, ts: dt.datetime) -> float | None:
        found = self.reading(ts)
        return None if found is None else found[0]

    def align(self, bar_times: Sequence[dt.datetime]) -> AlignedVix:
        values: list[float | None] = []
        sources: list[str | None] = []
        counts: dict[str, int] = {}
        for ts in bar_times:
            found = self.reading(ts)
            source = None if found is None else found[1]
            values.append(None if found is None else found[0])
            sources.append(source)
            label = source or "none"
            counts[label] = counts.get(label, 0) + 1
        return AlignedVix(values=values, sources=sources, counts=counts)


def align_vix(
    bar_times: Sequence[dt.datetime],
    resolution: str,
    bars: Iterable[tuple[dt.datetime, float, str]],
    daily: Iterable[tuple[dt.date, float, str]] = (),
    *,
    max_age: dt.timedelta = MAX_AGE,
) -> AlignedVix:
    """The VIX each bar in `bar_times` could have seen. See VixAsOf."""
    return VixAsOf(resolution, bars, daily, max_age=max_age).align(bar_times)
