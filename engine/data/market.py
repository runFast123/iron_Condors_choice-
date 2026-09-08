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
from typing import Iterable

import pandas as pd

from engine.choice.errors import ChoiceError, ChoiceInstrumentError, ChoiceNoDataError
from engine.choice.history import FetchReport, HistoryClient
from engine.choice.instruments import Contract, ScripMaster
from engine.choice.session import ChoiceSession
from engine.config import IST

log = logging.getLogger(__name__)

NIFTY = "NIFTY"
INDIA_VIX = "INDIAVIX"

TOUCHLINE_ENDPOINT = "api/OpenAPI/MultipleTouchline"


@dataclass
class ChoiceMarketData:
    """Historical and live market data, sourced exclusively from Choice."""

    session: ChoiceSession
    master: ScripMaster
    history: HistoryClient
    reports: list[FetchReport] = field(default_factory=list)

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

        ``kkunal`` documents the MultipleSegToken format three contradictory
        ways, so we send the segment@token form its only runnable example uses
        and validate what comes back. Prices arrive in paisa despite an
        upstream comment claiming otherwise, so they are divided by 100.
        """
        items = list(contracts)
        if not items:
            return {}
        payload = {"MultipleSegToken": ",".join(f"{c.segment_id}@{c.token}" for c in items)}
        resp = self.session.request("POST", TOUCHLINE_ENDPOINT, payload)

        if str(resp.get("Status", "")).lower() != "success":
            raise ChoiceError(
                f"MultipleTouchline failed: {resp.get('Message') or resp}", payload=payload
            )

        body = resp.get("Response") or []
        rows = body if isinstance(body, list) else body.get("Touchline") or body.get("data") or []

        out: dict[int, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            token = row.get("Token") or row.get("token") or row.get("ScripCode")
            ltp = row.get("LTP") or row.get("Ltp") or row.get("LastTradedPrice") or row.get("ltp")
            if token is None or ltp is None:
                continue
            try:
                out[int(float(token))] = float(ltp) / 100.0
            except (TypeError, ValueError):
                continue
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
