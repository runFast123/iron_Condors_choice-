"""Groww's backtesting API: the backtest's backup source.

Choice is the source. This is asked only by a backtest, and only for what
Choice could not supply:

  * an option contract Choice has no usable history for -- above all an
    expired one, which Choice answers with an empty series; and
  * a trading day Choice returned no NIFTY or India VIX bars for.

Never by a live run, which trades on the broker it is connected to.

Groww's backtesting endpoints serve exact contracts, expired ones included,
back to 2020, by a symbol such as ``NSE-NIFTY-30Mar26-23600-PE``. They need a
paid Groww API plan and a token that expires every morning at six. Given an
API key and the TOTP secret behind its QR code, this mints a fresh token when
it needs one, so nothing has to be approved by hand each day.

Nothing that reaches the dashboard names the provider: to a user it is "the
backup source". Every message raised from here is worded that way, because
the notes and warnings a backtest collects are shown on its Data Health page.
Only this file and the engine's own log say Groww.

Bars come back in Choice's shape -- IST, stamped one second before the bar
closes, the way Choice stamps a candle with its last trade. Groww stamps a
candle with its start. Moved forward, a backup bar can never look as if it
were known before it closed; if the assumption about Groww's stamps were ever
wrong the other way, the cost is a bar one interval stale, never one from the
future.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import logging
import os
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd
import requests

from engine.choice.errors import remember_secret
from engine.config import IST

log = logging.getLogger(__name__)

BASE_URL = "https://api.groww.in"
TOKEN_PATH = "/v1/token/api/access"
CANDLES_PATH = "/v1/historical/candles"
CONTRACTS_PATH = "/v1/historical/contracts"

#: Choice resolution -> Groww candle interval.
INTERVALS = {
    "1": "1minute", "5": "5minute", "10": "10minute", "15": "15minute", "30": "30minute",
    "60": "1hour", "D": "1day", "W": "1week", "M": "1month",
}
_MINUTES = {"1": 1, "5": 5, "10": 10, "15": 15, "30": 30, "60": 60}

#: Most days one request may span, per Groww's limits for each interval.
MAX_SPAN_DAYS = {"1": 30, "5": 30, "10": 90, "15": 90, "30": 90, "60": 180, "D": 180, "W": 180, "M": 180}

#: The indices there is a backup for, as Groww names them.
INDEX_SYMBOLS = {"NIFTY": "NSE-NIFTY", "INDIAVIX": "NSE-INDIAVIX"}

#: Groww's derivative history starts here.
EARLIEST = dt.date(2020, 1, 1)

# Hard-coded rather than strftime("%b"), which follows the Windows locale and
# has already broken a URL in this codebase once.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

COLUMNS = ["ts", "open", "high", "low", "close", "volume", "oi"]

# Environment variables. Neutral names: they appear in the operator's own
# .env.engine.local and nowhere a user looks.
ENV_API_KEY = "BACKUP_DATA_API_KEY"
ENV_TOTP_SECRET = "BACKUP_DATA_TOTP_SECRET"
ENV_API_SECRET = "BACKUP_DATA_API_SECRET"
ENV_ACCESS_TOKEN = "BACKUP_DATA_ACCESS_TOKEN"
ENV_RATE = "BACKUP_DATA_RATE"


class BackupUnavailable(Exception):
    """The backup source could not supply what was asked, and why.

    The message is shown to users as it stands, so it never names the
    provider.
    """


# ----------------------------------------------------------------- sign-in


def totp(secret: str, at: float | None = None, *, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 time-based one-time code, as an authenticator app shows it."""
    clean = secret.replace(" ", "").upper()
    key = base64.b32decode(clean + "=" * (-len(clean) % 8))
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return f"{code:0{digits}d}"


@dataclass(frozen=True)
class Credentials:
    """How to sign in. Any one of the three ways is enough.

    * an API key with its TOTP secret: a token is minted whenever one is
      needed, with no daily approval -- the way to leave it running;
    * an API key with its secret: the same, but Groww wants the key approved
      on its website every day before it will mint;
    * a ready access token, pasted in: valid until six the next morning.
    """

    api_key: str = ""
    totp_secret: str = ""
    api_secret: str = ""
    access_token: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Credentials":
        env = os.environ if env is None else env
        return cls(
            api_key=(env.get(ENV_API_KEY) or "").strip(),
            totp_secret=(env.get(ENV_TOTP_SECRET) or "").strip(),
            api_secret=(env.get(ENV_API_SECRET) or "").strip(),
            access_token=(env.get(ENV_ACCESS_TOKEN) or "").strip(),
        )

    @property
    def usable(self) -> bool:
        return bool(self.access_token or (self.api_key and (self.totp_secret or self.api_secret)))


