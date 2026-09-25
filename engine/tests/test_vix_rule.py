"""The VIX rule: no new positions while India VIX is above the run's limit.

As chosen: above the limit pauses, below resumes, a reading exactly on it
changes nothing. While paused the ladder is frozen -- a campaign that has not
anchored waits to do so. On resuming it carries on from the current price: the
level the market is at opens, levels beyond it open when reached, and the
levels between the old bound and the price are passed over. The backtest's two
passes and the forward runner must all make the same decisions.
"""

from __future__ import annotations

import datetime as dt

import pytest

from engine.backtest.vix_series import MAX_AGE, align_vix, interval_start
from engine.config import IST
from engine.strategy.condor import StrategyConfig, vix_allows_entries
from engine.strategy.hic import HicConfig
from engine.strategy.ladder import Ladder

T0 = dt.datetime(2026, 9, 1, 9, 15, tzinfo=IST)


def feed(ladder: Ladder, prices, *, allowed=True, start=0):
    out = []
    for i, p in enumerate(prices, start):
        out += [
            (t.level, t.side)
            for t in ladder.on_price(p, T0 + dt.timedelta(minutes=i), entries_allowed=allowed)
        ]
    return out


# ============================================================ the rule itself


@pytest.mark.parametrize(
    "vix, paused, expected",
    [
        (14.99, False, True), (14.99, True, True),     # below resumes
        (15.01, False, False), (15.01, True, False),   # above pauses
        (15.0, False, True), (15.0, True, False),      # on the line: no change
        (None, False, False),                          # no reading: pause
        (float("nan"), False, False), (0.0, False, False),
    ],
)
def test_the_rule_as_stated(vix, paused, expected):
    assert vix_allows_entries(vix, 15.0, paused) is expected


def test_no_limit_means_no_rule():
    assert vix_allows_entries(None, None, True) is True
    assert vix_allows_entries(80.0, None, False) is True


@pytest.mark.parametrize("bad", [0.0, -5.0, 101.0])
def test_a_nonsense_limit_is_refused(bad):
    with pytest.raises(ValueError):
        StrategyConfig(max_entry_vix=bad)


def test_the_limit_is_off_unless_asked_for():
    """So a run saved before the rule existed restores without it."""
    assert StrategyConfig().max_entry_vix is None
    assert HicConfig(direction="both").max_entry_vix is None


# ====================================================== the ladder, paused


def test_the_worked_example_carries_on_from_the_current_price():
    """Holding 24,000 / 23,900 / 23,800. VIX goes above 15. NIFTY falls to
    23,300, bounces to 23,550, and VIX drops back. 23,600 opens now; 23,500,
    23,400 and 23,300 open if NIFTY falls back to them; 23,700 is passed."""
    ladder = Ladder(config=StrategyConfig(anchor_mode="floor"))
    assert [lv for lv, _ in feed(ladder, [24_000, 23_950, 23_900, 23_850, 23_800])] == [
        24_000, 23_900, 23_800,
    ]

    assert feed(ladder, [23_700, 23_500, 23_300, 23_450, 23_550], allowed=False) == []
    assert ladder.paused

    assert feed(ladder, [23_550]) == [(23_600.0, "down")]
    assert [p.level for p in ladder.passed] == [23_700.0]
    assert not ladder.paused

    assert [lv for lv, _ in feed(ladder, [23_480, 23_390, 23_300])] == [23_500, 23_400, 23_300]


def test_a_pause_moves_nothing():
    ladder = Ladder(config=StrategyConfig(direction="both"))
    feed(ladder, [24_010])
    before = ladder.dump_state()
    feed(ladder, [23_000, 25_000, 22_000], allowed=False)
    after = ladder.dump_state()
    assert {k: v for k, v in after.items() if k != "paused"} == {
        k: v for k, v in before.items() if k != "paused"
    }
    assert ladder.triggers[-1].level == 24_000.0 and len(ladder.triggers) == 1


