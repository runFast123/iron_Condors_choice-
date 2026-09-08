"""The backtest runner.

Two passes, and the split matters:

**Pass 1** drives the ladder over the spot series alone.  That costs one cheap
series and tells us the exact set of ``(expiry, strike, right)`` the campaign
will ever touch.

**Pass 2** replays the bars, opening condors at the triggers pass 1 found and
pricing each leg through a :class:`PriceProvider`.

Without pass 1 the only safe thing to do would be to fetch the whole option
chain for every expiry in the range — thousands of ChartData calls against an
API with no documented rate limit.  With it, we fetch only the legs the
strategy actually trades.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import pandas as pd

from engine.backtest import metrics as metrics_mod
from engine.backtest.metrics import EquityPoint, Metrics
from engine.backtest.providers import PriceProvider, PriceRequest
from engine.data.expiry_calendar import MAX_WEEKLY_DTE
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
from engine.strategy.ladder import AnchorMode, Ladder, LadderTrigger

log = logging.getLogger(__name__)

ExpiryResolver = Callable[[dt.date], dt.date]


@dataclass
class BacktestParams:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    costs: CostModel = field(default_factory=CostModel)
    anchor_mode: AnchorMode = "floor"
    explicit_anchor: float | None = None
    # NOTE: no `underlying` field. The engine is NIFTY-only today, and a
    # config option that silently does nothing is worse than none at all.
    # Square everything off at this time on expiry day rather than assuming a
    # perfect settlement print.
    settlement_time: dt.time = dt.time(15, 30)
    roll_to_next_expiry: bool = True
    min_dte: int = 1
    label: str = ""


@dataclass
class LegRequirement:
    """One option contract the campaign needs, discovered in pass 1."""

    expiry: dt.date
    strike: float
    right: str
    first_needed: dt.datetime

    def key(self) -> tuple[dt.date, float, str]:
        return (self.expiry, self.strike, self.right)


@dataclass
class BacktestResult:
    params: BacktestParams
    condors: list[Condor] = field(default_factory=list)
    equity: list[EquityPoint] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)
    triggers: list[LadderTrigger] = field(default_factory=list)
    requirements: list[LegRequirement] = field(default_factory=list)
    skipped: list[tuple[dt.datetime, float, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # (when, expiry left behind, expiry rolled into)
    rolls: list[tuple[dt.datetime, dt.date, dt.date]] = field(default_factory=list)

    @property
    def campaigns(self) -> int:
        """How many separate expiry campaigns this run covered."""
        return len(self.rolls) + 1 if self.condors else 0

    @property
    def netting(self) -> dict[str, float]:
        return netting_summary(self.condors, open_only=False)

    def strike_matrix(self) -> list[dict]:
        """Rows for the Strike Ladder Matrix view.

        One row per (expiry, right, strike) with each condor's contribution
        and the net — the view that makes the offsetting thesis visible.
        """
        rows = []
        for position in net_positions(self.condors, open_only=False):
            per_condor: dict[int, int] = {}
            for condor in self.condors:
                for fl in condor.legs:
                    if (
                        condor.expiry == position.expiry
                        and fl.leg.right == position.right
                        and fl.leg.strike == position.strike
                    ):
                        per_condor[condor.index] = per_condor.get(condor.index, 0) + fl.leg.signed_qty
            rows.append(
                {
                    "expiry": position.expiry.isoformat(),
                    "right": position.right,
                    "strike": position.strike,
                    "net_qty": position.net_qty,
                    "gross_long": position.gross_long,
                    "gross_short": position.gross_short,
                    "is_flat": position.is_flat,
                    "by_condor": per_condor,
                }
            )
        return rows


# --------------------------------------------------------------------- pass 1


def discover_requirements(
    spots: Sequence[tuple[dt.datetime, float]],
    params: BacktestParams,
    expiry_for: ExpiryResolver,
) -> tuple[list[LadderTrigger], list[LegRequirement]]:
    """Pass 1: run the ladder on spot alone and collect the legs it will need."""
    ladder = Ladder(
        config=params.strategy,
        anchor_mode=params.anchor_mode,
        explicit_anchor=params.explicit_anchor,
    )
    requirements: dict[tuple[dt.date, float, str], LegRequirement] = {}
    triggers: list[LadderTrigger] = []
    campaign_expiry: dt.date | None = None

    for when, spot in spots:
        try:
            expiry = expiry_for(when.date())
        except ValueError:
            # Past the end of the calendar: nothing can be opened here, so
            # there are no legs to plan for. Pass 2 makes the same decision and
            # records the warning; the two passes must agree or the wrong legs
            # get fetched.
            continue
        # Mirror the roll in pass 2, or this pass discovers the wrong legs.
        if campaign_expiry is None:
            campaign_expiry = expiry
        elif expiry != campaign_expiry:
            if params.roll_to_next_expiry:
                ladder.reset()
            campaign_expiry = expiry

        for trigger in ladder.on_price(spot, when):
            triggers.append(trigger)
            for leg in build_legs(trigger.level, params.strategy):
                requirement = LegRequirement(expiry, leg.strike, leg.right, when)
                requirements.setdefault(requirement.key(), requirement)

    ordered = sorted(requirements.values(), key=lambda r: (r.expiry, r.right, r.strike))
    log.info("Pass 1: %d triggers requiring %d distinct option legs", len(triggers), len(ordered))
    return triggers, ordered


# --------------------------------------------------------------------- pass 2


class Backtest:
    """Replays the campaign bar by bar."""

    def __init__(
        self,
        params: BacktestParams,
        provider: PriceProvider,
        expiry_for: ExpiryResolver,
    ) -> None:
        self.params = params
        self.provider = provider
        self.expiry_for = expiry_for

    # -- pricing helpers ---------------------------------------------------

    def _price_legs(
        self, legs: Iterable[Leg], expiry: dt.date, when: dt.datetime, spot: float
    ) -> dict[Leg, tuple[float, PriceSource]] | None:
        """Price every leg, or return None so the rung is skipped entirely.

        Opening three legs of a four-leg structure would leave a naked short in
        the book, so a partial fill is refused rather than patched.
        """
        out: dict[Leg, tuple[float, PriceSource]] = {}
        for leg in legs:
            quote = self.provider.quote(
                PriceRequest(expiry=expiry, strike=leg.strike, right=leg.right, when=when, spot=spot)
            )
            if quote is None:
                return None
            out[leg] = (quote.price, quote.source)
        return out

    def _mark(self, condor: Condor, when: dt.datetime, spot: float) -> dict[Leg, float]:
        marks: dict[Leg, float] = {}
        for fl in condor.legs:
            quote = self.provider.quote(
                PriceRequest(
                    expiry=condor.expiry, strike=fl.leg.strike, right=fl.leg.right, when=when, spot=spot
                )
            )
            if quote is not None:
                marks[fl.leg] = quote.price
        return marks

    # -- main loop ---------------------------------------------------------

    def run(self, spots: Sequence[tuple[dt.datetime, float]]) -> BacktestResult:
        params = self.params
        result = BacktestResult(params=params)

        if not spots:
            result.warnings.append("No spot data in the requested range; nothing to backtest.")
            return result

        triggers, requirements = discover_requirements(spots, params, self.expiry_for)
        result.triggers = triggers
        result.requirements = requirements
        ladder = Ladder(
            config=params.strategy,
            anchor_mode=params.anchor_mode,
            explicit_anchor=params.explicit_anchor,
        )
        condors: list[Condor] = []
        realised: list[float] = []
        holding_days: list[float] = []
        equity: list[EquityPoint] = []
        cumulative_realised = 0.0
        peak_risk = 0.0
        max_concurrent = 0
        next_index = 0

        campaign_expiry: dt.date | None = None

        # The final bar of each session, so an expiry always settles on its own
        # day even when the series never reaches the settlement time.
        last_bar_of_day: dict[dt.date, dt.datetime] = {}
        for when, _ in spots:
            last_bar_of_day[when.date()] = when

        expiry_exhausted = False

        for when, spot in spots:
            try:
                expiry = self.expiry_for(when.date())
            except ValueError as exc:
                # Past the end of the expiry calendar. Opening is impossible,
                # but the condors already on the book still have to be marked
                # and settled, so this skips entries rather than aborting.
                if not expiry_exhausted:
                    result.warnings.append(
                        f"No expiry available from {when.date()} ({exc}). No further condors "
                        "were opened; those already open were still managed to settlement."
                    )
                    expiry_exhausted = True
                expiry = None

            # 0. Roll into the next expiry.
            #
            # The strategy's whole premise is that overlapping strikes of later
            # condors offset earlier ones -- but that only holds *within one
            # expiry*. A long 23,600 PE expiring in March does not offset a
            # short 23,600 PE expiring in April; they are separate instruments
            # and net to nothing. So a campaign lives inside a single expiry,
            # and when that expiry settles a fresh ladder is anchored at the
            # prevailing spot.
            #
            # Without this the ladder keeps its old reference level forever:
            # after its rungs settle it sits waiting for a level far below the
            # market and simply stops trading, which is an artefact of ignoring
            # expiry rather than anything the strategy asks for.
            if expiry is not None:
                if campaign_expiry is None:
                    campaign_expiry = expiry
                elif expiry != campaign_expiry:
                    if params.roll_to_next_expiry:
                        ladder.reset()
                        result.rolls.append((when, campaign_expiry, expiry))
                    campaign_expiry = expiry

            # 1. Open new rungs.
            for trigger in (ladder.on_price(spot, when) if expiry is not None else ()):
                legs = build_legs(trigger.level, params.strategy)
                priced = self._price_legs(legs, expiry, when, spot)
                if priced is None:
                    result.skipped.append((when, trigger.level, "no price for one or more legs"))
                    continue

                filled = [
                    FilledLeg(leg=leg, entry_price=price, source=source)
                    for leg, (price, source) in priced.items()
                ]
                entry_costs = sum(
                    params.costs.leg_cost(fl.leg.side, fl.entry_price, fl.leg.qty) for fl in filled
                ) + sum(params.costs.slippage(fl.leg.qty) for fl in filled)

                condor = Condor(
                    level=trigger.level,
                    entry_time=when,
                    expiry=expiry,
                    legs=filled,
                    config=params.strategy,
                    entry_costs=entry_costs,
                    index=next_index,
                )
                next_index += 1
                condors.append(condor)

            # 2. Manage open rungs: expiry settlement first, then TP/SL.
            for condor in condors:
                if not condor.is_open:
                    continue
                if self._is_settlement(condor, when, last_bar_of_day):
                    self._settle(condor, when, spot, params)
                    realised.append(condor.realised_pnl())
                    cumulative_realised += condor.realised_pnl()
                    holding_days.append((when - condor.entry_time).total_seconds() / 86_400)
                    continue

                marks = self._mark(condor, when, spot)
                reason = condor.exit_signal(marks)
                if reason is not None:
                    exit_costs = sum(
                        params.costs.leg_cost(_flip(fl.leg.side), marks[fl.leg], fl.leg.qty)
                        for fl in condor.legs
                    )
                    for fl in condor.legs:
                        fl.exit_price = marks[fl.leg]
                    status = (
                        CondorStatus.CLOSED_TARGET if "take-profit" in reason else CondorStatus.CLOSED_STOP
                    )
                    condor.close(when, reason, status, exit_costs)
                    realised.append(condor.realised_pnl())
                    cumulative_realised += condor.realised_pnl()
                    holding_days.append((when - condor.entry_time).total_seconds() / 86_400)

            # 3. Mark the book.
            open_condors = [c for c in condors if c.is_open]
            max_concurrent = max(max_concurrent, len(open_condors))
            peak_risk = max(peak_risk, sum(c.max_loss for c in open_condors))

            unrealised = 0.0
            for condor in open_condors:
                marks = self._mark(condor, when, spot)
                unrealised += condor.mtm(marks)

            equity.append(
                EquityPoint(
                    ts=when,
                    equity=cumulative_realised + unrealised,
                    open_condors=len(open_condors),
                    spot=spot,
                )
            )

        # Anything still open at the end of the range settles at the last spot.
        last_when, last_spot = spots[-1]
        settled_at_end = 0
        for condor in condors:
            if condor.is_open:
                self._settle(condor, last_when, last_spot, params, reason="end of backtest range")
                realised.append(condor.realised_pnl())
                cumulative_realised += condor.realised_pnl()
                holding_days.append((last_when - condor.entry_time).total_seconds() / 86_400)
                settled_at_end += 1

        if settled_at_end and equity:
            # Those settlements happen after the final bar's equity point was
            # recorded, so the curve used to end below the reported net P&L --
            # and drawdown, Sharpe and CAGR were all computed from that
            # truncated curve. The last bar is the settlement bar, so correct
            # it in place rather than adding a point the series never had.
            final = equity[-1]
            final.equity = cumulative_realised
            final.open_condors = 0

        draws = metrics_mod.drawdown_series([p.equity for p in equity])
        for point, draw in zip(equity, draws):
            point.drawdown = draw

        provider_summary = getattr(self.provider, "summary", lambda: {})()
        result.condors = condors
        result.equity = equity
        result.metrics = metrics_mod.compute(
            realised=realised,
            equity=equity,
            total_credit=sum(c.credit for c in condors),
            total_costs=sum(c.entry_costs + c.exit_costs for c in condors),
            capital_at_risk=peak_risk,
            max_concurrent=max_concurrent,
            holding_days=holding_days,
            real_price_fraction=float(provider_summary.get("real_fraction", 0.0)),
            modeled_quotes=int(provider_summary.get("modeled_quotes", 0)),
        )

        if result.metrics.modeled_quotes:
            result.warnings.append(
                f"{100 * (1 - result.metrics.real_price_fraction):.1f}% of quotes were MODELED "
                "(Black-76 from India VIX), not real Choice premiums."
            )
        if result.rolls:
            result.warnings.append(
                f"Rolled through {len(result.rolls) + 1} expiry campaigns; the ladder re-anchors "
                "at each new expiry because offsetting only works within one."
            )
        if result.skipped:
            result.warnings.append(
                f"{len(result.skipped)} ladder trigger(s) were skipped because no price was available."
            )
        return result

    # -- settlement --------------------------------------------------------

    def _is_settlement(
        self, condor: Condor, when: dt.datetime, last_bar_of_day: dict[dt.date, dt.datetime]
    ) -> bool:
        """Whether this bar settles the condor.

        The obvious rule -- "past settlement_time on expiry day" -- silently
        fails whenever the series has no bar that late. Daily candles are
        stamped at the session open, so at 15:30 the condition never fires and
        every condor settles on the *following* day against the following
        day's spot. So the last bar of the expiry day settles it regardless.
        """
        if when.date() < condor.expiry:
            return False
        if when.date() > condor.expiry:
            return True
        return (
            when.time() >= self.params.settlement_time
            or last_bar_of_day.get(when.date()) == when
        )

    @staticmethod
    def _settle(
        condor: Condor, when: dt.datetime, spot: float, params: BacktestParams, reason: str = "expired"
    ) -> None:
        """Settle every leg at intrinsic value.

        Index options cash-settle, so there is no exit brokerage on an expiry
        — only STT on in-the-money shorts, which the cost model applies.
        """
        exit_costs = 0.0
        for fl in condor.legs:
            intrinsic = fl.leg.intrinsic(spot)
            fl.exit_price = intrinsic
            fl.exit_source = PriceSource.CHOICE
            if intrinsic > 0 and fl.leg.side is Side.SELL:
                exit_costs += params.costs.leg_cost(Side.SELL, intrinsic, fl.leg.qty)
        condor.close(when, reason, CondorStatus.EXPIRED, exit_costs)


def _flip(side: Side) -> Side:
    return Side.BUY if side is Side.SELL else Side.SELL


# ------------------------------------------------------------------ helpers


def weekly_expiry_resolver(
    expiries: Sequence[dt.date],
    min_dte: int = 1,
    *,
    max_dte: int | None = MAX_WEEKLY_DTE,
) -> ExpiryResolver:
    """Pick the nearest expiry at least ``min_dte`` days out.

    ``max_dte`` is a correctness guard, not a preference. The scrip master only
    lists contracts that still exist, so asking it for an expiry after a date
    six months ago answers with the nearest *current* one -- and a weekly
    condor priced as a half-year option produces a credit worth 80% of its wing
    width, which is impossible. That failure is silent and ruins a whole run,
    so a resolver that can only offer something absurdly far out says so.
    """
    ordered = sorted(expiries)
    if not ordered:
        raise ValueError("No expiries supplied to the resolver")

    def resolve(day: dt.date) -> dt.date:
        cutoff = day + dt.timedelta(days=min_dte)
        chosen = next((e for e in ordered if e >= cutoff), None)
        if chosen is None:
            # Falling back to the last known expiry hands back a date in the
            # *past*, and a condor opened after its own expiry settles the
            # instant it is created. There is no answer here; say so.
            raise ValueError(
                f"No expiry on/after {cutoff}; the calendar ends at {ordered[-1]}."
            )
        dte = (chosen - day).days
        if max_dte is not None and dte > max_dte:
            raise ValueError(
                f"Nearest available expiry for {day} is {chosen} — {dte} days out, "
                f"beyond the {max_dte}-day limit for a weekly campaign. The expiry "
                "calendar is missing the contracts that were actually listed then; "
                "derive them rather than reading today's scrip master."
            )
        return chosen

    return resolve


def spots_from_frame(frame: pd.DataFrame, column: str = "close") -> list[tuple[dt.datetime, float]]:
    """Turn a candle frame into the (timestamp, spot) series the runner wants."""
    if frame is None or frame.empty:
        return []
    return [
        (row.ts.to_pydatetime() if hasattr(row.ts, "to_pydatetime") else row.ts, float(getattr(row, column)))
        for row in frame.itertuples()
    ]
