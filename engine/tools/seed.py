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
from engine.backtest.serialise import empty_bundle, serialise
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
