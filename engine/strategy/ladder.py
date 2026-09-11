"""The 100-point decline ladder.

Rules, as specified:

* An opening condor is built at the anchor level.
* Every subsequent ``step`` points of *decline* opens another condor around
  the new level.
* Each level fires **at most once** per campaign.
* Down-only by default: rallies do nothing, and the ladder never unwinds or
  re-arms on the way back up. Set ``direction`` to ``up`` or ``both`` and a
  rally opens condors the same way a decline does, with its own bound and its
  own cap. The two sides never fire on the same observation.

Level arithmetic runs on integer step counts rather than floats, so a long
campaign cannot drift into ``23799.999999`` and miss a trigger.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from engine.strategy.condor import ANCHOR_MODES, AnchorMode, StrategyConfig

__all__ = ["ANCHOR_MODES", "AnchorMode", "Ladder", "LadderTrigger", "simulate_levels"]

# Guards level arithmetic against float division landing a hair off an exact
# multiple (e.g. spot 23900 -> 238.99999999999997 before rounding).
_EPS = 1e-9


@dataclass(frozen=True)
class LadderTrigger:
    """One firing decision, emitted for the UI and the executor."""

    level: float
    time: dt.datetime
    spot: float
    reason: str  # "anchor" | "decline" | "rally" | "gap-fill"
    # Which way the ladder was going. "gap-fill" happens on both sides, so the
    # reason alone cannot tell you, and P&L is attributed by this.
    side: str = "down"  # "anchor" | "down" | "up"


@dataclass
class Ladder:
    """Stateful trigger engine shared by the backtester and the live runner.

    Both paths drive the *same* instance type, so a forward test cannot
    silently diverge from the backtest that justified it.
    """

    config: StrategyConfig
    # None defers to the config, which is the part that survives a restart.
    # The backtester still sets this per run to compare anchor modes.
    anchor_mode: AnchorMode | None = None
    explicit_anchor: float | None = None

    anchor: float | None = None
    # The two bounds. `last_level` keeps its name and its meaning -- the lowest
    # level reached -- because it is a persisted key, and renaming a persisted
    # key to read better is how a live run comes back wrong.
    last_level: float | None = None
    high_level: float | None = None
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

    def _up_trigger_level(self, spot: float) -> float:
        """The highest level ``spot`` has actually reached.

        The mirror of ``_trigger_level``, and it must round the **other** way.
        An up rung fires once price trades at or above it, so this rounds
        down: at 23,401 the ladder has reached 23,400 and no further. Copying
        the down path's ceil here would fire the 23,500 rung on a one-point
        tick above 23,400 -- and, with gap-fill, every rung above it too.

        The epsilon flips sign with the rounding. Both forms mean the same
        thing: an exact multiple is that level, not the next one along.
        """
        return math.floor(spot / self.step + _EPS) * self.step

    def _trigger_level(self, spot: float) -> float:
        """The lowest level ``spot`` has actually reached.

        A level fires once price trades at or below it, so this rounds *up*:
        at 23,899 the ladder has reached 23,900 and no further. Rounding down
        here would fire the 23,800 rung on a one-point breach of 23,900.
        """
        return math.ceil(spot / self.step - _EPS) * self.step

    def _choose_anchor(self, spot: float) -> float:
        # The ladder's own setting wins when given -- the backtester sets it
        # per run -- otherwise the config decides, which is what makes the
        # choice survive a restart.
        mode = self.anchor_mode or self.config.effective_anchor_mode
        if mode == "explicit":
            if self.explicit_anchor is None:
                raise ValueError("anchor_mode='explicit' requires explicit_anchor")
            return float(self.explicit_anchor)
        if mode == "round":
            return round(spot / self.step) * self.step
        if mode == "nearest":
            # Plain half-up, unlike "round" above. Deterministic, which is what
            # a two-way ladder needs: with banker's rounding a spot exactly
            # half a step above a level anchors differently depending on
            # whether that level is odd or even.
            return math.floor(spot / self.step + 0.5 + _EPS) * self.step
        # "floor" is the default: it reproduces the specified table exactly for
        # an on-the-level spot, and avoids opening two condors on the first
        # bar when the spot happens to sit just above a level.
        return self._level_of(spot)

    # ------------------------------------------------------------------ core

    @property
    def count(self) -> int:
        return len(self.fired_levels)

    @property
    def next_down_level(self) -> float | None:
        """The next rung below, or None once a cap forbids it."""
        if self.last_level is None or self.config.direction == "up":
            return None
        if self.count >= self.config.max_condors:
            return None
        if self.config.max_down is not None and self.down_count >= self.config.max_down:
            return None
        return self.last_level - self.step

    @property
    def next_up_level(self) -> float | None:
        if self.high_level is None or self.config.direction == "down":
            return None
        if self.count >= self.config.max_condors:
            return None
        if self.config.max_up is not None and self.up_count >= self.config.max_up:
            return None
        return self.high_level + self.step

    @property
    def next_trigger_level(self) -> float | None:
        """The level that would fire next — shown live in the UI.

        Keeps meaning the *down* rung whenever there is one, so every existing
        surface reading this shows exactly what it showed before. Only an
        up-only ladder reports the up rung here.
        """
        down = self.next_down_level
        return down if down is not None else self.next_up_level

    def distance_to_next(self, spot: float) -> float | None:
        """How far the spot is above the next down rung. Unchanged."""
        nxt = self.next_down_level if self.config.direction != "up" else None
        if nxt is None:
            nxt = self.next_trigger_level
        return None if nxt is None else spot - nxt

    def distance_to_next_up(self, spot: float) -> float | None:
        """How far the spot is below the next up rung."""
        nxt = self.next_up_level
        return None if nxt is None else nxt - spot

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
            self.high_level = self.anchor
            self._fire(self.anchor, when, spot, "anchor", fired, side="anchor")
            return fired

        assert self.last_level is not None
        # State written before the up side existed carries no high bound. The
        # anchor is the right answer: nothing above it has ever fired.
        if self.high_level is None:
            self.high_level = self.anchor

        direction = self.config.direction
        if direction in ("down", "both"):
            fired += self._scan_down(spot, when)
        if direction in ("up", "both"):
            fired += self._scan_up(spot, when)
        return fired

    def _scan_down(self, spot: float, when: dt.datetime) -> list[LadderTrigger]:
        """Unchanged from the down-only ladder, byte for byte in behaviour."""
        assert self.last_level is not None
        fired: list[LadderTrigger] = []
        level = self._trigger_level(spot)

        # A rally, or a dip that does not clear a full step, is a no-op.
        if level > self.last_level - self.step:
            return []

        if self.config.fill_gaps:
            # A gap-down that skips levels fires each one it passed through, so
            # an overnight drop builds the same ladder a gradual decline would.
            start_units = self._to_units(self.last_level) - 1
            end_units = self._to_units(level)
            for units in range(start_units, end_units - 1, -1):
                reason = "decline" if units == start_units else "gap-fill"
                if not self._fire(self._from_units(units), when, spot, reason, fired, side="down"):
                    break
        else:
            self._fire(level, when, spot, "decline", fired, side="down")

        # Set after the loop, including when a cap stopped it. That is what
        # stops a capped rung firing later if the cap is ever raised, and it
        # is existing behaviour that both sides must share.
        self.last_level = level
        return fired

    def _scan_up(self, spot: float, when: dt.datetime) -> list[LadderTrigger]:
        """The mirror of `_scan_down`. See `_up_trigger_level` for the rounding."""
        assert self.high_level is not None
        fired: list[LadderTrigger] = []
        level = self._up_trigger_level(spot)

        if level < self.high_level + self.step:
            return []

        if self.config.fill_gaps:
            start_units = self._to_units(self.high_level) + 1
            end_units = self._to_units(level)
            for units in range(start_units, end_units + 1):
                reason = "rally" if units == start_units else "gap-fill"
                if not self._fire(self._from_units(units), when, spot, reason, fired, side="up"):
                    break
        else:
            self._fire(level, when, spot, "rally", fired, side="up")

        self.high_level = level
        return fired

    def _fire(
        self,
        level: float,
        when: dt.datetime,
        spot: float,
        reason: str,
        out: list[LadderTrigger],
        *,
        side: str = "down",
    ) -> bool:
        """Record a trigger. Returns False when a cap stops it.

        False also stops the gap-fill loop that called it, which is correct
        because every rung inside one scan is on the same side of the anchor.
        """
        units = self._to_units(level)
        if units in self.fired_levels:
            return True
        if len(self.fired_levels) >= self.config.max_condors:
            return False
        if side == "down" and self.config.max_down is not None:
            if self.down_count >= self.config.max_down:
                return False
        if side == "up" and self.config.max_up is not None:
            if self.up_count >= self.config.max_up:
                return False
        self.fired_levels.add(units)
        trigger = LadderTrigger(
            level=self._from_units(units), time=when, spot=spot, reason=reason, side=side
        )
        self.triggers.append(trigger)
        out.append(trigger)
        return True

    # Counts are derived from `fired_levels`, never stored. A stored counter
    # would be a second source of truth that drifts on the first partial save,
    # and it would need a new persisted key; this needs none and cannot
    # disagree with the set it counts.
    @property
    def _anchor_units(self) -> int | None:
        return None if self.anchor is None else self._to_units(self.anchor)

    @property
    def down_count(self) -> int:
        """Rungs fired below the anchor. The anchor itself counts as neither."""
        at = self._anchor_units
        return 0 if at is None else sum(1 for u in self.fired_levels if u < at)

    @property
    def up_count(self) -> int:
        at = self._anchor_units
        return 0 if at is None else sum(1 for u in self.fired_levels if u > at)

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
        """Start a fresh campaign. Called on every expiry roll.

        `high_level` has to go with the rest: a stale up bound left over from
        last month would suppress the whole up side of the new campaign.
        """
        self.anchor = None
        self.last_level = None
        self.high_level = None
        self.fired_levels.clear()
        self.triggers.clear()

    # --------------------------------------------------------- persistence

    def dump_state(self) -> dict:
        """What a resumed run needs to not re-fire what it already holds.

        Owned here rather than spelled out in the forward runner, so a new
        field is added in one place instead of two -- the split is why
        `anchor_mode` went unpersisted for as long as it did.
        """
        return {
            "anchor": self.anchor,
            "last_level": self.last_level,
            "high_level": self.high_level,
            "fired_levels": sorted(self.fired_levels),
        }

    def load_state(self, raw: dict) -> None:
        self.anchor = raw.get("anchor")
        self.last_level = raw.get("last_level")
        self.high_level = raw.get("high_level")
        if self.high_level is None:
            # Written before the up side existed: nothing above the anchor has
            # fired, so the anchor is the bound.
            self.high_level = self.anchor
        self.fired_levels = set(raw.get("fired_levels") or [])


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
