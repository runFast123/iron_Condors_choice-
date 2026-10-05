"""Engine HTTP API.

Runs on the machine whose static IP is declared against each user's Choice API
key. The web app never talks to Choice directly -- it calls this service, which
holds one authenticated Choice session per logged-in user.

    uvicorn engine.api:app --host 0.0.0.0 --port 8000

Security posture:

* Every route except ``/health`` and ``/auth/login`` requires a bearer token
  issued by ``/auth/login``.
* Credentials are accepted once, exchanged for a Choice session, and never
  stored, logged or echoed back.
* ``ENGINE_SHARED_SECRET``, when set, must be presented by the calling web app
  in ``X-Engine-Key``, so the engine is not an open login proxy for the
  internet even if its port is reachable.
* CORS is closed by default; set ``ENGINE_ALLOWED_ORIGINS`` to your deployment.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from dataclasses import replace
import logging
import os
import re
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal

from starlette.middleware.gzip import GZipMiddleware
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from engine.auth.sessions import UserSession, registry, set_id_salt
from engine.backtest.jobs import store as backtest_store
from engine.choice.netinfo import egress_ip
from engine.choice.errors import (
    ChoiceAuthError,
    ChoiceError,
    ChoiceInstrumentError,
    StaticIpRejectedError,
)
from engine.config import IST, engine_config
from engine.backtest.jobs import CALIBRATION_MAX_AGE_DAYS
from engine.data.expiry_calendar import nearest_listed_expiry
from engine.pricing.calibrate import calibrate
from engine.data.market import NIFTY, ChoiceMarketData
from engine.forward.runner import (
    ForwardRunner,
    UnsupportedStateVersion,
    market_calendar,
    market_is_open,
    settings_from_state,
)
from engine.store.db import DEFAULT_RUN_KEY, HIC, LADDER, STRATEGIES, Store
from engine.pricing.costs import CostModel
from engine.strategy.condor import StrategyConfig
from engine.strategy.hic import HicConfig

log = logging.getLogger(__name__)


def _log_to_file() -> None:
    """Keep the engine's own log on disk.

    It only ever went to stdout, which the Windows supervisor discards -- so
    when a forward run went quiet mid-session there was no record of why, and
    the only evidence left was the run's own event list. Rotating, because an
    engine left running for a month should not fill the disk.
    """
    path = Path(os.environ.get("ENGINE_LOG", "engine/state/logs/api.log"))
    root = logging.getLogger()
    if any(getattr(h, "_engine_file_log", False) for h in root.handlers):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    except OSError:
        # A read-only or missing directory must not stop the engine booting.
        log.warning("Could not open %s for logging; continuing to stdout only", path)
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler._engine_file_log = True                  # type: ignore[attr-defined]
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("ENGINE_ALLOWED_ORIGINS", "").split(",") if o.strip()
]

# Durable state. Created before anything can log in, because the user-id salt
# lives here: derive an id with a fresh salt and every run this user saved
# earlier becomes unreachable under an id that no longer resolves.
store = Store()
set_id_salt(store.user_id_salt())
backtest_store.bind(store)
# Sessions outlive the process. Without this every restart -- a crash, a
# reboot, a deploy -- signs every user out mid-run, which on a multi-user
# platform drops the dashboard to a login page while a ladder is in flight.
registry.bind_storage(store, engine_config.shared_secret or None)
# Holidays learned from Choice in earlier sessions, so a restart does not have
# to rediscover that today is Diwali.
market_calendar.learned |= store.holidays()
market_calendar.on_learn = store.add_holiday

@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI):
    # Only a running server writes the operational log.
    #
    # This used to install at import, which meant the test suite and any
    # one-off script that imports this module appended to the file an operator
    # reads during an incident -- including, from a script with no credentials
    # in its environment, "No ENGINE_SHARED_SECRET, sessions are not
    # persisted". That line is true of the script and alarming about the
    # engine, and I lost time to it myself.
    _log_to_file()
    # Said here rather than only at import, so the fact is in the file the
    # warning used to be in, and is about this process rather than some other.
    log.info(
        "Sessions are %s",
        "durable" if engine_config.shared_secret
        else "in memory only (no ENGINE_SHARED_SECRET): a restart signs users out",
    )
    registry.on_session_restored = _resume_forward
    # Immediately, not only after the first interval: a restart mid-session is
    # exactly when a run sits in the database with no worker behind it.
    threading.Thread(target=_watchdog_pass, name="forward-watchdog-boot", daemon=True).start()
    _start_watchdog()
    # Keeps nifty.db current: each trading evening's one-minute option bars.
    from engine.data import history_job

    history_job.start(registry)
    yield


app = FastAPI(
    title="Iron Condor Ladder engine", version="1.0.0",
    docs_url=None, redoc_url=None, lifespan=_lifespan,
)

# Every response over a kilobyte goes compressed. The dashboard talks to this
# engine from Vercel through a tunnel, and a run's state (60-70 KB, polled every
# ten seconds) or a backtest's dataset (megabytes at five-minute bars) crossed it
# as plain JSON; both shrink about sevenfold.
app.add_middleware(GZipMiddleware, minimum_size=1000)

if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )


# --------------------------------------------------------------------- models


class LoginRequest(BaseModel):
    vendor_id: str = Field(min_length=1, max_length=200)
    api_key: str = Field(min_length=1, max_length=4000)
    mobile: str = Field(min_length=6, max_length=20)


class RunBacktestRequest(BaseModel):
    strategy: str = Field(default=LADDER, pattern=f"^({'|'.join(STRATEGIES)})$")
    # HIC only; the ladder has no band and buys no spreads.
    full_band_steps: int = Field(default=1, ge=0, le=10)
    half_mode: str = Field(default="buy", pattern="^(buy|sell)$")
    debit_shift: float = Field(default=0.0, ge=0, le=1000)
    max_put_spreads: int = Field(default=10, ge=0, le=100)
    max_call_spreads: int = Field(default=10, ge=0, le=100)
    days: int = Field(default=120, ge=5, le=3650)
    resolution: str = Field(default="D", pattern="^(1|5|10|15|30|60|D|W|M)$")
    option_resolution: str | None = Field(default=None, pattern="^(1|5|10|15|30|60|D|W|M)$")
    lots: int = Field(default=1, ge=1, le=100)
    step: float = Field(default=100.0, gt=0, le=5000)
    max_condors: int = Field(default=20, ge=1, le=100)
    take_profit: float | None = Field(default=None, gt=0, le=1)
    stop_loss: float | None = Field(default=None, gt=0, le=20)
    trailing_sl: float | None = Field(default=None, gt=0, le=20)
    trailing_sl_trigger: float | None = Field(default=None, ge=0, le=20)
    # Ladder entry filters. Ignored for HIC. Off unless set.
    min_entry_dte: int | None = Field(default=None, ge=0, le=45)
    min_credit_ratio: float | None = Field(default=None, gt=0, lt=1)
    # No new positions while India VIX is above this; both strategies. On at
    # 15 unless the request says otherwise -- null switches it off.
    max_entry_vix: float | None = Field(default=15.0, gt=0, le=100)
    roll: bool = True
    # Weekly or monthly contracts.
    expiry_cadence: str = Field(default="monthly", pattern="^(weekly|monthly)$")
    # Which way the ladder ladders. Down-only is the strategy as it has always
    # run and stays the default: the up side ships off until a backtest
    # justifies turning it on.
    direction: str = Field(default="down", pattern="^(down|up|both)$")
    # Per-side caps. None means only max_condors binds. The rally side starts
    # smaller because its credits are usually thinner for the same 200-point
    # risk -- NIFTY implied vol tends to fall on the way up.
    max_down: int | None = Field(default=None, ge=0, le=100)
    max_up: int | None = Field(default=None, ge=0, le=100)
    anchor_mode: str | None = Field(default=None, pattern="^(floor|round|nearest|explicit)$")

    # Term structure of the modelled IV surface.
    #
    # 0.0 is a FLAT term structure: every tenor is priced off the 30-day India
    # VIX. That is the conservative default, but it is not neutral -- it is
    # known to be wrong for the weeklies this ladder trades, and wrong in one
    # direction. At NIFTY 24,000 with VIX 14 the modelled condor credit comes
    # out at Rs1,372 for 1 DTE against Rs6,020 at -0.25 and Rs8,383 at -0.40.
    # Left as a knob rather than a new default, because picking a number here
    # silently improves every reported result, and that is the user's call to
    # make, not this engine's.
    term_exponent: float = Field(default=0.0, ge=-1.0, le=1.0)


# Which strategy a request is about. A defaulted query parameter rather than a
# path segment on purpose: the engine and the Vercel dashboard deploy
# independently, and /forward/stop is posted today with no body and no
# arguments by two live users. A default keeps that working through the
# rollout; a path change would break it the moment the engine shipped first.
# How many forward tests one user may drive at once.
#
# Not arbitrary: the Choice rate limit is per user, and every run polls. Five
# is enough to compare a handful of parameter sets side by side and stays well
# inside the budget now that runs share quotes. A cap that says why it was
# reached beats an unbounded one that quietly slows every run down.
MAX_RUNS_PER_USER = int(os.environ.get("ENGINE_MAX_RUNS", "5") or 5)

# The strategies this build has an implementation for.
#
# Deliberately separate from db.STRATEGIES, which is the set of names the
# database may hold -- including ones written by a newer build than this one.
# Conflating "a name I recognise" with "a strategy I can trade" is how a run
# came to be recorded as HIC while trading a down-only ladder.
RUNNABLE_STRATEGIES = (LADDER, HIC)

_RUN_KEY_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")


def slugify_run_key(name: str) -> str:
    """A short, URL-safe name for a run. Raises if nothing usable is left."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:24].strip("-")
    if not _RUN_KEY_OK.match(slug):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{name!r} is not a usable run name. Use letters and digits.",
        )
    return slug


