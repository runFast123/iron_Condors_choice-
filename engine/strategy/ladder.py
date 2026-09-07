"""The 100-point decline ladder.

Rules, as specified:

* An opening condor is built at the anchor level.
* Every subsequent ``step`` points of *decline* opens another condor around
  the new level.
* Each level fires **at most once** per campaign.  Rallies do nothing — the
  ladder never unwinds or re-arms on the way back up.

Level arithmetic runs on integer step counts rather than floats, so a long
campaign cannot drift into ``23799.999999`` and miss a trigger.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Literal

from engine.strategy.condor import StrategyConfig

AnchorMode = Literal["floor", "round", "explicit"]

# Guards level arithmetic against float division landing a hair off an exact
# multiple (e.g. spot 23900 -> 238.99999999999997 before rounding).
_EPS = 1e-9


@dataclass(frozen=True)
class LadderTrigger:
    """One firing decision, emitted for the UI and the executor."""

    level: float
    time: dt.datetime
    spot: float
    reason: str  # "anchor" | "decline" | "gap-fill"


@dataclass
class Ladder:
    """Stateful trigger engine shared by the backtester and the live runner.

    Both paths drive the *same* instance type, so a forward test cannot
    silently diverge from the backtest that justified it.
    """

    config: StrategyConfig
    anchor_mode: AnchorMode = "floor"
    explicit_anchor: float | None = None

    anchor: float | None = None
    last_level: float | None = None
    fired_levels: set[int] = field(default_factory=set)
    triggers: list[LadderTrigger] = field(default_factory=list)

    # --------------------------------------------------------------- helpers

    @property
    def step(self) -> float:
        return self.config.step

    def _to_units(self, level: float) -> int:
        """Levels as integer multiples of ``step`` — exact, unlike floats."""
        return int(round(level / self.step))

    def _from_units(self, units: int) -> float:
        return units * self.step

    def _level_of(self, spot: float) -> float:
        """The ladder level at or below ``spot`` — used to place the anchor."""
        return math.floor(spot / self.step + _EPS) * self.step

    def _trigger_level(self, spot: float) -> float:
        """The lowest level ``spot`` has actually reached.

        A level fires once price trades at or below it, so this rounds *up*:
        at 23,899 the ladder has reached 23,900 and no further. Rounding down
        here would fire the 23,800 rung on a one-point breach of 23,900.
        """
        return math.ceil(spot / self.step - _EPS) * self.step

    def _choose_anchor(self, spot: float) -> float:
        if self.anchor_mode == "explicit":
            if self.explicit_anchor is None:
                raise ValueError("anchor_mode='explicit' requires explicit_anchor")
            return float(self.explicit_anchor)
        if self.anchor_mode == "round":
            return round(spot / self.step) * self.step
        # "floor" is the default: it reproduces the specified table exactly for
        # an on-the-level spot, and avoids opening two condors on the first
        # bar when the spot happens to sit just above a level.
        return self._level_of(spot)

    # ------------------------------------------------------------------ core

    @property
    def count(self) -> int:
        return len(self.fired_levels)

    @property
    def next_trigger_level(self) -> float | None:
        """The level that would fire next — shown live in the UI."""
        if self.last_level is None:
            return None
        if self.count >= self.config.max_condors:
            return None
        return self.last_level - self.step

    def distance_to_next(self, spot: float) -> float | None:
        nxt = self.next_trigger_level
        return None if nxt is None else spot - nxt

    def on_price(self, spot: float, when: dt.datetime) -> list[LadderTrigger]:
        """Feed one observation; return the levels that should fire now.

        Returns an empty list on the overwhelming majority of bars.
        """
        if spot is None or not math.isfinite(spot):
            return []

        fired: list[LadderTrigger] = []

        # First observation establishes the anchor and opens the first condor.
        if self.anchor is None:
            self.anchor = self._choose_anchor(spot)
            self.last_level = self.anchor
            if self._fire(self.anchor, when, spot, "anchor", fired):
                return fired
            return fired

        assert self.last_level is not None
        level = self._trigger_level(spot)

        # Down-only: a rally, or a dip that does not clear a full step, is a no-op.
        if level > self.last_level - self.step:
            return []

        if self.config.fill_gaps:
            # A gap-down that skips levels fires each one it passed through, so
            # an overnight drop builds the same ladder a gradual decline would.
            start_units = self._to_units(self.last_level) - 1
            end_units = self._to_units(level)
            for units in range(start_units, end_units - 1, -1):
                reason = "decline" if units == start_units else "gap-fill"
                if not self._fire(self._from_units(units), when, spot, reason, fired):
                    break
        else:
            self._fire(level, when, spot, "decline", fired)

        self.last_level = level
        return fired

    def _fire(self, level: float, when: dt.datetime, spot: float, reason: str, out: list[LadderTrigger]) -> bool:
        """Record a trigger. Returns False when the ladder is capped out."""
        units = self._to_units(level)
        if units in self.fired_levels:
            return True
        if len(self.fired_levels) >= self.config.max_condors:
            return False
        self.fired_levels.add(units)
        trigger = LadderTrigger(level=self._from_units(units), time=when, spot=spot, reason=reason)
        self.triggers.append(trigger)
        out.append(trigger)
        return True

    # ------------------------------------------------------------- inspection

    def levels(self) -> list[float]:
        """Every level fired so far, highest first."""
        return [self._from_units(u) for u in sorted(self.fired_levels, reverse=True)]

    def planned_strikes(self, level: float) -> dict[str, float]:
        """The four strikes a condor at ``level`` would use."""
        cfg = self.config
        return {
            "long_pe": level - cfg.long_offset,
            "short_pe": level - cfg.short_offset,
            "short_ce": level + cfg.short_offset,
            "long_ce": level + cfg.long_offset,
        }

    def reset(self) -> None:
        self.anchor = None
        self.last_level = None
        self.fired_levels.clear()
        self.triggers.clear()


def simulate_levels(
    spots: list[float],
    config: StrategyConfig,
    *,
    anchor_mode: AnchorMode = "floor",
    explicit_anchor: float | None = None,
    start: dt.datetime | None = None,
) -> list[LadderTrigger]:
    """Run the ladder over a bare price series.

    This is pass 1 of the backtest: it costs one spot series and tells us
    exactly which strikes and expiries the campaign will ever touch, so pass 2
    fetches only those option legs instead of the entire chain.
    """
    ladder = Ladder(config=config, anchor_mode=anchor_mode, explicit_anchor=explicit_anchor)
    base = start or dt.datetime(2020, 1, 1)
    for i, spot in enumerate(spots):
        ladder.on_price(spot, base + dt.timedelta(minutes=i))
    return ladder.triggers
