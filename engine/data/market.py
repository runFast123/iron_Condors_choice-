"""Market data — Choice FinX only.

This is the single entry point for every price the engine consumes, historical
and live. There is deliberately no third-party fallback: Choice is the only
permitted source, so a gap in Choice data surfaces as an explicit error rather
than being quietly papered over with a different vendor's numbers.

Everything routes through :class:`~engine.choice.history.HistoryClient`, which
means every fetch inherits its chunking, retries, epoch calibration and typed
errors, and every failure lands in ``data_coverage`` with Choice's own message.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

import pandas as pd

from engine.choice.errors import ChoiceError, ChoiceInstrumentError, ChoiceNoDataError
from engine.config import IST
from engine.choice.history import FetchReport, HistoryClient
from engine.choice.instruments import Contract, ScripMaster
from engine.choice.session import ChoiceSession

log = logging.getLogger(__name__)

NIFTY = "NIFTY"
INDIA_VIX = "INDIAVIX"

TOUCHLINE_ENDPOINT = "api/OpenAPI/MultipleTouchline"

# kkunal documents the MultipleTouchline payload three contradictory ways: the
# docstring says "comma-separated segments and tokens", a code comment says
# "SegmentId1,Token1|SegmentId2,Token2", and the only runnable example passes
# "1@2885,1@11536". Rather than guess, try each shape until one returns quotes
# and remember the winner for the rest of the session.
TOUCHLINE_FORMATS: tuple[tuple[str, Callable[[list[Contract]], str]], ...] = (
    ("segment@token,", lambda cs: ",".join(f"{c.segment_id}@{c.token}" for c in cs)),
    ("segment,token|", lambda cs: "|".join(f"{c.segment_id},{c.token}" for c in cs)),
    ("segment,token,", lambda cs: ",".join(f"{c.segment_id},{c.token}" for c in cs)),
    ("token,", lambda cs: ",".join(str(c.token) for c in cs)),
)

# Response rows have been seen under several envelope keys.
_ROW_KEYS = ("Touchline", "data", "Data", "MultipleTouchline", "Result", "items")
_TOKEN_KEYS = ("Token", "token", "ScripCode", "scripcode", "InstrumentToken")
_LTP_KEYS = ("LTP", "Ltp", "ltp", "LastTradedPrice", "LastRate", "Last", "ClosePrice")
_BID_KEYS = ("BestBidPrice", "BidPrice", "Bid", "BuyPrice", "BestBuyPrice", "bid")
_ASK_KEYS = ("BestAskPrice", "AskPrice", "Ask", "SellPrice", "BestSellPrice", "BestOfferPrice", "ask")


@dataclass(frozen=True)
class Quote:
    """One instrument's touch.

    ``bid``/``ask`` are optional because not every Choice response carries the
    depth, and a paper fill must be able to say honestly whether it crossed a
    real spread or fell back to modelling one.
    """

    token: int
    ltp: float
    bid: float | None = None
    ask: float | None = None
    # True when this price came from the last traded candle rather than the
    # live book. Still a real Choice price, just not the current touch.
    stale: bool = False

    @property
    def has_depth(self) -> bool:
        return self.bid is not None and self.ask is not None and self.ask >= self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0 if self.has_depth else self.ltp

    @property
    def spread(self) -> float | None:
        return (self.ask - self.bid) if self.has_depth else None


def _paisa(value: Any) -> float | None:
    """Choice quotes prices in paisa; anything unparseable or <=0 is absent."""
    try:
        price = float(value) / 100.0
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _first(row: dict, keys: tuple[str, ...]) -> Any:
    return next((row[k] for k in keys if row.get(k) not in (None, "")), None)


def iter_quote_rows(body: Any) -> Iterator[dict]:
    """Yield quote rows from whatever envelope Choice wrapped them in.

    The endpoint has been seen returning a bare list of rows, a list under one
    of several envelope keys, a dict keyed by token, and a single row returned
    bare. Enumerating those shapes is what kept failing, so this descends until
    it finds dicts that actually look like rows and lets the caller filter.
    """
    if isinstance(body, dict):
        if any(k in body for k in _TOKEN_KEYS):
            yield body
            return
        for value in body.values():
            yield from iter_quote_rows(value)
    elif isinstance(body, list):
        for item in body:
            yield from iter_quote_rows(item)


def describe_payload(value: Any, depth: int = 0) -> str:
    """A bounded description of an unparseable payload.

    "dict keys=['MultipleTouchline']" says nothing about *why* parsing failed,
    which is precisely when this gets read, so single-key wrappers are unwrapped
    and the first element of a list is described too.
    """
    if isinstance(value, dict):
        keys = sorted(value)[:8]
        if depth < 3 and len(value) == 1:
            inner = next(iter(value.values()))
            return f"dict keys={keys} -> {describe_payload(inner, depth + 1)}"
        return f"dict keys={keys}"
    if isinstance(value, list):
        if not value:
            return "empty list"
        return f"list[{len(value)}] of {describe_payload(value[0], depth + 1)}"
    if isinstance(value, str):
        return f"str {value[:160]!r}"
    if value is None:
        return "null"
    return type(value).__name__


@dataclass
class ChoiceMarketData:
    """Historical and live market data, sourced exclusively from Choice."""

    session: ChoiceSession
    master: ScripMaster
    history: HistoryClient
    reports: list[FetchReport] = field(default_factory=list)
    # Which payload shape MultipleTouchline actually accepted, once known.
    touchline_format: str | None = None
    last_touchline_error: str | None = None

    @classmethod
    def connect(cls, session: ChoiceSession | None = None) -> "ChoiceMarketData":
        """Log in, load the scrip master and calibrate the ChartData epoch."""
        session = session or ChoiceSession()
        session.ensure_session()
        session.save_session()

        master = ScripMaster()
        master.fetch()

        history = HistoryClient(session)
        index = master.find_index(NIFTY)
        if index is not None:
            try:
                history.calibrate_epoch(index.segment_id, index.token)
            except ChoiceError as exc:
                log.warning("Epoch calibration failed, using default offset: %s", exc)

        return cls(session=session, master=master, history=history)

    # ------------------------------------------------------------ historical

    def candles(
        self,
        contract: Contract,
        start: dt.date | dt.datetime,
        end: dt.date | dt.datetime,
        resolution: str = "5",
        *,
        strict: bool = False,
    ) -> pd.DataFrame:
        """Candles for any resolved contract, recording a coverage report."""
        frame, report = self.history.fetch(
            contract.segment_id, contract.token, start, end, resolution, allow_partial=not strict
        )
        self.reports.append(report)
        if strict and report.status != "ok":
            raise ChoiceNoDataError(
                f"No usable Choice data for {contract} "
                f"[{report.start:%Y-%m-%d}..{report.end:%Y-%m-%d} @ {resolution}]: "
                f"{report.error_message}"
            )
        return frame

    def index_candles(
        self, name: str, start, end, resolution: str = "D", *, strict: bool = True
    ) -> pd.DataFrame:
        return self.candles(self.master.index(name), start, end, resolution, strict=strict)

    def nifty(self, start, end, resolution: str = "D") -> pd.DataFrame:
        """The underlying series the ladder runs on."""
        return self.index_candles(NIFTY, start, end, resolution)

    def india_vix(self, start, end, resolution: str = "D") -> pd.DataFrame:
        """India VIX, used as the at-the-money volatility level."""
        return self.index_candles(INDIA_VIX, start, end, resolution)

    def vix_by_date(self, start, end) -> dict[dt.date, float]:
        """Daily India VIX closes keyed by date.

        Returns an empty mapping if Choice has no VIX series, rather than
        substituting a constant: the caller decides whether to proceed.
        """
        try:
            frame = self.india_vix(start, end, "D")
        except (ChoiceError, ChoiceInstrumentError) as exc:
            log.warning("India VIX unavailable from Choice: %s", exc)
            return {}
        if frame.empty:
            return {}
        return {row.ts.date(): float(row.close) for row in frame.itertuples()}

    def option_candles(
        self,
        underlying: str,
        expiry: dt.date,
        strike: float,
        right: str,
        start,
        end,
        resolution: str = "5",
    ) -> pd.DataFrame:
        """Historical premiums for one option leg."""
        contract = self.master.option(underlying, expiry, strike, right)
        return self.candles(contract, start, end, resolution)

    # ------------------------------------------------------------------ live

    def quotes(
        self,
        contracts: Iterable[Contract],
        *,
        allow_history_fallback: bool = True,
    ) -> dict[int, Quote]:
        """Best available price per token, live if possible.

        ``MultipleTouchline`` has been observed answering ``Success`` with an
        empty row list for index tokens -- correctly addressed, during market
        hours, and simply not served. That left the ladder with no spot at all
        and a forward run that could never start.

        So a missing quote falls back to ChartData, which serves the same
        instrument from the same broker: the close of the most recent candle.
        It is a real traded price rather than a model, but it is not the touch,
        so it is flagged ``stale`` and counted separately. Only when *both*
        endpoints come back empty is this an error.
        """
        items = list(contracts)
        if not items:
            return {}

        found = self._touchline_quotes(items)
        missing = [c for c in items if c.token not in found]

        if missing and allow_history_fallback:
            for token, price in self.last_prices(missing).items():
                found[token] = Quote(token=token, ltp=price, stale=True)

        if not found:
            raise ChoiceError(
                "No usable quote from MultipleTouchline or ChartData. Tried "
                f"{len(TOUCHLINE_FORMATS)} payload shapes: "
                f"{self.last_touchline_error or 'no rows returned'}"
            )
        return found

    def _touchline_quotes(self, items: list[Contract]) -> dict[int, Quote]:
        """Probe each documented payload shape until one yields quotes.

        The SDK documents three mutually exclusive formats and only testing
        against the live endpoint settles it. Prices arrive in paisa despite an
        upstream comment claiming otherwise, so they are divided by 100.
        Returns ``{}`` rather than raising: an empty book is a fact for the
        caller to handle, not an exception.
        """
        # Prefer the shape that already worked this session.
        ordered = sorted(
            TOUCHLINE_FORMATS, key=lambda f: f[0] != self.touchline_format
        )
        attempts: list[str] = []

        for name, build in ordered:
            payload = {"MultipleSegToken": build(items)}
            try:
                resp = self.session.request("POST", TOUCHLINE_ENDPOINT, payload)
            except ChoiceError as exc:
                attempts.append(f"{name}: {exc}")
                continue

            if str(resp.get("Status", "")).lower() != "success":
                attempts.append(f"{name}: {resp.get('Message') or 'non-success status'}")
                continue

            parsed = self._parse_quotes(resp)
            if parsed:
                if self.touchline_format != name:
                    log.info("MultipleTouchline accepted the %r payload shape", name)
                self.touchline_format = name
                self.last_touchline_error = None
                return parsed

            attempts.append(f"{name}: succeeded but no rows parsed ({self._shape_of(resp)})")

        self.last_touchline_error = " | ".join(attempts[:4])
        return {}

    def last_prices(self, contracts: Iterable[Contract], *, lookback_days: int = 7) -> dict[int, float]:
        """Latest traded price per token, from ChartData.

        Used when the live book is unavailable. A week of lookback covers a
        long weekend plus a holiday, so a Monday morning before the open still
        resolves to Friday's close rather than nothing.
        """
        out: dict[int, float] = {}
        if self.history is None:
            return out
        end = dt.datetime.now(tz=IST)
        start = end - dt.timedelta(days=lookback_days)
        for contract in contracts:
            try:
                frame = self.candles(contract, start, end, "1")
            except ChoiceError as exc:
                # The broker declining is ordinary here; the caller still gets
                # the primary touchline error, which is the useful one.
                log.debug("No fallback candle for token %s: %s", contract.token, exc)
                continue
            except Exception:                   # noqa: BLE001
                # Anything else is our bug, not Choice's. Swallowing it keeps
                # the run alive, but silently would make it undiagnosable --
                # it surfaces as "no quote" three layers away.
                log.exception("Fallback candle fetch failed for token %s", contract.token)
                continue
            if frame is None or frame.empty:
                continue
            try:
                price = float(frame.iloc[-1]["close"])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if price > 0:
                out[contract.token] = price
        return out

    def touchline(self, contracts: Iterable[Contract]) -> dict[int, float]:
        """LTP only, for callers that do not care about the spread."""
        return {token: q.ltp for token, q in self.quotes(contracts).items()}

    @staticmethod
    def _shape_of(resp: dict) -> str:
        """Describe an unparseable response precisely enough to act on it."""
        return describe_payload(resp.get("Response"))

    @staticmethod
    def _parse_quotes(resp: dict) -> dict[int, Quote]:
        out: dict[int, Quote] = {}
        for row in iter_quote_rows(resp.get("Response")):
            token, ltp = _first(row, _TOKEN_KEYS), _first(row, _LTP_KEYS)
            if token is None or ltp is None:
                continue
            price = _paisa(ltp)
            if price is None:
                continue
            token = int(float(token))
            out[token] = Quote(
                token=token,
                ltp=price,
                bid=_paisa(_first(row, _BID_KEYS)),
                ask=_paisa(_first(row, _ASK_KEYS)),
            )
        return out

    def ltp(self, contract: Contract) -> float | None:
        return self.touchline([contract]).get(contract.token)
    # -------------------------------------------------------------- coverage

    def coverage_summary(self) -> dict[str, int]:
        """What succeeded and what did not, for the Data Health page."""
        return {
            "fetches": len(self.reports),
            "ok": sum(1 for r in self.reports if r.status == "ok"),
            "no_data": sum(1 for r in self.reports if r.status == "no_data"),
            "errors": sum(1 for r in self.reports if r.status == "error"),
            "bars": sum(r.bars for r in self.reports),
            "requests": sum(r.requests_made for r in self.reports),
        }

    def failures(self) -> list[dict[str, str]]:
        return [
            {
                "token": str(r.token),
                "resolution": r.resolution,
                "range": f"{r.start:%Y-%m-%d}..{r.end:%Y-%m-%d}",
                "status": r.status,
                "error": r.error_message or "",
            }
            for r in self.reports
            if r.status != "ok"
        ]
