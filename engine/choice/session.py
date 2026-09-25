"""Hardened session/transport layer over the Choice FinX API.

We wrap the upstream ``choice_api`` (kkunal) concepts rather than forking the
library, but every HTTP call goes through :meth:`ChoiceSession.request` here
instead of ``client.request`` so we can add the things the library lacks:
explicit timeouts, a token bucket, exponential backoff with jitter, typed
errors, and automatic re-login when the day-scoped session expires.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import Callable, Any

import requests

from engine.choice.errors import (
    ChoiceAuthError,
    ChoiceError,
    ChoiceRateLimitError,
    ChoiceSessionRejected,
    ChoiceTransportError,
    StaticIpRejectedError,
    remember_secret,
    scrub,
)
from engine.choice.ratelimit import TokenBucket
from engine.config import ChoiceConfig, choice_config

log = logging.getLogger(__name__)

# A broker may legitimately answer with an HTTP-date rather than a number
# (RFC 7231), and may ask for an unreasonable wait. float() on the former
# raises ValueError, which escapes request() -- ForwardRunner.tick catches only
# ChoiceError, so the polling thread died silently while /forward/state
# happily kept reporting the run as live.
MAX_RETRY_AFTER = 120.0


def _retry_after_seconds(value: str | None) -> float | None:
    """Seconds to wait, from a numeric or HTTP-date Retry-After. Capped."""
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            from email.utils import parsedate_to_datetime

            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        seconds = (when - dt.datetime.now(tz=dt.timezone.utc)).total_seconds()
    if seconds <= 0:
        return None
    # Honouring an unbounded value would park the engine for days and drain
    # the rate-limit bucket into deep negative territory behind it.
    return min(seconds, MAX_RETRY_AFTER)

# Choice exposes the same API on two gateways. If one is unreachable the other
# usually still answers, so a transport failure retries against the alternate
# before giving up -- this is a host outage, not a bad request.
GATEWAYS = (
    "https://finxomne.choiceindia.com",
    "https://finx.choiceindia.com",
)


def alternate_gateway(base_url: str) -> str | None:
    """The other Choice host, or None if this base URL is not a known gateway."""
    current = base_url.rstrip("/")
    for host in GATEWAYS:
        if host != current and current in GATEWAYS:
            return host
    return None

# The login endpoints themselves. A 401 from one of these means "these
# credentials are wrong", NOT "the session expired" -- so the re-login retry
# must never fire for them, or login() calls request() calls login() forever.
AUTH_ENDPOINTS = (
    "api/OpenAPIV1/LoginTOTP",
    "api/OpenAPIV1/GetClientLoginTOTP",
    "api/OpenAPIV1/ValidateTOTP",
)

# Interactive sign-in must fail fast. A user waiting on a login cannot sit
# through the full backoff budget a background backfill can afford.
AUTH_MAX_RETRIES = 1

# Substrings in a broker error body that mean "your IP is not the declared one".
_STATIC_IP_MARKERS = ("static ip", "ip not", "invalid ip", "ip address", "whitelist")
_AUTH_MARKERS = ("session", "unauthor", "expired", "invalid token", "login", "forbidden")


# ---------------------------------------------------------------- login ledger

#: No automatic re-login within this long of the account's previous login.
RELOGIN_COOLDOWN = dt.timedelta(minutes=30)
#: And never more than this many automatic logins for one account in a day:
#: the morning's renewal of yesterday's session, and one retry if it fails.
MAX_AUTOMATIC_LOGINS_PER_DAY = 2
#: No automatic login before this time of day, so an OTP never arrives in the
#: middle of the night. The market opens at 09:15; nothing needs Choice sooner.
FIRST_AUTOMATIC_LOGIN = dt.time(8, 0)
#: How often a session Choice keeps refusing is mentioned in the log.
REFUSAL_LOG_EVERY = dt.timedelta(minutes=10)

#: For a session that cannot be renewed, whatever the reason.
UNRENEWABLE_MESSAGE = (
    "This Choice session has expired and cannot be renewed automatically, because "
    "credentials are never stored. Sign out and sign in again to renew it."
)
NO_SESSION_MESSAGE = (
    "There is no Choice session to renew: it was signed out, or never opened. Signing "
    "in on the dashboard opens one (Choice texts you one OTP)."
)


def _default_ledger_path() -> Path:
    """engine/state/choice_logins.json, or under ENGINE_STATE_DIR if set."""
    base = os.environ.get("ENGINE_STATE_DIR")
    root = Path(base) if base else Path(__file__).resolve().parents[1] / "state"
    return root / "choice_logins.json"


class _LoginLedger:
    """Every Choice login, per account, remembered across engine restarts.

    A Choice login is not free: `LoginTOTP` sends the account holder an OTP.
    On 24 Sep the engine logged in 304 times before 08:35 -- one OTP every
    thirty seconds -- because two session objects held the same account, and
    each fresh login cancelled the other's session, whose next call was then
    rejected and logged in again. Nothing counted, so nothing stopped it.

    This is the backstop, whatever the cause of the next loop: an automatic
    login is refused before FIRST_AUTOMATIC_LOGIN, within RELOGIN_COOLDOWN of
    the account's last one, and after MAX_AUTOMATIC_LOGINS_PER_DAY in a day. A
    sign-in the user makes themselves is recorded but never refused here.

    An automatic attempt is charged *before* it is made (`claim_automatic`):
    the OTP goes out with the first of the three login calls, so an attempt
    that fails at the second or third has still cost one, and charged only on
    success, a login failing half-way could be retried without limit.

    It is kept on disk and re-read on every use, so an engine restart does not
    hand out a fresh allowance, and a login made by one of the command-line
    tools meanwhile is seen rather than overwritten.
    """

    def __init__(self, path: Path | None = None, *,
                 first_automatic_login: dt.time | None = FIRST_AUTOMATIC_LOGIN) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, dt.datetime] = {}
        self._automatic: dict[str, tuple[dt.date, int]] = {}
        self._path = path
        self._warned = False
        self.first_automatic_login = first_automatic_login

    def bind(self, path: Path | None) -> None:
        """Keep the ledger at `path` from now on (None: memory only). What it
        held in memory is dropped; the file is read on next use."""
        with self._lock:
            self._path = path
            self._last.clear()
            self._automatic.clear()
            self._warned = False

    def _sync_locked(self) -> None:
        """Merge in what the file holds: the later of each last login, and the
        larger count for the same day. Anything unreadable is ignored, loudly
        once, rather than stopping a login that already cost an OTP."""
        if self._path is None:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            if not self._warned:
                self._warned = True
                log.warning("Could not read the Choice login ledger %s: %s", self._path, exc)
            return
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(accounts, dict):
            return
        for account, entry in accounts.items():
            if not isinstance(entry, dict):
                continue
            try:
                last = entry.get("last")
                if last:
                    when = dt.datetime.fromisoformat(str(last))
                    if account not in self._last or when > self._last[account]:
                        self._last[account] = when
                day_text = entry.get("automatic_day")
                if day_text:
                    day = dt.date.fromisoformat(str(day_text))
                    count = int(entry.get("automatic_count") or 0)
                    held = self._automatic.get(account)
                    if held is None or day > held[0]:
                        self._automatic[account] = (day, count)
                    elif day == held[0]:
                        self._automatic[account] = (day, max(count, held[1]))
            except (TypeError, ValueError):
                continue

    def _save_locked(self) -> None:
        if self._path is None:
            return
        accounts: dict[str, dict[str, Any]] = {}
        for account in set(self._last) | set(self._automatic):
            entry: dict[str, Any] = {}
            if account in self._last:
                entry["last"] = self._last[account].isoformat()
            if account in self._automatic:
                day, count = self._automatic[account]
                entry["automatic_day"] = day.isoformat()
                entry["automatic_count"] = count
            accounts[account] = entry
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_name(f"{self._path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            temp.write_text(json.dumps({"accounts": accounts}, indent=1), encoding="utf-8")
            os.replace(temp, self._path)
        except OSError as exc:
            log.warning("Could not save the Choice login ledger %s: %s", self._path, exc)

    def _refusal_locked(self, account: str, now: dt.datetime) -> ChoiceAuthError | None:
        if self.first_automatic_login is not None and now.time() < self.first_automatic_login:
            return ChoiceAuthError(
                f"Automatic Choice logins wait until {self.first_automatic_login:%H:%M}, so an "
                "OTP never arrives in the middle of the night. Runs pick up then by themselves; "
                "signing in on the dashboard renews the session now (one OTP)."
            )
        last = self._last.get(account)
        if last is not None and now - last < RELOGIN_COOLDOWN:
            minutes = max(1, int((now - last).total_seconds() // 60))
            return ChoiceAuthError(
                f"Choice rejected a session that was renewed {minutes} minute(s) ago. "
                "The engine will not log in again by itself for now, because every "
                "Choice login texts you an OTP. Runs keep their positions; signing in "
                "again on the dashboard renews the session (one OTP)."
            )
        day, count = self._automatic.get(account, (now.date(), 0))
        if day == now.date() and count >= MAX_AUTOMATIC_LOGINS_PER_DAY:
            return ChoiceAuthError(
                f"This Choice account has already been logged in automatically {count} "
                "times today, and the engine will not do it again, because every login texts "
                "you an OTP. Runs keep their positions; signing in again on the dashboard "
                "renews the session (one OTP)."
            )
        return None

    def check_automatic(self, account: str, now: dt.datetime | None = None) -> None:
        """Raise if an automatic login for `account` would be refused now."""
        now = now or dt.datetime.now()
        with self._lock:
            self._sync_locked()
            refusal = self._refusal_locked(account, now)
        if refusal is not None:
            raise refusal

    def claim_automatic(self, account: str, now: dt.datetime | None = None) -> None:
        """Check and charge one automatic login in a single step, before it is
        attempted. Raises, charging nothing, when it would be refused."""
        now = now or dt.datetime.now()
        with self._lock:
            self._sync_locked()
            refusal = self._refusal_locked(account, now)
            if refusal is None:
                self._charge_locked(account, automatic=True, now=now)
        if refusal is not None:
            raise refusal

    def _charge_locked(self, account: str, *, automatic: bool, now: dt.datetime) -> None:
        self._last[account] = now
        if automatic:
            day, count = self._automatic.get(account, (now.date(), 0))
            if day != now.date():
                day, count = now.date(), 0
            self._automatic[account] = (day, count + 1)
        self._save_locked()

    def record(self, account: str, *, automatic: bool, now: dt.datetime | None = None) -> None:
        """Record a login made, or an OTP sent, outside `claim_automatic`."""
        now = now or dt.datetime.now()
        with self._lock:
            self._sync_locked()
            self._charge_locked(account, automatic=automatic, now=now)

    def automatic_today(self, account: str, now: dt.datetime | None = None) -> int:
        """Automatic logins charged to `account` today."""
        now = now or dt.datetime.now()
        with self._lock:
            self._sync_locked()
            day, count = self._automatic.get(account, (now.date(), 0))
            return count if day == now.date() else 0

    def forget(self) -> None:
        """For tests."""
        with self._lock:
            self._last.clear()
            self._automatic.clear()


def session_rejected_message(since: dt.datetime | None, error: object = None) -> str:
    """What to tell a user whose session, opened today, Choice is refusing."""
    when = f" since {since:%H:%M}" if since else ""
    said = f" (Choice said: {error})" if error else ""
    return (
        f"Choice has been refusing this account's session{when}{said}. The session was "
        "opened today and a Choice session lasts the whole day, so this is trouble on "
        "Choice's side rather than an expired login. The engine is not logging in again "
        "by itself, because every Choice login texts you an OTP and a new login would not "
        "fix this. Runs keep their positions and pick up again as soon as Choice accepts "
        "the session. If it is still refused later, signing in again renews it (one OTP)."
    )


LOGIN_LEDGER = _LoginLedger(_default_ledger_path())


class ChoiceSession:
    """Owns the authenticated Choice connection for the process."""

    def __init__(self, config: ChoiceConfig | None = None, *, interactive: bool = False) -> None:
        self.config = config or choice_config
        # A session a person drives from the command line: its logins are that
        # person's own doing, recorded as sign-ins, not charged to the engine's
        # automatic allowance -- two runs of a tool would otherwise use up the
        # morning renewal the live runs depend on.
        self.interactive = interactive
        remember_secret(self.config.api_key)
        self.session_id: str | None = None
        self.access_token: str | None = None
        self.bcast_ip: str | None = None
        self.bcast_port: int | None = None

        self._http = requests.Session()
        self._lock = threading.RLock()
        self._data_bucket = TokenBucket(self.config.data_rate_limit)
        self._order_bucket = TokenBucket(self.config.order_rate_limit)
        self._login_date: dt.date | None = None
        # Called after every successful login, so whoever stores this session
        # can store the new id -- otherwise an engine restart revives the old
        # one, is rejected, and spends another login (and another OTP).
        self.on_login: Callable[[], None] | None = None
        # Set when a transport failure moved us to the alternate gateway.
        self.active_base_url = self.config.base_url
        # When Choice last accepted this session, and since when it has been
        # refusing it. The first lets a sign-in reuse a session that is known
        # to work instead of spending an OTP on a new one; the second says,
        # on the dashboard, how long Choice has been refusing it.
        self.last_ok: dt.datetime | None = None
        self.rejected_since: dt.datetime | None = None
        self._refusal_logged: dt.datetime | None = None

    # ------------------------------------------------------------------ auth

    @staticmethod
    def _encode_mobile(mobile: str) -> str:
        return base64.b64encode(mobile.encode("utf-8")).decode("utf-8")

    def _headers(self, include_auth: bool = True) -> dict[str, str]:
        # Note the unusual scheme: the API key travels in a header literally
        # named "Bearer", while "Authorization" carries "SessionId <id>".
        headers = {
            "VendorId": self.config.vendor_id,
            "Bearer": self.config.api_key,
            "Content-Type": "application/json",
        }
        if include_auth and self.session_id:
            headers["Authorization"] = f"SessionId {self.session_id}"
        return headers

    @property
    def account(self) -> str:
        """The key Choice invalidates sessions by: one live session each."""
        return str(self.config.vendor_id or "")

    def login(self, force: bool = False, *, automatic: bool = True) -> str:
        """Complete the 3-step non-interactive TOTP login.

        Choice serves the OTP back to us from ``GetClientLoginTOTP``, so no
        human input or authenticator app is involved.
        """
        with self._lock:
            if not force and self.session_id and self._login_date == dt.date.today():
                return self.session_id

            automatic = automatic and not self.interactive
            self.config.require()
            if automatic:
                # Charged before LoginTOTP goes out, because that call is what
                # texts the OTP: a login that then fails has still cost one.
                LOGIN_LEDGER.claim_automatic(self.account)
            encoded = self._encode_mobile(self.config.mobile_no)

            r1 = self.request("POST", "api/OpenAPIV1/LoginTOTP", {"MobileNo": encoded}, require_auth=False, retry_auth=False)
            if str(r1.get("Status", "")).lower() != "success":
                raise ChoiceAuthError(f"LoginTOTP rejected: {r1.get('Message') or r1}", payload=r1)
            if not automatic:
                # An OTP is on its way: it starts the cooldown, so an automatic
                # login cannot follow a person's own on its heels.
                LOGIN_LEDGER.record(self.account, automatic=False)

            r2 = self.request("POST", "api/OpenAPIV1/GetClientLoginTOTP", {"MobileNo": encoded}, require_auth=False, retry_auth=False)
            otp = r2.get("Response")
            if otp in (None, ""):
                raise ChoiceAuthError(f"GetClientLoginTOTP returned no OTP: {r2.get('Message') or r2}", payload=r2)

            r3 = self.request(
                "POST",
                "api/OpenAPIV1/ValidateTOTP",
                {"MobileNo": encoded, "OTP": str(otp)},
                require_auth=False,
                retry_auth=False,
            )
            resp = r3.get("Response")

            if isinstance(resp, str) and resp:
                self.session_id = resp
                # kkunal leaves access_token as None on this branch, which then
                # sends an empty token to the price-feed logon. Apply the same
                # fallback its dict branch uses.
                self.access_token = self.config.api_key
            elif isinstance(resp, dict):
                self.session_id = resp.get("SessionId") or resp.get("session_id")
                self.access_token = resp.get("AccessToken") or self.config.api_key
                self.bcast_ip = resp.get("OdinBcastIP")
                try:
                    self.bcast_port = int(resp["OdinBcastPort"]) if resp.get("OdinBcastPort") else None
                except (TypeError, ValueError):
                    self.bcast_port = None
            else:
                raise ChoiceAuthError(f"ValidateTOTP returned unusable response: {r3}", payload=r3)

            if not self.session_id:
                raise ChoiceAuthError("Login succeeded but no SessionId was returned", payload=r3)

            # Registered by value, so they are redacted even when the broker
            # echoes one back inside prose the shape-matching patterns miss.
            remember_secret(self.session_id)
            remember_secret(self.access_token)

            self._login_date = dt.date.today()
            self.last_ok = dt.datetime.now()
            self.rejected_since = None
            self._refusal_logged = None
            log.info(
                "Choice login OK for %s (%s; session valid until end of day)",
                self.account, "automatic renewal" if automatic else "user sign-in",
            )
            if self.on_login is not None:
                try:
                    self.on_login()
                except Exception:                   # noqa: BLE001
                    log.exception("Could not store the renewed session")
            return self.session_id

    def proven_live(self, within: dt.timedelta, now: dt.datetime | None = None) -> bool:
        """Whether this session is known to work right now: opened today,
        accepted by Choice within `within`, and not refused since."""
        now = now or dt.datetime.now()
        # Each read once: other threads update these without the lock.
        last_ok, refused = self.last_ok, self.rejected_since
        return bool(
            self.session_id
            and self._login_date == now.date()
            and refused is None
            and last_ok is not None
            and now - last_ok <= within
        )

    def ensure_session(self) -> str:
        if self.session_id and self._login_date == dt.date.today():
            return self.session_id
        if self.load_session():
            return self.session_id  # type: ignore[return-value]
        return self.login()

    # ---------------------------------------------------------- persistence

    def save_session(self, path: Path | None = None) -> bool:
        """Cache the session to disk. No-op unless a path is configured.

        Writing this file stores a live session id and the raw API key in
        plaintext, so it happens only when a caller explicitly asks for it.
        """
        path = path or self.config.session_file
        if path is None:
            return False
        try:
            path.write_text(
                json.dumps(
                    {
                        "date": dt.date.today().isoformat(),
                        "session_id": self.session_id,
                        "access_token": self.access_token,
                        "bcast_ip": self.bcast_ip,
                        "bcast_port": self.bcast_port,
                    }
                ),
                encoding="utf-8",
            )
            return True
        except OSError as exc:
            log.warning("Could not persist session: %s", exc)
            return False

    def load_session(self, path: Path | None = None) -> bool:
        """Load a cached session, but only if it was created today.

        Choice sessions do not survive the trading day, so a stale file is
        worse than none: it produces confusing 401s deep inside a backfill.
        """
        path = path or self.config.session_file
        if path is None:
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return False
        if data.get("date") != dt.date.today().isoformat() or not data.get("session_id"):
            return False
        self.session_id = data["session_id"]
        self.access_token = data.get("access_token") or self.config.api_key
        self.bcast_ip = data.get("bcast_ip")
        self.bcast_port = data.get("bcast_port")
        self._login_date = dt.date.today()
        return True

    def logoff(self) -> None:
        if not self.session_id:
            return
        try:
            # Never renewed first: logging in only to log off would text the
            # account holder an OTP to end a session that is already dead.
            self.request("GET", "api/OpenAPI/LogOff", retry_auth=False)
        except ChoiceError as exc:
            log.warning("Logoff failed: %s", exc)
        finally:
            self.session_id = None
            self._login_date = None

    # ------------------------------------------------------------ transport

    def _classify(self, status_code: int, body: str, payload: Any) -> ChoiceError:
        low = body.lower()
        if status_code == 429:
            return ChoiceRateLimitError("Rate limited by Choice", payload=payload)
        if any(m in low for m in _STATIC_IP_MARKERS):
            return StaticIpRejectedError(f"HTTP {status_code}: {body[:300]}", payload=payload)
        if status_code in (401, 403) or any(m in low for m in _AUTH_MARKERS):
            return ChoiceAuthError(f"HTTP {status_code}: {body[:300]}", payload=payload)
        return ChoiceError(f"HTTP {status_code}: {body[:300]}", payload=payload)

    def _renew_after_rejection(self, sent_with: str | None, error: object = None) -> None:
        """Log in again after a rejection -- unless someone already has, or a
        login cannot be what fixes it.

        Several threads share one session (tick threads, market-status checks,
        backtests). When the day's session expires they are all rejected at
        once, and each used to log in on its own: one OTP per caller.

        Only a session from an earlier day is renewed. A Choice session lasts
        the whole day, so one opened today being refused is Choice's trouble,
        not an expiry -- and on 25 Sep four automatic logins into such a spell
        cured nothing and texted the account holder four OTPs. That refusal is
        raised as ChoiceSessionRejected and the session is kept: callers try
        again later, and it works again as soon as Choice accepts it.
        """
        with self._lock:
            if self.session_id and self.session_id != sent_with:
                log.info("Session already renewed by another caller; retrying on it")
                return
            if not self.session_id or self._login_date is None:
                # Signed out (logoff clears both) or never opened: nothing to
                # renew. A login here would open a session for someone who has
                # just ended theirs -- and text them an OTP for it.
                raise ChoiceAuthError(NO_SESSION_MESSAGE)
            if self._login_date >= dt.date.today():
                if not (self.config.api_key and self.config.mobile_no):
                    # Restored from a row sealed before credentials were kept:
                    # dated today because it cannot be renewed, not because it
                    # is new. Only a sign-in replaces it.
                    raise ChoiceAuthError(UNRENEWABLE_MESSAGE)
                now = dt.datetime.now()
                if self.rejected_since is None:
                    self.rejected_since = now
                # Once per spell and then every few minutes, not once per call:
                # when one endpoint is refused and another accepted, a spell
                # can open and close on every tick.
                if self._refusal_logged is None or now - self._refusal_logged >= REFUSAL_LOG_EVERY:
                    self._refusal_logged = now
                    log.warning(
                        "Choice refused %s's session, which was opened today (%s). Not "
                        "logging in again: a login would not fix it and would text the "
                        "account holder an OTP. Keeping the session and trying again later.",
                        self.account, error or "no reason given",
                    )
                raise ChoiceSessionRejected(session_rejected_message(self.rejected_since, error))
            self.login(force=True)

    def request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        *,
        require_auth: bool = True,
        is_order: bool = False,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        """Perform one Choice API call with pacing, retries and typed errors."""
        url = f"{self.active_base_url}/{endpoint.lstrip('/')}"
        # The session this call goes out with. If it is rejected but another
        # caller has renewed the session meanwhile, the call is retried on the
        # new one instead of logging in yet again.
        sent_with = self.session_id
        bucket = self._order_bucket if is_order else self._data_bucket
        timeout = (self.config.connect_timeout, self.config.read_timeout)
        last: Exception | None = None

        # Belt and braces: a login endpoint can never trigger a re-login, no
        # matter what the caller passed.
        is_auth_call = any(endpoint.lstrip("/").startswith(a) for a in AUTH_ENDPOINTS)
        if is_auth_call:
            retry_auth = False
        max_retries = AUTH_MAX_RETRIES if is_auth_call else self.config.max_retries
        if endpoint.lstrip("/").startswith(AUTH_ENDPOINTS[0]):
            # LoginTOTP is the call that texts the OTP. If it timed out, the OTP
            # may well have gone; asking again could send a second.
            max_retries = 0

        for attempt in range(max_retries + 1):
            bucket.acquire()
            try:
                resp = self._http.request(
                    method.upper(), url, headers=self._headers(require_auth), json=data, timeout=timeout
                )
            except requests.Timeout as exc:
                last = ChoiceTransportError(f"Timeout calling {endpoint}: {exc}")
            except requests.RequestException as exc:
                last = ChoiceTransportError(f"Transport error calling {endpoint}: {scrub(exc)}")
                other = alternate_gateway(self.active_base_url)
                if other:
                    log.warning("Gateway %s unreachable; switching to %s", self.active_base_url, other)
                    self.active_base_url = other
                    url = f"{other}/{endpoint.lstrip('/')}"
            else:
                if resp.status_code == 429:
                    retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
                    bucket.penalise(retry_after or 2.0)
                    last = ChoiceRateLimitError("Rate limited by Choice", retry_after=retry_after)
                elif 500 <= resp.status_code < 600:
                    last = ChoiceError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                elif resp.status_code >= 400:
                    err = self._classify(resp.status_code, resp.text, None)
                    # A dead session is recoverable exactly once: re-login and
                    # replay. Anything else at 4xx is the caller's problem.
                    if isinstance(err, ChoiceAuthError) and not isinstance(err, StaticIpRejectedError) and retry_auth:
                        # Choice's own words, so a refusal that is not really an
                        # expired session (as MarketStatus's was not) shows up as such.
                        log.info("Session rejected (%s) on %s", err, endpoint)
                        try:
                            self._renew_after_rejection(sent_with, err)
                        except ChoiceError:
                            raise
                        except Exception as exc:      # noqa: BLE001
                            # A session revived from storage holds no
                            # credentials -- they are deliberately never
                            # persisted in a usable form -- so `login` raises a
                            # bare RuntimeError about missing environment
                            # variables. That is not a `ChoiceError`, so it
                            # escaped the runner's quote handling entirely and
                            # reached the "unexpected error" branch, which
                            # *stops* the run. Two live campaigns with open
                            # positions were stopped that way by a session
                            # expiring overnight. It is an authentication
                            # failure and nothing else, so it is typed as one:
                            # the run pauses marking and waits to be signed in
                            # again, keeping its book.
                            raise ChoiceAuthError(UNRENEWABLE_MESSAGE) from exc
                        return self.request(
                            method, endpoint, data, require_auth=require_auth, is_order=is_order, retry_auth=False
                        )
                    raise err
                else:
                    if require_auth and self.session_id and not is_auth_call:
                        # Accepted: the session works, whatever the body says.
                        now = dt.datetime.now()
                        self.last_ok = now
                        # Read once: another thread may clear it meanwhile, and
                        # arithmetic on the None it leaves raised a TypeError
                        # that escaped every handler and stopped a live run.
                        since = self.rejected_since
                        if since is not None:
                            self.rejected_since = None
                            if now - since >= dt.timedelta(minutes=1):
                                log.info(
                                    "Choice is accepting %s's session again, after refusing "
                                    "it since %s", self.account, f"{since:%H:%M}",
                                )
                    try:
                        parsed = resp.json()
                    except ValueError as exc:
                        raise ChoiceError(
                            f"Non-JSON response from {endpoint} (HTTP {resp.status_code}): {resp.text[:200]}"
                        ) from exc
                    return parsed if isinstance(parsed, dict) else {"Response": parsed, "Status": "Success"}

            if attempt < max_retries:
                delay = min(30.0, 2.0**attempt) * (0.5 + random.random())
                if isinstance(last, ChoiceRateLimitError) and last.retry_after:
                    delay = max(delay, last.retry_after)
                log.warning(
                    "%s failed (%s); retry %d/%d in %.1fs",
                    endpoint, last, attempt + 1, max_retries, delay,
                )
                time.sleep(delay)

        assert last is not None
        raise last
