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
import logging
import os
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

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
from engine.forward.runner import ForwardRunner, UnsupportedStateVersion, market_calendar, market_is_open
from engine.store.db import LADDER, STRATEGIES, Store
from engine.pricing.costs import CostModel
from engine.strategy.condor import StrategyConfig

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
    yield


app = FastAPI(
    title="Iron Condor Ladder engine", version="1.0.0",
    docs_url=None, redoc_url=None, lifespan=_lifespan,
)

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
    days: int = Field(default=120, ge=5, le=3650)
    resolution: str = Field(default="D", pattern="^(1|3|5|10|15|30|60|D|W)$")
    option_resolution: str | None = Field(default=None, pattern="^(1|3|5|10|15|30|60|D|W)$")
    lots: int = Field(default=1, ge=1, le=100)
    step: float = Field(default=100.0, gt=0, le=5000)
    max_condors: int = Field(default=20, ge=1, le=100)
    take_profit: float | None = Field(default=None, gt=0, le=1)
    stop_loss: float | None = Field(default=None, gt=0, le=20)
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
def strategy_param(strategy: str = LADDER) -> str:
    if strategy not in STRATEGIES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown strategy {strategy!r}. Known: {', '.join(STRATEGIES)}.",
        )
    return strategy


class StartForwardRequest(BaseModel):
    strategy: str = Field(default=LADDER, pattern=f"^({'|'.join(STRATEGIES)})$")
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

    by_strategy: dict[str, list[dict]] = {}
    for row in pending:
        strategy_id = row.get("strategy_id") or LADDER
        if only is not None and strategy_id != only:
            continue
        by_strategy.setdefault(strategy_id, []).append(row)

    resumed = False
    for strategy_id, rows in by_strategy.items():
        if session.runner_for(strategy_id) is not None:
            continue                                # already has a driver
        resumed |= _resume_one(session, strategy_id, rows)
    return resumed


def _resume_one(session: UserSession, strategy_id: str, rows: list[dict]) -> bool:
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
            "Superseded %s run %s for user %s (%d open condor(s)); retiring it",
            strategy_id, stale["session_id"], session.user_id, stale_open,
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
        except ChoiceError:
            log.exception("Could not attach market data to resume %s", record["session_id"])
            return False
    market = session.market
    try:
        runner = ForwardRunner.restore(
            record["state"], market=market, costs=CostModel(),
            state_path=_state_path(session, strategy_id), store=store,
            session_id=record["session_id"], user_id=session.user_id,
            strategy_id=strategy_id,
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
    session.set_runner(strategy_id, runner)
    runner.emit(
        "info", "Forward run resumed after an engine restart",
        condors=len([c for c in runner.condors if c.is_open]),
        fired=len(runner.ladder.fired_levels),
    )
    _start_tick_thread(runner, session, poll_seconds=15.0)
    log.info(
        "Resumed %s run %s for user %s", strategy_id, record["session_id"], session.user_id
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
        name=f"forward-{session.user_id}-{runner.strategy_id}",
        daemon=True,
    )
    runner.tick_thread = thread
    runner.poll_seconds = poll
    thread.start()


WATCHDOG_SECONDS = float(os.environ.get("ENGINE_WATCHDOG_SECONDS", "60") or 60)


def _revive_run(session: UserSession, strategy_id: str = LADDER) -> bool:
    """Make sure one strategy's saved run is actually being driven.

    Three states have to be told apart, and conflating them is what let a
    ladder sit frozen through a 100-point decline:

    * no runner in memory -- resume it from the database;
    * a runner whose worker died or was suspended -- start a new worker on the
      same object, keeping the ladder, the fills and the open positions;
    * a runner that is ticking -- leave it alone.
    """
    with _user_revive_lock(session.user_id):
        runner = session.runner_for(strategy_id)
        if runner is None:
            return _resume_forward_locked(session, only=strategy_id)
        if runner.stopped_reason or runner.is_ticking:
            return False
        runner.unsuspend()
        runner.emit("warn", "Forward run had stopped ticking; restarting its worker")
        log.warning(
            "Restarting a dead %s worker for user %s (run %s)",
            strategy_id, session.user_id, runner.session_id,
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
        wanted.setdefault(row["user_id"], set()).add(row.get("strategy_id") or LADDER)

    for user_id, strategies in wanted.items():
        try:
            session = registry.revive_for_user(user_id)
            if session is None:
                # No stored credentials: the run stays marked running, which is
                # true. It has positions open and simply nobody to quote it.
                continue
        except Exception:                           # noqa: BLE001
            log.exception("Watchdog could not revive a session for %s", user_id)
            continue
        for strategy_id in sorted(strategies):
            try:
                _revive_run(session, strategy_id)
            except Exception:                       # noqa: BLE001
                # One strategy failing must not cost the user the other one.
                log.exception("Watchdog failed for %s/%s", user_id, strategy_id)


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
def backtest_dataset(session: UserSession = Depends(current_user)) -> dict[str, Any]:
    """This user's latest result, or an honest empty bundle explaining why not."""
    return backtest_store.dataset(session.user_id)


@app.get("/backtest/history", dependencies=[Depends(check_engine_key)])
def backtest_history(session: UserSession = Depends(current_user)) -> dict[str, Any]:
    """Past runs, which now outlive the process that produced them."""
    return {"runs": backtest_store.history(session.user_id)}


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


@app.post("/forward/start", dependencies=[Depends(check_engine_key)])
def forward_start(
    body: StartForwardRequest,
    session: UserSession = Depends(current_user),
    market: ChoiceMarketData = Depends(user_market),
) -> dict[str, Any]:
    strategy_id = body.strategy
    live = session.runner_for(strategy_id)
    if live is not None and live.stopped_reason is None:
        return {"ok": True, "already_running": True, "state": live.snapshot()}

    # Adopt a saved run before minting a new one.
    #
    # The in-memory handle is only that. After a restart it is None until a
    # login resumes the run, so a browser that reconnects and hits start first
    # would create a second run -- empty, newer, and therefore the one a later
    # resume picks, destroying the ladder that had been trading all day.
    # Resuming here closes that window.
    if _resume_forward(session):
        adopted = session.runner_for(strategy_id)
        if adopted is not None:
            return {"ok": True, "already_running": True, "resumed": True,
                    "state": adopted.snapshot()}

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
        strategy=StrategyConfig(
            step=body.step, lots=body.lots, lot_size=lot_size,
            max_condors=min(body.max_condors, engine_config.max_condors),
            direction=body.direction,
            anchor_mode=body.anchor_mode,
            max_down=body.max_down,
            max_up=body.max_up,
            strike_step=strike_step,
            take_profit_pct=body.take_profit,
            stop_loss_mult=body.stop_loss,
        ),
        costs=CostModel(),
        expiry_cadence=body.expiry_cadence,
        # Per user and per strategy: neither another user's run nor this
        # user's other strategy may overwrite this one's snapshot.
        state_path=_state_path(session, strategy_id),
        store=store,
        session_id=uuid.uuid4().hex,
        user_id=session.user_id,
        strategy_id=strategy_id,
    )
    session.set_runner(strategy_id, runner)

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


@app.post("/forward/tick", dependencies=[Depends(check_engine_key)])
def forward_tick(
    session: UserSession = Depends(current_user),
    strategy_id: str = Depends(strategy_param),
) -> dict[str, Any]:
    """Force one polling cycle. Refused once the run has stopped.

    Without this guard the kill switch was advisory: a run that had tripped its
    daily loss limit reported itself stopped and then opened fresh condors on
    the next manual tick.
    """
    runner = _require_runner(session, strategy_id)
    if runner.stopped_reason:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This run has stopped ({runner.stopped_reason}). Start a new one to continue.",
        )
    runner.tick()
    runner.save()
    return {"ok": True, "state": runner.snapshot()}


