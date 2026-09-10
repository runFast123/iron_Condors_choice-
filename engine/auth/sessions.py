"""Multi-user session registry.

Each user brings their own Choice credentials; the server never has a master
key and never trades on behalf of someone who has not authenticated. A login
here *is* a Choice login: if Choice rejects the credentials there is no
session, so only real Choice account holders can get in and there is no
separate password for us to leak.

Deliberate choices, and their reasons:

* **Credentials live in memory only.** They are held inside the user's
  :class:`ChoiceSession` for the life of the session and never written to disk,
  logged, or returned to the client. Persisting them would make this process a
  far more attractive target than it needs to be.
* **Sessions expire with Choice's own.** Choice sessions are day-scoped, so
  ours are too -- a token that outlived the broker session would only produce
  confusing 401s deep inside a request.
* **The token is opaque and random**, not a signed blob of user data, so it
  carries nothing an attacker could read or tamper with offline.
* **User ids are derived, not raw.** A mobile number is PII and a login
  identifier; the id exposed to the app is a salted hash of it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

from engine.choice.errors import ChoiceAuthError, ChoiceError
from engine.auth.persistence import SessionVault, is_expired, token_fingerprint
from engine.choice.session import ChoiceSession
from engine.config import IST, ChoiceConfig

log = logging.getLogger(__name__)

# Choice sessions die at end of day; ours must not outlive them.
MARKET_DAY_END = dt.time(23, 59, 0)

# Salt for deriving user ids. Resolved lazily so the engine can install a
# salt persisted in its database: a per-process salt rotates every user's id
# on restart, which orphans their saved runs under an id that no longer
# resolves. USER_ID_SALT still wins if it is set explicitly.
_ID_SALT: bytes | None = None


def set_id_salt(salt: bytes) -> None:
    """Install a durable salt. Call before the first login."""
    global _ID_SALT
    _ID_SALT = salt


def _id_salt() -> bytes:
    global _ID_SALT
    if _ID_SALT is None:
        _ID_SALT = os.environ.get("USER_ID_SALT", "").encode() or secrets.token_bytes(16)
    return _ID_SALT

MAX_SESSIONS = int(os.environ.get("ENGINE_MAX_SESSIONS", "50") or 50)


def derive_user_id(mobile: str) -> str:
    """Stable, non-reversible id for a mobile number."""
    digest = hmac.new(_id_salt(), mobile.strip().encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:16]


def mask_mobile(mobile: str) -> str:
    """Show only the last two digits, for display."""
    digits = "".join(ch for ch in str(mobile) if ch.isdigit())
    return f"{'*' * max(0, len(digits) - 2)}{digits[-2:]}" if len(digits) >= 2 else "****"


def _parse_iso(value: Any) -> dt.datetime | None:
    """Parse a stored timestamp, or None if it is unusable."""
    try:
        return dt.datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def end_of_day(now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now(tz=IST)
    return dt.datetime.combine(now.date(), MARKET_DAY_END, tzinfo=IST)


@dataclass
class UserSession:
    user_id: str
    token: str
    choice: ChoiceSession
    mobile_masked: str
    vendor_id: str
    created_at: dt.datetime
    expires_at: dt.datetime
    last_seen: dt.datetime
    profile: dict[str, Any] = field(default_factory=dict)
    # Populated lazily: loading a scrip master per user is expensive.
    market: Any = None
    runner: Any = None

    @property
    def expired(self) -> bool:
        return dt.datetime.now(tz=IST) >= self.expires_at

    def touch(self) -> None:
        self.last_seen = dt.datetime.now(tz=IST)

    def public(self) -> dict[str, Any]:
        """Only what is safe to hand the browser. Never credentials."""
        return {
            "user_id": self.user_id,
            "mobile": self.mobile_masked,
            "vendor_id": self.vendor_id,
            "name": self.profile.get("name") or self.profile.get("Name") or None,
            "client_code": self.profile.get("ClientCode") or self.profile.get("UCC") or None,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "has_market_data": self.market is not None,
            "forward_running": self.runner is not None,
        }


class LoginThrottle:
    """Per-identifier attempt limiter.

    Without this, the login endpoint is an oracle for guessing API keys, and
    every guess costs Choice a request.
    """

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300) -> None:
        self.max_attempts = max_attempts
        self.window = dt.timedelta(seconds=window_seconds)
        self._hits: dict[str, list[dt.datetime]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = dt.datetime.now(tz=IST)
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.max_attempts:
                retry_in = int((self.window - (now - hits[0])).total_seconds())
                raise ChoiceAuthError(
                    f"Too many failed login attempts. Try again in {retry_in}s."
                )
            self._hits[key] = hits

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._hits.setdefault(key, []).append(dt.datetime.now(tz=IST))

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


class SessionRegistry:
    """Holds one authenticated Choice session per logged-in user."""

    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._sessions: dict[str, UserSession] = {}
        self._by_user: dict[str, str] = {}
        self._lock = threading.RLock()
        self._throttle = LoginThrottle()
        self.max_sessions = max_sessions
        # Durable backing, attached by the API once the database is open.
        # Optional so the registry keeps working -- in memory only -- for
        # tests and for an engine started without a shared secret.
        self._db: Any = None
        self._vault: SessionVault | None = None

    def bind_storage(self, db: Any, secret: str | None) -> None:
        """Let sessions survive a restart.

        Without this the registry behaves exactly as it did before: memory
        only, and every restart signs everyone out.
        """
        vault = SessionVault(secret)
        if not vault.enabled:
            log.warning(
                "No ENGINE_SHARED_SECRET, so sessions are not persisted. "
                "Every engine restart will sign users out."
            )
            return
        self._db = db
        self._vault = vault
        try:
            dropped = db.purge_expired_auth_sessions(dt.datetime.now(tz=IST).isoformat())
            if dropped:
                log.info("Purged %d expired session(s) on startup", dropped)
        except Exception:                           # noqa: BLE001
            log.exception("Could not purge expired sessions")

    # ------------------------------------------------------------------ auth

    def login(self, vendor_id: str, api_key: str, mobile: str) -> UserSession:
        """Authenticate against Choice and open a server session.

        Raises :class:`ChoiceAuthError` if Choice rejects the credentials --
        we never mint a session for an unverified user.
        """
        vendor_id = (vendor_id or "").strip()
        api_key = (api_key or "").strip()
        mobile = (mobile or "").strip()
        if not (vendor_id and api_key and mobile):
            raise ChoiceAuthError("Vendor ID, API key and mobile number are all required.")

        user_id = derive_user_id(mobile)
        self._throttle.check(user_id)

        config = ChoiceConfig(vendor_id=vendor_id, api_key=api_key, mobile_no=mobile)
        choice = ChoiceSession(config)
        try:
            choice.login(force=True)
        except ChoiceError as exc:
            self._throttle.record_failure(user_id)
            log.warning("Login failed for user %s: %s", user_id, exc)
            raise
        self._throttle.clear(user_id)

        profile: dict[str, Any] = {}
        try:
            resp = choice.request("GET", "api/OpenAPI/UserProfile")
            body = resp.get("Response")
            if isinstance(body, dict):
                profile = body
        except ChoiceError as exc:
            # Not fatal: the session is valid even if the profile call is not
            # available for this account type.
            log.info("Could not load profile for %s: %s", user_id, exc)

        now = dt.datetime.now(tz=IST)
        session = UserSession(
            user_id=user_id,
            token=secrets.token_urlsafe(32),
            choice=choice,
            mobile_masked=mask_mobile(mobile),
            vendor_id=vendor_id,
            created_at=now,
            expires_at=end_of_day(now),
            last_seen=now,
            profile=profile,
        )

        with self._lock:
            self._sweep_locked()
            # One live session per user: a second login replaces the first,
            # so a leaked token cannot outlive a re-login.
            previous = self._by_user.get(user_id)
            if previous:
                # Must go through _drop_locked: popping the session leaves its
                # forward runner ticking on a thread nobody owns, and the next
                # login resumes the same run from the database -- two ladders
                # driving one run id, both writing the whole state, last writer
                # wins. A second browser tab was enough to trigger it.
                self._drop_locked(previous)
            if len(self._sessions) >= self.max_sessions:
                oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
                self._drop_locked(oldest.token)
            self._sessions[session.token] = session
            self._by_user[user_id] = session.token

        self._persist(session)
        log.info("User %s logged in (vendor %s)", user_id, vendor_id)
        return session

    def _persist(self, session: UserSession) -> None:
        """Write the session so a restart can pick it back up.

        Only the Choice session id and the profile go in. The API key is not
        needed after login -- requests carry `Authorization: SessionId ...` --
        so it is never written, and a leaked row is useless once the trading
        day ends or from any IP but the declared one.
        """
        if self._db is None or self._vault is None:
            return
        sealed = self._vault.seal({
            "session_id": session.choice.session_id,
            "bcast_ip": getattr(session.choice, "bcast_ip", None),
            "bcast_port": getattr(session.choice, "bcast_port", None),
            "profile": session.profile,
            "created_at": session.created_at.isoformat(),
        })
        if sealed is None:
            return
        try:
            self._db.save_auth_session(
                token_hash=token_fingerprint(session.token),
                user_id=session.user_id,
                vendor_id=session.vendor_id,
                mobile_masked=session.mobile_masked,
                payload=sealed,
                expires_at=session.expires_at.isoformat(),
            )
        except Exception:                           # noqa: BLE001
            # A session that cannot be saved still works for this process.
            log.exception("Could not persist the session for %s", session.user_id)

    def get(self, token: str | None) -> UserSession | None:
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session is not None:
                if session.expired:
                    self._drop_locked(token)
                    return None
                session.touch()
                return session

        # Not in memory. It may still be a valid session this process has not
        # seen -- the usual case being that the engine restarted underneath a
        # browser that is still holding a perfectly good cookie.
        return self._rehydrate(token)

    def _rehydrate(self, token: str) -> UserSession | None:
        """Rebuild a session from durable storage, or None."""
        if self._db is None or self._vault is None:
            return None
        try:
            row = self._db.auth_session(token_fingerprint(token))
        except Exception:                           # noqa: BLE001
            log.exception("Could not read a stored session")
            return None
        if row is None:
            return None

        now = dt.datetime.now(tz=IST)
        if is_expired(row["expires_at"], now):
            self._db.drop_auth_session(token_hash=row["token_hash"])
            return None

        payload = self._vault.open(row["payload"])
        if payload is None or not payload.get("session_id"):
            # Unreadable means the shared secret changed, which is how an
            # operator revokes every session. Honour it.
            self._db.drop_auth_session(token_hash=row["token_hash"])
            return None

        choice = ChoiceSession(config=ChoiceConfig(vendor_id=row["vendor_id"]))
        choice.session_id = payload["session_id"]
        choice.bcast_ip = payload.get("bcast_ip")
        choice.bcast_port = payload.get("bcast_port")
        # The API key was never stored, so this session cannot re-login on its
        # own. Marking the login date keeps `ensure_session` from trying.
        choice._login_date = now.date()

        session = UserSession(
            user_id=row["user_id"],
            token=token,
            choice=choice,
            mobile_masked=row["mobile_masked"],
            vendor_id=row["vendor_id"],
            created_at=_parse_iso(payload.get("created_at")) or now,
            expires_at=_parse_iso(row["expires_at"]) or end_of_day(now),
            last_seen=now,
            profile=payload.get("profile") or {},
        )

        with self._lock:
            previous = self._by_user.get(session.user_id)
            if previous and previous != token:
                self._drop_locked(previous)
            self._sessions[token] = session
            self._by_user[session.user_id] = token

        log.info("Restored session for %s after a restart", session.user_id)
        return session

    def require(self, token: str | None) -> UserSession:
        session = self.get(token)
        if session is None:
            raise ChoiceAuthError("Not signed in, or the session has expired.")
        return session

    def logout(self, token: str | None) -> bool:
        """Drop the session, then tell Choice.

        The registry lock guards every authenticated request, and `logoff()` is
        a network call that can retry for minutes against a slow broker.
        Holding the lock across it stalls every other user's request for as
        long as it takes, so the session is removed under the lock and the
        round-trip happens outside it. Dropping first is also the safer order:
        if the broker call hangs, the session is already gone here.
        """
        if not token:
            return False
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return False
            self._drop_locked(token)

        try:
            session.choice.logoff()
        except ChoiceError as exc:
            log.info("Choice logoff failed for %s (session already dropped): %s",
                     session.user_id, exc)
        return True

    # ------------------------------------------------------------- internals

    def _drop_locked(self, token: str) -> None:
        if self._db is not None:
            try:
                self._db.drop_auth_session(token_hash=token_fingerprint(token))
            except Exception:                       # noqa: BLE001
                log.exception("Could not remove a stored session")
        session = self._sessions.pop(token, None)
        if session is not None:
            runner = getattr(session, "runner", None)
            if runner is not None:
                # Suspend, do not stop. Ending a session means the engine can
                # no longer fetch quotes for this user; it does not mean the
                # user decided to close a ladder that still holds positions.
                # Marking it stopped made the run unresumable, and the only
                # way back was editing the database by hand.
                runner.suspend("session ended")
            if self._by_user.get(session.user_id) == token:
                self._by_user.pop(session.user_id, None)

    def _sweep_locked(self) -> None:
        for token in [t for t, s in self._sessions.items() if s.expired]:
            self._drop_locked(token)

    def sweep(self) -> int:
        with self._lock:
            before = len(self._sessions)
            self._sweep_locked()
            return before - len(self._sessions)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "sessions": len(self._sessions),
                "max_sessions": self.max_sessions,
                "users": sorted({s.user_id for s in self._sessions.values()}),
            }


registry = SessionRegistry()
