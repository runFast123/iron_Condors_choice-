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
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from engine.auth.sessions import UserSession, registry
from engine.choice.netinfo import egress_ip
from engine.choice.errors import (
    ChoiceAuthError,
    ChoiceError,
    ChoiceInstrumentError,
    StaticIpRejectedError,
)
from engine.config import IST, engine_config
from engine.data.market import NIFTY, ChoiceMarketData
from engine.forward.runner import ForwardRunner, market_is_open
from engine.pricing.costs import CostModel
from engine.strategy.condor import StrategyConfig

log = logging.getLogger(__name__)

ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("ENGINE_ALLOWED_ORIGINS", "").split(",") if o.strip()
]

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


class StartForwardRequest(BaseModel):
    mode: str = Field(default="paper", pattern="^(paper|live)$")
    arm: bool = False
    lots: int = Field(default=1, ge=1, le=100)
    step: float = Field(default=100.0, gt=0)


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
    return {"token": session.token, "user": session.public()}


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


# ------------------------------------------------------------ forward testing


@app.post("/forward/start", dependencies=[Depends(check_engine_key)])
def forward_start(
    body: StartForwardRequest,
    session: UserSession = Depends(current_user),
    market: ChoiceMarketData = Depends(user_market),
) -> dict[str, Any]:
    if session.runner is not None and session.runner.stopped_reason is None:
        return {"ok": True, "already_running": True, "state": session.runner.snapshot()}

    if body.mode == "live" and not body.arm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Live mode requires an explicit arm. Send arm=true to place real orders.",
        )

    try:
        lot_size = market.master.lot_size_for(NIFTY)
        expiry = market.master.nearest_expiry(NIFTY, dt.datetime.now(tz=IST).date(), min_days=0)
        strike_step = market.master.strike_step(NIFTY, expiry)
    except ChoiceInstrumentError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    runner = ForwardRunner(
        market=market,
        strategy=StrategyConfig(
            step=body.step, lots=body.lots, lot_size=lot_size,
            max_condors=engine_config.max_condors, strike_step=strike_step,
        ),
        mode=body.mode,
        costs=CostModel(),
        # Per-user state file: one user's run must never overwrite another's.
        state_path=_state_path(session),
    )
    if body.mode == "live" and body.arm:
        runner.arm()
    session.runner = runner
    runner.tick()
    runner.save()
    return {"ok": True, "state": runner.snapshot()}


@app.post("/forward/tick", dependencies=[Depends(check_engine_key)])
def forward_tick(session: UserSession = Depends(current_user)) -> dict[str, Any]:
    runner = _require_runner(session)
    runner.tick()
    runner.save()
    return {"ok": True, "state": runner.snapshot()}


@app.get("/forward/state", dependencies=[Depends(check_engine_key)])
def forward_state(session: UserSession = Depends(current_user)) -> dict[str, Any]:
    if session.runner is None:
        return {"running": False, "state": None}
    return {"running": session.runner.stopped_reason is None, "state": session.runner.snapshot()}


@app.post("/forward/stop", dependencies=[Depends(check_engine_key)])
def forward_stop(session: UserSession = Depends(current_user)) -> dict[str, Any]:
    runner = _require_runner(session)
    runner.stopped_reason = "stopped by user"
    runner.armed = False
    runner.emit("warn", "Forward run stopped by user")
    runner.save()
    return {"ok": True, "state": runner.snapshot()}


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
