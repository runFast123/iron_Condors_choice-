"""B0: the ladder must behave exactly as it did before the multi-strategy work.

The refactor that makes this engine run two strategies touches the trigger, the
config, the persisted state and the risk maths -- all of it under the existing
ladder. A regression there would be invisible in a P&L number and obvious only
months later, so this pins the whole observable surface of one deterministic
run: which rungs fire and why, every strike and fill price, per-condor risk,
the headline metrics, and the netting that is the strategy's entire thesis.

The fixture was recorded from the code as it stood before Phase 0. If a change
alters any of it, that change is either a bug or a decision that has to be made
deliberately -- by re-recording the fixture and saying why in the commit.

    python -m engine.tests.test_ladder_parity --record
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

from engine.backtest.providers import ModelPriceProvider
from engine.backtest.runner import Backtest, BacktestParams, weekly_expiry_resolver
from engine.config import IST
from engine.pricing.costs import CostModel
from engine.pricing.iv_surface import IVSurface
from engine.strategy.condor import StrategyConfig

GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "ladder_b0_golden.json"

EXPIRY = dt.date(2026, 3, 26)
START = dt.datetime(2026, 3, 23, 9, 15, tzinfo=IST)
SURFACE = IVSurface(atm_vol=0.14)

# Chosen to exercise every branch of the rule set in one path: the anchor, a
# dip too shallow to fire, a rally that must do nothing, plain declines, a
# gap-down that fills the rungs it skipped, and a recovery that must not
# re-fire anything.
PRICES = [
    24_010, 23_960, 23_905, 23_880, 23_990, 24_050,
    23_795, 23_700, 23_660,
    23_310,
    23_500, 23_620, 23_240, 23_180,
]


def observe() -> dict:
    """Everything about one run that a user could notice changing."""
    params = BacktestParams(strategy=StrategyConfig(lots=1, lot_size=75), costs=CostModel())
    engine = Backtest(params, ModelPriceProvider(surface=SURFACE), weekly_expiry_resolver([EXPIRY]))
    path = [(START + dt.timedelta(minutes=5 * i), p) for i, p in enumerate(PRICES)]
    result = engine.run(path)
    return {
        "prices": PRICES,
        "triggers": [[t.level, t.reason, round(t.spot, 2)] for t in result.triggers],
        "condors": [
            {
                "level": c.level,
                "status": c.status.value,
                "credit": round(c.credit, 4),
                "max_loss": round(c.max_loss, 4),
                "breakevens": [round(b, 4) for b in c.breakevens],
                "legs": [
                    [
                        leg.leg.right, leg.leg.side.value, leg.leg.strike, leg.leg.qty,
                        round(leg.entry_price, 4),
                        None if leg.exit_price is None else round(leg.exit_price, 4),
                    ]
                    for leg in c.legs
                ],
                "realised": round(c.realised_pnl(), 4),
            }
            for c in result.condors
        ],
        "metrics": {
            "net_pnl": round(result.metrics.net_pnl, 4),
            "total_credit": round(result.metrics.total_credit, 4),
            "total_costs": round(result.metrics.total_costs, 4),
            "condors": result.metrics.condors,
            "capital_at_risk": round(result.metrics.capital_at_risk, 4),
            "max_concurrent": result.metrics.max_concurrent,
        },
        "netting": {k: round(v, 6) for k, v in result.netting.items()},
    }


def test_the_ladder_reproduces_its_recorded_run_exactly():
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    actual = observe()

    # Compared section by section: a whole-dict assertion on this much data
    # reports "dicts differ" and leaves you to find where.
    assert actual["prices"] == expected["prices"], "the fixture was recorded on another path"
    assert actual["triggers"] == expected["triggers"], "which rungs fired, and why, changed"
    assert actual["netting"] == expected["netting"], "the offsetting behaviour changed"
    assert actual["metrics"] == expected["metrics"], "headline metrics changed"

    assert len(actual["condors"]) == len(expected["condors"])
    for got, want in zip(actual["condors"], expected["condors"]):
        assert got == want, f"condor at {want['level']:,.0f} changed"


def test_the_recorded_run_is_worth_pinning():
    """A fixture that exercises nothing would pass through any regression."""
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    reasons = {t[1] for t in expected["triggers"]}

    assert reasons == {"anchor", "decline", "gap-fill"}, reasons
    assert len(expected["condors"]) >= 8, "too few rungs to show offsetting"
    assert expected["netting"]["strikes_fully_offset"] > 0, "the thesis is not exercised"


if __name__ == "__main__":                      # pragma: no cover - operator tool
    import sys

    if "--record" in sys.argv:
        GOLDEN.write_text(json.dumps(observe(), indent=1), encoding="utf-8")
        print(f"recorded {GOLDEN}")
    else:
        print(__doc__)