def test_a_campaign_that_starts_paused_waits_to_place_its_anchor():
    ladder = Ladder(config=StrategyConfig(direction="both"))
    assert feed(ladder, [24_000, 23_700], allowed=False) == []
    assert ladder.anchor is None
    assert feed(ladder, [23_760]) == [(23_800.0, "anchor")]
    assert ladder.anchor == 23_800.0
    assert ladder.passed == [], "nothing had been placed, so nothing was passed"


def test_levels_crossed_in_the_pause_and_recovered_stay_available():
    """The down bound never moved past them, so they open when reached."""
    ladder = Ladder(config=StrategyConfig(anchor_mode="floor"))
    feed(ladder, [24_000, 23_900, 23_800])
    feed(ladder, [23_300, 23_950], allowed=False)          # down and back up
    assert feed(ladder, [23_950]) == []                    # above the old bound
    assert ladder.passed == []
    assert [lv for lv, _ in feed(ladder, [23_700, 23_600, 23_500])] == [23_700, 23_600, 23_500]


def test_a_two_way_ladder_resumes_on_the_up_side_too():
    ladder = Ladder(config=StrategyConfig(direction="both"))
    feed(ladder, [24_010])                                 # anchor 24,000
    feed(ladder, [24_300, 24_600, 24_250], allowed=False)
    assert feed(ladder, [24_250]) == [(24_200.0, "up")]
    assert [(p.level, p.side) for p in ladder.passed] == [(24_100.0, "up")]
    assert ladder.last_level == 24_000.0, "the down side was not touched"


def test_a_down_only_ladder_leaves_its_up_bound_alone():
    ladder = Ladder(config=StrategyConfig(anchor_mode="floor"))
    feed(ladder, [24_000])
    feed(ladder, [24_900], allowed=False)
    assert feed(ladder, [24_900]) == []
    assert ladder.high_level == 24_000.0 and ladder.passed == []


def test_resuming_respects_the_cap():
    ladder = Ladder(config=StrategyConfig(anchor_mode="floor", max_condors=2))
    feed(ladder, [24_000, 23_900])
    feed(ladder, [23_500], allowed=False)
    assert feed(ladder, [23_550]) == []                    # at the cap already


def test_exactly_on_the_old_bound_nothing_is_passed():
    ladder = Ladder(config=StrategyConfig(anchor_mode="floor"))
    feed(ladder, [24_000, 23_900])
    feed(ladder, [23_700], allowed=False)
    assert feed(ladder, [23_800]) == [(23_800.0, "down")]  # the next rung, as normal
    assert ladder.passed == []


def test_the_pause_survives_a_restart_and_old_state_never_paused():
    ladder = Ladder(config=StrategyConfig())
    feed(ladder, [24_000])
    feed(ladder, [23_500], allowed=False)
    restored = Ladder(config=StrategyConfig())
    restored.load_state(ladder.dump_state())
    assert restored.paused
    assert feed(restored, [23_550]) == [(23_600.0, "down")]

    legacy = {"anchor": 24_000.0, "last_level": 24_000.0, "fired_levels": [240]}
    old = Ladder(config=StrategyConfig())
    old.load_state(legacy)
    assert old.paused is False


def test_a_roll_keeps_the_pause_and_clears_what_was_passed():
    """A spell running through an expiry is one spell; a new campaign has no
    bounds for a resume to move."""
    ladder = Ladder(config=StrategyConfig())
    feed(ladder, [24_000])
    feed(ladder, [23_500], allowed=False)
    feed(ladder, [23_550])
    assert ladder.passed
    feed(ladder, [23_000], allowed=False)
    ladder.reset()
    assert ladder.paused and ladder.passed == []
    assert feed(ladder, [23_000]) == [(23_000.0, "anchor")]