def run_param(run: str = DEFAULT_RUN_KEY) -> str:
    """Which of the user's runs a request is about.

    Defaulted, so a dashboard that predates named runs keeps addressing the
    one it always did -- the ladder run is named "ladder".
    """
    return slugify_run_key(run)


class StartForwardRequest(BaseModel):
    strategy: str = Field(default=LADDER, pattern=f"^({'|'.join(STRATEGIES)})$")
    # What to call this run. Defaults to the strategy, which is what a single
    # run has always been addressed as. Give two runs different names to
    # compare them on the same live ticks.
    name: str | None = Field(default=None, max_length=40)
    # This run's own kill switch, in rupees. Defaults to the engine-wide
    # figure. Per run so one runaway test cannot stop the others; the total
    # across a user's runs is checked separately.
    daily_loss_limit: float | None = Field(default=None, gt=0, le=10_000_000)

    # --- HIC only. Ignored by the ladder, which has no band and no spreads.
    #
    # Steps either side of the anchor that open a full condor rather than a
    # spread. 0 makes only the anchor a condor.
    full_band_steps: int = Field(default=1, ge=0, le=10)
    # "buy" is the strategy. "sell" is the opposite reading, kept so a run can
    # price the comparison the document calls H4.
    half_mode: str = Field(default="buy", pattern="^(buy|sell)$")
    # 0 reverses the condor's own strikes at that level; 200 buys at the level
    # itself, which costs more and starts paying sooner.
    debit_shift: float = Field(default=0.0, ge=0, le=1000)
    max_put_spreads: int = Field(default=10, ge=0, le=100)
    max_call_spreads: int = Field(default=10, ge=0, le=100)
    lots: int = Field(default=1, ge=1, le=100)
    step: float = Field(default=100.0, gt=0, le=5000)
    poll_seconds: float = Field(default=15.0, ge=5, le=300)
    max_condors: int = Field(default=20, ge=1, le=100)
    # Must match whatever the backtest used, or the forward run is testing a
    # different strategy from the one that justified it.
    #
    # Monthly by default. Runs saved before this was persisted still resume on
    # weeklies, which is what they actually traded -- changing that under them
    # would silently make a resumed run a different strategy.
    expiry_cadence: str = Field(default="monthly", pattern="^(weekly|monthly)$")
    # Which way the ladder ladders. Down-only is the strategy as it has always
    # run and stays the default: the up side ships off until a backtest
    # justifies turning it on.
    direction: str = Field(default="down", pattern="^(down|up|both)$")
    # Per-side caps. None means only max_condors binds. The rally side starts
    # smaller because its credits are usually thinner for the same 200-point
    # risk -- NIFTY implied vol tends to fall on the way up.
    max_down: int | None = Field(default=None, ge=0, le=100)
    max_up: int | None = Field(default=None, ge=0, le=100)
    anchor_mode: str | None = Field(default=None, pattern="^(floor|round|nearest|explicit)$")

    # Without these the forward runner has no exit at all: `exit_signal`
    # returns None when both are unset, so `_close` is unreachable and every
    # condor is held to expiry regardless of what was backtested. A ladder
    # backtested with a 50% take-profit was then forward-tested as a different
    # strategy -- exactly the divergence this engine exists to prevent.
    take_profit: float | None = Field(default=None, gt=0, le=1)
    stop_loss: float | None = Field(default=None, gt=0, le=20)
    trailing_sl: float | None = Field(default=None, gt=0, le=20)
    trailing_sl_trigger: float | None = Field(default=None, ge=0, le=20)
    # Ladder entry filters. Ignored for HIC. Off unless set.
    min_entry_dte: int | None = Field(default=None, ge=0, le=45)
    min_credit_ratio: float | None = Field(default=None, gt=0, lt=1)
    # No new positions while India VIX is above this; both strategies. On at
    # 15 for every new run unless the request says otherwise -- null switches
    # it off. Runs already trading keep the rules they started with.
    max_entry_vix: float | None = Field(default=15.0, gt=0, le=100)
    # The time frame the ladder acts on, in minutes: a level fires on the close
    # of a bar this long, as a backtest at that bar size decides it. 1 acts on
    # every minute's price, which is how every run worked before this existed.
    bar_minutes: Literal[1, 5, 15, 30, 60] = 1


# ----------------------------------------------------------------- dependencies


def check_engine_key(x_engine_key: str | None = Header(default=None)) -> None:
    """Gate the whole API behind a shared secret, when one is configured."""
    expected = engine_config.shared_secret
    if not expected:
        return
    if not x_engine_key or not _constant_eq(x_engine_key, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid engine key")


def _constant_eq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode(), b.encode())


def current_user(authorization: str | None = Header(default=None)) -> UserSession:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    try:
        return registry.require(token)
    except ChoiceAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc


def user_market(session: UserSession = Depends(current_user)) -> ChoiceMarketData:
    """Lazily attach market data to a session (the scrip master is expensive)."""
    if session.market is None:
        try:
            session.market = ChoiceMarketData.connect(session.choice)
        except ChoiceError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return session.market


# --------------------------------------------------------------------- routes


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "time": dt.datetime.now(tz=IST).isoformat(),
        "market_open": market_is_open(),
        "sessions": registry.count,
        "requires_engine_key": bool(engine_config.shared_secret),
    }


@app.get("/client_ip", dependencies=[Depends(check_engine_key)])
def client_ip(request: Request) -> dict[str, Any]:
    """The address Choice will see, so the user knows what to declare.

    Note which IP this is. Choice enforces its allowlist on the TCP source
    address of the connection, which is *this engine's* egress IP -- not the
    browser's, and not anything a forwarded header can claim. Reporting the
    caller's address here would tell the user to register the wrong thing.
    """
    info = egress_ip.get()
    return {
        "engine_egress_ip": info["ip"],
        "source": info["source"],
        "note": info["note"],
        "caller_ip": request.client.host if request.client else None,
        "declare_this_with_choice": info["ip"],
        "hint": (
            "Register this address at finx.choiceindia.com -> Profile -> Settings -> "
            "Generate API Key. Requests from any other IP are rejected, and a VPN or "
            "proxy will always fail the check."
        ),
    }


