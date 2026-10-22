"""Test cross-session shared forward run viewing.

When two users are authenticated, each can view the other's active forward runs
in read-only mode, without being able to stop or mutate them.
"""

from __future__ import annotations

import datetime as dt
from fastapi.testclient import TestClient
import pytest

from engine import api
from engine.api import app, slugify_run_key
from engine.auth.sessions import IST, SessionRegistry, UserSession
from engine.store.db import LADDER, Store


class FakeRunner:
    def __init__(self, run_key=LADDER, total=0.0):
        self.run_key = run_key
        self.run_label = run_key
        self.strategy_id = LADDER
        self.session_id = f"sess-{run_key}"
        self.stopped_reason = None
        self.is_ticking = True
        self.started_at = dt.datetime(2026, 9, 15, 9, 15, tzinfo=IST)
        self.daily_loss_limit = 25_000.0
        self.strategy = type("S", (), {"lots": 1})()
        self._total = total

    def emit(self, *a, **k):
        pass

    def save(self):
        pass

    def snapshot(self):
        return {
            "session": {
                "status": "running",
                "stopped_reason": None,
                "started_at": self.started_at.isoformat(),
                "last_tick": "2026-09-15T09:15:00",
                "market_open": True,
                "connected": True,
                "expiry": "2026-09-24",
            },
            "pnl": {"total": self._total, "open_condors": 1},
            "ladder": {"direction": "both", "fired": [self.run_key]},
            "positions": [{"index": 0, "level": 23_400, "tag": self.run_key}],
            "netting": {"offset_ratio": 0.5},
            "fills": [],
            "events": [],
            "risk": {},
            "marks": {},
        }


@pytest.fixture
def two_user_sessions():
    now = dt.datetime.now(tz=IST)
    sess_a = UserSession(
        user_id="u_a", token="tok_a", choice=object(), mobile_masked="**1",
        vendor_id="VA", created_at=now, expires_at=now + dt.timedelta(hours=6),
        last_seen=now, _runners=api.registry._runners,
    )
    sess_b = UserSession(
        user_id="u_b", token="tok_b", choice=object(), mobile_masked="**2",
        vendor_id="VB", created_at=now, expires_at=now + dt.timedelta(hours=6),
        last_seen=now, _runners=api.registry._runners,
    )
    api.registry._sessions["tok_a"] = sess_a
    api.registry._by_user["u_a"] = "tok_a"
    api.registry._sessions["tok_b"] = sess_b
    api.registry._by_user["u_b"] = "tok_b"
    try:
        yield sess_a, sess_b
    finally:
        for k in ("two-way", "ladder"):
            sess_a.set_runner(k, None)
            sess_b.set_runner(k, None)
        api.registry._sessions.pop("tok_a", None)
        api.registry._by_user.pop("u_a", None)
        api.registry._sessions.pop("tok_b", None)
        api.registry._by_user.pop("u_b", None)


def test_shared_forward_run_viewing(two_user_sessions, tmp_path, monkeypatch):
    sess_a, sess_b = two_user_sessions
    store = Store(tmp_path / "engine.db")
    monkeypatch.setattr(api, "store", store)
    api.app.dependency_overrides[api.check_engine_key] = lambda: None
    try:
        # User A runs 'two-way'
        r_two_way = FakeRunner("two-way", total=8472.0)
        sess_a.set_runner("two-way", r_two_way)

        # User B runs 'ladder'
        r_ladder = FakeRunner("ladder", total=1200.0)
        sess_b.set_runner("ladder", r_ladder)

        client_b = TestClient(app, headers={"Authorization": f"Bearer {sess_b.token}"})

        # 1. User B lists runs -> sees both 'ladder' (owned) and 'two-way' (shared)
        runs_res = client_b.get("/forward/runs")
        assert runs_res.status_code == 200
        runs = runs_res.json()["runs"]
        run_map = {r["run_key"]: r for r in runs}

        assert "ladder" in run_map
        assert not run_map["ladder"].get("shared")

        assert "two-way" in run_map
        assert run_map["two-way"]["shared"] is True
        assert run_map["two-way"]["pnl"]["total"] == 8472.0

        # 2. User B fetches state of 'two-way' -> gets read-only shared snapshot
        state_res = client_b.get("/forward/state?run=two-way")
        assert state_res.status_code == 200
        body = state_res.json()
        assert body["run_key"] == "two-way"
        assert body["state"]["session"]["shared"] is True
        assert body["state"]["session"]["readonly"] is True
        assert body["state"]["pnl"]["total"] == 8472.0

        # 3. User B cannot stop User A's 'two-way' run
        stop_res = client_b.post("/forward/stop?run=two-way")
        assert stop_res.status_code == 409

        # 4. User A can stop their own run
        client_a = TestClient(app, headers={"Authorization": f"Bearer {sess_a.token}"})
        stop_a_res = client_a.post("/forward/stop?run=two-way")
        assert stop_a_res.status_code == 200
    finally:
        api.app.dependency_overrides.pop(api.check_engine_key, None)
