"""Transport-level tests for ChoiceSession.

These exercise the real ``request()`` path with a stubbed HTTP layer. The
existing auth and API tests replace ``ChoiceSession`` wholesale, which is why
they missed an infinite recursion that any user with a wrong API key would have
hit: a 401 from the login endpoint was treated as an expired session, so
``login()`` called ``request()`` which called ``login()`` again, ~490 frames
deep, until the worker died with a RecursionError instead of returning a clean
401.
"""

from __future__ import annotations

import pytest
import requests

from engine.choice.errors import ChoiceAuthError, ChoiceError, ChoiceTransportError
from engine.choice.session import AUTH_ENDPOINTS, ChoiceSession
from engine.config import ChoiceConfig

CONFIG = ChoiceConfig(vendor_id="V1", api_key="k", mobile_no="9000000001")


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")
        self.headers: dict[str, str] = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHttp:
    """Records calls and replays a scripted response per endpoint."""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[str] = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append(url)
        return self.responder(url, self.calls)


def session_with(responder) -> tuple[ChoiceSession, FakeHttp]:
    s = ChoiceSession(CONFIG)
    http = FakeHttp(responder)
    s._http = http  # type: ignore[attr-defined]
    return s, http


# ------------------------------------------------------------- the recursion


def test_bad_credentials_raise_once_and_do_not_recurse():
    """The regression. A 401 on LoginTOTP must be one clean error."""
    s, http = session_with(lambda url, calls: FakeResponse(401, text="Invalid API key"))

    with pytest.raises(ChoiceAuthError):
        s.login(force=True)

    # Without the fix this ran ~490 times before blowing the stack.
    assert len(http.calls) <= 3, f"login retried {len(http.calls)} times"


def test_a_401_on_any_auth_endpoint_never_triggers_a_relogin():
    for endpoint in AUTH_ENDPOINTS:
        s, http = session_with(lambda url, calls: FakeResponse(401, text="nope"))
        with pytest.raises(ChoiceAuthError):
            s.request("POST", endpoint, {}, require_auth=False)
        assert len(http.calls) == 1, f"{endpoint} retried"


def test_a_401_on_a_data_endpoint_does_re_login_exactly_once():
    """The behaviour we actually want to keep: expired sessions recover.
    Expired means opened on an earlier day -- a session opened today is kept."""
    import datetime as _dt

    state = {"logged_in": False}

    def responder(url, calls):
        if "OpenAPIV1/" in url:                       # the login sequence
            state["logged_in"] = True
            return FakeResponse(200, {"Status": "Success", "Response": "sess-1"})
        if not state["logged_in"]:
            return FakeResponse(401, text="session expired")
        return FakeResponse(200, {"Status": "Success", "Response": {"ok": True}})

    s, http = session_with(responder)
    s.session_id, s._login_date = "sess-yesterday", _dt.date.today() - _dt.timedelta(days=1)
    out = s.request("POST", "api/OpenGraph/ChartData", {})
    assert out["Status"] == "Success"
    assert s.session_id == "sess-1"
    # One failed call, three login calls, one replay.
    assert len(http.calls) == 5


def test_a_second_401_after_relogin_gives_up(monkeypatch):
    """No infinite loop even when the re-login itself succeeds but access stays denied."""
    def responder(url, calls):
        if "OpenAPIV1/" in url:
            return FakeResponse(200, {"Status": "Success", "Response": "sess-1"})
        return FakeResponse(401, text="still denied")

    import datetime as _dt

    s, http = session_with(responder)
    s.session_id, s._login_date = "sess-yesterday", _dt.date.today() - _dt.timedelta(days=1)
    with pytest.raises(ChoiceAuthError):
        s.request("POST", "api/OpenGraph/ChartData", {})
    assert len(http.calls) < 12


# ----------------------------------------------------------------- fail fast


def test_login_gives_up_quickly_when_choice_does_not_respond():
    """Interactive sign-in must not sit through the full backfill retry budget.

    A hung broker endpoint used to burn ~125s of read timeouts and backoff,
    long enough that the caller timed out first and reported 'engine
    unreachable' instead of the real cause.
    """
    def responder(url, calls):
        raise requests.Timeout("no response")

    s, http = session_with(responder)
    with pytest.raises(ChoiceTransportError):
        s.request("POST", "api/OpenAPIV1/LoginTOTP", {}, require_auth=False, retry_auth=False)
    assert len(http.calls) <= 2, f"login attempted {len(http.calls)} times"


def test_data_endpoints_keep_the_larger_retry_budget():
    def responder(url, calls):
        raise requests.Timeout("no response")

    s, http = session_with(responder)
    with pytest.raises(ChoiceTransportError):
        s.request("POST", "api/OpenGraph/ChartData", {})
    assert len(http.calls) > 2


# -------------------------------------------------------------- other paths


def test_a_successful_call_returns_the_parsed_body():
    s, _ = session_with(lambda url, calls: FakeResponse(200, {"Status": "Success", "Response": 1}))
    assert s.request("GET", "api/OpenAPI/UserProfile")["Response"] == 1


