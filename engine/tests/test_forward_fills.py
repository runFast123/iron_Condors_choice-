"""Tests for paper fill pricing and the condor lifecycle.

This platform places no orders, so the fill model is not a rehearsal for
something real -- it is the whole result. Filling at the last traded price
would report a P&L nobody could have traded, so these pin down that every leg
crosses the touch, that a missing order book is charged a modelled spread
rather than a free one, and that the run says which of the two it used.
"""

from __future__ import annotations

import datetime as dt

import pytest

from engine.choice.errors import ChoiceError
from engine.choice.instruments import Contract
from engine.data.market import Quote
from engine.forward.fills import TICK, FillModel
from engine.forward.runner import ForwardRunner
from engine.strategy.condor import Side, StrategyConfig, build_legs

LOT = 65


def cfg(**kw) -> StrategyConfig:
    return StrategyConfig(**{"lots": 1, "lot_size": LOT, "max_condors": 20, **kw})


def contract(token: int, strike: float, right: str) -> Contract:
    return Contract(
        token=token, segment_id=2, symbol="NIFTY", description="", lot_size=LOT,
        expiry=EXPIRY, strike=strike, option_type=right, underlying="NIFTY",
    )


class FakeMarket:
    """Serves quotes; raises if `fails` is set, to exercise the error path."""

    def __init__(self, prices=None, master=None, depth=None, fails=False, as_of=None):
        self.session = None
        self.prices = prices or {}
        self.depth = depth or {}
        self.master = master
        self.fails = fails
        self.calls = 0
        # When these prices printed. Set it to serve candle-derived quotes,
        # which is what the index actually gets.
        self.as_of = as_of

    def quotes(self, contracts):
        self.calls += 1
        if self.fails:
            raise ChoiceError("MultipleTouchline returned no usable quotes")
        out = {}
        for c in contracts:
            ltp = self.prices.get(c.token, 100.0)
            if ltp is None:
                continue
            bid, ask = self.depth.get(c.token, (None, None))
            out[c.token] = Quote(
                token=c.token, ltp=ltp, bid=bid, ask=ask,
                stale=self.as_of is not None, as_of=self.as_of,
            )
        return out

    def touchline(self, contracts):
        return {t: q.ltp for t, q in self.quotes(contracts).items()}

    def ltp(self, c):
        return self.touchline([c]).get(c.token)


class FakeMaster:
    """Resolves any strike, so leg lookup never gets in the way."""

    def __init__(self, missing: set | None = None):
        self.missing = missing or set()
        self._tokens: dict[tuple, int] = {}

    def index(self, name):
        return Contract(
            token=26000, segment_id=1, symbol="NIFTY", description="Nifty 50", lot_size=LOT
        )

    def nearest_expiry(self, underlying, on, *, min_days=0):
        return EXPIRY

    def expiries(self, underlying, *, after=None):
        return [e for e in (EXPIRY, EXPIRY + dt.timedelta(days=28))
                if after is None or e >= after]

    def option(self, underlying, expiry, strike, right):
        if (strike, right) in self.missing:
            raise ChoiceError(f"no contract for {strike} {right}")
        key = (expiry, strike, right)
        self._tokens.setdefault(key, 40_000 + len(self._tokens))
        return contract(self._tokens[key], strike, right)


def runner(prices=None, depth=None, missing=None, fails=False, fill_model=None, **kw) -> ForwardRunner:
    market = FakeMarket(prices=prices, depth=depth, master=FakeMaster(missing), fails=fails)
    return ForwardRunner(  # type: ignore[arg-type]
        market=market, strategy=cfg(**kw), fill_model=fill_model
    )


# Relative to today: a hard-coded date silently became "in the past" and the
# cadence resolver -- which refuses to trade an expiry that has already gone --
# then had nothing to return.
EXPIRY = dt.date.today() + dt.timedelta(days=7)


# ==================================================== the fill model itself


def test_a_buy_lifts_the_offer_and_a_sell_hits_the_bid():
    """With a real book there is nothing to model: use the actual prices."""
    model = FillModel()
    quote = Quote(token=1, ltp=100.0, bid=98.0, ask=102.0)
    assert model.fill(quote, Side.BUY).price == pytest.approx(102.0)
    assert model.fill(quote, Side.SELL).price == pytest.approx(98.0)


def test_a_real_book_is_not_flagged_as_modelled_and_records_its_slippage():
    quote = Quote(token=1, ltp=100.0, bid=98.0, ask=102.0)
    fill = FillModel().fill(quote, Side.BUY)
    assert fill.spread_modelled is False
    assert fill.reference == pytest.approx(100.0)      # mid
    assert fill.slippage == pytest.approx(2.0)


