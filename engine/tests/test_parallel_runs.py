"""Several forward tests at once, for one user, none able to touch another.

The point of this is comparison. A backtest cannot settle whether the two-way
ladder beats down-only, because it prices options from a model wherever real
quotes are missing. Two runs on identical live ticks, differing in one setting,
can. That only works if the runs are genuinely independent.
"""

from __future__ import annotations

import datetime as dt

import pytest

from engine.api import (
    MAX_RUNS_PER_USER,
    _enforce_account_loss_limit,
    _resume_forward_locked,
    slugify_run_key,
)
from engine.auth.sessions import IST, SessionRegistry, UserSession
from engine.store.db import HIC, LADDER, Store


class FakeRunner:
    """Only what the registry, the watchdog and the backstop touch."""

    def __init__(self, run_key=LADDER, total=0.0, stopped=None):
        self.run_key = run_key
        self.run_label = run_key
        self.strategy_id = LADDER
        self.session_id = f"sess-{run_key}"
        self.stopped_reason = stopped
        self.events: list[str] = []
        self.saved = 0
        self._total = total
        self._suspended = False

    def snapshot(self):
        # Every field carries the run's name, so a page showing one run's
        # numbers under another's heading is detectable rather than plausible.
        return {
            "pnl": {"total": self._total, "open_condors": 1},
            "session": {"last_tick": None, "expiry": None},
            "ladder": {"direction": "down", "fired": [self.run_key]},
            "positions": [{"index": 0, "level": 23_400, "tag": self.run_key}],
            "fills": [{"condor_index": 0, "tag": self.run_key}],
            "events": [{"ts": "2026-09-15T09:15:00", "message": f"opened in {self.run_key}"}],
        }

    def emit(self, severity, message, **detail):
        self.events.append(message)

    def save(self):
        self.saved += 1

    def suspend(self, reason="session ended"):
        self._suspended = True

    @property
    def is_ticking(self):
        return not self._suspended


@pytest.fixture()
def registry() -> SessionRegistry:
    return SessionRegistry()


def _session(registry: SessionRegistry, user_id="u1", token="tok") -> UserSession:
    now = dt.datetime.now(tz=IST)
    session = UserSession(
        user_id=user_id, token=token, choice=object(), mobile_masked="**7",
        vendor_id="V1", created_at=now, expires_at=now + dt.timedelta(hours=6),
        last_seen=now, _runners=registry._runners,
    )
    with registry._lock:
        registry._sessions[token] = session
        registry._by_user[user_id] = token
    return session


# ============================== naming


@pytest.mark.parametrize(
    "given,expected",
    [
        ("Two-way 1 lot", "two-way-1-lot"),
        ("  DOWN only  ", "down-only"),
        ("ladder", "ladder"),
        ("a" * 60, "a" * 24),
    ],
)
def test_a_run_name_becomes_a_short_safe_key(given, expected):
    assert slugify_run_key(given) == expected


