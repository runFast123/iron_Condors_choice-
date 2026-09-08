"""Historical OHLCV from Choice ``api/OpenGraph/ChartData`` — hardened.

This module exists because the upstream ``kkunal`` implementation is unusable
for a backtest.  Its ``historical.py`` is byte-identical between 1.2.0 and the
current 1.3.0, so upgrading does not help.  The specific defects fixed here:

1. ``_parse_date`` swallowed every exception and returned ``0``, i.e. the 1980
   epoch — so one typo silently requested 45 years of data.  We raise
   :class:`ChoiceDateError` instead and never guess.
2. A non-``Success`` status returned an **empty DataFrame** with the broker's
   error message discarded.  A bad token, an expired session, a throttle and a
   genuine market holiday were indistinguishable.  We raise
   :class:`ChoiceHistoryError` on failure and :class:`ChoiceNoDataError` on a
   genuinely empty window, and carry the broker's own message in both.
3. No chunking or pagination, so any long range silently came back empty.  We
   chunk per resolution and bisect on failure.
4. ``int(parts[5])`` raised ``ValueError`` on decimal volume (``"1234.0"``),
   killing the whole batch.  We parse per row and account for bad rows.
5. Naive local-time epoch arithmetic with no timezone, so intraday bars could
   be silently shifted by 5.5h.  We are explicit about IST and *calibrate* the
   epoch against the server rather than assuming.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import pandas as pd

from engine.choice.errors import (
    ChoiceDateError,
    ChoiceHistoryError,
    ChoiceNoDataError,
)
from engine.choice.session import ChoiceSession
from engine.config import IST

log = logging.getLogger(__name__)

CHART_ENDPOINT = "api/OpenGraph/ChartData"

# Reference epoch for the Choice ChartData API: seconds elapsed since this
# instant. Kept naive on purpose — whether the server means IST-naive or
# UTC-naive is resolved empirically by ``calibrate_epoch`` below.
EPOCH_1980 = dt.datetime(1980, 1, 1)

IST_OFFSET_SECONDS = 19_800  # +05:30

COLUMNS = ["ts", "open", "high", "low", "close", "volume", "oi"]

# Maximum span we will ask for in a single request, per resolution.  Broker
# chart endpoints universally cap intraday history; kkunal documents no limits
# and does no chunking, which is the most likely reason long-range requests
# come back empty.  Conservative defaults, auto-bisected on failure.
MAX_SPAN_DAYS: dict[str, int] = {
    "1": 7,
    "2": 10,
    "3": 15,
    "5": 30,
    "10": 60,
    "15": 90,
    "30": 120,
    "60": 180,
    "D": 365,
    "W": 1825,
    "M": 3650,
}

_RESOLUTION_ALIASES = {
    "1m": "1", "1min": "1", "minute": "1",
    "3m": "3", "5m": "5", "10m": "10", "15m": "15", "30m": "30",
    "1h": "60", "60m": "60", "hour": "60",
    "d": "D", "1d": "D", "day": "D", "daily": "D",
    "w": "W", "1w": "W", "week": "W", "weekly": "W",
    "mo": "M", "1mo": "M", "month": "M", "monthly": "M",
}


def normalise_resolution(resolution: str) -> str:
    """Map friendly names onto the codes the Choice API expects."""
    raw = str(resolution).strip()
    key = raw.lower()
    if key in _RESOLUTION_ALIASES:
        return _RESOLUTION_ALIASES[key]
    if raw.isdigit() or raw.upper() in {"D", "W", "M"}:
        return raw.upper() if not raw.isdigit() else raw
    raise ChoiceDateError(
        f"Unsupported resolution {resolution!r}. Use one of: "
        + ", ".join(sorted(set(MAX_SPAN_DAYS)))
    )


def max_span_days(resolution: str) -> int:
    return MAX_SPAN_DAYS.get(normalise_resolution(resolution), 30)


# --------------------------------------------------------------------- dates

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d-%b-%Y",
)


def to_ist(value: Any) -> dt.datetime:
    """Coerce anything date-like into a tz-aware IST datetime, or raise.

    Unlike the upstream ``_parse_date`` this never returns a sentinel: a value
    we cannot understand is a bug in the caller, and silently substituting
    1980-01-01 turns that bug into a corrupt backtest.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        raise ChoiceDateError("Date is required, got None")

    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()

    if isinstance(value, dt.datetime):
        return value.astimezone(IST) if value.tzinfo else value.replace(tzinfo=IST)

    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=IST)

    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ChoiceDateError("Date string is empty")
        for fmt in _DATE_FORMATS:
            try:
                return dt.datetime.strptime(text, fmt).replace(tzinfo=IST)
            except ValueError:
                continue
        try:  # last resort, still strict about the outcome
            parsed = pd.to_datetime(text, dayfirst=False)
        except Exception as exc:  # noqa: BLE001 - pandas raises many types
            raise ChoiceDateError(f"Unparseable date {value!r}") from exc
        if pd.isna(parsed):
            raise ChoiceDateError(f"Unparseable date {value!r}")
        py = parsed.to_pydatetime()
        return py.astimezone(IST) if py.tzinfo else py.replace(tzinfo=IST)

    raise ChoiceDateError(f"Unsupported date type {type(value).__name__}: {value!r}")


