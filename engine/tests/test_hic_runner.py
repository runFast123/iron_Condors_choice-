"""HIC driven through the forward runner, and back out of saved state.

The strategy is correct in isolation (test_hic). This is the part that can
still go wrong: the runner has to build the right shape at each level, persist
what it built, and rebuild it as the same thing after a restart. A unit that
came back as the wrong type would have the wrong risk maths attached to a real
position.
"""

from __future__ import annotations


import pytest

from engine.forward.runner import ForwardRunner
from engine.strategy.condor import Condor, UnitKind
from engine.strategy.hic import HicConfig
from engine.strategy.vertical import VerticalSpread
from engine.tests.test_forward_fills import EXPIRY, FakeMarket, FakeMaster

ANCHOR = 23_400.0


def hic(**kw) -> HicConfig:
    return HicConfig(
        **{
            "lots": 1, "lot_size": 65, "direction": "both",
            "max_condors": 40, "max_down": 25, "max_up": 25,
            **kw,
        }
    )


def runner(config: HicConfig | None = None) -> ForwardRunner:
    r = ForwardRunner(
        market=FakeMarket(master=FakeMaster()),  # type: ignore[arg-type]
        strategy=config or hic(),
        strategy_id="hic",
    )
    # Anchor explicitly, so a test says which levels it means rather than
    # depending on where the fake's first quote happens to land.
    r.ladder.anchor_mode = "explicit"
    r.ladder.explicit_anchor = ANCHOR
    r.ladder.anchor = ANCHOR
    r.ladder.last_level = ANCHOR
    r.ladder.high_level = ANCHOR
    return r


def _open(r: ForwardRunner, *levels: float) -> None:
    for level in levels:
        r._open_condor(level, EXPIRY)


# ============================== the runner builds the right shape


def test_the_core_band_opens_condors_and_beyond_it_opens_spreads():
    r = runner()

    _open(r, 23_400, 23_300, 23_200, 23_100)

    assert [type(u) for u in r.condors] == [Condor, Condor, VerticalSpread, VerticalSpread]
    assert [u.kind for u in r.condors] == [
        UnitKind.CONDOR, UnitKind.CONDOR,
        UnitKind.PUT_DEBIT_SPREAD, UnitKind.PUT_DEBIT_SPREAD,
    ]
    assert [u.k for u in r.condors] == [0, -1, -2, -3]


def test_a_rally_past_the_band_buys_calls_not_puts():
    r = runner()

    _open(r, 23_400, 23_600, 23_700)

    spreads = [u for u in r.condors if u.kind is not UnitKind.CONDOR]
    assert [u.kind for u in spreads] == [UnitKind.CALL_DEBIT_SPREAD] * 2
    assert all(fl.leg.right == "CE" for u in spreads for fl in u.legs)


def test_each_spread_has_two_legs_and_each_condor_four():
    r = runner()

    _open(r, 23_400, 23_300, 23_200, 23_600)

    assert [len(u.legs) for u in r.condors] == [4, 4, 2, 2]


def test_the_ladder_still_opens_condors_at_every_level():
    """The other strategy must be untouched by HIC existing."""
    from engine.strategy.condor import StrategyConfig

    r = ForwardRunner(
        market=FakeMarket(master=FakeMaster()),  # type: ignore[arg-type]
        strategy=StrategyConfig(lots=1, lot_size=65, max_condors=40),
    )
    r.ladder.anchor = ANCHOR
    r.ladder.last_level = ANCHOR

    _open(r, 23_400, 23_300, 23_200, 23_100)

    assert all(isinstance(u, Condor) for u in r.condors)
    assert all(u.kind is UnitKind.CONDOR for u in r.condors)
    assert all(u.k is None for u in r.condors), "a ladder rung has no step index"


# ============================== risk is the right risk


def test_a_spread_in_a_live_run_reports_the_debit_not_a_wing():
    """The misreport this whole split exists to stop, checked where it would
    actually have reached a user: on a position the runner opened."""
    r = runner()
    _open(r, 23_400, 23_300, 23_200)
    spread = r.condors[-1]

    paid = -spread.credit
    assert paid > 0, "the fake market should price this as a debit"
    # Costs are part of the worst case, the same way the condor counts them.
    assert spread.max_loss == pytest.approx(paid + spread.entry_costs + spread.exit_costs)
    assert spread.max_loss < spread.config.qty * 200, "a wing's width would be larger"
    assert spread.max_profit > 0