@pytest.mark.parametrize("given", ["", "   ", "!!!", "---"])
def test_a_name_with_nothing_usable_in_it_is_refused(given):
    """Better to refuse than to silently name a run after another one."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        slugify_run_key(given)


def test_two_different_names_do_not_collide_after_slugging():
    assert slugify_run_key("Two Way") != slugify_run_key("Down Only")


# ============================== independence


def test_one_user_can_hold_several_runs_at_once(registry):
    session = _session(registry)
    for key in ("ladder", "two-way", "wide"):
        session.set_runner(key, FakeRunner(key))

    assert sorted(session.runners()) == ["ladder", "two-way", "wide"]


def test_stopping_one_run_leaves_the_others_alone(registry):
    session = _session(registry)
    a, b = FakeRunner("a"), FakeRunner("b")
    session.set_runner("a", a)
    session.set_runner("b", b)

    session.set_runner("a", None)

    assert session.runner_for("a") is None
    assert session.runner_for("b") is b
    assert b.stopped_reason is None


def test_runs_never_leak_between_users(registry):
    mine = _session(registry, "u1", "tok-1")
    theirs = _session(registry, "u2", "tok-2")
    mine.set_runner("two-way", FakeRunner("two-way"))

    assert theirs.runners() == {}
    assert theirs.runner_for("two-way") is None


def test_losing_the_last_session_suspends_every_run(registry):
    session = _session(registry)
    runs = [FakeRunner(k) for k in ("a", "b", "c")]
    for r in runs:
        session.set_runner(r.run_key, r)

    with registry._lock:
        registry._drop_locked("tok")

    assert all(not r.is_ticking for r in runs), "a run was left without a session"


# ============================== resuming


def test_each_saved_run_resumes_on_its_own(monkeypatch, registry):
    """The hazard this replaces: ranking every running row against each other
    and retiring all but one. With several runs that destroys live books."""
    from engine import api

    session = _session(registry)
    rows = [
        {"session_id": "s1", "user_id": "u1", "strategy_id": LADDER, "run_key": "down-only",
         "started_at": "2026-09-15T09:15:00", "state": {"condors": [{"status": "OPEN"}]}},
        {"session_id": "s2", "user_id": "u1", "strategy_id": LADDER, "run_key": "two-way",
         "started_at": "2026-09-15T09:16:00", "state": {"condors": [{"status": "OPEN"}]}},
        {"session_id": "s3", "user_id": "u1", "strategy_id": HIC, "run_key": "hic",
         "started_at": "2026-09-15T09:17:00", "state": {"condors": [{"status": "OPEN"}]}},
    ]
    retired: list[str] = []
    monkeypatch.setattr(api.store, "running_forwards", lambda *a, **k: rows)
    monkeypatch.setattr(api.store, "mark_stopped", lambda sid, reason: retired.append(sid))

    seen: list[str] = []

    def fake_resume_one(sess, run_key, group):
        seen.append(run_key)
        sess.set_runner(run_key, FakeRunner(run_key))
        return True

    monkeypatch.setattr(api, "_resume_one", fake_resume_one)

    assert _resume_forward_locked(session) is True
    assert sorted(seen) == ["down-only", "hic", "two-way"]
    assert retired == [], "a live run was retired for belonging to another test"


def test_two_rows_sharing_a_run_name_still_leave_only_one(monkeypatch, registry):
    """Grouping by name must not weaken the rule it groups: a duplicate of the
    same run is still a duplicate."""
    from engine import api

    session = _session(registry)
    rows = [
        {"session_id": "real", "user_id": "u1", "strategy_id": LADDER, "run_key": "two-way",
         "started_at": "2026-09-15T09:15:00",
         "state": {"condors": [{"status": "OPEN"}], "last_tick": "2026-09-15T15:29:00"}},
        {"session_id": "empty", "user_id": "u1", "strategy_id": LADDER, "run_key": "two-way",
         "started_at": "2026-09-15T15:46:00", "state": {"condors": []}},
    ]
    monkeypatch.setattr(api.store, "running_forwards", lambda *a, **k: rows)

    groups: list[int] = []
    monkeypatch.setattr(
        api, "_resume_one",
        lambda sess, key, group: groups.append(len(group)) or True,
    )

    _resume_forward_locked(session)
    assert groups == [2], "the duplicate was not ranked against its own kind"


def test_a_row_written_before_runs_had_names_is_read_as_the_ladder(monkeypatch, registry):
    """Both live runs are stored in exactly this shape."""
    from engine import api

    session = _session(registry)
    rows = [{"session_id": "old", "user_id": "u1", "strategy_id": LADDER,
             "started_at": "2026-09-10T08:57:00", "state": {"condors": []}}]
    monkeypatch.setattr(api.store, "running_forwards", lambda *a, **k: rows)

    seen: list[str] = []
    monkeypatch.setattr(api, "_resume_one", lambda s, k, g: seen.append(k) or True)

    _resume_forward_locked(session)
    assert seen == [LADDER]


# ============================== the account backstop


def test_one_run_polices_itself_without_the_account_check(registry):
    """A single run has its own limit. The backstop exists for the sum."""
    session = _session(registry)
    session.set_runner("solo", FakeRunner("solo", total=-10_000_000))

    assert _enforce_account_loss_limit(session) is False


def test_several_small_losses_that_add_up_stop_every_run(registry):
    """Per-run limits alone leave the total unbounded: five runs each stopping
    at their own limit is five times the intended worst case."""
    from engine.config import engine_config

    session = _session(registry)
    each = -(abs(engine_config.account_loss_limit) / 2 + 1)
    runs = [FakeRunner("a", total=each), FakeRunner("b", total=each)]
    for r in runs:
        session.set_runner(r.run_key, r)

    assert _enforce_account_loss_limit(session) is True
    assert all(r.stopped_reason for r in runs)
    assert all("account loss limit" in r.stopped_reason for r in runs)
    assert all(r.saved for r in runs), "a stopped run must be persisted as stopped"


def test_runs_inside_the_account_limit_are_left_running(registry):
    session = _session(registry)
    runs = [FakeRunner("a", total=-100.0), FakeRunner("b", total=-100.0)]
    for r in runs:
        session.set_runner(r.run_key, r)

    assert _enforce_account_loss_limit(session) is False
    assert all(r.stopped_reason is None for r in runs)


def test_an_already_stopped_run_does_not_drag_the_others_down(registry):
    """Its loss is realised and done. Counting it again would stop healthy
    runs for a breach that already had its consequence."""
    from engine.config import engine_config

    session = _session(registry)
    dead = FakeRunner("dead", total=-abs(engine_config.account_loss_limit) * 2,
                      stopped="stopped by user")
    alive = FakeRunner("alive", total=-100.0)
    session.set_runner("dead", dead)
    session.set_runner("alive", alive)

    assert _enforce_account_loss_limit(session) is False
    assert alive.stopped_reason is None


# ============================== the cap


def test_the_cap_is_a_real_number_not_unlimited():
    """Every run polls, and the broker rate limit is per user rather than per
    run, so this has to bind somewhere."""
    assert 1 <= MAX_RUNS_PER_USER <= 20


def test_a_run_is_stored_and_read_back_under_its_own_name(tmp_path):
    store = Store(tmp_path / "engine.db")
    try:
        for key in ("down-only", "two-way"):
            store.save_forward(
                session_id=f"s-{key}", user_id="u1", status="running",
                started_at="2026-09-15T09:15:00", stopped_reason=None,
                state={"version": 1}, strategy_id=LADDER, run_key=key, run_label=key,
            )
        keys = {r["run_key"] for r in store.running_forwards()}
        assert keys == {"down-only", "two-way"}
    finally:
        store.close()


# ============================== the routes themselves


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A signed-in user with three named runs, reachable over HTTP.

    The unit tests above exercise the resume and backstop logic directly, and
    missed that the routes were still reading a different query parameter --
    so every request silently addressed the default run. These close that gap
    by going through the wiring rather than around it.
    """
    from fastapi.testclient import TestClient

    from engine import api
    from engine.auth.sessions import registry as live_registry

    now = dt.datetime.now(tz=IST)
    session = UserSession(
        user_id="u1", token="tok", choice=object(), mobile_masked="**7",
        vendor_id="V1", created_at=now, expires_at=now + dt.timedelta(hours=6),
        last_seen=now, _runners=live_registry._runners,
    )
    live_registry._sessions["tok"] = session
    live_registry._by_user["u1"] = "tok"
    for key, total in (("down-only", 1200.0), ("two-way", -450.0), ("wide", 0.0)):
        runner = FakeRunner(key, total=total)
        runner.strategy = type("C", (), {"lots": 1})()
        runner.started_at = now
        runner.daily_loss_limit = 25_000.0
        session.set_runner(key, runner)

    with TestClient(api.app) as c:
        c.headers.update({"Authorization": "Bearer tok"})
        yield c

    for key in ("down-only", "two-way", "wide"):
        session.set_runner(key, None)
    live_registry._sessions.pop("tok", None)
    live_registry._by_user.pop("u1", None)


