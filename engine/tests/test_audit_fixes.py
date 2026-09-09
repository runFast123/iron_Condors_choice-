"""Regressions for defects found by re-deriving the numbers from arithmetic.

Each test names the wrong answer the code used to give. None of these were
caught by the existing suite, because every one of them returns a plausible
number rather than raising.
"""

from __future__ import annotations

import datetime as dt
import math
import threading

import pytest

from engine.backtest import metrics as M
from engine.choice.errors import scrub
from engine.data.market import Quote
from engine.forward.fills import TICK, FillModel
from engine.pricing.black76 import greeks, price
from engine.strategy.condor import (
    Condor,
    FilledLeg,
    PriceSource,
    Side,
    StrategyConfig,
    build_legs,
)

LOT = 65


def condor(cfg: StrategyConfig, prices, entry_costs=0.0, exit_costs=0.0) -> Condor:
    legs = build_legs(24_000.0, cfg)
    filled = [
        FilledLeg(leg=leg, entry_price=prices[i], source=PriceSource.CHOICE, token=i)
        for i, leg in enumerate(legs)
    ]
    return Condor(
        level=24_000.0, entry_time=dt.datetime(2026, 4, 20), expiry=dt.date(2026, 4, 28),
        legs=filled, config=cfg, entry_costs=entry_costs, exit_costs=exit_costs, index=0,
    )


# ================================================================ Black-76


@pytest.mark.parametrize("right", ["CE", "PE"])
@pytest.mark.parametrize("days", [7, 30, 365])
def test_theta_matches_a_finite_difference_of_price(right, days):
    """The rate term carried the wrong sign.

    Black-76 discounts the whole payoff, so as the calendar advances and T
    shrinks, e^{-rT} *rises* and adds value: theta = +r*P - decay. Carrying it
    negative left an error of exactly 2*r*premium/365 per share per day, which
    never cancels and grows with the rate and the premium.
    """
    F, K, r, vol = 24_000.0, 24_200.0, 0.065, 0.14
    T = days / 365.0
    h = T * 1e-4
    finite_diff = -(price(F, K, T + h, vol, r, right) - price(F, K, T - h, vol, r, right)) / (2 * h) / 365.0
    assert greeks(F, K, T, vol, r, right).theta == pytest.approx(finite_diff, abs=1e-6)


def test_theta_is_not_the_old_sign_flipped_value():
    """Guards specifically against reintroducing `- rate * premium`."""
    F, K, T, r, vol = 24_000.0, 24_200.0, 30 / 365.0, 0.065, 0.14
    g = greeks(F, K, T, vol, r, "CE")
    wrong = g.theta - 2.0 * r * g.price / 365.0
    assert abs(g.theta - wrong) > 1e-6


# ================================================================= metrics


def test_downside_deviation_is_not_the_stdev_of_the_losses():
    """A run of identical small losses has real downside risk.

    Taking the standard deviation of the negative subset measures how much the
    losses vary about their own mean, which is zero here -- so Sortino came out
    0.0 on a strongly positive series, the worst possible reading.
    """
    rets = [0.10, -0.01, -0.01, -0.01]
    assert M._stdev([r for r in rets if r < 0]) == 0.0        # the old answer
    assert M._downside_deviation(rets) == pytest.approx(0.0086603, abs=1e-6)
    sortino = M._mean(rets) / M._downside_deviation(rets) * math.sqrt(252)
    assert sortino == pytest.approx(32.078, abs=0.01)


def test_downside_deviation_divides_by_all_observations():
    """A period with no shortfall contributes zero risk, not no data point."""
    assert M._downside_deviation([0.0, 0.0, -0.02, 0.0]) == pytest.approx(0.02 / 2.0)


def test_sortino_exceeds_sharpe_on_a_positively_skewed_series():
    pts = [M.EquityPoint(ts=dt.datetime(2026, 3, 1) + dt.timedelta(days=i), equity=e)
           for i, e in enumerate([100, 90, 80, 400, 390, 380, 700])]
    m = M.compute(realised=[700.0], equity=pts, total_credit=0, total_costs=0,
                  capital_at_risk=10_000, max_concurrent=1)
    assert m.sortino > m.sharpe > 0


