"""Per-user backtest jobs.

A backtest fetches historical candles for every option leg the ladder touches,
which against a real broker takes minutes rather than milliseconds. That cannot
be a synchronous HTTP request, so a run happens on a worker thread and the
dashboard polls for progress.

Each job belongs to exactly one signed-in user and holds that user's own Choice
session. Nobody sees anyone else's run.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
import logging
import threading
import uuid
import traceback
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from engine.backtest.providers import (
    CandlePriceProvider,
    FallbackPriceProvider,
    ModelPriceProvider,
)
from engine.backtest.runner import (
    Backtest,
    BacktestParams,
    discover_requirements,
    spots_from_frame,
    weekly_expiry_resolver,
)
from engine.backtest.serialise import empty_bundle, serialise
from engine.backtest.vix_series import VixAsOf
from engine.data import groww
from engine.data.expiry_calendar import MAX_WEEKLY_DTE, confirm_derived_expiries, expiry_calendar
from engine.data.market_calendar import MARKET_CLOSE, MarketCalendar
from engine.choice.errors import ChoiceAuthError, ChoiceError
from engine.choice.instruments import HistoricalInstruments
from engine.config import IST
from engine.data.market import INDIA_VIX, NIFTY, ChoiceMarketData
from engine.pricing.costs import CostModel
from engine.pricing.iv_surface import IVSurface, from_vix
from engine.store.db import Store
from engine.strategy.condor import PriceSource, StrategyConfig
from engine.strategy.hic import HicConfig

log = logging.getLogger(__name__)

# Bumped whenever a fix changes what a backtest *means*, so stored results from
# an older engine are retired rather than shown as if they were current.
#   1 -> first durable results
#   2 -> expiries derived for historical ranges; before this every condor in a
#        past range carried the nearest *currently listed* expiry, which priced
#        weeklies as half-year options and understated max loss about threefold.
#   3 -> the modelled IV surface fitted to Choice's live chain (skew and term
#        structure) instead of a flat default.
#   4 -> expiries settle against NIFTY's official close, not the last bar, and
#        modelled premiums use only the India VIX known at that moment.
RESULT_VERSION = 4

#: Why results older than RESULT_VERSION are no longer shown -- the newest fix
#: first, since it is the one every older result is missing.
RETIRED_BECAUSE = (
    "expiries now settle against NIFTY's official closing price rather than the last "
    "five-minute bar, which sat 18 to 61 points away on most 2026 expiries, and modelled "
    "premiums now use only the India VIX known at the moment they price"
)

# A fitted surface describes the market on the day it was measured. Older than
# this and the shape has moved enough that the flat default is more honest.
CALIBRATION_MAX_AGE_DAYS = 14

DEFAULT_ATM_VOL = 0.14


@dataclass
class BacktestJob:
    """One run, its progress, and its result."""

    job_id: str
    user_id: str
    params: dict[str, Any]
    status: str = "queued"          # queued | running | done | error
    stage: str = "starting"
    progress: float = 0.0           # 0..1
    message: str = ""
    error: str | None = None
    started_at: str = field(default_factory=lambda: dt.datetime.now(tz=IST).isoformat())
    finished_at: str | None = None
    result: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        """Progress without the (large) result payload."""
        return {
            "job_id": self.job_id,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 3),
            "message": self.message,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "params": self.params,
        }


def _strategy_for(p: dict, lot_size: int, listed, market) -> StrategyConfig:
    """The geometry a backtest will trade, of whichever strategy's shape.

    HIC's config is a subclass, so the trigger, the leg builder and the cost
    model all take one without needing to know which they were handed.
    """
    common = dict(
        direction=str(p.get("direction") or "down"),
        anchor_mode=p.get("anchor_mode"),
        max_down=p.get("max_down"),
        max_up=p.get("max_up"),
        step=float(p["step"]),
        lots=int(p["lots"]),
        lot_size=lot_size,
        max_condors=int(p["max_condors"]),
        strike_step=market.master.strike_step(NIFTY, listed[0]) if listed else 50.0,
        take_profit_pct=p.get("take_profit"),
        stop_loss_mult=p.get("stop_loss"),
        # The VIX rule applies to both strategies, unlike the entry filters.
        max_entry_vix=p.get("max_entry_vix"),
    )
    wanted = str(p.get("strategy") or "ladder")
    if wanted != "hic":
        # The ladder's entry filters. HIC's config ignores them regardless,
        # but they are only ever passed to the strategy they belong to.
        common["min_entry_dte"] = p.get("min_entry_dte")
        common["min_credit_ratio"] = p.get("min_credit_ratio")
    if wanted == "ladder":
        return StrategyConfig(**common)
    if wanted != "hic":
        # Same reasoning as the forward path: a build that knows a name but not
        # how to trade it must say so rather than run something else.
        raise ChoiceError(
            f"This engine build cannot backtest {wanted!r}; it has no implementation."
        )

    # Two-way and centred, for the reasons the forward path forces the same:
    # the spreads follow the move, and a structure symmetric by construction
    # cannot be anchored to one side of the spot.
    common["direction"] = "both"
    common["anchor_mode"] = None
    config = HicConfig(
        **common,
        full_band_steps=int(p.get("full_band_steps", 1)),
        half_mode=str(p.get("half_mode") or "buy"),
        debit_shift=float(p.get("debit_shift") or 0.0),
        max_put_spreads=int(p.get("max_put_spreads", 10)),
        max_call_spreads=int(p.get("max_call_spreads", 10)),
    )
    return replace(
        config,
        max_down=min(p.get("max_down") or config.max_down_levels, config.max_down_levels),
        max_up=min(p.get("max_up") or config.max_up_levels, config.max_up_levels),
    )


def expected_sessions(start: dt.date, end: dt.date, now: dt.datetime | None = None) -> set[dt.date]:
    """The weekdays in a range the shipped calendar says should have traded.

    Only a first cut: the calendar lists the fixed-date holidays and nothing
    movable, so the caller narrows it with Choice's own record of which days
    traded wherever it has one. Today counts only once the session is over.
    """
    calendar = MarketCalendar.load()
    now = now or dt.datetime.now(tz=IST)
    days: set[dt.date] = set()
    day = start
    while day <= end:
        finished = day < now.date() or (day == now.date() and now.time() >= MARKET_CLOSE)
        if finished and calendar.is_trading_day(day):
            days.add(day)
        day += dt.timedelta(days=1)
    return days


def _fetch_with_backup(
    fetch_choice, name: str, days_needed: set[dt.date], resolution: str,
    backup: "groww.GrowwBackup | None",
) -> tuple[pd.DataFrame, list[dt.date], str | None, str | None]:
    """A series from Choice, with the backup source filling any needed day
    Choice skipped.

    Returns the frame, the days taken from the backup, a note on what neither
    could cover, and Choice's own error if it refused outright. An expired
    login is not a gap in the data and is raised as it is.
    """
    choice_error: str | None = None
    try:
        frame = fetch_choice()
    except ChoiceAuthError:
        raise
    except ChoiceError as exc:
        choice_error = str(exc)
        log.warning("Choice returned no %s: %s; trying the backup source", name, exc)
        frame = pd.DataFrame(columns=groww.COLUMNS)
    if frame is None:
        frame = pd.DataFrame(columns=groww.COLUMNS)
    if backup is None:
        have = set(frame["ts"].dt.date) if not frame.empty else set()
        missing = [d for d in days_needed if d not in have]
        note = (f"{len(missing)} day(s) missing from Choice; no backup source is configured"
                if missing else None)
        return frame, [], note, choice_error
    merged, taken, note = backup.fill_missing_days(frame, name, days_needed, resolution)
    return merged, taken, note, choice_error


def _choice_falls_short(
    key: tuple[dt.date, float, str],
    first_needed: dt.datetime,
    spans: dict,
    run_end: dt.date,
    staleness: dt.timedelta,
) -> bool:
    """Whether Choice's bars for a leg leave part of its life unpriced.

    No bars at all, bars that start after the leg was first needed, or bars
    that stop well before it stops mattering. Any of those is a stretch the
    model would otherwise fill, so it is worth asking the backup about.
    """
    span = spans.get(key)
    if span is None:
        return True
    _, first_bar, last_bar, _ = span
    if first_bar is None or last_bar is None:
        return True
    if first_bar > first_needed + staleness:
        return True
    expiry = key[0]
    needed_until = dt.datetime.combine(min(expiry, run_end), dt.time(15, 0), tzinfo=IST)
    return last_bar < needed_until - dt.timedelta(days=1)


class OfficialCloses(dict):
    """NIFTY's official close by day, and which of them came from the backup."""

    def __init__(self, closes: dict[dt.date, float] | None = None, backup_days=()) -> None:
        super().__init__(closes or {})
        self.backup_days: list[dt.date] = sorted(backup_days)


