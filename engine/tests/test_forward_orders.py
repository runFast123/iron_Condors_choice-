"""Tests for the live order path.

This is the code that can lose real money, and it was the least covered in the
project. Everything here exercises `_place`, `_open_condor` and the tick loop
directly rather than through the API, so the wire payload itself is asserted.
"""

from __future__ import annotations

import datetime as dt

import pytest

from engine.choice.errors import ChoiceError
from engine.choice.instruments import Contract
from engine.forward.runner import ForwardRunner
from engine.strategy.condor import Side, StrategyConfig, build_legs

LOT = 65


def cfg(**kw) -> StrategyConfig:
    return StrategyConfig(**{"lots": 1, "lot_size": LOT, "max_condors": 20, **kw})


def contract(token: int, strike: float, right: str) -> Contract:
    return Contract(
        token=token, segment_id=2, symbol="NIFTY", description="", lot_size=LOT,
        expiry=dt.date(2026, 9, 8), strike=strike, option_type=right, underlying="NIFTY",
    )


class FakeSession:
    """Records every order payload and replays a scripted response."""

    def __init__(self, response=None, raises=False):
        self.orders: list[dict] = []
        self.response = response if response is not None else {
            "Status": "Success", "Response": {"OrderNo": "ORD-1"}
        }
        self.raises = raises

    def request(self, method, endpoint, data=None, **kw):
        if "NewOrder" in endpoint:
            self.orders.append(data)
            if self.raises:
                raise ChoiceError("broker refused the connection")
            return self.response
        return {"Status": "Success", "Response": {}}


class FakeMarket:
    def __init__(self, session, quotes=None, master=None):
        self.session = session
        self.quotes = quotes or {}
        self.master = master

    def touchline(self, contracts):
        return {c.token: self.quotes.get(c.token, 100.0) for c in contracts}


class FakeMaster:
    """Resolves any strike, so leg lookup never gets in the way."""

    def __init__(self, missing: set | None = None):
        self.missing = missing or set()
        self._tokens: dict[tuple, int] = {}

    def option(self, underlying, expiry, strike, right):
        if (strike, right) in self.missing:
            raise ChoiceError(f"no contract for {strike} {right}")
        key = (expiry, strike, right)
        self._tokens.setdefault(key, 40_000 + len(self._tokens))
        return contract(self._tokens[key], strike, right)


def runner(mode="paper", quotes=None, session=None, missing=None, **kw) -> ForwardRunner:
    session = session or FakeSession()
    market = FakeMarket(session, quotes=quotes, master=FakeMaster(missing))
    r = ForwardRunner(market=market, strategy=cfg(**kw), mode=mode)  # type: ignore[arg-type]
    return r


# ============================================================ order payload


def test_live_order_payload_matches_the_choice_contract():
    session = FakeSession()
    r = runner(mode="live", session=session)
    r.arm()
    leg = build_legs(24_000, cfg())[2]          # SELL PE 23,800
    assert leg.side is Side.SELL

    r._place(leg, contract(42632, 23_800, "PE"), ltp=60.0)

    order = session.orders[0]
    assert order["OrderType"] == "RL_LIMIT"      # Choice has no market order
    assert order["BS"] == 2                      # 2 = sell
    assert order["Qty"] == LOT                   # shares, not lots
    assert order["Validity"] == 1
    assert order["ProductType"] == "M"
    assert order["Token"] == 42632
    assert order["SegmentId"] == 2


def test_prices_go_on_the_wire_in_paisa():
    session = FakeSession()
    r = runner(mode="live", session=session)
    r.arm()
    leg = build_legs(24_000, cfg())[0]           # BUY PE
    r._place(leg, contract(1, 23_600, "PE"), ltp=25.0)
    # 25.00 + 1% buffer = 25.25 -> 2525 paisa. An integer, never rupees.
    assert session.orders[0]["Price"] == 2525
    assert isinstance(session.orders[0]["Price"], int)


def test_buy_prices_through_the_offer_and_sell_through_the_bid():
    """A limit must cross the touch or it may never fill."""
    session = FakeSession()
    r = runner(mode="live", session=session)
    r.arm()
    legs = build_legs(24_000, cfg())
    buy = next(l for l in legs if l.side is Side.BUY)
    sell = next(l for l in legs if l.side is Side.SELL)

    r._place(buy, contract(1, 23_600, "PE"), ltp=100.0)
    r._place(sell, contract(2, 23_800, "PE"), ltp=100.0)

    assert session.orders[0]["Price"] > 10_000    # buy above 100.00
    assert session.orders[1]["Price"] < 10_000    # sell below


