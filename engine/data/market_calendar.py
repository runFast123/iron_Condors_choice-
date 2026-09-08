"""When the NSE equity-derivatives session is actually open.

Weekday-and-clock is not a calendar. It says the market is open on Diwali, on
Republic Day, and on every other holiday, which makes a forward run spend the
day logging "no quotes" and a backtest treat a closed session as a data gap.

Three sources are layered, most authoritative first:

1. **Choice's own MarketStatus endpoint.** The exchange is the only real
   authority, and it knows about unscheduled closures that no static list can.
2. **Learned closures.** A weekday that MarketStatus reported closed during
   session hours is remembered, so the answer survives losing the endpoint.
3. **A shipped holiday file.** Fixed-date national holidays are certain years
   in advance; movable ones (Holi, Eid, Diwali) are not, and are deliberately
   *not* guessed here -- they get added from NSE's annual circular or learned
   from source 1.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Callable
from pathlib import Path

from engine.config import IST

log = logging.getLogger(__name__)

MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)

HOLIDAY_FILE = Path(__file__).with_name("nse_holidays.json")

# How long a MarketStatus answer stays fresh. The session boundary is the thing
# that moves, and it moves twice a day, so minutes is plenty.
STATUS_TTL = dt.timedelta(minutes=5)


def _parse_mmdd(value: str, year: int) -> dt.date | None:
    try:
        month, day = (int(part) for part in value.split("-"))
        return dt.date(year, month, day)
    except (ValueError, TypeError):
        log.warning("Ignoring unparseable holiday entry %r", value)
        return None


def _parse_iso(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except (ValueError, TypeError):
        log.warning("Ignoring unparseable holiday date %r", value)
        return None


@dataclass
class MarketCalendar:
    """Trading-day and session-hours arithmetic for NSE F&O."""

    recurring_fixed: tuple[str, ...] = ()
    explicit: set[dt.date] = field(default_factory=set)
    learned: set[dt.date] = field(default_factory=set)
    open_time: dt.time = MARKET_OPEN
    close_time: dt.time = MARKET_CLOSE
    # Called once per newly learned closure so the caller can persist it.
    # A holiday discovered at 09:20 is worthless if it is forgotten at 15:31.
    on_learn: Callable[[dt.date], None] | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "MarketCalendar":
        """Read the shipped holiday file, tolerating its absence."""
        path = path or HOLIDAY_FILE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.warning("No holiday file at %s; only weekends are known", path)
            return cls()
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Holiday file %s is unreadable (%s); only weekends are known", path, exc)
            return cls()

        explicit = {d for d in (_parse_iso(v) for v in raw.get("dates", [])) if d}
        return cls(
            recurring_fixed=tuple(raw.get("recurring_fixed", ())),
            explicit=explicit,
        )

    # ------------------------------------------------------------ holidays

    def holidays_in(self, year: int) -> set[dt.date]:
        fixed = {d for d in (_parse_mmdd(v, year) for v in self.recurring_fixed) if d}
        same_year = {d for d in self.explicit | self.learned if d.year == year}
        return fixed | same_year

    def is_holiday(self, day: dt.date) -> bool:
        return day in self.holidays_in(day.year)

    def is_trading_day(self, day: dt.date) -> bool:
        return day.weekday() < 5 and not self.is_holiday(day)

    def record_closure(self, day: dt.date) -> bool:
        """Remember a weekday the exchange turned out to be shut.

        Returns True the first time a given day is learned, so a caller can
        persist it rather than re-deriving it every session.
        """
        if day.weekday() >= 5 or day in self.learned:
            return False
        self.learned.add(day)
        log.info("Learned %s is not a trading day", day)
        if self.on_learn is not None:
            try:
                self.on_learn(day)
            except Exception:                       # noqa: BLE001
                log.exception("Could not persist the learned holiday %s", day)
        return True

    # -------------------------------------------------------------- window

    def is_session_time(self, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now(tz=IST)
        return self.open_time <= now.time() <= self.close_time

    def is_open(self, now: dt.datetime | None = None) -> bool:
        """Local best answer: a trading day, inside the session window."""
        now = now or dt.datetime.now(tz=IST)
        return self.is_trading_day(now.date()) and self.is_session_time(now)

    def next_open(self, now: dt.datetime | None = None) -> dt.datetime:
        """When the session next begins — used to idle instead of spinning."""
        now = now or dt.datetime.now(tz=IST)
        day = now.date()
        if self.is_trading_day(day) and now.time() < self.open_time:
            return dt.datetime.combine(day, self.open_time, tzinfo=IST)
        for ahead in range(1, 15):
            candidate = day + dt.timedelta(days=ahead)
            if self.is_trading_day(candidate):
                return dt.datetime.combine(candidate, self.open_time, tzinfo=IST)
        # Two weeks of holidays is not a thing; fall back rather than loop.
        return dt.datetime.combine(day + dt.timedelta(days=1), self.open_time, tzinfo=IST)


@dataclass
class MarketStatus:
    """Choice's live view of whether the exchange is open, with a short cache.

    The endpoint is consulted rather than trusted blindly: a transport failure
    must not be read as "closed" (that would silently halt a run) nor as
    "open" (that would spam a shut exchange). On failure the caller falls back
    to the local calendar, which is what ``is_open`` returns as ``None``.
    """

    session: object
    calendar: MarketCalendar
    _cached: bool | None = None
    _fetched_at: dt.datetime | None = None

    def is_open(self, now: dt.datetime | None = None) -> bool | None:
        now = now or dt.datetime.now(tz=IST)
        if self._fetched_at and now - self._fetched_at < STATUS_TTL:
            return self._cached

        try:
            resp = self.session.request("GET", "api/OpenAPI/MarketStatus")  # type: ignore[attr-defined]
        except Exception as exc:                     # noqa: BLE001 - any transport failure
            log.debug("MarketStatus unavailable (%s); using the local calendar", exc)
            return None

        state = _read_status(resp)
        self._cached, self._fetched_at = state, now
        if state is False and self.calendar.is_session_time(now):
            # Shut during what should be trading hours: that is a holiday.
            self.calendar.record_closure(now.date())
        return state


_OPEN_WORDS = ("open", "normal", "live", "continuous")
_CLOSED_WORDS = ("close", "closed", "holiday", "halt", "suspend")


def _read_status(resp: object) -> bool | None:
    """Find an open/closed signal anywhere in an undocumented response.

    The endpoint's shape is not published and has changed before, so rather
    than bind to one field this looks for an unambiguous word. Ambiguity
    returns None, which means "ask the calendar" -- never a guess.
    """
    text = json.dumps(resp, default=str).lower() if not isinstance(resp, str) else resp.lower()
    has_open = any(word in text for word in _OPEN_WORDS)
    has_closed = any(word in text for word in _CLOSED_WORDS)
    if has_open and not has_closed:
        return True
    if has_closed and not has_open:
        return False
    return None
