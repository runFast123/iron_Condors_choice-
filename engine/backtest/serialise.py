"""One JSON shape for a backtest result.

Both the CLI (``engine.tools.seed``) and the HTTP API serve dashboard data, and
they must agree exactly -- a field present in one and missing in the other
shows up as a blank tile rather than an error. So the shape lives here and both
import it.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Sequence

from engine.backtest.runner import BacktestResult
from engine.config import IST


def json_safe(value):
    """Replace non-finite floats with None, recursively.

    Python emits `Infinity` and `NaN` as bare tokens, which are not valid JSON:
    `JSON.parse` throws on them and the whole dashboard fails to load. This is
    reachable on a perfectly ordinary run -- profit factor is infinite when no
    condor loses, which is the strategy's best case, not an edge case.

    None serialises to null, and the UI already renders a non-finite ratio as
    an infinity sign, so the meaning survives.
    """
    import math

    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def empty_bundle(reason: str, *, awaiting_connection: bool = True) -> dict:
    """A dataset that says plainly there is nothing to show yet.

    ``awaiting_connection`` separates two states the UI must not conflate:
    Choice has never been connected (fix: configure credentials), versus the
    user is signed in but has not run a backtest (fix: press the button). The
    same "awaiting connection" banner for both would send a signed-in user off
    to re-check credentials that are already working.
    """
    return {
        "provenance": {
            "spot_source": "choice:NIFTY",
            "vol_source": "choice:INDIAVIX",
            "premium_source": "choice:ChartData",
            "expiry_source": "choice:scripmaster",
            "verified": False,
            "awaiting_connection": awaiting_connection,
            "note": reason,
            "resolution": "-",
            "generated_at": dt.datetime.now(tz=IST).isoformat(),
            "provider": {"real_quotes": 0, "modeled_quotes": 0, "total_quotes": 0, "real_fraction": 0.0},
            "bars": 0,
            "range": ["-", "-"],
            "legs_requested": 0,
            "legs_with_choice_data": 0,
            "coverage": {},
            "failures": [],
        },
        "params": {
            "step": 100.0, "short_offset": 200.0, "long_offset": 400.0,
            # No lot size is asserted with no data: NSE revises it, and showing
            # a stale number is worse than showing none.
            "lots": 1, "lot_size": 0, "qty": 0, "max_condors": 20,
            "fill_gaps": True, "take_profit_pct": None, "stop_loss_mult": None,
            "direction": "down", "max_down": None, "max_up": None,
            "anchor_mode": "floor", "roll_to_next_expiry": True,
            "label": "Awaiting Choice connection",
        },
        "campaigns": 0,
        "rolls": [],
        "metrics": {
            k: 0 for k in (
                "net_pnl gross_pnl total_costs total_credit condors wins losses win_rate "
                "profit_factor expectancy avg_win avg_loss best worst max_drawdown "
                "max_drawdown_pct sharpe sortino calmar cagr max_concurrent avg_days_held "
                "capital_at_risk real_price_fraction modeled_quotes"
            ).split()
        },
        "attribution": {
            "down_pnl": 0.0, "up_pnl": 0.0, "down_condors": 0, "up_condors": 0,
            "down_credit": 0.0, "up_credit": 0.0,
        },
        "netting": {
            "strikes_touched": 0, "strikes_fully_offset": 0, "gross_qty": 0,
            "net_qty": 0, "offset_qty": 0, "offset_ratio": 0.0,
        },
        "payoff_campaign": {"expiry": None, "campaigns": 0, "units": 0,
                     "max_loss": 0.0, "credit": 0.0, "debit": 0.0},
        "condors": [], "equity": [], "payoff": [], "strike_matrix": [],
        "triggers": [], "warnings": [reason], "skipped": [],
    }


def _campaign_payoff(
    condors: Sequence, grid: Sequence[float]
) -> tuple[list[dict], dict]:
    """The expiry payoff of one campaign, not of every campaign stacked.

    A rolling backtest re-anchors at each expiry, so its positions belong to
    several books that were never held at the same time. Summing them puts
    every book's worst case at every spot at once: two campaigns of three
    condors reported a trough of -42,900 where no single book could go below
    -21,450, and six months of weeklies is off by roughly twenty-six times.
    That is not an error a reader can discount, either -- it grows with how
    long the backtest ran, so a longer run looks riskier for no other reason.

    So: one campaign, the one that actually carried the most risk, with its
    expiry reported alongside so the chart can say which book it is drawing.
    """
    by_expiry: dict[object, list] = defaultdict(list)
    for c in condors:
        by_expiry[c.expiry].append(c)

    if not by_expiry:
        return [{"spot": round(s, 2), "pnl": 0.0} for s in grid], {
            "expiry": None, "campaigns": 0, "units": 0,
            "max_loss": 0.0, "credit": 0.0, "debit": 0.0,
        }

    curves = {
        expiry: [sum(c.payoff_at_expiry(s) for c in units) for s in grid]
        for expiry, units in by_expiry.items()
    }
    worst = min(curves, key=lambda e: min(curves[e]))
    units = by_expiry[worst]
    return (
        [{"spot": round(s, 2), "pnl": round(v, 2)} for s, v in zip(grid, curves[worst])],
        {
            "expiry": worst.isoformat(),
            "campaigns": len(by_expiry),
            "units": len(units),
            "max_loss": round(sum(u.max_loss for u in units), 2),
            "credit": round(sum(u.credit for u in units if u.credit > 0), 2),
            "debit": round(-sum(u.credit for u in units if u.credit < 0), 2),
        },
    )


def serialise(result: BacktestResult, provenance: dict) -> dict:
    """Flatten a result into the JSON shape the dashboard consumes."""
    params = result.params
    strategy = params.strategy

    condors = [
        {
            "index": c.index,
            "level": c.level,
            "side": getattr(c, "side", "down"),
            "kind": c.kind.value,
            "k": c.k,
            "entry_time": c.entry_time.isoformat(),
            "expiry": c.expiry.isoformat(),
            "status": c.status.value,
            "exit_time": c.exit_time.isoformat() if c.exit_time else None,
            "exit_reason": c.exit_reason,
            "credit": round(c.credit, 2),
            "entry_costs": round(c.entry_costs, 2),
            "exit_costs": round(c.exit_costs, 2),
            "max_profit": round(c.max_profit, 2),
            "max_loss": round(c.max_loss, 2),
            "breakevens": [round(b, 2) for b in c.breakevens],
            "pnl": round(c.realised_pnl(), 2),
            "modeled": c.uses_modeled_prices,
            "legs": [
                {
                    "right": fl.leg.right,
                    "side": fl.leg.side.value,
                    "strike": fl.leg.strike,
                    "qty": fl.leg.qty,
                    "signed_qty": fl.leg.signed_qty,
                    "entry_price": round(fl.entry_price, 2),
                    "exit_price": round(fl.exit_price, 2) if fl.exit_price is not None else None,
                    "source": fl.source.value,
                }
                for fl in c.legs
            ],
        }
        for c in result.condors
    ]

    equity = result.equity
    stride = max(1, len(equity) // 1500)
    curve = [
        {
            "ts": p.ts.isoformat(),
            "equity": round(p.equity, 2),
            "drawdown": round(p.drawdown, 2),
            "open_condors": p.open_condors,
            "spot": round(p.spot, 2) if p.spot is not None else None,
        }
        for p in equity[::stride]
    ]

    spots = [p.spot for p in equity if p.spot]
    lo, hi = (min(spots) * 0.94, max(spots) * 1.06) if spots else (23000, 25000)
    grid = [lo + (hi - lo) * i / 200 for i in range(201)]
    payoff, campaign = _campaign_payoff(result.condors, grid)

    down_condors = [c for c in result.condors if getattr(c, "side", "down") in ("down", "anchor")]
    up_condors = [c for c in result.condors if getattr(c, "side", "down") == "up"]
    down_pnl = sum(c.realised_pnl() for c in down_condors)
    up_pnl = sum(c.realised_pnl() for c in up_condors)
    down_credit = sum(c.credit for c in down_condors)
    up_credit = sum(c.credit for c in up_condors)
    attribution = {
        "down_pnl": round(down_pnl, 2),
        "up_pnl": round(up_pnl, 2),
        "down_condors": len(down_condors),
        "up_condors": len(up_condors),
        "down_credit": round(down_credit, 2),
        "up_credit": round(up_credit, 2),
    }

    bundle = {
        "provenance": provenance,
        "params": {
            "step": strategy.step,
            "short_offset": strategy.short_offset,
            "long_offset": strategy.long_offset,
            "lots": strategy.lots,
            "lot_size": strategy.lot_size,
            "qty": strategy.qty,
            "max_condors": strategy.max_condors,
            "fill_gaps": strategy.fill_gaps,
            "take_profit_pct": strategy.take_profit_pct,
            "stop_loss_mult": strategy.stop_loss_mult,
            "direction": strategy.direction,
            "max_down": strategy.max_down,
            "max_up": strategy.max_up,
            "anchor_mode": params.anchor_mode or strategy.effective_anchor_mode,
            "roll_to_next_expiry": params.roll_to_next_expiry,
            "label": params.label,
        },
        "metrics": result.metrics.to_dict(),
        "attribution": attribution,
        "netting": result.netting,
        "campaigns": result.campaigns,
        "rolls": [
            {"when": w.isoformat(), "from": a.isoformat(), "to": b.isoformat()}
            for w, a, b in result.rolls
        ],
        "payoff_campaign": campaign,
        "condors": condors,
        "equity": curve,
        "payoff": payoff,
        "strike_matrix": result.strike_matrix(),
        "triggers": [
            {
                "level": t.level,
                "time": t.time.isoformat(),
                "spot": round(t.spot, 2),
                "reason": t.reason,
                "side": getattr(t, "side", "down"),
            }
            for t in result.triggers
        ],
        "warnings": result.warnings,
        "skipped": [[w.isoformat(), lv, why] for w, lv, why in result.skipped],
    }
    return json_safe(bundle)
