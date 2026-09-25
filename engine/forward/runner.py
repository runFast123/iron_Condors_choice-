"""Forward testing against live Choice data.

Drives the *same* :class:`~engine.strategy.ladder.Ladder` and
:func:`~engine.strategy.condor.build_legs` the backtester uses, so a forward
run cannot silently diverge from the backtest that justified it. The only
difference is where prices come from: live Choice quotes instead of a bar
iterator.

Paper only. Fills are simulated against the live Choice book and nothing is
sent to the exchange -- there is no order-placement path in this codebase, so
there is no arming step and no mode to switch into. A daily-loss kill switch
and a max-rung cap still apply, because a runaway paper run wastes a day of
testing.

Everything the runner does is appended to a structured event log, and every
fill lands in the trade history, so the dashboard can show exactly what
happened and when.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, Callable

from engine.choice.errors import ChoiceAuthError, ChoiceError, ChoiceSessionRejected
from engine.choice.instruments import Contract
from engine.config import IST, engine_config
from engine.choice.errors import ChoiceInstrumentError
from engine.data.expiry_calendar import nearest_listed_expiry
from engine.data.market import NIFTY, ChoiceMarketData, Quote
from engine.data.market_calendar import MARKET_CLOSE as MARKET_CLOSE_TIME
from engine.data.market_calendar import MARKET_OPEN as MARKET_OPEN_TIME
from engine.data.market_calendar import MarketCalendar, MarketStatus
from engine.forward.fills import FillModel, FillPrice
from engine.pricing.costs import CostModel
from engine.strategy.condor import (
    Condor,
    CondorStatus,
    FilledLeg,
    Leg,
    PositionUnit,
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
from engine.strategy.vertical import VerticalSpread
from engine.store.db import HIC, LADDER, Store
from engine.strategy.ladder import Ladder

log = logging.getLogger(__name__)

MARKET_OPEN = MARKET_OPEN_TIME
MARKET_CLOSE = MARKET_CLOSE_TIME

# How often to trim the stored tick history. Every 500 ticks is roughly two
# hours at the default poll -- often enough to bound the table, rare enough
# that the delete never sits on the polling path.
TICK_PRUNE_EVERY = 500

# A real iron condor collects a meaningful fraction of its wing width -- tens
# of percent for a weekly. Below this the premiums are wrong, not merely thin.
MIN_CREDIT_FRACTION = 0.02

# An India VIX reading older than this is not a reading of now. It arrives as
# the close of the latest one-minute candle, so in session it is normally a
# minute or two old; past this the feed has stalled, and the VIX rule pauses
# entries rather than trade on a stale number.
VIX_MAX_AGE = dt.timedelta(minutes=15)
#: How long a run holds still, waiting for a usable India VIX reading, before
#: the rule's "no reading pauses" applies. Covers the open -- the first tick
#: at 09:15 can come before the day's first VIX print -- and a single failed
#: request, neither of which says anything about volatility.
VIX_WAIT_LIMIT = dt.timedelta(minutes=30)

# When NIFTY's official close for the day can be read as final. The exchange
# publishes it after 15:30; a settlement read at the bell could catch a
# provisional daily candle, so expiry day waits until this before settling.
OFFICIAL_CLOSE_READY = dt.time(16, 0)


class UnsupportedStateVersion(ValueError):
    """A saved run was written by an engine whose state format we cannot read.

    Distinct from every other ValueError `restore` can raise -- enum lookups,
    float parsing, config validation -- because only this one is genuinely
    unrecoverable. Catching plain ValueError meant one malformed byte
    permanently retired a run that still had positions open.
    """

# One calendar for the process: holidays are global, and learning one in a
# forward run should stop every other run polling a shut exchange too.
market_calendar = MarketCalendar.load()


def market_is_open(now: dt.datetime | None = None) -> bool:
    """Weekday, inside session hours, and not a known holiday.

    This is the local answer. A runner with a Choice session prefers the
    exchange's own MarketStatus and falls back to this when that is silent.
    """
    return market_calendar.is_open(now)


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
    action: str = "OPEN"       # OPEN | CLOSE
    # When the market data behind this trade actually printed.
    #
    # `ts` is the engine's clock -- when it processed the tick. That is not
    # when the market did the thing that caused the trade. The index is served
    # from candles rather than the live book, so a rung fires off a spot that
    # printed earlier, and one tick can fire two rungs a third of a second
    # apart that the market crossed minutes or a session apart. Showing only
    # the engine clock is what made the trade log disagree with the chart.
    #
    # None when the two are the same, so nothing is invented.
    market_ts: str | None = None
    # What fair value was, what crossing the spread cost, and whether that
    # spread came from a real book or was modelled in its absence.
    reference: float | None = None
    slippage: float = 0.0
    spread_modelled: bool = False


#: The config class each strategy's geometry lives in.
_CONFIG_CLASSES: dict[str, type[StrategyConfig]] = {LADDER: StrategyConfig, HIC: HicConfig}


def _strategy_from_state(
    blob: dict[str, Any], strategy_id: str
) -> tuple[StrategyConfig, str | None]:
    """Rebuild the config of whichever strategy this run trades.

    Dispatched on the strategy id rather than always building a
    `StrategyConfig`. `asdict` writes HIC's band and spread settings out
    faithfully, but filtering them back in against the *base* class's field
    names drops every one of them, and the rebuilt object is then a plain
    `StrategyConfig`. `_plan_unit` asks `isinstance(config, HicConfig)`, gets
    False, and opens a four-leg condor at the next level -- where the strategy
    calls for a two-leg spread -- while every surface still labels the run HIC
    because the database column is untouched. An engine restart silently
    turned HIC into a ladder, at several times the risk per rung.

    The reverse skew is real too, and worse to paper over. A run started by an
    engine that knew the name "hic" but had no code to build one recorded the
    column and traded a ladder, so its saved geometry carries none of HIC's
    settings. Filling those in from defaults here would change what a live run
    trades, mid-campaign, into something nobody chose -- with condors already
    open at levels the new shape would have made spreads. It keeps trading
    what it has been trading, and returns a note saying so.
    """
    cls = _CONFIG_CLASSES.get(strategy_id, StrategyConfig)
    if cls is StrategyConfig:
        known = {f.name for f in dataclass_fields(cls)}
        return StrategyConfig(**{k: v for k, v in blob.items() if k in known}), None

    base = {f.name for f in dataclass_fields(StrategyConfig)}
    extra = {f.name for f in dataclass_fields(cls)} - base
    if not (extra & set(blob)):
        return (
            StrategyConfig(**{k: v for k, v in blob.items() if k in base}),
            f"This run is recorded as {strategy_id} but its saved settings are a "
            f"ladder's, so it was started by an engine that could not build "
            f"{strategy_id} and has been trading a ladder under that name. It "
            f"continues as a ladder rather than changing shape mid-campaign. "
            f"Stop it and start a new one to trade {strategy_id} properly.",
        )
    return cls(**{k: v for k, v in blob.items() if k in base | extra}), None


class ForwardRunner:
    """Runs the ladder against live Choice prices."""

    # The VIX rule's last reading, when it printed, and why there was no usable
    # one when there was not -- shown on the dashboard, because a run that has
    # stopped opening positions must say why. Class-level defaults: they are
    # only ever replaced, never mutated, and a runner put together without
    # __init__ (several tests build one that way) still has them.
    last_vix: float | None = None
    last_vix_as_of: dt.datetime | None = None
    vix_problem: str | None = None
    # Since when the run has had no usable reading, while it waits for one.
    _vix_missing_since: dt.datetime | None = None
    # Expiries a "Cannot settle" warning has been given for, so a settlement
    # that keeps failing says so once instead of on every tick.
    _settle_warned: frozenset = frozenset()

    def __init__(
        self,
        market: ChoiceMarketData,
        strategy: StrategyConfig,
        *,
        costs: CostModel | None = None,
        state_path: Path | None = None,
        max_events: int = 500,
        fill_model: FillModel | None = None,
        expiry_cadence: str = "weekly",
        store: "Store | None" = None,
        session_id: str | None = None,
        user_id: str | None = None,
        strategy_id: str = LADDER,
        run_key: str | None = None,
        run_label: str = "",
        daily_loss_limit: float | None = None,
    ) -> None:
        self.market = market
        self.strategy = strategy
        # Paper is the only mode this platform has. There is no order-placement
        # path to switch into, which is why no arming step exists either.
        self.mode = "paper"
        self.costs = costs or CostModel()
        self.fill_model = fill_model or FillModel()
        # Weekly or monthly. A forward run on a different cadence from the
        # backtest that justified it is a different strategy.
        self.expiry_cadence = expiry_cadence
        self.state_path = state_path or Path("web/data/live.json")
        # Durable home for this run. Without it the run exists only for as long
        # as this process does, which is the failure being fixed here.
        self.store = store
        self.session_id = session_id
        self.user_id = user_id
        # Which strategy this run belongs to. Not the same thing as
        # `self.strategy`, which is the geometry -- steps, offsets, lot size.
        self.strategy_id = strategy_id
        # Which run it is. Defaults to the strategy, so a user with one run per
        # strategy addresses it exactly as they always did.
        self.run_key = run_key or strategy_id
        self.run_label = run_label
        # This run's own kill switch. Per run, so one runaway test cannot stop
        # the others; the account-wide total is checked separately by the
        # caller, which is the only place that can see all of a user's runs.
        self.daily_loss_limit = (
            abs(daily_loss_limit) if daily_loss_limit else abs(engine_config.daily_loss_limit)
        )
        self.max_events = max_events

        self.ladder = Ladder(config=strategy)
        # Still called `condors`: that is the persisted key and the name every
        # surface reads. It now holds whatever the strategy opens -- for HIC,
        # condors near the anchor and vertical spreads beyond it. The name is
        # a little wrong; renaming it is a schema change, so the debt is
        # recorded rather than paid.
        self.condors: list[PositionUnit] = []
        self.events: list[Event] = []
        self.fills: list[Fill] = []
        self.started_at = dt.datetime.now(tz=IST)
        self.last_tick: dt.datetime | None = None
        self.last_spot: float | None = None
        self.expiry: dt.date | None = None
        self.realised = 0.0
        # Which open positions the last kill-switch check could not see, so
        # the warning is raised when that changes rather than every tick.
        self._unmarked_seen: tuple[int, ...] = ()
        # Whether the last tick failed on authentication, so the expiry is
        # reported once per spell instead of on every poll.
        self._auth_failed = False
        self.stopped_reason: str | None = None
        # Why the last tick produced no quotes, shown in the UI rather than
        # buried in the log: a dash with no explanation is not diagnosable.
        self.last_error: str | None = None
        # Unrealised P&L per open condor from the most recent tick. Kept so
        # the snapshot reports a real number instead of a placeholder.
        self.last_mtm: dict[int, float] = {}
        # Latest mid per option token. The per-condor MTM answers "is this
        # structure up or down"; this answers "where is that leg now", which is
        # the question a fill log actually raises.
        self.last_marks: dict[int, float] = {}
        # How many legs were priced on a real book versus a modelled spread,
        # so the UI can say how trustworthy a run's P&L actually is.
        session = getattr(market, "session", None)
        # The exchange is the only real authority on an unscheduled closure;
        # the calendar is the fallback when the endpoint is unreachable.
        self.market_status = MarketStatus(session, market_calendar) if session else None
        # One lock for the whole runner.
        #
        # Two callers legitimately drive this object: the background poll
        # thread and POST /forward/tick, while GET /forward/state serialises it
        # concurrently. Unguarded that produced double-counted realised P&L, a
        # condor's fills split across two indices, and -- worst -- `emit`
        # trimming the event list while `snapshot` iterated it in reverse,
        # duplicating entries in the run log most of the time.
        #
        # Reentrant because tick() -> _open_condor() -> emit() all take it.
        self._lock = threading.RLock()
        self._ticks_recorded = 0
        # Suspended, not stopped.
        #
        # When a session ends the runner loses the credentials it needs for
        # quotes, so its thread must exit -- but the *run* has not ended. It
        # still holds positions and a ladder mid-flight, and the next login
        # should pick it back up. Conflating the two marked runs permanently
        # stopped for the sin of the browser being closed.
        self._suspended = False
        # The worker driving `run`, and how fast. Set by whoever starts the
        # thread; the watchdog reads them to tell a live run from a dead one.
        self.tick_thread: threading.Thread | None = None
        self.poll_seconds = 15.0
        # Whether the most recent spot came from a candle rather than the book,
        # and when that candle printed.
        self.spot_is_stale = False
        self.last_spot_ts: dt.datetime | None = None
        self.legs_on_real_depth = 0
        self.legs_on_modelled_spread = 0
        self.total_slippage = 0.0
        self._contracts: dict[tuple[str, float, str], Contract] = {}

    # ------------------------------------------------------------- logging

    def suspend(self, reason: str = "session ended") -> None:
        """Halt the tick thread without ending the run.

        `stopped_reason` is deliberately untouched: it is what marks a run
        finished in the database, and a run whose session expired is not
        finished. It is waiting for someone to sign in again.
        """
        self._suspended = True
        self.emit("info", f"Run suspended ({reason}); it resumes at the next login")

    def unsuspend(self) -> None:
        """Clear the halt so a fresh worker can drive this run again.

        Only clears the flag. Starting the thread is the caller's job, because
        only the caller knows the poll interval and owns the session the run
        will quote through.
        """
        self._suspended = False

    @property
    def suspended(self) -> bool:
        return self._suspended

    @property
    def is_ticking(self) -> bool:
        """Whether a live worker is actually driving this run.

        The distinction that matters: a run can be marked running in every
        surface -- database, dashboard, snapshot -- while the thread that was
        supposed to advance its ladder is long gone.
        """
        thread = self.tick_thread
        return bool(thread is not None and thread.is_alive())

    def emit(self, severity: str, message: str, **detail: Any) -> None:
        """Append one line to the run log.

        The parameter is `severity`, not `level`: callers routinely pass a
        strike `level=` as detail, and naming both the same thing made every
        condor-opened log line raise TypeError -- on the main path, the moment
        a forward run actually did something.
        """
        event = Event(
            ts=dt.datetime.now(tz=IST).isoformat(), level=severity, message=message, detail=detail
        )
        with self._lock:
            self.events.append(event)
        if len(self.events) > self.max_events:
            del self.events[: len(self.events) - self.max_events]
        getattr(log, "error" if severity == "error" else "info")("%s %s", message, detail or "")

    # ------------------------------------------------------------- pricing

    def _contract(self, expiry: dt.date, strike: float, right: str) -> Contract:
        key = (expiry.isoformat(), strike, right)
        if key not in self._contracts:
            self._contracts[key] = self.market.master.option(NIFTY, expiry, strike, right)
        return self._contracts[key]

    def _quote_legs(self, legs: list[Leg], expiry: dt.date) -> dict[Leg, tuple[Quote, Contract]] | None:
        """Live quotes for all four legs, or None if any is missing.

        A partial quote is refused: opening three of four legs would leave an
        unhedged short, which is the one outcome this structure exists to
        avoid -- in paper just as much as anywhere else, because a paper run
        that silently reports an impossible position is worse than no run.
        """
        try:
            contracts = {leg: self._contract(expiry, leg.strike, leg.right) for leg in legs}
        except ChoiceError as exc:
            self.emit("error", "Could not resolve a leg", error=str(exc))
            return None

        try:
            quotes = self.market.quotes(list(contracts.values()))
        except ChoiceError as exc:
            self.emit("error", "Touchline failed", error=str(exc))
            return None

        out: dict[Leg, tuple[Quote, Contract]] = {}
        for leg, contract in contracts.items():
            quote = quotes.get(contract.token)
            if quote is None or quote.ltp <= 0:
                self.emit(
                    "warn",
                    "No live quote for a leg; condor skipped",
                    strike=leg.strike, right=leg.right, token=contract.token,
                )
                return None
            out[leg] = (quote, contract)
        return out

    def _record_fill_quality(self, fill: FillPrice) -> None:
        if fill.spread_modelled:
            self.legs_on_modelled_spread += 1
        else:
            self.legs_on_real_depth += 1
        self.total_slippage += fill.slippage

    # ---------------------------------------------------------------- open

    def _plan_unit(self, level: float) -> tuple[list[Leg], UnitKind, int | None]:
        """What this strategy opens at `level`: its legs, its kind, its step.

        The ladder opens a condor at every level. HIC opens a condor near the
        anchor and a vertical spread beyond it, so it has to know how far the
        level is from the anchor before it can say what to build.
        """
        config = self.strategy
        if not isinstance(config, HicConfig):
            return build_legs(level, config), UnitKind.CONDOR, None

        anchor = self.ladder.anchor
        if anchor is None:
            # Nothing can be outside a band that has not been placed yet.
            return build_legs(level, config), UnitKind.CONDOR, 0
        k = steps_from_anchor(level, anchor, config.step)
        return build_hic_legs(level, k, config), structure_kind(k, config), k

    def shape_at(self, level: float | None) -> str | None:
        """What kind of structure a level would open, without building it.

        For a tile that has to say whether the next rung is a condor or a
        bought spread. HIC's answer changes with distance from the anchor, and
        a level on its own does not tell a reader which they are about to get
        -- the only way to know was to wait and count the legs.
        """
        if level is None:
            return None
        config = self.strategy
        if not isinstance(config, HicConfig) or self.ladder.anchor is None:
            return UnitKind.CONDOR.value
        k = steps_from_anchor(level, self.ladder.anchor, config.step)
        return structure_kind(k, config).value

    def _open_condor(
        self, level: float, expiry: dt.date, side: str = "down"
    ) -> PositionUnit | None:
        try:
            legs, kind, k = self._plan_unit(level)
        except ValueError as exc:
            # A structure that cannot be built -- a spread whose two strikes
            # collapse onto one, say -- is a configuration fault, not a
            # tradeable position.
            self.emit(
                "error", "Could not build a unit at this level", level=level, error=str(exc)
            )
            return None
        quoted = self._quote_legs(legs, expiry)
        if quoted is None:
            return None

        # Price every leg before committing to any of them: a book too wide to
        # trade through on one leg invalidates the whole structure, and finding
        # that out halfway would leave a partial condor in the book.
        priced: dict[Leg, tuple[FillPrice, Contract]] = {}
        for leg in legs:
            quote, contract = quoted[leg]
            fill = self.fill_model.fill(quote, leg.side)
            if fill is None:
                self.emit(
                    "warn", "Spread too wide to trade; condor skipped",
                    level=level, strike=leg.strike, right=leg.right,
                    bid=quote.bid, ask=quote.ask,
                )
                return None
            priced[leg] = (fill, contract)

        now = dt.datetime.now(tz=IST)
        unit_type = Condor if kind is UnitKind.CONDOR else VerticalSpread

        # The ladder's entry filters, decided on the prices it would trade at
        # and before a single fill is recorded -- deciding afterwards would
        # leave fills in the log for a position that was never opened. The
        # same function the backtest uses, so a filter behaves identically in
        # both. The rung stays fired either way.
        probe = unit_type(
            level=level, entry_time=now, expiry=expiry,
            legs=[FilledLeg(leg=leg, entry_price=priced[leg][0].price) for leg in legs],
            config=self.strategy, index=len(self.condors), side=side, kind=kind, k=k,
        )
        refusal = entry_refusal(probe, now.date(), self.strategy)
        if refusal is not None:
            self.emit("info", f"Condor at {level:,.0f} not opened", level=level, reason=refusal)
            return None

        filled: list[FilledLeg] = []
        entry_costs = 0.0
        modelled = 0

        for leg in legs:
            fill, contract = priced[leg]
            self._record_fill_quality(fill)
            modelled += int(fill.spread_modelled)
            filled.append(
                FilledLeg(
                    leg=leg, entry_price=fill.price,
                    source=PriceSource.CHOICE, token=contract.token,
                )
            )
            entry_costs += self.costs.leg_cost(leg.side, fill.price, leg.qty)
            # The spot first: it is the rung's trigger, and the reason this
            # trade exists at all. The leg's own quote time is the fallback,
            # for when the option itself came from a candle.
            quote_as_of = self.last_spot_ts or quoted[leg][0].as_of
            self.fills.append(
                Fill(
                    ts=now.isoformat(), condor_index=len(self.condors), condor_level=level,
                    expiry=expiry.isoformat(), right=leg.right, side=leg.side.value,
                    strike=leg.strike, qty=leg.qty, price=fill.price, source="choice",
                    mode=self.mode, token=contract.token, action="OPEN",
                    reference=round(fill.reference, 2), slippage=round(fill.slippage, 2),
                    spread_modelled=fill.spread_modelled,
                    market_ts=quote_as_of.isoformat() if quote_as_of else None,
                )
            )

        condor = unit_type(
            level=level, entry_time=now, expiry=expiry, legs=filled,
            config=self.strategy, entry_costs=entry_costs, index=len(self.condors),
            side=side, kind=kind, k=k,
        )
        self.condors.append(condor)
        self.emit(
            "trade", f"Opened condor at {level:,.0f}",
            level=level, credit=round(condor.credit, 2),
            max_loss=round(condor.max_loss, 2), expiry=expiry.isoformat(),
            modelled_legs=modelled,
        )
        self._check_premium_is_plausible(condor, level)
        return condor

    def _check_premium_is_plausible(self, unit: PositionUnit, level: float) -> None:
        """Warn when what a structure cost looks like a quote fault.

        Written for a real one: a 20-DTE 200-point condor opened for Rs62
        against Rs13,000 of risk, because every premium came through at a
        hundredth of its value. The structure itself looked perfectly well
        formed, which is what made it dangerous.

        The test is against what the structure was *meant* to be, not against
        the sign of what it cost. A condor is a credit structure, so a debit is
        a fault. A bought spread is a debit structure, so a credit is equally
        one -- and that case was invisible before, because the old check only
        looked for the condor's failure.
        """
        span = _premium_span(unit)
        if span <= 0:
            return

        expects_credit = unit.kind is UnitKind.CONDOR or not _expects_debit(unit)
        taken = unit.credit
        paid = -taken

        if taken == 0:
            self.emit(
                "warn", "Opened for nothing at all; check the quotes",
                level=level, kind=unit.kind.value,
            )
            return

        if expects_credit and taken < 0:
            self.emit(
                "warn", "Credit structure opened at a net debit; check the quotes",
                level=level, kind=unit.kind.value, credit=round(taken, 2),
            )
            return
        if not expects_credit and taken > 0:
            self.emit(
                "warn", "Debit structure opened for a credit; check the quotes",
                level=level, kind=unit.kind.value, credit=round(taken, 2),
            )
            return

        premium = taken if expects_credit else paid
        if premium < span * MIN_CREDIT_FRACTION:
            self.emit(
                "warn",
                "Premium is implausibly small for the risk; check the quote scale",
                level=level, kind=unit.kind.value, premium=round(premium, 2),
                risk=round(span, 2),
                premium_pct_of_width=round(100 * premium / span, 3),
            )
        elif not expects_credit and paid >= span:
            # A bought spread cannot rationally cost more than it can ever pay,
            # and the width is the most it can pay.
            self.emit(
                "warn", "Debit spread cost more than its maximum payout",
                level=level, kind=unit.kind.value, debit=round(paid, 2),
                max_payout=round(span, 2),
            )

    # -------------------------------------------------------------- manage

    def _mark_all(self) -> dict[int, float]:
        """Live MTM per open condor index, marked at mid.

        Unrealised P&L is marked to fair value, not to what liquidating would
        fetch; the cost of crossing the spread is charged when a condor is
        actually opened or closed rather than smeared across every tick.
        """
        out: dict[int, float] = {}
        open_condors = [c for c in self.condors if c.is_open]
        if not open_condors:
            self.last_mtm = out
            return out
        contracts = []
        for condor in open_condors:
            for fl in condor.legs:
                try:
                    contracts.append(self._contract(condor.expiry, fl.leg.strike, fl.leg.right))
                except ChoiceError as exc:
                    # Swallowed silently, this drops the condor out of MTM, out
                    # of the kill-switch total, and out of exit evaluation --
                    # take-profit and stop-loss stop working for that rung with
                    # no trace anywhere.
                    self.emit(
                        "warn", "Could not resolve a leg for MTM; condor not marked",
                        level=condor.level, strike=fl.leg.strike, right=fl.leg.right,
                        error=str(exc),
                    )
        try:
            quotes = self.market.quotes(contracts)
        except ChoiceError as exc:
            self.emit("warn", "MTM refresh failed", error=str(exc))
            return out

        for token, quote in quotes.items():
            self.last_marks[token] = quote.mid

        for condor in open_condors:
            marks: dict[Leg, float] = {}
            leg_quotes: dict[Leg, Quote] = {}
            for fl in condor.legs:
                quote = quotes.get(fl.token) if fl.token is not None else None
                if quote is not None:
                    marks[fl.leg] = quote.mid
                    leg_quotes[fl.leg] = quote
            if len(marks) == len(condor.legs):
                out[condor.index] = condor.mtm(marks)
                reason = condor.exit_signal(marks)
                if reason:
                    self._close(condor, leg_quotes, reason)
                    out.pop(condor.index, None)
        self.last_mtm = out
        return out

    def _close(self, condor: Condor, quotes: dict[Leg, Quote], reason: str) -> None:
        """Close a condor, crossing the spread the other way on every leg."""
        if not condor.is_open:
            # Two callers reaching this for the same condor booked its P&L
            # twice and wrote eight CLOSE fills for four legs.
            return
        now = dt.datetime.now(tz=IST)
        exit_costs = 0.0
        exits: dict[Leg, FillPrice] = {}
        for fl in condor.legs:
            fill = self.fill_model.exit_fill(quotes[fl.leg], fl.leg.side)
            if fill is None:
                # Nothing sensible to close at. Leave the condor open and try
                # again next tick rather than book an invented exit price.
                self.emit(
                    "warn", "Spread too wide to close; condor left open",
                    level=condor.level, strike=fl.leg.strike, right=fl.leg.right,
                )
                return
            exits[fl.leg] = fill

        for fl in condor.legs:
            fill = exits[fl.leg]
            exit_as_of = self.last_spot_ts or quotes[fl.leg].as_of
            opposite = Side.BUY if fl.leg.side is Side.SELL else Side.SELL
            self._record_fill_quality(fill)
            fl.exit_price = fill.price
            fl.exit_source = PriceSource.CHOICE
            exit_costs += self.costs.leg_cost(opposite, fill.price, fl.leg.qty)
            self.fills.append(
                Fill(
                    ts=now.isoformat(), condor_index=condor.index, condor_level=condor.level,
                    expiry=condor.expiry.isoformat(), right=fl.leg.right, side=opposite.value,
                    strike=fl.leg.strike, qty=fl.leg.qty, price=fill.price, source="choice",
                    mode=self.mode, token=fl.token, action="CLOSE",
                    reference=round(fill.reference, 2), slippage=round(fill.slippage, 2),
                    spread_modelled=fill.spread_modelled,
                    market_ts=(
                        exit_as_of.isoformat() if exit_as_of else None
                    ),
                )
            )
        status = CondorStatus.CLOSED_TARGET if "take-profit" in reason else CondorStatus.CLOSED_STOP
        condor.close(now, reason, status, exit_costs)
        self.realised += condor.realised_pnl()
        self.emit(
            "trade", f"Closed condor at {condor.level:,.0f}",
            level=condor.level, reason=reason, pnl=round(condor.realised_pnl(), 2),
        )

    # ----------------------------------------------------- expiry settlement

    def _expiry_is_settled(self, now: dt.datetime) -> bool:
        """Whether this campaign's expiry is past its settlement.

        On expiry day only after the close: the positions trade and mark right
        up to it, and settling at midday would book an intrinsic against a
        spot with hours left to move.
        """
        if self.expiry is None:
            return False
        if now.date() > self.expiry:
            return True
        return now.date() == self.expiry and now.time() >= MARKET_CLOSE_TIME

    def _settlement_spot(self, now: dt.datetime, spot: float) -> tuple[float | None, str]:
        """The index level to settle against, and where it came from.

        NSE settles index options against NIFTY's official closing price -- an
        average of the final half hour -- and Choice's daily candle carries
        exactly that figure: it matched the exchange's close to the paisa on
        every monthly expiry of 2026. So that is the settlement, always.

        Not the last level a tick saw. On six of those eight expiries the last
        five-minute bar sat 18 to 61 points from the official close -- enough
        to put a strike on the wrong side of the money -- and a spot read on a
        later day is a different day's number altogether.

        On expiry day the official figure is published after the close, so
        until OFFICIAL_CLOSE_READY this waits rather than read a candle that
        may still be provisional. A run is not normally ticking then anyway;
        it settles on the next morning's first tick. If the close cannot be
        fetched nothing settles, because leaving positions open and visibly
        unsettled is recoverable and booking a fiction is not.
        """
        if self.expiry is None:
            return None, ""
        if now.date() < self.expiry or (
            now.date() == self.expiry and now.time() < OFFICIAL_CLOSE_READY
        ):
            return None, ""
        try:
            # To the day after, not to the expiry itself: a date-only end is
            # midnight at the start of expiry day, so whether that day's own
            # candle came back depended on how Choice treats the boundary.
            frame = self.market.nifty(
                self.expiry - dt.timedelta(days=10), self.expiry + dt.timedelta(days=1)
            )
        except ChoiceError as exc:
            self._warn_settle("Cannot settle: no NIFTY close for the expiry", error=str(exc))
            return None, ""
        for row in reversed(list(frame.itertuples())):
            ts = row.ts.to_pydatetime() if hasattr(row.ts, "to_pydatetime") else row.ts
            if ts.date() == self.expiry:
                return float(row.close), f"the official NIFTY close on {self.expiry:%d-%b-%Y}"
        self._warn_settle("Cannot settle: the expiry date is missing from the NIFTY series")
        return None, ""

    def _warn_settle(self, message: str, **fields) -> None:
        """Say once per expiry that it cannot settle yet. Every tick retries,
        and repeating it every few seconds flushed the run's event log."""
        key = (self.expiry, message)
        if key in self._settle_warned:
            return
        self._settle_warned = self._settle_warned | {key}
        self.emit("warn", message, expiry=self.expiry.isoformat() if self.expiry else None, **fields)

    def _settle(self, unit: PositionUnit, now: dt.datetime, spot: float, note: str) -> None:
        """Close one position at intrinsic value.

        Index options cash-settle, so there is no exit brokerage -- only STT on
        in-the-money shorts, which the cost model applies. Mirrors the
        backtester's `_settle` exactly, so a forward run and a backtest of the
        same path book the same expiry.
        """
        exit_costs = 0.0
        for fl in unit.legs:
            intrinsic = fl.leg.intrinsic(spot)
            fl.exit_price = intrinsic
            fl.exit_source = PriceSource.CHOICE
            if intrinsic > 0 and fl.leg.side is Side.SELL:
                exit_costs += self.costs.leg_cost(Side.SELL, intrinsic, fl.leg.qty)
            opposite = Side.BUY if fl.leg.side is Side.SELL else Side.SELL
            self.fills.append(
                Fill(
                    ts=now.isoformat(), condor_index=unit.index, condor_level=unit.level,
                    expiry=unit.expiry.isoformat(), right=fl.leg.right, side=opposite.value,
                    strike=fl.leg.strike, qty=fl.leg.qty, price=intrinsic, source="settlement",
                    mode=self.mode, token=fl.token, action="CLOSE",
                )
            )
        unit.close(now, f"expired; settled at intrinsic against {note}",
                   CondorStatus.EXPIRED, exit_costs)

    def _settle_and_roll(self, now: dt.datetime, spot: float) -> None:
        """Settle an expired book, then start a fresh campaign.

        Nothing did this before. `self.expiry` was resolved once and never
        reset, so after expiry day `_contract` could no longer resolve the
        dead contracts, `_mark_all` dropped every position out of the marks,
        and the run's headline P&L fell to whatever had closed early --
        usually zero -- while the positions sat OPEN for ever. The book did not
        lose money on the dashboard so much as quietly cease to exist. Then the
        ladder went on firing into the settled expiry.

        Offsetting only works inside one expiry, so the ladder re-anchors
        afterwards, exactly as the backtest does at each roll.
        """
        expired = self.expiry
        if expired is None:
            return
        settlement, note = self._settlement_spot(now, spot)
        if settlement is None:
            return                      # already explained; try again next tick

        settled = 0
        for unit in self.condors:
            if unit.is_open and unit.expiry == expired:
                self._settle(unit, now, settlement, note)
                self.realised += unit.realised_pnl()
                self.last_mtm.pop(unit.index, None)
                settled += 1
                self.emit(
                    "trade", f"Settled {unit.level:,.0f} at expiry",
                    level=unit.level, reason=unit.exit_reason,
                    pnl=round(unit.realised_pnl(), 2),
                )

        self.emit(
            "info",
            f"{expired:%d-%b-%Y} expired: settled {settled} position"
            f"{'' if settled == 1 else 's'} against {note} of {settlement:,.0f}",
            expiry=expired.isoformat(), settled=settled,
            settlement_spot=round(settlement, 2),
            realised=round(self.realised, 2),
        )
        self.expiry = None
        self.ladder.reset()
        self.emit(
            "info",
            "Ladder re-anchored for the next expiry, because offsetting only "
            "works within one",
        )

    # ------------------------------------------------------------ VIX rule

    def _read_vix(self, now: dt.datetime) -> tuple[float | None, dt.datetime | None, str | None]:
        """This tick's India VIX, when it printed, and why it is unusable if it is."""
        getter = getattr(self.market, "india_vix_now", None)
        if getter is None:
            return None, None, "this market connection cannot read India VIX"
        try:
            value, as_of = getter()
        except ChoiceError as exc:
            return None, None, f"India VIX unavailable ({exc})"
        if as_of is not None and now - as_of > VIX_MAX_AGE:
            minutes = int((now - as_of).total_seconds() // 60)
            return value, as_of, (
                f"the latest India VIX reading printed at {as_of:%H:%M}, "
                f"{minutes} minutes ago, which is too old to go on"
            )
        return value, as_of, None

    def _vix_allows_entries(self, now: dt.datetime) -> bool | None:
        """The VIX rule for this tick, saying so in the log when it changes.

        The same decision the backtest makes on every bar: no new positions
        while India VIX is above the run's limit, or while there is no usable
        reading to judge it by. Open positions are not touched either way.

        None means "hold still": there is no usable reading yet, and the caller
        must not feed the ladder this tick. Treated as a pause instead, the
        first tick of the day -- before India VIX had printed -- paused the run
        and resumed it seconds later, and the resume passed over every level an
        opening gap had crossed, with VIX nowhere near the limit. The backtest,
        which reads the VIX of the bar's own interval, opened them. Held still,
        the ladder simply sees the gap once a reading arrives. Only a spell of
        VIX_WAIT_LIMIT with no reading becomes the rule's pause.
        """
        limit = self.strategy.max_entry_vix
        if limit is None:
            return True
        value, as_of, problem = self._read_vix(now)
        self.last_vix, self.last_vix_as_of, self.vix_problem = value, as_of, problem
        if problem:
            if self._vix_missing_since is None:
                self._vix_missing_since = now
            if now - self._vix_missing_since < VIX_WAIT_LIMIT:
                return None
        else:
            self._vix_missing_since = None
        was_paused = self.ladder.paused
        allowed = vix_allows_entries(None if problem else value, limit, was_paused)
        if not allowed and not was_paused:
            if problem:
                self.emit("warn", f"New positions paused: {problem}", limit=limit)
            else:
                self.emit(
                    "info",
                    f"New positions paused: India VIX {value:.2f} is above {limit:g}",
                    vix=round(value, 2), limit=limit,
                )
        elif allowed and was_paused:
            self.emit(
                "info",
                f"New positions resumed: India VIX {value:.2f} is below {limit:g}",
                vix=round(value, 2), limit=limit,
            )
        return allowed

    def _pnl_total_locked(self) -> tuple[float, tuple[int, ...]]:
        """Total P&L as the dashboard shows it, and what is missing from it."""
        open_units = [c for c in self.condors if c.is_open]
        marked = [self.last_mtm[c.index] for c in open_units if c.index in self.last_mtm]
        unmarked = tuple(c.index for c in open_units if c.index not in self.last_mtm)
        return self.realised + sum(marked), unmarked

    # ----------------------------------------------------------------- tick

    def tick(self) -> None:
        """One polling cycle: read spot, fire triggers, refresh MTM.

        Serialised against every other caller. The lock is held across the
        broker round-trip, which briefly delays a concurrent snapshot -- that
        is the intended trade: a reader waiting 200ms beats a reader seeing a
        condor whose fills exist but whose position does not.
        """
        with self._lock:
            self._tick_locked()

    def _tick_locked(self) -> None:
        try:
            index = self.market.master.index(NIFTY)
            quote = self.market.quotes([index]).get(index.token)
        except ChoiceAuthError as exc:
            # Choice sessions last a day. When one expires the run keeps
            # ticking and every tick fails the same way, so say what will fix
            # it rather than repeating the broker's wording -- and say it once
            # per spell, not every ten seconds into a log nobody can then read.
            #
            # A session opened today being refused is not an expiry, and the
            # engine deliberately does not log in again for it; its message
            # says so. "Sign out and sign in again" was the advice here, and
            # on 25 Sep it was exactly what cost another OTP for nothing.
            refused_today = isinstance(exc, ChoiceSessionRejected)
            self.last_error = str(exc) if refused_today else (
                "Choice has rejected this session, so no quotes can be fetched. The run "
                "keeps its positions and resumes marking as soon as it can quote. Signing "
                "in again on the dashboard renews the session (Choice texts you one OTP)."
            )
            if not self._auth_failed:
                self._auth_failed = True
                self.emit(
                    "error",
                    "Choice is refusing the session" if refused_today else "Choice session expired",
                    error=str(exc),
                )
            return
        except ChoiceError as exc:
            self.last_error = str(exc)
            self.emit("error", "Live quote failed", error=str(exc))
            return
        if self._auth_failed:
            self._auth_failed = False
            self.emit("info", "Choice session is live again; marking resumed")
        spot = quote.ltp if quote else None
        if spot is None or spot <= 0:
            self.last_error = (
                "Choice returned no price for NIFTY from either the live book or "
                "ChartData. Check that the market is open and the token is subscribed."
            )
            self.emit("warn", self.last_error)
            return
        # MultipleTouchline does not serve index tokens, so the spot normally
        # arrives from the last traded candle. Say so rather than implying a
        # live tick the endpoint never gave us.
        was_stale, self.spot_is_stale = self.spot_is_stale, bool(quote and quote.stale)
        if self.spot_is_stale and not was_stale:
            self.emit("info", "Spot is coming from the last traded candle, not the live book")
        self.last_error = None

        now = dt.datetime.now(tz=IST)
        self.last_spot, self.last_tick = spot, now
        # When that spot actually printed, which is not when we read it. The
        # index is served from candles, not the live book, so the level that
        # fires a rung can have traded a minute or more before this tick.
        self.last_spot_ts = quote.as_of if quote else None
        if self.store is not None and self.session_id:
            try:
                self.store.record_tick(self.session_id, now.isoformat(), spot)
                # Nothing else called prune_ticks, so the table grew for the
                # life of the database -- roughly 150 MB per user per year at
                # the fastest poll, for a chart that draws a few hundred
                # points. Trimmed occasionally rather than every tick, since
                # the delete is the expensive half.
                self._ticks_recorded += 1
                if self._ticks_recorded % TICK_PRUNE_EVERY == 0:
                    self.store.prune_ticks(self.session_id)
            except Exception:                       # noqa: BLE001
                log.exception("Could not record tick")

        # Settle before resolving, so a campaign that has expired books its
        # P&L and clears the way for the next one rather than sitting open
        # against contracts that no longer trade.
        if self.expiry is not None and self._expiry_is_settled(now):
            self._settle_and_roll(now, spot)
            if self.expiry is not None:
                # Settlement is waiting -- for the official close, or for a
                # candle Choice could not serve. Either way the contract has
                # stopped trading, and a rung opened now would be opened on
                # it; the old path went on firing into the dead expiry.
                return

        if self.expiry is None:
            try:
                self.expiry = nearest_listed_expiry(
                    self.market.master.expiries(NIFTY), now.date(),
                    cadence=self.expiry_cadence, min_days=1,
                )
                if self.expiry is None:
                    raise ChoiceInstrumentError(
                        f"No {self.expiry_cadence} NIFTY expiry is listed after today."
                    )
            except ChoiceError as exc:
                # Outside the guard above this escaped tick(), escaped run(),
                # and killed the worker -- while stopped_reason stayed None, so
                # every surface kept reporting the run as live with a frozen
                # timestamp.
                self.last_error = str(exc)
                self.emit("error", "Could not resolve an expiry", error=str(exc))
                return
            self.emit("info", f"Trading expiry {self.expiry:%d-%b-%Y}", expiry=self.expiry.isoformat())

        allowed = self._vix_allows_entries(now)
        passed_before = len(self.ladder.passed)
        # No usable India VIX reading yet: nothing is decided this tick, and the
        # ladder is left exactly where it was for the next one.
        fresh = [] if allowed is None else self.ladder.on_price(spot, now, entries_allowed=allowed)
        passed = self.ladder.passed[passed_before:]
        if passed:
            self.emit(
                "info",
                "Passed over " + ", ".join(f"{p.level:,.0f}" for p in passed)
                + " while new positions were paused; not opened",
                levels=[p.level for p in passed],
            )
        for trigger in fresh:
            # The ladder enforces the same cap when it decides whether to fire,
            # so this is a backstop -- but it must read the *run's* limit, not a
            # global one, or a run configured for 30 would silently drop 10.
            if len([c for c in self.condors if c.is_open]) >= self.strategy.max_condors:
                self.emit("warn", "Max concurrent condors reached; trigger ignored", level=trigger.level)
                continue
            self._open_condor(trigger.level, self.expiry, side=trigger.side)

        self._mark_all()
        # From `last_mtm`, not from what `_mark_all` just returned. A failed
        # quote refresh returns nothing without clearing the stored marks, so
        # the switch summed an empty dict and read a book 40,000 under water
        # as exactly zero -- while the snapshot next to it, which does read
        # `last_mtm`, still showed the real figure. Stale marks are a worse
        # answer than fresh ones and a far better one than silence.
        total, unmarked = self._pnl_total_locked()
        if total <= -self.daily_loss_limit:
            self.stopped_reason = f"daily loss limit hit ({total:,.0f})"
            self.emit("error", "KILL SWITCH: " + self.stopped_reason, pnl=round(total, 2))
        elif unmarked and unmarked != self._unmarked_seen:
            # Said when it changes, not every ten seconds. The limit is being
            # measured against part of the book, and a reader deciding whether
            # the run is safe should know which part is missing.
            self.emit(
                "warn", "Daily loss limit is being measured against an incomplete book",
                unmarked=len(unmarked), total=round(total, 2),
                limit=round(self.daily_loss_limit, 2),
            )
        self._unmarked_seen = unmarked

    # ---------------------------------------------------------------- state

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        # "Not marked yet" and "worth exactly zero" are different facts, and
        # rendering both as 0 hid a resumed run's entire unrealised P&L behind
        # a confident-looking "+0".
        open_condors = [c for c in self.condors if c.is_open]
        marked = {c.index: self.last_mtm[c.index] for c in open_condors if c.index in self.last_mtm}
        unmarked = [c.index for c in open_condors if c.index not in self.last_mtm]
        unrealised = sum(marked.values())

        down_condors = [c for c in self.condors if getattr(c, "side", "down") in ("down", "anchor")]
        up_condors = [c for c in self.condors if getattr(c, "side", "down") == "up"]
        down_pnl = sum(
            self.last_mtm[c.index] if c.is_open and c.index in self.last_mtm
            else c.realised_pnl() if not c.is_open else 0.0
            for c in down_condors
        )
        up_pnl = sum(
            self.last_mtm[c.index] if c.is_open and c.index in self.last_mtm
            else c.realised_pnl() if not c.is_open else 0.0
            for c in up_condors
        )
        # The live book, the same set the `net_positions` table below lists.
        # It used to describe the whole campaign, closed rungs included, so a
        # half-closed run reported a self-hedge ratio over twenty strikes above
        # a table showing ten -- a percentage for positions no longer held.
        summary = netting_summary(self.condors, open_only=True)

        return {
            "session": {
                "mode": self.mode,
                "status": "stopped" if self.stopped_reason else "running",
                "stopped_reason": self.stopped_reason,
                "started_at": self.started_at.isoformat(),
                "last_tick": self.last_tick.isoformat() if self.last_tick else None,
                "market_open": market_is_open(),
                "connected": True,
                "expiry": self.expiry.isoformat() if self.expiry else None,
                "last_error": self.last_error,
                "quote_format": getattr(self.market, "touchline_format", None),
            },
            # How much of this run was priced on a real order book. A run made
            # mostly of modelled spreads is still useful, but it is not the
            # same claim as one filled against live depth, so say which it is.
            "fill_quality": {
                "legs_on_real_depth": self.legs_on_real_depth,
                "legs_on_modelled_spread": self.legs_on_modelled_spread,
                "real_depth_fraction": (
                    self.legs_on_real_depth
                    / (self.legs_on_real_depth + self.legs_on_modelled_spread)
                    if (self.legs_on_real_depth + self.legs_on_modelled_spread)
                    else 0.0
                ),
                "total_slippage": round(self.total_slippage, 2),
            },
            # Latest mid per token, so the fill log can show where each leg
            # trades now next to what it filled at. As of `last_tick`.
            "marks": {str(k): round(v, 2) for k, v in self.last_marks.items()},
            "market": {
                "spot": self.last_spot,
                "ts": self.last_tick.isoformat() if self.last_tick else None,
                "stale": self.spot_is_stale,
                # When the spot printed, as against when we read it.
                "as_of": self.last_spot_ts.isoformat() if self.last_spot_ts else None,
            },
            # The VIX rule. `limit` None means the run has no rule -- runs
            # started before it existed keep trading as they always did.
            "vix": {
                "limit": self.strategy.max_entry_vix,
                "value": round(self.last_vix, 2) if self.last_vix is not None else None,
                "as_of": self.last_vix_as_of.isoformat() if self.last_vix_as_of else None,
                "paused": self.ladder.paused,
                "problem": self.vix_problem,
            },
            "ladder": {
                "anchor": self.ladder.anchor,
                "last_level": self.ladder.last_level,
                "next_trigger": self.ladder.next_trigger_level,
                "distance": self.ladder.distance_to_next(self.last_spot) if self.last_spot else None,
                "fired": self.ladder.levels(),
                "step": self.strategy.step,
                # Additive. `next_trigger` and `distance` keep meaning the down
                # rung, so every surface reading them is unaffected; a two-way
                # ladder needs both sides and one field cannot carry them.
                "direction": self.strategy.direction,
                "high_level": self.ladder.high_level,
                "next_down": self.ladder.next_down_level,
                "next_up": self.ladder.next_up_level,
                "distance_up": (
                    self.ladder.distance_to_next_up(self.last_spot) if self.last_spot else None
                ),
                "down_count": self.ladder.down_count,
                "up_count": self.ladder.up_count,
                # HIC only: how many steps either side of the anchor still open
                # a full condor. None for a ladder, which has no band.
                "band": getattr(self.strategy, "full_band_steps", None),
                # And what the next rung on each side will actually be, so a
                # tile can say "condor" or "put spread" rather than leaving a
                # reader to open it and count the legs.
                "next_down_kind": self.shape_at(self.ladder.next_down_level),
                "next_up_kind": self.shape_at(self.ladder.next_up_level),
            },
            "pnl": {
                "realised": round(self.realised, 2),
                "unrealised": round(unrealised, 2),
                "total": round(self.realised + unrealised, 2),
                "down_pnl": round(down_pnl, 2),
                "up_pnl": round(up_pnl, 2),
                # How many open condors the total does NOT include, so a
                # partial figure is never mistaken for a complete one.
                "unmarked_condors": len(unmarked),
                "open_condors": len(open_condors),
                "total_condors": len(self.condors),
            },
            "netting": summary,
            "positions": [
                {
                    "index": c.index, "level": c.level, "side": getattr(c, "side", "down"),
                    # What shape this is, and how far from the anchor. A page
                    # showing a condor and a spread in one table has no other
                    # way to tell them apart: both are rows of legs.
                    "kind": c.kind.value,
                    "k": c.k,
                    "expiry": c.expiry.isoformat(),
                    "entry_time": c.entry_time.isoformat(), "status": c.status.value,
                    "credit": round(c.credit, 2), "max_loss": round(c.max_loss, 2),
                    "max_profit": round(c.max_profit, 2),
                    "breakevens": [round(b, 2) for b in c.breakevens],
                    # None, not 0.0, when this condor has never been marked.
                    "pnl": (
                        round(self.last_mtm[c.index], 2)
                        if c.is_open and c.index in self.last_mtm
                        else round(c.realised_pnl(), 2) if not c.is_open
                        else None
                    ),
                    "is_open": c.is_open,
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
        """Persist the run, durably first and then as a readable snapshot.

        The database is what a restart reads back; the JSON file is a
        convenience for eyeballing state on the box. A failure to write either
        must not kill the tick loop -- a run that stops trading because its
        disk is full is a worse outcome than one that stops being saved.
        """
        if self.store is not None and self.session_id and self.user_id:
            try:
                self.store.save_forward(
                    session_id=self.session_id,
                    user_id=self.user_id,
                    status="stopped" if self.stopped_reason else "running",
                    started_at=self.started_at.isoformat(),
                    stopped_reason=self.stopped_reason,
                    state=self.to_state(),
                    strategy_id=self.strategy_id,
                    run_key=self.run_key,
                    run_label=self.run_label,
                )
            except Exception:                       # noqa: BLE001
                log.exception("Could not persist forward state")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self.snapshot(), separators=(",", ":")), encoding="utf-8"
            )
        except OSError:
            log.exception("Could not write the state snapshot file")

    # ------------------------------------------------------------------ run

    def is_market_open(self, now: dt.datetime | None = None) -> bool:
        """Choice's answer if it has one, the local calendar otherwise."""
        if self.market_status is not None:
            live = self.market_status.is_open(now)
            if live is not None:
                return live
        return market_is_open(now)

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
            idle_logged = False
            while max_ticks is None or ticks < max_ticks:
                # Checked before the market-hours branch, not after the tick:
                # a stopped run used to sit in the closed-market sleep all
                # night and then trade once more at the open.
                if self.stopped_reason or self._suspended:
                    break
                if not self.is_market_open():
                    # Log the transition once, not every minute all weekend.
                    if not idle_logged:
                        nxt = market_calendar.next_open()
                        self.emit("info", f"Market closed; idling until {nxt:%d-%b %H:%M}")
                        self.save()
                        idle_logged = True
                    if max_ticks is not None:
                        break
                    time.sleep(60)
                    continue
                idle_logged = False
                self.tick()
                self.save()
                if on_tick:
                    on_tick(self)
                ticks += 1
                if self.stopped_reason or self._suspended:
                    break
                if max_ticks is None or ticks < max_ticks:
                    time.sleep(poll_seconds)
        except KeyboardInterrupt:
            self.stopped_reason = self.stopped_reason or "interrupted"
            self.emit("warn", "Interrupted by user")
        except Exception as exc:                    # noqa: BLE001
            # A worker thread that dies with stopped_reason unset leaves every
            # surface reporting a live run that will never tick again. Whatever
            # went wrong, record it where the user can see it.
            self.stopped_reason = f"engine error: {type(exc).__name__}: {exc}"
            self.last_error = str(exc)
            log.exception("Forward run crashed")
            self.emit("error", "Forward run stopped by an unexpected error", error=str(exc))
        finally:
            if self._suspended and not self.stopped_reason:
                self.emit("info", "Forward run suspended", ticks=ticks)
            else:
                self.emit("info", "Forward run stopped", ticks=ticks)
            self.save()

    # -------------------------------------------------------------- resume

    STATE_VERSION = 1

    @classmethod
    def _readable_state(cls, state: dict[str, Any]) -> dict[str, Any]:
        """Bring a saved run up to the shape this engine reads, or refuse it.

        The test is "newer than we can read", not "not exactly ours". Exact
        equality meant that bumping the version retired every open run on the
        next login -- positions, ladder and all -- because `_resume_forward`
        treats UnsupportedStateVersion as a run worth abandoning. A format we
        wrote ourselves an hour ago is not unreadable; it just needs upgrading.

        Upgrades are applied in order and never mutate the caller's dict, so a
        failure part-way leaves the stored state exactly as it was.
        """
        version = state.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise UnsupportedStateVersion(
                f"Forward state has no usable version (got {version!r})"
            )
        if version > cls.STATE_VERSION:
            raise UnsupportedStateVersion(
                f"Forward state version {version} was written by a newer engine; "
                f"this one reads up to {cls.STATE_VERSION}"
            )
        if version == cls.STATE_VERSION:
            return state

        upgraded = dict(state)
        while upgraded["version"] < cls.STATE_VERSION:
            step = cls._UPGRADES.get(upgraded["version"])
            if step is None:                        # pragma: no cover - guarded by a test
                raise UnsupportedStateVersion(
                    f"No upgrade path from forward state version {upgraded['version']}"
                )
            upgraded = step(upgraded)
        return upgraded

    # version -> how to turn it into the next one. Keyed by the version being
    # left behind, so the chain is readable from any starting point.
    _UPGRADES: dict[int, Any] = {}


    @staticmethod
    def _condor_state(condor: Condor) -> dict[str, Any]:
        return {
            "index": condor.index,
            "level": condor.level,
            "side": getattr(condor, "side", "down"),
            # Absent on state written before HIC existed, where every unit was
            # a condor -- which is exactly what the restore default says.
            "kind": condor.kind.value,
            "k": condor.k,
            "entry_time": condor.entry_time.isoformat(),
            "expiry": condor.expiry.isoformat(),
            "status": condor.status.value,
            "exit_time": condor.exit_time.isoformat() if condor.exit_time else None,
            "exit_reason": condor.exit_reason,
            "entry_costs": condor.entry_costs,
            "exit_costs": condor.exit_costs,
            "legs": [
                {
                    "right": fl.leg.right,
                    "side": fl.leg.side.value,
                    "strike": fl.leg.strike,
                    "qty": fl.leg.qty,
                    "entry_price": fl.entry_price,
                    "source": fl.source.value,
                    "token": fl.token,
                    "exit_price": fl.exit_price,
                    "exit_source": fl.exit_source.value if fl.exit_source else None,
                }
                for fl in condor.legs
            ],
        }

    def to_state(self) -> dict[str, Any]:
        """Everything needed to resume this run in a fresh process.

        Deliberately not ``snapshot()``: that is shaped for the dashboard, and
        rebuilding a strategy from its own rendering is how a resumed run ends
        up subtly different from the one it replaced. This is the ladder's
        actual state -- anchor, fired levels, open positions and their fills.
        """
        return {
            "version": self.STATE_VERSION,
            "mode": self.mode,
            "strategy_id": self.strategy_id,
            "run_key": self.run_key,
            "run_label": self.run_label,
            "daily_loss_limit": self.daily_loss_limit,
            # Persisted because a resumed run that silently changed cadence is
            # a different strategy from the one the user started.
            "expiry_cadence": self.expiry_cadence,
            "started_at": self.started_at.isoformat(),
            "last_tick": self.last_tick.isoformat() if self.last_tick else None,
            "last_spot": self.last_spot,
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "realised": self.realised,
            "stopped_reason": self.stopped_reason,
            "legs_on_real_depth": self.legs_on_real_depth,
            "legs_on_modelled_spread": self.legs_on_modelled_spread,
            "total_slippage": self.total_slippage,
            "strategy": asdict(self.strategy),
            "ladder": self.ladder.dump_state(),
            # The last marks. Without these a resumed run reports every open
            # condor at zero until the next tick -- and outside market hours
            # there is no next tick, so a position with real P&L sits at "+0"
            # overnight looking perfectly settled.
            "last_mtm": {str(k): v for k, v in self.last_mtm.items()},
            "last_marks": {str(k): v for k, v in self.last_marks.items()},
            # So a resumed run can say why it is paused before its first tick.
            "last_vix": self.last_vix,
            "last_vix_as_of": self.last_vix_as_of.isoformat() if self.last_vix_as_of else None,
            "vix_problem": self.vix_problem,
            "condors": [self._condor_state(c) for c in self.condors],
            "fills": [asdict(f) for f in self.fills],
            "events": [asdict(e) for e in self.events],
        }

    @classmethod
    def restore(
        cls,
        state: dict[str, Any],
        *,
        market: ChoiceMarketData,
        costs: CostModel | None = None,
        state_path: Path | None = None,
        fill_model: FillModel | None = None,
        store: "Store | None" = None,
        session_id: str | None = None,
        user_id: str | None = None,
        strategy_id: str | None = None,
        run_key: str | None = None,
        run_label: str | None = None,
    ) -> "ForwardRunner":
        """Rebuild a runner from :meth:`to_state`.

        A restore that silently dropped positions would be worse than no
        restore at all -- the ladder would re-fire levels it already holds --
        so every open condor is reconstructed with its original fills, and the
        fired-level set is restored so nothing opens twice.
        """
        state = cls._readable_state(state)

        # The database column wins over the blob: it is the one a query can
        # filter on, so it is the one the rest of the engine believes. Resolved
        # before the config, because it decides which config to build.
        resolved_strategy_id = strategy_id or state.get("strategy_id") or LADDER
        strategy, skew = _strategy_from_state(state["strategy"], resolved_strategy_id)

        runner = cls(
            market=market, strategy=strategy, costs=costs,
            state_path=state_path, fill_model=fill_model,
            store=store, session_id=session_id, user_id=user_id,
            strategy_id=resolved_strategy_id,
            # The caller's value wins over the blob: it comes from the
            # database column, which is the one a query can filter on and
            # therefore the one the rest of the engine believes.
            run_key=run_key or state.get("run_key"),
            run_label=run_label if run_label is not None else (state.get("run_label") or ""),
            daily_loss_limit=state.get("daily_loss_limit"),
            expiry_cadence=state.get("expiry_cadence") or "weekly",
        )
        runner.started_at = _parse_dt(state.get("started_at")) or runner.started_at
        runner.last_tick = _parse_dt(state.get("last_tick"))
        runner.last_spot = state.get("last_spot")
        runner.expiry = _parse_date(state.get("expiry"))
        runner.realised = float(state.get("realised") or 0.0)
        # Restore the marks, so a resumed run reports the P&L it last knew
        # rather than zero until the next tick -- which, outside market hours,
        # never comes.
        for key, value in (state.get("last_mtm") or {}).items():
            try:
                runner.last_mtm[int(key)] = float(value)
            except (TypeError, ValueError):
                continue
        for key, value in (state.get("last_marks") or {}).items():
            try:
                runner.last_marks[int(key)] = float(value)
            except (TypeError, ValueError):
                continue
        runner.stopped_reason = state.get("stopped_reason")
        try:
            runner.last_vix = (
                float(state["last_vix"]) if state.get("last_vix") is not None else None
            )
        except (TypeError, ValueError):
            runner.last_vix = None
        runner.last_vix_as_of = _parse_dt(state.get("last_vix_as_of"))
        runner.vix_problem = state.get("vix_problem")
        runner.legs_on_real_depth = int(state.get("legs_on_real_depth") or 0)
        runner.legs_on_modelled_spread = int(state.get("legs_on_modelled_spread") or 0)
        runner.total_slippage = float(state.get("total_slippage") or 0.0)

        runner.ladder.load_state(state.get("ladder") or {})

        runner.condors = [
            _restore_condor(raw, strategy) for raw in state.get("condors") or []
        ]
        runner.fills = [Fill(**raw) for raw in state.get("fills") or []]
        runner.events = [Event(**raw) for raw in state.get("events") or []]
        # After the saved log is loaded, not before: emitting into a list that
        # is about to be replaced is the same as not emitting at all.
        if skew is not None:
            log.error("%s: %s", run_key or resolved_strategy_id, skew)
            runner.emit("error", skew)
        return runner


def _expects_debit(unit: PositionUnit) -> bool:
    """Whether this structure was built to be paid for rather than sold."""
    return unit.kind in (UnitKind.PUT_DEBIT_SPREAD, UnitKind.CALL_DEBIT_SPREAD)


def _premium_span(unit: PositionUnit) -> float:
    """The most the structure can be worth at expiry, in rupees.

    A condor's is one wing; a vertical's is the gap between its two strikes.
    Both are the figure the premium has to look sane against.
    """
    width = getattr(unit, "wing_width", None)
    if width is None:
        width = getattr(unit, "width", 0.0)
    return float(width) * unit.config.qty


def _parse_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _restore_condor(raw: dict[str, Any], strategy: StrategyConfig) -> PositionUnit:
    """Rebuild one saved unit as the type it actually is.

    State written before HIC carries no kind, and every unit in it is a condor,
    so that is the default. Getting this wrong would not raise -- a vertical
    rebuilt as a Condor would refuse to construct, which is the validation
    doing its job, but a condor rebuilt as a vertical would too. Either way the
    run would fail to resume rather than resume wrong, which is the right way
    round for a mistake.
    """
    legs = [
        FilledLeg(
            leg=Leg(
                right=item["right"], side=Side(item["side"]),
                strike=float(item["strike"]), qty=int(item["qty"]),
            ),
            entry_price=float(item["entry_price"]),
            source=PriceSource(item.get("source") or PriceSource.CHOICE.value),
            token=item.get("token"),
            exit_price=item.get("exit_price"),
            exit_source=PriceSource(item["exit_source"]) if item.get("exit_source") else None,
        )
        for item in raw.get("legs") or []
    ]
    kind = UnitKind(raw.get("kind") or UnitKind.CONDOR.value)
    unit_type = Condor if kind is UnitKind.CONDOR else VerticalSpread
    k = raw.get("k")
    return unit_type(
        level=float(raw["level"]),
        entry_time=_parse_dt(raw.get("entry_time")) or dt.datetime.now(tz=IST),
        expiry=_parse_date(raw.get("expiry")) or dt.date.today(),
        legs=legs,
        config=strategy,
        status=CondorStatus(raw.get("status") or CondorStatus.OPEN.value),
        exit_time=_parse_dt(raw.get("exit_time")),
        exit_reason=raw.get("exit_reason"),
        entry_costs=float(raw.get("entry_costs") or 0.0),
        exit_costs=float(raw.get("exit_costs") or 0.0),
        index=int(raw.get("index") or 0),
        side=raw.get("side", "down"),
        kind=kind,
        k=int(k) if k is not None else None,
    )
