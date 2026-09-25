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

    def nifty(self, start, end, resolution, strict=True):
        return self._spot

    def vix_by_date(self, start, end):
        return self._vix

    def india_vix(self, start, end, resolution, strict=True):
        """Intraday VIX, for the entry rule: `vix_bars` if given, else none."""
        return getattr(self, "vix_bars", pd.DataFrame())

    def option_candles(
        self, underlying, expiry, strike, right, start, end, resolution, instruments=None
    ):
        self.option_calls += 1
        if not self._option_frames:
            return pd.DataFrame()
        px = 100.0
        if self._option_frames == "first-bar-only":
            # Bars that exist but never sit near a moment the run needs them.
            stamps = [dt.datetime.combine(start, dt.time(9, 15), tzinfo=IST)]
        else:
            # One bar at every spot bar, so every request finds a fresh print.
            stamps = list(self._spot["ts"])
        return pd.DataFrame([{"ts": ts, "open": px, "high": px, "low": px, "close": px,
                              "volume": 1, "oi": 1} for ts in stamps])

    def coverage_summary(self, since=0):
        return {"fetches": self.option_calls, "ok": self.option_calls}

    def failures(self, since=0):
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
        def nifty(self, start, end, resolution, strict=True):
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


# ============================================== modelled-IV term structure


def test_the_term_structure_assumption_is_recorded_on_every_run():
    """Every reported number rests on this, so a run must say which
    assumption produced it."""
    job = run_job()
    assert job.result["provenance"]["term_exponent"] == 0.0
    assert "flat-term" in job.result["provenance"]["vol_source"]


def test_a_term_slope_lifts_short_dated_credit_and_is_labelled():
    """Flat is not neutral: it prices a 1-DTE weekly off the 30-day VIX.

    At NIFTY 24,000 with VIX 14 that is Rs1,372 of modelled condor credit
    against Rs6,020 at -0.25 -- on a strategy whose entire P&L is the credit.
    """
    flat = run_job()
    sloped = run_job(term_exponent=-0.25)
    assert flat.status == "done" and sloped.status == "done"

    def mean_credit(job):
        condors = job.result["condors"]
        return sum(c["credit"] for c in condors) / len(condors)

    assert mean_credit(sloped) > mean_credit(flat)
    prov = sloped.result["provenance"]
    assert prov["term_exponent"] == -0.25
    assert "term^-0.25" in prov["vol_source"]


# ==================================== expiry cadence and surface provenance


def test_a_monthly_run_uses_month_end_contracts():
    """`expiry_cadence` was read by this runner all along and never sent, so
    every run silently used weeklies."""
    weekly = run_job(days=120, expiry_cadence="weekly")
    monthly = run_job(days=120, expiry_cadence="monthly")
    assert weekly.status == "done" and monthly.status == "done", monthly.error

    def expiries(job):
        return sorted({c["expiry"] for c in job.result["condors"]})

    assert len(expiries(monthly)) < len(expiries(weekly))

    # The property is "last contract of its month", not a day-of-month
    # threshold: which date that is depends on the exchange calendar.
    weeklies = [dt.date.fromisoformat(e) for e in expiries(weekly)]
    for iso in expiries(monthly):
        expiry = dt.date.fromisoformat(iso)
        later_same_month = [
            w for w in weeklies
            if (w.year, w.month) == (expiry.year, expiry.month) and w > expiry
        ]
        assert not later_same_month, f"{expiry} is not the last contract of its month"


def test_a_monthly_condor_is_held_longer_than_a_weekly_one():
    """The reason the cadence matters: offsetting needs several condors alive
    in one expiry, and a weekly settles before the ladder gets that deep."""
    def mean_dte(job):
        d = [(dt.date.fromisoformat(c["expiry"]) - dt.date.fromisoformat(c["entry_time"][:10])).days
             for c in job.result["condors"]]
        return sum(d) / len(d)

    assert mean_dte(run_job(days=120, expiry_cadence="monthly")) > mean_dte(
        run_job(days=120, expiry_cadence="weekly")
    )


