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
from typing import Callable, Iterable

import pandas as pd

from engine.choice.errors import ChoiceError, ChoiceInstrumentError, ChoiceNoDataError
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

    def touchline(self, contracts: Iterable[Contract]) -> dict[int, float]:
        """Snapshot LTP for a set of contracts, keyed by token.

        Tries each documented payload shape until one yields quotes, because
        the SDK documents three mutually exclusive formats and only testing
        against the live endpoint settles it. Prices arrive in paisa despite an
        upstream comment claiming otherwise, so they are divided by 100.
        """
        items = list(contracts)
        if not items:
            return {}

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

            quotes = self._parse_touchline(resp)
            if quotes:
                if self.touchline_format != name:
                    log.info("MultipleTouchline accepted the %r payload shape", name)
                self.touchline_format = name
                self.last_touchline_error = None
                return quotes

            attempts.append(f"{name}: succeeded but no rows parsed ({self._shape_of(resp)})")

        self.last_touchline_error = " | ".join(attempts[:4])
        raise ChoiceError(
            "MultipleTouchline returned no usable quotes. Tried "
            f"{len(ordered)} payload shapes: {self.last_touchline_error}"
        )

    @staticmethod
    def _shape_of(resp: dict) -> str:
        """Describe an unrecognised response so the cause is visible."""
        body = resp.get("Response")
        if isinstance(body, list):
            first = body[0] if body else None
            return f"list[{len(body)}], first keys={sorted(first)[:8] if isinstance(first, dict) else type(first).__name__}"
        if isinstance(body, dict):
            return f"dict keys={sorted(body)[:8]}"
        return f"Response is {type(body).__name__}"

    @staticmethod
    def _parse_touchline(resp: dict) -> dict[int, float]:
        body = resp.get("Response") or []
        rows: list = []
        if isinstance(body, list):
            rows = body
        elif isinstance(body, dict):
            for key in _ROW_KEYS:
                value = body.get(key)
                if isinstance(value, list):
                    rows = value
                    break
            else:
                # A single quote returned bare rather than in a list.
                if any(k in body for k in _TOKEN_KEYS):
                    rows = [body]

        out: dict[int, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            token = next((row[k] for k in _TOKEN_KEYS if row.get(k) not in (None, "")), None)
            ltp = next((row[k] for k in _LTP_KEYS if row.get(k) not in (None, "")), None)
            if token is None or ltp is None:
                continue
            try:
                price = float(ltp) / 100.0
            except (TypeError, ValueError):
                continue
            if price > 0:
                out[int(float(token))] = price
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
