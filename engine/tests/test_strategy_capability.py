"""An engine must not run one strategy under another's name.

This happened. An engine build accepted `strategy: "hic"`, recorded the run as
HIC in the database, and -- having no code to construct one -- silently built a
plain down-only condor ladder. Every surface then reported a strategy the run
was not trading: the dashboard, the run listing, the saved row. The numbers
were all correct for what it was doing, and all wrong for what it said it was.

The cause is ordinary version skew: the dashboard was newer than the engine.
That will happen again, because the two deploy separately.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from engine.api import RUNNABLE_STRATEGIES, StartForwardRequest, _strategy_config
from engine.store.db import HIC, LADDER, STRATEGIES
from engine.strategy.condor import StrategyConfig
from engine.strategy.hic import HicConfig


def _config(strategy: str, **kw):
    body = StartForwardRequest(strategy=LADDER, **kw)
    # Set past the pattern, which is exactly what a newer client would send.
    object.__setattr__(body, "strategy", strategy)
    return _strategy_config(body, lot_size=65, strike_step=50.0)


def test_each_runnable_strategy_builds_its_own_config():
    assert isinstance(_config(LADDER), StrategyConfig)
    assert isinstance(_config(HIC), HicConfig)


def test_a_name_this_build_cannot_trade_is_refused_not_substituted():
    """The whole point. Silently running something else is the failure."""
    with pytest.raises(HTTPException) as caught:
        _config("strangle")

    assert caught.value.status_code == 501
    assert "cannot run" in caught.value.detail
    assert "older than the dashboard" in caught.value.detail


def test_the_refusal_names_the_likely_cause():
    """An operator reading this at 3pm should not have to guess."""
    with pytest.raises(HTTPException) as caught:
        _config("butterfly")

    assert "butterfly" in caught.value.detail
    assert "Restart the engine" in caught.value.detail


def test_what_the_engine_advertises_is_what_it_can_actually_build():
    """The list a dashboard reads must not promise more than the build has."""
    for strategy in RUNNABLE_STRATEGIES:
        config = _config(strategy)
        assert isinstance(config, StrategyConfig)


def test_the_runnable_set_is_not_the_same_thing_as_the_storable_set():
    """db.STRATEGIES is what a row may hold, including names written by a newer
    build. Treating the two as one is what let this happen."""
    assert set(RUNNABLE_STRATEGIES) <= set(STRATEGIES)


def test_hic_is_forced_two_way_and_centred_whatever_the_form_sends():
    """A one-directional HIC is a different strategy wearing the name, and an
    off-centre anchor defeats a structure that is symmetric by construction."""
    config = _config(HIC, direction="down", anchor_mode="floor")

    assert config.direction == "both"
    assert config.effective_anchor_mode == "nearest"


def test_the_ladder_keeps_whatever_it_was_asked_for():
    config = _config(LADDER, direction="down", anchor_mode="floor")

    assert config.direction == "down"
    assert config.effective_anchor_mode == "floor"