class BacktestRunner:
    """Runs one backtest for one user on a worker thread."""

    def __init__(
        self, market: ChoiceMarketData, job: BacktestJob, db: "Store | None" = None
    ) -> None:
        self.market = market
        self.job = job
        self.db = db

    def _calibration(self) -> dict | None:
        """The most recent stored surface fit, if it is recent enough to use.

        Read rather than fitted here: fitting needs a burst of live quote
        requests and the market to be open, neither of which a backtest can
        assume. The engine refits on demand and on login.
        """
        db = getattr(self, "db", None)
        if db is None:
            return None
        cutoff = (dt.datetime.now(tz=IST).date() - dt.timedelta(days=CALIBRATION_MAX_AGE_DAYS))
        try:
            return db.latest_calibration(not_before=cutoff.isoformat())
        except Exception:                           # noqa: BLE001
            log.exception("Could not read the stored IV calibration")
            return None

    def _official_closes(
        self,
        start: dt.date,
        end: dt.date,
        resolution: str,
        nifty: pd.DataFrame,
        expiries: list[dt.date],
        backup: "groww.GrowwBackup | None",
        notes: list[str],
    ) -> OfficialCloses:
        """NIFTY's official close on each expiry in the range.

        Choice's daily candle carries it -- it matched the exchange's close on
        every monthly expiry of 2026 -- so a daily run already holds it in its
        own bars and an intraday run fetches the daily series once. The backup
        is asked only for an expiry Choice has no daily bar for.
        """
        wanted = set(expiries)
        if not wanted:
            return OfficialCloses()
        if resolution == "D":
            daily = nifty
        else:
            self._step("settlement", 0.175, "Fetching NIFTY's official closes for settlement")
            try:
                daily = self.market.nifty(start, end, "D", strict=False)
            except ChoiceAuthError:
                raise
            except ChoiceError as exc:
                notes.append(f"Settlement: Choice refused NIFTY's daily closes ({exc})")
                daily = None
        closes: dict[dt.date, float] = {}
        if daily is not None and not daily.empty:
            for row in daily.itertuples():
                day = row.ts.date()
                if day in wanted and float(row.close) > 0:
                    closes[day] = float(row.close)
        missing = sorted(wanted - set(closes))
        from_backup: list[dt.date] = []
        if missing and backup is not None:
            found, note = backup.daily_closes(NIFTY, missing)
            closes.update(found)
            from_backup = sorted(found)
            if note:
                notes.append(f"Settlement: {note}")
        if not closes:
            # The replay names each expiry that settled on its last bar, but
            # only when it was given some closes to compare against.
            notes.append(
                "Settlement: no official NIFTY close was available, so every expiry "
                "settled at its last bar"
            )
        return OfficialCloses(closes, from_backup)

    def _step(self, stage: str, progress: float, message: str = "") -> None:
        self.job.stage = stage
        self.job.progress = progress
        self.job.message = message
        log.info("[%s] %s %.0f%% %s", self.job.job_id, stage, progress * 100, message)

    def run(self) -> None:
        job = self.job
        job.status = "running"
        try:
            self._execute()
            job.status = "done"
            self._step("complete", 1.0, "Backtest finished")
        except ChoiceError as exc:
            job.status = "error"
            job.error = str(exc)
            log.warning("[%s] Choice error: %s", job.job_id, exc)
        except Exception as exc:  # noqa: BLE001 - a worker thread must not die silently
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            log.error("[%s] failed:\n%s", job.job_id, traceback.format_exc())
        finally:
            job.finished_at = dt.datetime.now(tz=IST).isoformat()

    # ------------------------------------------------------------ the work

    def _execute(self) -> None:
        job = self.job
        p = job.params
        market = self.market

        end = dt.datetime.now(tz=IST).date()
        start = end - dt.timedelta(days=int(p["days"]))
        resolution = p["resolution"]

        # Where this run's fetch reports begin; the list belongs to the session.
        report_mark = len(getattr(market, "reports", []) or [])
        self._step("spot", 0.05, f"Fetching NIFTY {start} to {end}")
        # Choice first. The backup source is asked only about what Choice did
        # not supply: a trading day with no NIFTY or VIX bars, and -- below --
        # an option contract with no usable history.
        #
        # Which days traded comes from Choice's own daily VIX where it has
        # one: the shipped calendar lists only the fixed-date holidays, so on
        # its own it would send every Holi and Good Friday to the backup and
        # then report them missing from both.
        backup = groww.shared_backup()
        backup_notes: list[str] = []
        choice_vix = dict(market.vix_by_date(start, end))
        sessions = expected_sessions(start, end)
        if choice_vix:
            sessions = {d for d in sessions if d in choice_vix}
        nifty, nifty_backup, note, choice_error = _fetch_with_backup(
            lambda: market.nifty(start, end, resolution, strict=False),
            NIFTY, sessions, resolution, backup,
        )
        if choice_error:
            backup_notes.append(f"NIFTY: Choice refused the request ({choice_error})")
        if note:
            backup_notes.append(f"NIFTY: {note}")
        if nifty.empty:
            raise ChoiceError(
                "Choice returned no NIFTY spot data for this range"
                + (f" ({choice_error})" if choice_error else "")
                + (f", and the backup source could not fill it: {note}" if note else "")
                + ". Try a shorter range or a daily resolution."
            )
        spots = spots_from_frame(nifty)
        session_days = sorted({when.date() for when, _ in spots})

        self._step("vix", 0.12, "Checking India VIX")
        vix_map = dict(choice_vix)
        vix_daily_backup: list[dt.date] = []
        without_close = [d for d in session_days if d not in vix_map]
        if without_close:
            closes, note = (
                backup.daily_closes(INDIA_VIX, without_close) if backup is not None
                else ({}, "no backup source is configured")
            )
            vix_map.update(closes)
            vix_daily_backup = sorted(closes)
            left = len(without_close) - len(closes)
            if left:
                backup_notes.append(
                    f"India VIX: {left} day(s) with no daily close from Choice or the backup source"
                    + (f" ({note})" if note else "")
                )
        if vix_map:
            # The latest day's level, not the last one added: backup days are
            # added after Choice's and can sit anywhere in the range.
            surface = from_vix(vix_map[max(vix_map)])
            choice_days = len(vix_map) - len(vix_daily_backup)
            if not vix_daily_backup:
                vol_source = "choice:INDIAVIX"
            elif choice_days:
                vol_source = f"choice:INDIAVIX + backup ({len(vix_daily_backup)} day(s))"
            else:
                vol_source = "backup:INDIAVIX"
        else:
            surface = IVSurface(atm_vol=DEFAULT_ATM_VOL)
            vol_source = f"default:{DEFAULT_ATM_VOL:.0%}"

        # Shape from the live chain where we have it.
        #
        # Almost every leg in a historical range is modelled -- the contracts
        # were delisted -- so the surface *is* the result. Its skew and term
        # structure are measured from Choice's own currently-traded chain
        # rather than assumed; India VIX still sets the day-by-day level,
        # because that is the one thing there is real history for.
        calibration = self._calibration()
        if calibration:
            surface = replace(
                surface,
                slope=float(calibration["slope"]),
                curvature=float(calibration["curvature"]),
                term_exponent=float(calibration["term_exponent"]),
                fitted_from=int(calibration.get("observations") or 0),
            )
            vol_source += (
                f" +chain-fit({calibration['as_of_date']},"
                f"{calibration.get('observations', 0)}q,"
                f"term^{float(calibration['term_exponent']):+.2f})"
            )
            # The one number that says how much a modelled premium can be
            # trusted: how far this surface prices the condor credit from the
            # chain it was fitted to. Errors that cancel within a leg-by-leg
            # score live here, and the credit is what the strategy earns.
            err = calibration.get("credit_error")
            if err is not None:
                vol_source += f" credit{float(err):+.1%}-vs-market"

        # An explicit override always wins, so a run can be compared against
        # its own assumption rather than only against the fit.
        override = p.get("term_exponent")
        term_exponent = float(override) if override else float(surface.term_exponent)
        if override:
            surface = replace(surface, term_exponent=float(override))
            vol_source += f" override-term^{float(override):+.2f}"
        elif not calibration:
            vol_source += " flat-term"

        first_day, last_day = spots[0][0].date(), spots[-1][0].date()

        # The scrip master only lists contracts that still exist, so for any
        # range in the past its earliest expiry is *today's*. Reading it
        # directly gave every historical condor a six-month expiry and priced
        # weeklies as half-year options. Derive the calendar the exchange
        # actually ran, and keep listed contracts wherever they overlap.
        listed = market.master.expiries(NIFTY)
        try:
            expiries, derived_expiries = expiry_calendar(
                first_day, last_day + dt.timedelta(days=MAX_WEEKLY_DTE), listed,
                cadence=str(p.get("expiry_cadence") or "weekly"),
            )
        except ValueError as exc:
            raise ChoiceError(str(exc)) from exc

        # Check every derived date against the scrip master Choice published a
        # few days before it: that file lists the contract the exchange really
        # traded, holiday roll-backs included. Dated before the expiry, not on
        # it -- on the day after a contract expires it is already delisted.
        corrected: dict[dt.date, dt.date] = {}
        listed_near = getattr(market, "listed_expiries_near", None)
        if derived_expiries and listed_near is not None:
            self._step("calendar", 0.16, "Confirming expiry dates against Choice's scrip masters")
            # Past dates only: a future contract is already in today's listing,
            # and Choice has not published a master for a future day.
            past = {e for e in derived_expiries if e < dt.date.today()}
            expiries, corrected = confirm_derived_expiries(expiries, past, listed_near)
            derived_expiries = {e for e in derived_expiries if e not in corrected}
        lot_size = market.master.lot_size_for(NIFTY)

        params = BacktestParams(
            strategy=_strategy_for(p, lot_size, listed, market),
            costs=CostModel(),
            anchor_mode=p.get("anchor_mode"),
            roll_to_next_expiry=bool(p.get("roll", True)),
            label=f"NIFTY ladder {first_day}..{last_day}",
        )
        expiry_for = weekly_expiry_resolver(expiries, min_dte=1)

        # India VIX as it stood at each bar, for the premium model and the
        # entry rule alike. A daily close would price a 10:00 entry -- and
        # decide whether to open it -- on a number not known until 15:30.
        # Daily closes back up a day with no intraday bars, from that day's
        # close onwards only.
        self._step("vix", 0.17, "Fetching India VIX bars")
        vix_bars, vix_intraday_backup, note, choice_error = _fetch_with_backup(
            lambda: market.india_vix(start, end, resolution, strict=False),
            INDIA_VIX, set(session_days), resolution, backup,
        )
        if choice_error:
            backup_notes.append(f"India VIX bars: Choice refused the request ({choice_error})")
        if note:
            backup_notes.append(f"India VIX bars: {note}")
        bars_from_backup = set(vix_intraday_backup)
        closes_from_backup = set(vix_daily_backup)
        vix_lookup = VixAsOf(
            resolution,
            bars=(
                (row.ts.to_pydatetime(), float(row.close),
                 "backup" if row.ts.date() in bars_from_backup else "choice")
                for row in vix_bars.itertuples()
            ),
            daily=(
                (day, value, "backup" if day in closes_from_backup else "choice")
                for day, value in vix_map.items()
            ),
        )
        vix_series: list[float | None] | None = None
        vix_readings: dict[str, int] = {}
        if params.strategy.max_entry_vix is not None:
            aligned = vix_lookup.align([when for when, _ in spots])
            vix_series, vix_readings = aligned.values, aligned.counts

        # What NSE settles against: NIFTY's official close on each expiry.
        # Choice's daily candle carries it; the backup is asked only for an
        # expiry Choice has no daily bar for.
        settlement = self._official_closes(
            start, end, resolution, nifty,
            [e for e in expiries if first_day <= e <= last_day], backup, backup_notes,
        )

        # Pass 1 is cheap and tells us exactly which legs to fetch, instead of
        # pulling the entire option chain.
        self._step("plan", 0.18, "Planning which option legs are needed")
        _, requirements = discover_requirements(spots, params, expiry_for, vix_series)

        candles = CandlePriceProvider()
        fetched = 0
        # Today's scrip master lists nothing that has already expired, so a
        # run over past months could not resolve a single one of its legs:
        # every fetch failed, the failure was swallowed as "no data", and the
        # whole run silently priced off the model. This reaches the file from
        # a day when each contract was still listed.
        instruments = HistoricalInstruments(market.master)
        missing: list[str] = []
        # Resolved fine, and Choice returned an empty series anyway. A
        # different fact from "the contract could not be found", and until now
        # indistinguishable: an empty frame fell through `if not frame.empty`
        # with nothing logged and nothing recorded, so the only symptom was a
        # MODELED percentage nobody could account for.
        served_nothing: list[str] = []
        # For every leg that came back with bars: how many, from when to when,
        # and when the run first needed it -- the evidence for why a leg with
        # data could still end up modelled.
        spans: dict[tuple[dt.date, float, str], tuple[int, Any, Any, dt.datetime]] = {}
        total = max(1, len(requirements))
        for i, req in enumerate(requirements, 1):
            self._step(
                "legs",
                0.20 + 0.65 * (i / total),
                f"Fetching option {i} of {total} ({req.strike:g} {req.right} {req.expiry})",
            )
            try:
                frame = market.option_candles(
                    NIFTY, req.expiry, req.strike, req.right, start, end,
                    p.get("option_resolution") or resolution,
                    instruments=instruments,
                )
            except ChoiceError as exc:
                log.info("[%s] leg %s %g %s unavailable: %s", job.job_id, req.expiry, req.strike, req.right, exc)
                missing.append(f"{req.expiry} {req.strike:g}{req.right}")
                continue
            if not frame.empty:
                candles.add(req.expiry, req.strike, req.right, frame)
                fetched += 1
                spans[(req.expiry, float(req.strike), req.right)] = (
                    len(frame), frame["ts"].min(), frame["ts"].max(), req.first_needed,
                )
            else:
                served_nothing.append(f"{req.expiry} {req.strike:g}{req.right}")

        if served_nothing:
            by_expiry: dict[str, int] = {}
            for entry in served_nothing:
                by_expiry[entry.split(" ", 1)[0]] = by_expiry.get(entry.split(" ", 1)[0], 0) + 1
            log.warning(
                "[%s] Choice resolved %d legs and returned no candles for any of them: %s. "
                "These are modelled.",
                job.job_id, len(served_nothing),
                ", ".join(f"{k} ({v} legs)" for k, v in sorted(by_expiry.items())),
            )
        if missing:
            # Said plainly rather than left to be inferred from a MODELED
            # badge: "some legs were modelled" and "no real premium was found
            # for any of them" are very different results.
            log.warning(
                "[%s] %d of %d legs had no Choice premium; %d scrip master(s) consulted",
                job.job_id, len(missing), total, instruments.downloads,
            )
        # The backup source, for every leg Choice's history leaves unpriced --
        # above all an expired contract, which Choice answers with nothing.
        backup_candles = CandlePriceProvider(source=PriceSource.BACKUP)
        backup_found: list[str] = []
        short = [
            req for req in requirements
            if _choice_falls_short(
                (req.expiry, float(req.strike), req.right), req.first_needed, spans, end,
                candles.max_staleness,
            )
        ]
        if short and backup is not None:
            failures = 0
            last_error = ""
            for i, req in enumerate(short, 1):
                self._step(
                    "backup", 0.86 + 0.03 * (i / len(short)),
                    f"Fetching option {i} of {len(short)} from the backup source "
                    f"({req.strike:g} {req.right} {req.expiry})",
                )
                try:
                    frame = backup.option_candles(
                        req.expiry, req.strike, req.right,
                        req.first_needed.date() - dt.timedelta(days=1), end,
                        p.get("option_resolution") or resolution,
                    )
                except groww.BackupUnavailable as exc:
                    failures += 1
                    last_error = str(exc)
                    if failures >= 3 and not backup_found:
                        # Three refusals before a single success is a sign-in
                        # or plan problem, not a missing contract; asking about
                        # the rest would only repeat it.
                        break
                    continue
                if not frame.empty:
                    backup_candles.add(req.expiry, req.strike, req.right, frame)
                    backup_found.append(f"{req.expiry} {req.strike:g}{req.right}")
            if last_error:
                backup_notes.append(f"Option legs: the backup source failed on {failures} leg(s) ({last_error})")
        elif short:
            backup_notes.append(
                f"Option legs: {len(short)} leg(s) had no usable Choice history and no backup "
                "source is configured, so they were modelled"
            )

        self._step("replay", 0.90, "Replaying the ladder")
        provider = FallbackPriceProvider(
            primary=candles,
            secondary=backup_candles if backup_candles.candles else None,
            fallback=ModelPriceProvider(surface=surface, vix_at=vix_lookup.at),
        )
        result = Backtest(params, provider, expiry_for).run(spots, vix_series, settlement)
        if backup_notes:
            # A day neither source had is a jump in the replay; say so.
            result.warnings.extend(backup_notes)

        # Which fetched legs were actually priced from their bars. The two
        # were reported as one number, so a leg whose bars never sat near a
        # moment it was needed counted as "priced from real candles" while
        # every quote for it came from the model: on 23 Sep the banner said 52
        # of 80 legs were real, and the 20 August legs among them had not
        # priced a single quote.
        used = {k for k, n in candles.hits_by_key.items() if n > 0}
        unused = sorted(k for k in spans if k not in used)
        leg_detail = [
            {
                "expiry": k[0].isoformat(), "strike": k[1], "right": k[2],
                "bars": spans[k][0],
                "first_bar": spans[k][1].isoformat() if spans[k][1] is not None else None,
                "last_bar": spans[k][2].isoformat() if spans[k][2] is not None else None,
                "first_needed": spans[k][3].isoformat(),
            }
            for k in unused
        ]
        if unused:
            log.warning(
                "[%s] %d legs returned bars that never matched a moment they were needed: %s",
                job.job_id, len(unused),
                "; ".join(
                    f"{d['expiry']} {d['strike']:g}{d['right']}: {d['bars']} bars "
                    f"{(d['first_bar'] or '')[:16]}..{(d['last_bar'] or '')[:16]}, "
                    f"needed from {d['first_needed'][:16]}"
                    for d in leg_detail[:12]
                ),
            )

        self._step("serialise", 0.97, "Building the dashboard dataset")
        used_backup = sorted(k for k, n in backup_candles.hits_by_key.items() if n > 0)
        settled_official = sorted(e for e in settlement if e <= last_day)
        backup_used = bool(
            nifty_backup or vix_daily_backup or vix_intraday_backup or used_backup
            or settlement.backup_days
        )
        if not nifty_backup:
            spot_source = "choice:NIFTY"
        elif len(nifty_backup) >= len(session_days):
            spot_source = "backup:NIFTY"
        else:
            spot_source = f"choice:NIFTY + backup ({len(nifty_backup)} day(s))"
        if provider.modeled_quotes == 0:
            note_text = (
                "All prices sourced from Choice FinX." if not backup_used
                else "Every price is a real traded price: from Choice FinX, and where Choice "
                     "had none, from the backup source -- listed below."
            )
        else:
            note_text = (
                f"{100 * provider.modeled_quotes / max(1, provider.total_quotes):.0f}% of "
                "the price lookups in this replay came from the model rather than a real "
                "candle -- Black-76, driven by India VIX and a strike skew. The breakdown "
                "by leg is below."
            )
        if fetched and used_backup:
            premium_source = "choice:ChartData + backup"
        elif fetched:
            premium_source = "choice:ChartData"
        elif used_backup:
            premium_source = "backup"
        else:
            premium_source = "modeled:black76"
        provenance = {
            "spot_source": spot_source,
            "vol_source": vol_source,
            "term_exponent": term_exponent,
            "iv_calibration": calibration,
            "premium_source": premium_source,
            # Why a leg is modelled, split by cause, because the three are
            # different problems: one is fixed by a shorter range, one by a
            # correct expiry, and one cannot be fixed at all.
            "legs_total": total,
            "legs_real": len(used),
            # Bars came back, none of them usable -- a different fault from an
            # empty series, and the one that was being counted as real.
            "legs_unused": len(unused),
            "unused_legs": leg_detail[:40],
            "legs_empty": len(served_nothing),
            "legs_unresolved": len(missing),
            "empty_expiries": sorted({e.split(" ", 1)[0] for e in served_nothing}),
            # The scrip master delists expired contracts, so a historical run
            # necessarily rests partly on a derived calendar. Say how much.
            "expiry_source": (
                "choice:scripmaster" if not derived_expiries
                else f"derived+scripmaster ({len(derived_expiries)} of {len(expiries)} derived)"
            ),
            "expiries_derived": len(derived_expiries),
            # Derived dates the dated scrip masters showed to be wrong, and
            # what the exchange actually used -- a holiday roll-back the local
            # calendar did not know about.
            "expiries_corrected": {k.isoformat(): v.isoformat() for k, v in corrected.items()},
            "expiries_listed": len(expiries) - len(derived_expiries),
            # "Everything from Choice": no modelled premium and no backup day.
            "verified": provider.modeled_quotes == 0 and not backup_used,
            "note": note_text,
            # What came from the backup source, day by day and leg by leg. It
            # is never named: on the dashboard it is "the backup source".
            "backup": {
                "provider": "backup",
                "configured": backup is not None,
                "used": backup_used,
                "nifty_days": [d.isoformat() for d in nifty_backup],
                "vix_bar_days": [d.isoformat() for d in vix_intraday_backup],
                "vix_close_days": [d.isoformat() for d in vix_daily_backup],
                "settlement_days": [d.isoformat() for d in settlement.backup_days],
                "option_legs_asked": len(short) if backup is not None else 0,
                "option_legs_found": len(backup_found),
                "option_legs_used": len(used_backup),
                "notes": backup_notes,
            },
            # Legs priced from the backup at least once. Counted apart from the
            # Choice split above, which still says what Choice itself served.
            "legs_backup": len(used_backup),
            # Which expiries settled against the official close.
            "settlement": {
                "official_close": len(settled_official),
                "last_bar": sorted(
                    e.isoformat() for e in {c.expiry for c in result.condors}
                    if e <= last_day and e not in settlement
                ),
            },
            # How the VIX rule shaped the run; absent when the run had no limit.
            "vix_gate": (
                {**result.vix_gate, "readings": vix_readings, "resolution": resolution}
                if result.vix_gate else None
            ),
            "resolution": resolution,
            "generated_at": dt.datetime.now(tz=IST).isoformat(),
            "provider": provider.summary(),
            "bars": len(spots),
            "range": [first_day.isoformat(), last_day.isoformat()],
            "legs_requested": len(requirements),
            "legs_with_choice_data": fetched,
            "coverage": market.coverage_summary(since=report_mark),
            "failures": market.failures(since=report_mark)[:50],
            "lot_size": lot_size,
        }
        job.result = serialise(result, provenance)


