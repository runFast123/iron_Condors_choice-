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

import datetime as dt
import logging
import os
import threading
import uuid
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
from engine.store.db import Store
from engine.pricing.costs import CostModel
from engine.strategy.condor import StrategyConfig

log = logging.getLogger(__name__)

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

app = FastAPI(title="Iron Condor Ladder engine", version="1.0.0", docs_url=None, redoc_url=None)

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
    # Weekly or monthly contracts. Read by the job runner all along, but never
    # sent by anything, so every run silently used weeklies.
    expiry_cadence: str = Field(default="weekly", pattern="^(weekly|monthly)$")
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


class StartForwardRequest(BaseModel):
    lots: int = Field(default=1, ge=1, le=100)
    step: float = Field(default=100.0, gt=0, le=5000)
    poll_seconds: float = Field(default=15.0, ge=5, le=300)
    max_condors: int = Field(default=20, ge=1, le=100)
    # Must match whatever the backtest used, or the forward run is testing a
    # different strategy from the one that justified it.
    expiry_cadence: str = Field(default="weekly", pattern="^(weekly|monthly)$")
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
        "forward_running": bool(session and session.runner is not None),
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


def _resume_forward(session: UserSession) -> bool:
    """Pick a forward run back up after an engine restart.

    A run cannot be resumed until someone supplies the Choice credentials it
    needs for quotes -- those are deliberately never persisted -- so the point
    of resumption is login, not startup. Until then the run sits in the
    database marked running, which is the truth: it has positions open and a
    ladder mid-flight, it simply has nobody to ask for prices.
    """
    if session.runner is not None:
        return False
    try:
        pending = [r for r in store.running_forwards() if r["user_id"] == session.user_id]
    except Exception:                               # noqa: BLE001
        log.exception("Could not read saved forward runs")
        return False
    if not pending:
        return False

    # Only one run can be driven at a time, so the newest is resumed -- but the
    # older ones must not be left marked `running` forever. They would be
    # re-read on every login and would never become resumable, while claiming
    # to have live positions. Retire them explicitly so the record is honest.
    record = pending[-1]
    for stale in pending[:-1]:
        log.warning(
            "Superseded forward run %s for user %s; retiring it",
            stale["session_id"], session.user_id,
        )
        try:
            store.mark_stopped(
                stale["session_id"],
                "superseded by a newer run; not resumed after the engine restarted",
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
            state_path=_state_path(session), store=store,
            session_id=record["session_id"], user_id=session.user_id,
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
    session.runner = runner
    runner.emit(
        "info", "Forward run resumed after an engine restart",
        condors=len([c for c in runner.condors if c.is_open]),
        fired=len(runner.ladder.fired_levels),
    )
    _start_tick_thread(runner, session, poll_seconds=15.0)
    log.info("Resumed forward run %s for user %s", record["session_id"], session.user_id)
    return True


def _start_tick_thread(runner: ForwardRunner, session: UserSession, poll_seconds: float) -> None:
    thread = threading.Thread(
        target=runner.run,
        kwargs={"poll_seconds": max(5.0, float(poll_seconds))},
        name=f"forward-{session.user_id}",
        daemon=True,
    )
    thread.start()


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
    if session.runner is not None and session.runner.stopped_reason is None:
        return {"ok": True, "already_running": True, "state": session.runner.snapshot()}

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
            strike_step=strike_step,
            take_profit_pct=body.take_profit,
            stop_loss_mult=body.stop_loss,
        ),
        costs=CostModel(),
        expiry_cadence=body.expiry_cadence,
        # Per-user state file: one user's run must never overwrite another's.
        state_path=_state_path(session),
        store=store,
        session_id=uuid.uuid4().hex,
        user_id=session.user_id,
    )
    session.runner = runner

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
def forward_tick(session: UserSession = Depends(current_user)) -> dict[str, Any]:
    """Force one polling cycle. Refused once the run has stopped.

    Without this guard the kill switch was advisory: a run that had tripped its
    daily loss limit reported itself stopped and then opened fresh condors on
    the next manual tick.
    """
    runner = _require_runner(session)
    if runner.stopped_reason:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This run has stopped ({runner.stopped_reason}). Start a new one to continue.",
        )
    runner.tick()
    runner.save()
    return {"ok": True, "state": runner.snapshot()}


@app.get("/forward/state", dependencies=[Depends(check_engine_key)])
def forward_state(session: UserSession = Depends(current_user)) -> dict[str, Any]:
    if session.runner is None:
        return {"running": False, "state": None}
    return {"running": session.runner.stopped_reason is None, "state": session.runner.snapshot()}


@app.get("/forward/ticks", dependencies=[Depends(check_engine_key)])
def forward_ticks(
    limit: int = 900, session: UserSession = Depends(current_user)
) -> dict[str, Any]:
    """The live chart's history.

    Held in the database rather than the browser, so a page reload -- or a
    second device -- picks up the whole session instead of redrawing from an
    empty series.
    """
    runner = session.runner
    if runner is None or not runner.session_id:
        return {"ticks": []}
    return {"ticks": store.ticks(runner.session_id, limit=max(1, min(limit, 5_000)))}


@app.get("/forward/history", dependencies=[Depends(check_engine_key)])
def forward_history(session: UserSession = Depends(current_user)) -> dict[str, Any]:
    return {"sessions": store.forward_history(session.user_id)}


@app.post("/forward/stop", dependencies=[Depends(check_engine_key)])
def forward_stop(session: UserSession = Depends(current_user)) -> dict[str, Any]:
    runner = _require_runner(session)
    runner.stopped_reason = "stopped by user"
    runner.emit("warn", "Forward run stopped by user")
    runner.save()
    if runner.session_id:
        store.mark_stopped(runner.session_id, "stopped by user")
    state = runner.snapshot()
    session.runner = None
    return {"ok": True, "state": state}


def _require_runner(session: UserSession) -> ForwardRunner:
    if session.runner is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "No forward run started for this session.")
    return session.runner


def _state_path(session: UserSession):
    from pathlib import Path

    base = Path(os.environ.get("ENGINE_STATE_DIR", "engine/state"))
    return base / f"live-{session.user_id}.json"


@app.exception_handler(ChoiceError)
def choice_error_handler(_request: Request, exc: ChoiceError):
    from fastapi.responses import JSONResponse

    code = status.HTTP_401_UNAUTHORIZED if isinstance(exc, ChoiceAuthError) else status.HTTP_502_BAD_GATEWAY
    return JSONResponse(status_code=code, content={"detail": str(exc)})
