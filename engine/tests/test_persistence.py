"""Tests for durable state, run resumption and the trading calendar.

The failure these exist to prevent: an engine restart used to log every user
out, discard every backtest, and kill running forward tests while their
positions were still notionally open. Nothing about that was visible in the
UI, which went on showing RUNNING.
"""

from __future__ import annotations

import datetime as dt

import pytest

from engine.config import IST
from engine.data.market_calendar import MarketCalendar, MarketStatus, _read_status
from engine.forward.runner import ForwardRunner
from engine.store.db import Store
from engine.strategy.condor import CondorStatus
from engine.tests.test_forward_fills import EXPIRY, FakeMarket, FakeMaster, cfg


@pytest.fixture()
def store(tmp_path) -> Store:
    db = Store(tmp_path / "engine.db")
    yield db
    db.close()


def _runner(store=None, session_id=None, user_id=None, **kw) -> ForwardRunner:
    market = FakeMarket(prices=kw.pop("prices", None), master=FakeMaster())
    return ForwardRunner(  # type: ignore[arg-type]
        market=market, strategy=cfg(**kw),
        store=store, session_id=session_id, user_id=user_id,
    )


# =================================================================== store


def test_a_saved_run_comes_back_after_the_process_is_gone(store, tmp_path):
    r = _runner(store=store, session_id="s1", user_id="u1")
    r.state_path = tmp_path / "live.json"
    r._open_condor(24_000, EXPIRY)
    r.save()

    # A brand-new Store on the same file is what a restart actually sees.
    reopened = Store(store.path)
    pending = reopened.running_forwards()
    assert [p["session_id"] for p in pending] == ["s1"]
    assert pending[0]["user_id"] == "u1"
    reopened.close()


def test_a_stopped_run_is_not_offered_for_resumption(store, tmp_path):
    r = _runner(store=store, session_id="s1", user_id="u1")
    r.state_path = tmp_path / "live.json"
    r.save()
    store.mark_stopped("s1", "stopped by user")
    assert store.running_forwards() == []


def test_ticks_come_back_oldest_first_so_a_chart_can_draw_them(store):
    base = dt.datetime(2026, 9, 8, 10, 0, tzinfo=IST)
    for i in range(5):
        store.record_tick("s1", (base + dt.timedelta(seconds=i * 5)).isoformat(), 24_000 + i)
    ticks = store.ticks("s1")
    assert [t["spot"] for t in ticks] == [24_000, 24_001, 24_002, 24_003, 24_004]


def test_tick_history_is_capped_from_the_newest_end(store):
    base = dt.datetime(2026, 9, 8, 10, 0, tzinfo=IST)
    for i in range(10):
        store.record_tick("s1", (base + dt.timedelta(seconds=i)).isoformat(), 100 + i)
    assert [t["spot"] for t in store.ticks("s1", limit=3)] == [107, 108, 109]


def test_the_user_id_salt_survives_reopening(store):
    """Otherwise every restart orphans that user's saved runs."""
    first = store.user_id_salt()
    reopened = Store(store.path)
    assert reopened.user_id_salt() == first
    reopened.close()


def test_a_backtest_outlives_the_process_that_ran_it(store):
    store.save_backtest(
        run_id="bt-1", user_id="u1", status="done",
        params={"days": 120}, dataset={"metrics": {"net_pnl": 1234.5}},
    )
    saved = store.latest_backtest("u1")
    assert saved and saved["dataset"]["metrics"]["net_pnl"] == 1234.5


def test_only_finished_backtests_are_served_as_the_latest(store):
    store.save_backtest(run_id="bt-1", user_id="u1", status="error",
                        params={}, error="Choice said no")
    assert store.latest_backtest("u1") is None
    assert store.backtest_history("u1")[0]["error"] == "Choice said no"


def test_learned_holidays_persist(store):
    store.add_holiday(dt.date(2026, 11, 8))
    assert dt.date(2026, 11, 8) in Store(store.path).holidays()


# ================================================================== resume


