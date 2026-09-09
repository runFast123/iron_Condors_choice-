"""HTTP-level tests for the engine API.

The important property is isolation: two logged-in users must never be able to
see or touch each other's session, and an unauthenticated caller must get
nothing at all.
"""

from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from engine.auth import sessions as auth
from engine.choice.errors import ChoiceAuthError, StaticIpRejectedError

VENDOR, KEY = "VEND1", "secret-api-key"
ALICE, BOB = "9000000001", "9000000002"


class FakeChoiceSession:
    accept = True
    raise_static_ip = False

    def __init__(self, config):
        self.config = config
        self.session_id = None
        self.logged_off = False

    def login(self, force: bool = False):
        if FakeChoiceSession.raise_static_ip:
            raise StaticIpRejectedError("from 1.2.3.4")
        if not FakeChoiceSession.accept:
            raise ChoiceAuthError("Invalid credentials")
        self.session_id = "sess-1"
        return self.session_id

    def request(self, method, endpoint, data=None, **kw):
        return {"Status": "Success", "Response": {"Name": f"User {self.config.mobile_no[-2:]}"}}

    def logoff(self):
        self.logged_off = True


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    FakeChoiceSession.accept = True
    FakeChoiceSession.raise_static_ip = False
    monkeypatch.setattr(auth, "ChoiceSession", FakeChoiceSession)
    monkeypatch.setattr(auth, "registry", auth.SessionRegistry())
    import engine.api as api

    monkeypatch.setattr(api, "registry", auth.registry)
    yield


@pytest.fixture
def client() -> TestClient:
    from engine.api import app

    return TestClient(app)


def login(client: TestClient, mobile: str):
    return client.post("/auth/login", json={"vendor_id": VENDOR, "api_key": KEY, "mobile": mobile})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------- health


def test_health_is_public(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["ok"] is True


# -------------------------------------------------------------------- login


def test_login_returns_a_token_and_a_redacted_user(client):
    res = login(client, ALICE)
    assert res.status_code == 200
    body = res.json()
    assert len(body["token"]) >= 32
    assert body["user"]["mobile"].endswith("01")
    # Nothing sensitive may cross the wire.
    assert KEY not in res.text
    assert ALICE not in res.text


def test_bad_credentials_are_401(client):
    FakeChoiceSession.accept = False
    assert login(client, ALICE).status_code == 401


def test_static_ip_rejection_is_403_not_401(client):
    """A different problem with a different fix, so a different status."""
    FakeChoiceSession.raise_static_ip = True
    res = login(client, ALICE)
    assert res.status_code == 403
    assert "static IP" in res.json()["detail"] or "IP" in res.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        {"vendor_id": "", "api_key": KEY, "mobile": ALICE},
        {"vendor_id": VENDOR, "api_key": "", "mobile": ALICE},
        {"vendor_id": VENDOR, "api_key": KEY, "mobile": ""},
        {"vendor_id": VENDOR},
    ],
)
def test_incomplete_logins_are_rejected(client, payload):
    assert client.post("/auth/login", json=payload).status_code in (400, 401, 422)


# ------------------------------------------------------------------ session


def test_protected_routes_require_a_token(client):
    for path in ("/me", "/market/spot", "/market/expiries", "/forward/state"):
        assert client.get(path).status_code == 401, path


def test_garbage_token_is_rejected(client):
    assert client.get("/me", headers=bearer("not-a-token")).status_code == 401


def test_me_returns_the_signed_in_user(client):
    token = login(client, ALICE).json()["token"]
    res = client.get("/me", headers=bearer(token))
    assert res.status_code == 200
    assert res.json()["user"]["mobile"].endswith("01")


def test_logout_invalidates_the_token(client):
    token = login(client, ALICE).json()["token"]
    assert client.post("/auth/logout", headers=bearer(token)).json()["ok"] is True
    assert client.get("/me", headers=bearer(token)).status_code == 401


# ---------------------------------------------------------------- isolation


def test_two_users_get_different_tokens_and_identities(client):
    a = login(client, ALICE).json()
    b = login(client, BOB).json()
    assert a["token"] != b["token"]
    assert a["user"]["user_id"] != b["user"]["user_id"]


def test_one_users_token_never_resolves_to_another(client):
    a = login(client, ALICE).json()
    b = login(client, BOB).json()
    seen_a = client.get("/me", headers=bearer(a["token"])).json()["user"]["user_id"]
    seen_b = client.get("/me", headers=bearer(b["token"])).json()["user"]["user_id"]
    assert seen_a == a["user"]["user_id"]
    assert seen_b == b["user"]["user_id"]
    assert seen_a != seen_b


def test_one_user_logging_out_does_not_affect_another(client):
    a = login(client, ALICE).json()["token"]
    b = login(client, BOB).json()["token"]
    client.post("/auth/logout", headers=bearer(a))
    assert client.get("/me", headers=bearer(a)).status_code == 401
    assert client.get("/me", headers=bearer(b)).status_code == 200


