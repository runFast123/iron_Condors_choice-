"""Which saved runs an engine agrees to read, and which it refuses.

This gate decides the fate of live positions. `_resume_forward` treats
UnsupportedStateVersion as a run worth abandoning, so anything this refuses is
a ladder the user never gets back. It used to refuse on `version != ours`,
which meant the act of shipping a new state format retired every open run on
the next login.
"""

from __future__ import annotations

import pytest

from engine.forward.runner import ForwardRunner, UnsupportedStateVersion
from engine.tests.test_forward_fills import EXPIRY, FakeMarket, FakeMaster, cfg


def _saved() -> dict:
    """A real saved run, with a condor open."""
    runner = ForwardRunner(market=FakeMarket(master=FakeMaster()), strategy=cfg())  # type: ignore[arg-type]
    runner._open_condor(23_400, EXPIRY)
    return runner.to_state()


def _restore(state: dict) -> ForwardRunner:
    return ForwardRunner.restore(
        state, market=FakeMarket(master=FakeMaster()), state_path=None,  # type: ignore[arg-type]
    )


def test_the_current_version_round_trips():
    revived = _restore(_saved())
    assert len(revived.condors) == 1
    assert revived.condors[0].is_open


def test_state_from_a_newer_engine_is_refused():
    """The one case that is genuinely unreadable: we cannot know what a future
    format means, and guessing would corrupt a real position."""
    state = _saved()
    state["version"] = ForwardRunner.STATE_VERSION + 1

    with pytest.raises(UnsupportedStateVersion, match="newer engine"):
        _restore(state)


def test_state_with_no_version_is_refused():
    state = _saved()
    del state["version"]

    with pytest.raises(UnsupportedStateVersion, match="no usable version"):
        _restore(state)


@pytest.mark.parametrize("bogus", ["1", 1.5, True, 0, -3, None], ids=repr)
def test_a_version_that_is_not_a_real_version_is_refused(bogus):
    """`True` is in here deliberately: it is an int as far as isinstance is
    concerned, and would otherwise be read as version 1."""
    state = _saved()
    state["version"] = bogus

    with pytest.raises(UnsupportedStateVersion):
        _restore(state)


def test_an_older_version_is_upgraded_rather_than_abandoned():
    """The whole point. An engine that can read an old format must, because
    refusing it destroys a live ladder to save a migration."""
    if not ForwardRunner._UPGRADES:
        pytest.skip("no older format exists yet; the path is exercised once one does")

    oldest = min(ForwardRunner._UPGRADES)
    state = _saved()
    state["version"] = oldest

    revived = _restore(state)
    assert len(revived.condors) == 1, "the open position survived the upgrade"


def test_upgrading_does_not_mutate_the_stored_state():
    """A failure part-way through must leave the saved run exactly as it was,
    or a crash mid-upgrade loses the position it was trying to preserve."""
    if not ForwardRunner._UPGRADES:
        pytest.skip("no older format exists yet")

    oldest = min(ForwardRunner._UPGRADES)
    state = _saved()
    state["version"] = oldest
    before = dict(state)

    ForwardRunner._readable_state(state)

    assert state == before


def test_every_version_below_the_current_one_has_an_upgrade():
    """A gap in the chain strands exactly the runs saved at that version."""
    missing = [
        v for v in range(1, ForwardRunner.STATE_VERSION)
        if v not in ForwardRunner._UPGRADES
    ]
    assert missing == [], f"no upgrade path from version(s) {missing}"
