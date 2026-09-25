"""The exchange's daily record of every NIFTY option contract.

NSE publishes one F&O "bhavcopy" per trading day: the open, high, low, close,
last trade, settlement price, traded volume and open interest of every listed
contract. It is the only record there is of what an *expired* option traded
at -- Choice serves no history once a contract has expired -- so for most of a
backtest's range it is the evidence the premium model is anchored to, and
measured against.

What the day's figures mean matters more than it looks:

* ``close`` is the exchange's closing price: for an option that traded late in
  the day, the volume-weighted average of the last half hour, not the last
  trade. It pairs with ``underlying``, NIFTY's official close, which is the
  same kind of average over the same half hour.
* ``last`` is the last trade itself, and ``open``/``high``/``low`` are real
  traded prices.
* A contract that did not trade that day still gets a row: its ``close`` is
  the previous day's carried forward and its ``settle`` is a theoretical
  value. Neither is a price anyone paid, so ``volume`` must be checked before
  either is believed.

Two file formats, one shape out:

* UDiFF (``BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip``), current since
  July 2024 and published for earlier years as well;
* the legacy ``foDDMONYYYYbhav.csv.zip``, asked for only when a pre-July-2024
  day has no UDiFF file.

Only NIFTY's index options and futures are kept, one small file per day,
cached under ``engine/state/exchange``. The archive is public, but it is
somebody else's server and a published day never changes, so each day is
downloaded at most once. Nothing here is ever uploaded anywhere.

User-facing text names no source: to a user this is "the exchange's daily
closing prices".
"""

from __future__ import annotations

import bisect
import datetime as dt
import io
import logging
import math
import os
import pathlib
import threading
import time
import zipfile
from dataclasses import dataclass
from typing import Callable, Iterable

import pandas as pd
import requests

from engine.config import IST

log = logging.getLogger(__name__)

ARCHIVE = "https://nsearchives.nseindia.com"
UDIFF_PATH = "/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
LEGACY_PATH = "/content/historical/DERIVATIVES/{year}/{mon}/fo{dd}{mon}{year}bhav.csv.zip"

#: The legacy file stopped with the switch to UDiFF. A day after this with no
#: UDiFF file is a day the market was shut, and asking for the legacy file
#: would only be a second wasted request.
LEGACY_UNTIL = dt.date(2024, 7, 5)

#: The archive refuses a bare HTTP client and serves anything that looks like
#: a browser.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/zip,application/octet-stream,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

#: A missing file is believed to mean "no session" only once the day is this
#: old. Today's file is published in the evening, and a late one must not be
#: remembered as a holiday.
CLOSED_AFTER_DAYS = 4

DEFAULT_CACHE = pathlib.Path(__file__).resolve().parents[1] / "state" / "exchange" / "fo"

# Fixed, not strftime("%b"), which follows the Windows locale and has already
# broken a URL in this codebase once.
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

COLUMNS = [
    "kind", "expiry", "strike", "right",
    "open", "high", "low", "close", "last", "settle",
    "volume", "trades", "oi", "underlying",
]


#: Set to "off" to run backtests on the India VIX model alone.
ENV_SWITCH = "EXCHANGE_CLOSES"


class ExchangeDataUnavailable(Exception):
    """The archive could not be reached, or refused. The message is shown to
    users as it stands, so it never names the source."""


# ------------------------------------------------------------------ parsing


def udiff_url(day: dt.date) -> str:
    return ARCHIVE + UDIFF_PATH.format(ymd=f"{day:%Y%m%d}")


def legacy_url(day: dt.date) -> str:
    mon = _MONTHS[day.month - 1]
    return ARCHIVE + LEGACY_PATH.format(year=day.year, mon=mon, dd=f"{day.day:02d}")


