"""Build the dashboard dataset from Choice FinX.

Every price in the output comes from Choice: the NIFTY spot series, India VIX,
the real weekly expiries from the scrip master, and the option premiums
themselves. There is no third-party fallback -- if Choice cannot serve a leg,
that fact is recorded and the premium is modeled with Black-76 off
Choice-sourced volatility, tagged ``modeled`` so nothing can be mistaken for a
broker fill.

    python -m engine.tools.seed --days 120 --resolution 5

Requires CHOICE_VENDOR_ID / CHOICE_API_KEY / CHOICE_MOBILE_NO in .env, and must
run from the static IP declared against that API key.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

from engine.backtest.providers import (
    CandlePriceProvider,
    FallbackPriceProvider,
    ModelPriceProvider,
)
from engine.backtest.runner import (
    Backtest,
    BacktestParams,
    BacktestResult,
    discover_requirements,
    spots_from_frame,
    weekly_expiry_resolver,
)
from engine.choice.errors import ChoiceError
from engine.config import IST, choice_config
from engine.data.market import NIFTY, ChoiceMarketData
from engine.pricing.costs import CostModel
from engine.pricing.iv_surface import IVSurface, from_vix
from engine.strategy.condor import StrategyConfig

log = logging.getLogger(__name__)

DEFAULT_ATM_VOL = 0.14


def build(
    days: int = 120,
    resolution: str = "D",
    lots: int = 1,
    step: float = 100.0,
    max_condors: int = 20,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    option_resolution: str | None = None,
) -> tuple[BacktestResult, dict]:
    market = ChoiceMarketData.connect()

    end = dt.datetime.now(tz=IST).date()
    start = end - dt.timedelta(days=days)

    log.info("Fetching NIFTY %s..%s @ %s from Choice", start, end, resolution)
    nifty = market.nifty(start, end, resolution)
    if nifty.empty:
        raise RuntimeError(
            "Choice returned no NIFTY spot data for the requested range. "
            "Run `python -m engine.tools.doctor` to see the broker's own error."
        )
    spots = spots_from_frame(nifty)

    vix_map = market.vix_by_date(start, end)
    if vix_map:
        surface = from_vix(list(vix_map.values())[-1])
        vol_source = "choice:INDIAVIX"
    else:
        surface = IVSurface(atm_vol=DEFAULT_ATM_VOL)
        vol_source = f"default:{DEFAULT_ATM_VOL:.0%}"
        log.warning("India VIX unavailable from Choice; using a flat %.0f%% ATM vol", DEFAULT_ATM_VOL * 100)

    # Real listed expiries, not a synthesised weekday.
    first_day, last_day = spots[0][0].date(), spots[-1][0].date()
    expiries = market.master.expiries(NIFTY, after=first_day)
    if not expiries:
        raise RuntimeError(
            f"No {NIFTY} option expiries in the scrip master on/after {first_day}. "
            "The scrip master may be stale."
        )
    lot_size = market.master.lot_size_for(NIFTY)

    params = BacktestParams(
        strategy=StrategyConfig(
            step=step,
            lots=lots,
            lot_size=lot_size,
            max_condors=max_condors,
            strike_step=market.master.strike_step(NIFTY, expiries[0]),
            take_profit_pct=take_profit,
            stop_loss_mult=stop_loss,
        ),
        costs=CostModel(),
        label=f"NIFTY ladder {first_day}..{last_day}",
    )
    expiry_for = weekly_expiry_resolver(expiries, min_dte=1)

    # Pass 1: find exactly which legs the campaign touches, then fetch only
    # those from Choice rather than the entire chain.
    _, requirements = discover_requirements(spots, params, expiry_for)
    log.info("Fetching %d option legs from Choice", len(requirements))

    candles = CandlePriceProvider()
    fetched = missing = 0
    opt_res = option_resolution or resolution
    for i, req in enumerate(requirements, 1):
        try:
            frame = market.option_candles(
                NIFTY, req.expiry, req.strike, req.right, start, end, opt_res
            )
        except ChoiceError as exc:
            missing += 1
            log.warning("  [%d/%d] %s %g %s: %s", i, len(requirements), req.expiry, req.strike, req.right, exc)
            continue
        if frame.empty:
            missing += 1
            continue
        candles.add(req.expiry, req.strike, req.right, frame)
        fetched += 1
        if i % 20 == 0:
            log.info("  ...%d/%d legs", i, len(requirements))

    log.info("Option legs: %d with Choice data, %d without", fetched, missing)

    provider = FallbackPriceProvider(
        primary=candles,
        fallback=ModelPriceProvider(surface=surface, vix_by_date=vix_map or None),
    )
    result = Backtest(params, provider, expiry_for).run(spots)

    coverage = market.coverage_summary()
    provenance = {
        "spot_source": "choice:NIFTY",
        "vol_source": vol_source,
        "premium_source": "choice:ChartData" if fetched else "modeled:black76",
        "expiry_source": "choice:scripmaster",
        "verified": provider.modeled_quotes == 0,
        "note": (
            "All prices sourced from Choice FinX."
            if provider.modeled_quotes == 0
            else (
                f"{fetched} of {len(requirements)} option legs had Choice historical data. "
                "The rest are MODELED with Black-76 driven by Choice-sourced India VIX and a "
                "strike skew, because Choice served no candles for those contracts."
            )
        ),
        "resolution": resolution,
        "option_resolution": opt_res,
        "generated_at": dt.datetime.now(tz=IST).isoformat(),
        "provider": provider.summary(),
        "bars": len(spots),
        "range": [first_day.isoformat(), last_day.isoformat()],
        "legs_requested": len(requirements),
        "legs_with_choice_data": fetched,
        "coverage": coverage,
        "failures": market.failures()[:50],
        "lot_size": lot_size,
    }
    return result, provenance


def empty_bundle(reason: str) -> dict:
    """A dataset that says plainly there is nothing to show yet.

    Shipped when Choice has not been connected, so the dashboard renders an
    honest empty state instead of numbers from a source that is not allowed.
    """
    return {
        "provenance": {
            "spot_source": "choice:NIFTY",
            "vol_source": "choice:INDIAVIX",
            "premium_source": "choice:ChartData",
            "expiry_source": "choice:scripmaster",
            "verified": False,
            "awaiting_connection": True,
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
            "lots": 1, "lot_size": 75, "qty": 75, "max_condors": 20,
            "fill_gaps": True, "take_profit_pct": None, "stop_loss_mult": None,
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
        "netting": {
            "strikes_touched": 0, "strikes_fully_offset": 0, "gross_qty": 0,
            "net_qty": 0, "offset_qty": 0, "offset_ratio": 0.0,
        },
        "condors": [], "equity": [], "payoff": [], "strike_matrix": [],
        "triggers": [], "warnings": [reason], "skipped": [],
    }


def serialise(result: BacktestResult, provenance: dict) -> dict:
    """Flatten a result into the JSON shape the dashboard consumes."""
    params = result.params
    strategy = params.strategy

    condors = [
        {
            "index": c.index,
            "level": c.level,
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
    payoff = [
        {"spot": round(s, 2), "pnl": round(sum(c.payoff_at_expiry(s) for c in result.condors), 2)}
        for s in grid
    ]

    return {
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
            "anchor_mode": params.anchor_mode,
            "roll_to_next_expiry": params.roll_to_next_expiry,
            "label": params.label,
        },
        "metrics": result.metrics.to_dict(),
        "netting": result.netting,
        "campaigns": result.campaigns,
        "rolls": [
            {"when": w.isoformat(), "from": a.isoformat(), "to": b.isoformat()}
            for w, a, b in result.rolls
        ],
        "condors": condors,
        "equity": curve,
        "payoff": payoff,
        "strike_matrix": result.strike_matrix(),
        "triggers": [
            {"level": t.level, "time": t.time.isoformat(), "spot": round(t.spot, 2), "reason": t.reason}
            for t in result.triggers
        ],
        "warnings": result.warnings,
        "skipped": [[w.isoformat(), lv, why] for w, lv, why in result.skipped],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--resolution", default="D", help="Spot bar size: 1, 5, 15, 60, D")
    parser.add_argument("--option-resolution", default=None, help="Defaults to --resolution")
    parser.add_argument("--lots", type=int, default=1)
    parser.add_argument("--step", type=float, default=100.0)
    parser.add_argument("--max-condors", type=int, default=20)
    parser.add_argument("--take-profit", type=float, default=None)
    parser.add_argument("--stop-loss", type=float, default=None)
    parser.add_argument("--out", default="web/data/seed.json")
    parser.add_argument(
        "--write-empty",
        action="store_true",
        help="Write the 'awaiting Choice connection' placeholder without calling the API.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.write_empty or not choice_config.configured:
        reason = (
            "Choice FinX is not connected. Choice is the only permitted data source for this "
            "project, so there is nothing to display until credentials are configured and the "
            "engine is run from the declared static IP."
        )
        if not args.write_empty:
            print("Choice credentials are not configured; writing the empty-state dataset.\n")
        out.write_text(json.dumps(empty_bundle(reason), separators=(",", ":")), encoding="utf-8")
        print(f"  Wrote {out} (empty state)")
        print("  Fill .env, then run: python -m engine.tools.doctor && python -m engine.tools.seed")
        return 0 if args.write_empty else 1

    result, provenance = build(
        days=args.days,
        resolution=args.resolution,
        lots=args.lots,
        step=args.step,
        max_condors=args.max_condors,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        option_resolution=args.option_resolution,
    )
    out.write_text(json.dumps(serialise(result, provenance), separators=(",", ":")), encoding="utf-8")

    m = result.metrics
    print(f"\n  Wrote {out}  ({out.stat().st_size / 1024:.0f} KB)")
    print(f"  Range        : {provenance['range'][0]} .. {provenance['range'][1]}  ({provenance['bars']} bars)")
    print(f"  Option legs  : {provenance['legs_with_choice_data']}/{provenance['legs_requested']} from Choice")
    print(f"  Condors      : {m.condors}   max concurrent {m.max_concurrent}")
    print(f"  Net P&L      : Rs {m.net_pnl:,.0f}   (costs Rs {m.total_costs:,.0f})")
    print(f"  Win rate     : {m.win_rate:.1%}   profit factor {m.profit_factor:.2f}")
    print(f"  Max drawdown : Rs {m.max_drawdown:,.0f}")
    print(f"  Offset ratio : {result.netting['offset_ratio']:.1%} of gross qty self-hedged")
    print(f"  Real premiums: {m.real_price_fraction:.1%}")
    for warning in result.warnings:
        print(f"  ! {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
