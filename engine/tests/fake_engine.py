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

from engine.auth import sessions as auth
from engine.choice.errors import ChoiceAuthError, StaticIpRejectedError

GOOD_KEY = "good-key"


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
        if "UserProfile" in endpoint:
            return {
                "Status": "Success",
                "Response": {"Name": f"Test Trader {self.config.mobile_no[-2:]}", "UCC": "X12345"},
            }
        return {"Status": "Success", "Response": {}}

    def logoff(self):
        self.session_id = None


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