def test_non_json_success_is_reported_clearly():
    s, _ = session_with(lambda url, calls: FakeResponse(200, None, text="<html>gateway</html>"))
    with pytest.raises(ChoiceError, match="Non-JSON"):
        s.request("GET", "api/OpenAPI/UserProfile")


def test_credentials_never_appear_in_an_error_message():
    """The broker echoes the request body back on a 500, key included.

    The old assertion here was `"Bearer" not in msg or secret not in msg`,
    which is satisfied by the first clause alone -- "Bearer" never appears --
    so it passed while the key went out verbatim into the logs.
    """
    from engine.choice.errors import forget_secrets

    secret = "super-secret-key-value"
    forget_secrets()
    try:
        config = ChoiceConfig(vendor_id="V1", api_key=secret, mobile_no="9000000001")
        s = ChoiceSession(config)
        s._http = FakeHttp(lambda url, calls: FakeResponse(500, text=f"boom {secret}"))  # type: ignore[attr-defined]
        with pytest.raises(ChoiceError) as exc:
            s.request("GET", "api/OpenAPI/UserProfile")
        assert secret not in str(exc.value)
        assert "<redacted>" in str(exc.value)
    finally:
        forget_secrets()


def test_a_bare_credential_in_prose_is_redacted():
    """A key standing next to its own name is the easy case. The patterns miss
    one that arrives inside a sentence, and this engine keeps its logs."""
    from engine.choice.errors import forget_secrets, remember_secret, scrub

    forget_secrets()
    try:
        remember_secret("kX92mfQ1zzTvLpAA")
        assert scrub("upstream said kX92mfQ1zzTvLpAA is unknown") == (
            "upstream said <redacted> is unknown"
        )
    finally:
        forget_secrets()


def test_something_too_short_to_be_a_credential_is_not_registered():
    """Blanking a common short string would corrupt every message carrying it."""
    from engine.choice.errors import forget_secrets, remember_secret, scrub

    forget_secrets()
    try:
        remember_secret("abc")
        assert scrub("abc happened") == "abc happened"
    finally:
        forget_secrets()


def test_a_logged_in_session_registers_its_own_key():
    """Registration has to happen without a login, or an engine that only ever
    rehydrates a stored session never protects its key at all."""
    from engine.choice.errors import forget_secrets, scrub

    forget_secrets()
    try:
        ChoiceSession(ChoiceConfig(vendor_id="V1", api_key="a-very-long-api-key-x"))
        assert scrub("boom a-very-long-api-key-x") == "boom <redacted>"
    finally:
        forget_secrets()


# ============================== redaction must not eat the message


def test_scrubbing_keeps_an_error_readable():
    """The whitespace rule has no delimiter to anchor on, so it redacted
    whatever word followed a key name. Choice answers a dead session with
    "VendorId doesn't exists", which reached the log as "VendorId
    <redacted>'t exists" -- an error nobody could read or act on."""
    from engine.choice.errors import scrub

    assert scrub("HTTP 401: Unauthorized, VendorId doesn't exists") == (
        "HTTP 401: Unauthorized, VendorId doesn't exists"
    )
    assert scrub("Bearer token expired") == "Bearer token expired"
    assert scrub("OTP required for this account") == "OTP required for this account"


def test_scrubbing_still_removes_real_credentials():
    from engine.choice.errors import forget_secrets, remember_secret, scrub

    assert scrub("VendorId M09984 rejected") == "VendorId <redacted> rejected"
    assert scrub("OTP 483920 sent") == "OTP <redacted> sent"
    assert scrub("SessionId a1b2c3d4e5f6 expired") == "SessionId <redacted> expired"
    # No digit, but far longer than any English word.
    assert scrub("Bearer abcdefghijklmnopqrst") == "Bearer <redacted>"
    # A delimiter is enough on its own.
    assert "<redacted>" in scrub("vendorid=M09984")

    # And anything this process actually holds goes by value, whatever shape.
    try:
        remember_secret("SuperSecretAlphaKey")
        assert scrub("boom SuperSecretAlphaKey here") == "boom <redacted> here"
    finally:
        forget_secrets()


def test_a_session_that_cannot_be_renewed_is_an_auth_failure_not_a_crash():
    """A session revived from storage holds no credentials -- they are never
    persisted in a usable form -- so `login` raised a bare RuntimeError about
    missing environment variables. That is not a ChoiceError, so it escaped the
    runner's quote handling and reached the "unexpected error" branch, which
    *stops* a run. Two live campaigns with open positions were stopped that way
    by a session expiring overnight."""
    import datetime as _dt

    s, _http = session_with(lambda url, calls: FakeResponse(401, text="VendorId does not exist"))
    s.access_token = "stale"
    s.session_id, s._login_date = "sess-overnight", _dt.date.today() - _dt.timedelta(days=1)

    def login_without_credentials(force: bool = False):
        raise RuntimeError("Missing Choice credentials: CHOICE_API_KEY, CHOICE_MOBILE_NO.")

    s.login = login_without_credentials  # type: ignore[method-assign]

    with pytest.raises(ChoiceAuthError) as caught:
        s.request("POST", "api/OpenAPI/MultipleTouchline", {})

    assert "Sign out and sign in again" in str(caught.value)