@app.get("/status", dependencies=[Depends(check_engine_key)])
def status_endpoint(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Booleans only -- never a credential, a session id or a token.

    Deliberately answers three different questions separately, because they
    need three different fixes and collapsing them into one "not authorised"
    leaves the user guessing:

      * ``engine_reachable``  -- is the static-IP machine up at all?
      * ``has_session``       -- is this caller signed in?
      * ``session_expired``   -- was there a session that has since lapsed?
    """
    token = (
        authorization[7:].strip()
        if authorization and authorization.lower().startswith("bearer ")
        else None
    )
    session = registry.get(token)
    return {
        "engine_reachable": True,
        "market_open": market_is_open(),
        "has_token": bool(token),
        "has_session": session is not None,
        # A token that no longer resolves means it lapsed or was replaced.
        "session_expired": bool(token) and session is None,
        "logged_in": session is not None,
        "user_id": session.user_id if session else None,
        "mobile": session.mobile_masked if session else None,
        "vendor_id": session.vendor_id if session else None,
        "expires_at": session.expires_at.isoformat() if session else None,
        "has_market_data": bool(session and session.market is not None),
        # What this build can actually trade, not merely name. A dashboard
        # newer than its engine would otherwise offer a strategy the engine
        # will refuse, and the refusal is better surfaced before the click.
        "strategies": sorted(RUNNABLE_STRATEGIES),
        "forward_running": bool(session and session.runners()),
        "forward_strategies": sorted(session.runners()) if session else [],
        "engine_egress_ip": egress_ip.get()["ip"],
        "active_sessions": registry.count,
    }


@app.post("/auth/login", dependencies=[Depends(check_engine_key)])
def login(body: LoginRequest, request: Request) -> dict[str, Any]:
    """Exchange Choice credentials for an engine session token.

    The credentials are used once to establish a Choice session and are then
    held only in memory for the life of that session.
    """
    try:
        session = registry.login(body.vendor_id, body.api_key, body.mobile)
    except StaticIpRejectedError as exc:
        # Worth its own status: the credentials may be perfectly valid and the
        # only problem is where the request came from.
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ChoiceAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    except ChoiceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    log.info("Login from %s for user %s", request.client.host if request.client else "?", session.user_id)
    resumed = _resume_forward(session)
    _refresh_calibration_if_stale(session)
    return {"token": session.token, "user": session.public(), "resumed_forward": resumed}


def _resume_rank(row: dict) -> tuple[int, str, str]:
    """How much a saved run deserves to be the one that gets resumed.

    "Newest" is the wrong way to choose. An engine restart can leave a user with
    two rows marked running: the one that has been trading all day, and an empty
    one minted seconds later when their browser reconnected and started a run
    before the first had been picked up. Ranking by started_at hands it to the
    empty one and destroys the real ladder, open positions and all.

    So a run holding open condors wins outright, then the one that ticked most
    recently, and only then the newest. Sorted ascending, the winner is last.
    """
    state = row.get("state") or {}
    condors = state.get("condors") or []
    has_open = any(c.get("status") == "OPEN" for c in condors)
    return (1 if has_open else 0, str(state.get("last_tick") or ""), row["started_at"])


_revive_locks: dict[str, threading.RLock] = {}
_revive_locks_guard = threading.Lock()


def _user_revive_lock(user_id: str) -> threading.RLock:
    """One lock per user, guarding everything that can attach a runner.

    Resumption now has three entry points -- a login, a session rebuilt from a
    cookie, and the watchdog -- and they run on different threads. Two of them
    racing would each restore the same saved run and start a worker on it: two
    ladders driving one run id, both writing the whole state, last writer wins.
    """
    with _revive_locks_guard:
        return _revive_locks.setdefault(user_id, threading.RLock())


def _resume_forward(session: UserSession) -> bool:
    """Pick a forward run back up and start driving it again.

    A run needs Choice credentials to quote, and those are never persisted in a
    form any process can use without the session vault -- so resumption happens
    when a session exists, whether that came from a login, a remembered cookie,
    or the watchdog reviving one from storage. Until then the run sits in the
    database marked running, which is the truth: it has positions open and a
    ladder mid-flight, it simply has nobody to ask for prices.
    """
    with _user_revive_lock(session.user_id):
        return _resume_forward_locked(session)


def _resume_forward_locked(session: UserSession, *, only: str | None = None) -> bool:
    """Resume one saved run per strategy, never one per user.

    The grouping is the whole point. This used to rank every running row a user
    had against each other and retire all but the winner -- which, the moment a
    user runs two strategies, silently destroys a live book with open positions
    because it belongs to the other one. Runs only ever compete with their own
    kind.
    """
    try:
        pending = [r for r in store.running_forwards() if r["user_id"] == session.user_id]
    except Exception:                               # noqa: BLE001
        log.exception("Could not read saved forward runs")
        return False

    by_run: dict[str, list[dict]] = {}
    for row in pending:
        run_key = row.get("run_key") or row.get("strategy_id") or LADDER
        if only is not None and run_key != only:
            continue
        by_run.setdefault(run_key, []).append(row)

    resumed = False
    for run_key, rows in by_run.items():
        if session.runner_for(run_key) is not None:
            continue                                # already has a driver
        resumed |= _resume_one(session, run_key, rows)
    return resumed


_RESUME_WAIT_NOTED: dict[tuple[str, str], float] = {}
_RESUME_WAIT_EVERY = 1800.0
#: Why each run that should be ticking is waiting instead, by (user, run).
_RESUME_WAITING: dict[tuple[str, str], str] = {}
#: Said for a waiting run before the watchdog has given its own reason.
WAITING_REASON = (
    "It is waiting for a Choice session to resume. The engine renews the session by itself on the next "
    "trading morning and the run carries on, positions and all; signing in on the dashboard renews it now "
    "(one OTP)."
)


def _note_resume_wait(user_id: str, run_key: str, reason: str) -> None:
    now = time.monotonic()
    key = (user_id, run_key)
    _RESUME_WAITING[key] = reason
    if now - _RESUME_WAIT_NOTED.get(key, -_RESUME_WAIT_EVERY) >= _RESUME_WAIT_EVERY:
        _RESUME_WAIT_NOTED[key] = now
        log.info("Run %s for %s waits to resume: %s", run_key, user_id, reason)


def _resume_one(session: UserSession, run_key: str, rows: list[dict]) -> bool:
    # Only one run of a strategy can be driven at a time, so its siblings --
    # and only its siblings -- are retired.
    ordered = sorted(rows, key=_resume_rank)
    record = ordered[-1]
    for stale in ordered[:-1]:
        stale_state = stale.get("state") or {}
        stale_open = sum(
            1 for c in (stale_state.get("condors") or []) if c.get("status") == "OPEN"
        )
        log.warning(
            "Superseded run %s (%s) for user %s (%d open condor(s)); retiring it",
            run_key, stale["session_id"], session.user_id, stale_open,
        )
        try:
            store.mark_stopped(
                stale["session_id"],
                "superseded; another run of this user's was resumed instead",
            )
        except Exception:                           # noqa: BLE001
            log.exception("Could not retire superseded run %s", stale["session_id"])

    # The scrip master is expensive, which is why it is normally attached
    # lazily on first use -- but a run cannot be resumed without it, and the
    # cost is only paid by users who actually have a run waiting.
    if session.market is None:
        try:
            session.market = ChoiceMarketData.connect(session.choice)
        except ChoiceAuthError as exc:
            # Waiting for a session -- before 08:00, after a sign-out, or with
            # the day's automatic logins used -- is expected and not a fault.
            # The watchdog asks every minute; say so once every half hour.
            _note_resume_wait(session.user_id, run_key, str(exc))
            return False
        except ChoiceError:
            log.exception("Could not attach market data to resume %s", record["session_id"])
            return False
    market = session.market
    try:
        runner = ForwardRunner.restore(
            record["state"], market=market, costs=CostModel(),
            state_path=_state_path(session, run_key), store=store,
            session_id=record["session_id"], user_id=session.user_id,
            strategy_id=record.get("strategy_id") or LADDER,
            run_key=run_key, run_label=record.get("run_label") or "",
        )
    except UnsupportedStateVersion:
        # Only an explicitly unsupported version is retired. `ValueError` was
        # far too wide a net: StrategyConfig validation, every enum lookup and
        # every float() in restore() raise it, so one malformed byte
        # permanently abandoned a run with open positions.
        log.exception("Saved forward run %s is not a supported version", record["session_id"])
        store.mark_stopped(record["session_id"], "saved state is not a supported version")
        return False
    except Exception:                               # noqa: BLE001
        # Anything else -- a bug, a transient failure -- must not silently
        # retire a run that still has positions open. Leave it stored and let
        # a later login (or a fixed build) pick it up.
        log.exception("Could not resume forward run %s; leaving it saved", record["session_id"])
        return False

    # Any run that was live before is stale by definition, so re-open it rather
    # than leaving a stopped reason from the shutdown hanging around.
    runner.stopped_reason = None
    session.set_runner(run_key, runner)
    runner.emit(
        "info", "Forward run resumed after an engine restart",
        condors=len([c for c in runner.condors if c.is_open]),
        fired=len(runner.ladder.fired_levels),
    )
    _start_tick_thread(runner, session, poll_seconds=15.0)
    log.info(
        "Resumed run %s (%s) for user %s", run_key, record["session_id"], session.user_id
    )
    return True


def _start_tick_thread(runner: ForwardRunner, session: UserSession, poll_seconds: float) -> None:
    """Drive `runner` on a worker thread, and remember which thread that is.

    The handle is what lets the watchdog tell a run that is ticking from one
    that merely says it is. Without it a dead worker was indistinguishable from
    a quiet market, which is how a ladder sat frozen through a 100-point
    decline with every surface reporting it live.
    """
    existing = getattr(runner, "tick_thread", None)
    if existing is not None and existing.is_alive():
        return
    poll = max(5.0, float(poll_seconds))
    thread = threading.Thread(
        target=runner.run,
        kwargs={"poll_seconds": poll},
        name=f"forward-{session.user_id}-{runner.run_key}",
        daemon=True,
    )
    runner.tick_thread = thread
    runner.poll_seconds = poll
    thread.start()


WATCHDOG_SECONDS = float(os.environ.get("ENGINE_WATCHDOG_SECONDS", "60") or 60)


def _revive_run(session: UserSession, run_key: str = DEFAULT_RUN_KEY) -> bool:
    """Make sure one saved run is actually being driven.

    Three states have to be told apart, and conflating them is what let a
    ladder sit frozen through a 100-point decline:

    * no runner in memory -- resume it from the database;
    * a runner whose worker died or was suspended -- start a new worker on the
      same object, keeping the ladder, the fills and the open positions;
    * a runner that is ticking -- leave it alone.
    """
    with _user_revive_lock(session.user_id):
        runner = session.runner_for(run_key)
        if runner is None:
            return _resume_forward_locked(session, only=run_key)
        if runner.stopped_reason or runner.is_ticking:
            return False
        runner.unsuspend()
        runner.emit("warn", "Forward run had stopped ticking; restarting its worker")
        log.warning(
            "Restarting a dead worker for %s/%s (run %s)",
            session.user_id, run_key, runner.session_id,
        )
        _start_tick_thread(runner, session, runner.poll_seconds)
        return True


def _watchdog_pass() -> None:
    """One sweep: every run the database calls running must really be ticking.

    Runs marked running are the source of truth, not the in-memory session
    table, because the whole failure mode is memory and database disagreeing.
    Credentials come from storage, so a run keeps laddering through a restart
    or a closed browser without waiting for anyone to sign in.
    """
    try:
        rows = store.running_forwards()
    except Exception:                               # noqa: BLE001
        log.exception("Watchdog could not read saved forward runs")
        return
    wanted: dict[str, set[str]] = {}
    for row in rows:
        key = row.get("run_key") or row.get("strategy_id") or LADDER
        wanted.setdefault(row["user_id"], set()).add(key)

    for user_id, run_keys in wanted.items():
        try:
            session = registry.revive_for_user(user_id)
            if session is None:
                # No stored credentials: the run stays marked running, which is
                # true. It has positions open and simply nobody to quote it.
                continue
        except Exception:                           # noqa: BLE001
            log.exception("Watchdog could not revive a session for %s", user_id)
            continue
        for run_key in sorted(run_keys):
            try:
                _revive_run(session, run_key)
            except Exception:                       # noqa: BLE001
                # One run failing must not cost the user the others.
                log.exception("Watchdog failed for %s/%s", user_id, run_key)
        try:
            _enforce_account_loss_limit(session)
        except Exception:                           # noqa: BLE001
            log.exception("Could not check the account loss limit for %s", user_id)


def _enforce_account_loss_limit(session: UserSession) -> bool:
    """Halt every one of a user's runs for the day if their combined loss
    today breaches the account limit.

    Each run carries its own daily limit, so one runaway test cannot take the
    others down. That leaves the sum unbounded -- five runs each stopping at
    the limit is five times the intended worst case -- which is what this
    catches. Measured on today's P&L, like the per-run limit, and answered the
    same way: no new positions today, open ones held to settle. It used to
    compare the runs' whole P&L since they started and stop them outright.
    """
    runners = [r for r in session.runners().values() if r.stopped_reason is None]
    if len(runners) < 2:
        return False                     # a single run polices itself

    limit = abs(engine_config.account_loss_limit)
    today = dt.datetime.now(tz=IST).date()
    total = 0.0
    for runner in runners:
        risk = runner.snapshot().get("risk") or {}
        total += float(risk.get("day_pnl") or 0.0)
    if total > -limit:
        return False

    fresh = [r for r in runners if not r.entries_halted(today)]
    if not fresh:
        return False
    message = (
        f"Account loss limit hit: today's P&L across {len(runners)} runs is {total:,.0f} "
        f"against a limit of {limit:,.0f}. No new positions in any run for the rest of today; "
        "open positions are held and settle as usual."
    )
    log.error("Account loss limit for %s: %s", session.user_id, message)
    for runner in fresh:
        runner.halt_entries(today, message, day_pnl=round(total, 2))
        runner.save()
    return True


def _start_watchdog() -> None:
    if WATCHDOG_SECONDS <= 0:
        log.info("Forward-run watchdog disabled")
        return

    def loop() -> None:
        while True:
            time.sleep(WATCHDOG_SECONDS)
            _watchdog_pass()

    threading.Thread(target=loop, name="forward-watchdog", daemon=True).start()
    log.info("Forward-run watchdog every %.0fs", WATCHDOG_SECONDS)


@app.post("/auth/logout", dependencies=[Depends(check_engine_key)])
def logout(authorization: str | None = Header(default=None)) -> dict[str, bool]:
    token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None
    return {"ok": registry.logout(token)}


@app.get("/me", dependencies=[Depends(check_engine_key)])
def me(session: UserSession = Depends(current_user)) -> dict[str, Any]:
    return {"user": session.public(), "market_open": market_is_open()}


@app.get("/market/spot", dependencies=[Depends(check_engine_key)])
def spot(market: ChoiceMarketData = Depends(user_market)) -> dict[str, Any]:
    try:
        contract = market.master.index(NIFTY)
        ltp = market.ltp(contract)
    except ChoiceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {"symbol": NIFTY, "token": contract.token, "ltp": ltp,
            "ts": dt.datetime.now(tz=IST).isoformat()}


@app.get("/market/expiries", dependencies=[Depends(check_engine_key)])
def expiries(market: ChoiceMarketData = Depends(user_market)) -> dict[str, Any]:
    try:
        found = market.master.expiries(NIFTY, after=dt.datetime.now(tz=IST).date())
        lot = market.master.lot_size_for(NIFTY)
    except ChoiceInstrumentError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {"expiries": [e.isoformat() for e in found[:12]], "lot_size": lot}


# ------------------------------------------------------------------ backtest


@app.post("/backtest/run", dependencies=[Depends(check_engine_key)])
def backtest_run(
    body: RunBacktestRequest,
    session: UserSession = Depends(current_user),
    market: ChoiceMarketData = Depends(user_market),
) -> dict[str, Any]:
    """Start a backtest for this user.

    Runs on a worker thread: fetching historical candles for every option leg
    takes minutes against a real broker, which cannot be a blocking request.
    A second call while one is running returns the run in progress rather than
    starting a competing one.
    """
    job = backtest_store.start(market, session.user_id, body.model_dump())
    return {"ok": True, "job": job.public()}


@app.get("/backtest/status", dependencies=[Depends(check_engine_key)])
def backtest_status(session: UserSession = Depends(current_user)) -> dict[str, Any]:
    job = backtest_store.get(session.user_id)
    return {"job": job.public() if job else None}


@app.get("/backtest/dataset", dependencies=[Depends(check_engine_key)])
def backtest_dataset(
    run_id: str | None = None,
    session: UserSession = Depends(current_user),
) -> dict[str, Any]:
    """This user's latest result, or a specific past run by run_id, or an honest empty bundle explaining why not."""
    return backtest_store.dataset(session.user_id, run_id=run_id)


@app.get("/backtest/history", dependencies=[Depends(check_engine_key)])
def backtest_history(
    limit: int = 30,
    session: UserSession = Depends(current_user),
) -> dict[str, Any]:
    """Past runs, which now outlive the process that produced them."""
    return {"runs": backtest_store.history(session.user_id, limit=limit)}


@app.post("/backtest/clear", dependencies=[Depends(check_engine_key)])
def backtest_clear(session: UserSession = Depends(current_user)) -> dict[str, bool]:
    backtest_store.clear(session.user_id)
    return {"ok": True}


# ------------------------------------------------------------ forward testing


def _refresh_calibration_if_stale(session: UserSession) -> None:
    """Fit the IV surface from the live chain, at most once a day.

    On login rather than on demand, because fitting needs a Choice session and
    an open market, and because a feature nobody remembers to trigger is a
    feature that never runs. Failure is silent by design: a backtest falls back
    to the flat surface and says so in its provenance.
    """
    today = dt.datetime.now(tz=IST).date().isoformat()
    try:
        if store.latest_calibration(not_before=today):
            return
    except Exception:                               # noqa: BLE001
        log.exception("Could not read the stored calibration")
        return

    market = session.market
    if market is None:
        # Deliberately not connecting here. Attaching market data downloads and
        # parses the scrip master, which does not belong on the login path --
        # and assigning it from a background thread would race with the request
        # that is already resolving `user_market`. The next login after any
        # market call will have it, or /calibration/refresh can be asked
        # directly.
        log.debug("Skipping calibration: no market data attached yet")
        return

    def worker() -> None:
        try:
            result = calibrate(market)
            if result is None:
                log.info("Chain calibration produced nothing usable today")
                return
            store.save_calibration(today, result.summary())
            log.info("Stored IV calibration for %s: %s", today, result.summary())
        except Exception:                           # noqa: BLE001
            log.exception("Chain calibration failed")

    threading.Thread(target=worker, name="calibrate", daemon=True).start()


@app.get("/calibration", dependencies=[Depends(check_engine_key)])
def read_calibration(_: UserSession = Depends(current_user)) -> dict[str, Any]:
    """The stored surface fit, and how old it is."""
    fit = store.latest_calibration()
    return {"calibration": fit, "stale_after_days": CALIBRATION_MAX_AGE_DAYS}


@app.post("/calibration/refresh", dependencies=[Depends(check_engine_key)])
def refresh_calibration(
    session: UserSession = Depends(current_user),
    market: ChoiceMarketData = Depends(user_market),
) -> dict[str, Any]:
    """Refit now, from the chain Choice is quoting at this moment."""
    result = calibrate(market)
    if result is None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Could not fit the surface: Choice returned too few usable option quotes. "
            "This needs an open market and a live chain.",
        )
    today = dt.datetime.now(tz=IST).date().isoformat()
    store.save_calibration(today, result.summary())
    return {"ok": True, "calibration": result.summary()}