def test_hic_builds_its_band_around_the_anchor_it_waited_for():
    from engine.backtest.runner import _kind_for
    from engine.strategy.condor import UnitKind

    config = HicConfig(direction="both", full_band_steps=1)
    ladder = Ladder(config=config)
    feed(ladder, [24_000], allowed=False)
    feed(ladder, [23_510])                                 # anchor 23,500 (nearest)
    assert ladder.anchor == 23_500.0
    kinds = [_kind_for(lv, ladder, config)[0] for lv, _ in feed(ladder, [23_400, 23_300])]
    assert kinds == [UnitKind.CONDOR, UnitKind.PUT_DEBIT_SPREAD]


# ================================================= VIX against each bar


def at(h, m, s=0, day=1):
    return dt.datetime(2026, 9, day, h, m, s, tzinfo=IST)


def test_a_bar_takes_the_vix_of_its_own_interval_even_stamped_later():
    """Choice stamps a bar with its last trade. A NIFTY bar at 09:19:50 and a
    VIX bar at 09:19:59 cover the same five minutes; comparing raw stamps
    would hand the NIFTY bar the VIX of the interval before."""
    got = align_vix(
        [at(9, 19, 50)], "5",
        bars=[(at(9, 14, 59), 14.0, "choice"), (at(9, 19, 59), 16.0, "choice")],
    )
    assert got.values == [16.0]


def test_a_bar_never_sees_the_next_intervals_vix():
    got = align_vix([at(9, 24, 59)], "5", bars=[(at(9, 19, 59), 14.0, "c"), (at(9, 25, 1), 30.0, "c")])
    assert got.values == [14.0]


def test_a_daily_close_waits_for_the_next_session():
    """Known only at 15:30, so never used on its own day."""
    got = align_vix(
        [at(10, 0, day=2), at(9, 19, 59, day=3)], "5",
        bars=[],
        daily=[(dt.date(2026, 9, 1), 14.0, "choice"), (dt.date(2026, 9, 2), 18.0, "choice")],
    )
    assert got.values == [14.0, 18.0]
    assert got.sources == ["choice:close", "choice:close"]


def test_a_reading_too_old_is_no_reading():
    got = align_vix([at(10, 0, day=10)], "5", bars=[(at(15, 29, 59, day=1), 14.0, "c")])
    assert got.values == [None] and got.missing == 1
    assert MAX_AGE == dt.timedelta(days=4)


def test_hourly_bars_are_cut_from_the_open():
    assert interval_start(at(10, 14, 59), "60") == at(9, 15)
    assert interval_start(at(10, 15, 1), "60") == at(10, 15)
    assert interval_start(at(9, 4, 59), "5") == at(9, 0), "a pre-open bar keeps its own interval"


def test_a_daily_run_uses_the_same_days_close():
    """A daily bar's NIFTY and VIX closes print together at 15:30."""
    day = dt.datetime(2026, 9, 2, tzinfo=IST)
    got = align_vix([day], "D", bars=[(day, 17.0, "choice")],
                    daily=[(dt.date(2026, 9, 1), 12.0, "choice")])
    assert got.values == [17.0]


# ===================================================== the backtest runner


def _backtest(spots, vix, **kw):
    from engine.backtest.providers import ModelPriceProvider
    from engine.backtest.runner import Backtest, BacktestParams, weekly_expiry_resolver
    from engine.pricing.costs import ZERO_COST
    from engine.pricing.iv_surface import IVSurface

    expiry = spots[-1][0].date() + dt.timedelta(days=20)
    params = BacktestParams(
        strategy=StrategyConfig(lots=1, lot_size=65, max_condors=20, anchor_mode="floor", **kw),
        costs=ZERO_COST,
    )
    bt = Backtest(params, ModelPriceProvider(surface=IVSurface(atm_vol=0.12)),
                  weekly_expiry_resolver([expiry], max_dte=None))
    return bt.run(spots, vix)


