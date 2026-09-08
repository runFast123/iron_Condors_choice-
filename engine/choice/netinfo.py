"""Which IP address Choice actually sees.

Choice binds every API key to a declared static IP and rejects everything else
(Integration Guide Sec 8). It is the single most common reason a set of
perfectly valid credentials fails, so the app should be able to *tell* the user
which address to register rather than leaving them to guess.

One correction worth stating plainly, because it is easy to get backwards:
forwarding the browser's address in ``X-Forwarded-For`` does **not** change
what Choice enforces on. An IP allowlist is applied to the TCP source address
of the connection, which is this engine's egress IP. A header is just a claim
in the request body and cannot move the packet's origin. So the useful thing is
to report *this machine's* public IP, which is the one that must be declared.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
import time
import urllib.request

log = logging.getLogger(__name__)

# Plain IP-echo endpoints. These are not market data -- they answer "what is my
# own public address", which cannot be obtained any other way from inside a
# NATed host. Set ENGINE_PUBLIC_IP to skip the lookup entirely.
_ECHO_SERVICES = (
    ("https://api.ipify.org?format=json", "ip"),
    ("https://ifconfig.co/json", "ip"),
)

_CACHE_TTL = 900.0  # seconds; a static IP does not move, but a restart might


class EgressIp:
    """Caches this process's public IP, with a configured override."""

    def __init__(self) -> None:
        self._value: str | None = None
        self._checked_at: float = 0.0
        self._source: str = "unknown"
        self._lock = threading.Lock()

    def _configured(self) -> str | None:
        raw = (os.environ.get("ENGINE_PUBLIC_IP") or "").strip()
        if not raw:
            return None
        try:
            ipaddress.ip_address(raw)
        except ValueError:
            log.warning("ENGINE_PUBLIC_IP=%r is not a valid IP address; ignoring", raw)
            return None
        return raw

    def get(self, refresh: bool = False) -> dict[str, str | None]:
        override = self._configured()
        if override:
            return {"ip": override, "source": "ENGINE_PUBLIC_IP", "note": None}

        with self._lock:
            fresh = time.monotonic() - self._checked_at < _CACHE_TTL
            if self._value and fresh and not refresh:
                return {"ip": self._value, "source": self._source, "note": None}

            for url, key in _ECHO_SERVICES:
                try:
                    request = urllib.request.Request(url, headers={"User-Agent": "condor-ladder"})
                    with urllib.request.urlopen(request, timeout=6) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    candidate = str(payload.get(key, "")).strip()
                    ipaddress.ip_address(candidate)
                except Exception as exc:  # noqa: BLE001 - any failure just means "try the next one"
                    log.debug("IP lookup via %s failed: %s", url, exc)
                    continue
                self._value = candidate
                self._source = url.split("/")[2]
                self._checked_at = time.monotonic()
                return {"ip": candidate, "source": self._source, "note": None}

            return {
                "ip": None,
                "source": "unavailable",
                "note": (
                    "Could not determine this server's public IP. Set ENGINE_PUBLIC_IP "
                    "to the address you declared with Choice."
                ),
            }


egress_ip = EgressIp()
