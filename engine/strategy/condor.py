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
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar, Iterable, Literal, Sequence

CALL, PUT = "CE", "PE"

Direction = Literal["down", "up", "both"]

# How the opening level is chosen.
#
# `round` and `nearest` are not the same thing and both are kept. `round` uses
# Python's round(), which is banker's rounding: 23,450 goes down to 23,400 but
# 23,550 goes up to 23,600, so a spot exactly half a step above a level anchors
# differently depending on where it sits on the grid. That is odd but harmless
# for a down-only ladder and is pinned by an existing test. `nearest` is plain
# half-up and is the sane default for a two-way ladder.
AnchorMode = Literal["floor", "round", "nearest", "explicit"]
ANCHOR_MODES = ("floor", "round", "nearest", "explicit")


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
    """Where a price came from.

    Choice is the source. BACKUP is a real traded price from the backtest's
    backup source, used only where Choice has no history for a contract --
    never on a live run. MODELED is not a source at all but Black-76.
    """

    CHOICE = "choice"       # a real Choice FinX candle or quote
    BACKUP = "backup"       # a real candle from the backtest's backup source
    MODELED = "modeled"     # Black-76, from India VIX


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
    max_condors: int = 20          # the total cap, either side of the anchor
    fill_gaps: bool = True         # a gap fires every level it skipped

    # Which way the ladder ladders.
    #
    # "down" is the original strategy and stays the default, so every existing
    # run and every stored config reproduces exactly what it did before. In
    # "up" or "both" a rally opens condors the same way a decline does.
    direction: Direction = "down"
    # None means "whatever suits the direction" -- see effective_anchor_mode.
    # Stored here rather than on the Ladder because this dataclass is the part
    # that survives a restart; the forward runner was hard-wired to "floor"
    # simply because nothing persisted the choice.
    anchor_mode: AnchorMode | None = None
    # Per-side caps, on top of max_condors. None means only the total binds.
    # There is deliberately no `max_total`: max_condors already is the total,
    # and two names for one cap is where the next "which one wins" bug lives.
    max_down: int | None = None
    max_up: int | None = None

    # Exits. Hold-to-expiry is the default; either overlay may be disabled.
    take_profit_pct: float | None = None   # e.g. 0.50 -> close at 50% of credit
    stop_loss_mult: float | None = None    # e.g. 2.0  -> close at 2x credit lost

    # Entry filters, the ladder's only. Both off by default, so every existing
    # run and result is unchanged. A refused rung still counts as fired: the
    # ladder moves on rather than reopening the level later at a stale price.
    #
    # Skip a condor with fewer than this many calendar days to expiry. Late
    # in the cycle the premium has mostly gone and gamma is at its highest.
    min_entry_dte: int | None = None
    # Skip a condor collecting less than this fraction of its wing, gross of
    # costs. 0.5 is "max loss must not exceed the credit".
    min_credit_ratio: float | None = None

    # No new positions while India VIX is above this; they resume once it is
    # back below. Unlike the two filters above it applies to HIC as well: it
    # says when the strategy trades at all, not which rung is worth opening.
    #
    # None here, so a run saved before the rule existed resumes without it --
    # a rule appearing mid-campaign would make it a different strategy from
    # the one it started as. New runs and backtests get 15 from the API.
    max_entry_vix: float | None = None

    #: HIC's core condors are part of its band structure and are never
    #: filtered; HicConfig turns this off.
    entry_filters_apply: ClassVar[bool] = True

    def __post_init__(self) -> None:
        if self.step <= 0:
            raise ValueError("step must be positive")
        if self.long_offset <= self.short_offset:
            raise ValueError("long_offset must exceed short_offset (the wing needs width)")
        if self.lots < 1:
            raise ValueError("lots must be at least 1")
        if self.direction not in ("down", "up", "both"):
            raise ValueError(f"direction must be down, up or both (got {self.direction!r})")
        if self.anchor_mode is not None and self.anchor_mode not in ANCHOR_MODES:
            raise ValueError(f"anchor_mode must be one of {ANCHOR_MODES} (got {self.anchor_mode!r})")
        for name in ("max_down", "max_up"):
            cap = getattr(self, name)
            if cap is not None and cap < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.min_entry_dte is not None and self.min_entry_dte < 0:
            raise ValueError("min_entry_dte cannot be negative")
        if self.min_credit_ratio is not None and not 0 < self.min_credit_ratio < 1:
            raise ValueError("min_credit_ratio must be between 0 and 1")
        if self.max_entry_vix is not None and not 0 < self.max_entry_vix <= 100:
            raise ValueError("max_entry_vix must be above 0 and at most 100")

    @property
    def effective_anchor_mode(self) -> AnchorMode:
        """Where to put the anchor, given the direction.

        `floor` for a down-only ladder, which is what it has always used and
        what keeps its results identical. For a two-way ladder floor is
        lopsided: the spot sits 0-99 points above the anchor, so the first up
        rung is ~50 points away on average while the first down rung is ~150.
        """
        if self.anchor_mode is not None:
            return self.anchor_mode
        return "floor" if self.direction == "down" else "nearest"

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


