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

from engine.choice.errors import ChoiceAuthError, ChoiceSessionRejected
from engine.choice.session import (
    LOGIN_LEDGER, MAX_AUTOMATIC_LOGINS_PER_DAY, RELOGIN_COOLDOWN, ChoiceSession, _LoginLedger,
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

    def __init__(self, opened: dt.date | None = None):
        super().__init__(CONFIG)
        self.logins = 0
        self.session_id = "sess-0"
        # Yesterday's session by default: the one a morning renewal replaces.
        self._login_date = opened or dt.date.today() - dt.timedelta(days=1)

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



# ============================== a session opened today is never renewed


def test_a_session_opened_today_is_not_renewed_when_refused():
    """A Choice session lasts the whole day, so one minted today being
    refused is Choice's trouble, not an expiry. On 25 Sep the engine logged
    in four times into such a spell -- four OTPs, none of which helped."""
    s = Counting(opened=dt.date.today())
    for _ in range(50):                       # a long afternoon of refusals
        with pytest.raises(ChoiceSessionRejected) as caught:
            s._renew_after_rejection(s.session_id, "HTTP 401: Unauthorized, VendorId doesn't exists")
    assert s.logins == 0
    assert s.session_id == "sess-0", "the session is kept, to be used again when Choice accepts it"
    assert s.rejected_since is not None
    assert "OTP" in str(caught.value) and "VendorId" in str(caught.value)


def test_yesterdays_session_is_renewed_once_in_the_morning():
    s = Counting()                            # opened yesterday
    s._renew_after_rejection(s.session_id)
    assert s.logins == 1 and s._login_date == dt.date.today()
    # Refused again on the session it just minted: not renewed a second time.
    with pytest.raises(ChoiceSessionRejected):
        s._renew_after_rejection(s.session_id)
    assert s.logins == 1


def test_the_25_september_afternoon_costs_no_otp():
    """Replayed through the real transport: a session opened this morning,
    then half an hour of Choice answering 401 "VendorId doesn't exists". Every
    call fails, none of them logs in."""
    from engine.tests.test_session_transport import FakeResponse, session_with

    logins = []

    def responder(url, calls):
        if "OpenAPIV1/" in url:
            logins.append(url)
            return FakeResponse(200, {"Status": "Success", "Response": "sess-new"})
        return FakeResponse(401, text="Unauthorized, VendorId doesn't exists")

    s, _http = session_with(responder)
    from engine.choice.ratelimit import TokenBucket
    s._data_bucket = TokenBucket(10_000.0)           # the real 3/s would make this 40 s
    s.session_id, s._login_date = "sess-morning", dt.date.today()
    for _ in range(120):                      # a call every fifteen seconds for thirty minutes
        with pytest.raises(ChoiceSessionRejected):
            s.request("POST", "api/OpenAPI/MultipleTouchline", {})
    assert logins == [], "not one login, so not one OTP"
    assert s.session_id == "sess-morning"


def test_the_session_is_known_to_work_again_once_choice_accepts_it():
    from engine.tests.test_session_transport import FakeResponse, session_with

    answers = iter([401, 401, 200, 200])

    def responder(url, calls):
        code = next(answers)
        return FakeResponse(code, {"Status": "Success", "Response": {}} if code == 200 else None,
                            text="Unauthorized, VendorId doesn't exists" if code == 401 else "")

    s, _http = session_with(responder)
    s.session_id, s._login_date = "sess-morning", dt.date.today()
    for _ in range(2):
        with pytest.raises(ChoiceSessionRejected):
            s.request("POST", "api/OpenAPI/MultipleTouchline", {})
    assert s.rejected_since is not None and not s.proven_live(dt.timedelta(minutes=15))
    s.request("POST", "api/OpenAPI/MultipleTouchline", {})
    assert s.rejected_since is None and s.proven_live(dt.timedelta(minutes=15))


def test_proven_live_needs_today_a_recent_answer_and_no_refusal():
    s = ChoiceSession(CONFIG)
    now = dt.datetime.now()
    s.session_id, s._login_date, s.last_ok = "sess-1", now.date(), now - dt.timedelta(minutes=2)
    assert s.proven_live(dt.timedelta(minutes=15), now)
    assert not s.proven_live(dt.timedelta(minutes=1), now), "too long since Choice last accepted it"
    s._login_date = now.date() - dt.timedelta(days=1)
    assert not s.proven_live(dt.timedelta(minutes=15), now), "yesterday's session"
    s._login_date, s.rejected_since = now.date(), now
    assert not s.proven_live(dt.timedelta(minutes=15), now), "refused since"


def test_logging_off_never_logs_in_first():
    """Logging in only to log off would text an OTP to end a dead session."""
    from engine.tests.test_session_transport import FakeResponse, session_with

    logins = []

    def responder(url, calls):
        if "OpenAPIV1/" in url:
            logins.append(url)
            return FakeResponse(200, {"Status": "Success", "Response": "sess-new"})
        return FakeResponse(401, text="Unauthorized, VendorId doesn't exists")

    s, _http = session_with(responder)
    s.session_id = "sess-old"                  # no date: exactly what would renew
    s.logoff()
    assert logins == [] and s.session_id is None


# ============================== the ledger survives a restart


def test_the_ledger_survives_an_engine_restart(tmp_path):
    """Kept in memory, a restart reset the count: a fresh allowance of OTPs
    for an engine restarted in the middle of a bad afternoon."""
    path = tmp_path / "choice_logins.json"
    now = dt.datetime(2026, 9, 25, 12, 45)
    before = _LoginLedger(path)
    before.record("X1", automatic=True, now=now)

    after = _LoginLedger(path)                # the next process
    with pytest.raises(ChoiceAuthError):
        after.check_automatic("X1", now=now + dt.timedelta(minutes=5))
    assert after.automatic_today("X1", now=now) == 1

    after.record("X1", automatic=True, now=now + RELOGIN_COOLDOWN)
    third = _LoginLedger(path)
    with pytest.raises(ChoiceAuthError) as caught:
        third.check_automatic("X1", now=now + 3 * RELOGIN_COOLDOWN)
    assert "times today" in str(caught.value)
    # 25 Sep 2026 is a Friday: the next allowance is Monday's.
    third.check_automatic("X1", now=now + dt.timedelta(days=3))


def test_an_unreadable_ledger_is_not_fatal(tmp_path):
    path = tmp_path / "choice_logins.json"
    path.write_text("{not json", encoding="utf-8")
    # The wall clock decides nothing here: no night or closed-day rule.
    ledger = _LoginLedger(path, first_automatic_login=None, trading_days_only=False)
    ledger.check_automatic("X1")
    ledger.record("X1", automatic=True)
    assert _LoginLedger(path).automatic_today("X1") == 1


def test_at_most_two_automatic_logins_a_day():
    """The morning's renewal, and one retry if it fails. Nothing after."""
    assert MAX_AUTOMATIC_LOGINS_PER_DAY == 2
    assert RELOGIN_COOLDOWN >= dt.timedelta(minutes=30)



# ============================== after review


def _fast(s):
    """The default data rate is 3 requests a second: fine for Choice, slow for a test."""
    from engine.choice.ratelimit import TokenBucket

    s._data_bucket = TokenBucket(10_000.0)
    return s


def _yesterday():
    return dt.date.today() - dt.timedelta(days=1)


def test_a_login_that_fails_half_way_is_still_charged():
    """LoginTOTP texts the OTP; the login can still fail at the next step.
    Charged only on success, such a login could be retried without limit --
    twenty refusals made twenty OTP-sending calls."""
    from engine.tests.test_session_transport import FakeResponse, session_with

    otp_calls = []

    def responder(url, calls):
        if "LoginTOTP" in url and "GetClient" not in url:
            otp_calls.append(url)
            return FakeResponse(200, {"Status": "Success"})
        if "GetClientLoginTOTP" in url:
            return FakeResponse(500, text="boom")
        return FakeResponse(401, text="Unauthorized, VendorId doesn't exists")

    from engine.choice.errors import ChoiceError

    s, _http = session_with(responder)
    _fast(s)
    s.session_id, s._login_date = "sess-yesterday", _yesterday()
    for _ in range(20):
        with pytest.raises(ChoiceError):
            s.request("POST", "api/OpenAPI/MultipleTouchline", {})
    assert len(otp_calls) == 1, "one attempt, then the cooldown holds"
    assert LOGIN_LEDGER.automatic_today(s.account) == 1


def test_the_call_that_sends_the_otp_is_never_retried():
    from engine.tests.test_session_transport import session_with
    import requests as _requests

    otp_calls = []

    def responder(url, calls):
        otp_calls.append(url)
        raise _requests.Timeout("no answer")

    s, _http = session_with(responder)
    with pytest.raises(Exception):
        s.login(force=True, automatic=False)
    assert len(otp_calls) == 1


def test_a_signed_out_session_never_logs_itself_back_in():
    """Logoff clears the session; a tick still in flight on the shared object
    was refused, and the refusal logged the user straight back in."""
    from engine.tests.test_session_transport import FakeResponse, session_with

    logins = []

    def responder(url, calls):
        if "OpenAPIV1/" in url:
            logins.append(url)
            return FakeResponse(200, {"Status": "Success", "Response": "sess-new"})
        return FakeResponse(401, text="Unauthorized, VendorId doesn't exists")

    s, _http = session_with(responder)
    s.session_id, s._login_date = "sess-today", dt.date.today()
    s.logoff()
    with pytest.raises(ChoiceAuthError) as caught:
        s.request("POST", "api/OpenAPI/MultipleTouchline", {})
    assert logins == [] and "no Choice session to renew" in str(caught.value)


def test_a_session_nobody_opened_is_not_opened_by_a_refusal():
    from engine.tests.test_session_transport import FakeResponse, session_with

    s, http = session_with(lambda url, calls: FakeResponse(401, text="Unauthorized"))
    with pytest.raises(ChoiceAuthError):
        s.request("POST", "api/OpenAPI/MultipleTouchline", {})
    assert not any("OpenAPIV1/" in u for u in http.calls)


def test_an_old_session_without_credentials_says_sign_in():
    """Restored from a row sealed before credentials were kept, it is dated
    today because it cannot be renewed -- so "Choice's trouble, it will pick
    up again" would be wrong. Only a sign-in replaces it."""
    from engine.choice.session import UNRENEWABLE_MESSAGE
    from engine.tests.test_session_transport import FakeResponse, session_with
    from engine.config import ChoiceConfig as _Config

    s, _http = session_with(lambda url, calls: FakeResponse(401, text="Unauthorized"))
    s.config = _Config(vendor_id="V1", api_key="", mobile_no="")
    s.session_id, s._login_date = "sess-legacy", dt.date.today()
    with pytest.raises(ChoiceAuthError) as caught:
        s.request("POST", "api/OpenAPI/MultipleTouchline", {})
    assert str(caught.value) == UNRENEWABLE_MESSAGE
    assert not isinstance(caught.value, ChoiceSessionRejected)


def test_refusals_and_acceptances_racing_never_crash_a_run():
    """Two threads' calls on one session, one refused and one accepted at the
    same moment, used to read `rejected_since` three times without a lock: a
    TypeError that escaped every handler and stopped a live run for good."""
    import itertools
    import sys
    from engine.tests.test_session_transport import FakeResponse, session_with

    flip = itertools.cycle([401, 200])
    lock = threading.Lock()

    def responder(url, calls):
        with lock:
            code = next(flip)
        if code == 200:
            return FakeResponse(200, {"Status": "Success", "Response": {}})
        return FakeResponse(401, text="Unauthorized, VendorId doesn't exists")

    s, _http = session_with(responder)
    _fast(s)
    s.session_id, s._login_date = "sess-today", dt.date.today()
    unexpected = []

    def worker():
        for _ in range(300):
            try:
                s.request("POST", "api/OpenAPI/MultipleTouchline", {})
            except ChoiceSessionRejected:
                pass
            except Exception as exc:          # noqa: BLE001
                unexpected.append(exc)

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for th in threads: th.start()
        for th in threads: th.join()
    finally:
        sys.setswitchinterval(old)
    assert unexpected == []


def test_no_automatic_login_before_eight_in_the_morning(tmp_path):
    ledger = _LoginLedger(tmp_path / "logins.json")          # keeps the night rule
    with pytest.raises(ChoiceAuthError) as caught:
        ledger.claim_automatic("X1", now=dt.datetime(2026, 9, 28, 7, 59))
    assert "08:00" in str(caught.value)
    assert ledger.automatic_today("X1", now=dt.datetime(2026, 9, 28, 7, 59)) == 0
    ledger.claim_automatic("X1", now=dt.datetime(2026, 9, 28, 8, 0))
    # A person's own sign-in at night is theirs to make.
    ledger.record("X2", automatic=False, now=dt.datetime(2026, 9, 28, 2, 0))


def test_logins_by_another_process_are_seen_not_overwritten(tmp_path):
    """The command-line tools write the same file. Read once, the engine
    missed their logins, and its next save erased them."""
    path = tmp_path / "logins.json"
    engine = _LoginLedger(path, first_automatic_login=None)
    tool = _LoginLedger(path, first_automatic_login=None)
    t0 = dt.datetime(2026, 9, 28, 9, 0)
    engine.check_automatic("X1", now=t0)                     # the engine reads the file
    tool.record("X1", automatic=True, now=t0)                # a tool logs in meanwhile
    with pytest.raises(ChoiceAuthError):
        engine.check_automatic("X1", now=t0 + dt.timedelta(minutes=5))
    engine.record("X2", automatic=True, now=t0 + dt.timedelta(minutes=6))
    assert _LoginLedger(path).automatic_today("X1", now=t0) == 1, "the tool's login survived"


def test_an_odd_ledger_file_never_breaks_a_login(tmp_path):
    path = tmp_path / "logins.json"
    for junk in ("null", "[]", '{"accounts": []}', '{"accounts": {"X1": "oops"}}',
                 '{"accounts": {"X1": {"last": 5, "automatic_day": "not a date"}}}'):
        path.write_text(junk, encoding="utf-8")
        ledger = _LoginLedger(path, first_automatic_login=None)
        ledger.claim_automatic("X1", now=dt.datetime(2026, 9, 28, 9, 0))
        ledger.record("X1", automatic=False, now=dt.datetime(2026, 9, 28, 9, 1))


def test_a_person_running_a_tool_is_not_charged_as_the_engine():
    from engine.tests.test_session_transport import FakeResponse, session_with

    def responder(url, calls):
        return FakeResponse(200, {"Status": "Success", "Response": "sess-1"})

    s, _http = session_with(responder)
    s.interactive = True
    s.login()
    assert LOGIN_LEDGER.automatic_today(s.account) == 0



def test_a_refused_session_is_not_swallowed_as_a_missing_candle():
    """The index price falls back to the last candle; a refused session there
    became "no usable quote" on every tick instead of the message that says
    what is happening."""
    from engine.data.market import ChoiceMarketData

    class Refusing:
        def candles(self, *a, **k):
            raise ChoiceSessionRejected("Choice has been refusing this account's session")

    market = ChoiceMarketData.__new__(ChoiceMarketData)
    market.history = object()
    market.candles = Refusing().candles        # type: ignore[method-assign]
    contract = type("C", (), {"token": 26000})()
    with pytest.raises(ChoiceSessionRejected):
        market.last_traded([contract])


def test_no_automatic_login_on_a_day_the_market_is_shut(tmp_path):
    """Restarted on a Saturday, the engine renewed Friday's session: an OTP for
    a day nothing can trade. It waits for the next trading morning."""
    ledger = _LoginLedger(tmp_path / "logins.json", first_automatic_login=None)
    saturday, monday = dt.datetime(2026, 9, 26, 10, 0), dt.datetime(2026, 9, 28, 10, 0)
    with pytest.raises(ChoiceAuthError) as caught:
        ledger.claim_automatic("X1", now=saturday)
    assert "market is shut" in str(caught.value)
    ledger.claim_automatic("X1", now=monday)
    ledger.record("X2", automatic=False, now=saturday)          # a person may always sign in
