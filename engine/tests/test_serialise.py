"""Tests for the dashboard JSON contract.

The bundle crosses a process boundary into a browser, so it has to be strict
JSON. Python will happily emit `Infinity` and `NaN`, which `JSON.parse` rejects
outright -- and the dashboard then fails to load at all.
"""

from __future__ import annotations

import datetime as dt
import json
import math

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

