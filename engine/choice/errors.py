"""Typed exceptions for the Choice FinX adapter.

The upstream ``kkunal`` library raises bare ``Exception`` with the raw HTTP
response body interpolated into the message, which means (a) callers cannot
catch selectively and (b) credentials leak into logs.  Everything here is
catchable by type and scrubs secrets before the message is ever formatted.
"""

from __future__ import annotations

import re
from typing import Any

# Values that must never reach a log line or an HTTP response.
#
# The *shape* matters as much as the key name. A Choice response is normally
# interpolated as a Python dict repr, which quotes with ' rather than ", so a
# pattern accepting only double quotes matched nothing on exactly the strings
# that carry a live SessionId and AccessToken. Bearer tokens and OTPs arrive
# whitespace-separated, which a ':'/'=' pattern also misses.
_SECRET_KEYS = (
    r"(?:Bearer|VendorId|vendor_?id|api_?key|AccessToken|access_token|SessionId"
    r"|session_?id|OTP|token|mobile(?:_?no)?|password)"
)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # key: value / key = value, value optionally quoted with either quote.
    re.compile(
        r"""(['"]?""" + _SECRET_KEYS + r"""['"]?\s*[:=]\s*)(['"]?)([^'",;&\s}\]]{4,})""",
        re.I,
    ),
    # `Bearer <token>`, `OTP 483920` -- whitespace-separated, no delimiter.
    re.compile(r"\b(" + _SECRET_KEYS + r")(\s+)([A-Za-z0-9._-]{4,})", re.I),
    re.compile(r"(eyJ[A-Za-z0-9_-]{8,})\.[A-Za-z0-9._-]+"),  # JWTs
)


def scrub(text: Any) -> str:
    """Redact anything that looks like a credential."""
    s = str(text)
    s = _SECRET_PATTERNS[0].sub(r"\1\2<redacted>", s)
    s = _SECRET_PATTERNS[1].sub(r"\1\2<redacted>", s)
    s = _SECRET_PATTERNS[2].sub(r"\1.<redacted>", s)
    return s

class ChoiceError(Exception):
    """Base class for every failure originating from the Choice API."""

    def __init__(self, message: str, *, payload: Any = None, status: str | None = None) -> None:
        self.raw_message = str(message)
        self.status = status
        self.payload = payload
        super().__init__(scrub(message))

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = scrub(self.raw_message)
        if self.status:
            base = f"[{self.status}] {base}"
        return base


class ChoiceAuthError(ChoiceError):
    """Session expired, rejected, or never established.

    Choice sessions are day-scoped, so this is expected once per trading day
    and should trigger a re-login rather than a hard failure.
    """


class ChoiceRateLimitError(ChoiceError):
    """HTTP 429 or a broker-side throttle.  Carries ``retry_after`` seconds."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any) -> None:
        self.retry_after = retry_after
        super().__init__(message, **kw)


class ChoiceTransportError(ChoiceError):
    """Network-level failure: timeout, DNS, connection reset, bad TLS."""


class ChoiceDateError(ChoiceError, ValueError):
    """An unparseable date was supplied to a history request.

    ``kkunal`` silently turns these into ``0`` (= 1980-01-01), which makes the
    caller unknowingly request 45 years of data.  We refuse instead.
    """


class ChoiceHistoryError(ChoiceError):
    """The ChartData endpoint returned a non-Success status.

    Distinct from :class:`ChoiceNoDataError`: this means the request *failed*.
    """


class ChoiceNoDataError(ChoiceError):
    """ChartData succeeded but the window genuinely contains no bars.

    A market holiday or a contract that did not exist yet.  Callers normally
    record this in ``data_coverage`` and move on, rather than retrying.
    """


class ChoiceInstrumentError(ChoiceError):
    """A contract could not be resolved in the scrip master."""


class ChoiceOrderError(ChoiceError):
    """An order was rejected, or could not be placed/modified/cancelled."""


class StaticIpRejectedError(ChoiceAuthError):
    """The request did not originate from the declared static IP.

    Choice binds every API key to one or more declared static IPs and rejects
    everything else (Integration Guide Sec 8).  This is the single most likely
    failure when the engine is accidentally run from a laptop on a different
    network, a VPN, or a cloud host with dynamic egress — so it gets its own
    type and an actionable message.
    """

    HINT = (
        "Request rejected as coming from an undeclared IP. Check your current "
        "public IP matches the static IP registered against this API key at "
        "finx.choiceindia.com -> Profile -> Settings -> Generate API Key. "
        "VPNs and proxies will always fail this check."
    )

    def __init__(self, message: str = "", **kw: Any) -> None:
        super().__init__(f"{message}\n{self.HINT}".strip(), **kw)
