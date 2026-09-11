"""A forward run belongs to the user, not to the browser token.

The incident these come from: a user re-logged in at 13:57 while their ladder
held an open condor. Replacing the session suspended the run, nothing restarted
it, and at 15:01 the spot traded through the next rung. The condor never fired
and every surface still reported the run as live.
"""

from __future__ import annotations

import datetime as dt
import threading

import pytest

from engine.auth.sessions import IST, SessionRegistry, UserSession
from engine.store.db import HIC, LADDER


class FakeRunner:
    """Only what the registry and the watchdog touch."""

    def __init__(self, alive: bool = True) -> None:
        self.session_id = "run-1"
        self.stopped_reason = None
        self._suspended = False
        self.suspend_calls: list[str] = []
        self.poll_seconds = 15.0
        self.events: list[str] = []
        self._alive = alive

    def suspend(self, reason: str = "session ended") -> None:
        self._suspended = True
        self.suspend_calls.append(reason)

    def unsuspend(self) -> None:
        self._suspended = False

    @property
    def suspended(self) -> bool:
        return self._suspended

    @property
    def is_ticking(self) -> bool:
        return self._alive and not self._suspended

    def emit(self, severity: str, message: str, **detail: object) -> None:
        self.events.append(message)


def _session(registry: SessionRegistry, user_id: str, token: str) -> UserSession:
    now = dt.datetime.now(tz=IST)
    session = UserSession(
        user_id=user_id, token=token, choice=object(), mobile_masked="**7",
        vendor_id="V1", created_at=now,
        expires_at=now + dt.timedelta(hours=6), last_seen=now,
        _runners=registry._runners,
    )
    with registry._lock:
        registry._sessions[token] = session
        registry._by_user[user_id] = token
    return session


@pytest.fixture()
def registry() -> SessionRegistry:
    return SessionRegistry()


def test_a_second_session_inherits_the_run_already_ticking(registry):
    """The bug: re-logging in killed a ladder that was mid-flight."""
    first = _session(registry, "u1", "tok-a")
    runner = FakeRunner()
    first.set_runner(LADDER, runner)

    second = _session(registry, "u1", "tok-b")

    assert second.runner_for(LADDER) is runner, "the new session must see the same run"
    assert second.runner_for(LADDER).is_ticking


def test_replacing_a_session_does_not_suspend_its_run(registry):
    first = _session(registry, "u1", "tok-a")
    runner = FakeRunner()
    first.set_runner(LADDER, runner)

    _session(registry, "u1", "tok-b")
    with registry._lock:
        registry._drop_locked("tok-a", handover=True)

    assert runner.suspend_calls == [], "a handover is not an ending"
    assert runner.is_ticking


def test_losing_the_last_session_still_suspends_the_run(registry):
    """The distinction only helps if a real ending is still an ending."""
    only = _session(registry, "u1", "tok-a")
    runner = FakeRunner()
    only.set_runner(LADDER, runner)

    with registry._lock:
        registry._drop_locked("tok-a")

    assert runner.suspend_calls == ["session ended"]
    assert not runner.is_ticking


def test_one_user_losing_a_session_leaves_another_user_alone(registry):
    a = _session(registry, "u1", "tok-a")
    b = _session(registry, "u2", "tok-b")
    run_a, run_b = FakeRunner(), FakeRunner()
    a.set_runner(LADDER, run_a)
    b.set_runner(LADDER, run_b)

    with registry._lock:
        registry._drop_locked("tok-a")

    assert run_a.suspend_calls == ["session ended"]
    assert run_b.suspend_calls == [], "u2 never signed out"
    assert b.runner_for(LADDER) is run_b


def test_runs_are_kept_per_user_not_shared_between_them(registry):
    a = _session(registry, "u1", "tok-a")
    b = _session(registry, "u2", "tok-b")
    run_a = FakeRunner()
    a.set_runner(LADDER, run_a)

    assert b.runner_for(LADDER) is None, "a run must never leak to another user"
    assert a.runner_for(LADDER) is run_a


def test_clearing_a_run_removes_it_for_every_session_of_that_user(registry):
    first = _session(registry, "u1", "tok-a")
    second = _session(registry, "u1", "tok-b")
    first.set_runner(LADDER, FakeRunner())

    second.set_runner(LADDER, None)

    assert first.runner_for(LADDER) is None


# ============================== the watchdog


def test_a_dead_worker_is_restarted_on_the_same_run(monkeypatch, registry):
    """A run whose thread died kept its ladder and positions. Restarting must
    reuse that object, not resume a fresh copy from the database, or the fills
    since the last save are lost."""
    from engine import api

    session = _session(registry, "u1", "tok-a")
    runner = FakeRunner(alive=False)
    session.set_runner(LADDER, runner)

    started: list[object] = []
    monkeypatch.setattr(api, "_start_tick_thread", lambda r, s, p: started.append(r))

    assert api._revive_run(session) is True
    assert started == [runner], "the same run, not a new one"
    assert runner.suspended is False
    assert any("stopped ticking" in e for e in runner.events)


