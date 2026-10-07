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
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Mapping, Sequence

import pandas as pd

from engine.backtest import metrics as metrics_mod
from engine.backtest.metrics import EquityPoint, Metrics
from engine.backtest.providers import PriceProvider, PriceRequest
from engine.data.expiry_calendar import MAX_WEEKLY_DTE
from engine.forward.fills import FillModel
from engine.pricing.costs import CostModel
from engine.strategy.condor import (
    Condor,
    CondorStatus,
    FilledLeg,
    Leg,
    PriceSource,
    Side,
    StrategyConfig,
    UnitKind,
    build_legs,
    entry_refusal,
    net_positions,
    netting_summary,
    vix_allows_entries,
)
from engine.strategy.hic import HicConfig, build_hic_legs, steps_from_anchor, structure_kind
from engine.strategy.ladder import AnchorMode, Ladder, LadderTrigger
from engine.strategy.vertical import VerticalSpread

log = logging.getLogger(__name__)

ExpiryResolver = Callable[[dt.date], dt.date]


@dataclass
class BacktestParams:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    costs: CostModel = field(default_factory=CostModel)
    # None defers to the strategy config, which picks per direction: floor for
    # a down-only ladder (identical results to before), nearest for two-way.
    # Set it to compare anchor modes on the same path.
    anchor_mode: AnchorMode | None = None
    explicit_anchor: float | None = None
    # NOTE: no `underlying` field. The engine is NIFTY-only today, and a
    # config option that silently does nothing is worse than none at all.
    # Square everything off at this time on expiry day rather than assuming a
    # perfect settlement print.
    settlement_time: dt.time = dt.time(15, 30)
    roll_to_next_expiry: bool = True
    min_dte: int = 1
    label: str = ""
    # The lot size of the contract expiring on a date. None: the strategy's
    # own `lot_size` for every trade. A run over years must not size a 2022
    # trade at today's lot (see engine/data/lot_sizes.py).
    lot_size_for: Callable[[dt.date], int] | None = None
    # How a traded price becomes a fill: half the bid-ask spread against the
    # trader on every leg opened or closed early (not at expiry, which settles
    # in cash). None fills at the traded price itself -- frictionless, for
    # isolating strategy behaviour. A backtest job always sets it, to the
    # same model the live run uses.
    fill_model: FillModel | None = None
    # One campaign on one expiry, replayed: on its expiry day nothing more
    # opens (as everywhere) and that is the end of it, not a missing expiry.
    single_campaign: bool = False
    # The live run's daily loss limit: once a day's P&L -- the book against
    daily_loss_limit: float | None = None
    # Monthly Campaign stop loss: once an expiry campaign's cumulative P&L
    # (realised + unrealised MTM) drops to minus this limit, all open positions
    # for the campaign are squared off immediately and no further condors are
    # opened for the remainder of this monthly expiry.
    campaign_stop_loss: float | None = None
    campaign_trailing_sl_pct: float | None = None
    campaign_trailing_sl: float | None = None
    campaign_trailing_sl_trigger: float | None = None


def _kind_for(level: float, ladder: Ladder, config) -> tuple[UnitKind, int | None]:
    """What this strategy opens at `level`, and how far from the anchor.

    The ladder opens a condor everywhere. HIC opens one near the anchor and a
    spread beyond it, so it has to know the distance first.
    """
    if not isinstance(config, HicConfig) or ladder.anchor is None:
        return UnitKind.CONDOR, None
    k = steps_from_anchor(level, ladder.anchor, config.step)
    return structure_kind(k, config), k


def _legs_for(level: float, ladder: Ladder, config):
    """The legs to price at `level`, for whichever strategy is running.

    Pass one has to ask for the strikes the strategy will actually touch. For
    HIC that is a different set beyond the band, and fetching the condor's
    would leave every spread unpriceable.
    """
    kind, k = _kind_for(level, ladder, config)
    if k is None:
        return build_legs(level, config)
    return build_hic_legs(level, k, config)


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
    # How the VIX rule shaped the run. Empty when the strategy has no limit.
    vix_gate: dict = field(default_factory=dict)

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


