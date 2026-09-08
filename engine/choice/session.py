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
import random
import threading
import time
from pathlib import Path
from typing import Any

import requests

from engine.choice.errors import (
    ChoiceAuthError,
    ChoiceError,
    ChoiceRateLimitError,
    ChoiceTransportError,
    StaticIpRejectedError,
    scrub,
)
from engine.choice.ratelimit import TokenBucket
from engine.config import ChoiceConfig, choice_config

log = logging.getLogger(__name__)

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

# Substrings in a broker error body that mean "your IP is not the declared one".
_STATIC_IP_MARKERS = ("static ip", "ip not", "invalid ip", "ip address", "whitelist")
_AUTH_MARKERS = ("session", "unauthor", "expired", "invalid token", "login", "forbidden")


class ChoiceSession:
    """Owns the authenticated Choice connection for the process."""

    def __init__(self, config: ChoiceConfig | None = None) -> None:
        self.config = config or choice_config
        self.session_id: str | None = None
        self.access_token: str | None = None
        self.bcast_ip: str | None = None
        self.bcast_port: int | None = None

        self._http = requests.Session()
        self._lock = threading.RLock()
        self._data_bucket = TokenBucket(self.config.data_rate_limit)
        self._order_bucket = TokenBucket(self.config.order_rate_limit)
        self._login_date: dt.date | None = None
        # Set when a transport failure moved us to the alternate gateway.
        self.active_base_url = self.config.base_url

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

    def login(self, force: bool = False) -> str:
        """Complete the 3-step non-interactive TOTP login.

        Choice serves the OTP back to us from ``GetClientLoginTOTP``, so no
        human input or authenticator app is involved.
        """
        with self._lock:
            if not force and self.session_id and self._login_date == dt.date.today():
                return self.session_id

            self.config.require()
            encoded = self._encode_mobile(self.config.mobile_no)

            r1 = self.request("POST", "api/OpenAPIV1/LoginTOTP", {"MobileNo": encoded}, require_auth=False)
            if str(r1.get("Status", "")).lower() != "success":
                raise ChoiceAuthError(f"LoginTOTP rejected: {r1.get('Message') or r1}", payload=r1)

            r2 = self.request("POST", "api/OpenAPIV1/GetClientLoginTOTP", {"MobileNo": encoded}, require_auth=False)
            otp = r2.get("Response")
            if otp in (None, ""):
                raise ChoiceAuthError(f"GetClientLoginTOTP returned no OTP: {r2.get('Message') or r2}", payload=r2)

            r3 = self.request(
                "POST", "api/OpenAPIV1/ValidateTOTP", {"MobileNo": encoded, "OTP": str(otp)}, require_auth=False
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

            self._login_date = dt.date.today()
            log.info("Choice login OK (session established, valid until end of day)")
            return self.session_id

    def ensure_session(self) -> str:
        if self.session_id and self._login_date == dt.date.today():
            return self.session_id
        if self.load_session():
            return self.session_id  # type: ignore[return-value]
        return self.login()

    # ---------------------------------------------------------- persistence

    def save_session(self, path: Path | None = None) -> bool:
        path = path or self.config.session_file
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
            self.request("GET", "api/OpenAPI/LogOff")
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
        bucket = self._order_bucket if is_order else self._data_bucket
        timeout = (self.config.connect_timeout, self.config.read_timeout)
        last: Exception | None = None

        for attempt in range(self.config.max_retries + 1):
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
                    retry_after = float(resp.headers.get("Retry-After") or 0) or None
                    bucket.penalise(retry_after or 2.0)
                    last = ChoiceRateLimitError("Rate limited by Choice", retry_after=retry_after)
                elif 500 <= resp.status_code < 600:
                    last = ChoiceError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                elif resp.status_code >= 400:
                    err = self._classify(resp.status_code, resp.text, None)
                    # A dead session is recoverable exactly once: re-login and
                    # replay. Anything else at 4xx is the caller's problem.
                    if isinstance(err, ChoiceAuthError) and not isinstance(err, StaticIpRejectedError) and retry_auth:
                        log.info("Session rejected; re-authenticating and retrying %s", endpoint)
                        self.login(force=True)
                        return self.request(
                            method, endpoint, data, require_auth=require_auth, is_order=is_order, retry_auth=False
                        )
                    raise err
                else:
                    try:
                        parsed = resp.json()
                    except ValueError as exc:
                        raise ChoiceError(
                            f"Non-JSON response from {endpoint} (HTTP {resp.status_code}): {resp.text[:200]}"
                        ) from exc
                    return parsed if isinstance(parsed, dict) else {"Response": parsed, "Status": "Success"}

            if attempt < self.config.max_retries:
                delay = min(30.0, 2.0**attempt) * (0.5 + random.random())
                if isinstance(last, ChoiceRateLimitError) and last.retry_after:
                    delay = max(delay, last.retry_after)
                log.warning(
                    "%s failed (%s); retry %d/%d in %.1fs",
                    endpoint, last, attempt + 1, self.config.max_retries, delay,
                )
                time.sleep(delay)

        assert last is not None
        raise last