def test_state_round_trips_through_the_database(store, tmp_path):
    r = _runner(store=store, session_id="s1", user_id="u1")
    r.state_path = tmp_path / "live.json"
    r.expiry = EXPIRY
    r._open_condor(24_000, EXPIRY)
    r._open_condor(23_900, EXPIRY)
    r.ladder.anchor = 24_000.0
    r.ladder.last_level = 23_900.0
    r.ladder.fired_levels = {240, 239}
    r.realised = -1234.0
    r.save()

    saved = store.running_forwards()[0]["state"]
    restored = ForwardRunner.restore(
        saved, market=FakeMarket(master=FakeMaster()),  # type: ignore[arg-type]
        state_path=tmp_path / "live2.json",
    )

    assert restored.ladder.anchor == 24_000.0
    assert restored.ladder.last_level == 23_900.0
    assert restored.ladder.fired_levels == {240, 239}
    assert restored.realised == -1234.0
    assert restored.expiry == EXPIRY
    assert len(restored.condors) == 2
    assert len(restored.fills) == 8


def test_a_resumed_run_keeps_its_open_positions_and_their_prices(store, tmp_path):
    r = _runner(store=store, session_id="s1", user_id="u1")
    r.state_path = tmp_path / "live.json"
    original = r._open_condor(24_000, EXPIRY)
    assert original is not None
    r.save()

    restored = ForwardRunner.restore(
        store.running_forwards()[0]["state"],
        market=FakeMarket(master=FakeMaster()),  # type: ignore[arg-type]
        state_path=tmp_path / "live2.json",
    )
    condor = restored.condors[0]
    assert condor.status is CondorStatus.OPEN
    assert condor.level == original.level
    assert [fl.entry_price for fl in condor.legs] == [fl.entry_price for fl in original.legs]
    assert [fl.leg.side for fl in condor.legs] == [fl.leg.side for fl in original.legs]
    assert condor.credit == pytest.approx(original.credit)


def test_a_resumed_ladder_does_not_re_fire_a_level_it_already_holds(store, tmp_path):
    """The dangerous failure: resume, then open a second condor on the same
    strike because the fired set came back empty."""
    r = _runner(store=store, session_id="s1", user_id="u1", prices={26000: 24_000.0})
    r.state_path = tmp_path / "live.json"
    r.tick()                                   # anchors at 24,000 and opens it
    assert len(r.condors) == 1
    r.save()

    restored = ForwardRunner.restore(
        store.running_forwards()[0]["state"],
        market=FakeMarket(prices={26000: 24_000.0}, master=FakeMaster()),  # type: ignore[arg-type]
        state_path=tmp_path / "live2.json",
    )
    restored.tick()                            # same price, already-fired level
    assert len(restored.condors) == 1


def test_an_unknown_state_version_is_refused_rather_than_half_restored():
    with pytest.raises(ValueError):
        ForwardRunner.restore({"version": 99}, market=None)  # type: ignore[arg-type]


def test_saving_survives_a_broken_store(tmp_path):
    """A run must not stop trading because its disk did."""
    class Exploding:
        def save_forward(self, **kw):
            raise OSError("disk full")

    r = _runner(store=Exploding(), session_id="s1", user_id="u1")
    r.state_path = tmp_path / "live.json"
    r.save()                                   # must not raise
    assert r.state_path.exists()


# ================================================================ calendar


def test_weekends_are_not_trading_days():
    cal = MarketCalendar()
    assert not cal.is_trading_day(dt.date(2026, 9, 12))   # Saturday
    assert not cal.is_trading_day(dt.date(2026, 9, 13))   # Sunday
    assert cal.is_trading_day(dt.date(2026, 9, 8))        # Tuesday


def test_a_fixed_date_holiday_recurs_every_year():
    cal = MarketCalendar(recurring_fixed=("01-26", "08-15"))
    for year in (2026, 2027, 2030):
        assert cal.is_holiday(dt.date(year, 1, 26))
        assert cal.is_holiday(dt.date(year, 8, 15))


def test_the_shipped_holiday_file_loads():
    cal = MarketCalendar.load()
    assert cal.is_holiday(dt.date(2026, 1, 26))           # Republic Day
    assert not cal.is_holiday(dt.date(2026, 9, 8))