def _strategy_config(
    body: "StartForwardRequest", *, lot_size: int, strike_step: float
) -> StrategyConfig:
    """The geometry a run will trade, of whichever strategy's shape.

    HIC's config is a subclass, so everything downstream -- the trigger, the
    leg builder, the cost model, the persistence round-trip -- takes one and
    none of them need to know which they were handed.
    """
    common = dict(
        step=body.step, lots=body.lots, lot_size=lot_size,
        max_condors=min(body.max_condors, engine_config.max_condors),
        direction=body.direction,
        anchor_mode=body.anchor_mode,
        max_down=body.max_down,
        max_up=body.max_up,
        strike_step=strike_step,
        take_profit_pct=body.take_profit,
        stop_loss_mult=body.stop_loss,
        trailing_sl_mult=body.trailing_sl,
        trailing_sl_trigger_pct=body.trailing_sl_trigger,
        max_entry_vix=body.max_entry_vix,
    )
    if body.strategy == LADDER:
        return StrategyConfig(
            **common,
            min_entry_dte=body.min_entry_dte,
            min_credit_ratio=body.min_credit_ratio,
        )
    if body.strategy != HIC:
        # Reached when a build knows a strategy's name but not how to trade it.
        # That has happened: an engine accepted `strategy: "hic"`, recorded the
        # row as HIC, and -- having no code to build one -- silently ran a
        # down-only ladder under the label. Every surface then reported a
        # strategy the run was not trading. Refusing is the only honest answer;
        # substituting quietly is the one that misleads.
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            f"This engine build cannot run {body.strategy!r}. It knows the name "
            f"but has no implementation, which usually means the engine is older "
            f"than the dashboard. Restart the engine on a matching build.",
        )

    # HIC is two-way by construction: the spreads it buys follow the move, so
    # a one-directional run of it is a different strategy wearing the name.
    common["direction"] = "both"
    # And centred. Under `floor` the spot sits up to 99 points above the
    # anchor, so the first rung up can be a point away while the first rung
    # down is 199 -- which defeats a structure that is symmetric by design.
    # Cleared rather than validated, so a form that sends the ladder's setting
    # (as it does, since it sends every field regardless) cannot tilt it.
    common["anchor_mode"] = None
    config = HicConfig(
        **common,
        full_band_steps=body.full_band_steps,
        half_mode=body.half_mode,
        debit_shift=body.debit_shift,
        max_put_spreads=body.max_put_spreads,
        max_call_spreads=body.max_call_spreads,
    )
    # The per-side caps say how far the ladder may go; for HIC that is the band
    # plus its spreads. Left to the caller only if they asked for something
    # tighter, since a cap is a limit rather than an instruction.
    return replace(
        config,
        max_down=min(body.max_down or config.max_down_levels, config.max_down_levels),
        max_up=min(body.max_up or config.max_up_levels, config.max_up_levels),
    )