class UnitKind(str, Enum):
    """What shape a position is, and therefore how its risk is computed.

    A discriminator rather than a subclass check, because it is what gets
    persisted and what the dashboard renders a badge from.
    """

    CONDOR = "condor"
    PUT_DEBIT_SPREAD = "put_debit_spread"
    CALL_DEBIT_SPREAD = "call_debit_spread"
    PUT_CREDIT_SPREAD = "put_credit_spread"
    CALL_CREDIT_SPREAD = "call_credit_spread"

    @property
    def is_vertical(self) -> bool:
        return self is not UnitKind.CONDOR


@dataclass
class PositionUnit:
    """One multi-leg position, whatever its shape.

    Everything here works off `self.legs` and holds for two legs as well as
    four: cash in, mark to market, payoff at expiry, what it realised. The
    parts that depend on the *shape* of the structure -- how wide the risk is,
    where it breaks even -- belong to the subclass, because there is no honest
    way to answer them without knowing what was built.

    The split exists because the condor's risk formulas are the credit-spread
    ones. Applied to a bought vertical they reported four times its true worst
    case and a negative best case. Rather than put a branch inside maths that
    is in live use behind a parity gate, the condor keeps its formulas exactly
    as they were and the other shapes bring their own.
    """

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
    side: str = "down"  # "anchor" | "down" | "up"

    #: What shape this is. Subclasses pin it; it is persisted and rendered.
    kind: UnitKind = UnitKind.CONDOR
    #: Steps from the anchor, where the strategy counts in steps. None for a
    #: ladder rung, which is identified by its level rather than its distance.
    k: int | None = None

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
    def risk_reference(self) -> float:
        """The figure take-profit and stop-loss are measured against.

        A credit structure is measured against what it took in. A bought one
        has no credit to measure against, so it uses what it paid. Abstract
        rather than hard-coded to `credit`, because the old gate was
        `credit <= 0: return None` -- which did not disable exits for a debit
        spread so much as silently pretend it had none.
        """
        return self.credit

    @property
    def risk_reference_noun(self) -> str:
        """What `risk_reference` is, for the exit message to name."""
        return "credit"

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
        # Against `risk_reference`, not `credit`. They are the same number for
        # a condor, but `credit` is *negative* on a bought spread, which flips
        # both comparisons: `pnl >= tp * credit` compares against a negative
        # threshold and is true at a loss, so a debit spread announced a
        # take-profit on its first mark and closed for whatever it was down.
        ref = self.risk_reference
        if len(prices) < len(self.legs) or ref <= 0:
            return None
        pnl = self.mtm(prices)
        noun = self.risk_reference_noun
        if tp is not None and pnl >= tp * ref:
            return f"take-profit: captured {pnl / ref:.0%} of {noun}"
        if sl is not None and pnl <= -sl * ref:
            return f"stop-loss: lost {abs(pnl) / ref:.1f}x {noun}"
        return None

    def close(self, when: dt.datetime, reason: str, status: CondorStatus, costs: float = 0.0) -> None:
        self.status = status
        self.exit_time = when
        self.exit_reason = reason
        self.exit_costs = costs


def vix_allows_entries(vix: float | None, limit: float | None, paused: bool) -> bool:
    """The VIX rule: no new positions while India VIX is above `limit`.

    Above the limit pauses, below it resumes, and a reading exactly on the
    line changes nothing -- it is neither above nor below, so a paused ladder
    stays paused and a trading one keeps trading. That is the rule as stated,
    and it is why the previous state is an argument.

    With no reading to go on, entries pause. The rule exists to keep the
    strategy out of volatile markets, and not knowing is not evidence of calm.

    One function for the backtest's two passes and the forward runner, so all
    three pause and resume on exactly the same readings.
    """
    if limit is None:
        return True
    if vix is None or not math.isfinite(vix) or vix <= 0:
        return False
    if vix > limit:
        return False
    if vix < limit:
        return True
    return not paused