def test_a_missing_holiday_file_degrades_to_weekends_only(tmp_path):
    cal = MarketCalendar.load(tmp_path / "nope.json")
    assert cal.is_trading_day(dt.date(2026, 1, 26))       # unknown, not crashed
    assert not cal.is_trading_day(dt.date(2026, 9, 12))


def test_the_session_window_excludes_pre_open_and_post_close():
    cal = MarketCalendar()
    day = dt.date(2026, 9, 8)
    assert not cal.is_open(dt.datetime.combine(day, dt.time(9, 0), tzinfo=IST))
    assert cal.is_open(dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST))
    assert cal.is_open(dt.datetime.combine(day, dt.time(15, 30), tzinfo=IST))
    assert not cal.is_open(dt.datetime.combine(day, dt.time(15, 31), tzinfo=IST))


def test_a_learned_closure_is_reported_once_and_handed_to_the_caller():
    seen: list[dt.date] = []
    cal = MarketCalendar(on_learn=seen.append)
    day = dt.date(2026, 11, 9)                            # a Monday
    assert cal.record_closure(day) is True
    assert cal.record_closure(day) is False               # already known
    assert seen == [day]
    assert not cal.is_trading_day(day)


def test_a_weekend_is_never_learned_as_a_holiday():
    cal = MarketCalendar()
    assert cal.record_closure(dt.date(2026, 9, 12)) is False


def test_next_open_skips_the_weekend():
    cal = MarketCalendar()
    friday_evening = dt.datetime(2026, 9, 11, 16, 0, tzinfo=IST)
    assert cal.next_open(friday_evening).date() == dt.date(2026, 9, 14)   # Monday


def test_next_open_skips_a_holiday_too():
    cal = MarketCalendar(explicit={dt.date(2026, 9, 14)})
    friday_evening = dt.datetime(2026, 9, 11, 16, 0, tzinfo=IST)
    assert cal.next_open(friday_evening).date() == dt.date(2026, 9, 15)


# ------------------------------------------------------------ MarketStatus


class _StatusSession:
    def __init__(self, payload=None, raises=False):
        self.payload = payload
        self.raises = raises
        self.calls = 0

    def request(self, method, endpoint, data=None, **kw):
        self.calls += 1
        if self.raises:
            raise RuntimeError("no route to host")
        return self.payload


def test_an_unreachable_endpoint_defers_to_the_calendar_rather_than_guessing():
    status = MarketStatus(_StatusSession(raises=True), MarketCalendar())
    assert status.is_open() is None


def test_an_ambiguous_response_is_not_read_as_an_answer():
    assert _read_status({"Status": "Success", "Response": {"Market": "closed", "Next": "open"}}) is None


def test_a_clear_open_and_a_clear_close_are_both_understood():
    assert _read_status({"Response": [{"MarketStatus": "Open"}]}) is True
    assert _read_status({"Response": [{"MarketStatus": "Closed"}]}) is False


def test_the_status_answer_is_cached_rather_than_polled_every_tick():
    session = _StatusSession({"Response": [{"MarketStatus": "Open"}]})
    status = MarketStatus(session, MarketCalendar())
    for _ in range(5):
        status.is_open()
    assert session.calls == 1


def test_a_closure_during_session_hours_teaches_the_calendar():
    cal = MarketCalendar()
    session = _StatusSession({"Response": [{"MarketStatus": "Closed"}]})
    status = MarketStatus(session, cal)
    when = dt.datetime(2026, 11, 9, 11, 0, tzinfo=IST)     # a Monday, mid-session
    assert status.is_open(when) is False
    assert not cal.is_trading_day(dt.date(2026, 11, 9))


def test_a_closure_outside_session_hours_teaches_nothing():
    """Shut at 20:00 is just the evening, not a holiday."""
    cal = MarketCalendar()
    status = MarketStatus(_StatusSession({"Response": [{"MarketStatus": "Closed"}]}), cal)
    status.is_open(dt.datetime(2026, 11, 9, 20, 0, tzinfo=IST))
    assert cal.is_trading_day(dt.date(2026, 11, 9))