def test_a_short_span_does_not_annualise_into_a_fantasy():
    """A 2% gain over one day used to be reported as 137,641% CAGR."""
    pts = [M.EquityPoint(ts=dt.datetime(2026, 3, 2), equity=0.0),
           M.EquityPoint(ts=dt.datetime(2026, 3, 3), equity=20.0)]
    m = M.compute(realised=[20.0], equity=pts, total_credit=0, total_costs=0,
                  capital_at_risk=1000, max_concurrent=1)
    assert m.cagr == 0.0


def test_a_huge_short_run_return_does_not_overflow():
    """`7.0 ** 365` raises OverflowError and took the whole metric set with it."""
    pts = [M.EquityPoint(ts=dt.datetime(2026, 3, 2), equity=0.0),
           M.EquityPoint(ts=dt.datetime(2026, 3, 3), equity=6000.0)]
    m = M.compute(realised=[6000.0], equity=pts, total_credit=0, total_costs=0,
                  capital_at_risk=1000, max_concurrent=1)
    assert math.isfinite(m.cagr)


def test_drawdown_is_measured_from_inception():
    """The curve's first point is recorded after the first bar's marks, so a
    run that is under water from the opening trade has no zero to fall from."""
    assert min(M.drawdown_series([-500.0, -900.0, -1300.0])) == pytest.approx(-1300.0)


def test_a_non_finite_pnl_is_excluded_rather_than_poisoning_the_set():
    m = M.compute(realised=[float("nan"), 100.0, -50.0], equity=[], total_credit=0,
                  total_costs=0, capital_at_risk=1000, max_concurrent=1)
    assert m.condors == 2
    assert m.net_pnl == pytest.approx(50.0)
    assert m.win_rate == pytest.approx(0.5)
    assert math.isfinite(m.profit_factor)


# ================================================================== condor


def test_the_wing_comes_from_the_traded_strikes_not_the_config():
    """`build_legs` snaps to the listed grid, so nominal offsets that are not
    multiples of `strike_step` describe a structure that was never traded."""
    cfg = StrategyConfig(short_offset=225.0, long_offset=400.0, strike_step=50.0,
                         lots=1, lot_size=LOT)
    c = condor(cfg, [20.0, 20.0, 60.0, 60.0])
    assert cfg.wing_width == 175.0          # what the config claims
    assert c.wing_width == 200.0            # what was actually bought and sold


@pytest.mark.parametrize("short_offset,long_offset", [
    (200.0, 400.0), (225.0, 400.0), (150.0, 375.0), (100.0, 250.0),
])
def test_payoff_stays_inside_max_loss_and_max_profit_off_grid(short_offset, long_offset):
    cfg = StrategyConfig(short_offset=short_offset, long_offset=long_offset,
                         strike_step=50.0, lots=1, lot_size=LOT)
    c = condor(cfg, [12.0, 14.0, 48.0, 52.0], entry_costs=300.0, exit_costs=250.0)
    for spot in range(20_000, 28_001, 25):
        pnl = c.payoff_at_expiry(float(spot))
        assert pnl >= -c.max_loss - 0.01, f"lost more than max_loss at {spot}"
        assert pnl <= c.max_profit + 0.01, f"made more than max_profit at {spot}"


def test_breakevens_are_where_the_payoff_is_actually_zero():
    """They used the nominal level and the *gross* credit, putting both points
    ~14 points too far out -- reporting the position as safer than it is."""
    cfg = StrategyConfig(lots=1, lot_size=LOT, strike_step=50.0)
    c = condor(cfg, [15.0, 15.0, 55.0, 60.0], entry_costs=900.0)
    lo, hi = c.breakevens
    assert c.payoff_at_expiry(lo) == pytest.approx(0.0, abs=0.01)
    assert c.payoff_at_expiry(hi) == pytest.approx(0.0, abs=0.01)


def test_max_profit_accounts_for_exit_costs_like_max_loss_does():
    cfg = StrategyConfig(lots=1, lot_size=LOT, strike_step=50.0)
    c = condor(cfg, [15.0, 15.0, 55.0, 60.0], entry_costs=500.0, exit_costs=400.0)
    best = max(c.payoff_at_expiry(float(s)) for s in range(21_000, 27_001, 25))
    assert best == pytest.approx(c.max_profit, abs=0.01)


# =================================================================== fills


