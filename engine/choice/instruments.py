"""Scrip-master loading and option-contract resolution.

``kkunal``'s ``ScripMaster`` cannot resolve an option contract:

* ``get_token()`` is an exact, case-sensitive match on ``Symbol``/``SecDesc``,
  so you must already know the contract string to look it up.
* Its result dicts are projected down to seven keys — ``Token, Exchange,
  Segment, Symbol, SecDesc, Series, MarketLot`` — which **drops strike, expiry
  and CE/PE**.  Those only survive in the raw row behind ``get_details()``.
* ``search()`` linearly scans every row on every call.
* ``fetch()`` appends without clearing, so calling it twice (which happens if
  you ``login()`` after ``load_session()``) duplicates every row.
* The filename is built with a locale-dependent ``%b``, so on a non-English
  Windows locale every fetch 404s.

This module reads the raw CSV itself, discovers the real column names, and
builds an ``(underlying, expiry, strike, right) -> contract`` index.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

from engine.choice.errors import ChoiceInstrumentError
from engine.config import IST

log = logging.getLogger(__name__)

SCRIP_MASTER_URL = "https://scripmaster.choiceindia.com/scripmaster/SCRIP_MASTER_{date}.csv"

# Locale-independent month abbreviations. Python's %b follows the active
# locale, so on a non-English Windows install kkunal builds a URL the server
# has never heard of and 404s through all of its attempts.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

CALL, PUT = "CE", "PE"

# Candidate header names, most specific first. The Choice CSV schema is not
# documented anywhere in the package or the PDFs, so we detect rather than
# assume, and fail loudly if a required column is missing.
_COLUMN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "token": ("token", "instrumenttoken", "scripcode", "securityid"),
    "symbol": ("symbol", "tradingsymbol", "name"),
    "description": ("secdesc", "securitydesc", "description", "instrumentname", "longname"),
    "segment": ("segment", "segmentid", "exchangesegment"),
    "exchange": ("exchange", "exchangeid", "exch"),
    "series": ("series", "instrumenttype", "instrument"),
    "lot_size": ("marketlot", "lotsize", "lot", "boardlotquantity"),
    "expiry": ("expirydate", "expiry", "expdate", "expirydt"),
    "strike": ("strikeprice", "strike", "strikeprc"),
    "option_type": ("optiontype", "opttype", "option_type", "righttype", "callput"),
    "tick_size": ("ticksize", "tick"),
    "underlying": ("underlying", "underlyingsymbol", "assettoken", "basesymbol"),
}

_EXPIRY_FORMATS = (
    "%d-%b-%Y", "%d%b%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y",
    "%d-%b-%y", "%Y%m%d", "%b %d %Y", "%d %b %Y",
)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _fmt_master_date(day: dt.date) -> str:
    return f"{day.day:02d}{_MONTHS[day.month - 1]}{day.year}"


@dataclass(frozen=True)
class Contract:
    """One tradable instrument, with the option fields kkunal discards."""

    token: int
    segment_id: int
    symbol: str
    description: str
    lot_size: int
    exchange: str | None = None
    series: str | None = None
    expiry: dt.date | None = None
    strike: float | None = None
    option_type: str | None = None       # "CE" | "PE" | None
    underlying: str | None = None
    tick_size: float | None = None

    @property
    def is_option(self) -> bool:
        return self.option_type in (CALL, PUT) and self.strike is not None

    def __str__(self) -> str:
        if self.is_option:
            return f"{self.underlying or self.symbol} {self.expiry:%d-%b-%Y} {self.strike:g} {self.option_type}"
        return self.symbol


def _parse_expiry(raw: Any) -> dt.date | None:
    if raw in (None, "", "0"):
        return None
    text = str(raw).strip()
    for fmt in _EXPIRY_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # Some feeds ship an epoch. Accept both second- and millisecond-scale.
    try:
        num = float(text)
    except ValueError:
        return None
    if num <= 0:
        return None
    for scale in (1, 1000):
        try:
            candidate = dt.datetime.fromtimestamp(num / scale, tz=dt.timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            continue
        if 2000 <= candidate.year <= 2100:
            return candidate
    return None


def _parse_option_type(raw: Any, description: str = "") -> str | None:
    text = str(raw or "").strip().upper()
    if text in ("CE", "PE"):
        return text
    if text in ("C", "CALL"):
        return CALL
    if text in ("P", "PUT"):
        return PUT
    # Fall back to the contract description, which almost always ends in CE/PE.
    match = re.search(r"\b(CE|PE)\b", str(description).upper())
    return match.group(1) if match else None


def _parse_float(raw: Any) -> float | None:
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


class ScripMaster:
    """Downloads, indexes and queries the Choice scrip master."""

    def __init__(self) -> None:
        self.contracts: list[Contract] = []
        self.by_token: dict[int, Contract] = {}
        # (underlying, expiry, strike, right) -> Contract
        self._options: dict[tuple[str, dt.date, float, str], Contract] = {}
        self._by_underlying: dict[str, list[Contract]] = {}
        self.columns: dict[str, str] = {}
        self.loaded_for: dt.date | None = None

    # ---------------------------------------------------------------- load

    def fetch(self, on: dt.date | None = None, lookback_days: int = 7) -> bool:
        """Download the most recent available scrip master.

        Widened from kkunal's 3-day window to 7: a Friday holiday plus a
        weekend plus a Monday holiday already exceeds three days.
        """
        target = on or dt.datetime.now(tz=IST).date()
        last_error: Exception | None = None

        for back in range(lookback_days):
            day = target - dt.timedelta(days=back)
            url = SCRIP_MASTER_URL.format(date=_fmt_master_date(day))
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 404:
                    continue
                # Unlike kkunal we keep trying rather than breaking out: a
                # transient 502 on one date should not abort the whole search.
                log.warning("Scrip master %s -> HTTP %s", day, exc.code)
                continue
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                log.warning("Scrip master %s -> %s", day, exc)
                continue

            self.load_csv(payload)
            self.loaded_for = day
            log.info(
                "Scrip master %s loaded: %d contracts (%d options)",
                day, len(self.contracts), len(self._options),
            )
            return True

        raise ChoiceInstrumentError(
            f"Could not download a scrip master within {lookback_days} days of {target}. "
            f"Last error: {last_error}"
        )

    def load_csv(self, text: str) -> None:
        """Parse a scrip-master CSV, replacing any previously loaded data.

        Note the reset: kkunal appends, so a second fetch silently duplicates
        every row and doubles every subsequent search.
        """
        self.contracts.clear()
        self.by_token.clear()
        self._options.clear()
        self._by_underlying.clear()

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ChoiceInstrumentError("Scrip master CSV has no header row")

        self.columns = self._detect_columns(reader.fieldnames)
        for required in ("token", "symbol"):
            if required not in self.columns:
                raise ChoiceInstrumentError(
                    f"Scrip master is missing a {required!r} column. Headers: {reader.fieldnames[:20]}"
                )

        for row in reader:
            contract = self._row_to_contract(row)
            if contract is None:
                continue
            self.contracts.append(contract)
            self.by_token[contract.token] = contract
            if contract.is_option and contract.expiry is not None:
                key = (
                    (contract.underlying or contract.symbol).upper(),
                    contract.expiry,
                    float(contract.strike),  # type: ignore[arg-type]
                    contract.option_type,     # type: ignore[arg-type]
                )
                self._options[key] = contract
            self._by_underlying.setdefault((contract.underlying or contract.symbol).upper(), []).append(contract)

    def _detect_columns(self, fieldnames: Iterable[str]) -> dict[str, str]:
        available = {_norm(name): name for name in fieldnames}
        detected: dict[str, str] = {}
        for logical, candidates in _COLUMN_CANDIDATES.items():
            for candidate in candidates:
                if candidate in available:
                    detected[logical] = available[candidate]
                    break
        log.debug("Scrip master columns detected: %s", detected)
        return detected

    def _get(self, row: dict[str, Any], logical: str) -> Any:
        column = self.columns.get(logical)
        return row.get(column) if column else None

    def _row_to_contract(self, row: dict[str, Any]) -> Contract | None:
        try:
            token = int(float(str(self._get(row, "token")).strip()))
        except (TypeError, ValueError):
            return None

        description = str(self._get(row, "description") or "").strip()
        symbol = str(self._get(row, "symbol") or "").strip()
        if not symbol and not description:
            return None

        try:
            segment_id = int(float(str(self._get(row, "segment") or 0).strip() or 0))
        except (TypeError, ValueError):
            segment_id = 0

        # A blank MarketLot must not become 1: for an NFO leg that would send a
        # 1-share order instead of one lot. Keep 0 so callers can detect it.
        try:
            lot_size = int(float(str(self._get(row, "lot_size") or 0).strip() or 0))
        except (TypeError, ValueError):
            lot_size = 0

        option_type = _parse_option_type(self._get(row, "option_type"), description)
        strike = _parse_float(self._get(row, "strike"))
        expiry = _parse_expiry(self._get(row, "expiry"))
        underlying = str(self._get(row, "underlying") or "").strip() or None
        if underlying is None and option_type and symbol:
            # NFO symbols are usually the bare underlying (e.g. "NIFTY").
            underlying = re.split(r"\d", symbol, maxsplit=1)[0].strip() or symbol

        return Contract(
            token=token,
            segment_id=segment_id,
            symbol=symbol or description,
            description=description or symbol,
            lot_size=lot_size,
            exchange=str(self._get(row, "exchange") or "").strip() or None,
            series=str(self._get(row, "series") or "").strip() or None,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            underlying=underlying,
            tick_size=_parse_float(self._get(row, "tick_size")),
        )

    # --------------------------------------------------------------- query

    def option(self, underlying: str, expiry: dt.date, strike: float, right: str) -> Contract:
        """Resolve exactly one option contract, or raise with context."""
        right = _parse_option_type(right) or right.upper()
        key = (underlying.upper(), expiry, float(strike), right)
        found = self._options.get(key)
        if found is not None:
            return found
        available = sorted({s for (u, e, s, r) in self._options if u == underlying.upper() and e == expiry and r == right})
        raise ChoiceInstrumentError(
            f"No {underlying.upper()} {right} at strike {strike:g} expiring {expiry:%d-%b-%Y}. "
            + (
                f"Nearest available strikes: {available[:3]} ... {available[-3:]}"
                if available
                else f"No {right} contracts at all for that expiry — check the expiry date."
            )
        )

    def find_option(self, underlying: str, expiry: dt.date, strike: float, right: str) -> Contract | None:
        try:
            return self.option(underlying, expiry, strike, right)
        except ChoiceInstrumentError:
            return None

    def expiries(self, underlying: str, *, after: dt.date | None = None) -> list[dt.date]:
        """All known expiries for an underlying, ascending."""
        key = underlying.upper()
        found = {e for (u, e, _s, _r) in self._options if u == key}
        if after is not None:
            found = {e for e in found if e >= after}
        return sorted(found)

    def nearest_expiry(self, underlying: str, on: dt.date, *, min_days: int = 0) -> dt.date:
        """The nearest expiry at least ``min_days`` away — the weekly, normally."""
        candidates = self.expiries(underlying, after=on + dt.timedelta(days=min_days))
        if not candidates:
            raise ChoiceInstrumentError(
                f"No expiry for {underlying.upper()} on/after {on + dt.timedelta(days=min_days)}. "
                "The scrip master may be stale, or the underlying name may be wrong."
            )
        return candidates[0]

    def strikes(self, underlying: str, expiry: dt.date, right: str = CALL) -> list[float]:
        key = underlying.upper()
        right = _parse_option_type(right) or right.upper()
        return sorted({s for (u, e, s, r) in self._options if u == key and e == expiry and r == right})

    def strike_step(self, underlying: str, expiry: dt.date) -> float:
        """Smallest gap between adjacent listed strikes (50 for NIFTY)."""
        listed = self.strikes(underlying, expiry)
        if len(listed) < 2:
            return 50.0
        gaps = [b - a for a, b in zip(listed, listed[1:]) if b > a]
        return min(gaps) if gaps else 50.0

    def search(self, text: str, *, limit: int = 50) -> list[Contract]:
        needle = text.upper()
        out = [c for c in self.contracts if needle in c.symbol.upper() or needle in c.description.upper()]
        return out[:limit]

    def infer_nfo_segment(self, underlying: str = "NIFTY") -> int:
        """Determine the NSE F&O segment id from the data, not the docs.

        kkunal's README contradicts itself — a table says segment 2 is NSE F&O
        while the adjacent example uses 13 — so we read the value off a real
        option row instead of trusting either.
        """
        key = underlying.upper()
        counts: dict[int, int] = {}
        for contract in self._by_underlying.get(key, ()):
            if contract.is_option and contract.segment_id:
                counts[contract.segment_id] = counts.get(contract.segment_id, 0) + 1
        if not counts:
            raise ChoiceInstrumentError(f"No {key} option rows found; cannot infer the NFO segment id.")
        return max(counts.items(), key=lambda kv: kv[1])[0]

    def lot_size_for(self, underlying: str) -> int:
        """Lot size read from the master rather than hardcoded."""
        for contract in self._by_underlying.get(underlying.upper(), ()):
            if contract.is_option and contract.lot_size > 0:
                return contract.lot_size
        raise ChoiceInstrumentError(f"No lot size found for {underlying.upper()}")