def test_restore_accepts_the_durable_wiring_the_api_passes(store, tmp_path):
    """A restored run must keep saving, or it is lost again on the next restart.

    This pins the call signature the API actually uses: a mismatch here failed
    only at runtime, during login, where it was swallowed as "could not
    resume" rather than surfacing as the plain TypeError it was.
    """
    r = _runner(store=store, session_id="s1", user_id="u1")
    r.state_path = tmp_path / "live.json"
    r._open_condor(24_000, EXPIRY)
    r.save()

    restored = ForwardRunner.restore(
        store.running_forwards()[0]["state"],
        market=FakeMarket(master=FakeMaster()),  # type: ignore[arg-type]
        state_path=tmp_path / "live2.json",
        store=store, session_id="s1", user_id="u1",
    )
    assert (restored.store, restored.session_id, restored.user_id) == (store, "s1", "u1")

    # And it really does write through: the resumed run stays resumable.
    restored._open_condor(23_900, EXPIRY)
    restored.save()
    assert len(store.running_forwards()[0]["state"]["condors"]) == 2


# ============================================== result versioning


def test_results_from_an_older_engine_are_not_served_as_current(store):
    """A correctness fix invalidates stored results, it does not just change
    future ones. Showing a stale dataset is worse than showing nothing: it
    looks current and is wrong."""
    store.save_backtest(run_id="old", user_id="u1", status="done",
                        params={}, dataset={"metrics": {}}, result_version=1)
    assert store.latest_backtest("u1", min_version=2) is None
    assert store.latest_backtest("u1", min_version=1) is not None


def test_a_current_result_is_served(store):
    store.save_backtest(run_id="new", user_id="u1", status="done",
                        params={}, dataset={"metrics": {"net_pnl": 1.0}}, result_version=2)
    saved = store.latest_backtest("u1", min_version=2)
    assert saved and saved["run_id"] == "new"


def test_a_database_from_an_earlier_build_gains_the_version_column(tmp_path):
    """CREATE TABLE IF NOT EXISTS leaves an existing table alone, so without a
    migration every read of the new column fails on an existing database."""
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE backtest_runs (run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,"
        " created_at TEXT NOT NULL, status TEXT NOT NULL, params_json TEXT NOT NULL,"
        " dataset_json TEXT, error TEXT);"
    )
    con.execute(
        "INSERT INTO backtest_runs VALUES ('a','u1','2026-01-01','done','{}','{\"m\":1}',NULL)"
    )
    con.commit()
    con.close()

    store = Store(path)
    try:
        # Pre-existing rows default to version 0, so they are retired, not shown.
        assert store.latest_backtest("u1", min_version=2) is None
        assert store.backtest_history("u1")[0]["run_id"] == "a"
    finally:
        store.close()


# ============================================ bounded history and safe resume


def test_tick_history_is_trimmed_rather_than_growing_forever(store):
    """prune_ticks existed but nothing called it, so the table grew for the
    life of the database -- ~150 MB per user per year at the fastest poll, for
    a chart that draws a few hundred points."""
    for i in range(120):
        store.record_tick("s1", f"2026-09-09T09:{i // 60:02d}:{i % 60:02d}+05:30", 24_000.0 + i)
    store.prune_ticks("s1", keep=50)
    kept = store.ticks("s1", limit=10_000)
    assert len(kept) == 50
    # The newest are the ones worth keeping.
    assert kept[-1]["spot"] == pytest.approx(24_119.0)


def test_pruning_one_run_leaves_another_alone(store):
    for i in range(30):
        store.record_tick("keep-me", f"2026-09-09T10:00:{i:02d}+05:30", 100.0 + i)
        store.record_tick("trim-me", f"2026-09-09T10:00:{i:02d}+05:30", 200.0 + i)
    store.prune_ticks("trim-me", keep=5)
    assert len(store.ticks("keep-me", limit=100)) == 30
    assert len(store.ticks("trim-me", limit=100)) == 5


def test_forward_history_does_not_drag_every_state_blob_off_disk(store):
    """It returns five scalar columns; SELECT * pulled tens of kilobytes of
    state_json per row on every login."""
    store.save_forward(session_id="s1", user_id="u1", status="running",
                       state={"blob": "x" * 50_000},
                       started_at="2026-09-09T09:15:00+05:30", stopped_reason=None)
    rows = store.forward_history("u1")
    assert rows and "state_json" not in rows[0]
    assert set(rows[0]) == {"session_id", "status", "started_at", "updated_at",
                            "stopped_reason"}


