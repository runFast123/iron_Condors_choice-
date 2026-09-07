"""End-to-end diagnostic against the live Choice FinX API.

This is the proof that the historical-data path works. It logs in, loads the
scrip master, resolves a real NIFTY option contract, calibrates the ChartData
epoch, and fetches candles for both the index and one option leg — printing
exactly what succeeded and, on failure, **Choice's own error message** rather
than the empty DataFrame the upstream library would hand back.

    python -m engine.tools.doctor
    python -m engine.tools.doctor --underlying NIFTY --resolution 5

Requires CHOICE_VENDOR_ID / CHOICE_API_KEY / CHOICE_MOBILE_NO in .env, and
must be run from the static IP declared against that API key.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import traceback

from engine.choice.errors import ChoiceError, StaticIpRejectedError
from engine.choice.history import HistoryClient
from engine.choice.instruments import ScripMaster
from engine.choice.session import ChoiceSession
from engine.config import IST, choice_config

OK = "  [ OK ]"
BAD = "  [FAIL]"
INFO = "       "


class Doctor:
    def __init__(self, underlying: str = "NIFTY", resolution: str = "5") -> None:
        self.underlying = underlying.upper()
        self.resolution = resolution
        self.failures = 0
        self.session: ChoiceSession | None = None
        self.master: ScripMaster | None = None
        self.history: HistoryClient | None = None
        self.nfo_segment: int | None = None

    def step(self, name: str) -> None:
        print(f"\n{name}")
        print("-" * max(46, len(name)))

    def fail(self, message: str) -> None:
        self.failures += 1
        print(f"{BAD} {message}")

    # ------------------------------------------------------------------ steps

    def check_config(self) -> bool:
        self.step("1. Credentials")
        if not choice_config.configured:
            self.fail("Missing credentials. Copy .env.example to .env and fill in:")
            print(f"{INFO}CHOICE_VENDOR_ID, CHOICE_API_KEY, CHOICE_MOBILE_NO")
            return False
        print(f"{OK} vendor id, api key and mobile are set")
        print(f"{INFO}base url: {choice_config.base_url}")
        print(f"{INFO}timeouts: connect {choice_config.connect_timeout}s / read {choice_config.read_timeout}s")
        print(f"{INFO}rate limit: {choice_config.data_rate_limit}/s data, {choice_config.order_rate_limit}/s orders")
        return True

    def check_login(self) -> bool:
        self.step("2. Login (non-interactive TOTP)")
        try:
            self.session = ChoiceSession()
            session_id = self.session.ensure_session()
        except StaticIpRejectedError as exc:
            self.fail("Rejected as coming from an undeclared IP.")
            print(f"{INFO}{exc}")
            return False
        except ChoiceError as exc:
            self.fail(f"Login failed: {exc}")
            return False
        print(f"{OK} session established ({session_id[:6]}...)")
        if self.session.access_token:
            print(f"{OK} access token present (needed by the price feed)")
        else:
            self.fail("access_token is None - the price-feed logon would send an empty token")
        self.session.save_session()
        return True

    def check_scrip_master(self) -> bool:
        self.step("3. Scrip master")
        try:
            self.master = ScripMaster()
            self.master.fetch()
        except ChoiceError as exc:
            self.fail(f"Could not load the scrip master: {exc}")
            return False
        print(f"{OK} loaded for {self.master.loaded_for} - {len(self.master.contracts):,} contracts")
        print(f"{INFO}columns detected: {', '.join(sorted(self.master.columns))}")

        try:
            self.nfo_segment = self.master.infer_nfo_segment(self.underlying)
            print(f"{OK} NFO segment id inferred from data: {self.nfo_segment}")
            print(f"{INFO}(kkunal's README claims both 2 and 13; this is read off real rows)")
        except ChoiceError as exc:
            self.fail(str(exc))
            return False

        try:
            lot = self.master.lot_size_for(self.underlying)
            print(f"{OK} {self.underlying} lot size: {lot}")
        except ChoiceError as exc:
            self.fail(str(exc))
        return True

    def check_option_resolution(self):
        self.step("4. Option contract resolution")
        assert self.master is not None
        today = dt.datetime.now(tz=IST).date()
        try:
            expiry = self.master.nearest_expiry(self.underlying, today, min_days=1)
        except ChoiceError as exc:
            self.fail(str(exc))
            return None
        print(f"{OK} nearest expiry: {expiry:%d-%b-%Y}")

        strikes = self.master.strikes(self.underlying, expiry)
        if not strikes:
            self.fail("No strikes listed for that expiry")
            return None
        step = self.master.strike_step(self.underlying, expiry)
        print(f"{OK} {len(strikes)} strikes listed, step {step:g} "
              f"({strikes[0]:g} .. {strikes[-1]:g})")

        middle = strikes[len(strikes) // 2]
        try:
            contract = self.master.option(self.underlying, expiry, middle, "PE")
        except ChoiceError as exc:
            self.fail(str(exc))
            return None
        print(f"{OK} resolved {contract} -> token {contract.token}, segment {contract.segment_id}")
        return contract

    def check_history(self, contract) -> None:
        self.step("5. Historical data (the ChartData fix)")
        assert self.session is not None
        self.history = HistoryClient(self.session)

        index = self._find_index_token()
        if index is not None:
            print(f"{INFO}calibrating epoch against {self.underlying} index token {index.token}...")
            try:
                offset = self.history.calibrate_epoch(index.segment_id, index.token)
                print(f"{OK} epoch offset: {offset}s "
                      f"({'IST-naive' if offset == 0 else 'shifted'})")
            except ChoiceError as exc:
                self.fail(f"Epoch calibration failed: {exc}")

            self._fetch("index", index.segment_id, index.token, days=5)

        if contract is not None:
            print()
            self._fetch(
                f"option {contract}", contract.segment_id, contract.token, days=5,
                note="If this returns no bars, Choice does not serve historical NFO option "
                     "candles for this contract, and backtests must use MODELED premiums.",
            )

    def _find_index_token(self):
        assert self.master is not None
        for contract in self.master.contracts:
            if (
                not contract.is_option
                and contract.symbol.upper() == self.underlying
                and (contract.series or "").upper() in ("", "INDEX", "IN", "EQ")
            ):
                return contract
        matches = [c for c in self.master.search(self.underlying, limit=200) if not c.is_option]
        return matches[0] if matches else None

    def _fetch(self, label: str, segment_id: int, token: int, days: int, note: str = "") -> None:
        assert self.history is not None
        end = dt.datetime.now(tz=IST)
        start = end - dt.timedelta(days=days)
        try:
            frame, report = self.history.fetch(
                segment_id, token, start, end, self.resolution, allow_partial=True
            )
        except ChoiceError as exc:
            self.fail(f"{label}: {exc}")
            return

        if report.status == "ok" and report.bars:
            print(f"{OK} {label}: {report.bars} bars in {report.requests_made} request(s)")
            print(f"{INFO}first {frame.iloc[0]['ts']}  close {frame.iloc[0]['close']:.2f}")
            print(f"{INFO}last  {frame.iloc[-1]['ts']}  close {frame.iloc[-1]['close']:.2f}")
            if report.bad_rows:
                print(f"{INFO}{report.bad_rows} malformed row(s) skipped (not fatal)")
        elif report.status == "no_data":
            self.fail(f"{label}: Choice returned no bars for the last {days} days")
            print(f"{INFO}{report.error_message}")
            if note:
                print(f"{INFO}{note}")
        else:
            self.fail(f"{label}: {report.error_message}")
            for win_start, win_end, error in report.windows_failed[:3]:
                print(f"{INFO}{win_start[:16]} .. {win_end[:16]}: {error}")
            if note:
                print(f"{INFO}{note}")

    # ------------------------------------------------------------------- run

    def run(self) -> int:
        print("=" * 60)
        print("  Choice FinX connectivity doctor")
        print("=" * 60)

        if not self.check_config():
            return 1
        if not self.check_login():
            return 1
        if not self.check_scrip_master():
            return 1
        contract = self.check_option_resolution()
        self.check_history(contract)

        print()
        print("=" * 60)
        if self.failures:
            print(f"  {self.failures} check(s) FAILED - see the messages above.")
        else:
            print("  All checks passed. Re-run `python -m engine.tools.seed` to")
            print("  rebuild the dashboard dataset with real Choice premiums.")
        print("=" * 60)
        return 1 if self.failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--resolution", default="5")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return Doctor(args.underlying, args.resolution).run()
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001 - a diagnostic must never mask a traceback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
