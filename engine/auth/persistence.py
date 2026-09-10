"""Keep a signed-in session across an engine restart.

Sessions used to live only in memory, so every restart signed every user out
mid-run -- twelve times in one morning while a bug was being fixed, and once
per crash, reboot or deploy in normal use. On a multi-user platform that is not
a minor annoyance: the dashboard drops to the login page while a forward run is
mid-ladder.

What is stored is deliberately the *smallest* thing that works:

* Requests authenticate with ``Authorization: SessionId <session_id>``, so the
  API key is not needed after login and is never written here.
* A Choice session dies at end of day regardless, so the row expires with it.
  The blast radius of a leaked database is one trading day.
* Choice binds each API key to a declared IP, so a stolen session id is refused
  from anywhere else.

The encryption is honest about what it is worth: the key is derived from
ENGINE_SHARED_SECRET, which lives on the same machine as the database. It
protects against a stray backup or a copied file, not against someone who owns
the box. Defence in depth, not a vault.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)


def token_fingerprint(token: str) -> str:
    """A one-way handle for a bearer token.

    The token itself is never stored: it is the credential the browser holds,
    and a database that contains it is a database that can impersonate every
    signed-in user. A SHA-256 of it is enough to look a session up.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _key_from(secret: str) -> bytes:
    """A Fernet key derived from the engine's shared secret."""
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class SessionVault:
    """Encrypts and decrypts the per-session payload.

    Disabled -- rather than insecure -- when there is no shared secret. An
    engine running without one already accepts every caller, so writing
    plaintext broker sessions next to it would add a second problem without
    fixing the first.
    """

    def __init__(self, secret: str | None) -> None:
        self._fernet = Fernet(_key_from(secret)) if secret else None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def seal(self, payload: dict[str, Any]) -> str | None:
        if self._fernet is None:
            return None
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(raw).decode("ascii")

    def open(self, sealed: str) -> dict[str, Any] | None:
        """Decrypt, or None if the secret has changed or the row is corrupt.

        A rotated shared secret makes every stored session unreadable, which is
        the correct outcome: rotating it is how an operator revokes access, and
        silently ignoring that would defeat the point.
        """
        if self._fernet is None:
            return None
        try:
            return json.loads(self._fernet.decrypt(sealed.encode("ascii")))
        except (InvalidToken, ValueError, json.JSONDecodeError):
            return None


def is_expired(expires_at: str, now: dt.datetime) -> bool:
    """Whether a stored session has passed its expiry."""
    try:
        when = dt.datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return True
    if when.tzinfo is None:
        return True
    return now >= when