# ================================= marks survive a resume, and unknown != zero


def _runner_with_open_condor(tmp_path, store):
    """A runner holding one open condor with a known mark."""
    import datetime as dt

    from engine.forward.runner import ForwardRunner
    from engine.strategy.condor import (
        Condor, FilledLeg, PriceSource, StrategyConfig, build_legs,
    )

    cfg = StrategyConfig(lots=1, lot_size=65, strike_step=50.0)
    runner = ForwardRunner.__new__(ForwardRunner)
    ForwardRunner.__init__(
        runner, market=None, strategy=cfg,  # type: ignore[arg-type]
        state_path=tmp_path / "live.json", store=store, session_id="s1", user_id="u1",
    )
    legs = [FilledLeg(leg=leg, entry_price=100.0, source=PriceSource.CHOICE, token=900 + i)
            for i, leg in enumerate(build_legs(23_500.0, cfg))]
    runner.condors.append(Condor(
        level=23_500.0, entry_time=dt.datetime(2026, 9, 9, 10, 54),
        expiry=dt.date(2026, 9, 29), legs=legs, config=cfg, entry_costs=120.0, index=0,
    ))
    return runner


def test_an_unmarked_position_reads_as_unknown_not_zero(tmp_path, store):
    """A condor opened for Rs7,595 showed "+Rs0" overnight after an engine
    restart, because `last_mtm.get(index, 0.0)` renders "never marked" exactly
    like "worth nothing"."""
    runner = _runner_with_open_condor(tmp_path, store)
    snap = runner.snapshot()

    assert snap["positions"][0]["pnl"] is None, "an unmarked position must not claim zero"
    assert snap["pnl"]["unmarked_condors"] == 1
    assert snap["pnl"]["open_condors"] == 1


def test_a_marked_position_reports_its_mark(tmp_path, store):
    runner = _runner_with_open_condor(tmp_path, store)
    runner.last_mtm[0] = -1_933.45
    snap = runner.snapshot()

    assert snap["positions"][0]["pnl"] == pytest.approx(-1_933.45)
    assert snap["pnl"]["unmarked_condors"] == 0
    assert snap["pnl"]["unrealised"] == pytest.approx(-1_933.45)


def test_marks_survive_a_resume(tmp_path, store):
    """Outside market hours there is no next tick to recompute them, so a run
    resumed in the evening would sit at zero until the morning."""
    from engine.forward.runner import ForwardRunner

    runner = _runner_with_open_condor(tmp_path, store)
    runner.last_mtm[0] = -1_933.45
    state = runner.to_state()

    revived = ForwardRunner.restore(
        state, market=None, state_path=tmp_path / "live.json",  # type: ignore[arg-type]
        store=store, session_id="s1", user_id="u1",
    )
    assert revived.last_mtm.get(0) == pytest.approx(-1_933.45)
    assert revived.snapshot()["pnl"]["unrealised"] == pytest.approx(-1_933.45)
    assert revived.snapshot()["pnl"]["unmarked_condors"] == 0


def test_the_snapshot_publishes_a_mark_per_contract(tmp_path, store):
    """The fill log needs to answer "where is this leg now", which the
    per-condor MTM cannot: it is one number for four contracts."""
    runner = _runner_with_open_condor(tmp_path, store)
    runner.last_marks[900] = 149.73
    runner.last_marks[901] = 91.25

    marks = runner.snapshot()["marks"]
    assert marks["900"] == pytest.approx(149.73)
    assert marks["901"] == pytest.approx(91.25)
    # A contract never quoted must be absent, not zero -- the UI shows "--".
    assert "902" not in marks


def test_marks_per_contract_survive_a_resume(tmp_path, store):
    from engine.forward.runner import ForwardRunner

    runner = _runner_with_open_condor(tmp_path, store)
    runner.last_marks[900] = 149.73
    revived = ForwardRunner.restore(
        runner.to_state(), market=None, state_path=tmp_path / "l.json",  # type: ignore[arg-type]
        store=store, session_id="s1", user_id="u1",
    )
    assert revived.last_marks.get(900) == pytest.approx(149.73)


