"""Tests for the backtest job pipeline.

This is the code every "Run backtest" click goes through -- fetch spot, derive
the expiry calendar, plan the legs, fetch them, replay, serialise, persist --
and it was the least-covered module in the project at 34%. A defect here does
not raise; it produces a dashboard full of confident wrong numbers.
"""

from __future__ import annotations

import datetime as dt
import time

import pandas as pd
import pytest

from engine.backtest.jobs import RESULT_VERSION, BacktestJob, BacktestRunner, JobStore
from engine.choice.errors import ChoiceError
from engine.choice.instruments import Contract
from engine.config import IST
from engine.store.db import Store

LOT = 65
TODAY = dt.date(2026, 9, 8)


def _frame(days: int, start_px: float = 24_000.0, drift: float = -12.0) -> pd.DataFrame:
    """A declining daily spot series — the ladder only fires on declines."""
    rows = []
    day = TODAY - dt.timedelta(days=days)
    n = 0
    while day <= TODAY:
        if day.weekday() < 5:
            px = start_px + drift * n
            rows.append({"ts": dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST),
                         "open": px, "high": px * 1.002, "low": px * 0.998,
                         "close": px, "volume": 1000, "oi": 10})
            n += 1
        day += dt.timedelta(days=1)
    return pd.DataFrame(rows)


class FakeMaster:
    def __init__(self, expiries=None, lot_size=LOT):
        self._expiries = expiries if expiries is not None else [
            TODAY, TODAY + dt.timedelta(days=7), TODAY + dt.timedelta(days=14)
        ]
        self._lot = lot_size

    def expiries(self, underlying, *, after=None):
        return [e for e in self._expiries if after is None or e >= after]

    def lot_size_for(self, underlying):
        return self._lot

    def strike_step(self, underlying, expiry):
        return 50.0

    def option(self, underlying, expiry, strike, right):
        return Contract(token=int(strike), segment_id=2, symbol="NIFTY", description="",
                        lot_size=self._lot, expiry=expiry, strike=strike,
                        option_type=right, underlying="NIFTY")


class FakeMarket:
    """Serves spot and VIX; option candles are absent so pricing falls to Black-76."""

    def __init__(self, days=90, spot=None, vix=None, option_frames=False, master=None):
        self._spot = _frame(days) if spot is None else spot
        self._vix = vix if vix is not None else {TODAY: 14.0}
        self._option_frames = option_frames
        self.master = master or FakeMaster()
        self.option_calls = 0

    def nifty(self, start, end, resolution):
        return self._spot

    def vix_by_date(self, start, end):
        return self._vix

    def option_candles(self, underlying, expiry, strike, right, start, end, resolution):
        self.option_calls += 1
        if not self._option_frames:
            return pd.DataFrame()
        px = 100.0
        return pd.DataFrame([{"ts": dt.datetime.combine(start, dt.time(9, 15), tzinfo=IST),
                              "open": px, "high": px, "low": px, "close": px,
                              "volume": 1, "oi": 1}])

    def coverage_summary(self):
        return {"fetches": self.option_calls, "ok": self.option_calls}

    def failures(self):
        return []


def params(**kw):
    return {"days": 90, "resolution": "D", "lots": 1, "step": 100.0,
            "max_condors": 20, **kw}


def run_job(market=None, **kw) -> BacktestJob:
    market = market or FakeMarket()
    job = BacktestJob(job_id="t-1", user_id="u1", params=params(**kw))
    BacktestRunner(market, job).run()
    return job


# ================================================================== happy path


def test_a_backtest_produces_a_complete_dashboard_dataset():
    job = run_job()
    assert job.status == "done", job.error
    d = job.result
    for key in ("metrics", "condors", "equity", "provenance"):
        assert key in d, f"dataset is missing {key}"
    assert d["condors"], "a declining path must open condors"
    assert d["equity"], "there must be an equity curve"


def test_progress_advances_monotonically_to_one():
    """The UI shows this as a progress bar; going backwards looks broken."""
    seen: list[float] = []
    market = FakeMarket()
    job = BacktestJob(job_id="t-2", user_id="u1", params=params())
    runner = BacktestRunner(market, job)
    original = runner._step

    def spy(stage, progress, message=""):
        seen.append(progress)
        return original(stage, progress, message)

    runner._step = spy  # type: ignore[method-assign]
    runner.run()
    assert job.status == "done", job.error
    assert seen == sorted(seen), f"progress went backwards: {seen}"
    assert seen[-1] == pytest.approx(1.0)


def test_every_condor_gets_an_expiry_from_its_own_era():
    """The regression that made weeklies price as six-month options."""
    job = run_job(days=120)
    assert job.status == "done", job.error
    for c in job.result["condors"]:
        opened = dt.date.fromisoformat(c["entry_time"][:10])
        expiry = dt.date.fromisoformat(c["expiry"])
        dte = (expiry - opened).days
        assert 0 <= dte <= 45, f"condor at {c['level']} got a {dte}-day expiry"


def test_a_credit_can_never_approach_the_wing_width():
    """An iron condor collecting most of its width means the pricing is wrong."""
    job = run_job()
    width = 200.0 * LOT
    for c in job.result["condors"]:
        assert c["credit"] < width * 0.85, (
            f"condor at {c['level']} collected {c['credit']:.0f} of a {width:.0f} wing"
        )


