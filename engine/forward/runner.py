"""Forward testing against live Choice data.

Drives the *same* :class:`~engine.strategy.ladder.Ladder` and
:func:`~engine.strategy.condor.build_legs` the backtester uses, so a forward
run cannot silently diverge from the backtest that justified it. The only
difference is where prices come from: live Choice quotes instead of a bar
iterator.

Two modes:

* ``paper``  — fills are simulated at the live Choice LTP. Nothing is sent to
  the exchange. This is the default and needs no order permissions.
* ``live``   — real orders. Requires an explicit ``arm()`` call, and is capped
  by a max-rung limit and a daily-loss kill switch.

Everything the runner does is appended to a structured event log, and every
fill lands in the trade history, so the dashboard can show exactly what
happened and when.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from engine.choice.errors import ChoiceError
from engine.choice.instruments import Contract
from engine.config import IST, engine_config
from engine.data.market import NIFTY, ChoiceMarketData
from engine.pricing.costs import CostModel
from engine.strategy.condor import (
    Condor,
    CondorStatus,
    FilledLeg,
    Leg,
    PriceSource,
    Side,
    StrategyConfig,
    build_legs,
    net_positions,
    netting_summary,
)
from engine.strategy.ladder import Ladder

log = logging.getLogger(__name__)

MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)


def market_is_open(now: dt.datetime | None = None) -> bool:
    """NSE regular session, Monday to Friday.

    Holidays are not encoded: the authority on whether the market is open is
    Choice's own MarketStatus endpoint, which the runner consults. This is a
    cheap pre-filter so we do not poll all night.
    """
    now = now or dt.datetime.now(tz=IST)
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


@dataclass
class Event:
    """One line of the run log."""

    ts: str
    level: str          # info | trade | warn | error
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Fill:
    """A completed leg, paper or real."""

    ts: str
    condor_index: int
    condor_level: float
    expiry: str
    right: str
    side: str
    strike: float
    qty: int
    price: float
    source: str
    mode: str
    token: int | None = None
    order_id: str | None = None
    action: str = "OPEN"       # OPEN | CLOSE


class ForwardRunner:
    """Runs the ladder against live Choice prices."""

    def __init__(
        self,
        market: ChoiceMarketData,
        strategy: StrategyConfig,
        *,
        mode: str = "paper",
        costs: CostModel | None = None,
        state_path: Path | None = None,
        max_events: int = 500,
    ) -> None:
        self.market = market
        self.strategy = strategy
        self.mode = mode
        self.costs = costs or CostModel()
        self.state_path = state_path or Path("web/data/live.json")
        self.max_events = max_events

        self.ladder = Ladder(config=strategy)
        self.condors: list[Condor] = []
        self.events: list[Event] = []
        self.fills: list[Fill] = []
        self.armed = mode != "live"      # live mode must be armed explicitly
        self.started_at = dt.datetime.now(tz=IST)
        self.last_tick: dt.datetime | None = None
        self.last_spot: float | None = None
        self.expiry: dt.date | None = None
        self.realised = 0.0
        self.stopped_reason: str | None = None
        self._contracts: dict[tuple[str, float, str], Contract] = {}

    # ------------------------------------------------------------- logging

    def emit(self, level: str, message: str, **detail: Any) -> None:
        event = Event(
            ts=dt.datetime.now(tz=IST).isoformat(), level=level, message=message, detail=detail
        )
        self.events.append(event)
        if len(self.events) > self.max_events:
            del self.events[: len(self.events) - self.max_events]
        getattr(log, "error" if level == "error" else "info")("%s %s", message, detail or "")

    def arm(self) -> None:
        """Enable real order placement. Deliberately a separate step."""
        self.armed = True
        self.emit("warn", "Runner ARMED for live orders", mode=self.mode)

    # ------------------------------------------------------------- pricing

    def _contract(self, expiry: dt.date, strike: float, right: str) -> Contract:
        key = (expiry.isoformat(), strike, right)
        if key not in self._contracts:
            self._contracts[key] = self.market.master.option(NIFTY, expiry, strike, right)
        return self._contracts[key]

    def _quote_legs(self, legs: list[Leg], expiry: dt.date) -> dict[Leg, tuple[float, Contract]] | None:
        """Live LTP for all four legs, or None if any is missing.

        A partial quote is refused: opening three of four legs would leave a
        naked short in the book.
        """
        try:
            contracts = {leg: self._contract(expiry, leg.strike, leg.right) for leg in legs}
        except ChoiceError as exc:
            self.emit("error", "Could not resolve a leg", error=str(exc))
            return None

        try:
            ltps = self.market.touchline(list(contracts.values()))
        except ChoiceError as exc:
            self.emit("error", "Touchline failed", error=str(exc))
            return None

        out: dict[Leg, tuple[float, Contract]] = {}
        for leg, contract in contracts.items():
            price = ltps.get(contract.token)
            if price is None or price <= 0:
                self.emit(
                    "warn",
                    "No live quote for a leg; rung skipped",
                    strike=leg.strike, right=leg.right, token=contract.token,
                )
                return None
            out[leg] = (price, contract)
        return out

    # ---------------------------------------------------------------- open

    def _open_condor(self, level: float, expiry: dt.date) -> Condor | None:
        legs = build_legs(level, self.strategy)
        quoted = self._quote_legs(legs, expiry)
        if quoted is None:
            return None

        now = dt.datetime.now(tz=IST)
        filled: list[FilledLeg] = []
        entry_costs = 0.0

        # Protective wings first, shorts last, so the account never shows a
        # naked short mid-structure.
        for leg in legs:
            price, contract = quoted[leg]
            order_id = None
            if self.mode == "live" and self.armed:
                order_id = self._place(leg, contract, price)
                if order_id is None:
                    self.emit("error", "Leg rejected; aborting rung", level=level, strike=leg.strike)
                    return None
            filled.append(
                FilledLeg(leg=leg, entry_price=price, source=PriceSource.CHOICE, token=contract.token)
            )
            entry_costs += self.costs.leg_cost(leg.side, price, leg.qty)
            self.fills.append(
                Fill(
                    ts=now.isoformat(), condor_index=len(self.condors), condor_level=level,
                    expiry=expiry.isoformat(), right=leg.right, side=leg.side.value,
                    strike=leg.strike, qty=leg.qty, price=price, source="choice",
                    mode=self.mode, token=contract.token, order_id=order_id, action="OPEN",
                )
            )

        condor = Condor(
            level=level, entry_time=now, expiry=expiry, legs=filled,
            config=self.strategy, entry_costs=entry_costs, index=len(self.condors),
        )
        self.condors.append(condor)
        self.emit(
            "trade", f"Opened rung at {level:,.0f}",
            level=level, credit=round(condor.credit, 2),
            max_loss=round(condor.max_loss, 2), expiry=expiry.isoformat(), mode=self.mode,
        )
        return condor

    def _place(self, leg: Leg, contract: Contract, ltp: float) -> str | None:
        """Place one real order.

        Choice supports only RL_LIMIT / SL_LIMIT -- there is no market order --
        so we send a limit priced through the touch by a slippage buffer.
        Prices go on the wire in paisa, quantity in shares.
        """
        buffer = max(0.05, ltp * 0.01)
        limit = ltp + buffer if leg.side is Side.BUY else max(0.05, ltp - buffer)
        payload = {
            "SegmentId": contract.segment_id,
            "Token": contract.token,
            "OrderType": "RL_LIMIT",
            "BS": 1 if leg.side is Side.BUY else 2,
            "Qty": leg.qty,
            "Price": int(round(limit * 100)),
            "TriggerPrice": 0,
            "Validity": 1,
            "ProductType": "M",
            "DisclosedQty": 0,
            # Unique per leg: kkunal hardcodes 123456 for every order, which
            # makes a four-leg structure impossible to modify or cancel.
            "ClientOrderNo": int(time.time() * 1000) % 2_000_000_000 + leg.strike.__hash__() % 1000,
            "Remarks": "condor-ladder",
            "ModeTyp": "WEBAPI",
            "Mode": 1,
            "DeviceId": "ENGINE",
        }
        try:
            resp = self.market.session.request(
                "POST", "api/OpenAPI/V2/NewOrder", payload, is_order=True
            )
        except ChoiceError as exc:
            self.emit("error", "Order failed", strike=leg.strike, right=leg.right, error=str(exc))
            return None
        if str(resp.get("Status", "")).lower() != "success":
            self.emit("error", "Order rejected", strike=leg.strike, response=str(resp)[:200])
            return None
        body = resp.get("Response") or {}
        return str(body.get("OrderNo") or body.get("ClientOrderNo") or "")

    # -------------------------------------------------------------- manage

    def _mark_all(self) -> dict[int, float]:
        """Live MTM per open condor index."""
        out: dict[int, float] = {}
        open_condors = [c for c in self.condors if c.is_open]
        if not open_condors:
            return out
        contracts = []
        for condor in open_condors:
            for fl in condor.legs:
                try:
                    contracts.append(self._contract(condor.expiry, fl.leg.strike, fl.leg.right))
                except ChoiceError:
                    pass
        try:
            ltps = self.market.touchline(contracts)
        except ChoiceError as exc:
            self.emit("warn", "MTM refresh failed", error=str(exc))
            return out

        for condor in open_condors:
            marks: dict[Leg, float] = {}
            for fl in condor.legs:
                if fl.token is not None and fl.token in ltps:
                    marks[fl.leg] = ltps[fl.token]
            if len(marks) == len(condor.legs):
                out[condor.index] = condor.mtm(marks)
                reason = condor.exit_signal(marks)
                if reason:
                    self._close(condor, marks, reason)
        return out

    def _close(self, condor: Condor, marks: dict[Leg, float], reason: str) -> None:
        now = dt.datetime.now(tz=IST)
        exit_costs = 0.0
        for fl in condor.legs:
            price = marks[fl.leg]
            fl.exit_price = price
            fl.exit_source = PriceSource.CHOICE
            opposite = Side.BUY if fl.leg.side is Side.SELL else Side.SELL
            exit_costs += self.costs.leg_cost(opposite, price, fl.leg.qty)
            self.fills.append(
                Fill(
                    ts=now.isoformat(), condor_index=condor.index, condor_level=condor.level,
                    expiry=condor.expiry.isoformat(), right=fl.leg.right, side=opposite.value,
                    strike=fl.leg.strike, qty=fl.leg.qty, price=price, source="choice",
                    mode=self.mode, token=fl.token, action="CLOSE",
                )
            )
        status = CondorStatus.CLOSED_TARGET if "take-profit" in reason else CondorStatus.CLOSED_STOP
        condor.close(now, reason, status, exit_costs)
        self.realised += condor.realised_pnl()
        self.emit(
            "trade", f"Closed rung at {condor.level:,.0f}",
            level=condor.level, reason=reason, pnl=round(condor.realised_pnl(), 2),
        )

    # ----------------------------------------------------------------- tick

    def tick(self) -> None:
        """One polling cycle: read spot, fire triggers, refresh MTM."""
        try:
            index = self.market.master.index(NIFTY)
            spot = self.market.ltp(index)
        except ChoiceError as exc:
            self.emit("error", "Spot quote failed", error=str(exc))
            return
        if spot is None or spot <= 0:
            self.emit("warn", "No spot quote returned")
            return

        now = dt.datetime.now(tz=IST)
        self.last_spot, self.last_tick = spot, now

        if self.expiry is None:
            self.expiry = self.market.master.nearest_expiry(NIFTY, now.date(), min_days=0)
            self.emit("info", f"Trading expiry {self.expiry:%d-%b-%Y}", expiry=self.expiry.isoformat())

        for trigger in self.ladder.on_price(spot, now):
            if len([c for c in self.condors if c.is_open]) >= engine_config.max_condors:
                self.emit("warn", "Max concurrent rungs reached; trigger ignored", level=trigger.level)
                continue
            self._open_condor(trigger.level, self.expiry)

        mtm = self._mark_all()
        total = self.realised + sum(mtm.values())
        if total <= -abs(engine_config.daily_loss_limit):
            self.stopped_reason = f"daily loss limit hit ({total:,.0f})"
            self.armed = False
            self.emit("error", "KILL SWITCH: " + self.stopped_reason, pnl=round(total, 2))

    # ---------------------------------------------------------------- state

    def snapshot(self) -> dict[str, Any]:
        mtm = {c.index: 0.0 for c in self.condors if c.is_open}
        unrealised = sum(mtm.values())
        summary = netting_summary(self.condors, open_only=False)

        return {
            "session": {
                "mode": self.mode,
                "armed": self.armed,
                "status": "stopped" if self.stopped_reason else "running",
                "stopped_reason": self.stopped_reason,
                "started_at": self.started_at.isoformat(),
                "last_tick": self.last_tick.isoformat() if self.last_tick else None,
                "market_open": market_is_open(),
                "connected": True,
                "expiry": self.expiry.isoformat() if self.expiry else None,
            },
            "market": {"spot": self.last_spot, "ts": self.last_tick.isoformat() if self.last_tick else None},
            "ladder": {
                "anchor": self.ladder.anchor,
                "last_level": self.ladder.last_level,
                "next_trigger": self.ladder.next_trigger_level,
                "distance": self.ladder.distance_to_next(self.last_spot) if self.last_spot else None,
                "fired": self.ladder.levels(),
                "step": self.strategy.step,
            },
            "pnl": {
                "realised": round(self.realised, 2),
                "unrealised": round(unrealised, 2),
                "total": round(self.realised + unrealised, 2),
                "open_rungs": len([c for c in self.condors if c.is_open]),
                "total_rungs": len(self.condors),
            },
            "netting": summary,
            "positions": [
                {
                    "index": c.index, "level": c.level, "expiry": c.expiry.isoformat(),
                    "entry_time": c.entry_time.isoformat(), "status": c.status.value,
                    "credit": round(c.credit, 2), "max_loss": round(c.max_loss, 2),
                    "pnl": round(c.realised_pnl(), 2) if not c.is_open else None,
                    "exit_reason": c.exit_reason,
                    "legs": [
                        {
                            "right": fl.leg.right, "side": fl.leg.side.value, "strike": fl.leg.strike,
                            "qty": fl.leg.qty, "entry_price": round(fl.entry_price, 2),
                            "exit_price": round(fl.exit_price, 2) if fl.exit_price is not None else None,
                            "token": fl.token, "source": fl.source.value,
                        }
                        for fl in c.legs
                    ],
                }
                for c in self.condors
            ],
            "fills": [asdict(f) for f in reversed(self.fills)],
            "events": [asdict(e) for e in reversed(self.events)],
            "net_positions": [
                {
                    "expiry": p.expiry.isoformat(), "right": p.right, "strike": p.strike,
                    "net_qty": p.net_qty, "gross_long": p.gross_long, "gross_short": p.gross_short,
                    "is_flat": p.is_flat,
                }
                for p in net_positions(self.condors, open_only=True)
            ],
            "generated_at": dt.datetime.now(tz=IST).isoformat(),
        }

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.snapshot(), separators=(",", ":")), encoding="utf-8"
        )

    # ------------------------------------------------------------------ run

    def run(
        self,
        poll_seconds: float = 15.0,
        max_ticks: int | None = None,
        on_tick: Callable[["ForwardRunner"], None] | None = None,
    ) -> None:
        self.emit(
            "info", f"Forward run started in {self.mode.upper()} mode",
            step=self.strategy.step, lots=self.strategy.lots, lot_size=self.strategy.lot_size,
        )
        ticks = 0
        try:
            while max_ticks is None or ticks < max_ticks:
                if not market_is_open():
                    self.emit("info", "Market closed; idling")
                    self.save()
                    if max_ticks is not None:
                        break
                    time.sleep(60)
                    continue
                self.tick()
                self.save()
                if on_tick:
                    on_tick(self)
                ticks += 1
                if self.stopped_reason:
                    break
                if max_ticks is None or ticks < max_ticks:
                    time.sleep(poll_seconds)
        except KeyboardInterrupt:
            self.emit("warn", "Interrupted by user")
        finally:
            self.emit("info", "Forward run stopped", ticks=ticks)
            self.save()
