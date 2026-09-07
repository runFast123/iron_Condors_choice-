"""Generate a dataset for the dashboard from real NIFTY history.

Runs the full two-pass backtest over real ``^NSEI`` spot and real India VIX,
with **modeled** option premiums (Black-76 + skew), and writes a JSON bundle
the web app reads.

This exists so the deployed dashboard has something honest to show before
Choice credentials and a static IP are wired up.  Every premium in the output
is tagged ``modeled`` and the bundle carries an explicit provenance block, so
nothing here can be mistaken for verified broker data.

    python -m engine.tools.seed --days 400 --out web/public/data/seed.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
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
    spots_from_frame,
    weekly_expiry_resolver,
)
from engine.config import IST
from engine.data.yahoo import YahooClient
from engine.pricing.costs import CostModel
from engine.pricing.iv_surface import IVSurface, from_vix
from engine.strategy.condor import StrategyConfig

log = logging.getLogger(__name__)

# NIFTY weekly expiry weekday. NSE has moved this more than once, so it is a
# parameter rather than a constant baked into the engine.
DEFAULT_EXPIRY_WEEKDAY = 3  # Thursday


def weekly_expiries(start: dt.date, end: dt.date, weekday: int = DEFAULT_EXPIRY_WEEKDAY) -> list[dt.date]:
    """Every expiry weekday between ``start`` and a month past ``end``."""
    out: list[dt.date] = []
    day = start
    while day.weekday() != weekday:
        day += dt.timedelta(days=1)
    horizon = end + dt.timedelta(days=31)
    while day <= horizon:
        out.append(day)
        day += dt.timedelta(days=7)
    return out


def build(
    days: int = 400,
    resolution: str = "D",
    lots: int = 1,
    step: float = 100.0,
    max_condors: int = 20,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    expiry_weekday: int = DEFAULT_EXPIRY_WEEKDAY,
) -> tuple[BacktestResult, dict]:
    yahoo = YahooClient()
    end = dt.datetime.now(tz=IST).date()
    start = end - dt.timedelta(days=days)

    log.info("Fetching ^NSEI %s..%s @ %s", start, end, resolution)
    nifty = yahoo.nifty(start, end, resolution)
    if nifty.empty:
        raise RuntimeError("Yahoo returned no NIFTY data for the requested range.")

    vix_map = yahoo.vix_by_date(start, end)
    latest_vix = list(vix_map.values())[-1] if vix_map else 14.0
    surface = from_vix(latest_vix)

    spots = spots_from_frame(nifty)
    first_day, last_day = spots[0][0].date(), spots[-1][0].date()
    expiries = weekly_expiries(first_day, last_day, expiry_weekday)

    params = BacktestParams(
        strategy=StrategyConfig(
            step=step,
            lots=lots,
            lot_size=75,
            max_condors=max_condors,
            take_profit_pct=take_profit,
            stop_loss_mult=stop_loss,
        ),
        costs=CostModel(),
        label=f"NIFTY ladder {first_day}..{last_day}",
    )

    provider = FallbackPriceProvider(
        primary=CandlePriceProvider(),                      # empty: no Choice data yet
        fallback=ModelPriceProvider(surface=surface, vix_by_date=vix_map),
    )
    engine = Backtest(params, provider, weekly_expiry_resolver(expiries, min_dte=1))
    result = engine.run(spots)

    provenance = {
        "spot_source": "yahoo:^NSEI",
        "vol_source": "yahoo:^INDIAVIX",
        "premium_source": "modeled:black76",
        "verified": False,
        "note": (
            "Option premiums are MODELED with Black-76 driven by India VIX and a strike skew. "
            "Yahoo Finance carries no Indian option chain, so no market premium exists for these "
            "legs until Choice historical data is connected. Expiries are synthesised weekly."
        ),
        "expiry_weekday": expiry_weekday,
        "resolution": resolution,
        "generated_at": dt.datetime.now(tz=IST).isoformat(),
        "provider": provider.summary(),
        "bars": len(spots),
        "range": [first_day.isoformat(), last_day.isoformat()],
    }
    return result, provenance


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

    # Thin the equity curve for the browser without losing its shape.
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

    # Payoff curve across a sensible spot range around the campaign.
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
            "label": params.label,
        },
        "metrics": result.metrics.to_dict(),
        "netting": result.netting,
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--resolution", default="D")
    parser.add_argument("--lots", type=int, default=1)
    parser.add_argument("--step", type=float, default=100.0)
    parser.add_argument("--max-condors", type=int, default=20)
    parser.add_argument("--take-profit", type=float, default=None)
    parser.add_argument("--stop-loss", type=float, default=None)
    parser.add_argument("--expiry-weekday", type=int, default=DEFAULT_EXPIRY_WEEKDAY)
    parser.add_argument("--out", default="web/public/data/seed.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    result, provenance = build(
        days=args.days,
        resolution=args.resolution,
        lots=args.lots,
        step=args.step,
        max_condors=args.max_condors,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        expiry_weekday=args.expiry_weekday,
    )
    bundle = serialise(result, provenance)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, separators=(",", ":")), encoding="utf-8")

    m = result.metrics
    print(f"\n  Wrote {out}  ({out.stat().st_size / 1024:.0f} KB)")
    print(f"  Range        : {provenance['range'][0]} .. {provenance['range'][1]}  ({provenance['bars']} bars)")
    print(f"  Condors      : {m.condors}   max concurrent {m.max_concurrent}")
    print(f"  Net P&L      : Rs {m.net_pnl:,.0f}   (costs Rs {m.total_costs:,.0f})")
    print(f"  Win rate     : {m.win_rate:.1%}   profit factor {m.profit_factor:.2f}")
    print(f"  Max drawdown : Rs {m.max_drawdown:,.0f}   ({m.max_drawdown_pct:.1%})")
    print(f"  Capital risk : Rs {m.capital_at_risk:,.0f}")
    print(f"  Offset ratio : {result.netting['offset_ratio']:.1%} of gross qty self-hedged")
    print(f"  Real premiums: {m.real_price_fraction:.1%}  (rest MODELED)")
    for warning in result.warnings:
        print(f"  ! {warning}")


if __name__ == "__main__":
    main()