def _numbers(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _legacy_date(text: str) -> dt.date | None:
    """'25-Jan-2024' -> date, without the locale-dependent %b."""
    try:
        dd, mon, yyyy = str(text).strip().split("-")
        return dt.date(int(yyyy), _MONTHS.index(mon.upper()[:3]) + 1, int(dd))
    except (ValueError, IndexError):
        return None


def parse_udiff(raw: pd.DataFrame, symbol: str = "NIFTY") -> pd.DataFrame:
    """The UDiFF file's rows for one index's options and futures."""
    rows = raw[
        (raw["TckrSymb"].astype(str).str.strip() == symbol)
        & raw["FinInstrmTp"].astype(str).str.strip().isin(["IDO", "IDF"])
    ]
    # The actual expiry where the file gives one: it differs from XpryDt only
    # when the exchange moved an expiry, and then it is the one that counts.
    expiry_text = rows["XpryDt"]
    if "FininstrmActlXpryDt" in rows.columns:
        expiry_text = rows["FininstrmActlXpryDt"].where(rows["FininstrmActlXpryDt"].notna(), expiry_text)
    out = pd.DataFrame({
        "kind": rows["FinInstrmTp"].astype(str).str.strip().map({"IDO": "OPT", "IDF": "FUT"}),
        "expiry": pd.to_datetime(expiry_text, errors="coerce").dt.date,
        "strike": _numbers(rows["StrkPric"]).fillna(0.0),
        "right": rows["OptnTp"].fillna("").astype(str).str.strip(),
        "open": _numbers(rows["OpnPric"]),
        "high": _numbers(rows["HghPric"]),
        "low": _numbers(rows["LwPric"]),
        "close": _numbers(rows["ClsPric"]),
        "last": _numbers(rows["LastPric"]) if "LastPric" in rows.columns else math.nan,
        "settle": _numbers(rows["SttlmPric"]),
        "volume": _numbers(rows["TtlTradgVol"]).fillna(0).astype("int64"),
        "trades": _numbers(rows["TtlNbOfTxsExctd"]) if "TtlNbOfTxsExctd" in rows.columns else math.nan,
        "oi": _numbers(rows["OpnIntrst"]).fillna(0).astype("int64"),
        "underlying": _numbers(rows["UndrlygPric"]) if "UndrlygPric" in rows.columns else math.nan,
    })
    out.loc[out["kind"] == "FUT", "right"] = ""
    return _tidy(out)


def parse_legacy(raw: pd.DataFrame, symbol: str = "NIFTY") -> pd.DataFrame:
    """The legacy file's rows for one index's options and futures.

    It has no last trade, trade count or underlying price; those stay empty.
    """
    raw = raw.rename(columns=lambda c: str(c).strip())
    instrument = raw["INSTRUMENT"].astype(str).str.strip()
    rows = raw[(raw["SYMBOL"].astype(str).str.strip() == symbol) & instrument.isin(["OPTIDX", "FUTIDX"])]
    kind = rows["INSTRUMENT"].astype(str).str.strip().map({"OPTIDX": "OPT", "FUTIDX": "FUT"})
    right = rows["OPTION_TYP"].fillna("").astype(str).str.strip()
    out = pd.DataFrame({
        "kind": kind,
        "expiry": rows["EXPIRY_DT"].map(_legacy_date),
        "strike": _numbers(rows["STRIKE_PR"]).fillna(0.0),
        "right": right.where(kind == "OPT", ""),
        "open": _numbers(rows["OPEN"]),
        "high": _numbers(rows["HIGH"]),
        "low": _numbers(rows["LOW"]),
        "close": _numbers(rows["CLOSE"]),
        "last": math.nan,
        "settle": _numbers(rows["SETTLE_PR"]),
        "volume": _numbers(rows["CONTRACTS"]).fillna(0).astype("int64"),
        "trades": math.nan,
        "oi": _numbers(rows["OPEN_INT"]).fillna(0).astype("int64"),
        "underlying": math.nan,
    })
    return _tidy(out)


def _tidy(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame[frame["expiry"].notna() & frame["kind"].isin(["OPT", "FUT"])]
    frame = frame[(frame["kind"] == "FUT") | frame["right"].isin(["CE", "PE"])]
    return frame[COLUMNS].sort_values(["kind", "expiry", "right", "strike"]).reset_index(drop=True)


def _read_zip(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError("the archive held no CSV")
        with archive.open(names[0]) as handle:
            return pd.read_csv(handle, low_memory=False)


# ------------------------------------------------------------------ archive


class BhavcopyArchive:
    """Downloads, parses and caches the daily files.

    ``http`` is anything with ``requests.Session``'s ``get``, so tests never
    touch the network. ``rate`` is requests per second: a day's file is about
    1.7 MB, and a first backtest over a year asks for some 250 of them.
    """

    def __init__(
        self,
        cache_dir: pathlib.Path | str | None = None,
        *,
        http: requests.Session | None = None,
        rate: float = 1.0,
        timeout: tuple[float, float] = (5.0, 60.0),
        symbol: str = "NIFTY",
        today: Callable[[], dt.date] | None = None,
        attempts: int = 3,
    ) -> None:
        self.cache_dir = pathlib.Path(cache_dir) if cache_dir else DEFAULT_CACHE
        self.http = http or requests.Session()
        self.rate = max(0.05, float(rate))
        self.timeout = timeout
        self.symbol = symbol
        self._today = today or (lambda: dt.datetime.now(tz=IST).date())
        self.attempts = max(1, attempts)
        self._lock = threading.Lock()
        self._last_request = 0.0
        self.downloads = 0
        self.cache_hits = 0
        self.requests_made = 0

    # -- cache -----------------------------------------------------------

    def _path(self, day: dt.date) -> pathlib.Path:
        return self.cache_dir / f"{day:%Y}" / f"{day:%Y%m%d}.csv.gz"

    def _closed_path(self, day: dt.date) -> pathlib.Path:
        return self.cache_dir / f"{day:%Y}" / f"{day:%Y%m%d}.closed"

    def _read(self, path: pathlib.Path) -> pd.DataFrame:
        frame = pd.read_csv(path, compression="gzip", keep_default_na=True)
        frame["expiry"] = pd.to_datetime(frame["expiry"]).dt.date
        frame["right"] = frame["right"].fillna("").astype(str)
        return frame[COLUMNS]

    def _write(self, path: pathlib.Path, frame: pd.DataFrame) -> None:
        # Written aside and moved into place, so a second backtest reading the
        # same day never sees half a file.
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        frame.to_csv(temp, index=False, compression="gzip")
        os.replace(temp, path)

    def is_cached(self, day: dt.date) -> bool:
        return self._path(day).exists() or self._closed_path(day).exists()

    # -- network ---------------------------------------------------------

    def _throttle(self) -> None:
        wait = self._last_request + 1.0 / self.rate - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _get(self, url: str) -> bytes | None:
        """The file at `url`, or None when the archive has none."""
        last_error = ""
        for attempt in range(self.attempts):
            self._throttle()
            try:
                response = self.http.get(url, headers=HEADERS, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = type(exc).__name__
                time.sleep(min(8.0, 1.0 * 2 ** attempt))
                continue
            self.requests_made += 1
            status = response.status_code
            if status == 404:
                return None
            if status == 200:
                content = response.content
                if content[:2] != b"PK":
                    # A page instead of a file: the archive's way of saying no
                    # without saying 404.
                    return None
                return content
            if status in (401, 403):
                raise ExchangeDataUnavailable(
                    f"the exchange's archive refused the request (HTTP {status})"
                )
            if status == 429 or status >= 500:
                last_error = f"HTTP {status}"
                time.sleep(min(15.0, 2.0 * 2 ** attempt))
                continue
            raise ExchangeDataUnavailable(f"the exchange's archive answered HTTP {status}")
        raise ExchangeDataUnavailable(
            f"the exchange's archive could not be reached ({last_error or 'no answer'})"
        )

    def _download(self, day: dt.date) -> pd.DataFrame | None:
        sources = [(udiff_url(day), parse_udiff)]
        if day <= LEGACY_UNTIL:
            sources.append((legacy_url(day), parse_legacy))
        for url, parser in sources:
            content = self._get(url)
            if content is None:
                continue
            try:
                frame = parser(_read_zip(content), self.symbol)
            except (ValueError, KeyError, zipfile.BadZipFile) as exc:
                log.warning("Exchange file for %s could not be read from %s: %s", day, url, exc)
                continue
            self.downloads += 1
            return frame
        return None

    # -- public ----------------------------------------------------------

    def day(self, day: dt.date) -> pd.DataFrame | None:
        """NIFTY's options and futures on `day`, or None when nothing traded.

        Raises ExchangeDataUnavailable when the archive cannot be asked; a day
        with no file is an answer, not an error.
        """
        path = self._path(day)
        with self._lock:
            if path.exists():
                self.cache_hits += 1
                return self._read(path)
            if self._closed_path(day).exists():
                return None
            today = self._today()
            if day > today:
                return None
            frame = self._download(day)
            if frame is None:
                if (today - day).days >= CLOSED_AFTER_DAYS:
                    marker = self._closed_path(day)
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.touch()
                return None
            self._write(path, frame)
            return frame

    def load(
        self,
        days: Iterable[dt.date],
        progress: Callable[[int, int, dt.date], None] | None = None,
    ) -> tuple[dict[dt.date, pd.DataFrame], str | None]:
        """Every day in `days` the exchange traded, and a note on what failed.

        A refusal stops the downloading -- asking again for the next day would
        only repeat it -- but whatever is already cached is still used.
        """
        wanted = sorted(set(days))
        frames: dict[dt.date, pd.DataFrame] = {}
        note: str | None = None
        for i, day in enumerate(wanted, 1):
            if progress is not None:
                progress(i, len(wanted), day)
            if note is not None and not self.is_cached(day):
                continue
            try:
                frame = self.day(day)
            except ExchangeDataUnavailable as exc:
                note = str(exc)
                log.warning("Exchange closing prices unavailable from %s: %s", day, exc)
                continue
            if frame is not None and not frame.empty:
                frames[day] = frame
        return frames, note


_shared: BhavcopyArchive | None = None
_shared_lock = threading.Lock()


def shared_archive() -> BhavcopyArchive | None:
    """The engine's one archive -- one cache, one request rate -- or None when
    switched off with EXCHANGE_CLOSES=off."""
    global _shared
    if (os.environ.get(ENV_SWITCH) or "").strip().lower() in ("off", "0", "false", "no"):
        return None
    with _shared_lock:
        if _shared is None:
            _shared = BhavcopyArchive()
        return _shared


# ------------------------------------------------------------ in-memory view


@dataclass(frozen=True)
class ContractDay:
    """One contract's figures for one day."""

    open: float
    high: float
    low: float
    close: float
    last: float | None
    settle: float
    volume: int
    trades: int | None
    oi: int

    @property
    def traded(self) -> bool:
        """Whether anyone paid anything for it that day. Without a trade the
        close is yesterday's, carried forward, and not evidence of today."""
        return self.volume > 0 and self.close > 0 and self.low > 0


def _optional(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class ChainHistory:
    """The loaded days, indexed the way the premium model asks.

    Only the expiries a backtest can trade are kept: a monthly run touches a
    handful of contracts per day out of the two thousand in the file.
    """

    def __init__(
        self,
        frames: dict[dt.date, pd.DataFrame],
        expiries: Iterable[dt.date] | None = None,
        underlying: dict[dt.date, float] | None = None,
    ) -> None:
        wanted = set(expiries) if expiries is not None else None
        self.days: list[dt.date] = sorted(frames)
        self._contracts: dict[tuple[dt.date, dt.date, float, str], ContractDay] = {}
        self._chains: dict[tuple[dt.date, dt.date], list[tuple[float, str, ContractDay]]] = {}
        self._futures: dict[tuple[dt.date, dt.date], float] = {}
        self._underlying: dict[dt.date, float] = {}
        for day, frame in frames.items():
            spot = None
            if "underlying" in frame.columns:
                values = frame["underlying"].dropna()
                values = values[values > 0]
                if not values.empty:
                    spot = float(values.iloc[0])
            if spot is None and underlying:
                spot = underlying.get(day)
            if spot is not None:
                self._underlying[day] = spot
            for row in frame.itertuples(index=False):
                if wanted is not None and row.expiry not in wanted:
                    continue
                if row.kind == "FUT":
                    close = _optional(row.close)
                    if close and row.volume > 0:
                        self._futures[(day, row.expiry)] = close
                    continue
                data = ContractDay(
                    open=_optional(row.open) or 0.0,
                    high=_optional(row.high) or 0.0,
                    low=_optional(row.low) or 0.0,
                    close=_optional(row.close) or 0.0,
                    last=_optional(row.last),
                    settle=_optional(row.settle) or 0.0,
                    volume=int(row.volume),
                    trades=None if _optional(row.trades) is None else int(row.trades),
                    oi=int(row.oi),
                )
                key = (day, row.expiry, float(row.strike), str(row.right))
                self._contracts[key] = data
                self._chains.setdefault((day, row.expiry), []).append(
                    (float(row.strike), str(row.right), data)
                )

    def __bool__(self) -> bool:
        return bool(self.days)

    def previous_day(self, day: dt.date) -> dt.date | None:
        """The latest loaded session strictly before `day`."""
        i = bisect.bisect_left(self.days, day) - 1
        return self.days[i] if i >= 0 else None

    def has_day(self, day: dt.date) -> bool:
        i = bisect.bisect_left(self.days, day)
        return i < len(self.days) and self.days[i] == day

    def contract(self, day: dt.date, expiry: dt.date, strike: float, right: str) -> ContractDay | None:
        return self._contracts.get((day, expiry, float(strike), right))

    def chain(self, day: dt.date, expiry: dt.date) -> list[tuple[float, str, ContractDay]]:
        return self._chains.get((day, expiry), [])

    def future(self, day: dt.date, expiry: dt.date) -> float | None:
        return self._futures.get((day, expiry))

    def underlying(self, day: dt.date) -> float | None:
        return self._underlying.get(day)

    @property
    def contracts(self) -> int:
        return len(self._contracts)
