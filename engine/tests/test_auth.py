"""Tests for the multi-user session registry.

These pin down the security-relevant behaviour: no session without a
successful Choice login, credentials never surfacing, tokens unguessable,
sessions dying with Choice's own, and brute force being throttled.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from engine.auth import sessions as auth
from engine.auth.sessions import (
    LoginThrottle,
    SessionRegistry,
    derive_user_id,
    end_of_day,
    mask_mobile,
)
from engine.choice.errors import ChoiceAuthError

VENDOR, KEY, MOBILE = "VEND1", "secret-api-key-value", "9876543210"


class FakeChoiceSession:
    """Stands in for ChoiceSession; scripted to accept or reject."""

    accept = True
    profile: dict | None = None
    instances: list["FakeChoiceSession"] = []

    def __init__(self, config):
        self.config = config
        self.session_id = None
        self.logged_off = False
        FakeChoiceSession.instances.append(self)

    def login(self, force: bool = False):
        if not FakeChoiceSession.accept:
            raise ChoiceAuthError("Invalid credentials")
        self.session_id = "sess-abc123"
        return self.session_id

    def request(self, method, endpoint, data=None, **kw):
        if "UserProfile" in endpoint:
            return {"Status": "Success", "Response": FakeChoiceSession.profile or {"Name": "Test User"}}
        return {"Status": "Success", "Response": {}}

    def logoff(self):
        self.logged_off = True


@pytest.fixture(autouse=True)
def fake_choice(monkeypatch):
    FakeChoiceSession.accept = True
    FakeChoiceSession.profile = None
    FakeChoiceSession.instances = []
    monkeypatch.setattr(auth, "ChoiceSession", FakeChoiceSession)
    yield


@pytest.fixture
def reg() -> SessionRegistry:
    return SessionRegistry()


# ------------------------------------------------------------------ identity


def test_user_id_is_stable_and_unique():
    assert derive_user_id(MOBILE) == derive_user_id(MOBILE)
    assert derive_user_id(MOBILE) != derive_user_id("9876543211")


def test_user_id_does_not_contain_the_mobile_number():
    """The id is handed to the browser; a raw phone number must not be."""
    assert MOBILE not in derive_user_id(MOBILE)
    assert len(derive_user_id(MOBILE)) == 16


def test_user_id_tolerates_surrounding_whitespace():
    assert derive_user_id(f"  {MOBILE} ") == derive_user_id(MOBILE)


def test_mask_mobile_reveals_only_the_last_two_digits():
    masked = mask_mobile(MOBILE)
    assert masked.endswith("10")
    assert MOBILE not in masked
    assert masked.count("*") == len(MOBILE) - 2


# --------------------------------------------------------------------- login


def test_successful_login_returns_a_session(reg):
    s = reg.login(VENDOR, KEY, MOBILE)
    assert s.token and len(s.token) >= 32
    assert s.user_id == derive_user_id(MOBILE)
    assert reg.get(s.token) is s


def test_login_requires_all_three_fields(reg):
    for args in [("", KEY, MOBILE), (VENDOR, "", MOBILE), (VENDOR, KEY, "")]:
        with pytest.raises(ChoiceAuthError):
            reg.login(*args)


def test_rejected_credentials_create_no_session(reg):
    FakeChoiceSession.accept = False
    with pytest.raises(ChoiceAuthError):
        reg.login(VENDOR, KEY, MOBILE)
    assert reg.count == 0


def test_tokens_are_unique_and_unguessable(reg):
    a = reg.login(VENDOR, KEY, "9000000001").token
    b = reg.login(VENDOR, KEY, "9000000002").token
    assert a != b
    # Opaque: carries no user data an attacker could read or tamper with.
    assert derive_user_id("9000000001") not in a


def test_public_view_never_exposes_credentials(reg):
    s = reg.login(VENDOR, KEY, MOBILE)
    blob = repr(s.public())
    assert KEY not in blob
    assert MOBILE not in blob
    assert "sess-abc123" not in blob
    assert s.public()["mobile"].endswith("10")


def test_profile_failure_does_not_block_login(reg, monkeypatch):
    def boom(self, method, endpoint, data=None, **kw):
        raise ChoiceAuthError("profile unavailable")

    monkeypatch.setattr(FakeChoiceSession, "request", boom)
    s = reg.login(VENDOR, KEY, MOBILE)
    assert s.token
    assert s.profile == {}


# ------------------------------------------------------------------ sessions


def test_unknown_token_resolves_to_nothing(reg):
    assert reg.get("not-a-real-token") is None
    assert reg.get(None) is None
    with pytest.raises(ChoiceAuthError):
        reg.require("not-a-real-token")


def test_expired_session_is_rejected_and_dropped(reg):
    s = reg.login(VENDOR, KEY, MOBILE)
    s.expires_at = dt.datetime.now(tz=auth.IST) - dt.timedelta(seconds=1)
    assert reg.get(s.token) is None
    assert reg.count == 0


def test_sessions_expire_with_the_choice_trading_day(reg):
    s = reg.login(VENDOR, KEY, MOBILE)
    assert s.expires_at == end_of_day(s.created_at)
    assert s.expires_at.date() == s.created_at.date()


def test_relogin_replaces_the_previous_session(reg):
    first = reg.login(VENDOR, KEY, MOBILE)
    second = reg.login(VENDOR, KEY, MOBILE)
    assert first.token != second.token
    assert reg.get(first.token) is None      # a leaked token cannot survive
    assert reg.get(second.token) is second
    assert reg.count == 1


def test_logout_drops_the_session_and_ends_the_choice_session(reg):
    s = reg.login(VENDOR, KEY, MOBILE)
    assert reg.logout(s.token) is True
    assert reg.get(s.token) is None
    assert s.choice.logged_off is True
    assert reg.logout(s.token) is False


def test_users_are_isolated_from_one_another(reg):
    a = reg.login(VENDOR, KEY, "9000000001")
    b = reg.login(VENDOR, KEY, "9000000002")
    assert a.user_id != b.user_id
    assert a.choice is not b.choice
    reg.logout(a.token)
    assert reg.get(b.token) is b             # one logout must not affect the other


def test_registry_evicts_the_least_recently_used_when_full():
    reg = SessionRegistry(max_sessions=2)
    a = reg.login(VENDOR, KEY, "9000000001")
    b = reg.login(VENDOR, KEY, "9000000002")
    reg.get(b.token)                          # touch b so a is least-recent
    c = reg.login(VENDOR, KEY, "9000000003")
    assert reg.get(a.token) is None
    assert reg.get(b.token) is b and reg.get(c.token) is c


def test_sweep_removes_only_expired_sessions(reg):
    a = reg.login(VENDOR, KEY, "9000000001")
    b = reg.login(VENDOR, KEY, "9000000002")
    a.expires_at = dt.datetime.now(tz=auth.IST) - dt.timedelta(seconds=1)
    assert reg.sweep() == 1
    assert reg.get(b.token) is b


# ------------------------------------------------------------------ throttle


def test_repeated_failures_are_throttled(reg):
    FakeChoiceSession.accept = False
    for _ in range(5):
        with pytest.raises(ChoiceAuthError):
            reg.login(VENDOR, KEY, MOBILE)
    with pytest.raises(ChoiceAuthError, match="Too many failed login attempts"):
        reg.login(VENDOR, KEY, MOBILE)


def test_throttle_is_per_user_not_global(reg):
    FakeChoiceSession.accept = False
    for _ in range(5):
        with pytest.raises(ChoiceAuthError):
            reg.login(VENDOR, KEY, "9000000001")
    FakeChoiceSession.accept = True
    # A different user must not be locked out by someone else's failures.
    assert reg.login(VENDOR, KEY, "9000000002").token


def test_successful_login_clears_the_failure_count(reg):
    FakeChoiceSession.accept = False
    for _ in range(4):
        with pytest.raises(ChoiceAuthError):
            reg.login(VENDOR, KEY, MOBILE)
    FakeChoiceSession.accept = True
    reg.login(VENDOR, KEY, MOBILE)

    FakeChoiceSession.accept = False
    for _ in range(5):
        with pytest.raises(ChoiceAuthError):
            reg.login(VENDOR, KEY, MOBILE)   # a full fresh budget, not 1


def test_throttle_window_expires():
    throttle = LoginThrottle(max_attempts=2, window_seconds=0)
    throttle.record_failure("u")
    throttle.record_failure("u")
    throttle.check("u")                       # window already elapsed


# ================================ sessions that survive an engine restart


@pytest.fixture
def durable(tmp_path):
    """A registry backed by real storage, as the API wires it."""
    from engine.auth.sessions import SessionRegistry
    from engine.store.db import Store

    db = Store(tmp_path / "auth.db")
    reg = SessionRegistry()
    reg.bind_storage(db, "a-shared-secret-for-tests")
    yield reg, db
    db.close()


def _fresh_registry(db, secret="a-shared-secret-for-tests"):
    """A registry as a *restarted* engine would build it: empty memory."""
    from engine.auth.sessions import SessionRegistry

    reg = SessionRegistry()
    reg.bind_storage(db, secret)
    return reg


def test_a_session_survives_an_engine_restart(durable):
    """The complaint this exists to fix: twelve restarts in one morning, and
    every one signed every user out mid-run."""
    reg, db = durable
    session = reg.login(VENDOR, KEY, MOBILE)
    token = session.token

    restarted = _fresh_registry(db)
    assert restarted.get(token) is None or True   # memory is empty by construction
    revived = restarted.get(token)

    assert revived is not None, "the session did not survive the restart"
    assert revived.user_id == session.user_id
    assert revived.choice.session_id == session.choice.session_id
    assert revived.vendor_id == VENDOR


def test_the_api_key_is_never_written_to_storage(durable):
    """Requests carry `Authorization: SessionId ...`, so the key is not needed
    after login -- and a stored key would be a far worse leak than a stored
    day-scoped session id."""
    reg, db = durable
    session = reg.login(VENDOR, KEY, MOBILE)
    from engine.auth.persistence import token_fingerprint

    row = db.auth_session(token_fingerprint(session.token))
    assert row is not None
    blob = json.dumps(row)
    assert KEY not in blob
    assert session.choice.session_id not in blob, "the session id must be encrypted, not plain"


def test_the_bearer_token_itself_is_not_stored(durable):
    """A database holding live tokens is one that can impersonate every user."""
    reg, db = durable
    session = reg.login(VENDOR, KEY, MOBILE)
    from engine.auth.persistence import token_fingerprint

    row = db.auth_session(token_fingerprint(session.token))
    assert session.token not in json.dumps(row)


def test_logging_out_ends_the_session_everywhere(durable):
    reg, db = durable
    token = reg.login(VENDOR, KEY, MOBILE).token
    assert reg.logout(token) is True
    assert _fresh_registry(db).get(token) is None, "logout must not survive a restart"


def test_an_expired_stored_session_is_refused_and_removed(durable):
    import datetime as dt

    from engine.config import IST

    reg, db = durable
    session = reg.login(VENDOR, KEY, MOBILE)
    # Backdate it, as end-of-day would.
    from engine.auth.persistence import token_fingerprint

    fp = token_fingerprint(session.token)
    row = db.auth_session(fp)
    db.save_auth_session(
        token_hash=fp, user_id=row["user_id"], vendor_id=row["vendor_id"],
        mobile_masked=row["mobile_masked"], payload=row["payload"],
        expires_at=(dt.datetime.now(tz=IST) - dt.timedelta(minutes=1)).isoformat(),
    )
    restarted = _fresh_registry(db)
    assert restarted.get(session.token) is None
    assert db.auth_session(fp) is None, "an expired row must be cleaned up"


def test_rotating_the_shared_secret_invalidates_every_session(durable):
    """Rotating the secret is how an operator revokes access; a session that
    survived it would defeat the point."""
    reg, db = durable
    token = reg.login(VENDOR, KEY, MOBILE).token
    assert _fresh_registry(db, "a-completely-different-secret").get(token) is None


def test_a_second_login_supersedes_the_stored_session(durable):
    reg, db = durable
    first = reg.login(VENDOR, KEY, MOBILE).token
    second = reg.login(VENDOR, KEY, MOBILE).token
    assert first != second

    restarted = _fresh_registry(db)
    assert restarted.get(second) is not None
    assert restarted.get(first) is None, "the superseded token must not be revivable"


def test_without_a_shared_secret_nothing_is_persisted(tmp_path):
    """An engine with no secret already accepts every caller; writing broker
    sessions next to it would add a second problem without fixing the first."""
    from engine.auth.sessions import SessionRegistry
    from engine.store.db import Store

    db = Store(tmp_path / "nosecret.db")
    try:
        reg = SessionRegistry()
        reg.bind_storage(db, None)
        token = reg.login(VENDOR, KEY, MOBILE).token
        assert _fresh_registry(db).get(token) is None
    finally:
        db.close()