def _vix_limit(
    params: BacktestParams,
    spots: Sequence[tuple[dt.datetime, float]],
    vix: Sequence[float | None] | None,
) -> float | None:
    """The run's VIX limit, once it is certain there is a reading per bar.

    Refused rather than run without one. A strategy that pauses on VIX,
    replayed with no VIX, would either never trade (no reading pauses) or
    trade as if the rule did not exist -- and both would look like a result.
    """
    limit = params.strategy.max_entry_vix
    if limit is None:
        return None
    if vix is None:
        raise ValueError(
            f"This strategy pauses new positions while India VIX is above {limit:g}, "
            "so the backtest needs a VIX reading for every bar and none was supplied."
        )
    if len(vix) != len(spots):
        raise ValueError(f"{len(vix)} VIX readings for {len(spots)} bars; they must pair one to one.")
    return limit


# --------------------------------------------------------------------- pass 1


def discover_requirements(
    spots: Sequence[tuple[dt.datetime, float]],
    params: BacktestParams,
    expiry_for: ExpiryResolver,
    vix: Sequence[float | None] | None = None,
) -> tuple[list[LadderTrigger], list[LegRequirement]]:
    """Pass 1: run the ladder on spot alone and collect the legs it will need.

    The VIX rule applies here exactly as it does in pass 2: a pause changes
    where the ladder later fires, not only whether, so leaving it out would
    fetch the wrong legs.
    """
    limit = _vix_limit(params, spots, vix)
    ladder = Ladder(
        config=params.strategy,
        anchor_mode=params.anchor_mode,
        explicit_anchor=params.explicit_anchor,
    )
    requirements: dict[tuple[dt.date, float, str], LegRequirement] = {}
    triggers: list[LadderTrigger] = []
    campaign_expiry: dt.date | None = None

    for i, (when, spot) in enumerate(spots):
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

        allowed = True if limit is None else vix_allows_entries(vix[i], limit, ladder.paused)
        for trigger in ladder.on_price(spot, when, entries_allowed=allowed):
            triggers.append(trigger)
            for leg in _legs_for(trigger.level, ladder, params.strategy):
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
            if quote.price <= 0 or quote.price > 2500 or (leg.right == "PE" and quote.price >= leg.strike):
                log.warning("Leg %s price %.2f rejected as unphysical", leg, quote.price)
                return None
            out[leg] = (quote.price, quote.source)
        return out

    def _fill(self, price: float, side: Side) -> float:
        """What a leg traded at `price` fills at, on `side`."""
        model = self.params.fill_model
        return price if model is None else model.trade_fill(price, side).price

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

    def run(
        self,
        spots: Sequence[tuple[dt.datetime, float]],
        vix: Sequence[float | None] | None = None,
        settlement: Mapping[dt.date, float] | None = None,
    ) -> BacktestResult:
        """Replay `spots`.

        `vix` is one India VIX reading per bar, required when the strategy has
        a VIX limit and ignored when it has none. `settlement` is NIFTY's
        official close by day -- what NSE settles index options against. An
        expiry missing from it settles at its last bar instead, and is named
        in the warnings: that bar can sit tens of points from the official
        figure, which is an average of the final half hour.
        """
        params = self.params
        result = BacktestResult(params=params)
        self._settlement = dict(settlement or {})
        self._settled_on_last_bar: set[dt.date] = set()

        if not spots:
            result.warnings.append("No spot data in the requested range; nothing to backtest.")
            return result

        limit = _vix_limit(params, spots, vix)
        triggers, requirements = discover_requirements(spots, params, self.expiry_for, vix)
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

        # What the VIX rule did: bars it held entries back on, how many
        # separate spells that made, and the readings it had to go without.
        gated_bars = paused_bars = spells = no_reading = 0

        # The daily loss limit, kept exactly as the live run keeps it.
        loss_limit = abs(params.daily_loss_limit) if params.daily_loss_limit else None
        risk_day: dt.date | None = None
        day_base: float | None = None
        last_equity: float | None = None
        halted_on: dt.date | None = None

        # Monthly Campaign stop loss & trailing SL state
        campaign_sl = (
            params.campaign_stop_loss
            or getattr(params.strategy, "campaign_stop_loss", None)
            or getattr(params.strategy, "stop_loss_mult", None)
        )
        campaign_tsl_pct = (
            params.campaign_trailing_sl_pct
            or getattr(params.strategy, "campaign_trailing_sl_pct", None)
            or params.campaign_trailing_sl
            or getattr(params.strategy, "campaign_trailing_sl", None)
            or getattr(params.strategy, "trailing_sl_mult", None)
        )
        if campaign_tsl_pct is not None:
            if campaign_tsl_pct > 1.0 and campaign_tsl_pct <= 100.0:
                campaign_tsl_pct = campaign_tsl_pct / 100.0

        peak_campaign_capital: float = 0.0
        strategy_tsl_halted: bool = False
        strategy_tsl_reason: str | None = None

        campaign_stopped = False
        campaign_stop_reason: str | None = None
        campaign_anchor_credit: float | None = None

        for i, (when, spot) in enumerate(spots):
            try:
                expiry = self.expiry_for(when.date())
            except ValueError as exc:
                # Past the end of the expiry calendar. Opening is impossible,
                # but the condors already on the book still have to be marked
                # and settled, so this skips entries rather than aborting.
                if not expiry_exhausted and not params.single_campaign:
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
                    # Previous campaign ended and settled:
                    # Update peak capital achieved at the end of a campaign
                    capital_at_end = cumulative_realised
                    if capital_at_end > peak_campaign_capital:
                        peak_campaign_capital = capital_at_end

                    if params.roll_to_next_expiry:
                        ladder.reset()
                        result.rolls.append((when, campaign_expiry, expiry))
                    campaign_expiry = expiry

                    # For the upcoming fresh monthly campaign:
                    if strategy_tsl_halted:
                        campaign_stopped = True
                        campaign_stop_reason = strategy_tsl_reason
                    else:
                        campaign_stopped = False
                        campaign_stop_reason = None
                        campaign_anchor_credit = None

            # 1. Open new rungs -- unless the VIX rule is holding entries back.
            fresh: list[LadderTrigger] = []
            if expiry is not None:
                allowed = True
                if limit is not None:
                    was_paused = ladder.paused
                    allowed = vix_allows_entries(vix[i], limit, was_paused)
                    gated_bars += 1
                    if not allowed:
                        paused_bars += 1
                        spells += int(not was_paused)
                        no_reading += int(vix[i] is None)
                passed_before = len(ladder.passed)
                fresh = ladder.on_price(spot, when, entries_allowed=allowed)
                for passed in ladder.passed[passed_before:]:
                    # Said, not silent: a ladder thinned by the rule must be
                    # distinguishable from one that broke.
                    result.skipped.append(
                        (when, passed.level,
                         f"passed while India VIX was above {limit:g}; not opened")
                    )
            # The contract's own lot size, so quantities, costs, max loss and
            # P&L are in the units that contract actually traded in.
            strategy = params.strategy
            if params.lot_size_for is not None and expiry is not None:
                size = params.lot_size_for(expiry)
                if size != strategy.lot_size:
                    strategy = replace(strategy, lot_size=size)

            if not strategy_tsl_halted and campaign_tsl_pct is not None and peak_campaign_capital > 0:
                tsl_pullback = peak_campaign_capital * campaign_tsl_pct
                tsl_threshold = peak_campaign_capital - tsl_pullback
                if cumulative_realised <= tsl_threshold:
                    strategy_tsl_halted = True
                    strategy_tsl_reason = (
                        f"campaign trailing-stop: capital ₹{cumulative_realised:,.0f} hit TSL limit ₹{tsl_threshold:,.0f} "
                        f"({campaign_tsl_pct * 100:.0f}% pullback of ₹{tsl_pullback:,.0f} from peak campaign capital ₹{peak_campaign_capital:,.0f})"
                    )
                    campaign_stopped = True
                    campaign_stop_reason = strategy_tsl_reason
                    result.warnings.append(
                        f"Campaign {campaign_expiry}: {strategy_tsl_reason} before entries on {when.strftime('%d-%b-%Y')}. "
                        "Halted all further entries."
                    )

            for trigger in fresh:
                if strategy_tsl_halted:
                    result.skipped.append((when, trigger.level, f"campaign trailing-stop active ({strategy_tsl_reason}); not opened"))
                    continue
                if campaign_stopped:
                    result.skipped.append((when, trigger.level, f"campaign stop-loss active ({campaign_stop_reason}); not opened"))
                    continue
                if halted_on == when.date():
                    result.skipped.append((when, trigger.level, "daily loss limit hit today; not opened"))
                    continue
                try:
                    legs = _legs_for(trigger.level, ladder, strategy)
                except ValueError as exc:
                    result.skipped.append((when, trigger.level, str(exc)))
                    continue
                priced = self._price_legs(legs, expiry, when, spot)
                if priced is None:
                    result.skipped.append((when, trigger.level, "no price for one or more legs"))
                    continue

                filled = [
                    FilledLeg(leg=leg, entry_price=self._fill(price, leg.side), source=source)
                    for leg, (price, source) in priced.items()
                ]
                entry_costs = sum(
                    params.costs.leg_cost(fl.leg.side, fl.entry_price, fl.leg.qty, when.date())
                    for fl in filled
                ) + sum(params.costs.slippage(fl.leg.qty) for fl in filled)

                kind, k = _kind_for(trigger.level, ladder, params.strategy)
                unit_type = Condor if kind is UnitKind.CONDOR else VerticalSpread
                condor = unit_type(
                    level=trigger.level,
                    entry_time=when,
                    expiry=expiry,
                    legs=filled,
                    config=strategy,
                    entry_costs=entry_costs,
                    index=next_index,
                    side=trigger.side,
                    kind=kind,
                    k=k,
                )
                if condor.kind is UnitKind.CONDOR:
                    max_allowed_credit = condor.wing_width * condor.config.qty
                    if condor.credit > max_allowed_credit:
                        log.warning(
                            "Condor at %g on %s rejected: credit %.2f exceeds max wing width limit %.2f",
                            trigger.level, when, condor.credit, max_allowed_credit,
                        )
                        result.skipped.append(
                            (when, trigger.level, f"invalid condor credit {condor.credit:.2f} (max allowed {max_allowed_credit:.2f})")
                        )
                        continue
                refusal = entry_refusal(condor, when.date(), params.strategy)
                if refusal is not None:
                    # Recorded, not silent: a filter that quietly thins the
                    # ladder is indistinguishable from one that is broken.
                    result.skipped.append((when, trigger.level, refusal))
                    continue
                next_index += 1
                condors.append(condor)
                if campaign_anchor_credit is None and condor.credit > 0:
                    campaign_anchor_credit = condor.credit

            # 2. Manage open rungs: expiry settlement first, then TP, then Campaign SL.
            for condor in condors:
                if not condor.is_open:
                    continue
                if self._is_settlement(condor, when, last_bar_of_day):
                    self._settle(condor, when, self._settlement_price(condor.expiry, spot), params)
                    realised.append(condor.realised_pnl())
                    cumulative_realised += condor.realised_pnl()
                    if cumulative_realised > peak_campaign_capital:
                        peak_campaign_capital = cumulative_realised
                    holding_days.append((when - condor.entry_time).total_seconds() / 86_400)
                    continue

                marks = self._mark(condor, when, spot)
                reason = condor.exit_signal(marks)
                if reason is not None:
                    status = (
                        CondorStatus.CLOSED_TARGET
                        if "take-profit" in reason
                        else CondorStatus.CLOSED_TRAILING_STOP
                        if "trailing-stop" in reason
                        else CondorStatus.CLOSED_STOP
                    )
                    exits = {fl.leg: self._fill(marks[fl.leg], _flip(fl.leg.side)) for fl in condor.legs if fl.leg in marks}
                    exit_costs = sum(
                        params.costs.leg_cost(_flip(fl.leg.side), exits[fl.leg], fl.leg.qty, when.date())
                        + params.costs.slippage(fl.leg.qty)
                        for fl in condor.legs
                    )
                    for fl in condor.legs:
                        fl.exit_price = exits[fl.leg]
                    condor.close(when, reason, status, exit_costs)
                    realised.append(condor.realised_pnl())
                    cumulative_realised += condor.realised_pnl()
                    if cumulative_realised > peak_campaign_capital:
                        peak_campaign_capital = cumulative_realised
                    holding_days.append((when - condor.entry_time).total_seconds() / 86_400)

            # Campaign-level Stop Loss & Trailing SL across active monthly campaign
            if (campaign_sl is not None or campaign_tsl_pct is not None) and campaign_expiry is not None and not campaign_stopped and not strategy_tsl_halted:
                active_campaign_condors = [c for c in condors if c.expiry == campaign_expiry]
                open_campaign_condors = [c for c in active_campaign_condors if c.is_open]
                if open_campaign_condors:
                    ref_crd = campaign_anchor_credit or (active_campaign_condors[0].credit if active_campaign_condors else 8000.0)
                    camp_realised = sum(c.realised_pnl() for c in active_campaign_condors if not c.is_open)
                    camp_unrealised = 0.0
                    for c in open_campaign_condors:
                        c_marks = self._mark(c, when, spot)
                        if len(c_marks) == len(c.legs):
                            c.open_pnl = c.mtm(c_marks)
                        camp_unrealised += c.open_pnl or 0.0
                    camp_total = camp_realised + camp_unrealised
                    current_total_capital = cumulative_realised + camp_unrealised

                    triggered_reason: str | None = None
                    triggered_status: CondorStatus = CondorStatus.CLOSED_STOP

                    # 1. Monthly Campaign Trailing Stop Loss (TSL) based on peak campaign-end capital
                    if campaign_tsl_pct is not None and peak_campaign_capital > 0:
                        tsl_pullback = peak_campaign_capital * campaign_tsl_pct
                        tsl_threshold = peak_campaign_capital - tsl_pullback
                        if current_total_capital <= tsl_threshold:
                            triggered_reason = (
                                f"campaign trailing-stop: capital ₹{current_total_capital:,.0f} hit TSL limit ₹{tsl_threshold:,.0f} "
                                f"({campaign_tsl_pct * 100:.0f}% pullback of ₹{tsl_pullback:,.0f} from peak campaign capital ₹{peak_campaign_capital:,.0f})"
                            )
                            triggered_status = CondorStatus.CLOSED_TRAILING_STOP
                            strategy_tsl_halted = True
                            strategy_tsl_reason = triggered_reason

                    # 2. Fixed Monthly Campaign Stop Loss
                    if triggered_reason is None and campaign_sl is not None:
                        sl_threshold = campaign_sl if campaign_sl > 20 else campaign_sl * ref_crd
                        if camp_total <= -sl_threshold:
                            triggered_reason = f"campaign stop-loss: monthly loss -₹{abs(camp_total):,.0f} hit limit -₹{sl_threshold:,.0f}"
                            triggered_status = CondorStatus.CLOSED_STOP

                    if triggered_reason is not None:
                        campaign_stopped = True
                        campaign_stop_reason = triggered_reason
                        for c in open_campaign_condors:
                            c_marks = self._mark(c, when, spot)
                            c_exits = {fl.leg: self._fill(c_marks[fl.leg], _flip(fl.leg.side)) for fl in c.legs if fl.leg in c_marks}
                            exit_costs = sum(
                                params.costs.leg_cost(_flip(fl.leg.side), c_exits.get(fl.leg, fl.entry_price), fl.leg.qty, when.date())
                                + params.costs.slippage(fl.leg.qty)
                                for fl in c.legs
                            )
                            for fl in c.legs:
                                fl.exit_price = c_exits.get(fl.leg, fl.entry_price)
                            c.close(when, triggered_reason, triggered_status, exit_costs)
                            realised.append(c.realised_pnl())
                            cumulative_realised += c.realised_pnl()
                            holding_days.append((when - c.entry_time).total_seconds() / 86_400)

                        result.warnings.append(
                            f"Campaign {campaign_expiry}: {triggered_reason} on {when.strftime('%d-%b-%Y')}. "
                            f"Squared off {len(open_campaign_condors)} open condor(s) and halted all further entries."
                            if strategy_tsl_halted else
                            f"Campaign {campaign_expiry}: {triggered_reason} on {when.strftime('%d-%b-%Y')}. "
                            f"Squared off {len(open_campaign_condors)} open condor(s) and halted new entries for this expiry."
                        )

            # 3. Mark the book.
            open_condors = [c for c in condors if c.is_open]
            max_concurrent = max(max_concurrent, len(open_condors))
            peak_risk = max(peak_risk, sum(c.max_loss for c in open_condors))

            unrealised = 0.0
            for condor in open_condors:
                marks = self._mark(condor, when, spot)
                # A leg with no price this bar keeps the position at its last
                # mark. `mtm` answers 0 then, and the curve showed the whole
                # position as flat for that bar -- a false jump in equity and
                # in drawdown.
                if len(marks) == len(condor.legs):
                    condor.open_pnl = condor.mtm(marks)
                unrealised += condor.open_pnl or 0.0

            equity.append(
                EquityPoint(
                    ts=when,
                    equity=cumulative_realised + unrealised,
                    open_condors=len(open_condors),
                    spot=spot,
                )
            )
            if loss_limit is not None:
                today, total = when.date(), cumulative_realised + unrealised
                if risk_day != today:
                    risk_day = today
                    day_base = last_equity if last_equity is not None else total
                last_equity = total
                if total - day_base <= -loss_limit:
                    halted_on = today

        # Positions still open when the range ends stay open, marked at the
        # last bar's prices. They used to be "settled" at intrinsic against the
        # last spot as if they expired then: a 27 Oct condor opened on 29 Sep,
        # out of the money with four weeks to run, booked its whole credit as
        # profit and read "Held to expiry". What an open position is worth is
        # its market value, which is what the final equity point already holds.
        open_at_end = [c for c in condors if c.is_open]
        open_pnl = sum(c.open_pnl or 0.0 for c in open_at_end)

        draws = metrics_mod.drawdown_series([p.equity for p in equity])
        for point, draw in zip(equity, draws):
            point.drawdown = draw

        provider_summary = getattr(self.provider, "summary", lambda: {})()
        result.condors = condors
        result.equity = equity
        result.metrics = metrics_mod.compute(
            realised=realised,
            equity=equity,
            total_credit=sum(c.credit for c in condors if c.credit > 0),
            total_debit=sum(-c.credit for c in condors if c.credit < 0),
            total_costs=sum(c.entry_costs + c.exit_costs for c in condors if not c.is_open),
            open_positions=len(open_at_end),
            open_pnl=open_pnl,
            capital_at_risk=peak_risk,
            max_concurrent=max_concurrent,
            holding_days=holding_days,
            real_price_fraction=float(provider_summary.get("real_fraction", 0.0)),
            modeled_quotes=int(provider_summary.get("modeled_quotes", 0)),
        )

        if result.metrics.modeled_quotes:
            anchored = int(provider_summary.get("anchored_quotes", 0))
            how = (
                "Black-76, anchored to the exchange's previous-day closing prices"
                if anchored and anchored == result.metrics.modeled_quotes
                else "Black-76, partly anchored to the exchange's previous-day closing prices "
                     "and otherwise from India VIX"
                if anchored
                else "Black-76 from India VIX"
            )
            result.warnings.append(
                f"{100 * (1 - result.metrics.real_price_fraction):.1f}% of quotes were MODELED "
                f"({how}), not real traded premiums."
            )
        if result.rolls:
            result.warnings.append(
                f"Rolled through {len(result.rolls) + 1} expiry campaigns; the ladder re-anchors "
                "at each new expiry because offsetting only works within one."
            )
        # One line per cause. They used to share a single count blamed on
        # missing prices, which was already wrong once the entry filters could
        # refuse a rung, and would be wrong by hundreds with the VIX rule.
        no_price = sum(1 for s in result.skipped if s[2] == "no price for one or more legs")
        filtered = sum(1 for s in result.skipped if s[2].startswith("entry filter"))
        passed_levels = sum(1 for s in result.skipped if "India VIX" in s[2])
        other = len(result.skipped) - no_price - filtered - passed_levels
        if no_price:
            result.warnings.append(
                f"{no_price} ladder trigger(s) were skipped because no price was available."
            )
        if filtered:
            result.warnings.append(f"{filtered} ladder trigger(s) were refused by an entry filter.")
        if other:
            result.warnings.append(f"{other} ladder trigger(s) could not be built as a position.")
        if self._settled_on_last_bar and self._settlement:
            # Only worth saying when official closes were supplied at all: a
            # caller with none has asked for last-bar settlement throughout.
            days = ", ".join(f"{d:%d %b %Y}" for d in sorted(self._settled_on_last_bar))
            result.warnings.append(
                f"No official NIFTY close for expiry {days}; settled at the last bar of the day instead."
            )
        if limit is not None:
            result.vix_gate = {
                "limit": limit,
                "bars": gated_bars,
                "paused_bars": paused_bars,
                "paused_fraction": paused_bars / gated_bars if gated_bars else 0.0,
                "spells": spells,
                "levels_passed": passed_levels,
                "bars_without_vix": no_reading,
            }
            if paused_bars:
                result.warnings.append(
                    f"New positions were paused on {paused_bars:,} of {gated_bars:,} bars "
                    f"({paused_bars / gated_bars:.0%}) because India VIX was above {limit:g}, "
                    f"in {spells} spell(s); {passed_levels} level(s) were passed while paused."
                )
            if no_reading:
                result.warnings.append(
                    f"{no_reading} bar(s) had no India VIX reading, so no position could open on them."
                )
        return result

    # -- settlement --------------------------------------------------------

    def _settlement_price(self, expiry: dt.date, last_spot: float) -> float:
        """What an expiry settles against: NSE's official close for the day.

        Not the last bar. The official close is an average of the final half
        hour, and on six of the eight monthly expiries of 2026 it sat 18 to 61
        points from the last five-minute bar -- enough to move a strike in or
        out of the money.
        """
        official = self._settlement.get(expiry)
        if official is not None and official > 0:
            return float(official)
        self._settled_on_last_bar.add(expiry)
        return last_spot

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

        Index options cash-settle: no order, so no brokerage or exchange
        charge -- only STT on exercise, paid by the holder of an in-the-money
        long. It used to charge the full selling charges to in-the-money
        shorts instead, and nothing to the longs that actually pay.
        """
        exit_costs = 0.0
        for fl in condor.legs:
            intrinsic = fl.leg.intrinsic(spot)
            fl.exit_price = intrinsic
            fl.exit_source = PriceSource.CHOICE
            exit_costs += params.costs.settlement_cost(fl.leg.side, intrinsic, fl.leg.qty, when.date())
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
