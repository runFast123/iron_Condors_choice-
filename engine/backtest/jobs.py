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
import logging
import threading
import traceback
from dataclasses import dataclass, field
from typing import Any

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
from engine.choice.errors import ChoiceError
from engine.config import IST
from engine.data.market import NIFTY, ChoiceMarketData
from engine.pricing.costs import CostModel
from engine.pricing.iv_surface import IVSurface, from_vix
from engine.strategy.condor import StrategyConfig

log = logging.getLogger(__name__)

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


class BacktestRunner:
    """Runs one backtest for one user on a worker thread."""

    def __init__(self, market: ChoiceMarketData, job: BacktestJob) -> None:
        self.market = market
        self.job = job

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

        self._step("spot", 0.05, f"Fetching NIFTY {start} to {end}")
        nifty = market.nifty(start, end, resolution)
        if nifty.empty:
            raise ChoiceError(
                "Choice returned no NIFTY spot data for this range. "
                "Try a shorter range or a daily resolution."
            )
        spots = spots_from_frame(nifty)

        self._step("vix", 0.12, "Fetching India VIX")
        vix_map = market.vix_by_date(start, end)
        if vix_map:
            surface = from_vix(list(vix_map.values())[-1])
            vol_source = "choice:INDIAVIX"
        else:
            surface = IVSurface(atm_vol=DEFAULT_ATM_VOL)
            vol_source = f"default:{DEFAULT_ATM_VOL:.0%}"

        first_day, last_day = spots[0][0].date(), spots[-1][0].date()
        expiries = market.master.expiries(NIFTY, after=first_day)
        if not expiries:
            raise ChoiceError(f"No {NIFTY} expiries in the scrip master on/after {first_day}.")
        lot_size = market.master.lot_size_for(NIFTY)

        params = BacktestParams(
            strategy=StrategyConfig(
                step=float(p["step"]),
                lots=int(p["lots"]),
                lot_size=lot_size,
                max_condors=int(p["max_condors"]),
                strike_step=market.master.strike_step(NIFTY, expiries[0]),
                take_profit_pct=p.get("take_profit"),
                stop_loss_mult=p.get("stop_loss"),
            ),
            costs=CostModel(),
            roll_to_next_expiry=bool(p.get("roll", True)),
            label=f"NIFTY ladder {first_day}..{last_day}",
        )
        expiry_for = weekly_expiry_resolver(expiries, min_dte=1)

        # Pass 1 is cheap and tells us exactly which legs to fetch, instead of
        # pulling the entire option chain.
        self._step("plan", 0.18, "Planning which option legs are needed")
        _, requirements = discover_requirements(spots, params, expiry_for)

        candles = CandlePriceProvider()
        fetched = 0
        total = max(1, len(requirements))
        for i, req in enumerate(requirements, 1):
            self._step(
                "legs",
                0.20 + 0.65 * (i / total),
                f"Fetching option {i} of {total} ({req.strike:g} {req.right} {req.expiry})",
            )
            try:
                frame = market.option_candles(
                    NIFTY, req.expiry, req.strike, req.right, start, end, p.get("option_resolution") or resolution
                )
            except ChoiceError as exc:
                log.info("[%s] leg %s %g %s unavailable: %s", job.job_id, req.expiry, req.strike, req.right, exc)
                continue
            if not frame.empty:
                candles.add(req.expiry, req.strike, req.right, frame)
                fetched += 1

        self._step("replay", 0.90, "Replaying the ladder")
        provider = FallbackPriceProvider(
            primary=candles,
            fallback=ModelPriceProvider(surface=surface, vix_by_date=vix_map or None),
        )
        result = Backtest(params, provider, expiry_for).run(spots)

        self._step("serialise", 0.97, "Building the dashboard dataset")
        provenance = {
            "spot_source": "choice:NIFTY",
            "vol_source": vol_source,
            "premium_source": "choice:ChartData" if fetched else "modeled:black76",
            "expiry_source": "choice:scripmaster",
            "verified": provider.modeled_quotes == 0,
            "note": (
                "All prices sourced from Choice FinX."
                if provider.modeled_quotes == 0
                else (
                    f"{fetched} of {len(requirements)} option legs had Choice historical data. "
                    "The rest are MODELED with Black-76 driven by Choice-sourced India VIX and a "
                    "strike skew, because Choice served no candles for those contracts."
                )
            ),
            "resolution": resolution,
            "generated_at": dt.datetime.now(tz=IST).isoformat(),
            "provider": provider.summary(),
            "bars": len(spots),
            "range": [first_day.isoformat(), last_day.isoformat()],
            "legs_requested": len(requirements),
            "legs_with_choice_data": fetched,
            "coverage": market.coverage_summary(),
            "failures": market.failures()[:50],
            "lot_size": lot_size,
        }
        job.result = serialise(result, provenance)


class JobStore:
    """Holds the latest backtest job per user."""

    def __init__(self) -> None:
        self._jobs: dict[str, BacktestJob] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def start(self, market: ChoiceMarketData, user_id: str, params: dict[str, Any]) -> BacktestJob:
        with self._lock:
            existing = self._jobs.get(user_id)
            if existing and existing.status in ("queued", "running"):
                return existing
            self._counter += 1
            job = BacktestJob(job_id=f"bt-{self._counter}", user_id=user_id, params=params)
            self._jobs[user_id] = job

        thread = threading.Thread(
            target=BacktestRunner(market, job).run, name=f"backtest-{user_id}", daemon=True
        )
        thread.start()
        return job

    def get(self, user_id: str) -> BacktestJob | None:
        with self._lock:
            return self._jobs.get(user_id)

    def dataset(self, user_id: str) -> dict[str, Any]:
        """The user's latest result, or an honest empty bundle."""
        job = self.get(user_id)
        if job is None:
            return empty_bundle(
                "No backtest has been run on this account yet. Choose a range and run one to "
                "populate the dashboard."
            )
        if job.status == "error":
            return empty_bundle(f"The last backtest failed: {job.error}")
        if job.result is None:
            return empty_bundle(
                f"A backtest is {job.status} ({job.stage}, {job.progress:.0%}). "
                "This page will fill in when it finishes."
            )
        return job.result

    def clear(self, user_id: str) -> None:
        with self._lock:
            self._jobs.pop(user_id, None)


store = JobStore()