def test_a_cheap_option_never_gets_a_non_positive_limit():
    session = FakeSession()
    r = runner(mode="live", session=session)
    r.arm()
    sell = next(l for l in build_legs(24_000, cfg()) if l.side is Side.SELL)
    r._place(sell, contract(1, 23_800, "PE"), ltp=0.05)
    assert session.orders[0]["Price"] >= 5        # >= 0.05 in paisa


# ====================================================== client order number


def test_every_leg_gets_a_distinct_client_order_number():
    """The whole reason for bypassing kkunal, which hardcodes 123456."""
    session = FakeSession()
    r = runner(mode="live", session=session)
    r.arm()
    for i, leg in enumerate(build_legs(24_000, cfg())):
        r._place(leg, contract(i, leg.strike, leg.right), ltp=50.0)

    numbers = [o["ClientOrderNo"] for o in session.orders]
    assert len(set(numbers)) == 4, f"duplicate order numbers: {numbers}"


def test_order_numbers_stay_distinct_across_condors_at_different_levels():
    """A strike-hash scheme collided for levels 1000 apart (23800 vs 24800)."""
    session = FakeSession()
    r = runner(mode="live", session=session)
    r.arm()
    for level in (24_000, 24_800, 23_000):
        for leg in build_legs(level, cfg()):
            r._place(leg, contract(1, leg.strike, leg.right), ltp=50.0)

    numbers = [o["ClientOrderNo"] for o in session.orders]
    assert len(set(numbers)) == len(numbers), "order numbers collided across levels"


# ============================================================== rejections


def test_a_rejected_order_returns_no_id():
    session = FakeSession(response={"Status": "Failure", "Message": "margin shortfall"})
    r = runner(mode="live", session=session)
    r.arm()
    leg = build_legs(24_000, cfg())[0]
    assert r._place(leg, contract(1, 23_600, "PE"), ltp=25.0) is None
    assert any(e.level == "error" for e in r.events)


def test_a_transport_failure_returns_no_id():
    r = runner(mode="live", session=FakeSession(raises=True))
    r.arm()
    leg = build_legs(24_000, cfg())[0]
    assert r._place(leg, contract(1, 23_600, "PE"), ltp=25.0) is None


def test_an_accepted_order_with_no_order_number_is_a_failure():
    """Without an id the leg cannot be modified, cancelled or reconciled.

    This used to return "" which is not None, so the caller treated an
    unidentifiable order as a good fill.
    """
    session = FakeSession(response={"Status": "Success", "Response": {}})
    r = runner(mode="live", session=session)
    r.arm()
    leg = build_legs(24_000, cfg())[0]
    assert r._place(leg, contract(1, 23_600, "PE"), ltp=25.0) is None


# ============================================================ opening a condor


def test_paper_mode_places_nothing():
    session = FakeSession()
    r = runner(mode="paper", session=session, quotes={})
    r.expiry = dt.date(2026, 9, 8)
    condor = r._open_condor(24_000, r.expiry)
    assert condor is not None
    assert session.orders == [], "paper mode must not touch the exchange"
    assert len(r.fills) == 4


def test_live_but_unarmed_places_nothing():
    """The arm step is the safety; starting a live run is not consent to trade."""
    session = FakeSession()
    r = runner(mode="live", session=session)
    r.expiry = dt.date(2026, 9, 8)
    assert r.armed is False
    r._open_condor(24_000, r.expiry)
    assert session.orders == []


def test_protective_wings_are_sent_before_the_shorts():
    """A naked short, even briefly, spikes margin and invites rejection."""
    session = FakeSession()
    r = runner(mode="live", session=session)
    r.arm()
    r.expiry = dt.date(2026, 9, 8)
    r._open_condor(24_000, r.expiry)

    sides = [o["BS"] for o in session.orders]     # 1 = buy, 2 = sell
    assert sides == [1, 1, 2, 2], f"order sequence was {sides}"