def entry_refusal(unit: "PositionUnit", entry_date: dt.date, config: StrategyConfig) -> str | None:
    """Why a ladder condor should not be opened, or None to open it.

    One function for the backtest and the forward runner, so the two cannot
    come to disagree about which rungs a filter removes -- the whole point of
    backtesting a filter is that the live run then does the same thing.
    """
    if not config.entry_filters_apply or unit.kind is not UnitKind.CONDOR:
        return None
    if config.min_entry_dte is not None:
        dte = (unit.expiry - entry_date).days
        if dte < config.min_entry_dte:
            return f"entry filter: {dte} days to expiry, below the minimum of {config.min_entry_dte}"
    if config.min_credit_ratio is not None:
        width = unit.wing_width * unit.config.qty
        ratio = unit.credit / width if width > 0 else 0.0
        if ratio < config.min_credit_ratio:
            return (
                f"entry filter: credit is {ratio:.0%} of the wing, "
                f"below the minimum of {config.min_credit_ratio:.0%}"
            )
    return None


@dataclass
class Condor(PositionUnit):
    """One rung of the ladder: four legs, opened for a net credit.

    Everything below is the code that was here before the base class existed,
    unchanged. That is deliberate. These four members are what the parity gate
    protects, and the cheapest way to guarantee a refactor did not move them is
    for the refactor not to touch them.
    """

    kind: UnitKind = UnitKind.CONDOR

    @property
    def wing_width(self) -> float:
        """The widest wing, measured from the strikes actually traded.

        Not ``config.wing_width``. ``build_legs`` snaps every strike to the
        listed grid, so the nominal offsets only describe the real structure
        when both are exact multiples of ``strike_step``. With short 225 and
        long 400 on a 50-point grid the strikes land 200 apart while the config
        says 175 -- and every risk figure derived from it understates the loss
        by 25 points a lot.
        """
        widest = 0.0
        for right in (PUT, CALL):
            strikes = sorted(fl.leg.strike for fl in self.legs if fl.leg.right == right)
            if len(strikes) >= 2:
                widest = max(widest, strikes[-1] - strikes[0])
        return widest or self.config.wing_width

    @property
    def max_profit(self) -> float:
        """Best case at expiry, net of every cost the structure will incur."""
        return self.net_credit - self.exit_costs

    @property
    def max_loss(self) -> float:
        """Worst case at expiry.

        Only one wing can finish in the money, so the exposure is one wing's
        width rather than both.

        Exit costs are included once they are known, and they are subtracted
        from ``max_profit`` for the same reason: a risk figure that understates
        risk, or a profit figure that overstates it, is the wrong way round.
        """
        return self.wing_width * self.config.qty - self.net_credit + self.exit_costs

    @property
    def breakevens(self) -> tuple[float, ...]:
        """Where the structure breaks even at expiry, costs included.

        Measured from the strikes actually sold and from the *net* credit.
        Using the nominal level and the gross credit put both points 13-14
        points too far out on a typical condor -- reporting the position as
        safer than it is, which is the one direction this must never err.
        """
        qty = self.config.qty
        if not qty:
            return (self.level, self.level)
        net_per_share = (self.net_credit - self.exit_costs) / qty
        short_put = max(
            (fl.leg.strike for fl in self.legs if fl.leg.right == PUT and fl.leg.side is Side.SELL),
            default=self.level - self.config.short_offset,
        )
        short_call = min(
            (fl.leg.strike for fl in self.legs if fl.leg.right == CALL and fl.leg.side is Side.SELL),
            default=self.level + self.config.short_offset,
        )
        return (short_put - net_per_share, short_call + net_per_share)

    def __post_init__(self) -> None:
        """Refuse anything that is not an iron condor.

        This is the cheapest guard against the whole class of bug that prompted
        the split: a two-leg vertical built as a Condor reported four times its
        true risk and a negative best case, and nothing anywhere said so. A
        wrong number is expensive to notice; a refused construction is not.

        Order-independent, so a position restored from saved state passes.
        """
        if not self.legs:
            return                      # a bare shell, built by restore paths
        if len(self.legs) != 4:
            raise ValueError(f"a condor has four legs, got {len(self.legs)}")
        for right in (PUT, CALL):
            sides = [fl.leg.side for fl in self.legs if fl.leg.right == right]
            if len(sides) != 2 or set(sides) != {Side.BUY, Side.SELL}:
                raise ValueError(
                    f"a condor needs one bought and one sold {right}, got {len(sides)} legs"
                )


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
