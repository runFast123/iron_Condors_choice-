"""Opening an older database must not disturb what is already in it.

Two users are holding open paper positions in the live database as this ships.
The migration that makes the engine multi-strategy runs inside `Store.__init__`,
before anything can object, so it gets one attempt and no supervision.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from engine.store.db import LADDER, Store

# The schema exactly as it stood before strategy_id existed. Copied rather than
# imported: the point is to build what is actually on disk today, and importing
# the current SCHEMA would test the migration against its own output.
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS forward_sessions (
    session_id     TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    status         TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    stopped_reason TEXT,
    state_json     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id       TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL,
    params_json  TEXT NOT NULL,
    dataset_json TEXT,
    error        TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

LIVE_STATE = {
    "version": 1,
    "condors": [{"index": 0, "level": 23_400.0, "status": "OPEN"}],
    "ladder": {"anchor": 23_400.0, "last_level": 23_400.0, "fired_levels": [234]},
}


@pytest.fixture()
def older_db(tmp_path):
    """A database written by the build that is running in production today."""
    path = tmp_path / "engine.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO forward_sessions VALUES (?,?,?,?,?,?,?)",
        ("live-run", "u1", "running", "2026-09-10T08:57:34+05:30",
         "2026-09-11T10:20:00+05:30", None, json.dumps(LIVE_STATE)),
    )
    conn.execute(
        "INSERT INTO backtest_runs VALUES (?,?,?,?,?,?,?)",
        ("bt-1", "u1", "2026-09-01T10:00:00+05:30", "done", "{}", "{}", None),
    )
    conn.commit()
    conn.close()
    return path


def test_a_live_run_survives_the_migration_and_is_tagged_as_the_ladder(older_db):
    """The whole migration: the column default tags every existing run without
    rewriting a row, because every run that exists today is a ladder run."""
    store = Store(older_db)
    try:
        running = store.running_forwards()
        assert len(running) == 1
        run = running[0]
        assert run["strategy_id"] == LADDER
        assert run["session_id"] == "live-run"
        # The part that actually matters: the ladder came back intact.
        assert run["state"]["ladder"]["fired_levels"] == [234]
        assert run["state"]["condors"][0]["status"] == "OPEN"
    finally:
        store.close()


def test_the_migration_adds_columns_rather_than_rewriting_rows(older_db):
    before = sqlite3.connect(older_db).execute(
        "SELECT state_json FROM forward_sessions WHERE session_id='live-run'"
    ).fetchone()[0]

    Store(older_db).close()

    after = sqlite3.connect(older_db).execute(
        "SELECT state_json FROM forward_sessions WHERE session_id='live-run'"
    ).fetchone()[0]
    assert after == before, "a migration that rewrites state can corrupt it"


def test_an_older_backtest_is_tagged_too(older_db):
    store = Store(older_db)
    try:
        assert [r["strategy_id"] for r in store.backtest_history("u1")] == [LADDER]
    finally:
        store.close()


def test_opening_the_same_database_twice_is_harmless(older_db):
    """The engine restarts often, and the watchdog opens the store on boot."""
    Store(older_db).close()
    store = Store(older_db)
    try:
        assert len(store.running_forwards()) == 1
    finally:
        store.close()


def test_the_migration_does_not_refuse_a_database_with_two_running_runs(older_db):
    """A unique index on (user, strategy, running) would be the obvious way to
    enforce one run per strategy -- and it would fail to build here, inside
    __init__, taking the whole engine down rather than reporting anything."""
    conn = sqlite3.connect(older_db)
    conn.execute(
        "INSERT INTO forward_sessions VALUES (?,?,?,?,?,?,?)",
        ("second-run", "u1", "running", "2026-09-10T15:46:22+05:30",
         "2026-09-10T15:46:22+05:30", None, json.dumps(LIVE_STATE)),
    )
    conn.commit()
    conn.close()

    store = Store(older_db)                     # must not raise
    try:
        assert len(store.running_forwards()) == 2
    finally:
        store.close()


def test_runs_can_be_read_back_per_strategy(older_db):
    store = Store(older_db)
    try:
        store.save_forward(
            session_id="hic-run", user_id="u1", status="running",
            started_at="2026-09-11T09:15:00+05:30", stopped_reason=None,
            state={"version": 1}, strategy_id="hic",
        )
        assert len(store.running_forwards()) == 2
        assert [r["session_id"] for r in store.running_forwards(LADDER)] == ["live-run"]
        assert [r["session_id"] for r in store.running_forwards("hic")] == ["hic-run"]
    finally:
        store.close()


def test_one_strategys_backtests_are_not_served_to_another(older_db):
    """The failure this prevents: opening the HIC dashboard and being shown the
    ladder's last run, with nothing saying so."""
    store = Store(older_db)
    try:
        store.save_backtest(
            run_id="bt-hic", user_id="u1", status="done", params={},
            dataset={"marker": "hic"}, result_version=3, strategy_id="hic",
        )
        store.save_backtest(
            run_id="bt-ladder", user_id="u1", status="done", params={},
            dataset={"marker": "ladder"}, result_version=3, strategy_id=LADDER,
        )
        ladder = store.latest_backtest("u1", min_version=3, strategy_id=LADDER)
        hic = store.latest_backtest("u1", min_version=3, strategy_id="hic")
        assert ladder["dataset"]["marker"] == "ladder"
        assert hic["dataset"]["marker"] == "hic"
    finally:
        store.close()
