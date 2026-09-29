"""The evening job that keeps nifty.db current.

Every trading day after COLLECT_FROM, the engine takes that day's one-minute
bars of every listed NIFTY option near the money -- and of NIFTY and India
VIX -- from Choice and appends them to nifty.db (see history_store). If it
missed days, because the engine was down or no session was open, it collects
them too, for as long as the contracts are still listed.

It never logs in. It borrows a Choice session a user already opened today:
logging in would text someone an OTP, and a session from an earlier day would
need exactly that. With no such session it waits and tries again later.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time

from engine.config import IST

log = logging.getLogger(__name__)

#: The session is over and the day's last bars (to 15:40) are in.
COLLECT_FROM = dt.time(15, 50)
#: How often the job looks.
CHECK_EVERY = 300.0
#: Set to "off" to stop collecting.
ENV_SWITCH = "HISTORY_COLLECTOR"
#: Never reach further back than this in one go: Choice has nothing for
#: contracts that expired meanwhile, and a long gap is a job for a person.
MAX_CATCH_UP_DAYS = 10

#: A run with more failed contracts than this share is tried again.
MAX_FAILED_SHARE = 0.10
#: ...but not all evening: after this many tries the day is recorded anyway.
MAX_ATTEMPTS = 3

# Days a full collection came back with no bars at all: a closure the
# calendar did not know. Asked once, not every five minutes until midnight.
_empty_days: set[dt.date] = set()
_attempts: dict[dt.date, int] = {}


def _enabled() -> bool:
    return (os.environ.get(ENV_SWITCH) or "").strip().lower() not in ("off", "0", "false", "no")


class _Unreadable(Exception):
    """nifty.db exists but could not be read: skip this pass."""


def _last_collected() -> dt.date | None:
    """The last day collected in full; None when there is no file yet."""
    from engine.data import history_store

    if not history_store.available():
        return None
    try:
        info = history_store.coverage()
    except Exception as exc:                    # noqa: BLE001 - locked or unreadable
        # Not "nothing collected": that would re-fetch a finished day, and
        # against a file that stays locked, every five minutes until midnight.
        raise _Unreadable(str(exc)) from exc
    # Only finished collections count: a day with some bars may be a run that
    # was cut short, and its missing contracts would never be asked for.
    candidates = [
        dt.date.fromisoformat(str(info[key])[:10])
        for key in ("collected_until", "delivered_until") if info.get(key)
    ]
    return max(candidates) if candidates else None


def _live_market(registry, today: dt.date):
    """A market connection on a Choice session opened `today`, or None."""
    from engine.data.market import ChoiceMarketData

    with registry._lock:                         # noqa: SLF001 - read-only look
        sessions = list(registry._sessions.values())  # noqa: SLF001
    for session in sessions:
        choice = session.choice
        if getattr(choice, "_login_date", None) != today or not getattr(choice, "session_id", None):
            continue
        if getattr(choice, "rejected_since", None) is not None:
            continue
        if session.market is None:
            try:
                session.market = ChoiceMarketData.connect(choice)
            except Exception as exc:            # noqa: BLE001
                log.info("History job could not use %s's session: %s", session.user_id, exc)
                continue
        return session.market
    return None


def run_once(registry, now: dt.datetime | None = None) -> dict | None:
    """Collect whatever trading days are missing up to today. Returns a
    summary, or None when there was nothing to do or no way to do it."""
    from engine.data import history_store
    from engine.data.market_calendar import MarketCalendar

    now = now or dt.datetime.now(tz=IST)
    calendar = MarketCalendar.load()
    today = now.date()
    if not calendar.is_trading_day(today) or now.time() < COLLECT_FROM or today in _empty_days:
        return None
    try:
        last = _last_collected()
    except _Unreadable as exc:
        log.info("History job: nifty.db unreadable (%s); will retry", exc)
        return None
    if last is not None and last >= today:
        return None
    start = today if last is None else max(last + dt.timedelta(days=1), today - dt.timedelta(days=MAX_CATCH_UP_DAYS))
    market = _live_market(registry, today)
    if market is None:
        log.info("History job: no Choice session opened today to collect with; will retry")
        return None
    daily = market.nifty(today - dt.timedelta(days=7), today, "D", strict=False)
    if daily is None or daily.empty:
        return None
    spot = float(daily["close"].iloc[-1])
    legs = history_store.plan(market.master, spot, start)
    started = time.monotonic()
    report = history_store.collect(market, legs, start, today)
    summary = {
        "from": start.isoformat(), "to": today.isoformat(), "legs": report.legs,
        "with_bars": report.with_bars, "rows": report.rows, "failed": report.failed,
        "index_rows": report.index_rows, "seconds": round(time.monotonic() - started),
    }
    log.info("History job collected %s", summary)
    if report.rows == 0:
        if report.failed == 0:
            _empty_days.add(today)
            log.warning("History job: no bars at all for %s..%s; treating %s as a closed day",
                        start, today, today)
        else:
            # Nothing served and requests failing: an outage, not a holiday.
            # Never recorded as done; the next pass asks again.
            log.warning("History job: Choice served nothing and %d requests failed; will retry",
                        report.failed)
        return summary
    _attempts[today] = _attempts.get(today, 0) + 1
    if report.failed > report.legs * MAX_FAILED_SHARE and _attempts[today] < MAX_ATTEMPTS:
        log.warning("History job: %d of %d contracts failed; trying again", report.failed, report.legs)
        return summary
    if report.failed:
        log.warning("History job: %d of %d contracts could not be collected", report.failed, report.legs)
    history_store.mark_collected(start, today, report)
    return summary


def start(registry) -> None:
    """Run the job on a background thread for the life of the engine."""
    if not _enabled():
        log.info("History collector disabled")
        return

    def loop() -> None:
        while True:
            try:
                run_once(registry)
            except Exception:                   # noqa: BLE001 - a bad evening must not end the job
                log.exception("History job failed; will retry")
            time.sleep(CHECK_EVERY)

    threading.Thread(target=loop, name="history-collector", daemon=True).start()
    log.info("History collector: every trading day after %s", COLLECT_FROM.strftime("%H:%M"))
