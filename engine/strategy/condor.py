"""Iron-condor construction, valuation and netting.

Structure at reference level ``L`` (the defaults; all offsets configurable):

    SELL PE  L - 200      LONG PE  L - 400
    SELL CE  L + 200      LONG CE  L + 400

Both wings are 200 points wide, so the maximum loss at expiry is
``wing_width * lot_qty - credit``: only one side can finish in the money, so
the two wings never both pay out.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

CALL, PUT = "CE", "PE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """+1 for a long position, -1 for a short one."""
        return 1 if self is Side.BUY else -1


class CondorStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED_TARGET = "CLOSED_TARGET"
    CLOSED_STOP = "CLOSED_STOP"
    EXPIRED = "EXPIRED"


class PriceSource(str, Enum):
    """Where a price came from. Choice is the only external data source."""

    CHOICE = "choice"       # a real Choice FinX candle or quote
    MODELED = "modeled"     # Black-76, from Choice-sourced India VIX


@dataclass(frozen=True)
class StrategyConfig:
    """Every knob the ladder exposes. Defaults mirror the original spec."""

    step: float = 100.0            # points of decline between entries
    short_offset: float = 200.0    # short strike distance from the level
    long_offset: float = 400.0     # long (protective) strike distance
    lots: int = 1
    # Placeholder only. NSE revises this (25 -> 50 -> 75 -> 65), so the real
    # value is always read from the Choice scrip master at runtime and this
    # default is never used on a live path.
    lot_size: int = 65
    strike_step: float = 50.0      # listed strike grid, for rounding
    max_condors: int = 20
    fill_gaps: bool = True         # a gap-down fires every level it skipped

    # Exits. Hold-to-expiry is the default; either overlay may be disabled.
    take_profit_pct: float | None = None   # e.g. 0.50 -> close at 50% of credit
    stop_loss_mult: float | None = None    # e.g. 2.0  -> close at 2x credit lost

    def __post_init__(self) -> None:
        if self.step <= 0:
            raise ValueError("step must be positive")
        if self.long_offset <= self.short_offset:
            raise ValueError("long_offset must exceed short_offset (the wing needs width)")
        if self.lots < 1:
            raise ValueError("lots must be at least 1")

    @property
    def wing_width(self) -> float:
        """Distance between the short and long strike on one side."""
        return self.long_offset - self.short_offset

    @property
    def qty(self) -> int:
        """Order quantity in shares — Choice wants shares, not lots."""
        return self.lots * self.lot_size


@dataclass(frozen=True)
class Leg:
    right: str          # CE | PE
    side: Side
    strike: float
    qty: int            # always positive; direction lives in ``side``

    @property
    def signed_qty(self) -> int:
        return self.side.sign * self.qty

    def intrinsic(self, spot: float) -> float:
        """Per-share intrinsic value of this option at ``spot``."""
        if self.right == CALL:
            return max(0.0, spot - self.strike)
        return max(0.0, self.strike - spot)

    def payoff(self, spot: float, entry_price: float) -> float:
        """Total P&L of the leg if held to expiry with settlement at ``spot``."""
        return (self.intrinsic(spot) - entry_price) * self.signed_qty

    def __str__(self) -> str:
        return f"{self.side.value} {self.strike:g}{self.right} x{self.qty}"


@dataclass
class FilledLeg:
    """A leg once it has a price attached."""

    leg: Leg
    entry_price: float
    source: PriceSource = PriceSource.CHOICE
    token: int | None = None
    exit_price: float | None = None
    exit_source: PriceSource | None = None

    @property
    def credit(self) -> float:
        """Cash received at entry (negative when we paid to open)."""
        return -self.leg.signed_qty * self.entry_price

    def value(self, price: float) -> float:
        """Mark-to-market value of the position at ``price`` per share."""
        return self.leg.signed_qty * price

    def pnl(self, price: float) -> float:
        return (price - self.entry_price) * self.leg.signed_qty


def round_to_strike(value: float, strike_step: float) -> float:
    """Snap a computed strike onto the listed grid (NIFTY lists every 50)."""
    if strike_step <= 0:
        return float(value)
    return round(value / strike_step) * strike_step


def build_legs(level: float, config: StrategyConfig) -> list[Leg]:
    """The four legs of the condor anchored at ``level``.

    Ordered defensively: **protective wings first, shorts last**.  Submitting
    a naked short before its hedge exists spikes the margin requirement and is
    a common cause of broker rejection mid-structure.
    """
    qty = config.qty
    snap = lambda v: round_to_strike(v, config.strike_step)  # noqa: E731
    return [
        Leg(PUT, Side.BUY, snap(level - config.long_offset), qty),
        Leg(CALL, Side.BUY, snap(level + config.long_offset), qty),
        Leg(PUT, Side.SELL, snap(level - config.short_offset), qty),
        Leg(CALL, Side.SELL, snap(level + config.short_offset), qty),
    ]


@dataclass
class Condor:
    """One rung of the ladder."""

    level: float
    entry_time: dt.datetime
    expiry: dt.date
    legs: list[FilledLeg]
    config: StrategyConfig
    status: CondorStatus = CondorStatus.OPEN
    exit_time: dt.datetime | None = None
    exit_reason: str | None = None
    entry_costs: float = 0.0
    exit_costs: float = 0.0
    index: int = 0

    # ------------------------------------------------------------- economics

    @property
    def credit(self) -> float:
        """Net cash received when the structure was opened."""
        return sum(fl.credit for fl in self.legs)

    @property
    def net_credit(self) -> float:
        """Credit after entry transaction costs."""
        return self.credit - self.entry_costs

    @property
    def max_profit(self) -> float:
        return self.net_credit

    @property
    def max_loss(self) -> float:
        """Worst case at expiry.

        Only one wing can finish in the money, so the exposure is one wing's
        width rather than both.

        Exit costs are included once they are known, because otherwise the
        stated worst case is smaller than the worst case ``payoff_at_expiry``
        actually reports -- a risk figure that understates risk, however
        slightly, is the wrong way round.
        """
        return (
            self.config.wing_width * self.config.qty - self.net_credit + self.exit_costs
        )

    @property
    def breakevens(self) -> tuple[float, float]:
        credit_per_share = self.credit / self.config.qty if self.config.qty else 0.0
        return (
            self.level - self.config.short_offset - credit_per_share,
            self.level + self.config.short_offset + credit_per_share,
        )

    @property
    def is_open(self) -> bool:
        return self.status is CondorStatus.OPEN

    @property
    def uses_modeled_prices(self) -> bool:
        return any(fl.source is PriceSource.MODELED for fl in self.legs)

    # ------------------------------------------------------------ valuation

    def value(self, prices: dict[Leg, float]) -> float:
        return sum(fl.value(prices[fl.leg]) for fl in self.legs if fl.leg in prices)

    def mtm(self, prices: dict[Leg, float]) -> float:
        """Unrealised P&L given current per-share prices, net of entry costs."""
        marked = [fl for fl in self.legs if fl.leg in prices]
        if len(marked) < len(self.legs):
            return 0.0
        return sum(fl.pnl(prices[fl.leg]) for fl in marked) - self.entry_costs

    def payoff_at_expiry(self, spot: float) -> float:
        """P&L if the structure is settled at ``spot``, net of all costs."""
        gross = sum(fl.leg.payoff(spot, fl.entry_price) for fl in self.legs)
        return gross - self.entry_costs - self.exit_costs

    def realised_pnl(self) -> float:
        """P&L using recorded exit prices; 0 while still open."""
        if self.is_open:
            return 0.0
        total = 0.0
        for fl in self.legs:
            if fl.exit_price is None:
                return 0.0
            total += fl.pnl(fl.exit_price)
        return total - self.entry_costs - self.exit_costs

    # --------------------------------------------------------------- exits

    def exit_signal(self, prices: dict[Leg, float]) -> str | None:
        """Whether a configured take-profit or stop-loss has triggered."""
        if not self.is_open:
            return None
        tp, sl = self.config.take_profit_pct, self.config.stop_loss_mult
        if tp is None and sl is None:
            return None
        if len(prices) < len(self.legs) or self.credit <= 0:
            return None
        pnl = self.mtm(prices)
        if tp is not None and pnl >= tp * self.credit:
            return f"take-profit: captured {pnl / self.credit:.0%} of credit"
        if sl is not None and pnl <= -sl * self.credit:
            return f"stop-loss: lost {abs(pnl) / self.credit:.1f}x credit"
        return None

    def close(self, when: dt.datetime, reason: str, status: CondorStatus, costs: float = 0.0) -> None:
        self.status = status
        self.exit_time = when
        self.exit_reason = reason
        self.exit_costs = costs


# ------------------------------------------------------------------- netting


@dataclass(frozen=True)
class NetPosition:
    """The broker's actual view of one strike, after offsetting."""

    expiry: dt.date
    right: str
    strike: float
    net_qty: int                    # signed: +long, -short
    gross_long: int = 0
    gross_short: int = 0
    contributors: tuple[int, ...] = field(default_factory=tuple)  # condor indices

    @property
    def is_flat(self) -> bool:
        return self.net_qty == 0


