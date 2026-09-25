"""The backtest's backup source, the official-close settlement, and VIX as of
the moment -- the three changes that make a replay's numbers trustworthy.

The backup is exercised through the real client against a fake HTTP server,
so the code under test is the code that ships. Nothing here reaches the
network; conftest switches the shared client off and each test that wants
one builds its own.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re

import pandas as pd
import pytest

from engine.config import IST
from engine.data import groww
from engine.strategy.condor import PriceSource

REPO = pathlib.Path(__file__).resolve().parents[2]
OTHER_VENDORS = re.compile(r"yahoo|yfinance|groww|dhan", re.IGNORECASE)


# ======================================================== a fake HTTP server


class Resp:
    def __init__(self, status: int, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body


def _day_range(start: str, end: str) -> list[dt.date]:
    a = dt.datetime.strptime(start[:10], "%Y-%m-%d").date()
    b = dt.datetime.strptime(end[:10], "%Y-%m-%d").date()
    return [a + dt.timedelta(days=i) for i in range((b - a).days + 1)]


class FakeHttp:
    """Serves Groww-shaped answers: a token, and candles for any symbol."""

    def __init__(self, *, level=24_000.0, vix=13.0, premium=55.0, candle_status=200,
                 token_status=200, busy_first=0, skip_days=()):
        self.level, self.vix, self.premium = level, vix, premium
        self.candle_status, self.token_status = candle_status, token_status
        self.busy_first = busy_first
        self.skip_days = set(skip_days)
        self.posts: list[dict] = []
        self.gets: list[dict] = []
        self.tokens_issued = 0

    def post(self, url, json=None, timeout=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        if self.token_status != 200:
            return Resp(self.token_status, {"status": "FAILURE",
                                            "error": {"code": "GA004", "message": "Key not approved"}})
        self.tokens_issued += 1
        return Resp(200, {"token": f"tok-{self.tokens_issued}",
                          "expiry": "2099-01-01T06:00:00+05:30", "isActive": True})

    def get(self, url, params=None, timeout=None, headers=None):
        self.gets.append({"url": url, "params": dict(params or {}), "headers": headers})
        if self.busy_first > 0:
            self.busy_first -= 1
            return Resp(429, {"status": "FAILURE"}, {"Retry-After": "0"})
        if url.endswith(groww.CONTRACTS_PATH):
            return Resp(200, {"status": "SUCCESS", "payload": {"contracts": []}})
        if self.candle_status != 200:
            return Resp(self.candle_status, {"status": "FAILURE",
                                             "error": {"code": "GA005", "message": "Plan does not include this"}})
        symbol = params["groww_symbol"]
        interval = params["candle_interval"]
        price = self.vix if symbol == "NSE-INDIAVIX" else self.level if symbol == "NSE-NIFTY" else self.premium
        rows = []
        for day in _day_range(params["start_time"], params["end_time"]):
            if day.weekday() >= 5 or day in self.skip_days:
                continue
            if interval == "1day":
                rows.append([f"{day:%Y-%m-%d}T00:00:00", price, price, price, price, 0, None])
                continue
            step = int(interval.replace("minute", "")) if "minute" in interval else 60
            t = dt.datetime.combine(day, dt.time(9, 15))
            while t.time() < dt.time(15, 30):
                rows.append([f"{t:%Y-%m-%dT%H:%M:%S}", price, price, price, price, 10, 5])
                t += dt.timedelta(minutes=step)
        return Resp(200, {"status": "SUCCESS", "payload": {"candles": rows, "interval_in_minutes": 5}})


def client(http=None, **creds) -> groww.GrowwBackup:
    creds = creds or {"api_key": "key-123", "totp_secret": "JBSWY3DPEHPK3PXP"}
    return groww.GrowwBackup(groww.Credentials(**creds), http=http or FakeHttp(),
                             rate_per_second=0, sleep=lambda s: None)


# ================================================================ sign-in


def test_totp_matches_the_rfc_6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"         # base32 of "12345678901234567890"
    assert groww.totp(secret, 59, digits=8) == "94287082"
    assert groww.totp(secret, 1111111109, digits=8) == "07081804"


def test_a_totp_key_signs_itself_in_and_reuses_the_token():
    http = FakeHttp()
    c = client(http)
    c.index_candles("NIFTY", dt.date(2026, 9, 21), dt.date(2026, 9, 21), "5")
    c.index_candles("NIFTY", dt.date(2026, 9, 22), dt.date(2026, 9, 22), "5")
    assert http.tokens_issued == 1, "one sign-in serves every request until it lapses"
    body = http.posts[0]["json"]
    assert body["key_type"] == "totp" and len(body["totp"]) == 6
    assert http.posts[0]["headers"]["Authorization"] == "Bearer key-123"
    assert http.gets[0]["headers"]["Authorization"] == "Bearer tok-1"


def test_a_secret_key_signs_in_with_the_checksum():
    import hashlib

    http = FakeHttp()
    c = client(http, api_key="key-9", api_secret="s3cret")
    c.index_candles("NIFTY", dt.date(2026, 9, 21), dt.date(2026, 9, 21), "D")
    body = http.posts[0]["json"]
    assert body["key_type"] == "approval"
    assert body["checksum"] == hashlib.sha256(("s3cret" + body["timestamp"]).encode()).hexdigest()


def test_a_pasted_token_is_used_as_it_is():
    http = FakeHttp()
    c = client(http, access_token="tok-pasted")
    c.index_candles("NIFTY", dt.date(2026, 9, 21), dt.date(2026, 9, 21), "D")
    assert http.posts == [] and http.gets[0]["headers"]["Authorization"] == "Bearer tok-pasted"


def test_a_rejected_token_is_renewed_once():
    class Expiring(FakeHttp):
        def get(self, url, params=None, timeout=None, headers=None):
            if headers["Authorization"] == "Bearer tok-1":
                self.gets.append({"url": url})
                return Resp(401, {"status": "FAILURE", "error": {"message": "expired"}})
            return super().get(url, params, timeout, headers)

    http = Expiring()
    frame = client(http).index_candles("NIFTY", dt.date(2026, 9, 21), dt.date(2026, 9, 21), "D")
    assert http.tokens_issued == 2 and len(frame) == 1


def test_a_refused_sign_in_is_said_without_naming_anyone():
    with pytest.raises(groww.BackupUnavailable) as err:
        client(FakeHttp(token_status=403)).index_candles(
            "NIFTY", dt.date(2026, 9, 21), dt.date(2026, 9, 21), "D")
    assert "backup source" in str(err.value) and not OTHER_VENDORS.search(str(err.value))


def test_a_malformed_totp_secret_is_the_backups_own_error_not_a_crash():
    """An API secret pasted into the TOTP setting raised a raw base32 error,
    which no backtest handler catches -- the whole run would have failed."""
    c = client(FakeHttp(), api_key="key-1", totp_secret="p8#Xq!zR0lT9vW$e1Yu&bN3mK*sD4fG%")
    with pytest.raises(groww.BackupUnavailable) as err:
        c.index_candles("NIFTY", dt.date(2026, 9, 21), dt.date(2026, 9, 21), "D")
    assert "TOTP secret" in str(err.value) and not OTHER_VENDORS.search(str(err.value))


def test_a_plan_refusal_is_remembered_and_costs_no_sign_ins():
    """A 403 is the account's plan, not the token: re-minting on it spent
    sign-ins (150 a day), and asking again cannot change the answer."""
    clock = [1_790_000_000.0]
    http = FakeHttp(candle_status=403)
    c = groww.GrowwBackup(groww.Credentials(api_key="k", totp_secret="JBSWY3DPEHPK3PXP"), http=http,
                          rate_per_second=0, sleep=lambda s: None, clock=lambda: clock[0])
    for _ in range(3):
        with pytest.raises(groww.BackupUnavailable) as err:
            c.index_candles("NIFTY", dt.date(2026, 9, 21), dt.date(2026, 9, 21), "D")
    assert "plan may not include this data" in str(err.value)
    assert http.tokens_issued == 1 and len(http.gets) == 1, "one ask, then the refusal is remembered"
    clock[0] += groww.FORBIDDEN_HOLD + 1
    with pytest.raises(groww.BackupUnavailable):
        c.index_candles("NIFTY", dt.date(2026, 9, 21), dt.date(2026, 9, 21), "D")
    assert len(http.gets) == 2, "asked again once the hold ran out"


def test_throttling_is_retried():
    http = FakeHttp(busy_first=2)
    frame = client(http).index_candles("NIFTY", dt.date(2026, 9, 21), dt.date(2026, 9, 21), "D")
    assert len(frame) == 1 and len(http.gets) == 3


def test_credentials_come_from_neutral_environment_names():
    env = {"BACKUP_DATA_API_KEY": "k", "BACKUP_DATA_TOTP_SECRET": "JBSWY3DPEHPK3PXP"}
    assert groww.Credentials.from_env(env).usable
    assert not groww.Credentials.from_env({}).usable
    assert not groww.Credentials.from_env({"BACKUP_DATA_API_KEY": "k"}).usable


# ================================================================ candles


def test_bars_are_stamped_at_their_close_like_choices():
    frame = client().index_candles("NIFTY", dt.date(2026, 9, 21), dt.date(2026, 9, 21), "5")
    assert frame["ts"].iloc[0] == pd.Timestamp("2026-09-21 09:19:59", tz="Asia/Kolkata")
    assert str(frame["ts"].dt.tz) == "Asia/Kolkata" and list(frame.columns) == groww.COLUMNS


def test_daily_bars_are_stamped_at_midnight_and_epoch_times_are_read():
    shaped = groww.to_choice_shape([[1_790_000_000, 1, 2, 0.5, 1.5, 0, None]], "D")
    assert shaped["ts"].iloc[0].hour == 0 and shaped["close"].iloc[0] == 1.5
    assert groww.to_choice_shape([["2026-09-21T09:15:00", 1, 1, 1, 0, 0, 0]], "5").empty, \
        "a bar with no close is not a price"


def test_long_ranges_are_split_to_the_providers_limit():
    http = FakeHttp()
    client(http).index_candles("NIFTY", dt.date(2026, 1, 1), dt.date(2026, 3, 31), "5")
    spans = [(g["params"]["start_time"][:10], g["params"]["end_time"][:10]) for g in http.gets]
    assert len(spans) == 3
    assert spans[0] == ("2026-01-01", "2026-01-30") and spans[1][0] == "2026-01-31"


def test_option_symbols_use_the_listed_name_and_never_the_locale():
    assert groww.GrowwBackup.contract_symbol(dt.date(2026, 3, 30), 23_600.0, "PE") == "NSE-NIFTY-30Mar26-23600-PE"
    assert groww.GrowwBackup.contract_symbol(dt.date(2026, 9, 1), 24_050.0, "CE") == "NSE-NIFTY-01Sep26-24050-CE"


def test_an_options_bars_stop_at_its_expiry():
    frame = client().option_candles(dt.date(2026, 9, 22), 24_000.0, "CE",
                                    dt.date(2026, 9, 21), dt.date(2026, 9, 25), "15")
    assert frame["ts"].max() <= pd.Timestamp("2026-09-22 15:30", tz="Asia/Kolkata")


def test_a_contract_the_exchange_never_listed_is_not_fetched():
    class Listing(FakeHttp):
        def get(self, url, params=None, timeout=None, headers=None):
            if url.endswith(groww.CONTRACTS_PATH):
                self.gets.append({"url": url, "params": params})
                return Resp(200, {"status": "SUCCESS", "payload": ["NSE-NIFTY-22Sep26-24000-CE"]})
            return super().get(url, params, timeout, headers)

    http = Listing()
    c = client(http)
    assert c.option_candles(dt.date(2026, 9, 22), 99_000.0, "CE",
                            dt.date(2026, 9, 21), dt.date(2026, 9, 22), "15").empty
    assert all(g["url"].endswith(groww.CONTRACTS_PATH) for g in http.gets)
    assert not c.option_candles(dt.date(2026, 9, 22), 24_000.0, "CE",
                                dt.date(2026, 9, 21), dt.date(2026, 9, 22), "15").empty


def test_only_days_choice_skipped_are_filled_and_choice_is_never_replaced():
    week = [dt.date(2026, 9, 21) + dt.timedelta(days=i) for i in range(5)]
    choice = pd.DataFrame([{
        "ts": dt.datetime.combine(d, dt.time(9, 19, 59), tzinfo=IST), "open": 1.0, "high": 1.0,
        "low": 1.0, "close": 23_000.0, "volume": 1, "oi": 0,
    } for d in (week[0], week[1], week[4])])
    merged, taken, note = client().fill_missing_days(choice, "NIFTY", set(week), "5")
    assert taken == [week[2], week[3]] and note is None
    by_day = merged.groupby(merged["ts"].dt.date)["close"].first()
    assert by_day[week[0]] == 23_000.0 and by_day[week[2]] == 24_000.0


# ======================================== settlement and VIX, in the replay


def _replay(settlement=None, **kw):
    from engine.backtest.providers import ModelPriceProvider
    from engine.backtest.runner import Backtest, BacktestParams, weekly_expiry_resolver
    from engine.pricing.costs import ZERO_COST
    from engine.pricing.iv_surface import IVSurface
    from engine.strategy.condor import StrategyConfig

    expiry = dt.date(2026, 9, 29)
    spots = [(dt.datetime.combine(dt.date(2026, 9, 21) + dt.timedelta(days=d), dt.time(10, 0), tzinfo=IST),
              23_400.0 - 100 * d) for d in range(9)]
    spots.append((dt.datetime.combine(expiry, dt.time(15, 29, 59), tzinfo=IST), 23_120.0))
    params = BacktestParams(strategy=StrategyConfig(lots=1, lot_size=65, anchor_mode="floor", **kw),
                            costs=ZERO_COST)
    bt = Backtest(params, ModelPriceProvider(surface=IVSurface(atm_vol=0.12)),
                  weekly_expiry_resolver([expiry], max_dte=None))
    return bt.run(spots, settlement=settlement), expiry


def test_an_expiry_settles_at_the_official_close_not_the_last_bar():
    result, expiry = _replay({dt.date(2026, 9, 29): 23_050.0})
    for c in result.condors:
        for fl in c.legs:
            assert fl.exit_price == pytest.approx(fl.leg.intrinsic(23_050.0))
    assert not any("official NIFTY close" in w for w in result.warnings)


def test_an_expiry_with_no_official_close_says_so():
    result, _ = _replay({dt.date(2026, 9, 1): 1.0})       # closes, just not this expiry
    for c in result.condors:
        for fl in c.legs:
            assert fl.exit_price == pytest.approx(fl.leg.intrinsic(23_120.0))
    assert any("No official NIFTY close for expiry 29 Sep 2026" in w for w in result.warnings)


def test_the_model_prices_off_the_vix_known_then_not_the_days_close():
    from engine.backtest.providers import ModelPriceProvider, PriceRequest
    from engine.backtest.vix_series import VixAsOf
    from engine.pricing.iv_surface import IVSurface

    day = dt.date(2026, 3, 9)
    morning = dt.datetime.combine(day, dt.time(10, 2), tzinfo=IST)
    lookup = VixAsOf("5", bars=[(dt.datetime.combine(day, dt.time(9, 59, 58), tzinfo=IST), 14.0, "choice")],
                     daily=[(day, 24.0, "choice")])
    request = PriceRequest(expiry=dt.date(2026, 3, 30), strike=23_000.0, right="PE", when=morning, spot=23_500.0)
    as_of = ModelPriceProvider(surface=IVSurface(atm_vol=0.12), vix_at=lookup.at).quote(request)
    close = ModelPriceProvider(surface=IVSurface(atm_vol=0.12), vix_by_date={day: 24.0}).quote(request)
    assert as_of.iv < close.iv, "the morning's 14 was known at 10:02; the close of 24 was not"


def test_the_price_chain_is_choice_then_backup_then_model():
    from engine.backtest.providers import (
        CandlePriceProvider, FallbackPriceProvider, ModelPriceProvider, PriceRequest,
    )
    from engine.pricing.iv_surface import IVSurface

    exp, when = dt.date(2026, 9, 29), dt.datetime(2026, 9, 21, 10, 0, tzinfo=IST)
    bar = pd.DataFrame([{"ts": when - dt.timedelta(minutes=1), "close": 80.0}])
    choice, backup = CandlePriceProvider(), CandlePriceProvider(source=PriceSource.BACKUP)
    choice.add(exp, 23_000.0, "PE", bar)
    backup.add(exp, 23_000.0, "PE", bar.assign(close=81.0))
    backup.add(exp, 23_100.0, "PE", bar.assign(close=90.0))
    chain = FallbackPriceProvider(primary=choice, secondary=backup,
                                  fallback=ModelPriceProvider(surface=IVSurface(atm_vol=0.12)))
    ask = lambda k: chain.quote(PriceRequest(exp, k, "PE", when, 23_400.0))  # noqa: E731
    both, backup_only, neither = ask(23_000.0), ask(23_100.0), ask(23_200.0)
    assert (both.price, both.source) == (80.0, PriceSource.CHOICE)
    assert (backup_only.price, backup_only.source) == (90.0, PriceSource.BACKUP)
    assert neither.source == PriceSource.MODELED
    s = chain.summary()
    assert (s["choice_quotes"], s["backup_quotes"], s["modeled_quotes"]) == (1, 1, 1)
    assert s["real_quotes"] == 2 and s["real_fraction"] == pytest.approx(2 / 3)


# ========================================================= inside a job


def _spot_at_ten(days: int = 40) -> pd.DataFrame:
    from engine.tests.test_jobs import TODAY

    rows, day, n = [], TODAY - dt.timedelta(days=days), 0
    while day <= TODAY:
        if day.weekday() < 5:
            px = 24_000.0 - 12.0 * n
            rows.append({"ts": dt.datetime.combine(day, dt.time(10, 0), tzinfo=IST),
                         "open": px, "high": px, "low": px, "close": px, "volume": 1, "oi": 1})
            n += 1
        day += dt.timedelta(days=1)
    return pd.DataFrame(rows)


def _job_with_backup(monkeypatch, market, http, **params):
    from engine.backtest import jobs
    from engine.tests.test_jobs import run_job

    backup = client(http)
    monkeypatch.setattr(groww, "shared_backup", lambda: backup)
    days = sorted(set(market._spot["ts"].dt.date))
    market._vix = {d: 13.0 for d in days}
    monkeypatch.setattr(jobs, "expected_sessions", lambda start, end, now=None: set(days))
    return run_job(market=market, **{"option_resolution": "5", **params}), backup


def test_legs_choice_has_no_history_for_are_priced_from_the_backup(monkeypatch):
    from engine.tests.test_jobs import FakeMarket

    job, backup = _job_with_backup(monkeypatch, FakeMarket(spot=_spot_at_ten()), FakeHttp())
    assert job.status == "done", job.error
    prov = job.result["provenance"]
    assert prov["legs_backup"] > 0 and prov["premium_source"] == "backup"
    assert prov["backup"]["used"] and prov["verified"] is False
    assert prov["provider"]["backup_quotes"] > 0
    sources = {leg["source"] for c in job.result["condors"] for leg in c["legs"]}
    assert "backup" in sources
    # Choice's own split is unchanged: it still served nothing.
    assert prov["legs_real"] == 0 and prov["legs_empty"] == prov["legs_total"]


def test_choice_still_wins_where_it_has_the_leg(monkeypatch):
    from engine.tests.test_jobs import FakeMarket

    market = FakeMarket(spot=_spot_at_ten(), option_frames=True)
    job, _ = _job_with_backup(monkeypatch, market, FakeHttp())
    assert job.status == "done", job.error
    prov = job.result["provenance"]
    assert prov["legs_backup"] == 0 and prov["premium_source"] == "choice:ChartData"
    assert {leg["source"] for c in job.result["condors"] for leg in c["legs"]} == {"choice"}


def test_a_failing_backup_is_noted_without_naming_it(monkeypatch):
    from engine.tests.test_jobs import FakeMarket

    job, _ = _job_with_backup(monkeypatch, FakeMarket(spot=_spot_at_ten()), FakeHttp(candle_status=403))
    assert job.status == "done", job.error
    notes = job.result["provenance"]["backup"]["notes"]
    assert any("backup source failed" in n for n in notes)
    assert job.result["provenance"]["premium_source"] == "modeled:black76"


def test_nothing_a_backtest_sends_the_dashboard_names_another_company(monkeypatch):
    """The dashboard shows Choice and nothing else by name, even when the
    backup did the work or failed doing it."""
    from engine.tests.test_jobs import FakeMarket

    for http in (FakeHttp(), FakeHttp(candle_status=403), FakeHttp(token_status=401)):
        job, _ = _job_with_backup(monkeypatch, FakeMarket(spot=_spot_at_ten()), http)
        payload = json.dumps({"result": job.result, "public": job.public()}, default=str)
        assert not OTHER_VENDORS.search(payload), OTHER_VENDORS.search(payload)


def test_an_unconfigured_backup_is_noted_and_the_run_carries_on():
    from engine.tests.test_jobs import FakeMarket, run_job

    job = run_job(market=FakeMarket(spot=_spot_at_ten()))
    assert job.status == "done", job.error
    prov = job.result["provenance"]
    assert prov["backup"]["configured"] is False and prov["legs_backup"] == 0
    assert any("no backup source is configured" in n for n in prov["backup"]["notes"])


def test_the_dashboard_code_names_no_other_company():
    offenders = []
    for folder in ("app", "components", "lib"):
        for path in (REPO / "web" / folder).rglob("*"):
            if path.suffix in {".ts", ".tsx", ".js", ".jsx", ".css"} and path.is_file():
                text = path.read_text(encoding="utf-8", errors="ignore")
                for match in OTHER_VENDORS.finditer(text):
                    offenders.append(f"{path.relative_to(REPO)}: {match.group(0)}")
    assert offenders == []