def test_every_run_names_the_surface_that_produced_it():
    prov = run_job().result["provenance"]
    assert "iv_calibration" in prov
    assert "chain-fit" in prov["vol_source"] or "flat-term" in prov["vol_source"]


def test_a_stored_chain_fit_is_applied_and_credited(store):
    """Without a fit the surface is flat, which prices a 1-DTE weekly off the
    30-day VIX."""
    store.save_calibration(
        dt.date.today().isoformat(),
        {"atm_vol": 0.15, "slope": -2.4, "curvature": 55.0,
         "term_exponent": -0.28, "observations": 96},
    )
    job = BacktestJob(job_id="cal-1", user_id="u1", params=params())
    BacktestRunner(FakeMarket(), job, db=store).run()
    assert job.status == "done", job.error

    prov = job.result["provenance"]
    assert "chain-fit" in prov["vol_source"]
    assert "term^-0.28" in prov["vol_source"]
    assert prov["iv_calibration"]["observations"] == 96
    assert prov["term_exponent"] == pytest.approx(-0.28)


def test_a_stale_fit_is_ignored_rather_than_trusted(store):
    """A surface measured a month ago describes a market that has moved."""
    old = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    store.save_calibration(old, {"atm_vol": 0.15, "slope": -2.4, "curvature": 55.0,
                                 "term_exponent": -0.28, "observations": 96})
    job = BacktestJob(job_id="cal-2", user_id="u1", params=params())
    BacktestRunner(FakeMarket(), job, db=store).run()
    assert job.status == "done", job.error
    assert "flat-term" in job.result["provenance"]["vol_source"]
    assert job.result["provenance"]["iv_calibration"] is None


# ============================== one user's run is not another's


def test_a_backtest_row_never_changes_owner(store):
    """The run id was a process-local counter, so the first backtest after
    every restart was "bt-1" -- and the upsert is keyed on it. One user's run
    overwrote another's stored row while keeping the original owner, so user A
    opened the dashboard and was served user B's backtest."""
    db = store

    db.save_backtest(run_id="bt-1", user_id="userA", status="done",
                     params={"days": 180}, dataset={"whose": "userA"},
                     result_version=RESULT_VERSION)
    db.save_backtest(run_id="bt-1", user_id="userB", status="done",
                     params={"days": 30}, dataset={"whose": "userB"},
                     result_version=RESULT_VERSION)

    mine = db.latest_backtest("userA", min_version=RESULT_VERSION)
    assert mine is not None
    assert mine["dataset"] == {"whose": "userA"}, "served another user's result"

    # B's write was refused rather than misfiled, so B simply has nothing yet.
    assert db.latest_backtest("userB", min_version=RESULT_VERSION) is None


def test_two_runs_by_one_user_both_survive(store):
    """Every re-run overwrote the previous row, so the history could never
    grow and the stored date stayed at the first run's -- a result computed
    today was dated to whenever the counter last started from zero."""
    db = store

    db.save_backtest(run_id="bt-aaa", user_id="u", status="done",
                     params={"days": 180}, dataset={"n": 1},
                     result_version=RESULT_VERSION,
                     created_at="2026-09-08T10:00:00+00:00")
    db.save_backtest(run_id="bt-bbb", user_id="u", status="done",
                     params={"days": 30}, dataset={"n": 2},
                     result_version=RESULT_VERSION,
                     created_at="2026-09-16T10:00:00+00:00")

    assert len(db.backtest_history("u")) == 2
    assert db.latest_backtest("u", min_version=RESULT_VERSION)["dataset"] == {"n": 2}


def test_job_ids_are_unique_across_engine_restarts(store):
    """Two JobStores stand in for two engine sessions. A counter restarted at
    zero in each and handed out the same id twice -- so the second user's run
    landed in the first user's row."""
    first = JobStore(store)
    a = first.start(FakeMarket(), "alice", params())
    _wait(first, "alice")

    restarted = JobStore(store)            # as if the engine had been restarted
    b = restarted.start(FakeMarket(), "bob", params())
    _wait(restarted, "bob")

    assert a.job_id != b.job_id
    assert store.latest_backtest("alice", min_version=RESULT_VERSION) is not None
    assert store.latest_backtest("bob", min_version=RESULT_VERSION) is not None
    assert len(store.backtest_history("alice")) == 1
    assert len(store.backtest_history("bob")) == 1


