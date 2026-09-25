"""What a forward run does when its expiry arrives.

Nothing, until now. `self.expiry` was resolved on the first tick and never
reset, so once the contracts settled `_contract` could no longer resolve them,
`_mark_all` dropped every position out of the marks, and the run's headline
P&L fell to whatever had closed early -- usually zero -- while the positions
themselves stayed OPEN for ever. A month of paper trading disappeared from the
dashboard without a single error, and the ladder went on firing into the dead
expiry.
"""

from __future__ import annotations

import datetime as dt

import pytest

from engine.choice.errors import ChoiceError
from engine.config import IST
from engine.strategy.condor import CondorStatus
from engine.tests.test_forward_fills import EXPIRY, FakeMarket, FakeMaster, cfg
from engine.forward.runner import ForwardRunner


class Frame:
    """The two attributes `_settlement_spot` reads off a candle frame."""

    def __init__(self, rows: list[tuple[dt.date, float]]) -> None:
        self._rows = rows

    def itertuples(self):
        return iter(
            [type("Row", (), {"ts": dt.datetime.combine(d, dt.time(15, 30)), "close": c})()
             for d, c in self._rows]
        )


class MarketWithHistory(FakeMarket):
    def __init__(self, closes: list[tuple[dt.date, float]] | None = None, **kw):
        super().__init__(**kw)
        self.closes = closes
        self.nifty_calls = 0

    def nifty(self, start, end, resolution="D"):
        self.nifty_calls += 1
        if self.closes is None:
            raise ChoiceError("Choice has no NIFTY series for that range")
        return Frame(self.closes)


def live(**kw) -> ForwardRunner:
    """A runner holding three condors on EXPIRY, anchored at 23,400."""
    market = MarketWithHistory(master=FakeMaster(), **kw)
    r = ForwardRunner(market=market, strategy=cfg(step=100.0))  # type: ignore[arg-type]
    r.expiry = EXPIRY
    for level in (23_400.0, 23_300.0, 23_200.0):
        r._open_condor(level, EXPIRY, side="down")
    r.ladder.anchor = 23_400.0
    r.ladder.last_level = 23_200.0
    r.ladder.fired_levels.update({23_400.0, 23_300.0, 23_200.0})
    assert len([c for c in r.condors if c.is_open]) == 3
    return r


def at(when: dt.datetime) -> dt.datetime:
    return when.replace(tzinfo=IST)


# ============================== when it settles


def test_it_does_not_settle_while_the_expiry_still_trades():
    r = live()
    assert not r._expiry_is_settled(at(dt.datetime.combine(EXPIRY, dt.time(14, 0))))
    assert not r._expiry_is_settled(
        at(dt.datetime.combine(EXPIRY - dt.timedelta(days=1), dt.time(15, 45)))
    )


def test_it_settles_after_the_close_on_expiry_day_and_after():
    r = live()
    assert r._expiry_is_settled(at(dt.datetime.combine(EXPIRY, dt.time(15, 30))))
    assert r._expiry_is_settled(
        at(dt.datetime.combine(EXPIRY + dt.timedelta(days=3), dt.time(10, 0)))
    )


# ============================== what it settles at

# NSE settles against NIFTY's official close -- the daily candle's close -- not
# the last level a tick saw. On six of the eight 2026 monthly expiries the last
# five-minute bar was 18-61 points away from it.
OFFICIAL = 23_050.0            # below both lower shorts, above the long wings
EVENING = at(dt.datetime.combine(EXPIRY, dt.time(16, 5)))


def test_it_books_intrinsic_against_the_official_close_not_the_last_tick():
    r = live(closes=[(EXPIRY, OFFICIAL)])
    last_tick = 23_120.0                              # where the tape happened to end
    r._settle_and_roll(EVENING, last_tick)

    assert all(c.status is CondorStatus.EXPIRED for c in r.condors)
    assert not [c for c in r.condors if c.is_open]
    expected = sum(c.payoff_at_expiry(OFFICIAL) for c in r.condors)
    assert r.realised == pytest.approx(expected)
    assert "official NIFTY close" in (r.condors[0].exit_reason or "")


def test_on_expiry_day_it_waits_for_the_official_figure():
    """Published after the bell; a candle read at 15:30 may be provisional."""
    r = live(closes=[(EXPIRY, OFFICIAL)])
    r._settle_and_roll(at(dt.datetime.combine(EXPIRY, dt.time(15, 31))), 23_120.0)
    assert all(c.is_open for c in r.condors), "nothing settles before the figure is final"
    assert r.market.nifty_calls == 0
    assert not any("Cannot settle" in e.message for e in r.events), "waiting is not a fault"

    r._settle_and_roll(EVENING, 23_120.0)
    assert not [c for c in r.condors if c.is_open]