class JobStore:
    """Holds the latest backtest job per user, backed by durable storage.

    In-flight jobs live in memory because they are tied to a running thread;
    finished ones are written through to SQLite so a restart does not throw
    away a result that took minutes of Choice calls to produce.
    """

    def __init__(self, db: "Store | None" = None) -> None:
        self._jobs: dict[str, BacktestJob] = {}
        self._lock = threading.Lock()
        self._counter = 0
        self._db = db

    def bind(self, db: "Store") -> None:
        """Attach durable storage. Done by the API once the DB is open."""
        self._db = db

    def start(self, market: ChoiceMarketData, user_id: str, params: dict[str, Any]) -> BacktestJob:
        with self._lock:
            existing = self._jobs.get(user_id)
            if existing and existing.status in ("queued", "running"):
                return existing
            # A uuid, not a counter. `self._counter` restarted at 0 with the
            # process, so the first backtest after every restart was "bt-1" --
            # and `save_backtest` upserts on the run id. One user's run
            # therefore overwrote another user's stored row, keeping the
            # original owner: user A opened the dashboard and was served user
            # B's backtest. It also froze `created_at` at the first run ever
            # to carry that number, so a result computed today was dated to
            # whenever the counter last started, and every re-run destroyed
            # the history rather than adding to it.
            job = BacktestJob(job_id=f"bt-{uuid.uuid4().hex}", user_id=user_id, params=params)
            self._jobs[user_id] = job

        thread = threading.Thread(
            target=self._run_and_persist, args=(market, job), name=f"backtest-{user_id}", daemon=True
        )
        thread.start()
        return job

    def _run_and_persist(self, market: ChoiceMarketData, job: BacktestJob) -> None:
        BacktestRunner(market, job, db=self._db).run()
        if self._db is None:
            return
        try:
            self._db.save_backtest(
                run_id=job.job_id, user_id=job.user_id, status=job.status,
                params=job.params, dataset=job.result, error=job.error,
                result_version=RESULT_VERSION,
            )
        except Exception:                           # noqa: BLE001
            log.exception("Could not persist backtest %s", job.job_id)

    def get(self, user_id: str) -> BacktestJob | None:
        with self._lock:
            return self._jobs.get(user_id)

    def dataset(self, user_id: str) -> dict[str, Any]:
        """The user's latest result, or an honest empty bundle."""
        job = self.get(user_id)
        # Signed in either way, so never "awaiting connection" here.
        if job is None:
            # Nothing in memory does not mean nothing ever ran: a restart
            # clears the job table but not the results it produced.
            saved = (
                self._db.latest_backtest(user_id, min_version=RESULT_VERSION)
                if self._db else None
            )
            if saved and saved.get("dataset"):
                return saved["dataset"]
            if self._db and self._db.backtest_history(user_id):
                # There is a saved result, but this engine no longer agrees
                # with how it was computed. Saying so beats showing it.
                return empty_bundle(
                    "Your last backtest was produced before a correctness fix and is no longer "
                    f"shown: {RETIRED_BECAUSE}. Run it again for corrected numbers.",
                    awaiting_connection=False,
                )
            return empty_bundle(
                "No backtest has been run on this account yet. Choose a range and run one to "
                "populate the dashboard.",
                awaiting_connection=False,
            )
        if job.status == "error":
            return empty_bundle(
                f"The last backtest failed: {job.error}", awaiting_connection=False
            )
        if job.result is None:
            return empty_bundle(
                f"A backtest is {job.status} ({job.stage}, {job.progress:.0%}). "
                "This page will fill in when it finishes.",
                awaiting_connection=False,
            )
        return job.result

    def clear(self, user_id: str) -> None:
        with self._lock:
            self._jobs.pop(user_id, None)
        if self._db is not None:
            self._db.clear_backtests(user_id)

    def history(self, user_id: str) -> list[dict[str, Any]]:
        return self._db.backtest_history(user_id) if self._db else []


store = JobStore()