# ============================== a session ending suspends, it does not stop


def test_ending_a_session_suspends_the_run_rather_than_stopping_it(tmp_path, store):
    """A run whose session expired still holds positions and a ladder
    mid-flight. Marking it stopped made it unresumable, and the only way back
    was editing the database by hand."""
    runner = _runner_with_open_condor(tmp_path, store)
    runner.save()

    runner.suspend("session ended")

    assert runner.suspended is True
    assert runner.stopped_reason is None, "a suspended run has not stopped"

    runner.save()
    rows = [r for r in store.running_forwards() if r["session_id"] == "s1"]
    assert rows, "the run must still be marked running so a login resumes it"


def test_stopping_a_run_still_stops_it(tmp_path, store):
    """The distinction only helps if a real stop is still a real stop."""
    runner = _runner_with_open_condor(tmp_path, store)
    runner.stopped_reason = "stopped by user"
    runner.save()

    assert [r for r in store.running_forwards() if r["session_id"] == "s1"] == []


def test_a_suspended_run_round_trips_with_its_ladder_intact(tmp_path, store):
    """Resuming must not re-fire a level the run already holds."""
    from engine.forward.runner import ForwardRunner

    runner = _runner_with_open_condor(tmp_path, store)
    runner.ladder.on_price(23_500.0, dt.datetime(2026, 9, 9, 10, 54, tzinfo=IST))
    fired_before = sorted(runner.ladder.levels())
    runner.suspend()
    runner.save()

    revived = ForwardRunner.restore(
        runner.to_state(), market=None, state_path=tmp_path / "l.json",  # type: ignore[arg-type]
        store=store, session_id="s1", user_id="u1",
    )
    assert sorted(revived.ladder.levels()) == fired_before
    assert revived.suspended is False, "a restored run starts live, not suspended"
    assert revived.stopped_reason is None


# ============================== choosing which saved run to resume


def _row(session_id, started_at, *, open_condors=0, last_tick=None):
    """A forward_sessions row as running_forwards() hands it back."""
    condors = [{"status": "OPEN"} for _ in range(open_condors)]
    return {
        "session_id": session_id,
        "user_id": "u1",
        "started_at": started_at,
        "state": {"condors": condors, "last_tick": last_tick},
    }


def _winner(rows):
    from engine.api import _resume_rank

    return sorted(rows, key=_resume_rank)[-1]["session_id"]


def test_a_run_holding_positions_beats_an_empty_one_started_later():
    """The bug this exists for: an engine restart left a user with the run that
    had traded all day plus an empty one their browser minted seconds later.
    Choosing by started_at resumed the empty run and destroyed the real ladder,
    open condor and all."""
    real = _row("real", "2026-09-10T08:57:34", open_condors=1, last_tick="2026-09-10T15:29:57")
    empty = _row("empty", "2026-09-10T15:46:22")

    assert _winner([real, empty]) == "real"
    assert _winner([empty, real]) == "real", "the input order must not decide it"


def test_between_two_runs_with_positions_the_one_ticking_most_recently_wins():
    stale = _row("stale", "2026-09-10T09:00:00", open_condors=1, last_tick="2026-09-10T10:00:00")
    fresh = _row("fresh", "2026-09-10T08:00:00", open_condors=2, last_tick="2026-09-10T15:20:00")

    assert _winner([stale, fresh]) == "fresh"


def test_between_two_empty_runs_the_newest_wins():
    """With nothing at stake, the user's most recent intent is the best guess."""
    old = _row("old", "2026-09-10T09:00:00")
    new = _row("new", "2026-09-10T15:00:00")

    assert _winner([old, new]) == "new"


def test_a_run_that_never_ticked_does_not_outrank_one_that_did():
    """A missing last_tick must not sort above a real timestamp."""
    never = _row("never", "2026-09-10T16:00:00", open_condors=1)
    ticked = _row("ticked", "2026-09-10T08:00:00", open_condors=1, last_tick="2026-09-10T15:29:00")

    assert _winner([never, ticked]) == "ticked"


# ============================== when a trade actually happened