def test_the_listing_shows_every_run(client):
    body = client.get("/forward/runs").json()

    assert {r["run_key"] for r in body["runs"]} == {"down-only", "two-way", "wide"}
    assert body["max_runs"] >= 1
    assert body["account_loss_limit"] > 0


def test_each_run_is_addressable_by_name(client):
    for key in ("down-only", "two-way", "wide"):
        body = client.get(f"/forward/state?run={key}").json()
        assert body["running"] is True, key


def test_an_unknown_name_returns_nothing_rather_than_another_run(client):
    """The failure mode this prevents is silent: a mistyped or stale name that
    quietly answers about a different test."""
    body = client.get("/forward/state?run=does-not-exist").json()

    assert body["state"] is None
    assert body["running"] is False


def test_stopping_one_run_over_http_leaves_the_others_running(client):
    assert client.post("/forward/stop?run=two-way").json()["ok"] is True

    # A stopped run leaves the live listing entirely -- it is no longer being
    # driven, and its history lives in the database rather than in memory.
    after = {r["run_key"]: r["running"] for r in client.get("/forward/runs").json()["runs"]}
    assert "two-way" not in after
    assert after == {"down-only": True, "wide": True}


def test_stopping_a_run_that_does_not_exist_is_refused_not_silent(client):
    assert client.post("/forward/stop?run=does-not-exist").status_code == 409


def test_one_runs_numbers_never_appear_under_another_runs_name(client):
    """The confusion this guards against: several tests going at once, all of
    them condor ladders on NIFTY, whose fills and P&L look identical. A page
    that fetched the wrong one would be wrong in a way nobody could see."""
    for key in ("down-only", "two-way", "wide"):
        state = client.get(f"/forward/state?run={key}").json()["state"]

        assert state["positions"][0]["tag"] == key
        assert state["fills"][0]["tag"] == key
        assert state["ladder"]["fired"] == [key]
        assert key in state["events"][0]["message"]


def test_each_runs_pnl_is_its_own(client):
    totals = {
        key: client.get(f"/forward/state?run={key}").json()["state"]["pnl"]["total"]
        for key in ("down-only", "two-way", "wide")
    }
    assert totals == {"down-only": 1200.0, "two-way": -450.0, "wide": 0.0}


def test_the_listing_agrees_with_what_each_run_reports(client):
    """A summary row that disagreed with the page it links to would be worse
    than no summary at all."""
    listed = {r["run_key"]: r["pnl"]["total"] for r in client.get("/forward/runs").json()["runs"]}

    for key, total in listed.items():
        detail = client.get(f"/forward/state?run={key}").json()["state"]["pnl"]["total"]
        assert detail == total, key