def _path():
    prices = [24_000, 23_900, 23_800, 23_700, 23_500, 23_300, 23_550, 23_480, 23_390, 23_300]
    vix = [12.0, 13.0, 14.0, 16.0, 17.0, 18.0, 14.0, 14.5, 14.9, 14.0]
    spots = [(T0 + dt.timedelta(minutes=5 * i), float(p)) for i, p in enumerate(prices)]
    return spots, vix


def test_a_limit_without_vix_is_refused_not_run_blind():
    spots, _ = _path()
    with pytest.raises(ValueError, match="needs a VIX reading"):
        _backtest(spots, None, max_entry_vix=15.0)


def test_nothing_opens_while_vix_is_above_the_limit():
    spots, vix = _path()
    result = _backtest(spots, vix, max_entry_vix=15.0)
    opened = [(c.level, c.entry_time) for c in result.condors]
    high = {spots[i][0] for i, v in enumerate(vix) if v > 15.0}
    assert not any(when in high for _, when in opened)
    assert [lv for lv, _ in opened] == [24_000, 23_900, 23_800, 23_600, 23_500, 23_400, 23_300]
    passed = [s for s in result.skipped if "India VIX" in s[2]]
    assert [lv for _, lv, _ in passed] == [23_700.0]


def test_the_two_passes_agree_under_the_rule():
    """Pass 1 plans the legs; if it ignored the pause it would plan the wrong ones."""
    spots, vix = _path()
    result = _backtest(spots, vix, max_entry_vix=15.0)
    assert [t.level for t in result.triggers] == [c.level for c in result.condors]


def test_the_run_reports_what_the_rule_did():
    spots, vix = _path()
    result = _backtest(spots, vix, max_entry_vix=15.0)
    gate = result.vix_gate
    assert gate["limit"] == 15.0 and gate["bars"] == len(spots)
    assert gate["paused_bars"] == 3 and gate["spells"] == 1
    assert gate["levels_passed"] == 1 and gate["bars_without_vix"] == 0
    assert any("paused on 3 of 10 bars" in w for w in result.warnings)


def test_a_missing_reading_pauses_and_is_counted():
    spots, vix = _path()
    vix = [None] + vix[1:]
    result = _backtest(spots, vix, max_entry_vix=15.0)
    assert result.vix_gate["bars_without_vix"] == 1
    assert result.condors[0].entry_time == spots[1][0], "the anchor waited for a reading"


def test_without_a_limit_the_vix_series_changes_nothing():
    spots, vix = _path()
    plain = _backtest(spots, None)
    ignored = _backtest(spots, [99.0] * len(spots))
    assert [c.level for c in plain.condors] == [c.level for c in ignored.condors]
    assert plain.vix_gate == {} and ignored.vix_gate == {}


# ===================================================== the forward runner


class VixMarket:
    """The forward fake, plus a controllable India VIX."""

    def __init__(self, vix=12.0, as_of=None, fails=False):
        from engine.tests.test_forward_fills import FakeMarket, FakeMaster

        self.inner = FakeMarket(prices={26000: 24_000.0}, master=FakeMaster())
        self.vix, self.as_of, self.fails, self.vix_calls = vix, as_of, fails, 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def india_vix_now(self):
        from engine.choice.errors import ChoiceNoDataError

        self.vix_calls += 1
        if self.fails:
            raise ChoiceNoDataError("Choice returned no India VIX reading")
        return self.vix, self.as_of


def fwd(market, **kw):
    from engine.forward.runner import ForwardRunner

    return ForwardRunner(market=market,  # type: ignore[arg-type]
                         strategy=StrategyConfig(lots=1, lot_size=65, max_condors=20, **kw))