def test_nothing_new_opens_while_settlement_is_pending():
    """The contract has stopped trading; a rung opened now is opened on it."""
    r = live(closes=None)
    r.expiry = dt.date.today() - dt.timedelta(days=1)  # expired, and no close to be had
    before = len(r.condors)
    r.market.prices[26000] = 22_000.0                  # far below every open rung
    r.tick()
    assert len(r.condors) == before
    assert r.expiry == dt.date.today() - dt.timedelta(days=1)


def test_the_total_stops_being_zero_once_the_book_has_settled():
    """The failure as a user met it: a whole campaign's P&L at nothing."""
    r = live(closes=[(EXPIRY, OFFICIAL)])
    r.last_mtm = {}                                   # contracts no longer resolve
    blind, _ = r._pnl_total_locked()
    assert blind == 0.0

    r._settle_and_roll(EVENING, 23_050.0)

    settled, unmarked = r._pnl_total_locked()
    assert unmarked == ()
    assert settled != 0.0
    assert settled == pytest.approx(r.realised)


def test_a_settlement_missed_by_days_uses_the_expiry_day_close_not_todays_spot():
    """Today's spot is a different day's number, and booking it would report
    a P&L that never happened."""
    r = live(closes=[(EXPIRY - dt.timedelta(days=1), 23_500.0), (EXPIRY, 23_050.0)])
    later = at(dt.datetime.combine(EXPIRY + dt.timedelta(days=4), dt.time(11, 0)))

    r._settle_and_roll(later, 24_900.0)                # a big rally since

    assert r.market.nifty_calls == 1
    assert r.realised == pytest.approx(sum(c.payoff_at_expiry(23_050.0) for c in r.condors))
    assert f"{EXPIRY:%d-%b-%Y}" in (r.condors[0].exit_reason or "")


def test_nothing_is_booked_when_the_closing_level_cannot_be_had():
    """Positions left open and visibly unsettled is recoverable. A number
    invented from the wrong day's spot is not."""
    r = live(closes=None)
    later = at(dt.datetime.combine(EXPIRY + dt.timedelta(days=4), dt.time(11, 0)))

    r._settle_and_roll(later, 24_900.0)

    assert all(c.is_open for c in r.condors)
    assert r.realised == 0.0
    assert r.expiry == EXPIRY, "the campaign is still the one that needs settling"
    assert any("Cannot settle" in e.message for e in r.events)


# ============================== and then it rolls


def test_the_ladder_re_anchors_so_the_next_campaign_starts_clean():
    """A long put in September does not offset a short put in October, so a
    campaign lives inside one expiry -- the same rule the backtest follows."""
    r = live(closes=[(EXPIRY, OFFICIAL)])
    r._settle_and_roll(EVENING, 23_050.0)

    assert r.expiry is None, "the next tick resolves the next expiry"
    assert r.ladder.anchor is None
    assert r.ladder.last_level is None
    assert r.ladder.fired_levels == set()


def test_a_settled_position_leaves_a_close_fill_for_every_leg():
    r = live(closes=[(EXPIRY, OFFICIAL)])
    before = len(r.fills)
    r._settle_and_roll(EVENING, 23_050.0)

    closes = [f for f in r.fills[before:] if f.action == "CLOSE"]
    assert len(closes) == 12                      # three condors, four legs each
    assert {f.source for f in closes} == {"settlement"}
    for fill in closes:
        assert fill.price >= 0.0


def test_settlement_survives_a_restart():
    r = live(closes=[(EXPIRY, OFFICIAL)])
    r._settle_and_roll(EVENING, 23_050.0)
    realised = r.realised

    revived = ForwardRunner.restore(r.to_state(), market=r.market, state_path=None)

    assert revived.realised == pytest.approx(realised)
    assert all(c.status is CondorStatus.EXPIRED for c in revived.condors)
    assert revived.expiry is None



def test_settlement_asks_past_the_expiry_day_and_warns_once():
    """A date-only end is midnight at the start of expiry day, so whether the
    day's own candle came back depended on Choice's boundary; and a failing
    settlement repeated its warning on every tick."""
    from engine.forward.runner import OFFICIAL_CLOSE_READY

    asked = []

    class Recording(MarketWithHistory):
        def nifty(self, start, end, resolution="D"):
            asked.append(end)
            raise ChoiceError("no data")

    r = ForwardRunner(market=Recording(master=FakeMaster()), strategy=cfg(step=100.0))  # type: ignore[arg-type]
    r.expiry = EXPIRY
    after = dt.datetime.combine(EXPIRY, OFFICIAL_CLOSE_READY, tzinfo=IST) + dt.timedelta(minutes=5)
    for _ in range(5):
        assert r._settlement_spot(after, 23_000.0) == (None, "")
    assert asked[0] > EXPIRY
    warnings = [e for e in r.events if e.level == "warn" and "Cannot settle" in e.message]
    assert len(warnings) == 1