def _rungs_requested(config: StrategyConfig) -> int | None:
    """How many rungs the per-side caps allow, anchor included.

    None when a side is unbounded: only `max_condors` limits it then, so there
    is nothing for the two to disagree about.
    """
    sides = {
        "down": (config.max_down,),
        "up": (config.max_up,),
    }.get(config.direction, (config.max_down, config.max_up))
    if any(s is None for s in sides):
        return None
    return sum(sides) + 1               # the anchor counts as neither side


@app.post("/forward/start", dependencies=[Depends(check_engine_key)])
def forward_start(
    body: StartForwardRequest,
    session: UserSession = Depends(current_user),
    market: ChoiceMarketData = Depends(user_market),
) -> dict[str, Any]:
    strategy_id = body.strategy
    run_key = slugify_run_key(body.name) if body.name else strategy_id
    live = session.runner_for(run_key)
    if live is not None and live.stopped_reason is None:
        # Nothing new starts under a name a live run holds. Said, not implied:
        # the page used to switch to the other run as if this one had begun.
        return {"ok": True, "already_running": True, "run_key": run_key,
                "note": (f"A test named {run_key!r} is already running, so nothing new was "
                         "started. Give the new test its own name to run it alongside."),
                "state": live.snapshot()}

    # Adopt a saved run before minting a new one.
    #
    # The in-memory handle is only that. After a restart it is None until a
    # login resumes the run, so a browser that reconnects and hits start first
    # would create a second run -- empty, newer, and therefore the one a later
    # resume picks, destroying the ladder that had been trading all day.
    # Resuming here closes that window.
    if _resume_forward(session):
        adopted = session.runner_for(run_key)
        if adopted is not None:
            return {"ok": True, "already_running": True, "resumed": True,
                    "run_key": run_key, "state": adopted.snapshot()}

    # Capped, and the cap says why. Every run polls, and the Choice rate limit
    # is per user rather than per run, so an unbounded count would quietly slow
    # every one of them down instead of refusing the one that broke the budget.
    running = [k for k, r in session.runners().items() if r.stopped_reason is None]
    if len(running) >= MAX_RUNS_PER_USER:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"You already have {len(running)} forward tests running "
            f"({', '.join(sorted(running))}). Stop one, or raise ENGINE_MAX_RUNS.",
        )

    try:
        lot_size = market.master.lot_size_for(NIFTY)
        # min_days=1, not 0: on expiry day the nearest contract settles in
        # hours, so the wings are nearly worthless and the structure is a
        # condor in name only.
        expiry = nearest_listed_expiry(
            market.master.expiries(NIFTY),
            dt.datetime.now(tz=IST).date(),
            cadence=body.expiry_cadence,
            min_days=1,
        )
        if expiry is None:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"No {body.expiry_cadence} NIFTY expiry is listed after today.",
            )
        strike_step = market.master.strike_step(NIFTY, expiry)
    except ChoiceInstrumentError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    runner = ForwardRunner(
        market=market,
        strategy=_strategy_config(body, lot_size=lot_size, strike_step=strike_step),
        costs=CostModel(),
        expiry_cadence=body.expiry_cadence,
        # Per user and per strategy: neither another user's run nor this
        # user's other strategy may overwrite this one's snapshot.
        state_path=_state_path(session, run_key),
        store=store,
        session_id=uuid.uuid4().hex,
        user_id=session.user_id,
        strategy_id=strategy_id,
        run_key=run_key,
        run_label=(body.name or strategy_id).strip()[:40],
        daily_loss_limit=body.daily_loss_limit,
        bar_minutes=body.bar_minutes,
    )
    session.set_runner(run_key, runner)

    # The per-side caps can ask for more rungs than the total cap allows, and
    # the ladder simply stops firing once it reaches the total -- silently, at
    # whichever rung happens to get there first. HIC's defaults do exactly
    # that: a one-step band with ten spreads a side wants 23 rungs against an
    # engine ceiling of 20, so the three furthest from the anchor -- the ones
    # a big move depends on -- never open, and nothing anywhere said so.
    wanted = _rungs_requested(runner.strategy)
    if wanted is not None and wanted > runner.strategy.max_condors:
        runner.emit(
            "warn",
            f"This configuration asks for {wanted} rungs but the run is capped "
            f"at {runner.strategy.max_condors}. The "
            f"{wanted - runner.strategy.max_condors} furthest from the anchor "
            f"will not open. Lower the per-side limits, or raise the cap.",
            requested=wanted, cap=runner.strategy.max_condors,
        )

    # First tick inline so the caller gets a populated state immediately, then
    # keep ticking on a worker thread. Without the background loop the ladder
    # would only advance when someone happened to open the page, which is not
    # a forward test -- it is a manual refresh.
    #
    # Outside market hours that first tick would only produce an empty book and
    # a red error on a run that is behaving perfectly well, so say what is
    # actually happening instead.
    if runner.is_market_open():
        runner.tick()
    else:
        runner.emit(
            "info",
            f"Market closed; the ladder starts at {market_calendar.next_open():%d-%b %H:%M}",
        )
    runner.save()

    _start_tick_thread(runner, session, body.poll_seconds)
    return {"ok": True, "state": runner.snapshot()}