def test_a_live_run_pauses_resumes_and_says_so_once():
    market = VixMarket(vix=16.0)
    r = fwd(market, max_entry_vix=15.0)
    r.tick()
    assert r.condors == [] and r.ladder.anchor is None, "the anchor waits"
    r.tick()
    paused = [e for e in r.events if e.message.startswith("New positions paused")]
    assert len(paused) == 1, "said on the change, not every tick"
    assert "16.00 is above 15" in paused[0].message

    market.vix = 14.0
    r.tick()
    assert len(r.condors) == 1 and r.condors[0].level == 24_000.0
    assert any(e.message.startswith("New positions resumed") for e in r.events)

    snap = r.snapshot()["vix"]
    assert snap == {"limit": 15.0, "value": 14.0, "as_of": None, "paused": False, "problem": None}


def test_open_positions_are_untouched_by_a_pause():
    market = VixMarket(vix=12.0)
    r = fwd(market, max_entry_vix=15.0)
    r.tick()
    assert len(r.condors) == 1
    market.vix = 20.0
    market.inner.prices[26000] = 23_700.0
    r.tick()
    assert len(r.condors) == 1 and r.condors[0].is_open


def test_no_reading_holds_still_then_pauses_with_the_reason():
    """A missing reading first holds the run still -- nothing opens and the
    ladder is left where it was -- and only a spell of VIX_WAIT_LIMIT turns
    into the rule's pause, with the reason given."""
    from engine.forward.runner import VIX_WAIT_LIMIT

    r = fwd(VixMarket(fails=True), max_entry_vix=15.0)
    r.tick()
    assert r.condors == []
    assert r.snapshot()["vix"]["problem"].startswith("India VIX unavailable")
    assert not r.ladder.paused, "a missing reading is not yet a pause"
    assert not any("paused" in e.message for e in r.events)

    r._vix_missing_since = r._vix_missing_since - VIX_WAIT_LIMIT - dt.timedelta(seconds=1)
    r.tick()
    assert r.condors == [] and r.ladder.paused
    assert any(e.level == "warn" and "paused" in e.message for e in r.events)


def test_a_stale_reading_pauses():
    old = dt.datetime.now(tz=IST) - dt.timedelta(minutes=40)
    r = fwd(VixMarket(vix=12.0, as_of=old), max_entry_vix=15.0)
    r.tick()
    assert r.condors == []
    assert "too old" in r.vix_problem


def test_a_run_without_the_rule_never_reads_vix():
    market = VixMarket(vix=40.0)
    r = fwd(market)
    r.tick()
    assert len(r.condors) == 1 and market.vix_calls == 0
    assert r.snapshot()["vix"]["limit"] is None


def test_a_market_that_cannot_quote_vix_pauses_a_ruled_run():
    from engine.tests.test_forward_fills import FakeMarket, FakeMaster

    r = fwd(FakeMarket(prices={26000: 24_000.0}, master=FakeMaster()), max_entry_vix=15.0)
    r.tick()
    assert r.condors == [] and "cannot read India VIX" in r.vix_problem


def test_the_limit_and_the_pause_survive_a_restart():
    from engine.forward.runner import ForwardRunner

    market = VixMarket(vix=16.0)
    r = fwd(market, max_entry_vix=15.0)
    r.tick()
    state = r.to_state()
    back = ForwardRunner.restore(state, market=market)  # type: ignore[arg-type]
    assert back.strategy.max_entry_vix == 15.0 and back.ladder.paused
    assert back.last_vix == 16.0


def test_a_run_saved_before_the_rule_resumes_without_it():
    """New runs only: a run already trading keeps the rules it started with."""
    from engine.forward.runner import ForwardRunner

    r = fwd(VixMarket(vix=12.0))
    r.tick()
    state = r.to_state()
    state["strategy"].pop("max_entry_vix")
    state["ladder"].pop("paused")
    back = ForwardRunner.restore(state, market=VixMarket(vix=40.0))  # type: ignore[arg-type]
    assert back.strategy.max_entry_vix is None and not back.ladder.paused