# ============================== why a leg was modelled


def test_a_leg_choice_resolves_but_will_not_serve_is_reported(monkeypatch):
    """Choice answers a settled option contract with an empty series rather
    than an error. That fell through `if not frame.empty` with nothing logged
    and nothing recorded, so the only symptom was a MODELED percentage with no
    way to account for it -- and "Choice served nothing" looked exactly like
    "the contract could not be found"."""
    market = FakeMarket(option_frames=True)
    served = market.option_candles

    def puts_come_back_empty(underlying, expiry, strike, right, start, end, resolution, **kw):
        if right == "PE":
            market.option_calls += 1
            return pd.DataFrame()
        return served(underlying, expiry, strike, right, start, end, resolution, **kw)

    monkeypatch.setattr(market, "option_candles", puts_come_back_empty)
    job = run_job(market=market)
    assert job.status == "done", job.error

    prov = job.result["provenance"]
    assert prov["legs_empty"] > 0, "empty legs must be counted"
    assert prov["legs_real"] > 0, "the calls still priced from real candles"
    assert prov["legs_unresolved"] == 0, "nothing failed to resolve"
    assert prov["legs_total"] == (prov["legs_real"] + prov["legs_unused"]
                                  + prov["legs_empty"] + prov["legs_unresolved"])
    assert prov["empty_expiries"], "and the expiries are named"


def test_a_fully_served_run_reports_no_empty_legs():
    job = run_job(market=FakeMarket(option_frames=True))

    prov = job.result["provenance"]
    assert prov["legs_empty"] == 0
    assert prov["empty_expiries"] == []
    assert prov["legs_real"] == prov["legs_total"]
    assert prov["legs_unused"] == 0


def test_a_run_choice_serves_nothing_for_says_so_rather_than_only_modelling():
    """The default fake serves no candles at all -- the shape of a backtest
    over settled expiries."""
    job = run_job(market=FakeMarket())

    prov = job.result["provenance"]
    assert prov["legs_real"] == 0
    assert prov["legs_empty"] == prov["legs_total"] > 0
    assert prov["premium_source"] == "modeled:black76"


def test_bars_that_never_match_a_request_are_not_counted_as_real():
    """The report used to count a leg as "priced from real candles" as soon as
    its fetch returned any bars. On 23 Sep it said 52 of 80 legs were real;
    the 20 August legs among them had not priced a single quote, because none
    of their bars sat within fifteen minutes of a moment they were needed."""
    job = run_job(market=FakeMarket(option_frames="first-bar-only"))
    assert job.status == "done", job.error
    prov = job.result["provenance"]

    assert prov["legs_unused"] > 0
    assert prov["legs_real"] < prov["legs_total"] - prov["legs_empty"]
    detail = prov["unused_legs"][0]
    for field in ("expiry", "strike", "right", "bars", "first_bar", "last_bar", "first_needed"):
        assert field in detail, field
    assert prov["legs_total"] == (prov["legs_real"] + prov["legs_unused"]
                                  + prov["legs_empty"] + prov["legs_unresolved"])


def test_coverage_describes_this_run_not_the_whole_session(store):
    """The fetch reports belong to the session, so every backtest reported
    the fetches of every earlier run too."""
    market = FakeMarket(option_frames=True)
    market.reports = ["an earlier run's fetch"] * 7          # already in the session

    seen = {}
    real_summary = market.coverage_summary

    def recording(since=0):
        seen["since"] = since
        return real_summary(since)

    market.coverage_summary = recording
    job = run_job(market=market)
    assert job.status == "done", job.error
    assert seen["since"] == 7, "the job must count from where it started"