def test_the_snapshot_reports_a_mixed_book_without_confusing_the_kinds():
    r = runner()
    _open(r, 23_400, 23_300, 23_200, 23_600)

    positions = r.snapshot()["positions"]
    assert [p["level"] for p in positions] == [23_400, 23_300, 23_200, 23_600]

    by_level = {p["level"]: p for p in positions}
    assert len(by_level[23_400]["legs"]) == 4
    assert len(by_level[23_200]["legs"]) == 2


# ============================== it survives a restart


def test_a_mixed_book_round_trips_as_the_same_shapes():
    """A unit rebuilt as the wrong type would carry the wrong risk formula on
    a real position, which is the failure worth guarding."""
    r = runner()
    _open(r, 23_400, 23_300, 23_200, 23_100, 23_600)
    before = [(type(u), u.kind, u.k, round(u.max_loss, 4)) for u in r.condors]

    revived = ForwardRunner.restore(
        r.to_state(), market=r.market, state_path=None, strategy_id="hic",
    )

    after = [(type(u), u.kind, u.k, round(u.max_loss, 4)) for u in revived.condors]
    assert after == before


def test_a_resumed_run_still_opens_spreads_at_new_levels():
    """Round-tripping what is already held is only half of it.

    `restore` filtered the saved config against `StrategyConfig`'s field names
    and built one, so the band and spread settings were dropped and the
    rebuilt object was a plain ladder config. Everything already open came
    back correctly -- each unit carries its own type -- so the test above
    passed while the *next* level opened a four-leg condor at several times
    the risk, under a run the database still labelled "hic".
    """
    r = runner()
    _open(r, 23_400, 23_300, 23_200)

    revived = ForwardRunner.restore(
        r.to_state(), market=r.market, state_path=None, strategy_id="hic",
    )
    assert isinstance(revived.strategy, HicConfig)
    assert revived.strategy.full_band_steps == r.strategy.full_band_steps
    assert revived.strategy.half_mode == r.strategy.half_mode

    _open(revived, 23_100)
    opened = revived.condors[-1]
    assert isinstance(opened, VerticalSpread), "a ladder condor at a spread level"
    assert opened.kind is UnitKind.PUT_DEBIT_SPREAD
    assert len(opened.legs) == 2
    assert opened.max_loss < opened.config.qty * 200


def test_a_resumed_ladder_is_not_handed_a_hic_config():
    """The dispatch has to work in the other direction too, or a ladder whose
    blob happened to carry the fields would start buying spreads."""
    from engine.strategy.condor import StrategyConfig

    r = runner()
    _open(r, 23_400)
    state = r.to_state()

    revived = ForwardRunner.restore(
        state, market=r.market, state_path=None, strategy_id="ladder",
    )
    assert type(revived.strategy) is StrategyConfig


def test_state_written_before_hic_existed_comes_back_as_condors():
    """Both live runs are stored in exactly that shape: no kind, no k."""
    from engine.strategy.condor import StrategyConfig

    r = ForwardRunner(
        market=FakeMarket(master=FakeMaster()),  # type: ignore[arg-type]
        strategy=StrategyConfig(lots=1, lot_size=65),
    )
    r.ladder.anchor = ANCHOR
    r.ladder.last_level = ANCHOR
    _open(r, 23_400, 23_300)

    state = r.to_state()
    for raw in state["condors"]:
        del raw["kind"]
        del raw["k"]

    revived = ForwardRunner.restore(state, market=r.market, state_path=None)

    assert all(isinstance(u, Condor) for u in revived.condors)
    assert all(u.kind is UnitKind.CONDOR for u in revived.condors)


def test_a_resumed_hic_run_does_not_reopen_what_it_holds():
    r = runner()
    _open(r, 23_400, 23_300, 23_200)

    revived = ForwardRunner.restore(
        r.to_state(), market=r.market, state_path=None, strategy_id="hic",
    )
    revived.ladder.load_state(r.ladder.dump_state())

    assert revived.ladder.fired_levels == r.ladder.fired_levels


# ============================== the quote-scale warning


def _warnings(r: ForwardRunner) -> list[str]:
    return [e.message for e in r.events if e.level == "warn"]


