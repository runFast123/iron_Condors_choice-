"""Tests for the dashboard JSON contract.

The bundle crosses a process boundary into a browser, so it has to be strict
JSON. Python will happily emit `Infinity` and `NaN`, which `JSON.parse` rejects
outright -- and the dashboard then fails to load at all.
"""

from __future__ import annotations

import datetime as dt
import json
import math

import pytest

from engine.backtest.providers import ModelPriceProvider
from engine.backtest.runner import Backtest, BacktestParams, weekly_expiry_resolver
from engine.backtest.serialise import empty_bundle, json_safe, serialise
from engine.config import IST
from engine.pricing.costs import ZERO_COST
from engine.pricing.iv_surface import IVSurface
from engine.strategy.condor import StrategyConfig

EXPIRY = dt.date(2026, 3, 26)
START = dt.datetime(2026, 3, 23, 9, 15, tzinfo=IST)


def strict(obj) -> str:
    """Serialise the way a browser will read it: no Infinity, no NaN."""
    return json.dumps(obj, allow_nan=False)


# --------------------------------------------------------------- sanitiser


def test_infinity_becomes_null():
    assert json_safe(float("inf")) is None
    assert json_safe(float("-inf")) is None
    assert json_safe(float("nan")) is None


def test_finite_numbers_are_untouched():
    assert json_safe(12.5) == 12.5
    assert json_safe(0.0) == 0.0
    assert json_safe(-3) == -3


def test_sanitising_reaches_nested_structures():
    dirty = {"a": [1.0, float("inf")], "b": {"c": float("nan"), "d": "text"}}
    clean = json_safe(dirty)
    assert clean == {"a": [1.0, None], "b": {"c": None, "d": "text"}}
    strict(clean)                       # must not raise


def test_strings_and_bools_survive():
    assert json_safe({"s": "x", "b": True, "n": None}) == {"s": "x", "b": True, "n": None}


# ------------------------------------------------------------ real bundles


def test_an_all_winning_backtest_still_produces_valid_json():
    """The regression, and it is the strategy's *best* case.

    With no losing condor the profit factor is infinite. Python writes that as
    a bare `Infinity`, JSON.parse throws, and the dashboard shows nothing at
    all -- for the run that went perfectly.
    """
    params = BacktestParams(strategy=StrategyConfig(lots=1, lot_size=65), costs=ZERO_COST)
    engine = Backtest(
        params,
        ModelPriceProvider(surface=IVSurface(atm_vol=0.14)),
        weekly_expiry_resolver([EXPIRY]),
    )
    # Pinned between the shorts for the whole run: every condor expires worthless.
    spots = [(START + dt.timedelta(minutes=5 * i), 24_000.0) for i in range(40)]
    result = engine.run(spots)

    assert result.metrics.losses == 0
    assert math.isinf(result.metrics.profit_factor), "expected the infinite case"

    bundle = serialise(result, {"note": "test", "range": ["a", "b"]})
    text = strict(bundle)               # raises if anything non-finite survived
    assert '"profit_factor":null' in text.replace(" ", "")


def test_the_empty_bundle_is_valid_json():
    strict(empty_bundle("nothing yet"))


def test_every_metric_in_a_serialised_bundle_is_finite_or_null():
    params = BacktestParams(strategy=StrategyConfig(lots=1, lot_size=65), costs=ZERO_COST)
    engine = Backtest(
        params,
        ModelPriceProvider(surface=IVSurface(atm_vol=0.14)),
        weekly_expiry_resolver([EXPIRY]),
    )
    spots = [(START + dt.timedelta(minutes=5 * i), 24_000.0 - i * 20) for i in range(60)]
    bundle = serialise(engine.run(spots), {"note": "t", "range": ["a", "b"]})

    for key, value in bundle["metrics"].items():
        assert value is None or not isinstance(value, float) or math.isfinite(value), key
    strict(bundle)