def test_relogin_invalidates_the_users_previous_token(client):
    first = login(client, ALICE).json()["token"]
    second = login(client, ALICE).json()["token"]
    assert client.get("/me", headers=bearer(first)).status_code == 401
    assert client.get("/me", headers=bearer(second)).status_code == 200


def test_forward_state_is_empty_until_a_run_is_started(client):
    token = login(client, ALICE).json()["token"]
    res = client.get("/forward/state", headers=bearer(token))
    assert res.status_code == 200 and res.json()["running"] is False


def test_forward_tick_without_a_run_is_a_conflict(client):
    token = login(client, ALICE).json()["token"]
    assert client.post("/forward/tick", headers=bearer(token)).status_code == 409


# ---------------------------------------------------------------- engine key


def test_engine_key_gates_the_api_when_configured(monkeypatch):
    import engine.api as api
    from engine.config import EngineConfig

    monkeypatch.setattr(api, "engine_config", EngineConfig(shared_secret="s3cret"))
    client = TestClient(api.app)

    assert client.post(
        "/auth/login", json={"vendor_id": VENDOR, "api_key": KEY, "mobile": ALICE}
    ).status_code == 401

    ok = client.post(
        "/auth/login",
        json={"vendor_id": VENDOR, "api_key": KEY, "mobile": ALICE},
        headers={"X-Engine-Key": "s3cret"},
    )
    assert ok.status_code == 200


# ------------------------------------------------------------------- status


def test_status_is_readable_without_a_session(client):
    """The UI polls this before sign-in, so it must answer unauthenticated."""
    res = client.get("/status")
    assert res.status_code == 200
    body = res.json()
    assert body["engine_reachable"] is True
    assert body["logged_in"] is False
    assert body["has_session"] is False
    assert body["session_expired"] is False   # no token was presented


def test_status_distinguishes_never_signed_in_from_expired(client):
    """Two states needing two different fixes must not collapse into one."""
    never = client.get("/status").json()
    assert never["has_token"] is False and never["session_expired"] is False

    stale = client.get("/status", headers=bearer("a-token-that-no-longer-resolves")).json()
    assert stale["has_token"] is True
    assert stale["session_expired"] is True   # -> offer "sign in again", not "configure"


def test_status_reflects_a_live_session(client):
    token = login(client, ALICE).json()["token"]
    body = client.get("/status", headers=bearer(token)).json()
    assert body["logged_in"] is True
    assert body["has_session"] is True
    assert body["session_expired"] is False
    assert body["mobile"].endswith("01")


def test_status_never_leaks_a_secret(client):
    token = login(client, ALICE).json()["token"]
    res = client.get("/status", headers=bearer(token))
    assert KEY not in res.text
    assert ALICE not in res.text
    assert token not in res.text          # the token must not be echoed back
    assert "session_id" not in res.text


def test_status_after_logout_reports_expired(client):
    token = login(client, ALICE).json()["token"]
    client.post("/auth/logout", headers=bearer(token))
    body = client.get("/status", headers=bearer(token)).json()
    assert body["logged_in"] is False and body["session_expired"] is True


# ---------------------------------------------------------------- client ip


def test_client_ip_reports_the_engine_egress_not_the_caller(client, monkeypatch):
    """Choice enforces on the engine's source IP, so that is what we report."""
    from engine.choice import netinfo

    monkeypatch.setattr(
        netinfo.egress_ip, "get", lambda refresh=False: {"ip": "203.0.113.7", "source": "test", "note": None}
    )
    body = client.get("/client_ip").json()
    assert body["engine_egress_ip"] == "203.0.113.7"
    assert body["declare_this_with_choice"] == "203.0.113.7"
    assert "finx.choiceindia.com" in body["hint"]


def test_client_ip_says_so_when_it_cannot_be_determined(client, monkeypatch):
    from engine.choice import netinfo

    monkeypatch.setattr(
        netinfo.egress_ip, "get",
        lambda refresh=False: {"ip": None, "source": "unavailable", "note": "Set ENGINE_PUBLIC_IP"},
    )
    body = client.get("/client_ip").json()
    assert body["engine_egress_ip"] is None
    assert "ENGINE_PUBLIC_IP" in body["note"]


# ------------------------------------------------------- static-path hardening


@pytest.mark.parametrize(
    "path",
    [
        "/secrets.json",
        "/.env",
        "/.choice_session.json",
        "/engine/api.py",
        "/engine/auth/sessions.py",
        "/../.env",
        "/state/live-abc.json",
    ],
)
def test_engine_serves_no_files_from_disk(client, path):
    """The engine mounts no static directory, so nothing on disk is reachable.

    Asserted rather than assumed: adding a StaticFiles mount later would
    otherwise silently expose credentials and source.
    """
    assert client.get(path).status_code == 404


# ================================================= IV surface calibration


def test_calibration_is_readable_and_gated(client):
    assert client.get("/calibration").status_code == 401
    token = login(client, ALICE).json()["token"]
    body = client.get("/calibration", headers=bearer(token)).json()
    assert "calibration" in body
    assert body["stale_after_days"] > 0
