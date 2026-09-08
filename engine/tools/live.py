"""Run the forward test against live Choice data.

    python -m engine.tools.live                      # paper, poll every 15s
    python -m engine.tools.live --ticks 4            # a few cycles, then stop
    python -m engine.tools.live --mode live --arm    # REAL ORDERS

Writes ``web/data/live.json`` on every tick, which is what the dashboard's
Forward Test section reads. Must run from the static IP declared against your
Choice API key.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from engine.choice.errors import ChoiceError
from engine.config import IST, choice_config, engine_config
from engine.data.market import NIFTY, ChoiceMarketData
from engine.forward.runner import ForwardRunner, market_is_open
from engine.pricing.costs import CostModel
from engine.strategy.condor import StrategyConfig

log = logging.getLogger(__name__)


def empty_state(reason: str) -> dict:
    """The 'not connected' shape, so the Forward Test pages render honestly."""
    import datetime as dt

    return {
        "session": {
            "mode": "paper", "armed": False, "status": "disconnected",
            "stopped_reason": reason, "started_at": None, "last_tick": None,
            "market_open": market_is_open(), "connected": False, "expiry": None,
        },
        "market": {"spot": None, "ts": None},
        "ladder": {"anchor": None, "last_level": None, "next_trigger": None,
                   "distance": None, "fired": [], "step": 100.0},
        "pnl": {"realised": 0, "unrealised": 0, "total": 0, "open_rungs": 0, "total_rungs": 0},
        "netting": {"strikes_touched": 0, "strikes_fully_offset": 0, "gross_qty": 0,
                    "net_qty": 0, "offset_qty": 0, "offset_ratio": 0.0},
        "positions": [], "fills": [], "events": [], "net_positions": [],
        "generated_at": dt.datetime.now(tz=IST).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    parser.add_argument("--arm", action="store_true", help="Required for --mode live to place orders.")
    parser.add_argument("--poll", type=float, default=15.0, help="Seconds between ticks.")
    parser.add_argument("--ticks", type=int, default=None, help="Stop after N ticks.")
    parser.add_argument("--lots", type=int, default=1)
    parser.add_argument("--step", type=float, default=100.0)
    parser.add_argument("--out", default="web/data/live.json")
    parser.add_argument("--write-empty", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.write_empty or not choice_config.configured:
        reason = (
            "Choice FinX is not connected. Forward testing needs a live session, which requires "
            "credentials and must run from the declared static IP."
        )
        out.write_text(json.dumps(empty_state(reason), separators=(",", ":")), encoding="utf-8")
        if args.write_empty:
            print(f"  Wrote {out} (disconnected state)")
            return 0
        print("Choice credentials are not configured.")
        print("  Fill .env, then: python -m engine.tools.doctor && python -m engine.tools.live")
        return 1

    if args.mode == "live" and not args.arm:
        print("Refusing to place real orders without --arm. Re-run with --mode live --arm.")
        return 2

    if not market_is_open():
        print("NSE regular session is 09:15-15:30 IST, Mon-Fri. Running anyway; the runner will idle.")

    try:
        market = ChoiceMarketData.connect()
    except ChoiceError as exc:
        print(f"Could not connect to Choice: {exc}")
        out.write_text(json.dumps(empty_state(str(exc)), separators=(",", ":")), encoding="utf-8")
        return 1

    lot_size = market.master.lot_size_for(NIFTY)
    strategy = StrategyConfig(
        step=args.step, lots=args.lots, lot_size=lot_size,
        max_condors=engine_config.max_condors,
        strike_step=market.master.strike_step(
            NIFTY, market.master.nearest_expiry(NIFTY, __import__("datetime").date.today())
        ),
    )

    runner = ForwardRunner(
        market=market, strategy=strategy, mode=args.mode,
        costs=CostModel(), state_path=out,
    )
    if args.mode == "live" and args.arm:
        runner.arm()

    print(f"Forward run: mode={args.mode} armed={runner.armed} lot={lot_size} step={args.step:g}")
    print(f"State file : {out}")
    runner.run(poll_seconds=args.poll, max_ticks=args.ticks)

    snap = runner.snapshot()
    print(f"\n  Ticks done. Spot {snap['market']['spot']}  rungs {snap['pnl']['total_rungs']}"
          f"  P&L {snap['pnl']['total']:,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
