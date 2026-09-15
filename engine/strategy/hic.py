"""The Hybrid Iron Condor.

Full condors at the anchor and one step either way; beyond that, one vertical
spread per step, in the direction of the move. The core earns in a quiet month;
the spreads earn only when the move keeps going past them. That makes it close
to the mirror image of the ladder, which is the point of running both.

Levels are counted in steps from the anchor. ``k = 0`` is the anchor, ``k = -1``
is one step below, ``k = +2`` two above.

    |k| <= band     full iron condor, opened for a credit
    k  <  -band     put spread
    k  >  +band     call spread

The trigger is the ladder's, not a second implementation of one. It already
fires both ways, fills gaps, fires each level once and survives a restart, and
a strategy whose entry rule silently diverged from the one the backtest proved
would be the most expensive kind of bug this codebase can have.

Anchor mode is ``nearest``, which is a correction to the strategy document.
Section 7.5's worked example needs ``floor`` to produce the levels it lists, but
sections 3.5 and 9 specify ``nearest``, and ``nearest`` is right: under
``floor`` the spot sits up to 99 points above the anchor, so at 23,499 the
first rung up is one point away and the first rung down is 199. A structure
that is symmetric by construction cannot be anchored asymmetrically. The
example's shape survives -- a gap still opens a core condor and then spreads,
in order -- but its levels shift one step.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from engine.strategy.condor import (
    CALL,
    PUT,
    Leg,
    Side,
    StrategyConfig,
    UnitKind,
    build_legs,
    round_to_strike,
)

#: What a level opens.
CORE = "core"
PUT_SPREAD = "put_spread"
CALL_SPREAD = "call_spread"


@dataclass(frozen=True)
class HicConfig(StrategyConfig):
    """The ladder's geometry, plus the band and the spread settings.

    A subclass so it passes anywhere a StrategyConfig is expected -- the
    trigger, the leg builder, the cost model and the persistence round-trip all
    take one and none of them need to know which they have.
    """

    #: Steps either side of the anchor that open a full condor. 0 makes only
    #: the anchor a condor and everything beyond it a spread.
    full_band_steps: int = 1
    #: "buy" is the strategy: a debit spread past the band. "sell" is the
    #: opposite reading, kept only so a backtest can price the comparison.
    half_mode: str = "buy"
    #: 0 takes the condor's own strikes at that level and reverses them. 200
    #: buys at the level itself, which costs more and starts paying sooner --
    #: the variant section 3.4 raises and leaves to the backtest.
    debit_shift: float = 0.0
    max_put_spreads: int = 10
    max_call_spreads: int = 10

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.full_band_steps < 0:
            raise ValueError("full_band_steps cannot be negative")
        if self.half_mode not in ("buy", "sell"):
            raise ValueError(f"half_mode must be buy or sell (got {self.half_mode!r})")
        if self.max_put_spreads < 0 or self.max_call_spreads < 0:
            raise ValueError("spread caps cannot be negative")
        if self.debit_shift and self.half_mode == "sell":
            # The document's own appendix ignores the shift on this branch.
            # Ignoring it silently is how H3 and H4 end up conflated in a
            # results table, each appearing to say something about the other.
            raise ValueError("debit_shift applies to bought spreads only")

    @property
    def effective_anchor_mode(self) -> str:
        """Always nearest. See the module docstring for why."""
        return self.anchor_mode or "nearest"

    @property
    def core_units(self) -> int:
        """How many levels open a full condor, if the market reaches them all."""
        return 2 * self.full_band_steps + 1

    @property
    def max_down_levels(self) -> int:
        """Rungs below the anchor worth firing: the band, then the spreads."""
        return self.full_band_steps + self.max_put_spreads

    @property
    def max_up_levels(self) -> int:
        return self.full_band_steps + self.max_call_spreads


def unit_kind_at(k: int, config: HicConfig) -> str:
    """Whether level `k` opens a condor, a put spread or a call spread."""
    if abs(k) <= config.full_band_steps:
        return CORE
    return PUT_SPREAD if k < 0 else CALL_SPREAD


def steps_from_anchor(level: float, anchor: float, step: float) -> int:
    """`k` for a level, as an exact integer.

    Rounded rather than divided, for the reason the ladder counts in integer
    units at all: 23,799.999999 is 23,800, and a campaign that drifts loses a
    rung without saying so.
    """
    return int(round((level - anchor) / step))


def structure_kind(k: int, config: HicConfig) -> UnitKind:
    """The persisted name for what level `k` builds."""
    kind = unit_kind_at(k, config)
    if kind == CORE:
        return UnitKind.CONDOR
    debit = config.half_mode == "buy"
    if kind == PUT_SPREAD:
        return UnitKind.PUT_DEBIT_SPREAD if debit else UnitKind.PUT_CREDIT_SPREAD
    return UnitKind.CALL_DEBIT_SPREAD if debit else UnitKind.CALL_CREDIT_SPREAD


def build_hic_legs(level: float, k: int, config: HicConfig) -> list[Leg]:
    """The legs level `k` opens, bought leg first.

    Buy-first is not an execution detail here -- nothing is executed -- but it
    is how the condor has always been built, and keeping one order means a
    fill log reads the same whichever strategy wrote it.
    """
    kind = unit_kind_at(k, config)
    if kind == CORE:
        # Delegated, so the core condor is the same structure the ladder
        # opens, built by the same code, rather than a second description of
        # it that can drift.
        return build_legs(level, config)

    qty = config.qty
    short_offset, long_offset = config.short_offset, config.long_offset
    shift = config.debit_shift

    def snap(value: float) -> float:
        return round_to_strike(value, config.strike_step)

    if config.half_mode == "sell":
        # The condor's own side at this level, unchanged: sold near, bought far.
        if kind == PUT_SPREAD:
            legs = [
                Leg(PUT, Side.BUY, snap(level - long_offset), qty),
                Leg(PUT, Side.SELL, snap(level - short_offset), qty),
            ]
        else:
            legs = [
                Leg(CALL, Side.BUY, snap(level + long_offset), qty),
                Leg(CALL, Side.SELL, snap(level + short_offset), qty),
            ]
    elif kind == PUT_SPREAD:
        # The same strikes, reversed: bought near, sold far. Shifted upward as
        # a whole when debit_shift is set.
        legs = [
            Leg(PUT, Side.BUY, snap(level - short_offset + shift), qty),
            Leg(PUT, Side.SELL, snap(level - long_offset + shift), qty),
        ]
    else:
        legs = [
            Leg(CALL, Side.BUY, snap(level + short_offset - shift), qty),
            Leg(CALL, Side.SELL, snap(level + long_offset - shift), qty),
        ]

    if legs[0].strike == legs[1].strike:
        # strike_step swallowed the distance between them, leaving a structure
        # that can neither win nor lose. Refused rather than opened at zero
        # width, which would be a position the P&L could never explain.
        raise ValueError(
            f"a {kind} at {level:,.0f} collapses to one strike on a "
            f"{config.strike_step:,.0f}-point grid"
        )
    # Stable, so the two rows above keep the order they were written in. Here
    # as an executable statement of the rule rather than as a transformation.
    return sorted(legs, key=lambda leg: leg.side is not Side.BUY)


def choose_anchor(spot: float, config: HicConfig) -> float:
    """Where the campaign centres. Half-up to the nearest step."""
    return math.floor(spot / config.step + 0.5 + 1e-9) * config.step