def test_serialise_includes_side_attribution_for_two_way_ladder():
    params = BacktestParams(
        strategy=StrategyConfig(lots=1, lot_size=65, direction="both"),
        costs=ZERO_COST,
    )
    engine = Backtest(
        params,
        ModelPriceProvider(surface=IVSurface(atm_vol=0.14)),
        weekly_expiry_resolver([EXPIRY]),
    )
    # Price goes down 100, then up 200
    spots = [
        (START, 24_000.0),
        (START + dt.timedelta(minutes=5), 23_900.0),
        (START + dt.timedelta(minutes=10), 24_100.0),
    ]
    bundle = serialise(engine.run(spots), {"note": "t", "range": ["a", "b"]})

    assert "attribution" in bundle
    attr = bundle["attribution"]
    assert "down_pnl" in attr and "up_pnl" in attr
    assert "down_condors" in attr and "up_condors" in attr
    assert attr["down_condors"] >= 1
    assert attr["up_condors"] >= 1
    assert bundle["params"]["direction"] == "both"
    for condor in bundle["condors"]:
        assert condor["side"] in ("anchor", "down", "up")
    for trigger in bundle["triggers"]:
        assert trigger["side"] in ("anchor", "down", "up")
    strict(bundle)



# ============================== the payoff curve is one book


def test_the_payoff_curve_describes_one_expiry_not_every_expiry_stacked():
    """A rolling run re-anchors at each expiry, so its positions belong to
    books that were never held together. Summing them put every book's worst
    case at every spot, and the error grew with the length of the run."""
    from engine.backtest.serialise import _campaign_payoff
    from engine.strategy.condor import CALL, PUT, Condor, FilledLeg, Leg, Side

    config = StrategyConfig(lots=1, lot_size=65)

    def condor(level: float, expiry: dt.date, index: int) -> Condor:
        return Condor(
            level=level, entry_time=START, expiry=expiry, config=config, index=index,
            legs=[
                FilledLeg(leg=Leg(PUT, Side.BUY, level - 400, 65), entry_price=40.0),
                FilledLeg(leg=Leg(CALL, Side.BUY, level + 400, 65), entry_price=40.0),
                FilledLeg(leg=Leg(PUT, Side.SELL, level - 200, 65), entry_price=95.0),
                FilledLeg(leg=Leg(CALL, Side.SELL, level + 200, 65), entry_price=95.0),
            ],
        )

    jan, feb = dt.date(2026, 1, 29), dt.date(2026, 2, 26)
    book = ([condor(23_000 + 100 * i, jan, i) for i in range(3)]
            + [condor(23_000 + 100 * i, feb, 3 + i) for i in range(3)])
    grid = [22_000.0 + 20 * i for i in range(150)]

    curve, campaign = _campaign_payoff(book, grid)

    drawn = min(p["pnl"] for p in curve)
    stacked = min(sum(c.payoff_at_expiry(s) for c in book) for s in grid)
    single = min(
        min(sum(c.payoff_at_expiry(s) for c in book if c.expiry == e) for s in grid)
        for e in (jan, feb)
    )

    assert drawn == pytest.approx(single)
    assert stacked == pytest.approx(2 * single), "the two campaigns are identical in size"
    assert campaign["campaigns"] == 2
    assert campaign["units"] == 3
    assert campaign["expiry"] in (jan.isoformat(), feb.isoformat())


def test_a_single_expiry_run_is_unchanged_by_the_grouping():
    """The common case -- one forward campaign, or a backtest inside one
    expiry -- must draw exactly what it drew before."""
    from engine.backtest.serialise import _campaign_payoff

    engine = Backtest(
        BacktestParams(
            strategy=StrategyConfig(step=100.0, lots=1, lot_size=65, max_condors=5),
            costs=ZERO_COST,
        ),
        ModelPriceProvider(surface=IVSurface(atm_vol=0.14)),
        weekly_expiry_resolver([EXPIRY]),
    )
    result = engine.run([
        (START, 24_000.0),
        (START + dt.timedelta(minutes=5), 23_900.0),
        (START + dt.timedelta(minutes=10), 23_800.0),
    ])
    assert result.condors, "the run must open something for this to mean anything"
    assert len({c.expiry for c in result.condors}) == 1

    grid = [23_000.0 + 20 * i for i in range(100)]
    curve, campaign = _campaign_payoff(result.condors, grid)

    assert campaign["campaigns"] == 1
    assert campaign["units"] == len(result.condors)
    for point, spot in zip(curve, grid):
        assert point["pnl"] == pytest.approx(
            round(sum(c.payoff_at_expiry(spot) for c in result.condors), 2)
        )


def test_an_empty_run_still_produces_a_flat_curve_and_the_same_keys():
    from engine.backtest.serialise import _campaign_payoff

    curve, campaign = _campaign_payoff([], [23_000.0, 24_000.0])

    assert [p["pnl"] for p in curve] == [0.0, 0.0]
    assert set(campaign) == set(empty_bundle("none")["payoff_campaign"])