def net_positions(condors: Iterable[Condor], *, open_only: bool = True) -> list[NetPosition]:
    """Collapse every condor's legs into the position the broker really holds.

    This is where the strategy's central claim becomes measurable: the long PE
    at ``L - 400`` of one rung sits on the same strike as the short PE at
    ``L - 200`` of the rung two steps below, so the two cancel and the ladder
    carries far less risk than the gross leg count suggests.
    """
    buckets: dict[tuple[dt.date, str, float], dict] = {}
    for condor in condors:
        if open_only and not condor.is_open:
            continue
        for fl in condor.legs:
            key = (condor.expiry, fl.leg.right, fl.leg.strike)
            slot = buckets.setdefault(key, {"net": 0, "long": 0, "short": 0, "from": []})
            slot["net"] += fl.leg.signed_qty
            if fl.leg.side is Side.BUY:
                slot["long"] += fl.leg.qty
            else:
                slot["short"] += fl.leg.qty
            slot["from"].append(condor.index)

    out = [
        NetPosition(
            expiry=expiry,
            right=right,
            strike=strike,
            net_qty=slot["net"],
            gross_long=slot["long"],
            gross_short=slot["short"],
            contributors=tuple(sorted(set(slot["from"]))),
        )
        for (expiry, right, strike), slot in buckets.items()
    ]
    out.sort(key=lambda p: (p.expiry, p.right, p.strike))
    return out


def netting_summary(condors: Sequence[Condor], *, open_only: bool = True) -> dict[str, float]:
    """Headline numbers for how much the ladder self-hedges.

    ``open_only=False`` describes the campaign as a whole, which is what a
    finished backtest wants: by then every rung has settled, so the live book
    is empty and would report no offsetting at all.
    """
    positions = net_positions(condors, open_only=open_only)
    gross = sum(p.gross_long + p.gross_short for p in positions)
    net = sum(abs(p.net_qty) for p in positions)
    flat = [p for p in positions if p.is_flat]
    return {
        "strikes_touched": len(positions),
        "strikes_fully_offset": len(flat),
        "gross_qty": gross,
        "net_qty": net,
        "offset_qty": gross - net,
        "offset_ratio": (gross - net) / gross if gross else 0.0,
    }


def portfolio_payoff(condors: Iterable[Condor], spots: Sequence[float]) -> list[float]:
    """Combined expiry payoff curve across every condor in the ladder."""
    active = [c for c in condors]
    return [sum(c.payoff_at_expiry(s) for c in active) for s in spots]
