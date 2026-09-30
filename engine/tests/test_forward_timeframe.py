"""A forward run on a time frame, its settings, and starting a stopped run again.

A backtest on 15-minute bars fires a level on a bar's close; a run that acts on
every minute's price fires it as soon as a minute touches it -- 14:20 against
14:59 for the same 23,200 level on 15 Sep. The two can only be compared on the
same time frame, so a run can now act on bar closes too.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from engine.config import IST
from engine.forward.runner import BAR_WAIT, ForwardRunner
from engine.strategy.condor import StrategyConfig
from engine.tests.test_parallel_runs import registry  # noqa: F401 - a fixture
from engine.tests.test_vix_rule import VixMarket

DAY = dt.date(2026, 9, 15)


def at(h: int, m: int, s: int = 0) -> dt.datetime:
    return dt.datetime.combine(DAY, dt.time(h, m, s), tzinfo=IST)


class Bars(VixMarket):
    """The forward fake, serving NIFTY candles of any bar size."""

    def __init__(self, bars=(), **kw):
        super().__init__(**kw)
        self.bars = list(bars)            # (ts, close), stamped at the bar's last tick
        self.asked: list[str] = []

    def nifty(self, start, end, resolution="D", strict=True):
        self.asked.append(resolution)
        return pd.DataFrame({"ts": [t for t, _ in self.bars], "close": [c for _, c in self.bars]})


def runner(market, bar_minutes=15, **kw) -> ForwardRunner:
    return ForwardRunner(market=market, bar_minutes=bar_minutes,  # type: ignore[arg-type]
                         strategy=StrategyConfig(lots=1, lot_size=65, max_condors=20, **kw))


# ============================================================ the bar close


def test_nothing_is_decided_while_the_first_bar_forms():
    r = runner(Bars())
    assert r._bar_close(at(9, 20), 24_000.0) is None


def test_the_close_of_each_completed_bar_is_acted_on_once():
    market = Bars([(at(9, 29, 58), 23_950.0)])
    r = runner(market)
    assert r._bar_close(at(9, 31), 24_000.0) == 23_950.0
    assert market.asked == ["15"]
    assert r._bar_close(at(9, 35), 24_000.0) is None, "the same bar is not acted on twice"
    market.bars.append((at(9, 44, 59), 23_880.0))
    assert r._bar_close(at(9, 45, 10), 24_000.0) == 23_880.0


def test_the_bar_still_forming_is_never_read_as_closed():
    """Choice serves the bar in progress too, stamped with its latest tick."""
    market = Bars([(at(9, 29, 58), 23_950.0), (at(9, 31, 5), 23_700.0)])
    assert runner(market)._bar_close(at(9, 31, 10), 24_000.0) == 23_950.0


def test_a_bar_choice_has_not_served_yet_is_waited_for_then_replaced():
    r = runner(Bars())
    assert r._bar_close(at(9, 31), 24_010.0) is None, "waited for"
    late = at(9, 30) + BAR_WAIT + dt.timedelta(seconds=1)
    assert r._bar_close(late, 24_010.0) == 24_010.0, "then the latest price"
    assert sum("acted on the latest price" in e.message for e in r.events) == 1


def test_the_last_bar_of_the_day_is_cut_short_at_the_close():
    market = Bars([(at(15, 29, 59), 23_500.0)])
    r = runner(market, bar_minutes=60)
    assert r._bar_close(at(15, 31), 23_600.0) == 23_500.0
    assert r.last_bar_end == at(15, 30)


def test_a_one_minute_run_acts_on_every_price_as_before():
    market = Bars()
    r = runner(market, bar_minutes=1)
    r.tick()
    assert len(r.condors) == 1 and market.asked == [], "no bar is asked for"


def test_a_level_touched_mid_bar_waits_for_the_close(monkeypatch):
    market = Bars()
    r = runner(market)
    monkeypatch.setattr(r, "_bar_close", lambda now, spot: None)          # mid-bar
    r.tick()
    assert r.condors == [] and r.ladder.anchor is None
    monkeypatch.setattr(r, "_bar_close", lambda now, spot: 24_000.0)      # the bar closes
    r.tick()
    assert len(r.condors) == 1 and r.condors[0].level == 24_000.0


def test_the_time_frame_survives_a_restart():
    r = runner(Bars(), bar_minutes=30)
    r.last_bar_end = at(10, 15)
    back = ForwardRunner.restore(r.to_state(), market=Bars())  # type: ignore[arg-type]
    assert back.bar_minutes == 30 and back.last_bar_end == at(10, 15)


def test_runs_saved_before_time_frames_act_on_every_minute():
    state = runner(Bars()).to_state()
    state.pop("bar_minutes")
    assert ForwardRunner.restore(state, market=Bars()).bar_minutes == 1  # type: ignore[arg-type]


def test_an_unknown_time_frame_falls_back_to_every_minute():
    assert runner(Bars(), bar_minutes=7).bar_minutes == 1


# ============================================================== settings


def test_the_snapshot_says_what_the_run_was_started_with():
    r = runner(Bars(), bar_minutes=15, direction="both", max_down=20, max_up=10,
               max_entry_vix=15.0, min_entry_dte=8, min_credit_ratio=0.5)
    s = r.snapshot()["settings"]
    assert s["bar_minutes"] == 15 and s["direction"] == "both"
    assert (s["max_down"], s["max_up"], s["max_entry_vix"]) == (20, 10, 15.0)
    assert (s["min_entry_dte"], s["min_credit_ratio"]) == (8, 0.5)


def test_settings_can_be_read_back_from_a_saved_run():
    from engine.forward.runner import settings_from_state

    r = runner(Bars(), bar_minutes=5, direction="down")
    s = settings_from_state(r.to_state(), strategy_id="ladder", name="two way")
    assert s["bar_minutes"] == 5 and s["name"] == "two way" and s["direction"] == "down"


# ============================================== evening settlement


def test_an_expiry_settles_the_evening_it_expires():
    """The tick loop sleeps while the market is shut, so 29 Sep's positions
    sat OPEN on the dashboard all night."""
    from engine.tests.test_forward_expiry import EVENING, OFFICIAL, live

    r = live(closes=[(EVENING.date(), OFFICIAL)])
    assert r.settle_if_expired(EVENING)
    assert all(not c.is_open for c in r.condors) and r.expiry is None


def test_nothing_settles_before_the_official_close_is_out():
    from engine.tests.test_forward_expiry import EVENING, OFFICIAL, live

    r = live(closes=[(EVENING.date(), OFFICIAL)])
    early = dt.datetime.combine(EVENING.date(), dt.time(15, 31), tzinfo=IST)
    assert not r.settle_if_expired(early)
    assert all(c.is_open for c in r.condors)


# =========================================== starting a stopped run again


def _stopped_two_way(tmp_path, monkeypatch, registry, *, snapshot_settings=True):
    import json

    import engine.api as api
    from engine.store.db import Store
    from engine.tests.test_parallel_runs import _session

    store = Store(tmp_path / "engine.db")
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setenv("ENGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(api, "_start_tick_thread", lambda *a, **k: None)
    # Not the wall clock: in market hours a resume ticks, and a tick opens.
    monkeypatch.setattr(ForwardRunner, "is_market_open", lambda self, now=None: False)
    session = _session(registry)

    market = Bars()
    r = runner(market, bar_minutes=15, direction="both")
    r.run_key, r.run_label = "two-way", "two way"
    r._open_condor(24_000.0, dt.date.today() + dt.timedelta(days=20), side="anchor")
    r.stopped_reason = "stopped by user"
    store.save_forward(session_id="sess-two-way", user_id=session.user_id, status="stopped",
                       started_at=r.started_at.isoformat(), stopped_reason="stopped by user",
                       state=r.to_state(), run_key="two-way", run_label="two way")
    snap = r.snapshot()
    if not snapshot_settings:
        snap.pop("settings")            # saved before snapshots carried them
    (tmp_path / f"live-{session.user_id}-two-way.json").write_text(json.dumps(snap), encoding="utf-8")
    return api, store, session, market


def test_a_stopped_run_can_be_resumed_with_its_positions(tmp_path, monkeypatch, registry):
    api, store, session, market = _stopped_two_way(tmp_path, monkeypatch, registry)
    body = api.forward_resume(session=session, run_key="two-way", market=market)
    assert body["resumed"] is True
    live = session.runner_for("two-way")
    assert live is not None and live.stopped_reason is None
    assert len([c for c in live.condors if c.is_open]) == 1, "its position came back with it"
    assert live.bar_minutes == 15 and live.strategy.direction == "both"
    assert store.forward_session("sess-two-way")["status"] == "running"
    assert any(e.message == "Forward run resumed by user" for e in live.events)


def test_the_stopped_view_offers_resume_and_its_settings(tmp_path, monkeypatch, registry):
    api, _, session, _ = _stopped_two_way(tmp_path, monkeypatch, registry, snapshot_settings=False)
    state = api.forward_state(session=session, run="two-way")["state"]
    assert state["session"]["resumable"] is True
    assert state["settings"]["bar_minutes"] == 15 and state["settings"]["name"] == "two way"


def test_resuming_is_refused_at_the_run_cap(tmp_path, monkeypatch, registry):
    import pytest
    from fastapi import HTTPException

    from engine.tests.test_parallel_runs import FakeRunner

    api, _, session, market = _stopped_two_way(tmp_path, monkeypatch, registry)
    for i in range(api.MAX_RUNS_PER_USER):
        session.set_runner(f"run-{i}", FakeRunner(f"run-{i}"))
    with pytest.raises(HTTPException) as err:
        api.forward_resume(session=session, run_key="two-way", market=market)
    assert err.value.status_code == 409 and "Stop one" in err.value.detail


def test_a_start_under_a_running_runs_name_says_nothing_new_started(registry):
    """The page used to switch to the other run as if this one had begun."""
    import engine.api as api
    from engine.tests.test_parallel_runs import FakeRunner, _session

    session = _session(registry)
    session.set_runner("ladder", FakeRunner("ladder"))
    body = api.forward_start(body=api.StartForwardRequest(), session=session, market=Bars())
    assert body["already_running"] is True and "nothing new was started" in body["note"]


def test_a_start_request_carries_the_time_frame():
    import pydantic
    import pytest

    import engine.api as api

    assert api.StartForwardRequest(bar_minutes=15).bar_minutes == 15
    assert api.StartForwardRequest().bar_minutes == 1
    with pytest.raises(pydantic.ValidationError):
        api.StartForwardRequest(bar_minutes=7)


# =========================================== a run saved before its cadence


def test_a_run_saved_without_its_cadence_is_read_from_what_it_traded():
    """Two ladders started as monthly on 9 and 10 Sep were saved before the
    cadence was; restored as weekly, their first roll went to the 6 Oct weekly."""
    r = runner(Bars(), bar_minutes=1)
    r._open_condor(23_400.0, dt.date.today() + dt.timedelta(days=19), side="down")
    state = r.to_state()
    state.pop("expiry_cadence")
    assert ForwardRunner.restore(state, market=Bars()).expiry_cadence == "monthly"  # type: ignore[arg-type]


def test_a_weekly_run_saved_without_its_cadence_stays_weekly():
    r = runner(Bars(), bar_minutes=1)
    r._open_condor(23_400.0, dt.date.today() + dt.timedelta(days=5), side="down")
    state = r.to_state()
    state.pop("expiry_cadence")
    assert ForwardRunner.restore(state, market=Bars()).expiry_cadence == "weekly"  # type: ignore[arg-type]


# =========================================== expiry day, as the backtest does it


def _on_expiry_day():
    """A run holding three condors on EXPIRY, on the morning EXPIRY expires."""
    from engine.tests.test_forward_expiry import EXPIRY, OFFICIAL, live

    r = live(closes=[(EXPIRY, OFFICIAL)])
    morning = dt.datetime.combine(EXPIRY, dt.time(9, 20), tzinfo=IST)
    return r, morning, EXPIRY


def test_on_expiry_day_nothing_more_opens_on_the_dying_contract():
    """The live run opened 22,700 and 22,600 on 29 Sep's own contract that
    morning for ₹1,164 and ₹1,209; the backtest had already rolled."""
    r, morning, expiry = _on_expiry_day()
    r._roll_on_expiry_day(morning)
    assert r.expiry is None, "the next tick resolves the next expiry"
    assert r.settling == expiry
    assert r.ladder.anchor is None and not r.ladder.fired_levels
    assert all(c.is_open for c in r.condors), "they settle at the close, not now"
    assert any("expires today" in e.message for e in r.events)


def test_the_rolled_positions_settle_at_the_official_close():
    from engine.tests.test_forward_expiry import EVENING

    r, morning, expiry = _on_expiry_day()
    r._roll_on_expiry_day(morning)
    r.expiry = expiry + dt.timedelta(days=28)            # the next campaign, resolved at the tick
    assert r.settle_if_expired(EVENING)
    assert r.settling is None
    assert all(not c.is_open for c in r.condors)
    assert r.realised == sum(c.realised_pnl() for c in r.condors) != 0.0
    assert all(f"{expiry:%d-%b-%Y}" in (c.exit_reason or "") for c in r.condors)
    assert r.expiry == expiry + dt.timedelta(days=28), "the new campaign is untouched"


def test_a_rolled_expiry_awaiting_its_close_survives_a_restart():
    r, morning, expiry = _on_expiry_day()
    r._roll_on_expiry_day(morning)
    back = ForwardRunner.restore(r.to_state(), market=r.market)  # type: ignore[arg-type]
    assert back.settling == expiry


def test_nothing_rolls_when_nothing_was_open():
    r, morning, expiry = _on_expiry_day()
    from engine.strategy.condor import CondorStatus

    for c in r.condors:
        c.close(morning, "closed early", CondorStatus.CLOSED_TARGET, 0.0)
    r._roll_on_expiry_day(morning)
    assert r.settling is None and r.expiry is None


# ==================================================== campaigns, one by one


def _two_campaigns():
    """A run whose first expiry has settled and whose second is trading."""
    from engine.tests.test_forward_expiry import EVENING, EXPIRY, OFFICIAL, live

    r = live(closes=[(EXPIRY, OFFICIAL)])
    r._settle_and_roll(EVENING, OFFICIAL)
    nxt = EXPIRY + dt.timedelta(days=28)
    r.expiry = nxt
    unit = r._open_condor(22_700.0, nxt, side="anchor")
    r.last_mtm[r.condors[-1].index] = -250.0
    return r, EXPIRY, nxt


def test_each_campaign_is_reported_on_its_own():
    """The run's total mixed a settled September with a morning-old October."""
    r, old, new = _two_campaigns()
    campaigns = r.snapshot()["campaigns"]
    assert [c["expiry"] for c in campaigns] == [new.isoformat(), old.isoformat()], "newest first"
    live_c, settled = campaigns
    assert live_c["status"] == "active" and live_c["pnl"] == -250.0 and live_c["open"] == 1
    assert settled["status"] == "settled" and settled["open"] == 0 and settled["positions"] == 3
    assert settled["pnl"] == round(sum(c.realised_pnl() for c in r.condors if c.expiry == old), 2)
    assert settled["settlement_spot"] is not None
    assert r.snapshot()["pnl"]["total"] == pytest.approx(settled["pnl"] + live_c["pnl"])


