"""Durable state for the engine.

Everything the engine knew used to live in process memory, so a restart logged
every user out, discarded every backtest, and killed running forward tests
while the user's browser still showed them as live. SQLite fixes that without
adding a service to run or a credential to leak: one file on the engine box,
written by the tick thread and read by the API thread.

Two deliberate choices:

* **Run state is stored as one JSON document, not a relational spread.** The
  thing being persisted is a resumable snapshot of a running strategy, and
  splitting it across five tables would buy queries nobody makes at the cost of
  a rehydration path that can half-fail. Ticks are separate because they *are*
  queried as a series.
* **No credential ever lands here.** API keys, session ids and access tokens
  stay in memory and die with the process, which is the one thing that should.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

# Roughly a full trading day at the fastest allowed poll, which is far more
# than the live chart draws and enough to rebuild it after a reload.
TICKS_KEPT = 5_000

DEFAULT_PATH = Path(os.environ.get("ENGINE_DB", "engine/state/engine.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS forward_sessions (
    session_id     TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    status         TEXT NOT NULL,          -- running | stopped
    started_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    stopped_reason TEXT,
    state_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_forward_user ON forward_sessions (user_id, status);

CREATE TABLE IF NOT EXISTS forward_ticks (
    session_id TEXT NOT NULL,
    ts         TEXT NOT NULL,
    spot       REAL NOT NULL,
    PRIMARY KEY (session_id, ts)
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id       TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL,
    params_json  TEXT NOT NULL,
    dataset_json TEXT,
    error        TEXT,
    result_version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_backtest_user ON backtest_runs (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS iv_calibrations (
    as_of_date   TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_holidays (
    day         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


def _now() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).isoformat()


class Store:
    """A small, thread-safe SQLite wrapper.

    The runner's worker thread writes on every tick while the API thread reads,
    so the connection is shared with ``check_same_thread=False`` and every
    statement is serialised through one lock. WAL keeps a reader from blocking
    behind the writer, which is what makes a snapshot save invisible to the UI.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or DEFAULT_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns to a database created by an earlier build.

        CREATE TABLE IF NOT EXISTS leaves an existing table alone, so a new
        column has to be added explicitly or every read of it fails.
        """
        existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(backtest_runs)")}
        if "result_version" not in existing:
            self._conn.execute(
                "ALTER TABLE backtest_runs ADD COLUMN result_version INTEGER NOT NULL DEFAULT 0"
            )
            log.info("Added result_version to backtest_runs")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _write(self, sql: str, params: Iterable = ()) -> None:
        with self._lock:
            self._conn.execute(sql, tuple(params))
            self._conn.commit()

    def _rows(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)))

    # ------------------------------------------------------- forward runs

    def save_forward(
        self,
        *,
        session_id: str,
        user_id: str,
        status: str,
        started_at: str,
        stopped_reason: str | None,
        state: dict[str, Any],
    ) -> None:
        self._write(
            """
            INSERT INTO forward_sessions
                (session_id, user_id, status, started_at, updated_at, stopped_reason, state_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                status=excluded.status,
                updated_at=excluded.updated_at,
                stopped_reason=excluded.stopped_reason,
                state_json=excluded.state_json
            """,
            (session_id, user_id, status, started_at, _now(), stopped_reason,
             json.dumps(state, separators=(",", ":"))),
        )

    def running_forwards(self) -> list[dict[str, Any]]:
        """Every run that was live when the engine last stopped.

        These are what a restart has to pick back up. A run is not "finished"
        just because the process that was driving it went away.
        """
        out = []
        for row in self._rows(
            "SELECT * FROM forward_sessions WHERE status = 'running' ORDER BY started_at"
        ):
            try:
                state = json.loads(row["state_json"])
            except json.JSONDecodeError:
                log.warning("Forward session %s has unreadable state; skipping", row["session_id"])
                continue
            out.append({
                "session_id": row["session_id"],
                "user_id": row["user_id"],
                "started_at": row["started_at"],
                "state": state,
            })
        return out

    def forward_history(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                "session_id": r["session_id"], "status": r["status"],
                "started_at": r["started_at"], "updated_at": r["updated_at"],
                "stopped_reason": r["stopped_reason"],
            }
            for r in self._rows(
                # Projected, not SELECT *: state_json is tens of kilobytes per
                # row and none of it is used here.
                "SELECT session_id, status, started_at, updated_at, stopped_reason "
                "FROM forward_sessions WHERE user_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (user_id, limit),
            )
        ]

    def mark_stopped(self, session_id: str, reason: str) -> None:
        self._write(
            "UPDATE forward_sessions SET status='stopped', stopped_reason=?, updated_at=? "
            "WHERE session_id=?",
            (reason, _now(), session_id),
        )

    # -------------------------------------------------------------- ticks

    def record_tick(self, session_id: str, ts: str, spot: float) -> None:
        self._write(
            "INSERT OR REPLACE INTO forward_ticks (session_id, ts, spot) VALUES (?, ?, ?)",
            (session_id, ts, float(spot)),
        )

    def ticks(self, session_id: str, limit: int = 900) -> list[dict[str, Any]]:
        """The most recent ticks, oldest first.

        The live chart used to hold its history only in the browser, so every
        reload redrew from an empty series. Serving it from here means a
        refresh -- or a second device -- sees the whole session.
        """
        rows = self._rows(
            "SELECT ts, spot FROM forward_ticks WHERE session_id = ? "
            "ORDER BY ts DESC LIMIT ?",
            (session_id, limit),
        )
        return [{"ts": r["ts"], "spot": r["spot"]} for r in reversed(rows)]

    def prune_ticks(self, session_id: str, keep: int = TICKS_KEPT) -> None:
        """Keep only the most recent ``keep`` ticks for a run.

        Without this the table grows for the life of the database: at the
        five-second floor that is ~4,500 rows a day per user and about 150 MB a
        year, for a chart that never shows more than the last few hundred
        points.
        """
        self._write(
            "DELETE FROM forward_ticks WHERE session_id = ? AND ts NOT IN "
            "(SELECT ts FROM forward_ticks WHERE session_id = ? ORDER BY ts DESC LIMIT ?)",
            (session_id, session_id, keep),
        )

    # -------------------------------------------------- IV calibration

    def save_calibration(self, as_of_date: str, payload: dict[str, Any]) -> None:
        """Store the day's fitted surface.

        Market-wide rather than per user: it is the shape of the public option
        chain, identical for everyone, and fitting it costs a burst of quote
        requests nobody should pay twice. Keyed by date so a new session
        naturally refits.
        """
        self._write(
            "INSERT INTO iv_calibrations (as_of_date, created_at, payload_json) "
            "VALUES (?, ?, ?) ON CONFLICT(as_of_date) DO UPDATE SET "
            "created_at=excluded.created_at, payload_json=excluded.payload_json",
            (as_of_date, _now(), json.dumps(payload, separators=(",", ":"))),
        )

    def latest_calibration(self, *, not_before: str | None = None) -> dict[str, Any] | None:
        """The most recent fit, optionally no older than ``not_before``.

        A surface goes stale: a fit from three weeks ago describes a market
        that has moved. The caller decides how old is too old, because a stale
        measurement is still better than an unmeasured guess -- but only if it
        is labelled as stale.
        """
        sql = "SELECT * FROM iv_calibrations"
        params: tuple = ()
        if not_before:
            sql += " WHERE as_of_date >= ?"
            params = (not_before,)
        sql += " ORDER BY as_of_date DESC LIMIT 1"
        rows = self._rows(sql, params)
        if not rows:
            return None
        row = rows[0]
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None
        return {
            "as_of_date": row["as_of_date"],
            "created_at": row["created_at"],
            **payload,
        }

    # ----------------------------------------------------------- backtests

    def save_backtest(
        self,
        *,
        run_id: str,
        user_id: str,
        status: str,
        params: dict[str, Any],
        dataset: dict[str, Any] | None = None,
        error: str | None = None,
        created_at: str | None = None,
        result_version: int = 0,
    ) -> None:
        self._write(
            """
            INSERT INTO backtest_runs
                (run_id, user_id, created_at, status, params_json, dataset_json, error,
                 result_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status=excluded.status,
                dataset_json=excluded.dataset_json,
                error=excluded.error,
                result_version=excluded.result_version
            """,
            (run_id, user_id, created_at or _now(), status,
             json.dumps(params, separators=(",", ":")),
             json.dumps(dataset, separators=(",", ":")) if dataset is not None else None,
             error, result_version),
        )

    def latest_backtest(self, user_id: str, *, min_version: int = 0) -> dict[str, Any] | None:
        """The newest finished run this engine still considers valid.

        ``min_version`` exists because a correctness fix does not just change
        future results, it invalidates stored ones. Serving a saved dataset
        computed by a materially different engine is worse than serving
        nothing: it looks current and is wrong.
        """
        rows = self._rows(
            "SELECT * FROM backtest_runs WHERE user_id = ? AND status = 'done' "
            "AND result_version >= ? ORDER BY created_at DESC LIMIT 1",
            (user_id, min_version),
        )
        if not rows:
            return None
        row = rows[0]
        try:
            dataset = json.loads(row["dataset_json"]) if row["dataset_json"] else None
        except json.JSONDecodeError:
            log.warning("Backtest %s has an unreadable dataset", row["run_id"])
            return None
        return {"run_id": row["run_id"], "created_at": row["created_at"], "dataset": dataset}

    def backtest_history(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                "run_id": r["run_id"], "created_at": r["created_at"], "status": r["status"],
                "params": json.loads(r["params_json"]), "error": r["error"],
            }
            for r in self._rows(
                "SELECT run_id, created_at, status, params_json, error FROM backtest_runs "
                "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            )
        ]

    def clear_backtests(self, user_id: str) -> None:
        self._write("DELETE FROM backtest_runs WHERE user_id = ?", (user_id,))

    # ---------------------------------------------------------------- meta

    def get_meta(self, key: str) -> str | None:
        rows = self._rows("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def user_id_salt(self) -> bytes:
        """A salt that survives restarts, minted once on first use.

        User ids are a salted hash of the mobile number. With a per-process
        salt they change on every restart, which silently orphans that user's
        saved runs and backtests -- the data is still there, under an id that
        no longer resolves. Persisting the salt is what makes resume work.
        """
        existing = self.get_meta("user_id_salt")
        if existing:
            return bytes.fromhex(existing)
        salt = secrets.token_bytes(16)
        self.set_meta("user_id_salt", salt.hex())
        return salt

    # ------------------------------------------------------------ holidays

    def holidays(self) -> set[dt.date]:
        out: set[dt.date] = set()
        for row in self._rows("SELECT day FROM market_holidays"):
            try:
                out.add(dt.date.fromisoformat(row["day"]))
            except ValueError:
                continue
        return out

    def add_holiday(self, day: dt.date, source: str = "marketstatus") -> None:
        self._write(
            "INSERT OR IGNORE INTO market_holidays (day, source, recorded_at) VALUES (?, ?, ?)",
            (day.isoformat(), source, _now()),
        )