def test_a_sensibly_priced_debit_spread_raises_no_warning():
    """The check used to fire on every one of them: it read a debit as a
    fault, which it is for a condor and is the whole point of a spread.

    Priced by hand, because the fake market puts a 200-point spread at about a
    hundredth of its width -- which the check is right to complain about, and
    which is therefore no use for showing that it stays quiet when it should.
    """
    r = runner()
    _open(r, 23_400, 23_300, 23_200)
    spread = r.condors[-1]
    for fl in spread.legs:
        fl.entry_price = 150.0 if fl.leg.side is fl.leg.side.BUY else 85.0

    r.events.clear()
    r._check_premium_is_plausible(spread, spread.level)

    assert _warnings(r) == []


def test_the_check_still_catches_a_spread_priced_a_hundredth_of_its_value():
    """The quote-scale fault the whole check exists for, now detectable on a
    bought structure as well as a sold one."""
    r = runner()
    _open(r, 23_400, 23_300, 23_200)
    spread = r.condors[-1]
    for fl in spread.legs:
        fl.entry_price = 1.50 if fl.leg.side is fl.leg.side.BUY else 0.85

    r.events.clear()
    r._check_premium_is_plausible(spread, spread.level)

    assert any("implausibly small" in w for w in _warnings(r))


def test_a_condor_opened_at_a_debit_still_warns():
    """The fault the check was written for has to survive its generalisation."""
    r = runner()
    _open(r, 23_400)
    condor = r.condors[0]
    # Flip it into a debit the way a bad quote scale would.
    for fl in condor.legs:
        fl.entry_price = 10.0 if fl.leg.side is fl.leg.side.SELL else 500.0

    r.events.clear()
    r._check_premium_is_plausible(condor, condor.level)

    assert any("net debit" in w for w in _warnings(r))


def test_a_debit_spread_opened_for_a_credit_warns_too():
    """Newly detectable. The old check only ever looked for the condor's
    failure, so this one was invisible."""
    r = runner()
    _open(r, 23_400, 23_300, 23_200)
    spread = r.condors[-1]
    for fl in spread.legs:
        fl.entry_price = 10.0 if fl.leg.side is fl.leg.side.BUY else 500.0

    r.events.clear()
    r._check_premium_is_plausible(spread, spread.level)

    assert any("opened for a credit" in w for w in _warnings(r))


def test_a_spread_costing_more_than_it_can_ever_pay_warns():
    r = runner()
    _open(r, 23_400, 23_300, 23_200)
    spread = r.condors[-1]
    width_value = spread.width * spread.config.qty
    for fl in spread.legs:
        fl.entry_price = 400.0 if fl.leg.side is fl.leg.side.BUY else 1.0

    r.events.clear()
    r._check_premium_is_plausible(spread, spread.level)

    assert -spread.credit >= width_value
    assert any("maximum payout" in w for w in _warnings(r))


def test_a_run_labelled_hic_that_was_never_hic_keeps_trading_what_it_has():
    """The reverse version-skew, and the one that reaches a live position.

    An engine that knew the name "hic" but had no code to build one recorded
    the column and traded a ladder, so the saved geometry has none of HIC's
    settings. Filling them in from defaults on restore would change what a
    running campaign trades into something nobody chose -- with condors
    already open at levels the new shape calls spreads.
    """
    from engine.strategy.condor import StrategyConfig

    r = ForwardRunner(
        market=FakeMarket(master=FakeMaster()),  # type: ignore[arg-type]
        strategy=StrategyConfig(lots=1, lot_size=65, step=100.0, max_condors=20),
        strategy_id="hic",
    )
    r.expiry = EXPIRY
    r.ladder.anchor = ANCHOR
    r.ladder.last_level = ANCHOR
    r._open_condor(ANCHOR, EXPIRY, side="anchor")

    revived = ForwardRunner.restore(
        r.to_state(), market=r.market, state_path=None, strategy_id="hic", run_key="hic",
    )

    assert type(revived.strategy) is StrategyConfig, "not promoted out of defaults"
    assert [e for e in revived.events if e.level == "error"], "and it says so"
    assert "trading a ladder under that name" in revived.events[-1].message

    # It goes on opening what it has been opening.
    _open(revived, 23_300)
    assert isinstance(revived.condors[-1], Condor)
