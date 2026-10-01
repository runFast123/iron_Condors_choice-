"""The playground: replays of settled campaigns, and simulated plans."""

from __future__ import annotations

import datetime as dt

import pytest

from engine.config import IST
from engine.playground import replay, simulate
from engine.strategy.condor import StrategyConfig
from engine.tests.test_parallel_runs import registry  # noqa: F401 - a fixture

SETTINGS = {
    "strategy": "ladder", "name": "ladder", "bar_minutes": 1, "expiry_cadence": "monthly", "lots": 1,
    "step": 100.0, "short_offset": 200.0, "long_offset": 400.0, "max_condors": 20, "direction": "down",
    "anchor_mode": None, "max_down": None, "max_up": None, "max_entry_vix": None, "min_entry_dte": None,
    "min_credit_ratio": None, "take_profit": None, "stop_loss": None, "daily_loss_limit": 25_000.0,
}
CAMPAIGN = {"expiry": "2026-09-29", "started_at": "2026-09-10T09:15:36+05:30", "status": "settled"}


# =============================================================== replay


def test_a_replay_is_the_campaigns_own_window_on_its_own_expiry():
    params = replay.backtest_params(SETTINGS, CAMPAIGN)
    assert params["window"] == {"start_at": CAMPAIGN["started_at"], "expiry": "2026-09-29"}
    assert params["roll"] is False and params["resolution"] == "1" and params["option_resolution"] == "1"
    assert params["short_offset"] == 200.0 and params["long_offset"] == 400.0


def test_only_editable_settings_change():
    merged = replay.merged_settings(SETTINGS, {"max_down": 5, "long_offset": 600, "strategy": "hic", "name": "x"})
    assert merged["max_down"] == 5 and merged["long_offset"] == 600
    assert merged["strategy"] == "ladder" and merged["name"] == "ladder", "identity is not editable"


def test_a_replay_window_starts_when_the_campaign_did():
    """A campaign that began at 13:00 must not be replayed from 09:15."""
    from engine.backtest.jobs import BacktestJob, BacktestRunner
    from engine.tests.test_jobs import FakeMarket, TODAY

    start = dt.datetime.combine(TODAY - dt.timedelta(days=10), dt.time(9, 15), tzinfo=IST)
    expiry = TODAY + dt.timedelta(days=5)
    params = replay.backtest_params({**SETTINGS, "bar_minutes": 1}, {
        "expiry": expiry.isoformat(), "started_at": start.isoformat(), "status": "settled",
    })
    params["resolution"] = "D"
    params["option_resolution"] = None
    job = BacktestJob(job_id="w", user_id="u1", params=params)
    BacktestRunner(FakeMarket(), job).run()
    assert job.status == "done", job.error
    assert job.result["provenance"]["range"][0] >= start.date().isoformat()
    assert {c["expiry"] for c in job.result["condors"]} <= {expiry.isoformat()}
    assert not any("No expiry available" in w for w in job.result["warnings"])


# ============================================================ simulation


def _sessions(n=200, seed=1):
    """Synthetic real-looking sessions: 25 bars, small moves, VIX 14%."""
    import random

    rng = random.Random(seed)
    out = []
    day = dt.date(2025, 1, 1)
    for i in range(n):
        returns = [rng.gauss(0, 0.0012) for _ in range(25)]
        out.append(simulate.Session(day + dt.timedelta(days=i), returns, 0.14))
    return out


def _book(expiry):
    return simulate.Book(expiry=expiry, open_units=[], realised=0.0, ladder_state=None)


def _now_and_expiry():
    now = dt.datetime.combine(dt.date(2026, 10, 1), dt.time(10, 0), tzinfo=IST)
    return now, dt.date(2026, 10, 27)


@pytest.fixture
def synthetic(monkeypatch):
    monkeypatch.setattr(simulate, "history", lambda market, today=None: _sessions())


def _plan(config, **kw):
    now, expiry = _now_and_expiry()
    return simulate.plan(market=None, config=config, book=_book(expiry), spot=22_500.0, vix=0.14,
                         now=now, paths=kw.pop("paths", 200), **kw)


