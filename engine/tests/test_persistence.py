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