@app.post("/forward/resume", dependencies=[Depends(check_engine_key)])
def forward_resume(
    session: UserSession = Depends(current_user),
    run_key: str = Depends(run_param),
    market: ChoiceMarketData = Depends(user_market),
) -> dict[str, Any]:
    """Start a stopped run again: the same run, with its ladder, its positions
    and its history, carrying on from where it stopped.

    "Start a new run" was the only way back, and it opened a blank form: with
    the name left empty the new run took the strategy's name, a live run
    already held it, and the page switched to that run as if the stopped one
    had started. An expiry that passed while it was stopped settles against
    its official close at the first tick, and the ladder re-anchors after it.
    """
    live = session.runner_for(run_key)
    if live is not None and live.stopped_reason is None:
        return {"ok": True, "already_running": True, "run_key": run_key, "state": live.snapshot()}
    row = _stopped_rows(session).get(run_key)
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"There is no stopped run named {run_key!r} to resume.")
    blocked = _resume_blocked(session)
    if blocked:
        raise HTTPException(status.HTTP_409_CONFLICT, blocked)
    record = store.forward_session(row["session_id"])
    if record is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This run's saved state could not be read.")
    try:
        runner = ForwardRunner.restore(
            record["state"], market=market, costs=CostModel(),
            state_path=_state_path(session, run_key), store=store,
            session_id=record["session_id"], user_id=session.user_id,
            strategy_id=record["strategy_id"], run_key=run_key, run_label=record["run_label"],
        )
    except UnsupportedStateVersion as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This run was saved in a form this engine cannot read."
        ) from exc
    runner.stopped_reason = None
    session.set_runner(run_key, runner)
    runner.emit(
        "info", "Forward run resumed by user",
        condors=len([c for c in runner.condors if c.is_open]),
        fired=len(runner.ladder.fired_levels),
    )
    if runner.is_market_open():
        runner.tick()
    else:
        runner.settle_if_expired()
    runner.save()
    _start_tick_thread(runner, session, poll_seconds=15.0)
    log.info("Run %s (%s) resumed by user %s", run_key, record["session_id"], session.user_id)
    return {"ok": True, "resumed": True, "run_key": run_key, "state": runner.snapshot()}


def _resume_blocked(session: UserSession) -> str | None:
    """Why a stopped run cannot be started again now, or None."""
    running = [k for k, r in session.runners().items() if r.stopped_reason is None]
    if len(running) >= MAX_RUNS_PER_USER:
        return (f"You already have {len(running)} forward tests running. "
                "Stop one to resume this one.")
    return None


@app.post("/forward/tick", dependencies=[Depends(check_engine_key)])
def forward_tick(
    session: UserSession = Depends(current_user),
    run_key: str = Depends(run_param),
) -> dict[str, Any]:
    """Force one polling cycle. Refused once the run has stopped.

    Without this guard the kill switch was advisory: a run that had tripped its
    daily loss limit reported itself stopped and then opened fresh condors on
    the next manual tick.
    """
    runner = _require_runner(session, run_key)
    if runner.stopped_reason:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This run has stopped ({runner.stopped_reason}). Start a new one to continue.",
        )
    runner.tick()
    runner.save()
    return {"ok": True, "state": runner.snapshot()}


#: How long a stopped run stays in the roll-call after it stopped.
STOPPED_RUNS_SHOWN_DAYS = 14


def _stopped_rows(session: UserSession) -> dict[str, dict[str, Any]]:
    """The latest stored row of each run this user has that the engine is no
    longer driving and that stopped recently, by run name."""
    try:
        rows = store.forward_history(session.user_id, limit=50)
    except Exception:                               # noqa: BLE001
        log.exception("Could not read stopped runs for %s", session.user_id)
        return {}
    live = session.runners()
    cutoff = dt.datetime.now(tz=IST) - dt.timedelta(days=STOPPED_RUNS_SHOWN_DAYS)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:                                # newest first
        key = row["run_key"]
        if key in live or key in out:
            continue
        out[key] = row
    keep = {}
    for key, row in out.items():
        reason = row.get("stopped_reason") or ""
        if row["status"] != "stopped" or reason.startswith(("retired", "superseded")):
            continue
        try:
            updated = dt.datetime.fromisoformat(row["updated_at"])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError):
            continue
        if updated >= cutoff:
            keep[key] = row
    return keep


def _waiting_rows(session: UserSession) -> dict[str, dict[str, Any]]:
    """Runs that should be ticking but are not yet, by run name: still marked
    running in the store, with no runner in memory -- after an engine restart,
    until the watchdog can resume them (on a closed day, not before the next
    trading morning's session). They were missing from every list, so the
    dashboard showed no runs at all for a whole weekend."""
    try:
        rows = store.forward_history(session.user_id, limit=50)
    except Exception:                               # noqa: BLE001
        log.exception("Could not read waiting runs for %s", session.user_id)
        return {}
    live = session.runners()
    newest: dict[str, dict[str, Any]] = {}
    for row in rows:                                # newest first
        newest.setdefault(row["run_key"], row)
    return {key: row for key, row in newest.items() if row["status"] == "running" and key not in live}


def _waiting_reason(session: UserSession, run_key: str) -> str:
    reason = _RESUME_WAITING.get((session.user_id, run_key))
    return reason or WAITING_REASON


def _saved_snapshot(session: UserSession, run_key: str) -> dict[str, Any] | None:
    """The last snapshot a run wrote before the engine stopped driving it.

    A stopped run left memory -- on a stop from the dashboard at once, on any
    restart otherwise -- and with it went its chart, its positions and its
    P&L: the page showed nothing at all for a run that had been trading for
    weeks. The snapshot each save writes is exactly what the page needs.
    """
    import json as _json

    path = _state_path(session, run_key)
    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _stopped_summary(session: UserSession, run_key: str, row: dict[str, Any]) -> dict[str, Any] | None:
    snap = _saved_snapshot(session, run_key)
    if snap is None:
        return None
    pnl = snap.get("pnl") or {}
    info = snap.get("session") or {}
    return {
        "run_key": run_key,
        "label": row.get("run_label") or run_key,
        "strategy": row.get("strategy_id") or LADDER,
        "running": False,
        "stopped_reason": row.get("stopped_reason") or "stopped",
        "ticking": False,
        "started_at": row["started_at"],
        "last_tick": info.get("last_tick"),
        "expiry": info.get("expiry"),
        "direction": (snap.get("ladder") or {}).get("direction"),
        "lots": info.get("lots"),
        "daily_loss_limit": (snap.get("risk") or {}).get("daily_loss_limit"),
        "pnl": pnl,
        "open_condors": pnl.get("open_condors", 0),
    }


def _run_summaries(session: UserSession) -> list[dict[str, Any]]:
    """Every forward test this user is driving, newest first.

    One entry per run with enough to render a row without asking for each
    run's full snapshot: what it is, what it was configured with, and where it
    stands.
    """
    out = []
    for run_key, runner in session.runners().items():
        snap = runner.snapshot()
        pnl = snap.get("pnl") or {}
        ladder = snap.get("ladder") or {}
        out.append({
            "run_key": run_key,
            "label": runner.run_label or run_key,
            "strategy": runner.strategy_id,
            "running": runner.stopped_reason is None,
            "stopped_reason": runner.stopped_reason,
            "ticking": runner.is_ticking,
            "started_at": runner.started_at.isoformat(),
            "last_tick": snap.get("session", {}).get("last_tick"),
            "expiry": snap.get("session", {}).get("expiry"),
            "direction": ladder.get("direction"),
            "lots": runner.strategy.lots,
            "daily_loss_limit": runner.daily_loss_limit,
            "pnl": pnl,
            "open_condors": pnl.get("open_condors", 0),
        })
    # Recently stopped runs too, listed as stopped, so one is never simply
    # gone from the page.
    for run_key, row in _stopped_rows(session).items():
        summary = _stopped_summary(session, run_key, row)
        if summary is not None:
            out.append(summary)
    # Runs waiting to be resumed: not stopped, so not listed as stopped.
    for run_key, row in _waiting_rows(session).items():
        summary = _stopped_summary(session, run_key, row)
        if summary is not None:
            summary.update(stopped_reason=None, waiting=True, wait_reason=_waiting_reason(session, run_key))
            out.append(summary)
    # Live runs first, then waiting, then stopped; each group newest first.
    out.sort(key=lambda r: r["started_at"], reverse=True)
    out.sort(key=lambda r: 0 if r["running"] else 1 if r.get("waiting") else 2)
    return out


