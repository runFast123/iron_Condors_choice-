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


def test_bars_after_a_contracts_expiry_are_dropped():
    """Choice recycles a settled contract's token, and ChartData is keyed by
    token, so a bar stamped after expiry belongs to whatever inherited it.
    Kept, it would price this leg with another instrument's premium and call
    it real."""
    import datetime as dt

    import pandas as pd

    from engine.config import IST
    from engine.data.market import ChoiceMarketData

    expiry = dt.date(2026, 7, 28)
    bars = [dt.datetime(2026, 7, 27, 10, 0, tzinfo=IST),
            dt.datetime(2026, 7, 28, 15, 25, tzinfo=IST),       # last legitimate bar
            dt.datetime(2026, 7, 29, 9, 15, tzinfo=IST),        # the token's next holder
            dt.datetime(2026, 8, 20, 11, 0, tzinfo=IST)]
    frame = pd.DataFrame({"ts": bars, "close": [100.0, 95.0, 3.0, 4.0]})

    class Resolver:
        def option(self, underlying, expiry, strike, right):
            return type("C", (), {"token": 63916})()

    market = ChoiceMarketData.__new__(ChoiceMarketData)
    market.candles = lambda contract, start, end, resolution="5", **kw: frame

    out = market.option_candles("NIFTY", expiry, 23_500.0, "PE",
                                dt.date(2026, 7, 1), dt.date(2026, 9, 1), instruments=Resolver())

    assert list(out["close"]) == [100.0, 95.0]
    assert out["ts"].max() <= dt.datetime(2026, 7, 28, 15, 30, tzinfo=IST)


def test_a_long_backtest_keeps_only_a_few_dated_masters(monkeypatch):
    """Each parsed master is about 110 MB; an all-data run consulted sixteen
    and kept every one."""
    import datetime as dt

    from engine.choice import instruments as mod

    class Master:
        def find_option(self, *a):
            return None

    base = Master()
    monkeypatch.setattr(mod, "shared_master", lambda day: Master())
    resolver = mod.HistoricalInstruments(base)
    for n in range(10):
        resolver.find_option("NIFTY", dt.date(2021, 1, 7) + dt.timedelta(weeks=n), 15000.0, "PE")
    dated = [m for _, m in resolver._loaded if m is not base]
    assert len(dated) == mod.LOADED_MASTERS
    assert any(m is base for _, m in resolver._loaded)            # today's always stays


def test_recycled_token_mapped_to_different_instrument_is_refused():
    """When a contract's token has been reassigned to a different active instrument
    (e.g. a stock future or equity), market.option_candles refuses Choice data to
    prevent corrupt foreign prices from leaking into the backtest."""
    from engine.config import IST
    from engine.data.market import ChoiceMarketData
    import pandas as pd

    expiry = dt.date(2025, 10, 28)
    token = 58894

    class FakeResolver:
        def option(self, underlying, exp, strike, right):
            return type("C", (), {
                "token": token,
                "expiry": exp,
                "strike": strike,
                "option_type": right,
                "underlying": underlying,
            })()

    # Active master maps this token to Apollo Hospitals December future instead of NIFTY PE
    class ActiveMaster:
        by_token = {
            token: type("Active", (), {
                "token": token,
                "symbol": "APOLLOHOSP",
                "underlying": None,
                "expiry": dt.date(2026, 12, 29),
                "strike": None,
                "option_type": None,
            })()
        }

    market = ChoiceMarketData.__new__(ChoiceMarketData)
    market.master = ActiveMaster()
    # If called, it would have returned Apollo Hospitals bars
    market.candles = lambda contract, start, end, resolution="5", **kw: pd.DataFrame({
        "ts": [dt.datetime(2025, 10, 3, 10, 0, tzinfo=IST)],
        "close": [7486.35],
    })

    out = market.option_candles(
        "NIFTY", expiry, 24_700.0, "PE",
        dt.date(2025, 10, 1), dt.date(2025, 10, 5),
        instruments=FakeResolver(),
    )
    assert out.empty, "Recycled token must be rejected and return empty DataFrame"


def test_unphysical_option_prices_are_discarded():
    """Option candles exceeding 2500 pts or with PE close >= strike are discarded."""
    from engine.config import IST
    from engine.data.market import ChoiceMarketData
    import pandas as pd

    expiry = dt.date(2025, 10, 28)

    class FakeResolver:
        def option(self, underlying, exp, strike, right):
            return type("C", (), {
                "token": 12345,
                "expiry": exp,
                "strike": strike,
                "option_type": right,
                "underlying": underlying,
            })()

    market = ChoiceMarketData.__new__(ChoiceMarketData)
    market.master = type("M", (), {"by_token": {}})()

    # Bars with price 7486 for a 24700 PE
    market.candles = lambda *a, **k: pd.DataFrame({
        "ts": [dt.datetime(2025, 10, 3, 10, 0, tzinfo=IST)],
        "close": [7486.35],
    })

    out = market.option_candles(
        "NIFTY", expiry, 24_700.0, "PE",
        dt.date(2025, 10, 1), dt.date(2025, 10, 5),
        instruments=FakeResolver(),
    )
    assert out.empty, "Unphysical candle price (>2500) must be discarded"