def test_a_missing_book_is_charged_a_modelled_spread_not_a_free_one():
    """The assumption that flatters every backtest is spread == 0."""
    quote = Quote(token=1, ltp=100.0)
    model = FillModel(modelled_spread_pct=0.02)
    buy, sell = model.fill(quote, Side.BUY), model.fill(quote, Side.SELL)
    assert buy.price > 100.0 > sell.price
    assert buy.spread_modelled and sell.spread_modelled
    assert buy.price == pytest.approx(101.0)           # half of a 2% spread
    assert sell.price == pytest.approx(99.0)


def test_a_cheap_wing_still_pays_at_least_one_tick():
    """A percentage spread rounds to nothing on a 40-paisa wing."""
    fill = FillModel().fill(Quote(token=1, ltp=0.40), Side.BUY)
    assert fill.price >= 0.40 + TICK - 1e-9


def test_fills_land_on_the_exchange_tick_grid():
    fill = FillModel().fill(Quote(token=1, ltp=123.37), Side.BUY)
    assert round(fill.price / TICK) == pytest.approx(fill.price / TICK)


def test_a_fill_never_prices_below_the_minimum_tick():
    fill = FillModel().fill(Quote(token=1, ltp=0.05), Side.SELL)
    assert fill.price >= TICK


def test_a_crossed_book_is_treated_as_no_book():
    quote = Quote(token=1, ltp=100.0, bid=102.0, ask=98.0)
    assert FillModel().fill(quote, Side.BUY).spread_modelled is True


def test_an_absurdly_wide_book_is_refused_rather_than_filled():
    """Filling through a 90%-wide book invents a price that never traded."""
    quote = Quote(token=1, ltp=100.0, bid=10.0, ask=190.0)
    assert FillModel(max_spread_pct=0.50).fill(quote, Side.BUY) is None


def test_the_exit_fill_crosses_the_spread_the_other_way():
    model = FillModel()
    quote = Quote(token=1, ltp=100.0, bid=98.0, ask=102.0)
    # A leg entered short is closed by buying, so it lifts the offer.
    assert model.exit_fill(quote, Side.SELL).price == pytest.approx(102.0)
    assert model.exit_fill(quote, Side.BUY).price == pytest.approx(98.0)


# ============================================================ opening a condor


def test_opening_crosses_the_spread_on_every_leg():
    """Sold legs come in below mid, bought legs above -- never all at mid."""
    r = runner()
    legs = build_legs(24_000, cfg())
    tokens = {}
    for leg in legs:
        c = r.market.master.option("NIFTY", EXPIRY, leg.strike, leg.right)
        tokens[leg] = c.token
    r.market.depth = {t: (95.0, 105.0) for t in tokens.values()}

    condor = r._open_condor(24_000, EXPIRY)
    assert condor is not None
    for fl in condor.legs:
        expected = 105.0 if fl.leg.side is Side.BUY else 95.0
        assert fl.entry_price == pytest.approx(expected)


def test_a_leg_without_a_quote_skips_the_condor_entirely():
    """Three of four legs is an unhedged short, so refuse the whole thing."""
    r = runner(prices={40_000: None})
    assert r._open_condor(24_000, EXPIRY) is None
    assert r.condors == []
    assert r.fills == []


def test_an_unresolvable_strike_skips_the_condor():
    r = runner(missing={(23_600.0, "PE")})
    assert r._open_condor(24_000, EXPIRY) is None
    assert r.condors == []


def test_a_leg_with_an_untradeable_book_skips_the_condor():
    r = runner()
    legs = build_legs(24_000, cfg())
    first = r.market.master.option("NIFTY", EXPIRY, legs[0].strike, legs[0].right)
    r.market.depth = {first.token: (1.0, 199.0)}
    assert r._open_condor(24_000, EXPIRY) is None
    assert r.condors == []


def test_opening_records_a_fill_per_leg_with_its_token_and_provenance():
    r = runner()
    condor = r._open_condor(24_000, EXPIRY)
    assert condor is not None
    assert len(r.fills) == 4
    for fill in r.fills:
        assert fill.action == "OPEN"
        assert fill.token is not None
        assert fill.mode == "paper"
        assert fill.reference is not None
        assert fill.spread_modelled is True          # no depth in this fixture


