"""The ladder's optional entry filters: minimum days to expiry, minimum credit.

Both are off by default, so nothing that exists changes. What matters when one
is on: the backtest and the forward runner must refuse the same rungs (the
point of backtesting a filter is that live does the same), HIC's core condors
are never touched, and a refused rung leaves nothing behind -- no position, no
orphan fills.
"""

from __future__ import annotations

import datetime as dt

import pytest

from engine.strategy.condor import (
    CALL, PUT, Condor, FilledLeg, Leg, Side, StrategyConfig, entry_refusal,
)
from engine.strategy.hic import HicConfig

EXPIRY = dt.date(2026, 9, 29)


def condor(config, credit_per_share: float, level=23_300.0) -> Condor:
    """A 200-wide condor collecting `credit_per_share` in total."""
    half = credit_per_share / 2
    return Condor(
        level=level, entry_time=dt.datetime(2026, 9, 21, 10, 0), expiry=EXPIRY, config=config,
        legs=[
            FilledLeg(leg=Leg(PUT, Side.BUY, level - 400, config.qty), entry_price=10.0),
            FilledLeg(leg=Leg(CALL, Side.BUY, level + 400, config.qty), entry_price=10.0),
            FilledLeg(leg=Leg(PUT, Side.SELL, level - 200, config.qty), entry_price=10.0 + half),
            FilledLeg(leg=Leg(CALL, Side.SELL, level + 200, config.qty), entry_price=10.0 + half),
        ],
    )


def cfg(**kw) -> StrategyConfig:
    return StrategyConfig(lots=1, lot_size=65, **kw)


def test_off_by_default():
    c = condor(cfg(), credit_per_share=20.0)            # 10% of the wing
    assert entry_refusal(c, EXPIRY, cfg()) is None     # expiry day, tiny credit: still opens


def test_days_to_expiry_cutoff():
    config = cfg(min_entry_dte=5)
    c = condor(config, 120.0)
    assert entry_refusal(c, EXPIRY - dt.timedelta(days=5), config) is None
    reason = entry_refusal(c, EXPIRY - dt.timedelta(days=4), config)
    assert reason and "4 days to expiry" in reason


def test_credit_ratio_cutoff_is_max_loss_against_credit_at_half():
    """0.5 is the rule as first proposed: do not open when the most that can
    be lost exceeds the credit. On a 200-point wing that is 100 points."""
    config = cfg(min_credit_ratio=0.5)
    assert entry_refusal(condor(config, 100.0), EXPIRY - dt.timedelta(days=10), config) is None
    reason = entry_refusal(condor(config, 99.0), EXPIRY - dt.timedelta(days=10), config)
    assert reason and "50%" in reason


def test_hic_core_condors_are_never_filtered():
    """The band's condors are what the spreads beyond them lean on."""
    config = HicConfig(lots=1, lot_size=65, direction="both", min_entry_dte=30, min_credit_ratio=0.9)
    c = condor(config, 20.0)
    assert entry_refusal(c, EXPIRY, config) is None


@pytest.mark.parametrize("bad", [dict(min_entry_dte=-1), dict(min_credit_ratio=0.0),
                                 dict(min_credit_ratio=1.0)])
def test_nonsense_settings_are_refused(bad):
    with pytest.raises(ValueError):
        cfg(**bad)


def test_the_backtest_records_what_it_skipped():
    """A filter that thins the ladder silently is indistinguishable from a
    broken one, so every refusal is listed with its reason."""
    from engine.backtest.providers import ModelPriceProvider
    from engine.backtest.runner import Backtest, BacktestParams, weekly_expiry_resolver
    from engine.config import IST
    from engine.pricing.costs import ZERO_COST
    from engine.pricing.iv_surface import IVSurface

    start = dt.datetime(2026, 9, 21, 9, 15, tzinfo=IST)
    # One rung per day, the last few inside five days of expiry.
    spots = [(start + dt.timedelta(days=i), 23_350.0 - 100 * i) for i in range(7)]

    def run(**kw):
        return Backtest(
            BacktestParams(strategy=cfg(max_condors=20, **kw), costs=ZERO_COST),
            ModelPriceProvider(surface=IVSurface(atm_vol=0.12)),
            weekly_expiry_resolver([EXPIRY]),
        ).run(spots)

    base, filtered = run(), run(min_entry_dte=5)
    refused = [s for s in filtered.skipped if "entry filter" in s[2]]

    assert refused, "late rungs must be refused"
    assert len(filtered.condors) == len(base.condors) - len(refused)
    assert all((EXPIRY - c.entry_time.date()).days >= 5 for c in filtered.condors)
    assert len(filtered.triggers) == len(base.triggers), "the ladder still fires every level"


def test_the_forward_runner_refuses_before_recording_a_fill():
    """Deciding after the fills are written would leave a trade in the log for
    a position that never existed."""
    from engine.forward.runner import ForwardRunner
    from engine.tests.test_forward_fills import FakeMarket, FakeMaster

    r = ForwardRunner(market=FakeMarket(master=FakeMaster()),  # type: ignore[arg-type]
                      strategy=cfg(min_credit_ratio=0.99, max_condors=20))
    expiry = dt.date.today() + dt.timedelta(days=7)
    r.expiry = expiry

    opened = r._open_condor(23_300.0, expiry, side="anchor")

    assert opened is None
    assert r.condors == []
    assert r.fills == [], "no fill may exist for a refused rung"
    assert any("not opened" in e.message for e in r.events)