def test_a_campaign_closed_before_expiry_is_not_called_settled():
    from engine.strategy.condor import CondorStatus

    r, old, new = _two_campaigns()
    wrong = new - dt.timedelta(days=21)
    r._open_condor(22_700.0, wrong, side="anchor")
    r.condors[-1].close(dt.datetime.now(tz=IST), "opened on the wrong expiry", CondorStatus.CLOSED, 0.0)
    by = {c["expiry"]: c for c in r.snapshot()["campaigns"]}
    assert by[wrong.isoformat()]["status"] == "closed"


def test_a_past_campaigns_chart_comes_from_choices_bars(monkeypatch, registry):
    import engine.api as api
    from engine.tests.test_parallel_runs import _session

    r, old, new = _two_campaigns()
    session = _session(registry)
    r.run_key = "ladder"
    session.set_runner("ladder", r)
    start = min(c.entry_time for c in r.condors if c.expiry == old).date()

    class Market:
        def nifty(self, a, b, resolution="D", strict=True):
            assert resolution == "15"
            return pd.DataFrame({
                "ts": pd.to_datetime([dt.datetime.combine(start, dt.time(9, 29, 59), tzinfo=IST)]),
                "close": [23_410.0],
            })

    body = api.forward_campaign(expiry=old.isoformat(), session=session, run_key="ladder", market=Market())
    assert body["campaign"]["status"] == "settled" and body["bars"][0]["spot"] == 23_410.0
    import pytest as _pytest
    from fastapi import HTTPException
    with _pytest.raises(HTTPException):
        api.forward_campaign(expiry="2020-01-01", session=session, run_key="ladder", market=Market())
    session.set_runner("ladder", None)
