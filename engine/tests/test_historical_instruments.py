"""Resolving a contract that no longer exists.

A scrip master describes what is listed on the day it was published. Today's
carries no expiry earlier than today -- checked against the live file: 18 NIFTY
expiries, the earliest of them today. So a backtest over past months could not
resolve a single leg it asked about. Every fetch raised, the raise was caught
and treated as "no data for this leg", and the run priced everything from the
model. A report reading 100% MODELED was not describing thin data; it was
describing a lookup that could never succeed.
"""

from __future__ import annotations

import datetime as dt

import pytest

from engine.choice.errors import ChoiceInstrumentError
from engine.choice.instruments import Contract, HistoricalInstruments

TODAY = dt.date(2026, 9, 15)
LIVE_EXPIRY = dt.date(2026, 9, 29)
PAST_EXPIRY = dt.date(2026, 7, 28)


def _contract(strike: float, right: str, expiry: dt.date) -> Contract:
    return Contract(
        token=int(strike) + (1 if right == "CE" else 2),
        segment_id=2, symbol="NIFTY", description="", lot_size=65,
        strike=strike, option_type=right, expiry=expiry, underlying="NIFTY",
    )


class FakeMaster:
    """A scrip master holding exactly the contracts of one publication day."""

    def __init__(self, expiries: list[dt.date]) -> None:
        self.expiries_held = expiries
        self.lookups = 0

    def find_option(self, underlying, expiry, strike, right):
        self.lookups += 1
        if expiry not in self.expiries_held:
            return None
        return _contract(strike, right, expiry)


@pytest.fixture()
def masters(monkeypatch):
    """Records which publication dates were asked for."""
    from engine.choice import instruments as mod

    asked: list[dt.date] = []

    def fake_shared(on=None):
        asked.append(on)
        # A file published on `on` lists expiries from that day forward, which
        # is how NSE actually works: weeklies about seven weeks ahead.
        return FakeMaster([e for e in (PAST_EXPIRY, LIVE_EXPIRY) if on is not None and e >= on])

    monkeypatch.setattr(mod, "shared_master", fake_shared)
    return asked


def test_a_live_expiry_is_answered_without_downloading_anything(masters):
    """The common case, and the one a forward run depends on."""
    resolver = HistoricalInstruments(FakeMaster([LIVE_EXPIRY]))

    found = resolver.find_option("NIFTY", LIVE_EXPIRY, 23_400.0, "PE")

    assert found is not None
    assert masters == [], "today's master already had it"
    assert resolver.downloads == 0


def test_an_expired_contract_is_found_in_the_file_that_still_listed_it(masters):
    """The whole point. Today's master cannot answer; the one published on the
    expiry can, because a contract is listed right up to the day it settles."""
    resolver = HistoricalInstruments(FakeMaster([LIVE_EXPIRY]))

    found = resolver.find_option("NIFTY", PAST_EXPIRY, 23_400.0, "CE")

    assert found is not None
    assert masters == [PAST_EXPIRY]
    assert resolver.downloads == 1


def test_a_master_once_loaded_answers_for_every_other_leg_of_that_expiry(masters):
    """A condor is four legs and a campaign is many condors. Downloading a
    20 MB file per leg would make a backtest unusable."""
    resolver = HistoricalInstruments(FakeMaster([LIVE_EXPIRY]))

    for strike in (23_000.0, 23_200.0, 23_600.0, 23_800.0):
        for right in ("CE", "PE"):
            assert resolver.find_option("NIFTY", PAST_EXPIRY, strike, right) is not None

    assert resolver.downloads == 1, "one file answered all eight legs"


def test_a_file_already_loaded_is_not_asked_for_twice(masters):
    """Including when it turned out not to have the contract: asking again
    would download the same bytes to get the same no."""
    resolver = HistoricalInstruments(FakeMaster([LIVE_EXPIRY]))
    unlisted = dt.date(2026, 7, 28)

    resolver.find_option("NIFTY", unlisted, 23_400.0, "CE")
    resolver.find_option("NIFTY", unlisted, 23_500.0, "CE")

    assert masters == [unlisted]


def test_a_contract_nobody_has_raises_with_something_actionable(masters):
    """Distinguishing "we could not look this up" from "no data exists" is the
    difference between a bug and a fact about the market."""
    resolver = HistoricalInstruments(FakeMaster([LIVE_EXPIRY]))
    ancient = dt.date(2019, 1, 31)

    with pytest.raises(ChoiceInstrumentError, match="predate"):
        resolver.option("NIFTY", ancient, 23_400.0, "CE")


def test_a_resolver_with_no_starting_master_still_works(masters):
    resolver = HistoricalInstruments()

    assert resolver.find_option("NIFTY", PAST_EXPIRY, 23_400.0, "PE") is not None


def test_the_shared_cache_holds_more_than_one_day(monkeypatch):
    """It used to be a single slot, so asking for two dates thrashed: every
    leg of a multi-expiry backtest would re-download."""
    from engine.choice import instruments as mod

    mod.clear_shared_master()
    built: list[dt.date] = []

    class Stub:
        contracts: list = []

        def fetch(self, on=None):
            built.append(on)
            return True

    monkeypatch.setattr(mod, "ScripMaster", Stub)
    try:
        for day in (TODAY, PAST_EXPIRY, TODAY, PAST_EXPIRY):
            mod.shared_master(day)
        assert built == [TODAY, PAST_EXPIRY], "each date fetched once"
    finally:
        mod.clear_shared_master()


def test_the_cache_is_bounded(monkeypatch):
    """Each parsed master is tens of megabytes. An engine left running for
    weeks must not accumulate one per day it was asked about."""
    from engine.choice import instruments as mod

    mod.clear_shared_master()

    class Stub:
        contracts: list = []

        def fetch(self, on=None):
            return True

    monkeypatch.setattr(mod, "ScripMaster", Stub)
    try:
        for i in range(mod.MASTER_CACHE_SIZE * 3):
            mod.shared_master(TODAY - dt.timedelta(days=i))
        assert len(mod._MASTER_CACHE) <= mod.MASTER_CACHE_SIZE
    finally:
        mod.clear_shared_master()
