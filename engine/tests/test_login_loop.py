"""The engine must not log in to Choice over and over.

Every Choice login sends the account holder an OTP. On 24 Sep the engine
logged in 304 times before 08:35: two session objects held the same account,
each login cancelled the other's session, and the other's next call was
rejected and logged in again. These pin the three things that stop it: one
session object per account, one renewal however many callers are rejected at
once, and a hard cap on automatic logins whatever the cause.
"""

from __future__ import annotations

import datetime as dt
import threading

import pytest

from engine.choice.errors import ChoiceAuthError
from engine.choice.session import (
    LOGIN_LEDGER, MAX_AUTOMATIC_LOGINS_PER_DAY, RELOGIN_COOLDOWN, ChoiceSession,
)
from engine.config import ChoiceConfig

CONFIG = ChoiceConfig(vendor_id="V1", api_key="k-long-enough", mobile_no="9000000001")


# ============================== the ledger


def test_an_automatic_login_right_after_another_is_refused():
    now = dt.datetime(2026, 9, 24, 6, 0, 13)
    LOGIN_LEDGER.record("V1", automatic=True, now=now)

    with pytest.raises(ChoiceAuthError) as caught:
        LOGIN_LEDGER.check_automatic("V1", now=now + dt.timedelta(seconds=29))
    assert "OTP" in str(caught.value)

    LOGIN_LEDGER.check_automatic("V1", now=now + RELOGIN_COOLDOWN + dt.timedelta(seconds=1))


def test_a_users_own_sign_in_counts_but_is_never_refused_by_it():
    """A sign-in the user makes is theirs to make. It does start the
    cooldown, so an automatic login cannot follow straight on its heels."""
    now = dt.datetime(2026, 9, 24, 9, 0)
    LOGIN_LEDGER.record("V1", automatic=False, now=now)
    with pytest.raises(ChoiceAuthError):
        LOGIN_LEDGER.check_automatic("V1", now=now + dt.timedelta(minutes=1))


def test_automatic_logins_stop_for_the_day_after_the_cap():
    day = dt.datetime(2026, 9, 24, 6, 0)
    for i in range(MAX_AUTOMATIC_LOGINS_PER_DAY):
        t = day + i * (RELOGIN_COOLDOWN + dt.timedelta(minutes=1))
        LOGIN_LEDGER.check_automatic("V1", now=t)
        LOGIN_LEDGER.record("V1", automatic=True, now=t)

    later = day + dt.timedelta(hours=10)
    with pytest.raises(ChoiceAuthError) as caught:
        LOGIN_LEDGER.check_automatic("V1", now=later)
    assert "times today" in str(caught.value)

    # A new day starts a new allowance.
    LOGIN_LEDGER.check_automatic("V1", now=day + dt.timedelta(days=1, hours=1))


def test_accounts_are_counted_separately():
    now = dt.datetime(2026, 9, 24, 6, 0)
    LOGIN_LEDGER.record("V1", automatic=True, now=now)
    LOGIN_LEDGER.check_automatic("V2", now=now + dt.timedelta(seconds=5))


# ============================== one renewal for many rejected callers


class Counting(ChoiceSession):
    """A session whose login only counts, and whose server rejects any
    session id but the latest one it issued."""

    def __init__(self):
        super().__init__(CONFIG)
        self.logins = 0
        self.session_id = "sess-0"
        self._login_date = dt.date.today()

    def login(self, force=False, *, automatic=True):
        with self._lock:
            if automatic:
                LOGIN_LEDGER.check_automatic(self.account)
            self.logins += 1
            self.session_id = f"sess-{self.logins}"
            self._login_date = dt.date.today()
            LOGIN_LEDGER.record(self.account, automatic=automatic)
            return self.session_id


def test_callers_rejected_together_share_one_renewal():
    """When the day's session expires every thread using it is rejected at
    once; each used to log in on its own, one OTP per caller."""
    s = Counting()
    stale = s.session_id
    threads = [threading.Thread(target=s._renew_after_rejection, args=(stale,)) for _ in range(8)]
    for th in threads: th.start()
    for th in threads: th.join()

    assert s.logins == 1


def test_a_second_rejection_within_the_cooldown_does_not_log_in_again():
    s = Counting()
    s._renew_after_rejection(s.session_id)
    assert s.logins == 1

    # Rejected again, on the session it just minted: something else is wrong,
    # and logging in again would only send another OTP.
    with pytest.raises(ChoiceAuthError):
        s._renew_after_rejection(s.session_id)
    assert s.logins == 1


# ============================== one object per account


def test_this_mornings_loop_cannot_form():
    """Two objects for one account: each login cancels the other. Driven the
    way the engine drove them -- a check every thirty seconds, alternating --
    the ledger stops it at one automatic login instead of two an minute."""
    a, b = Counting(), Counting()
    server = {"live": "sess-0"}

    def call(s):
        if s.session_id != server["live"]:
            try:
                s._renew_after_rejection(s.session_id)
            except ChoiceAuthError:
                return
            server["live"] = s.session_id

    # Both start on yesterday's session, which has expired.
    server["live"] = "expired"
    for _ in range(20):                      # ten minutes of alternating checks
        call(a); call(b)

    assert a.logins + b.logins == 1


# ============================== the endpoint that started it


def test_a_refused_market_status_check_never_logs_in():
    """MarketStatus was refused on sessions every other endpoint accepted,
    and each refusal was read as an expired session: a login, and an OTP,
    per check. The check is advisory -- the calendar answers the same
    question -- so it must never ask for a login."""
    from engine.data.market_calendar import MarketCalendar, MarketStatus

    calls = []

    class Session:
        def request(self, method, endpoint, data=None, **kw):
            calls.append(kw)
            raise ChoiceAuthError("HTTP 401: Unauthorized")

    status = MarketStatus(Session(), MarketCalendar())
    assert status.is_open() is None
    assert calls[0].get("retry_auth") is False


def test_a_refusing_market_status_is_not_asked_again_every_tick():
    """Failures were not cached, so every tick asked again at once."""
    from engine.data.market_calendar import STATUS_TTL, MarketCalendar, MarketStatus
    from engine.config import IST

    calls = []

    class Session:
        def request(self, method, endpoint, data=None, **kw):
            calls.append(1)
            raise ChoiceAuthError("HTTP 401: Unauthorized")

    status = MarketStatus(Session(), MarketCalendar())
    t0 = dt.datetime(2026, 9, 24, 10, 0, tzinfo=IST)
    for s in range(0, 50, 10):                      # five ticks in fifty seconds
        status.is_open(t0 + dt.timedelta(seconds=s))
    assert len(calls) == 1

    status.is_open(t0 + STATUS_TTL + dt.timedelta(seconds=1))
    assert len(calls) == 2