def test_a_ticking_run_is_left_alone(monkeypatch, registry):
    from engine import api

    session = _session(registry, "u1", "tok-a")
    session.set_runner(LADDER, FakeRunner(alive=True))

    started: list[object] = []
    monkeypatch.setattr(api, "_start_tick_thread", lambda r, s, p: started.append(r))

    assert api._revive_run(session) is False
    assert started == []


def test_a_deliberately_stopped_run_is_not_restarted(monkeypatch, registry):
    """The watchdog must not undo a user's decision to stop."""
    from engine import api

    session = _session(registry, "u1", "tok-a")
    runner = FakeRunner(alive=False)
    runner.stopped_reason = "stopped by user"
    session.set_runner(LADDER, runner)

    started: list[object] = []
    monkeypatch.setattr(api, "_start_tick_thread", lambda r, s, p: started.append(r))

    assert api._revive_run(session) is False
    assert started == []


def test_starting_a_worker_twice_does_not_leave_two_driving_one_run(registry):
    """Two threads on one runner would both fire the ladder and both write the
    whole state, last writer wins."""
    from engine import api
    from engine.forward.runner import ForwardRunner

    runner = object.__new__(ForwardRunner)
    runner.tick_thread = None
    runner.poll_seconds = 15.0
    runner.strategy_id = LADDER
    started = threading.Event()
    runner.run = lambda poll_seconds: started.wait(2.0)  # type: ignore[method-assign]

    session = _session(registry, "u1", "tok-a")
    api._start_tick_thread(runner, session, 5.0)
    first = runner.tick_thread
    api._start_tick_thread(runner, session, 5.0)

    assert runner.tick_thread is first, "the live worker must not be replaced"
    started.set()
    first.join(timeout=2.0)