def _candle_runner(printed):
    """A runner whose quotes come from candles that printed at `printed`."""
    from engine.forward.runner import ForwardRunner
    from engine.tests.test_forward_fills import FakeMarket, FakeMaster, cfg

    market = FakeMarket(master=FakeMaster(), as_of=printed)
    return ForwardRunner(market=market, strategy=cfg())  # type: ignore[arg-type]


def test_a_fill_records_when_the_price_traded_not_when_we_read_it():
    """The complaint this comes from: the trade log showed the moment the
    engine noticed, so it disagreed with the chart the user was reading."""
    printed = dt.datetime(2026, 9, 10, 15, 1, tzinfo=IST)
    runner = _candle_runner(printed)

    runner._open_condor(23_400, EXPIRY)

    assert len(runner.fills) == 4
    for fill in runner.fills:
        assert fill.market_ts == printed.isoformat()
        assert fill.ts != fill.market_ts, "the engine clock is still recorded separately"


def test_a_live_touch_leaves_the_market_time_unset():
    """Nothing to reconcile when the quote is the current touch, and an
    invented second timestamp would only invite doubt."""
    runner = _candle_runner(None)

    runner._open_condor(23_400, EXPIRY)

    assert runner.fills
    assert all(f.market_ts is None for f in runner.fills)


def test_the_market_time_survives_a_resume():
    """Resuming rebuilds fills from stored dicts, and a field the restore path
    drops is a field the user stops seeing after any restart."""
    from engine.forward.runner import ForwardRunner

    printed = dt.datetime(2026, 9, 10, 15, 1, tzinfo=IST)
    runner = _candle_runner(printed)
    runner._open_condor(23_400, EXPIRY)

    revived = ForwardRunner.restore(
        runner.to_state(), market=runner.market, state_path=None,
    )

    assert [f.market_ts for f in revived.fills] == [printed.isoformat()] * 4


def test_a_fill_saved_before_this_existed_still_loads():
    """Old rows have no market_ts. Refusing them would strand every run that
    was open when this shipped."""
    from engine.forward.runner import Fill

    old = {
        "ts": "2026-09-10T10:54:35+05:30", "condor_index": 0, "condor_level": 23500.0,
        "expiry": "2026-09-29", "right": "PE", "side": "SELL", "strike": 23300.0,
        "qty": 65, "price": 117.05, "source": "choice", "mode": "paper",
        "token": 12345, "action": "OPEN",
    }
    assert Fill(**old).market_ts is None


def test_the_spot_timestamp_reaches_the_dashboard():
    """The rung fires off the spot, so when the spot printed is what explains
    a trigger the user did not expect at that moment."""
    printed = dt.datetime(2026, 9, 10, 15, 1, tzinfo=IST)
    runner = _candle_runner(printed)
    runner.tick()

    assert runner.snapshot()["market"]["as_of"] == printed.isoformat()


def test_the_spot_that_fired_the_rung_is_what_dates_the_trade():
    """Legs are quoted from the live book, so their own prices are current --
    but the rung fires off the index, which is served from candles. Dating the
    trade by the leg quote would make every condor look like it traded the
    instant the engine woke up."""
    printed = dt.datetime(2026, 9, 11, 9, 15, tzinfo=IST)
    runner = _candle_runner(None)          # legs quoted live, no quote time
    runner.last_spot_ts = printed

    runner._open_condor(23_400, EXPIRY)

    assert runner.fills
    assert all(f.market_ts == printed.isoformat() for f in runner.fills)


def test_two_rungs_fired_on_one_tick_both_carry_the_market_time():
    """The case that prompted this: a gap-down fired 23,400 and 23,300 a third
    of a second apart on the engine's clock, which is not when the market
    crossed them."""
    printed = dt.datetime(2026, 9, 11, 9, 15, tzinfo=IST)
    runner = _candle_runner(None)
    runner.last_spot_ts = printed

    runner._open_condor(23_400, EXPIRY)
    runner._open_condor(23_300, EXPIRY)

    engine_times = {f.ts for f in runner.fills}
    market_times = {f.market_ts for f in runner.fills}
    assert len(engine_times) == 2, "the engine clock still separates the two"
    assert market_times == {printed.isoformat()}, "the market crossed both at one print"
