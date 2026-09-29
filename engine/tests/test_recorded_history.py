"""Backtests priced from the recorded one-minute history (nifty.db): after
Choice, before the backup, never replacing either, and never named as
anything but the recorded history."""

from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import pytest

from engine.config import IST
from engine.data import groww, history_store
from engine.tests.test_backup_source import OTHER_VENDORS, FakeHttp, _spot_at_ten, client


def minute_bars(first: dt.date, end: dt.date, close: float) -> pd.DataFrame:
    """One-minute bars 09:15..15:29 each weekday, stamped at the last second."""
    stamps, day = [], first
    while day <= end:
        if day.weekday() < 5:
            open_ = dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST)
            stamps += [open_ + dt.timedelta(minutes=m, seconds=59) for m in range(375)]
        day += dt.timedelta(days=1)
    return pd.DataFrame({"ts": pd.to_datetime(stamps), "close": close})


@pytest.fixture
def recorded(monkeypatch):
    """A recorded history holding every leg asked for, at 42.5."""
    asked: list = []

    def option_bars(legs, end, *, path=None):
        legs = list(legs)
        asked.extend(legs)
        return {(e, k, r): minute_bars(d, end, 42.5) for e, k, r, d in legs}

    monkeypatch.setattr(history_store, "available", lambda path=None: True)
    monkeypatch.setattr(history_store, "option_bars", option_bars)
    return asked


def _run(monkeypatch, market, **kw):
    from engine.backtest import jobs
    from engine.tests.test_jobs import run_job

    days = sorted(set(market._spot["ts"].dt.date))
    market._vix = {d: 13.0 for d in days}
    monkeypatch.setattr(jobs, "expected_sessions", lambda start, end, now=None: set(days))
    return run_job(market=market, **kw)


@pytest.mark.parametrize("resolution", ["D", "5"])
def test_legs_choice_no_longer_serves_come_from_the_recorded_history(monkeypatch, recorded, resolution):
    from engine.tests.test_jobs import FakeMarket

    job = _run(monkeypatch, FakeMarket(spot=_spot_at_ten()), resolution=resolution)
    assert job.status == "done", job.error
    prov = job.result["provenance"]
    assert prov["legs_history"] > 0 and prov["history"]["used"]
    assert prov["provider"]["history_quotes"] > 0 and prov["provider"]["modeled_quotes"] == 0
    assert prov["premium_source"].startswith("history:one-minute")
    assert prov["verified"] is False                     # real, but not all from Choice
    legs = [leg for c in job.result["condors"] for leg in c["legs"]]
    assert {leg["source"] for leg in legs} == {"history"}
    assert {leg["entry_price"] for leg in legs} == {42.5}


def test_choice_still_wins_where_it_has_the_leg(monkeypatch, recorded):
    from engine.tests.test_jobs import FakeMarket

    job = _run(monkeypatch, FakeMarket(spot=_spot_at_ten(), option_frames=True))
    assert job.status == "done", job.error
    assert {leg["source"] for c in job.result["condors"] for leg in c["legs"]} == {"choice"}
    assert job.result["provenance"]["legs_history"] == 0


def test_the_backup_is_asked_only_for_what_the_history_lacks(monkeypatch, recorded):
    from engine.tests.test_jobs import FakeMarket

    http = FakeHttp()
    monkeypatch.setattr(groww, "shared_backup", lambda: client(http))
    job = _run(monkeypatch, FakeMarket(spot=_spot_at_ten()), option_resolution="5")
    assert job.status == "done", job.error
    assert job.result["provenance"]["backup"]["option_legs_asked"] == 0
    assert not [g for g in http.gets if "candles" in g["url"] and "FNO" in json.dumps(g["params"])]


def test_an_absent_or_locked_history_costs_accuracy_not_the_run(monkeypatch):
    from engine.tests.test_jobs import FakeMarket

    def locked(legs, end, *, path=None):
        raise history_store.HistoryUnavailable("the recorded history was in use by another process")

    monkeypatch.setattr(history_store, "available", lambda path=None: True)
    monkeypatch.setattr(history_store, "option_bars", locked)
    job = _run(monkeypatch, FakeMarket(spot=_spot_at_ten()))
    assert job.status == "done", job.error
    info = job.result["provenance"]["history"]
    assert info["used"] is False and "in use" in info["note"]


def test_nothing_about_the_history_names_another_company(monkeypatch, recorded):
    from engine.tests.test_jobs import FakeMarket

    job = _run(monkeypatch, FakeMarket(spot=_spot_at_ten()))
    payload = json.dumps({"result": job.result, "public": job.public()}, default=str)
    assert not OTHER_VENDORS.search(payload)


def test_legs_with_real_prices_are_counted_once(monkeypatch, recorded):
    from engine.tests.test_jobs import FakeMarket

    job = _run(monkeypatch, FakeMarket(spot=_spot_at_ten(), option_frames="first-bar-only"))
    assert job.status == "done", job.error
    prov = job.result["provenance"]
    assert prov["legs_priced_real"] <= prov["legs_total"]
    assert prov["legs_priced_real"] == prov["legs_total"]      # every leg: Choice, then history


def test_weekly_bars_skip_the_history_without_calling_it_a_fault(monkeypatch, recorded):
    from engine.tests.test_jobs import FakeMarket

    job = _run(monkeypatch, FakeMarket(spot=_spot_at_ten()), resolution="W")
    assert job.status == "done", job.error
    info = job.result["provenance"]["history"]
    assert info["used"] is False and info["note"] is None and info["skipped"]
    assert recorded == []


def test_a_small_modelled_remainder_is_never_called_zero_percent():
    from engine.backtest.jobs import _share

    assert _share(4181, 603820) == "0.7%"
    assert _share(1, 603820) == "under 0.1%"
    assert _share(0, 10) == "0.0%"
    assert _share(35, 100) == "35%"
