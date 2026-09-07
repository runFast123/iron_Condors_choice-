"""Yahoo Finance fallback — underlying series only.

**Scope limit, enforced in code:** Yahoo carries no Indian option chain.  It
can supply the NIFTY index (``^NSEI``) and India VIX (``^INDIAVIX``) and
nothing else this project needs.  It is therefore used for exactly two things:

1. the spot series that drives the ladder, when Choice spot history is
   unavailable, and
2. the ATM volatility level that feeds modeled premiums.

Any attempt to source an option premium here raises.  That guard exists
because silently substituting a wrong premium is the failure mode that would
most easily go unnoticed in a backtest.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass

import pandas as pd

from engine.config import IST

log = logging.getLogger(__name__)

NIFTY = "^NSEI"
INDIA_VIX = "^INDIAVIX"

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Yahoo's own retention limits for intraday data.
_INTERVAL_MAX_DAYS = {"1m": 7, "2m": 60, "5m": 60, "15m": 60, "30m": 60, "60m": 730, "1d": 10_000}

_RESOLUTION_TO_YAHOO = {
    "1": "1m", "2": "2m", "5": "5m", "15": "15m", "30": "30m", "60": "60m",
    "D": "1d", "W": "1wk", "M": "1mo",
}


class YahooOptionDataUnavailable(RuntimeError):
    """Raised on any attempt to source an option premium from Yahoo."""


def option_premium(*_args, **_kwargs):
    """Always raises. Yahoo has no Indian option chain — do not guess one."""
    raise YahooOptionDataUnavailable(
        "Yahoo Finance does not provide NSE/NFO option chains. Option premiums must come "
        "from Choice, or be explicitly modeled via engine.pricing.black76 and tagged MODELED."
    )


@dataclass
class YahooClient:
    timeout: float = 30.0
    user_agent: str = "Mozilla/5.0"

    def _get(self, symbol: str, params: dict) -> dict:
        url = f"{CHART_URL.format(symbol=urllib.parse.quote(symbol))}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def candles(
        self,
        symbol: str,
        start: dt.date | dt.datetime,
        end: dt.date | dt.datetime,
        resolution: str = "D",
    ) -> pd.DataFrame:
        """Fetch OHLCV, returned in the same shape as the Choice history client."""
        interval = _RESOLUTION_TO_YAHOO.get(str(resolution), str(resolution))

        start_dt = _as_datetime(start)
        end_dt = _as_datetime(end, end_of_day=True)

        limit = _INTERVAL_MAX_DAYS.get(interval)
        if limit and (end_dt - start_dt).days > limit:
            # Yahoo silently truncates rather than erroring, so say so.
            log.warning(
                "Yahoo retains only %d days of %s data; requested range will be truncated.",
                limit, interval,
            )
            start_dt = max(start_dt, end_dt - dt.timedelta(days=limit))

        payload = self._get(
            symbol,
            {
                "period1": int(start_dt.timestamp()),
                "period2": int(end_dt.timestamp()),
                "interval": interval,
                "includePrePost": "false",
                "events": "div,splits",
            },
        )

        chart = (payload or {}).get("chart") or {}
        if chart.get("error"):
            raise RuntimeError(f"Yahoo error for {symbol}: {chart['error']}")
        results = chart.get("result") or []
        if not results:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "oi"])

        result = results[0]
        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]

        frame = pd.DataFrame(
            {
                "ts": [dt.datetime.fromtimestamp(t, tz=dt.timezone.utc).astimezone(IST) for t in stamps],
                "open": quote.get("open") or [],
                "high": quote.get("high") or [],
                "low": quote.get("low") or [],
                "close": quote.get("close") or [],
                "volume": quote.get("volume") or [],
            }
        )
        if frame.empty:
            return frame.assign(oi=[])

        frame = frame.dropna(subset=["close"]).reset_index(drop=True)
        frame["volume"] = frame["volume"].fillna(0).astype("int64")
        frame["oi"] = 0
        return frame

    def nifty(self, start, end, resolution: str = "D") -> pd.DataFrame:
        return self.candles(NIFTY, start, end, resolution)

    def india_vix(self, start, end, resolution: str = "D") -> pd.DataFrame:
        return self.candles(INDIA_VIX, start, end, resolution)

    def vix_by_date(self, start, end) -> dict[dt.date, float]:
        """Daily India VIX closes, keyed by date — the ATM vol input."""
        frame = self.india_vix(start, end, "D")
        if frame.empty:
            return {}
        return {row.ts.date(): float(row.close) for row in frame.itertuples()}


def _as_datetime(value, end_of_day: bool = False) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=IST)
    time = dt.time(23, 59, 59) if end_of_day else dt.time(0, 0)
    return dt.datetime.combine(value, time, tzinfo=IST)
