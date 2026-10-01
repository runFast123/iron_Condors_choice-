"""Background jobs for the playground, kept apart from the backtest store.

A replay is a backtest and a plan is a few thousand simulated campaigns; both
take longer than a request should. They run on a worker thread and the page
polls. They are never saved as the user's backtest -- the dashboard's result
stays what the user last ran there -- and only the latest few per user are
kept, in memory.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from engine.config import IST

log = logging.getLogger(__name__)

#: Jobs kept per user; older ones are dropped.
KEEP_PER_USER = 12


@dataclass
class PlaygroundJob:
    job_id: str
    user_id: str
    kind: str                         # "replay" | "plan"
    params: dict[str, Any]
    status: str = "running"           # running | done | error
    progress: float = 0.0
    message: str = "Starting"
    error: str | None = None
    result: dict[str, Any] | None = None
    started_at: str = field(default_factory=lambda: dt.datetime.now(tz=IST).isoformat())
    finished_at: str | None = None

    def public(self, with_result: bool = True) -> dict[str, Any]:
        out = {
            "job_id": self.job_id, "kind": self.kind, "status": self.status,
            "progress": round(self.progress, 3), "message": self.message, "error": self.error,
            "started_at": self.started_at, "finished_at": self.finished_at, "params": self.params,
        }
        if with_result:
            out["result"] = self.result
        return out


class PlaygroundJobs:
    def __init__(self) -> None:
        self._jobs: dict[str, PlaygroundJob] = {}
        self._order: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def start(
        self, user_id: str, kind: str, params: dict[str, Any],
        work: Callable[[PlaygroundJob], dict[str, Any]],
    ) -> PlaygroundJob:
        job = PlaygroundJob(job_id=f"pg-{uuid.uuid4().hex[:16]}", user_id=user_id, kind=kind, params=params)
        with self._lock:
            self._jobs[job.job_id] = job
            order = self._order.setdefault(user_id, [])
            order.append(job.job_id)
            while len(order) > KEEP_PER_USER:
                self._jobs.pop(order.pop(0), None)

        def run() -> None:
            try:
                job.result = work(job)
                job.status, job.progress, job.message = "done", 1.0, "Done"
            except Exception as exc:                 # noqa: BLE001 - reported to the page
                log.error("Playground %s %s failed: %s\n%s", kind, job.job_id, exc, traceback.format_exc())
                job.status, job.error = "error", str(exc) or type(exc).__name__
            finally:
                job.finished_at = dt.datetime.now(tz=IST).isoformat()

        threading.Thread(target=run, name=f"playground-{kind}-{user_id}", daemon=True).start()
        return job

    def get(self, user_id: str, job_id: str) -> PlaygroundJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
        return job if job is not None and job.user_id == user_id else None


jobs = PlaygroundJobs()