def test_a_login_and_the_watchdog_racing_start_only_one_worker(monkeypatch, registry):
    """Resumption has three entry points on three different threads. Two of
    them racing would each restore the same saved run and drive it: two ladders
    on one run id, both writing the whole state, last writer wins."""
    import time

    from engine import api

    session = _session(registry, "u1", "tok-a")
    restored: list[FakeRunner] = []

    def slow_restore(sess, *, only=None):
        if sess.runner_for(LADDER) is not None:
            return False
        # The real resumer loads a scrip master and talks to the broker here.
        # The pause stands in for that: long enough that a second caller walks
        # straight through the check above unless the two are serialised.
        time.sleep(0.3)
        runner = FakeRunner()
        restored.append(runner)
        sess.set_runner(LADDER, runner)
        return True

    monkeypatch.setattr(api, "_resume_forward_locked", slow_restore)

    threads = [
        threading.Thread(target=lambda: api._resume_forward(session)) for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive()

    assert len(restored) == 1, "exactly one worker may own a run"
    assert session.runner_for(LADDER) is restored[0]


# ============================== two strategies, one user


def test_a_user_can_hold_one_run_of_each_strategy(registry):
    session = _session(registry, "u1", "tok-a")
    ladder, hic = FakeRunner(), FakeRunner()

    session.set_runner(LADDER, ladder)
    session.set_runner(HIC, hic)

    assert session.runner_for(LADDER) is ladder
    assert session.runner_for(HIC) is hic
    assert session.runners() == {LADDER: ladder, HIC: hic}


def test_stopping_one_strategy_leaves_the_other_running(registry):
    """The failure this prevents: a stop button that reports success while the
    user's other strategy carries on trading, or worse, stops that one too."""
    session = _session(registry, "u1", "tok-a")
    ladder, hic = FakeRunner(), FakeRunner()
    session.set_runner(LADDER, ladder)
    session.set_runner(HIC, hic)

    session.set_runner(LADDER, None)

    assert session.runner_for(LADDER) is None
    assert session.runner_for(HIC) is hic
    assert hic.is_ticking


def test_losing_the_last_session_suspends_every_strategy_not_just_one(registry):
    session = _session(registry, "u1", "tok-a")
    ladder, hic = FakeRunner(), FakeRunner()
    session.set_runner(LADDER, ladder)
    session.set_runner(HIC, hic)

    with registry._lock:
        registry._drop_locked("tok-a")

    assert ladder.suspend_calls == ["session ended"]
    assert hic.suspend_calls == ["session ended"], "the second strategy was forgotten"


def test_a_handover_carries_both_strategies_over(registry):
    first = _session(registry, "u1", "tok-a")
    ladder, hic = FakeRunner(), FakeRunner()
    first.set_runner(LADDER, ladder)
    first.set_runner(HIC, hic)

    second = _session(registry, "u1", "tok-b")
    with registry._lock:
        registry._drop_locked("tok-a", handover=True)

    assert second.runners() == {LADDER: ladder, HIC: hic}
    assert ladder.suspend_calls == [] and hic.suspend_calls == []


def test_reviving_one_strategy_does_not_touch_the_other(monkeypatch, registry):
    from engine import api

    session = _session(registry, "u1", "tok-a")
    ladder = FakeRunner(alive=True)
    hic = FakeRunner(alive=False)
    session.set_runner(LADDER, ladder)
    session.set_runner(HIC, hic)

    started: list[object] = []
    monkeypatch.setattr(api, "_start_tick_thread", lambda r, s, p: started.append(r))

    assert api._revive_run(session, LADDER) is False, "a ticking run is left alone"
    assert api._revive_run(session, HIC) is True
    assert started == [hic]


def test_one_strategys_saved_run_never_retires_anothers(monkeypatch, registry):
    """The hazard at the centre of this change.

    Resumption used to rank every running row a user had against each other and
    retire all but the winner. The moment a user runs two strategies that
    destroys a live book with open positions -- permanently, since the loser is
    marked stopped in the database -- purely for belonging to the other one.
    """
    from engine import api

    session = _session(registry, "u1", "tok-a")
    rows = [
        {"session_id": "ladder-run", "user_id": "u1", "strategy_id": LADDER,
         "started_at": "2026-09-10T08:57:00", "state": {"condors": [{"status": "OPEN"}]}},
        {"session_id": "hic-run", "user_id": "u1", "strategy_id": HIC,
         "started_at": "2026-09-11T09:15:00", "state": {"condors": [{"status": "OPEN"}]}},
    ]
    retired: list[str] = []
    monkeypatch.setattr(api.store, "running_forwards", lambda *a, **k: rows)
    monkeypatch.setattr(api.store, "mark_stopped", lambda sid, reason: retired.append(sid))

    resumed: list[tuple[str, str]] = []

    def fake_resume_one(sess, strategy_id, group):
        resumed.append((strategy_id, group[-1]["session_id"]))
        sess.set_runner(strategy_id, FakeRunner())
        return True

    monkeypatch.setattr(api, "_resume_one", fake_resume_one)

    assert api._resume_forward_locked(session) is True
    assert sorted(resumed) == [(HIC, "hic-run"), (LADDER, "ladder-run")]
    assert retired == [], "a live run of another strategy was retired"


def test_two_runs_of_the_SAME_strategy_still_leave_only_one(monkeypatch, registry):
    """Grouping must not weaken the rule it groups. Within one strategy the
    ranked winner still wins and its siblings are still retired."""
    from engine import api

    session = _session(registry, "u1", "tok-a")
    rows = [
        {"session_id": "real", "user_id": "u1", "strategy_id": LADDER,
         "started_at": "2026-09-10T08:57:00",
         "state": {"condors": [{"status": "OPEN"}], "last_tick": "2026-09-10T15:29:57"}},
        {"session_id": "empty", "user_id": "u1", "strategy_id": LADDER,
         "started_at": "2026-09-10T15:46:22", "state": {"condors": []}},
    ]
    retired: list[str] = []
    monkeypatch.setattr(api.store, "running_forwards", lambda *a, **k: rows)
    monkeypatch.setattr(api.store, "mark_stopped", lambda sid, reason: retired.append(sid))
    monkeypatch.setattr(api, "_restore_runner", lambda *a, **k: FakeRunner(), raising=False)

    chosen: list[str] = []

    def fake_resume_one(sess, strategy_id, group):
        ordered = sorted(group, key=api._resume_rank)
        for stale in ordered[:-1]:
            api.store.mark_stopped(stale["session_id"], "superseded")
        chosen.append(ordered[-1]["session_id"])
        sess.set_runner(strategy_id, FakeRunner())
        return True

    monkeypatch.setattr(api, "_resume_one", fake_resume_one)

    api._resume_forward_locked(session)
    assert chosen == ["real"], "the run holding positions must win"
    assert retired == ["empty"]


def test_an_untagged_row_is_read_as_the_ladder(monkeypatch, registry):
    """Rows written before strategies existed have no tag in the blob. They
    are ladder runs, and must resume as such rather than being skipped."""
    from engine import api

    session = _session(registry, "u1", "tok-a")
    rows = [{"session_id": "old-run", "user_id": "u1",
             "started_at": "2026-09-09T10:54:33", "state": {"condors": []}}]
    monkeypatch.setattr(api.store, "running_forwards", lambda *a, **k: rows)

    seen: list[str] = []
    monkeypatch.setattr(api, "_resume_one",
                        lambda sess, sid, group: seen.append(sid) or True)

    api._resume_forward_locked(session)
    assert seen == [LADDER]