def test_every_plan_sees_the_same_paths(synthetic):
    """Common random numbers: the difference between two plans is theirs."""
    a = _plan(StrategyConfig(lots=1, lot_size=65))
    b = _plan(StrategyConfig(lots=1, lot_size=65))
    assert a["pnl"] == b["pnl"] and a["nifty_at_expiry"] == b["nifty_at_expiry"]
    c = _plan(StrategyConfig(lots=1, lot_size=65, max_down=2))
    assert c["nifty_at_expiry"] == a["nifty_at_expiry"], "settings do not change the paths"


def test_a_cap_cuts_the_bad_months(synthetic):
    loose = _plan(StrategyConfig(lots=1, lot_size=65))
    tight = _plan(StrategyConfig(lots=1, lot_size=65, max_down=2))
    assert tight["rungs"]["max"] <= 3                     # anchor + two below
    assert tight["pnl"]["worst"] >= loose["pnl"]["worst"]


def test_the_vix_rule_holds_entries_at_the_assumed_level(synthetic):
    now, expiry = _now_and_expiry()
    out = simulate.plan(market=None, config=StrategyConfig(lots=1, lot_size=65, max_entry_vix=12.0),
                        book=_book(expiry), spot=22_500.0, vix=0.14, now=now, paths=100)
    assert out["inputs"]["entries_paused_by_vix"] is True and out["rungs"]["max"] == 0


def test_nothing_opens_on_expiry_day(synthetic):
    now, expiry = _now_and_expiry()
    config = StrategyConfig(lots=1, lot_size=65)
    campaign = simulate._Campaign(config, _book(expiry), simulate.ModelPriceProvider(surface=simulate.from_vix(14.0)),
                                  simulate.CostModel(), simulate.FillModel(), True)
    on_expiry = [(dt.datetime.combine(expiry, dt.time(10, 0), tzinfo=IST), 22_000.0 - 100 * i) for i in range(10)]
    _, opened = campaign.run(on_expiry)
    assert opened == 0


def test_a_steady_fall_opens_the_ladder_and_loses(synthetic):
    out = _plan(StrategyConfig(lots=1, lot_size=65))
    by_spot = {round(e["spot"]): e["pnl"] for e in out["ends_at"]}
    assert by_spot[round(22_500 * 0.9)] < by_spot[round(22_500)], "a 10% fall is worse than staying put"


def test_the_summary_is_honest_about_its_tail():
    s = simulate._summary([-100.0] * 5 + [50.0] * 95)
    assert s["p_loss"] == 0.05 and s["es5"] == -100.0 and s["worst"] == -100.0
    assert s["se"] > 0


def test_a_plan_beyond_the_calendar_is_refused(synthetic):
    now = dt.datetime.combine(dt.date(2026, 10, 1), dt.time(10, 0), tzinfo=IST)
    with pytest.raises(ValueError):
        simulate.plan(market=None, config=StrategyConfig(lots=1, lot_size=65), book=_book(dt.date(2026, 10, 25)),
                      spot=22_500.0, vix=0.14, now=now, paths=100)


# ================================================================== api


def test_an_active_campaign_is_planned_not_replayed(registry):
    import engine.api as api
    from fastapi import HTTPException

    from engine.tests.test_forward_timeframe import _two_campaigns
    from engine.tests.test_parallel_runs import _session

    r, old, new = _two_campaigns()
    session = _session(registry)
    r.run_key = "ladder"
    session.set_runner("ladder", r)
    try:
        with pytest.raises(HTTPException) as err:
            api.playground_replay(api.ReplayRequest(run="ladder", expiry=new.isoformat()), session=session, market=None)
        assert err.value.status_code == 409
        with pytest.raises(HTTPException) as err:
            api.playground_replay(api.ReplayRequest(run="ladder", expiry="2020-01-01"), session=session, market=None)
        assert err.value.status_code == 404
    finally:
        session.set_runner("ladder", None)


def test_a_job_belongs_to_its_user():
    from engine.playground.jobs import PlaygroundJobs

    jobs = PlaygroundJobs()
    job = jobs.start("alice", "plan", {}, lambda job: {"ok": True})
    assert jobs.get("bob", job.job_id) is None
    assert jobs.get("alice", job.job_id) is job