@app.get("/forward/state", dependencies=[Depends(check_engine_key)])
def forward_state(
    session: UserSession = Depends(current_user),
    strategy_id: str = Depends(strategy_param),
) -> dict[str, Any]:
    """One strategy's run, plus a roll-call of every strategy this user has.

    `running` and `state` keep their exact meaning for the strategy asked for,
    so a dashboard that sends no strategy sees precisely what it saw before.
    `strategies` is additive, for a UI that wants to show both at once.
    """
    runner = session.runner_for(strategy_id)
    return {
        "running": runner is not None and runner.stopped_reason is None,
        "state": runner.snapshot() if runner is not None else None,
        "strategies": {
            sid: {"running": r.stopped_reason is None} for sid, r in session.runners().items()
        },
    }


@app.get("/forward/ticks", dependencies=[Depends(check_engine_key)])
def forward_ticks(
    limit: int = 900,
    session: UserSession = Depends(current_user),
    strategy_id: str = Depends(strategy_param),
) -> dict[str, Any]:
    """The live chart's history.

    Held in the database rather than the browser, so a page reload -- or a
    second device -- picks up the whole session instead of redrawing from an
    empty series.
    """
    runner = session.runner_for(strategy_id)
    if runner is None or not runner.session_id:
        return {"ticks": []}
    return {"ticks": store.ticks(runner.session_id, limit=max(1, min(limit, 5_000)))}


@app.get("/forward/history", dependencies=[Depends(check_engine_key)])
def forward_history(session: UserSession = Depends(current_user)) -> dict[str, Any]:
    return {"sessions": store.forward_history(session.user_id)}


@app.post("/forward/stop", dependencies=[Depends(check_engine_key)])
def forward_stop(
    session: UserSession = Depends(current_user),
    strategy_id: str = Depends(strategy_param),
) -> dict[str, Any]:
    """Stop one strategy's run. Never anyone else's, never the other one."""
    runner = _require_runner(session, strategy_id)
    runner.stopped_reason = "stopped by user"
    runner.emit("warn", "Forward run stopped by user")
    runner.save()
    if runner.session_id:
        store.mark_stopped(runner.session_id, "stopped by user")
    state = runner.snapshot()
    session.set_runner(strategy_id, None)
    return {"ok": True, "state": state}


def _require_runner(session: UserSession, strategy_id: str = LADDER) -> ForwardRunner:
    runner = session.runner_for(strategy_id)
    if runner is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"No {strategy_id} forward run started for this session.",
        )
    return runner


def _state_path(session: UserSession, strategy_id: str = LADDER):
    """Where a run's convenience snapshot is written.

    Named per strategy: one path per user meant a second strategy would
    overwrite the first's file on every tick. The database is the source of
    truth either way, so this only ever cost an operator a confusing file --
    but a confusing file during an incident is exactly when it matters.
    """
    from pathlib import Path

    base = Path(os.environ.get("ENGINE_STATE_DIR", "engine/state"))
    return base / f"live-{session.user_id}-{strategy_id}.json"


@app.exception_handler(ChoiceError)
def choice_error_handler(_request: Request, exc: ChoiceError):
    from fastapi.responses import JSONResponse

    code = status.HTTP_401_UNAUTHORIZED if isinstance(exc, ChoiceAuthError) else status.HTTP_502_BAD_GATEWAY
    return JSONResponse(status_code=code, content={"detail": str(exc)})
