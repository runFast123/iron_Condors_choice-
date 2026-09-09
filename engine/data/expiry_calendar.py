"""Which expiry a historical bar should have traded.

The scrip master is a snapshot of what is *listed today*. Contracts that have
already expired are delisted, so on 8 Sep 2026 the earliest NIFTY expiry it
knows about is 8 Sep 2026 itself. Asking it "what expires after 12 Mar 2026?"
therefore answers "8 Sep 2026" -- six months out -- and a backtest built on
that prices every weekly condor as a half-year option. Credits come out at 80%
of the wing width, which is impossible, and nothing ever rolls.

So historical expiries are *derived* from the exchange's rule rather than
looked up. The rule is read from the listed contracts rather than remembered:
NSE has moved NIFTY's expiry weekday before, and the contracts on file say what
it is now. Holiday roll-back is visible in that same data -- 25 Dec 2029 is a
Christmas, and the exchange lists 24 Dec 2029 instead.

Listed expiries always win where they exist. Derivation only fills in the past,
and can be a day out around a holiday the calendar has not been told about,
which is why the backtest reports how many of its expiries were derived.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from typing import Iterable, Sequence

from engine.data.market_calendar import MarketCalendar

log = logging.getLogger(__name__)

# NIFTY weekly expiry is Tuesday at the time of writing; only used when there
# are no listed contracts at all to learn from.
DEFAULT_EXPIRY_WEEKDAY = 1

# How far a "weekly" campaign may ever look for its expiry. A resolver that
# returns something further out than this has fallen back to a stale listing.
MAX_WEEKLY_DTE = 45


def infer_expiry_weekday(
    expiries: Sequence[dt.date], *, default: int = DEFAULT_EXPIRY_WEEKDAY
) -> int:
    """The weekday the exchange currently expires this underlying on.

    Taken as the mode rather than the first, because holiday roll-backs move
    individual expiries a day or two earlier and would otherwise be mistaken
    for the rule.
    """
    if not expiries:
        return default
    # Near-dated contracts are the weeklies; the far ones are half-yearly and
    # roll back more often, so they are poorer evidence of the weekly rule.
    near = sorted(expiries)[:12]
    return Counter(e.weekday() for e in near).most_common(1)[0][0]


def _roll_back_to_trading_day(day: dt.date, calendar: MarketCalendar) -> dt.date:
    """Step back to the previous trading day, as the exchange does."""
    for _ in range(10):
        if calendar.is_trading_day(day):
            return day
        day -= dt.timedelta(days=1)
    return day


def weekly_expiries(
    start: dt.date,
    end: dt.date,
    *,
    weekday: int = DEFAULT_EXPIRY_WEEKDAY,
    calendar: MarketCalendar | None = None,
) -> list[dt.date]:
    """Every weekly expiry in ``[start, end]``, rolled back off holidays."""
    calendar = calendar or MarketCalendar.load()
    out: list[dt.date] = []
    day = start + dt.timedelta(days=(weekday - start.weekday()) % 7)
    while day <= end:
        out.append(_roll_back_to_trading_day(day, calendar))
        day += dt.timedelta(days=7)
    return sorted(set(out))


def monthly_expiries(
    start: dt.date,
    end: dt.date,
    *,
    weekday: int = DEFAULT_EXPIRY_WEEKDAY,
    calendar: MarketCalendar | None = None,
) -> list[dt.date]:
    """The last weekly expiry of each month — the monthly contract.

    Offered because the ladder's offsetting thesis needs three condors deep in
    one expiry, which a weekly rarely allows and a monthly often does.
    """
    weeklies = weekly_expiries(start, end, weekday=weekday, calendar=calendar)
    by_month: dict[tuple[int, int], dt.date] = {}
    for expiry in weeklies:
        by_month[(expiry.year, expiry.month)] = expiry
    return sorted(by_month.values())


def expiry_calendar(
    start: dt.date,
    end: dt.date,
    listed: Iterable[dt.date] = (),
    *,
    cadence: str = "weekly",
    calendar: MarketCalendar | None = None,
) -> tuple[list[dt.date], set[dt.date]]:
    """Expiries covering ``[start, end]``, and which of them were derived.

    Listed contracts are authoritative wherever they exist; derivation fills in
    the delisted past. The second return value is the derived subset, so the
    caller can report how much of a run rests on an inferred calendar rather
    than on contracts the exchange actually published.
    """
    listed_in_range = {e for e in listed if start <= e <= end}
    weekday = infer_expiry_weekday(sorted(listed))

    build = monthly_expiries if cadence == "monthly" else weekly_expiries
    derived_all = build(start, end, weekday=weekday, calendar=calendar)

    # A derived date within a few days of a listed one is the same contract
    # seen through an incomplete holiday calendar; keep the exchange's version.
    derived: set[dt.date] = set()
    for candidate in derived_all:
        if any(abs((candidate - real).days) <= 3 for real in listed_in_range):
            continue
        derived.add(candidate)

    combined = sorted(listed_in_range | derived)
    if not combined:
        raise ValueError(f"No expiries could be established for {start}..{end}")
    log.info(
        "Expiry calendar %s..%s: %d listed, %d derived (weekday=%d, cadence=%s)",
        start, end, len(listed_in_range), len(derived), weekday, cadence,
    )
    return combined, derived


def nearest_listed_expiry(
    listed: Sequence[dt.date],
    on: dt.date,
    *,
    cadence: str = "weekly",
    min_days: int = 1,
) -> dt.date | None:
    """The next expiry to trade, honouring the campaign's cadence.

    A monthly campaign must not be handed the nearest *weekly*: it would open
    a structure with days to run where the strategy intends weeks, and its
    ladder would settle before the offsetting the thesis depends on has any
    chance to accumulate.

    The monthly contract is the last expiry of its calendar month, which is
    what NSE lists -- derived from the listed dates rather than a rule, since
    for a live run the exchange's own list is authoritative.
    """
    cutoff = on + dt.timedelta(days=min_days)
    candidates = sorted(e for e in listed if e >= cutoff)
    if not candidates:
        return None
    if cadence != "monthly":
        return candidates[0]

    last_of_month: dict[tuple[int, int], dt.date] = {}
    for expiry in sorted(listed):
        last_of_month[(expiry.year, expiry.month)] = expiry
    monthlies = sorted(e for e in last_of_month.values() if e >= cutoff)
    # If the current month's monthly has already passed, the next month's is
    # the right answer -- never a weekly standing in for it.
    return monthlies[0] if monthlies else None