def test_fill_quality_is_reported_so_optimism_is_visible():
    r = runner()
    r._open_condor(24_000, EXPIRY)
    quality = r.snapshot()["fill_quality"]
    assert quality["legs_on_modelled_spread"] == 4
    assert quality["legs_on_real_depth"] == 0
    assert quality["real_depth_fraction"] == 0.0
    assert quality["total_slippage"] > 0


def test_real_depth_is_reported_separately_from_modelled():
    r = runner()
    legs = build_legs(24_000, cfg())
    tokens = [r.market.master.option("NIFTY", EXPIRY, leg.strike, leg.right).token for leg in legs]
    r.market.depth = {t: (99.0, 101.0) for t in tokens}
    r._open_condor(24_000, EXPIRY)
    quality = r.snapshot()["fill_quality"]
    assert quality["legs_on_real_depth"] == 4
    assert quality["real_depth_fraction"] == 1.0


def test_credit_and_max_loss_are_computed_on_open():
    r = runner()
    condor = r._open_condor(24_000, EXPIRY)
    assert condor is not None
    # Flat 100.0 quotes make the structure symmetric, so the modelled spread is
    # the only thing separating the shorts from the wings.
    assert condor.max_loss > 0


def test_a_net_debit_condor_is_flagged_as_a_quote_problem():
    """An iron condor cannot be a debit unless the prices are wrong."""
    r = runner()
    legs = build_legs(24_000, cfg())
    tokens = {leg: r.market.master.option("NIFTY", EXPIRY, leg.strike, leg.right).token
              for leg in legs}
    # Wings priced above the shorts is only possible if the quotes are wrong,
    # which is exactly the condition the warning exists to catch.
    prices = {token: (200.0 if leg.side is Side.BUY else 10.0)
              for leg, token in tokens.items()}
    r.market.prices = prices
    condor = r._open_condor(24_000, EXPIRY)
    assert condor is not None and condor.credit < 0
    assert any("net debit" in e.message for e in r.events)


# ================================================================ tick loop


def test_the_condor_cap_stops_new_entries():
    r = runner(max_condors=1, prices={26000: 24_000.0})
    r.tick()                                  # anchors and opens the first
    assert len(r.condors) == 1
    r.market.prices[26000] = 23_900.0         # next level down
    r.tick()
    # The ladder refuses to fire past the cap, so the runner's own backstop
    # never even sees the trigger. Either way the position count holds.
    assert len([c for c in r.condors if c.is_open]) == 1


def test_a_failed_quote_is_surfaced_not_swallowed():
    r = runner(fails=True)
    r.tick()
    assert r.last_error and "MultipleTouchline" in r.last_error
    assert r.last_spot is None


def test_the_daily_loss_limit_stops_the_run():
    from engine.config import engine_config

    r = runner()
    r.realised = -abs(engine_config.daily_loss_limit) - 1
    r.market.prices = {26000: 24_000.0}
    r.expiry = EXPIRY
    r.tick()
    assert r.stopped_reason and "daily loss limit" in r.stopped_reason


def test_the_runner_has_no_order_placing_surface_at_all():
    """Paper-only is a structural property, not a flag that could flip back."""
    r = runner()
    for gone in ("_place", "_next_order_no", "arm", "armed"):
        assert not hasattr(r, gone), gone
    assert r.mode == "paper"


def test_an_implausibly_small_credit_is_flagged_as_a_pricing_fault():
    """Seen live: a 20-DTE 200-point condor opened for Rs62 against Rs13,000 of
    risk, because every premium was a hundredth of its value. The structure
    itself looked perfectly well formed, so nothing else would have caught it.
    """
    r = runner(prices={})
    legs = build_legs(24_000, cfg())
    tokens = [r.market.master.option("NIFTY", EXPIRY, leg.strike, leg.right).token for leg in legs]
    # build_legs orders wings first, then shorts. Prices chosen so the condor
    # still opens for a positive credit after crossing the spread -- a debit
    # would trip the separate net-debit warning instead.
    r.market.prices = {t: (1.00 if i < 2 else 1.40) for i, t in enumerate(tokens)}

    condor = r._open_condor(24_000, EXPIRY)
    assert condor is not None and condor.credit > 0
    assert any("implausibly small" in e.message for e in r.events), [e.message for e in r.events]


def test_a_normal_credit_is_not_flagged():
    r = runner()
    legs = build_legs(24_000, cfg())
    tokens = [r.market.master.option("NIFTY", EXPIRY, leg.strike, leg.right).token for leg in legs]
    r.market.prices = {t: (40.0 if i < 2 else 120.0) for i, t in enumerate(tokens)}
    r._open_condor(24_000, EXPIRY)
    assert not any("implausibly small" in e.message for e in r.events)
