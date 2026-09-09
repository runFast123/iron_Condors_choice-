"""Engine configuration, loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

# Indian markets. Every timestamp inside the engine is tz-aware in IST;
# naive datetimes are rejected at the boundary rather than guessed at.
IST = ZoneInfo("Asia/Kolkata")

# The Choice ChartData API expresses times as seconds since this epoch.
CHOICE_EPOCH_YEAR = 1980

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the engine has no hard dependency on python-dotenv."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.split("#", 1)[0].strip().strip("'\"")
        os.environ.setdefault(key, val)


_load_dotenv(REPO_ROOT / ".env")


def _f(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _i(key: str, default: int) -> int:
    try:
        return int(float(os.environ.get(key, "") or default))
    except ValueError:
        return default


@dataclass(frozen=True)
class ChoiceConfig:
    vendor_id: str = field(default_factory=lambda: os.environ.get("CHOICE_VENDOR_ID", ""))
    api_key: str = field(default_factory=lambda: os.environ.get("CHOICE_API_KEY", ""))
    mobile_no: str = field(default_factory=lambda: os.environ.get("CHOICE_MOBILE_NO", ""))
    base_url: str = field(
        default_factory=lambda: os.environ.get("CHOICE_BASE_URL", "https://finxomne.choiceindia.com").rstrip("/")
    )

    # kkunal passes no timeout to requests at all, so a hung broker endpoint
    # blocks the engine forever. Always send an explicit (connect, read) pair.
    connect_timeout: float = field(default_factory=lambda: _f("ENGINE_HTTP_CONNECT_TIMEOUT", 5.0))
    read_timeout: float = field(default_factory=lambda: _f("ENGINE_HTTP_READ_TIMEOUT", 30.0))

    data_rate_limit: float = field(default_factory=lambda: _f("ENGINE_DATA_RATE_LIMIT", 3.0))
    # Kept below 10/sec deliberately: at or above that threshold SEBI/NSE
    # require the strategy to be registered as an Algorithm with the exchange
    # (Integration Guide Sec 9).
    order_rate_limit: float = field(default_factory=lambda: _f("ENGINE_ORDER_RATE_LIMIT", 5.0))

    max_retries: int = field(default_factory=lambda: _i("ENGINE_MAX_RETRIES", 4))
    # Off by default, and deliberately not a shared path.
    #
    # This is a multi-user engine: each signed-in user has their own Choice
    # session held in memory. A single default file meant every user wrote
    # their live session id and raw API key over the previous user's, and any
    # code path that read it back adopted whoever wrote last -- one user's
    # requests going out under another's broker session. The same file has
    # already leaked once. Sessions now stay in memory unless a single-user
    # tool explicitly asks for a cache and names its own path.
    session_file: Path | None = None

    @property
    def configured(self) -> bool:
        return bool(self.vendor_id and self.api_key and self.mobile_no)

    def require(self) -> None:
        missing = [
            name
            for name, val in (
                ("CHOICE_VENDOR_ID", self.vendor_id),
                ("CHOICE_API_KEY", self.api_key),
                ("CHOICE_MOBILE_NO", self.mobile_no),
            )
            if not val
        ]
        if missing:
            raise RuntimeError(
                "Missing Choice credentials: " + ", ".join(missing) + ". Copy .env.example to .env and fill them in."
            )


@dataclass(frozen=True)
class EngineConfig:
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))
    shared_secret: str = field(default_factory=lambda: os.environ.get("ENGINE_SHARED_SECRET", ""))
    mode: str = field(default_factory=lambda: os.environ.get("ENGINE_MODE", "paper").lower())
    max_condors: int = field(default_factory=lambda: _i("ENGINE_MAX_CONDORS", 20))
    daily_loss_limit: float = field(default_factory=lambda: _f("ENGINE_DAILY_LOSS_LIMIT", 25_000.0))
    log_level: str = field(default_factory=lambda: os.environ.get("ENGINE_LOG_LEVEL", "INFO").upper())

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


choice_config = ChoiceConfig()
engine_config = EngineConfig()