def _next_six_am(now: float) -> float:
    """When a Groww token lapses: six the next morning, IST."""
    moment = dt.datetime.fromtimestamp(now, tz=IST)
    six = moment.replace(hour=6, minute=0, second=0, microsecond=0)
    if moment >= six:
        six += dt.timedelta(days=1)
    return six.timestamp()


# ------------------------------------------------------------------ client


class GrowwBackup:
    """The backup source, as a backtest uses it."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        http: Any = None,
        rate_per_second: float = 4.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        timeout: tuple[float, float] = (5.0, 30.0),
        max_retries: int = 3,
    ) -> None:
        self.credentials = credentials
        self.http = http or requests.Session()
        self.min_interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self.clock, self.sleep = clock, sleep
        self.timeout = timeout
        self.max_retries = max_retries
        self._token: str | None = credentials.access_token or None
        self._token_expires = _next_six_am(clock()) if self._token else 0.0
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._contracts: dict[dt.date, set[str] | None] = {}
        self.requests_made = 0
        for secret in (credentials.api_key, credentials.totp_secret,
                       credentials.api_secret, credentials.access_token):
            remember_secret(secret)

    # -------------------------------------------------------- token and HTTP

    def _mint(self) -> str:
        """A fresh access token, from whichever sign-in the credentials allow."""
        creds = self.credentials
        if not creds.api_key or not (creds.totp_secret or creds.api_secret):
            raise BackupUnavailable(
                "the backup source's access token has expired and no key is configured to renew it"
            )
        if creds.totp_secret:
            body = {"key_type": "totp", "totp": totp(creds.totp_secret, self.clock())}
        else:
            stamp = str(int(self.clock()))
            body = {
                "key_type": "approval",
                "checksum": hashlib.sha256((creds.api_secret + stamp).encode()).hexdigest(),
                "timestamp": stamp,
            }
        try:
            resp = self.http.post(
                BASE_URL + TOKEN_PATH, json=body, timeout=self.timeout,
                headers={"Authorization": f"Bearer {creds.api_key}",
                         "Content-Type": "application/json", "Accept": "application/json"},
            )
        except requests.RequestException as exc:
            raise BackupUnavailable(f"could not reach the backup source to sign in ({type(exc).__name__})") from exc
        self.requests_made += 1
        data = _json(resp)
        token = _find(data, "token") or _find(data, "access_token")
        if resp.status_code >= 400 or not token:
            reason = _error_text(data) or f"HTTP {resp.status_code}"
            raise BackupUnavailable(
                f"the backup source refused to sign in ({reason})"
                + ("; a key signed in with its secret must be approved each day" if not creds.totp_secret else "")
            )
        remember_secret(token)
        self._token = str(token)
        expiry = _find(data, "expiry")
        try:
            self._token_expires = pd.Timestamp(expiry).timestamp() if expiry else _next_six_am(self.clock())
        except (TypeError, ValueError):
            self._token_expires = _next_six_am(self.clock())
        log.info("Signed in to the backup source (Groww); token valid until %s",
                 dt.datetime.fromtimestamp(self._token_expires, tz=IST).isoformat())
        return self._token

    def _bearer(self) -> str:
        with self._lock:
            # A minute of slack, so a token is never sent in its last seconds.
            if self._token is None or self.clock() >= self._token_expires - 60:
                self._token = None
                return self._mint()
            return self._token

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_request + self.min_interval - self.clock()
            if wait > 0:
                self.sleep(wait)
            self._last_request = self.clock()

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        """GET with rate limiting, retries on throttling and server errors,
        and one fresh sign-in when a token turns out to be stale."""
        renewed = False
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            token = self._bearer()
            self._throttle()
            try:
                resp = self.http.get(
                    BASE_URL + path, params=params, timeout=self.timeout,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                             "X-API-VERSION": "1.0"},
                )
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise BackupUnavailable(f"the backup source did not answer ({type(exc).__name__})") from exc
                self.sleep(delay)
                delay *= 2
                continue
            self.requests_made += 1
            if resp.status_code in (401, 403) and not renewed and self.credentials.api_key:
                with self._lock:
                    self._token = None
                renewed = True
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= self.max_retries:
                    raise BackupUnavailable(f"the backup source is busy (HTTP {resp.status_code})")
                retry_after = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
                try:
                    pause = float(retry_after) if retry_after else delay
                except ValueError:
                    pause = delay
                self.sleep(pause)
                delay *= 2
                continue
            data = _json(resp)
            if resp.status_code >= 400:
                raise BackupUnavailable(
                    f"the backup source refused the request ({_error_text(data) or f'HTTP {resp.status_code}'})"
                )
            if isinstance(data, dict) and str(data.get("status", "")).upper() == "FAILURE":
                raise BackupUnavailable(f"the backup source refused the request ({_error_text(data)})")
            return data.get("payload", data) if isinstance(data, dict) else data
        raise BackupUnavailable("the backup source did not answer")          # pragma: no cover

    # ------------------------------------------------------------ candles

    def _candles(self, symbol: str, segment: str, start: dt.date, end: dt.date, resolution: str) -> pd.DataFrame:
        interval = INTERVALS.get(resolution)
        if interval is None:
            raise BackupUnavailable(f"the backup source has no {resolution} bars")
        start = max(start, EARLIEST)
        if start > end:
            return _empty()
        span = dt.timedelta(days=MAX_SPAN_DAYS[resolution] - 1)
        frames: list[pd.DataFrame] = []
        window = start
        while window <= end:
            upto = min(end, window + span)
            payload = self._get(CANDLES_PATH, {
                "exchange": "NSE", "segment": segment, "groww_symbol": symbol,
                "start_time": f"{window:%Y-%m-%d} 00:00:00",
                "end_time": f"{upto:%Y-%m-%d} 23:59:59",
                "candle_interval": interval,
            })
            frames.append(to_choice_shape(_find(payload, "candles") or [], resolution))
            window = upto + dt.timedelta(days=1)
        frames = [f for f in frames if not f.empty]
        if not frames:
            return _empty()
        merged = pd.concat(frames, ignore_index=True)
        return merged.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)

    def index_candles(self, name: str, start: dt.date, end: dt.date, resolution: str) -> pd.DataFrame:
        symbol = INDEX_SYMBOLS.get(name)
        if symbol is None:
            raise BackupUnavailable(f"the backup source has no series for {name}")
        return self._candles(symbol, "CASH", start, end, resolution)

    @staticmethod
    def contract_symbol(expiry: dt.date, strike: float, right: str) -> str:
        """Groww's name for a NIFTY option, e.g. NSE-NIFTY-30Mar26-23600-PE."""
        return f"NSE-NIFTY-{expiry.day:02d}{_MONTHS[expiry.month - 1]}{expiry:%y}-{strike:g}-{right}"

    def _listed(self, expiry: dt.date) -> set[str] | None:
        """Every NIFTY contract Groww lists for an expiry, or None if unknown."""
        if expiry not in self._contracts:
            try:
                payload = self._get(CONTRACTS_PATH, {
                    "exchange": "NSE", "underlying_symbol": "NIFTY", "expiry_date": f"{expiry:%Y-%m-%d}",
                })
                listed = payload if isinstance(payload, list) else (
                    _find(payload, "contracts") or _find(payload, "groww_symbols") or []
                )
                self._contracts[expiry] = {str(s) for s in listed} or None
            except BackupUnavailable as exc:
                log.info("Backup source could not list contracts for %s: %s", expiry, exc)
                self._contracts[expiry] = None
        return self._contracts[expiry]

    def option_candles(
        self, expiry: dt.date, strike: float, right: str, start: dt.date, end: dt.date, resolution: str
    ) -> pd.DataFrame:
        """One option contract's bars, nothing after its own expiry."""
        symbol = self.contract_symbol(expiry, strike, right)
        listed = self._listed(expiry)
        if listed is not None and symbol not in listed:
            return _empty()                     # the exchange never listed it
        frame = self._candles(symbol, "FNO", start, min(end, expiry), resolution)
        if frame.empty:
            return frame
        close = dt.datetime.combine(expiry, dt.time(15, 30), tzinfo=IST)
        return frame[frame["ts"] <= close].reset_index(drop=True)

    # ------------------------------------------- what a backtest asks for

    def fill_missing_days(
        self, choice: pd.DataFrame, name: str, days_needed: set[dt.date], resolution: str
    ) -> tuple[pd.DataFrame, list[dt.date], str | None]:
        """Choice's bars, plus the backup's for each needed day Choice has none for.

        Whole days only. A day Choice served in part is left as Choice served
        it: splicing two feeds inside one session would put two sources' idea
        of the same minute side by side, and a gap is easier to see than a seam.
        Returns the merged frame, the days taken from the backup, and a note
        when it could not cover everything (None when it did or was not needed).
        """
        have = set(choice["ts"].dt.date) if choice is not None and not choice.empty else set()
        missing = sorted(d for d in days_needed if d not in have)
        if not missing:
            return choice, [], None
        try:
            backup = self.index_candles(name, missing[0], missing[-1], resolution)
        except BackupUnavailable as exc:
            log.warning("No backup for %s on %d day(s): %s", name, len(missing), exc)
            return choice, [], f"{len(missing)} day(s) missing from Choice; {exc}"
        backup = backup[backup["ts"].dt.date.isin(set(missing))] if not backup.empty else backup
        taken = sorted(set(backup["ts"].dt.date)) if not backup.empty else []
        if not taken:
            return choice, [], f"{len(missing)} day(s) missing from Choice and from the backup source"
        if choice is not None and not choice.empty:
            backup = backup.assign(ts=backup["ts"].dt.tz_convert(choice["ts"].dt.tz))
            merged = pd.concat([choice, backup], ignore_index=True)
        else:
            merged = backup
        merged = merged.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
        log.warning("%s: Choice returned nothing for %d day(s); took %d from the backup source (Groww)",
                    name, len(missing), len(taken))
        left = len(missing) - len(taken)
        return merged, taken, (
            f"{left} day(s) missing from Choice and from the backup source" if left else None
        )

    def daily_closes(self, name: str, days: list[dt.date]) -> tuple[dict[dt.date, float], str | None]:
        """The backup's daily close for each of `days`, where it has one."""
        if not days:
            return {}, None
        try:
            frame = self.index_candles(name, min(days), max(days), "D")
        except BackupUnavailable as exc:
            return {}, str(exc)
        wanted = set(days)
        return {
            row.ts.date(): float(row.close) for row in frame.itertuples() if row.ts.date() in wanted
        }, None