@app.get("/forward/runs", dependencies=[Depends(check_engine_key)])
def forward_runs(session: UserSession = Depends(current_user)) -> dict[str, Any]:
    """The roll-call on its own. `/forward/state` carries the same list."""
    return {
        "runs": _run_summaries(session),
        "max_runs": MAX_RUNS_PER_USER,
        "account_loss_limit": abs(engine_config.account_loss_limit),
    }


@app.get("/forward/state", dependencies=[Depends(check_engine_key)])
def forward_state(
    session: UserSession = Depends(current_user),
    run: str | None = None,
) -> dict[str, Any]:
    """One run's full state, and the roll-call of every run beside it.

    Both in one response on purpose. The dashboard renders from Vercel and the
    engine answers from a machine in India behind a tunnel, so a page that
    asked for the roll-call and then the state paid two ocean round trips
    before it could draw anything -- about a second of blank screen per click,
    every click. Nothing about the two is ordered, so they travel together.

    `run` is optional. Omitted, it serves the run a dashboard would have shown
    anyway -- the one called "ladder", which is what a single run has always
    been -- but falls through to whichever run the user actually has, so a
    caller with no ladder gets a real run rather than a confident `null`. The
    response names the run it served, so the caller never has to guess.
    """
    runners = session.runners()
    waiting = _waiting_rows(session) if not runners or (run and slugify_run_key(run) not in runners) else {}
    if run:
        run_key = slugify_run_key(run)
    elif DEFAULT_RUN_KEY in runners:
        run_key = DEFAULT_RUN_KEY
    elif runners:
        run_key = next(iter(runners))
    else:
        run_key = DEFAULT_RUN_KEY if DEFAULT_RUN_KEY in waiting else next(iter(waiting), DEFAULT_RUN_KEY)

    runner = runners.get(run_key)
    state = runner.snapshot() if runner is not None else None
    if state is None:
        # Not driven: serve what it last looked like, marked stopped -- or
        # waiting, when it is only waiting to be resumed.
        row = _stopped_rows(session).get(run_key)
        waiting_row = waiting.get(run_key) if row is None else None
        state = _saved_snapshot(session, run_key) if (row or waiting_row) is not None else None
        if state is not None and waiting_row is not None:
            # Not stopped: it carries on by itself once a session is back.
            info = dict(state.get("session") or {})
            info.update(status="waiting", stopped_reason=None, resumable=False, resume_blocked=None,
                        wait_reason=_waiting_reason(session, run_key))
            state = {**state, "session": info}
            row = waiting_row
        elif state is not None:
            info = dict(state.get("session") or {})
            info["status"] = "stopped"
            info["stopped_reason"] = info.get("stopped_reason") or row.get("stopped_reason")
            blocked = _resume_blocked(session)
            info["resumable"] = blocked is None
            info["resume_blocked"] = blocked
            state = {**state, "session": info}
        if state is not None and not state.get("settings"):
            # Saved before snapshots carried their settings.
            record = store.forward_session(row["session_id"])
            if record is not None:
                state["settings"] = settings_from_state(
                    record["state"], strategy_id=record["strategy_id"],
                    name=record["run_label"] or run_key,
                )
    return {
        "run_key": run_key,
        "running": runner is not None and runner.stopped_reason is None,
        "state": state,
        "runs": _run_summaries(session),
        "max_runs": MAX_RUNS_PER_USER,
        "account_loss_limit": abs(engine_config.account_loss_limit),
    }


@app.get("/forward/ticks", dependencies=[Depends(check_engine_key)])
def forward_ticks(
    limit: int = 900,
    session: UserSession = Depends(current_user),
    run_key: str = Depends(run_param),
) -> dict[str, Any]:
    """The live chart's history.

    Held in the database rather than the browser, so a page reload -- or a
    second device -- picks up the whole session instead of redrawing from an
    empty series.
    """
    runner = session.runner_for(run_key)
    session_id = runner.session_id if runner is not None else None
    if session_id is None:
        # A stopped run's chart is still worth seeing.
        row = _stopped_rows(session).get(run_key)
        session_id = row["session_id"] if row is not None else None
    if not session_id:
        return {"ticks": []}
    return {"ticks": store.ticks(session_id, limit=max(1, min(limit, 5_000)))}


#: A settled campaign's NIFTY path never changes; asked once per engine life.
_CAMPAIGN_BARS: dict[tuple[str, str], list[dict[str, Any]]] = {}


@app.get("/forward/campaign", dependencies=[Depends(check_engine_key)])
def forward_campaign(
    expiry: str,
    session: UserSession = Depends(current_user),
    run_key: str = Depends(run_param),
    market: ChoiceMarketData = Depends(user_market),
) -> dict[str, Any]:
    """NIFTY over one of a run's campaigns, for its chart.

    The tick table keeps about the last six days, so a month-old campaign has
    no ticks left to draw. Choice's own 15-minute NIFTY bars cover it, from
    the campaign's first position to its expiry.
    """
    runner = session.runner_for(run_key)
    state = runner.snapshot() if runner is not None else _saved_snapshot(session, run_key)
    campaigns = (state or {}).get("campaigns") or []
    campaign = next((c for c in campaigns if c["expiry"] == expiry), None)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"This run has no campaign expiring {expiry}.")
    session_id = runner.session_id if runner is not None else run_key
    key = (str(session_id), expiry)
    bars = _CAMPAIGN_BARS.get(key)
    if bars is None:
        start = dt.date.fromisoformat(campaign["started_at"][:10])
        end = min(dt.date.fromisoformat(expiry), dt.datetime.now(tz=IST).date())
        try:
            frame = market.nifty(start, end + dt.timedelta(days=1), "15", strict=False)
        except ChoiceError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Choice could not serve NIFTY for {start}..{end}: {exc}") from exc
        bars = [
            {"ts": row.ts.isoformat(), "spot": round(float(row.close), 2)}
            for row in frame.itertuples()
            if start <= row.ts.date() <= end
        ]
        if campaign["status"] == "settled":
            _CAMPAIGN_BARS[key] = bars
    return {"run_key": run_key, "campaign": campaign, "bars": bars}


# ------------------------------------------------------------- playground

from engine.playground import replay as pg_replay  # noqa: E402
from engine.playground import simulate as pg_simulate  # noqa: E402
from engine.playground.jobs import jobs as playground_jobs  # noqa: E402

#: The as-traded replay of each campaign, run once and shared by every edit.
_BASELINES: dict[tuple[str, str, str], str] = {}


def _run_state(session: UserSession, run_key: str) -> tuple[ForwardRunner | None, dict[str, Any] | None]:
    """A run's runner if the engine drives it, and its state either way --
    settings included, read back from storage for a run that predates them."""
    runner = session.runner_for(run_key)
    if runner is not None:
        return runner, runner.snapshot()
    state = forward_state(session=session, run=run_key)["state"]
    return None, state


def _compact_positions(state: dict[str, Any], expiry: str | None = None) -> list[dict[str, Any]]:
    return [
        {
            "level": c["level"], "side": c.get("side"), "expiry": c["expiry"], "entry_time": c["entry_time"],
            "credit": c["credit"], "max_loss": c["max_loss"], "pnl": c["pnl"], "status": c["status"],
            "exit_reason": c.get("exit_reason"), "is_open": c["is_open"],
        }
        for c in state.get("positions") or []
        if expiry is None or c["expiry"] == expiry
    ]


@app.get("/playground/campaigns", dependencies=[Depends(check_engine_key)])
def playground_campaigns(session: UserSession = Depends(current_user)) -> dict[str, Any]:
    """Every run this user has, with its settings and its campaigns."""
    out = []
    for summary in _run_summaries(session):
        _, state = _run_state(session, summary["run_key"])
        if not state:
            continue
        out.append({
            "run_key": summary["run_key"], "label": summary["label"], "strategy": summary["strategy"],
            "running": summary["running"], "settings": state.get("settings"),
            "campaigns": state.get("campaigns") or [],
            "market": state.get("market"), "vix": state.get("vix"),
        })
    return {"runs": out}


class ReplayRequest(BaseModel):
    run: str
    expiry: str
    settings: dict[str, Any] = Field(default_factory=dict)