def test_a_rejected_leg_aborts_the_whole_condor():
    session = FakeSession(response={"Status": "Failure", "Message": "rejected"})
    r = runner(mode="live", session=session)
    r.arm()
    r.expiry = dt.date(2026, 9, 8)
    assert r._open_condor(24_000, r.expiry) is None
    assert r.condors == []


def test_a_leg_without_a_quote_skips_the_condor_entirely():
    """Three of four legs would leave a naked short in the book."""
    class NoQuoteMarket(FakeMarket):
        def touchline(self, contracts):
            return {}                            # nothing quotable

    session = FakeSession()
    r = ForwardRunner(
        market=NoQuoteMarket(session, master=FakeMaster()),  # type: ignore[arg-type]
        strategy=cfg(), mode="live",
    )
    r.arm()
    r.expiry = dt.date(2026, 9, 8)
    assert r._open_condor(24_000, r.expiry) is None
    assert session.orders == []
    assert r.condors == []


def test_an_unresolvable_strike_skips_the_condor():
    r = runner(mode="live", missing={(23_600.0, "PE")})
    r.arm()
    r.expiry = dt.date(2026, 9, 8)
    assert r._open_condor(24_000, r.expiry) is None


def test_opening_records_a_fill_per_leg_with_its_token():
    r = runner(mode="paper", quotes={})
    r.expiry = dt.date(2026, 9, 8)
    r._open_condor(24_000, r.expiry)
    assert len(r.fills) == 4
    assert all(f.action == "OPEN" for f in r.fills)
    assert all(f.token is not None for f in r.fills)
    assert all(f.mode == "paper" for f in r.fills)


def test_credit_and_max_loss_are_computed_on_open():
    r = runner(mode="paper", quotes={})
    r.expiry = dt.date(2026, 9, 8)
    condor = r._open_condor(24_000, r.expiry)
    assert condor is not None
    # Flat quotes mean no credit, so max loss is the full wing.
    assert condor.max_loss == pytest.approx(200 * LOT - condor.net_credit)


# ================================================================ safety


def test_the_daily_loss_limit_disarms_the_run():
    """The kill switch is the last line of defence; it must actually fire."""
    from engine.config import EngineConfig
    import engine.forward.runner as mod

    original = mod.engine_config
    mod.engine_config = EngineConfig(daily_loss_limit=1_000, max_condors=20)
    try:
        r = runner(mode="live", quotes={})
        r.arm()
        r.expiry = dt.date(2026, 9, 8)
        r.realised = -5_000.0            # already past the limit
        r.market.master = FakeMaster()   # type: ignore[attr-defined]

        class Idx:
            token, segment_id = 26000, 1
        r.market.master.index = lambda name: Idx()          # type: ignore[attr-defined]
        r.market.ltp = lambda c: 24_000.0                   # type: ignore[attr-defined]

        r.tick()
        assert r.stopped_reason is not None
        assert "loss limit" in r.stopped_reason
        assert r.armed is False
    finally:
        mod.engine_config = original


def test_the_condor_cap_stops_new_entries():
    from engine.config import EngineConfig
    import engine.forward.runner as mod

    original = mod.engine_config
    mod.engine_config = EngineConfig(max_condors=1, daily_loss_limit=10_000_000)
    try:
        r = runner(mode="paper", quotes={})
        r.expiry = dt.date(2026, 9, 8)

        class Idx:
            token, segment_id = 26000, 1
        r.market.master.index = lambda name: Idx()          # type: ignore[attr-defined]
        prices = iter([24_000.0, 23_900.0, 23_800.0])
        r.market.ltp = lambda c: next(prices, 23_800.0)     # type: ignore[attr-defined]

        for _ in range(3):
            r.tick()
        assert len([c for c in r.condors if c.is_open]) <= 1
        assert any("Max concurrent" in e.message for e in r.events)
    finally:
        mod.engine_config = original


def test_a_failed_quote_is_surfaced_not_swallowed():
    r = runner(mode="paper")

    class Idx:
        token, segment_id = 26000, 1
    r.market.master.index = lambda name: Idx()              # type: ignore[attr-defined]

    def boom(c):
        raise ChoiceError("MultipleTouchline returned no usable quotes")
    r.market.ltp = boom                                      # type: ignore[attr-defined]

    r.tick()
    assert r.last_error and "no usable quotes" in r.last_error
    assert r.snapshot()["session"]["last_error"] == r.last_error
