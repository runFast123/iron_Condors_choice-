"""A real engine process backed by a fake Choice, for end-to-end UI testing.

Runs the genuine FastAPI app, the genuine session registry and the genuine
cookie/middleware path — only the outbound Choice calls are stubbed. That way
the login flow under test is the one that ships, not a mock of it.

    python -m engine.tests.fake_engine --port 8010

Credentials accepted: any vendor id and mobile, with api_key "good-key".
Use api_key "bad-key" to exercise the rejection path, or "wrong-ip" to
exercise the static-IP rejection.
"""

from __future__ import annotations

import argparse
import os

from engine.auth import sessions as auth
from engine.choice.errors import ChoiceAuthError, StaticIpRejectedError

GOOD_KEY = "good-key"

# Set NO_CANDLES=1 to exercise the "Choice serves no history" failure path
# instead of returning data.
SERVE_CANDLES = os.environ.get("NO_CANDLES", "") != "1"

# Set EMPTY_TOUCHLINE=1 to reproduce what real Choice does with index tokens:
# answer Success with an empty row list. The ladder must then source spot from
# ChartData instead of stalling with no price.
EMPTY_TOUCHLINE = os.environ.get("EMPTY_TOUCHLINE", "") == "1"


def _synthetic_candles(payload: dict) -> dict:
    """A believable OHLC series, so a full backtest can complete end to end.

    Real enough in shape -- comma-joined rows, seconds since the 1980 epoch, a
    PriceDivisor -- that it exercises the same parsing path as live data.
    """
    if not SERVE_CANDLES:
        return {"Status": "Failure", "Message": "stub engine serves no candles"}

    import math

    start = int(payload.get("FromDate") or 0)
    end = int(payload.get("ToDate") or start + 86_400)
    token = int(payload.get("Token") or 0)
    step = 86_400
    rows = []
    # Index tokens drift like an index; option tokens sit at a plausible premium.
    is_index = token in (26000, 26017)
    base = 24_000.0 if token == 26000 else 14.0 if token == 26017 else 120.0
    n = 0
    t = start
    while t <= end and n < 400:
        if is_index and token == 26000:
            px = base - 600 * math.sin(math.pi * (n / 120.0)) + 40 * math.sin(n / 4.0)
        elif token == 26017:
            px = base + 2 * math.sin(n / 7.0)
        else:
            px = max(0.5, base - n * 0.4 + 8 * math.sin(n / 3.0))
        o = px * 0.999
        h = px * 1.004
        low = px * 0.996
        rows.append(f"{t},{o * 100:.0f},{h * 100:.0f},{low * 100:.0f},{px * 100:.0f},1000,50")
        t += step
        n += 1
    return {
        "Status": "Success",
        "Response": {"lstChartHistory": rows, "PriceDivisor": 100},
    }


class FakeChoiceSession:
    def __init__(self, config):
        self.config = config
        self.session_id = None

    def login(self, force: bool = False):
        key = (self.config.api_key or "").strip()
        if key == "wrong-ip":
            raise StaticIpRejectedError("Request came from 203.0.113.9")
        if key != GOOD_KEY:
            raise ChoiceAuthError("HTTP 401: Invalid API key or mobile number")
        self.session_id = "fake-session"
        return self.session_id

    def request(self, method, endpoint, data=None, **kw):
        if "MultipleTouchline" in endpoint:
            return _synthetic_touchline(data or {})
        if "ChartData" in endpoint:
            return _synthetic_candles(data or {})
        if "UserProfile" in endpoint:
            return {
                "Status": "Success",
                "Response": {"Name": f"Test Trader {self.config.mobile_no[-2:]}", "UCC": "X12345"},
            }
        return {"Status": "Success", "Response": {}}

    def ensure_session(self):
        return self.session_id or self.login()

    def save_session(self, path=None):
        return True

    def logoff(self):
        self.session_id = None


def _synthetic_touchline(payload: dict) -> dict:
    """Live quotes in the shape the app parses, so the chart can be driven.

    Only understands the segment@token,... form, which is what lets the format
    probe in ChoiceMarketData.touchline be exercised for real.
    """
    import math
    import time

    if EMPTY_TOUCHLINE:
        return {"Status": "Success", "Response": {"MultipleTouchline": []}}

    raw = str(payload.get("MultipleSegToken") or "")
    if "@" not in raw:
        return {"Status": "Success", "Response": []}

    rows = []
    now = time.time()
    for part in raw.split(","):
        part = part.strip()
        if "@" not in part:
            continue
        try:
            token = int(part.split("@", 1)[1])
        except ValueError:
            continue
        if token == 26000:                      # NIFTY, drifting
            px = 24_000 + 90 * math.sin(now / 40.0) + 25 * math.sin(now / 7.0)
        elif token == 26017:                    # India VIX
            px = 13.5 + 0.6 * math.sin(now / 90.0)
        else:                                   # an option premium
            px = max(0.5, 120 + 30 * math.sin(now / 25.0 + token % 17))
        rows.append({"Token": token, "LTP": round(px * 100)})   # paisa
    return {"Status": "Success", "Response": rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()

    auth.ChoiceSession = FakeChoiceSession  # type: ignore[assignment]

    import uvicorn

    from engine.api import app

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