@app.post("/playground/replay", dependencies=[Depends(check_engine_key)])
def playground_replay(
    body: ReplayRequest,
    session: UserSession = Depends(current_user),
    market: ChoiceMarketData = Depends(user_market),
) -> dict[str, Any]:
    """Replay a settled campaign with edited settings, beside the same
    campaign replayed as traded and what the run actually did."""
    run_key = slugify_run_key(body.run)
    _, state = _run_state(session, run_key)
    campaign = next((c for c in (state or {}).get("campaigns") or [] if c["expiry"] == body.expiry), None)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No campaign expiring {body.expiry} on {run_key!r}.")
    if campaign["status"] == "active":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This campaign is still trading; plan it instead, or replay one that has settled.")
    settings = state.get("settings")
    if not settings:
        raise HTTPException(status.HTTP_409_CONFLICT, "This run's settings could not be read.")
    edited = pg_replay.merged_settings(settings, body.settings)

    def start(kind_settings: dict[str, Any], label: str):
        def work(job):
            def progress(value: float, message: str) -> None:
                job.progress, job.message = value, message
            return pg_replay.run_replay(market, session.user_id, kind_settings, campaign, progress)
        return playground_jobs.start(session.user_id, "replay",
                                     {"run": run_key, "expiry": body.expiry, "label": label,
                                      "settings": kind_settings}, work)

    key = (session.user_id, run_key, body.expiry)
    baseline = playground_jobs.get(session.user_id, _BASELINES.get(key, ""))
    if baseline is None or baseline.status == "error":
        baseline = start(dict(settings), "as traded")
        _BASELINES[key] = baseline.job_id
    edited_job = start(edited, "your settings")
    return {
        "actual": {"campaign": campaign, "positions": _compact_positions(state, body.expiry)},
        "baseline": baseline.public(with_result=False),
        "edited": edited_job.public(with_result=False),
    }


class PlanRequest(BaseModel):
    run: str
    settings: dict[str, Any] = Field(default_factory=dict)
    spot: float | None = Field(default=None, gt=0)
    vix: float | None = Field(default=None, gt=0, le=100)
    paths: int = Field(default=1000, ge=100, le=5000)
    fresh: bool = False


#: What a plan may change -- the replay's set, without take-profit and
#: stop-loss: marking every position on every bar of every path is beyond a
#: request's budget, so they are not simulated rather than simulated wrongly.
_PLAN_FIELDS = {
    "step": float, "short_offset": float, "long_offset": float, "lots": int, "max_condors": int,
    "direction": str, "anchor_mode": str, "max_down": int, "max_up": int, "max_entry_vix": float,
    "min_entry_dte": int, "min_credit_ratio": float, "full_band_steps": int, "half_mode": str,
    "debit_shift": float, "max_put_spreads": int, "max_call_spreads": int,
}
#: Run settings a plan may change that are not the strategy's.
_PLAN_RUN_FIELDS = {"daily_loss_limit": float}


@app.post("/playground/plan", dependencies=[Depends(check_engine_key)])
def playground_plan(
    body: PlanRequest,
    session: UserSession = Depends(current_user),
    market: ChoiceMarketData = Depends(user_market),
) -> dict[str, Any]:
    """Simulate the campaign trading now -- or a fresh one -- under settings."""
    from dataclasses import replace as _replace

    from engine.data.lot_sizes import nifty_lot_size

    run_key = slugify_run_key(body.run)
    runner = session.runner_for(run_key)
    if runner is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Plan a running test; this one is not running.")
    changes: dict[str, Any] = {}
    for key, kind in _PLAN_FIELDS.items():
        if key in body.settings:
            value = body.settings[key]
            changes[key] = None if value in (None, "") else kind(value)
    loss_limit = runner.daily_loss_limit
    if "daily_loss_limit" in body.settings:
        value = body.settings["daily_loss_limit"]
        loss_limit = None if value in (None, "", 0) else abs(float(value))
    if not hasattr(runner.strategy, "full_band_steps"):
        for key in ("full_band_steps", "half_mode", "debit_shift", "max_put_spreads", "max_call_spreads"):
            changes.pop(key, None)
    try:
        config = _replace(runner.strategy, **changes)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Those settings do not make a valid strategy: {exc}") from exc

    with runner._lock:                                   # noqa: SLF001 - a consistent read
        expiry = runner.expiry
        fresh = body.fresh or expiry is None
        if expiry is None:
            expiry = nearest_listed_expiry(
                market.master.expiries(NIFTY), dt.datetime.now(tz=IST).date(),
                cadence=runner.expiry_cadence, min_days=1,
            )
        units = [u for u in runner.condors if u.expiry == expiry]
        book = pg_simulate.Book(
            expiry=expiry,
            open_units=[] if fresh else [u for u in units if u.is_open],
            realised=0.0 if fresh else sum(u.realised_pnl() for u in units if not u.is_open),
            ladder_state=None if fresh else runner.ladder.dump_state(),
            # Where today's loss limit stands on the live run, so a plan made
            # mid-session starts the day where the run actually is.
            day_pnl=0.0 if fresh else float(runner.day_pnl() or 0.0),
            halted_today=False if fresh else runner.entries_halted(dt.datetime.now(tz=IST).date()),
        )
        spot = body.spot or runner.last_spot
        vix = body.vix or runner.last_vix
    if expiry is None or not spot:
        raise HTTPException(status.HTTP_409_CONFLICT, "No price yet to plan from; try once the run has ticked.")
    if not vix:
        try:
            vix, _ = market.india_vix_now()
        except ChoiceError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"India VIX is unavailable: {exc}") from exc
    config = _replace(config, lot_size=nifty_lot_size(expiry, current=runner.strategy.lot_size))

    def work(job):
        def progress(value: float, message: str) -> None:
            job.progress, job.message = value, message
        out = pg_simulate.plan(market=market, config=config, book=book, spot=float(spot),
                               vix=float(vix) / 100.0, paths=body.paths, daily_loss_limit=loss_limit,
                               progress=progress)
        out["settings"] = {**(runner.settings()), **changes, "daily_loss_limit": loss_limit}
        return out

    job = playground_jobs.start(session.user_id, "plan",
                                {"run": run_key, "fresh": fresh, "settings": body.settings}, work)
    return {"job": job.public(with_result=False)}


@app.get("/playground/job", dependencies=[Depends(check_engine_key)])
def playground_job(id: str, session: UserSession = Depends(current_user)) -> dict[str, Any]:
    job = playground_jobs.get(session.user_id, id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such playground job (they are kept for a while only).")
    return {"job": job.public(with_result=job.status == "done")}


@app.get("/forward/history", dependencies=[Depends(check_engine_key)])
def forward_history(session: UserSession = Depends(current_user)) -> dict[str, Any]:
    return {"sessions": store.forward_history(session.user_id)}


@app.post("/forward/stop", dependencies=[Depends(check_engine_key)])
def forward_stop(
    session: UserSession = Depends(current_user),
    run_key: str = Depends(run_param),
) -> dict[str, Any]:
    """Stop one strategy's run. Never anyone else's, never the other one."""
    runner = _require_runner(session, run_key)
    runner.stopped_reason = "stopped by user"
    runner.emit("warn", "Forward run stopped by user")
    runner.save()
    if runner.session_id:
        store.mark_stopped(runner.session_id, "stopped by user")
    state = runner.snapshot()
    session.set_runner(run_key, None)
    return {"ok": True, "state": state}


def _require_runner(session: UserSession, run_key: str = DEFAULT_RUN_KEY) -> ForwardRunner:
    runner = session.runner_for(run_key)
    if runner is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"No forward run named {run_key!r} is started for this session.",
        )
    return runner


def _state_path(session: UserSession, run_key: str = DEFAULT_RUN_KEY):
    """Where a run's convenience snapshot is written.

    Named per strategy: one path per user meant a second strategy would
    overwrite the first's file on every tick. The database is the source of
    truth either way, so this only ever cost an operator a confusing file --
    but a confusing file during an incident is exactly when it matters.
    """
    from pathlib import Path

    base = Path(os.environ.get("ENGINE_STATE_DIR", "engine/state"))
    return base / f"live-{session.user_id}-{run_key}.json"


@app.exception_handler(ChoiceError)
def choice_error_handler(_request: Request, exc: ChoiceError):
    """Choice's answer, as a status the dashboard can act on.

    A 401 tells the dashboard the user's own sign-in is gone and sends the
    browser to the login page -- right for an unknown engine token, which
    `current_user` reports itself, and wrong for anything Choice says. When
    Choice refused the broker session every 401 from here bounced the user to
    a sign-in that texted them another OTP and fixed nothing. So a refused
    broker session is a 503 that carries the explanation, and the page stays
    where it is.
    """
    from fastapi.responses import JSONResponse

    if isinstance(exc, StaticIpRejectedError):
        code = status.HTTP_403_FORBIDDEN
    elif isinstance(exc, ChoiceAuthError):
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        code = status.HTTP_502_BAD_GATEWAY
    return JSONResponse(status_code=code, content={"detail": str(exc)})