def test_a_hic_run_keeps_the_limit_through_a_restart():
    from engine.forward.runner import ForwardRunner
    from engine.store.db import HIC

    market = VixMarket(vix=16.0)
    r = ForwardRunner(market=market, strategy_id=HIC,  # type: ignore[arg-type]
                      strategy=HicConfig(lots=1, lot_size=65, direction="both", max_entry_vix=15.0))
    r.tick()
    back = ForwardRunner.restore(r.to_state(), market=market)  # type: ignore[arg-type]
    assert isinstance(back.strategy, HicConfig) and back.strategy.max_entry_vix == 15.0


# ================================================================== the API


def test_new_runs_and_backtests_get_the_rule_at_15_unless_cleared():
    from engine.api import RunBacktestRequest, StartForwardRequest

    assert RunBacktestRequest().max_entry_vix == 15.0
    assert StartForwardRequest().max_entry_vix == 15.0
    assert RunBacktestRequest(max_entry_vix=None).max_entry_vix is None
    assert StartForwardRequest.model_validate({"max_entry_vix": 18.5}).max_entry_vix == 18.5
    with pytest.raises(Exception):
        StartForwardRequest(max_entry_vix=0)


@pytest.mark.parametrize("strategy", ["ladder", "hic"])
def test_both_strategies_carry_the_limit(strategy):
    from engine.api import StartForwardRequest, _strategy_config
    from engine.backtest.jobs import _strategy_for

    body = StartForwardRequest(strategy=strategy, max_entry_vix=17.0)
    assert _strategy_config(body, lot_size=65, strike_step=50.0).max_entry_vix == 17.0

    class M:
        class master:
            @staticmethod
            def strike_step(u, e):
                return 50.0

    p = {"strategy": strategy, "step": 100.0, "lots": 1, "max_condors": 20, "max_entry_vix": 17.0}
    assert _strategy_for(p, 65, [dt.date(2026, 10, 27)], M).max_entry_vix == 17.0



def test_an_opening_gap_before_vix_prints_opens_what_the_backtest_opens():
    """The first tick at 09:15 can come before India VIX has printed. Taken as
    a pause, the resume a few seconds later passed over every level the gap
    had crossed, with VIX at 12; the backtest, which reads the VIX of the bar's
    own interval, opened them all. Held still, the run sees the gap once."""
    market = VixMarket(fails=True)
    r = fwd(market, max_entry_vix=15.0)
    r.tick()                                   # no reading yet: holds still
    assert r.condors == [] and not r.ladder.passed

    market.fails, market.vix = False, 12.0     # VIX prints, well below the limit
    r.tick()
    assert r.condors, "the gap's levels open as they would without the rule"
    assert not r.ladder.passed



def test_levels_passed_while_paused_use_up_the_side_cap():
    """HIC caps its spreads as a count of rungs per side. Levels passed over
    while paused were not counted, so after a resume it fired deeper: three
    put spreads where two are allowed, at depths the configuration excludes."""
    ladder = Ladder(config=StrategyConfig(anchor_mode="floor", direction="both", max_down=3, max_up=3))
    feed(ladder, [24_000])
    feed(ladder, [23_750, 23_450], allowed=False, start=1)
    fired = feed(ladder, [23_450, 23_350, 23_250, 23_150], start=3)
    down = [lv for lv, side in fired if side == "down"]
    assert all(24_000 - lv <= 300 for lv in down), f"fired beyond the cap's depth: {down}"
    assert [p.level for p in ladder.passed] == [23_900.0, 23_800.0, 23_700.0, 23_600.0]


def test_passed_levels_survive_a_restart():
    config = StrategyConfig(anchor_mode="floor", direction="both", max_down=3, max_up=3)
    ladder = Ladder(config=config)
    feed(ladder, [24_000])
    feed(ladder, [23_750, 23_450], allowed=False, start=1)
    feed(ladder, [23_450], start=3)
    restored = Ladder(config=config)
    restored.load_state(ladder.dump_state())
    assert restored.passed_levels == ladder.passed_levels and restored.passed_levels
    assert restored.down_count == ladder.down_count