def test_a_fill_never_beats_the_price_the_market_showed():
    """Symmetric tick rounding gave a better-than-touch fill about 40% of the
    time -- always in the user's favour, which flatters every result."""
    model = FillModel()
    checked = 0
    for i in range(400):
        mid = 0.50 + i * 0.37
        half = max(TICK, mid * 0.04)          # a tradeable 8%-wide book
        bid, ask = round(mid - half, 4), round(mid + half, 4)
        q = Quote(token=1, ltp=mid, bid=bid, ask=ask)
        buy, sell = model.fill(q, Side.BUY), model.fill(q, Side.SELL)
        assert buy is not None and sell is not None
        assert buy.price >= ask - 1e-9, f"bought below the offer at {ask}"
        assert sell.price <= bid + 1e-9, f"sold above the bid at {bid}"
        checked += 1
    assert checked == 400


def test_an_untradeable_book_is_refused_rather_than_filled():
    """A 133%-wide book has no price a trade would have happened at."""
    q = Quote(token=1, ltp=0.15, bid=0.05, ask=0.25)
    assert FillModel().fill(q, Side.BUY) is None


def test_a_modelled_sale_never_prints_above_the_last_trade():
    """The one-tick floor turned the modelled spread into a modelled profit."""
    model = FillModel()
    for ltp in (0.05, 0.10, 0.40, 1.00, 23.45):
        fill = model.fill(Quote(token=1, ltp=ltp), Side.SELL)
        assert fill.price <= max(ltp, TICK) + 1e-9


def test_reported_slippage_describes_the_fill_actually_received():
    q = Quote(token=1, ltp=100.0, bid=98.03, ask=102.07)
    fill = FillModel().fill(q, Side.BUY)
    assert fill.slippage == pytest.approx(abs(fill.price - fill.reference), abs=1e-9)


# ================================================================ scrubbing


@pytest.mark.parametrize("secret,text", [
    ("AAAABBBBCCCCDDDD", str({"SessionId": "AAAABBBBCCCCDDDD"})),
    ("tok-verysecret-123", str({"AccessToken": "tok-verysecret-123"})),
    ("sk-live-abcdef123456", str({"api_key": "sk-live-abcdef123456"})),
    ("MYSECRETAPIKEY123", "Bearer MYSECRETAPIKEY123"),
    ("9988776655", "mobile=9988776655"),
    ("483920", "OTP 483920"),
])
def test_credentials_are_redacted_in_every_shape_they_arrive_in(secret, text):
    """A Choice response is interpolated as a Python dict repr, which quotes
    with ' -- so a pattern accepting only " matched nothing on exactly the
    strings carrying a live session id."""
    assert secret not in scrub(text)
    assert "<redacted>" in scrub(text)


def test_a_genuine_error_message_survives_scrubbing():
    """Over-redaction would destroy the diagnostics this exists to preserve."""
    assert scrub("Invalid Vendor Id or API key") == "Invalid Vendor Id or API key"


# ============================================== credentials never touch disk


def test_the_session_cache_is_off_unless_a_path_is_named(tmp_path):
    """One shared default file meant every user wrote their live session id and
    raw API key over the previous user's, and any read-back adopted whoever
    wrote last."""
    from engine.config import ChoiceConfig
    from engine.choice.session import ChoiceSession

    assert ChoiceConfig().session_file is None

    session = ChoiceSession(config=ChoiceConfig(vendor_id="v", api_key="k", mobile_no="9"))
    session.session_id = "LIVE-SESSION"
    assert session.save_session() is False
    assert session.load_session() is False
    assert not list(tmp_path.glob("*.json"))


def test_two_users_do_not_share_a_session_file_path():
    from engine.config import ChoiceConfig

    a = ChoiceConfig(vendor_id="A", api_key="ka", mobile_no="1")
    b = ChoiceConfig(vendor_id="B", api_key="kb", mobile_no="2")
    assert a.session_file is None and b.session_file is None


# ================================================================= runner


def test_the_runner_serialises_its_own_state():
    """Without a lock, `emit` trimming the event list while `snapshot`
    iterated it in reverse duplicated entries in most serialisations."""
    from engine.forward.runner import ForwardRunner

    runner = ForwardRunner.__new__(ForwardRunner)
    runner._lock = threading.RLock()
    runner.events = []
    runner.max_events = 50

    errors: list[Exception] = []

    def spam():
        try:
            for i in range(400):
                runner.emit("info", f"event {i}")
        except Exception as exc:                    # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=spam) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(runner.events) == runner.max_events