# ------------------------------------------------------------------ shapes


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS)


def _stamp(value: Any) -> pd.Timestamp | None:
    """A candle's time as Groww sent it: ISO text in IST, or epoch seconds."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000.0 if value > 1e12 else float(value)
            return pd.Timestamp(seconds, unit="s", tz="UTC").tz_convert(IST)
        stamp = pd.Timestamp(str(value))
    except (TypeError, ValueError):
        return None
    return stamp.tz_localize(IST) if stamp.tzinfo is None else stamp.tz_convert(IST)


def to_choice_shape(candles: list, resolution: str) -> pd.DataFrame:
    """Groww's candle rows as a frame shaped like Choice's.

    Each row is [time, open, high, low, close, volume, open interest]. Intraday
    bars are restamped one second before they close; longer ones at midnight
    of their first day, as Choice stamps them.
    """
    minutes = _MINUTES.get(resolution)
    rows = []
    for candle in candles or []:
        if not isinstance(candle, (list, tuple)) or len(candle) < 5:
            continue
        stamp = _stamp(candle[0])
        try:
            close = float(candle[4])
        except (TypeError, ValueError):
            continue
        if stamp is None or not close > 0:
            continue                            # a bar without a close is not a price
        if minutes is not None:
            stamp = stamp + pd.Timedelta(minutes=minutes) - pd.Timedelta(seconds=1)
        else:
            stamp = stamp.normalize()

        def num(i: int) -> float:
            try:
                return float(candle[i]) if len(candle) > i and candle[i] is not None else float("nan")
            except (TypeError, ValueError):
                return float("nan")

        rows.append({
            "ts": stamp, "open": num(1), "high": num(2), "low": num(3), "close": close,
            "volume": 0.0 if pd.isna(num(5)) else num(5), "oi": 0.0 if pd.isna(num(6)) else num(6),
        })
    if not rows:
        return _empty()
    frame = pd.DataFrame(rows, columns=COLUMNS)
    return frame.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


def _json(resp: Any) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {}


def _find(data: Any, key: str) -> Any:
    """A key at the top level or inside the `payload` envelope."""
    if not isinstance(data, dict):
        return None
    if key in data:
        return data[key]
    payload = data.get("payload")
    return payload.get(key) if isinstance(payload, dict) else None


def _error_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "").strip()
    return str(data.get("message") or error or "").strip()


# ---------------------------------------------------------------- shared


_shared: GrowwBackup | None = None
_shared_key: Credentials | None = None
_shared_lock = threading.Lock()


def shared_backup() -> GrowwBackup | None:
    """The engine's one backup client, or None when none is configured.

    One per process, so a token minted for one user's backtest serves the next
    rather than signing in again. Rebuilt if the credentials change -- the
    supervisor reloads .env.engine.local on every restart.
    """
    global _shared, _shared_key
    creds = Credentials.from_env()
    if not creds.usable:
        return None
    with _shared_lock:
        if _shared is None or _shared_key != creds:
            try:
                rate = float(os.environ.get(ENV_RATE, "") or 4.0)
            except ValueError:
                rate = 4.0
            _shared, _shared_key = GrowwBackup(creds, rate_per_second=rate), creds
        return _shared