def test_provenance_says_where_the_prices_came_from():
    job = run_job()
    prov = job.result["provenance"]
    assert prov["premium_source"].startswith("modeled"), (
        "no option candles were served, so premiums must be badged as modelled"
    )
    assert prov["expiry_source"]
    assert "expiries_derived" in prov


def test_real_option_candles_are_preferred_over_the_model():
    job = run_job(market=FakeMarket(option_frames=True))
    assert job.status == "done", job.error
    assert job.result["provenance"]["premium_source"] == "choice:ChartData"


# ==================================================================== failures


def test_no_spot_data_is_an_explained_error_not_a_crash():
    job = run_job(market=FakeMarket(spot=pd.DataFrame()))
    assert job.status == "error"
    assert "no NIFTY spot data" in job.error
    assert "shorter range" in job.error, "the message must say what to do about it"


def test_an_unexpected_exception_is_captured_with_its_type():
    """A worker thread that dies silently leaves the UI spinning forever."""
    class Exploding(FakeMarket):
        def vix_by_date(self, start, end):
            raise RuntimeError("kaboom")

    job = run_job(market=Exploding())
    assert job.status == "error"
    assert "RuntimeError" in job.error and "kaboom" in job.error
    assert job.finished_at, "a failed job must still be marked finished"


def test_a_broker_error_keeps_choices_own_wording():
    class Refusing(FakeMarket):
        def nifty(self, start, end, resolution):
            raise ChoiceError("Token not subscribed")

    job = run_job(market=Refusing())
    assert job.status == "error" and "Token not subscribed" in job.error


def test_a_market_with_no_expiries_at_all_fails_cleanly():
    job = run_job(market=FakeMarket(master=FakeMaster(expiries=[])))
    # Derivation covers the range even with nothing listed, so this must either
    # succeed on a derived calendar or fail with a sentence, never crash.
    assert job.status in ("done", "error")
    if job.status == "error":
        assert job.error and not job.error.startswith("Traceback")


# =================================================================== JobStore


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "jobs.db")
    yield db
    db.close()


def _wait(js: JobStore, user="u1", timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = js.get(user)
        if job and job.status in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("job never finished")


def test_a_finished_run_is_persisted_and_survives_a_new_store(store):
    js = JobStore(store)
    js.start(FakeMarket(), "u1", params())
    job = _wait(js)
    assert job.status == "done", job.error

    fresh = JobStore(store)                    # as if the engine restarted
    assert fresh.dataset("u1")["condors"], "the saved result did not come back"


def test_one_user_cannot_see_another_users_result(store):
    js = JobStore(store)
    js.start(FakeMarket(), "alice", params())
    _wait(js, "alice")
    assert js.dataset("bob")["condors"] == []
    assert js.get("bob") is None


def test_starting_while_a_run_is_in_flight_returns_the_same_job(store):
    js = JobStore(store)
    first = js.start(FakeMarket(days=200), "u1", params(days=200))
    second = js.start(FakeMarket(days=200), "u1", params(days=200))
    assert first.job_id == second.job_id, "a double click must not start two runs"
    _wait(js)


def test_a_second_run_replaces_the_first_result(store):
    """The complaint that the dashboard felt static: one run and no more."""
    js = JobStore(store)
    js.start(FakeMarket(days=60), "u1", params(days=60))
    _wait(js)
    first = len(js.dataset("u1")["condors"])

    js.start(FakeMarket(days=200), "u1", params(days=200, step=200.0))
    _wait(js)
    second = len(js.dataset("u1")["condors"])
    assert second != first, "the second run did not replace the first"
    assert len(store.backtest_history("u1")) == 2


def test_clear_removes_the_result_and_leaves_an_explanation(store):
    js = JobStore(store)
    js.start(FakeMarket(), "u1", params())
    _wait(js)
    js.clear("u1")
    bundle = js.dataset("u1")
    assert bundle["condors"] == []
    assert bundle["provenance"]["note"], "an empty dashboard must say why it is empty"


def test_a_result_from_an_older_engine_is_not_shown(store):
    """A correctness fix invalidates stored results; showing them anyway is worse
    than showing nothing, because they look current."""
    store.save_backtest(run_id="ancient", user_id="u1", status="done",
                        params={}, dataset={"condors": [{"level": 1}]},
                        result_version=RESULT_VERSION - 1)
    bundle = JobStore(store).dataset("u1")
    assert bundle["condors"] == []
    assert "correctness fix" in bundle["provenance"]["note"]


def test_no_database_still_serves_the_in_memory_result():
    """The engine must work before storage is bound."""
    js = JobStore(None)
    js.start(FakeMarket(), "u1", params())
    _wait(js)
    assert js.dataset("u1")["condors"]


def test_history_lists_runs_newest_first(store):
    js = JobStore(store)
    for days in (60, 90):
        js.start(FakeMarket(days=days), "u1", params(days=days))
        _wait(js)
    rows = js.history("u1")
    assert len(rows) == 2
    assert rows[0]["created_at"] >= rows[1]["created_at"]