def to_choice_epoch(value: Any, offset_seconds: int = 0) -> int:
    """IST datetime -> seconds since 1980-01-01 in the server's frame."""
    moment = to_ist(value)
    naive_ist = moment.replace(tzinfo=None)
    return int((naive_ist - EPOCH_1980).total_seconds()) + offset_seconds


def from_choice_epoch(seconds: float, offset_seconds: int = 0) -> dt.datetime:
    """Server epoch seconds -> tz-aware IST datetime."""
    naive = EPOCH_1980 + dt.timedelta(seconds=float(seconds) - offset_seconds)
    return naive.replace(tzinfo=IST)


# ------------------------------------------------------------------- results


@dataclass
class FetchReport:
    """What actually happened during a fetch, for the Data Health page.

    Recorded whether the fetch succeeded or not, so a failure is visible in
    the UI with the broker's own words instead of vanishing into an empty
    DataFrame.
    """

    token: int
    segment_id: int
    resolution: str
    start: dt.datetime
    end: dt.datetime
    status: str = "ok"          # ok | no_data | error
    bars: int = 0
    requests_made: int = 0
    bad_rows: int = 0
    error_message: str | None = None
    windows_failed: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# -------------------------------------------------------------------- client


class HistoryClient:
    """Fetches OHLCV candles from Choice, with chunking and honest errors."""

    def __init__(self, session: ChoiceSession, *, epoch_offset: int = 0) -> None:
        self.session = session
        # Corrective offset between our epoch arithmetic and the server's,
        # discovered by ``calibrate_epoch``. 0 means "server speaks IST-naive".
        self.epoch_offset = epoch_offset
        self._calibrated = False

    # -- single request ----------------------------------------------------

    def _request_window(
        self, segment_id: int, token: int, start: dt.datetime, end: dt.datetime, resolution: str
    ) -> tuple[list[str], float]:
        payload = {
            "SegmentId": int(segment_id),
            "Token": int(token),
            "FromDate": to_choice_epoch(start, self.epoch_offset),
            "ToDate": to_choice_epoch(end, self.epoch_offset),
            "Interval": resolution,
        }
        resp = self.session.request("POST", CHART_ENDPOINT, payload)

        status = str(resp.get("Status", "")).strip()
        if status.lower() != "success":
            message = resp.get("Message") or resp.get("Error") or resp.get("Reason") or str(resp)[:300]
            raise ChoiceHistoryError(
                f"ChartData failed for token {token} seg {segment_id} "
                f"[{start:%Y-%m-%d} to {end:%Y-%m-%d} @ {resolution}]: {message}",
                status=status or None,
                payload=payload,
            )

        # Guard the AttributeError kkunal would raise when Response is null.
        body = resp.get("Response") or {}
        if not isinstance(body, dict):
            raise ChoiceHistoryError(
                f"ChartData returned an unexpected Response type ({type(body).__name__}) for token {token}",
                payload=payload,
            )

        history = body.get("lstChartHistory") or []
        divisor = body.get("PriceDivisor", 1)
        try:
            divisor = float(divisor)
        except (TypeError, ValueError):
            divisor = 1.0
        if divisor <= 0:
            divisor = 1.0
        return list(history), divisor

    # -- parsing -----------------------------------------------------------

    def _parse_rows(self, rows: Iterable[str], divisor: float) -> tuple[list[list[Any]], int]:
        """Parse 'ts,o,h,l,c,v,oi' strings, tolerating malformed rows.

        Volume and OI are read via ``int(float(x))`` because the feed sends
        them as ``"1234.0"`` often enough that the upstream ``int(parts[5])``
        blows up and takes the entire batch with it.
        """
        out: list[list[Any]] = []
        bad = 0
        for row in rows:
            parts = str(row).split(",")
            if len(parts) < 5:
                bad += 1
                continue
            try:
                ts = from_choice_epoch(float(parts[0]), self.epoch_offset)
                o, h, low, c = (float(parts[i]) / divisor for i in range(1, 5))
                volume = int(float(parts[5])) if len(parts) > 5 and parts[5] not in ("", None) else 0
                oi = int(float(parts[6])) if len(parts) > 6 and parts[6] not in ("", None) else 0
            except (TypeError, ValueError):
                bad += 1
                continue
            out.append([ts, o, h, low, c, volume, oi])
        return out, bad

    # -- chunked public API -------------------------------------------------

    def fetch(
        self,
        segment_id: int,
        token: int,
        start: Any,
        end: Any,
        resolution: str = "5",
        *,
        allow_partial: bool = True,
    ) -> tuple[pd.DataFrame, FetchReport]:
        """Fetch candles across an arbitrary range.

        Returns the frame *and* a :class:`FetchReport`.  The report is the
        point: callers persist it so the Data Health page can show exactly
        which windows failed and why, instead of inferring it from row counts.

        ``allow_partial=True`` keeps whatever windows succeeded when others
        fail (right for a wide backfill); ``False`` re-raises (right when a
        single leg's prices must be trustworthy or absent).
        """
        resolution = normalise_resolution(resolution)
        start_ist, end_ist = to_ist(start), to_ist(end)
        if start_ist > end_ist:
            raise ChoiceDateError(f"start ({start_ist}) is after end ({end_ist})")

        report = FetchReport(
            token=int(token), segment_id=int(segment_id), resolution=resolution,
            start=start_ist, end=end_ist,
        )
        frames: list[list[Any]] = []

        for win_start, win_end in self._windows(start_ist, end_ist, resolution):
            rows, bad, err = self._fetch_with_bisect(segment_id, token, win_start, win_end, resolution, report)
            report.bad_rows += bad
            if err is not None:
                report.windows_failed.append((win_start.isoformat(), win_end.isoformat(), str(err)))
                if not allow_partial:
                    report.status = "error"
                    report.error_message = str(err)
                    raise err
                continue
            frames.extend(rows)

        df = self._to_frame(frames)
        report.bars = len(df)

        if report.windows_failed and not frames:
            report.status = "error"
            report.error_message = report.windows_failed[0][2]
        elif report.windows_failed:
            report.status = "ok"
            report.error_message = f"{len(report.windows_failed)} of the requested windows failed"
        elif not frames:
            # Genuinely empty: a holiday, or a contract that did not exist yet.
            report.status = "no_data"
            report.error_message = "Choice returned no bars for this range"

        return df, report

    def fetch_or_raise(
        self, segment_id: int, token: int, start: Any, end: Any, resolution: str = "5"
    ) -> pd.DataFrame:
        """Strict variant: any failure or empty result raises."""
        df, report = self.fetch(segment_id, token, start, end, resolution, allow_partial=False)
        if report.status == "no_data":
            raise ChoiceNoDataError(
                f"No bars for token {token} seg {segment_id} "
                f"[{report.start:%Y-%m-%d} to {report.end:%Y-%m-%d} @ {resolution}]"
            )
        return df

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _windows(start: dt.datetime, end: dt.datetime, resolution: str) -> list[tuple[dt.datetime, dt.datetime]]:
        span = dt.timedelta(days=max_span_days(resolution))
        windows: list[tuple[dt.datetime, dt.datetime]] = []
        cursor = start
        while cursor <= end:
            stop = min(cursor + span, end)
            windows.append((cursor, stop))
            if stop >= end:
                break
            cursor = stop + dt.timedelta(seconds=1)
        return windows or [(start, end)]

    def _fetch_with_bisect(
        self,
        segment_id: int,
        token: int,
        start: dt.datetime,
        end: dt.datetime,
        resolution: str,
        report: FetchReport,
        depth: int = 0,
    ) -> tuple[list[list[Any]], int, Exception | None]:
        """Fetch one window, halving it on failure before giving up.

        A window that fails because it is simply too wide for the broker looks
        identical to one that fails for a real reason, so we probe by halving
        (bounded depth) rather than assuming either.
        """
        try:
            report.requests_made += 1
            raw, divisor = self._request_window(segment_id, token, start, end, resolution)
        except ChoiceHistoryError as exc:
            if depth >= 3 or (end - start) <= dt.timedelta(days=1):
                return [], 0, exc
            mid = start + (end - start) / 2
            left_rows, left_bad, left_err = self._fetch_with_bisect(
                segment_id, token, start, mid, resolution, report, depth + 1
            )
            right_rows, right_bad, right_err = self._fetch_with_bisect(
                segment_id, token, mid + dt.timedelta(seconds=1), end, resolution, report, depth + 1
            )
            if left_err is not None and right_err is not None:
                return [], left_bad + right_bad, left_err
            return left_rows + right_rows, left_bad + right_bad, None

        rows, bad = self._parse_rows(raw, divisor)
        return rows, bad, None

    @staticmethod
    def _to_frame(rows: Sequence[Sequence[Any]]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=COLUMNS)
        df = pd.DataFrame(list(rows), columns=COLUMNS)
        df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
        return df

    # -- epoch calibration --------------------------------------------------

    def calibrate_epoch(self, segment_id: int, token: int, probe_days: int = 12) -> int:
        """Discover the server's epoch frame instead of assuming it.

        ``kkunal`` does naive local-time arithmetic against a naive 1980 epoch.
        Whether the server means IST-naive or UTC-naive is undocumented, and a
        5.5h error would silently misalign every intraday bar (and split
        sessions in any date-grouped calculation).  So: request a daily series,
        and pick the offset whose decoded timestamps land on IST session dates
        rather than the day before/after.
        """
        end = dt.datetime.now(tz=IST).replace(hour=15, minute=30, second=0, microsecond=0)
        start = end - dt.timedelta(days=probe_days)

        best_offset, best_score = self.epoch_offset, -1
        for candidate in (0, -IST_OFFSET_SECONDS, IST_OFFSET_SECONDS):
            previous = self.epoch_offset
            self.epoch_offset = candidate
            try:
                raw, divisor = self._request_window(segment_id, token, start, end, "D")
                rows, _ = self._parse_rows(raw, divisor)
            except Exception as exc:  # noqa: BLE001 - probing, any failure just scores 0
                log.debug("Epoch probe offset=%s failed: %s", candidate, exc)
                self.epoch_offset = previous
                continue
            # A correctly-aligned daily series sits on weekdays at 00:00-09:15 IST.
            score = sum(1 for r in rows if r[0].weekday() < 5)
            log.debug("Epoch probe offset=%s -> %d bars, score %d", candidate, len(rows), score)
            if score > best_score:
                best_offset, best_score = candidate, score
            self.epoch_offset = previous

        self.epoch_offset = best_offset
        self._calibrated = True
        log.info("ChartData epoch calibrated: offset=%ds (score %d)", best_offset, best_score)
        return best_offset
