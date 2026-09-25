"""The exchange's daily record, and the model anchored to it.

Three layers, each tested without a network:

* the archive: both file formats parse to one shape, a day is downloaded once,
  a holiday is remembered only once it is old enough to be sure, and a refusal
  stops the downloading without losing what is cached;
* the smile: read back exactly from a market built from a known one, and not
  bent by a thin or rogue print;
* the provider: prices from the previous session only, holds a price inside
  the day's real range, takes the real closing trade at the close, and falls
  back to the India VIX model where there is nothing to anchor to.
"""

from __future__ import annotations

import datetime as dt
import io
import math
import zipfile

import pandas as pd
import pytest

from engine.backtest.providers import (
    CandlePriceProvider,
    ExchangeAnchoredPriceProvider,
    FallbackPriceProvider,
    ModelPriceProvider,
    PriceRequest,
)
from engine.config import IST
from engine.data import nse_bhavcopy
from engine.data.nse_bhavcopy import (
    COLUMNS,
    BhavcopyArchive,
    ChainHistory,
    ExchangeDataUnavailable,
    legacy_url,
    parse_legacy,
    parse_udiff,
    udiff_url,
)
from engine.pricing.black76 import price as black76
from engine.pricing.exchange_smile import build_smile, carried_vol, years_to_expiry
from engine.pricing.iv_surface import from_vix
from engine.strategy.condor import PriceSource

RATE = 0.065
EXPIRY = dt.date(2026, 4, 28)
D0, D1, D2 = dt.date(2026, 4, 1), dt.date(2026, 4, 2), dt.date(2026, 4, 6)
STRIKES = range(21_000, 25_050, 50)


def true_iv(x: float) -> float:
    """The synthetic market's smile, in log-moneyness: a put skew with some curve."""
    return 0.16 - 0.30 * x + 1.8 * x * x


def market_day(day, spot, *, expiries=(EXPIRY,), carry=0.05, scale=1.0, volume=5_000,
               strikes=STRIKES, spread=0.10):
    """One day's file, priced from `true_iv` at the forward `carry` implies.

    Every contract closes, last-trades and settles at its Black-76 price;
    the day's low and high sit `spread` either side.
    """
    rows = []
    for expiry in expiries:
        years = years_to_expiry(dt.datetime.combine(day, dt.time(15, 30)), expiry)
        if years <= 0:
            continue
        forward = spot * math.exp(carry * years)
        for k in strikes:
            for right in ("CE", "PE"):
                iv = true_iv(math.log(k / forward)) * scale
                px = black76(forward, k, years, iv, RATE, right)
                rows.append(dict(kind="OPT", expiry=expiry, strike=float(k), right=right,
                                 open=px, high=px * (1 + spread), low=px * (1 - spread),
                                 close=px, last=px, settle=px, volume=volume, trades=50,
                                 oi=1_000, underlying=spot))
        rows.append(dict(kind="FUT", expiry=expiry, strike=0.0, right="", open=forward,
                         high=forward, low=forward, close=forward, last=forward, settle=forward,
                         volume=100, trades=10, oi=100, underlying=spot))
    return pd.DataFrame(rows, columns=COLUMNS)


def request(day, when, strike, right, spot, expiry=EXPIRY):
    return PriceRequest(expiry=expiry, strike=float(strike), right=right,
                        when=dt.datetime.combine(day, when, tzinfo=IST), spot=spot)


def vix_constant(level=16.0):
    return lambda _when: level


def anchored(history, *, vix=vix_constant(), **kw):
    model = ModelPriceProvider(surface=from_vix(16.0), vix_at=vix)
    return ExchangeAnchoredPriceProvider(history=history, fallback=model, vix_at=vix, **kw)


# ==================================================================== parsing

UDIFF_HEADER = [
    "TradDt", "BizDt", "Sgmt", "Src", "FinInstrmTp", "FinInstrmId", "ISIN", "TckrSymb", "SctySrs",
    "XpryDt", "FininstrmActlXpryDt", "StrkPric", "OptnTp", "FinInstrmNm", "OpnPric", "HghPric",
    "LwPric", "ClsPric", "LastPric", "PrvsClsgPric", "UndrlygPric", "SttlmPric", "OpnIntrst",
    "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd", "SsnId", "NewBrdLotQty",
    "Rmks", "Rsvd1", "Rsvd2", "Rsvd3", "Rsvd4",
]


def udiff_row(tp, symbol, expiry, strike, right, o, h, lo, c, last, settle, vol, und=22819.6,
              actual=None):
    row = dict.fromkeys(UDIFF_HEADER, "")
    row.update(TradDt="2026-03-27", FinInstrmTp=tp, TckrSymb=symbol, XpryDt=expiry,
               FininstrmActlXpryDt=actual or expiry, StrkPric=strike, OptnTp=right,
               OpnPric=o, HghPric=h, LwPric=lo, ClsPric=c, LastPric=last, UndrlygPric=und,
               SttlmPric=settle, OpnIntrst=1000, TtlTradgVol=vol, TtlNbOfTxsExctd=12)
    return row


def udiff_frame() -> pd.DataFrame:
    return pd.DataFrame([
        udiff_row("IDO", "NIFTY", "2026-03-30", 22800, "PE", 121.2, 214.5, 121.2, 203.05, 208.8, 203.05, 2516395),
        udiff_row("IDO", "NIFTY", "2026-03-30", 23400, "CE", 90.0, 95.0, 30.0, 41.6, 40.0, 41.6, 1500),
        # Did not trade: yesterday's close carried, a theoretical settlement.
        udiff_row("IDO", "NIFTY", "2026-04-07", 21750, "CE", 0, 0, 0, 2496.25, 2496.25, 1109.5, 0),
        udiff_row("IDF", "NIFTY", "2026-03-30", "", "", 22800, 22900, 22700, 22816.6, 22810, 22816.6, 165268),
        udiff_row("IDO", "BANKNIFTY", "2026-03-30", 52000, "CE", 1, 2, 1, 1.5, 1.5, 1.5, 10),
        udiff_row("IDO", "NIFTYNXT50", "2026-03-30", 60000, "PE", 1, 2, 1, 1.5, 1.5, 1.5, 10),
        # An expiry the exchange moved: the actual date is the one that counts.
        udiff_row("IDO", "NIFTY", "2026-04-14", 22000, "PE", 50, 60, 40, 55, 54, 55, 900,
                  actual="2026-04-13"),
    ])


def test_udiff_keeps_nifty_options_and_futures_only():
    out = parse_udiff(udiff_frame())
    assert list(out.columns) == COLUMNS
    assert set(out["kind"]) == {"OPT", "FUT"}
    assert len(out) == 5                      # BANKNIFTY and NIFTYNXT50 dropped
    put = out[(out["strike"] == 22800) & (out["right"] == "PE")].iloc[0]
    assert put["close"] == 203.05 and put["last"] == 208.8          # a close is not a last trade
    assert put["expiry"] == dt.date(2026, 3, 30)
    assert put["underlying"] == 22819.6 and put["volume"] == 2516395
    future = out[out["kind"] == "FUT"].iloc[0]
    assert future["right"] == "" and future["close"] == 22816.6


def test_udiff_prefers_the_actual_expiry_date():
    out = parse_udiff(udiff_frame())
    assert dt.date(2026, 4, 13) in set(out["expiry"])
    assert dt.date(2026, 4, 14) not in set(out["expiry"])


def test_legacy_file_parses_to_the_same_shape():
    raw = pd.DataFrame({
        "INSTRUMENT": ["OPTIDX", "FUTIDX", "OPTIDX", "OPTSTK"],
        "SYMBOL": ["NIFTY", "NIFTY", "BANKNIFTY", "RELIANCE"],
        "EXPIRY_DT": ["25-Jan-2024", "25-Jan-2024", "25-Jan-2024", "25-Jan-2024"],
        "STRIKE_PR": [21500.0, 0.0, 47000.0, 2500.0],
        "OPTION_TYP": ["PE", "XX", "CE", "CE"],
        "OPEN": [100.0, 21779.8, 1.0, 1.0], "HIGH": [120.0, 21831.0, 1.0, 1.0],
        "LOW": [90.0, 21677.0, 1.0, 1.0], "CLOSE": [110.0, 21793.85, 1.0, 1.0],
        "SETTLE_PR": [110.0, 21793.85, 1.0, 1.0], "CONTRACTS": [5000, 135030, 1, 1],
        "VAL_INLAKH": [0, 0, 0, 0], "OPEN_INT": [100, 11861300, 1, 1], "CHG_IN_OI": [0, 0, 0, 0],
        "TIMESTAMP": ["05-JAN-2024"] * 4, "Unnamed: 15": [None] * 4,
    })
    out = parse_legacy(raw)
    assert list(out.columns) == COLUMNS and len(out) == 2
    put = out[out["kind"] == "OPT"].iloc[0]
    assert put["expiry"] == dt.date(2024, 1, 25) and put["right"] == "PE"
    assert math.isnan(put["last"]) and math.isnan(put["underlying"])     # not in this format
    assert out[out["kind"] == "FUT"].iloc[0]["right"] == ""


def test_urls_follow_the_archive_layout():
    day = dt.date(2024, 1, 5)
    assert udiff_url(day).endswith("/content/fo/BhavCopy_NSE_FO_0_0_0_20240105_F_0000.csv.zip")
    assert legacy_url(day).endswith("/content/historical/DERIVATIVES/2024/JAN/fo05JAN2024bhav.csv.zip")


# ==================================================================== archive


class Response:
    def __init__(self, status: int, content: bytes = b"") -> None:
        self.status_code = status
        self.content = content


class FakeHttp:
    """URL -> Response; anything unknown is a 404 page, the way the archive answers."""

    def __init__(self, routes=None) -> None:
        self.routes = dict(routes or {})
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        assert headers and "Mozilla" in headers.get("User-Agent", ""), "the archive needs a browser agent"
        found = self.routes.get(url)
        return found if found is not None else Response(404, b"<html>404</html>")


def zipped(frame: pd.DataFrame, name="bhav.csv") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, frame.to_csv(index=False))
    return buffer.getvalue()


def make_archive(tmp_path, http, today=dt.date(2026, 9, 25)):
    return BhavcopyArchive(tmp_path, http=http, rate=1_000.0, today=lambda: today, attempts=2)


def test_a_day_is_downloaded_once_and_then_read_from_the_cache(tmp_path):
    day = dt.date(2026, 3, 27)
    http = FakeHttp({udiff_url(day): Response(200, zipped(udiff_frame()))})
    archive = make_archive(tmp_path, http)
    first = archive.day(day)
    again = archive.day(day)
    assert len(http.calls) == 1, "a published day never changes; ask once"
    assert archive.downloads == 1 and archive.cache_hits == 1
    pd.testing.assert_frame_equal(first.reset_index(drop=True), again.reset_index(drop=True),
                                  check_dtype=False)


def test_a_missing_file_is_a_holiday_only_once_the_day_is_old(tmp_path):
    http = FakeHttp()
    archive = make_archive(tmp_path, http, today=dt.date(2026, 9, 25))
    assert archive.day(dt.date(2026, 3, 3)) is None             # Holi: long past
    assert archive.day(dt.date(2026, 3, 3)) is None
    assert len(http.calls) == 1, "an old day with no file is remembered as closed"

    assert archive.day(dt.date(2026, 9, 24)) is None            # yesterday: maybe just late
    assert archive.day(dt.date(2026, 9, 24)) is None
    assert len(http.calls) == 3, "a recent missing file must be asked for again"


def test_a_future_day_is_never_requested(tmp_path):
    http = FakeHttp()
    archive = make_archive(tmp_path, http, today=dt.date(2026, 9, 25))
    assert archive.day(dt.date(2026, 9, 28)) is None
    assert http.calls == []


def test_a_page_instead_of_a_zip_is_not_a_file(tmp_path):
    day = dt.date(2026, 3, 27)
    http = FakeHttp({udiff_url(day): Response(200, b"<html>maintenance</html>")})
    assert make_archive(tmp_path, http).day(day) is None


def test_an_old_day_falls_back_to_the_legacy_file(tmp_path):
    day = dt.date(2024, 1, 5)
    legacy = pd.DataFrame({
        "INSTRUMENT": ["OPTIDX"], "SYMBOL": ["NIFTY"], "EXPIRY_DT": ["25-Jan-2024"],
        "STRIKE_PR": [21500.0], "OPTION_TYP": ["PE"], "OPEN": [100.0], "HIGH": [120.0],
        "LOW": [90.0], "CLOSE": [110.0], "SETTLE_PR": [110.0], "CONTRACTS": [5000],
        "VAL_INLAKH": [0], "OPEN_INT": [100], "CHG_IN_OI": [0], "TIMESTAMP": ["05-JAN-2024"],
    })
    http = FakeHttp({legacy_url(day): Response(200, zipped(legacy))})
    out = make_archive(tmp_path, http).day(day)
    assert http.calls == [udiff_url(day), legacy_url(day)]
    assert len(out) == 1 and out.iloc[0]["close"] == 110.0


def test_a_recent_holiday_is_not_asked_for_in_the_legacy_format(tmp_path):
    http = FakeHttp()
    make_archive(tmp_path, http).day(dt.date(2026, 3, 3))
    assert http.calls == [udiff_url(dt.date(2026, 3, 3))]


def test_a_refusal_stops_downloading_but_keeps_what_is_cached(tmp_path):
    cached_day, refused_day, later_day = dt.date(2026, 3, 26), dt.date(2026, 3, 27), dt.date(2026, 3, 30)
    make_archive(tmp_path, FakeHttp({udiff_url(cached_day): Response(200, zipped(udiff_frame()))})).day(cached_day)

    http = FakeHttp({udiff_url(refused_day): Response(403, b"denied"),
                     udiff_url(later_day): Response(200, zipped(udiff_frame()))})
    archive = make_archive(tmp_path, http)
    frames, note = archive.load([cached_day, refused_day, later_day])
    assert cached_day in frames, "the cache is still used after a refusal"
    assert later_day not in frames and udiff_url(later_day) not in http.calls
    assert note and "refused" in note and "NSE" not in note     # user-facing, names no source


def test_a_refusal_is_an_error_not_a_holiday(tmp_path):
    day = dt.date(2026, 3, 27)
    archive = make_archive(tmp_path, FakeHttp({udiff_url(day): Response(403)}))
    with pytest.raises(ExchangeDataUnavailable):
        archive.day(day)
    assert not archive.is_cached(day), "a refused day must not be remembered as closed"


def test_the_shared_archive_can_be_switched_off(monkeypatch):
    monkeypatch.undo()        # drop conftest's stand-in to test the real switch
    monkeypatch.setenv(nse_bhavcopy.ENV_SWITCH, "off")
    assert nse_bhavcopy.shared_archive() is None


# ============================================================== chain history


def test_history_answers_by_previous_session_and_contract():
    frames = {D0: market_day(D0, 23_000), D1: market_day(D1, 23_100)}
    history = ChainHistory(frames, expiries=[EXPIRY])
    assert history.previous_day(D1) == D0
    assert history.previous_day(D0) is None
    assert history.previous_day(D2) == D1
    assert history.has_day(D1) and not history.has_day(D2)
    assert history.underlying(D1) == 23_100
    assert history.contract(D1, EXPIRY, 23_000, "PE").traded
    assert history.future(D0, EXPIRY) > 23_000


def test_a_contract_nobody_traded_is_not_evidence():
    frame = market_day(D0, 23_000)
    frame.loc[(frame["strike"] == 22_000) & (frame["right"] == "PE"), "volume"] = 0
    history = ChainHistory({D0: frame})
    assert not history.contract(D0, EXPIRY, 22_000, "PE").traded


def test_history_keeps_only_the_expiries_asked_for():
    other = dt.date(2026, 5, 26)
    frames = {D0: market_day(D0, 23_000, expiries=(EXPIRY, other))}
    history = ChainHistory(frames, expiries=[EXPIRY])
    assert history.chain(D0, other) == []
    assert history.chain(D0, EXPIRY)


# ====================================================================== smile


def smile_of(frame, day=D0, vix=16.0):
    history = ChainHistory({day: frame})
    return build_smile(history.chain(day, EXPIRY), day, EXPIRY, history.underlying(day),
                       future=history.future(day, EXPIRY), vix=vix, rate=RATE)


def test_the_smile_reads_back_the_market_it_came_from():
    smile = smile_of(market_day(D0, 23_000, carry=0.05))
    true_forward = 23_000 * math.exp(0.05 * smile.years)
    assert smile.forward_source == "parity"
    assert smile.forward == pytest.approx(true_forward, abs=0.05)
    for strike in (22_000, 22_600, 23_000, 23_400, 24_000):
        assert smile.own[float(strike)] == pytest.approx(true_iv(math.log(strike / true_forward)), abs=1e-4)
        assert smile.fitted(strike) == pytest.approx(true_iv(math.log(strike / true_forward)), abs=2e-3)


def test_a_thin_print_never_reaches_the_smile():
    frame = market_day(D0, 23_000)
    thin = (frame["strike"] == 22_100) & (frame["right"] == "PE")
    frame.loc[thin, "close"] = frame.loc[thin, "close"] * 0.4        # ten vol points off
    frame.loc[thin, "volume"] = 40                                   # and barely traded
    smile = smile_of(frame)
    assert 22_100.0 not in smile.own
    clean = smile_of(market_day(D0, 23_000))
    assert smile.fitted(22_100) == pytest.approx(clean.fitted(22_100), abs=1e-3)


def test_a_rogue_print_is_dropped_from_the_fit():
    frame = market_day(D0, 23_000)
    rogue = (frame["strike"] == 22_300) & (frame["right"] == "PE")
    frame.loc[rogue, "close"] = frame.loc[rogue, "close"] * 0.5      # heavily traded, far off
    smile = smile_of(frame)
    assert 22_300.0 not in smile.own, "an outlier must not be used as the strike's own vol"
    forward = smile.forward
    assert smile.fitted(22_300) == pytest.approx(true_iv(math.log(22_300 / forward)), abs=3e-3)


def test_no_smile_without_enough_traded_strikes():
    frame = market_day(D0, 23_000, strikes=range(22_900, 23_150, 50))   # five strikes
    assert smile_of(frame) is None


def test_carried_vol_scales_with_vix_and_follows_the_smile():
    smile = smile_of(market_day(D0, 23_000), vix=16.0)
    strike = 22_600.0
    own = smile.own[strike]
    assert carried_vol(smile, strike, smile.forward, 19.2) == pytest.approx(own * 1.2, rel=1e-9)
    # NIFTY 1% higher: the strike sits further out of the money, higher up the put skew.
    higher = smile.forward * 1.01
    carried = carried_vol(smile, strike, higher, 16.0)
    assert carried > own
    assert carried == pytest.approx(true_iv(math.log(strike / higher)), abs=3e-3)


# =================================================================== provider


def two_days(spot0=23_000, spot1=23_050, scale1=1.0, **kw):
    return ChainHistory(
        {D0: market_day(D0, spot0, **kw), D1: market_day(D1, spot1, scale=scale1, **kw)},
        expiries=[EXPIRY],
    )


def test_a_price_comes_from_the_previous_session_only():
    base = anchored(two_days(spread=0.99))
    price = base.quote(request(D1, dt.time(10, 0), 22_600, "PE", 23_050)).price

    # Anything about D1 itself -- its closes, last trades -- must not move a
    # 10:00 price, except through the day's range, held wide here.
    rewritten = market_day(D1, 23_050, spread=0.99)
    for col in ("close", "last", "settle", "open"):
        rewritten[col] = rewritten[col] * 1.7
    later = anchored(ChainHistory({D0: market_day(D0, 23_000), D1: rewritten}, expiries=[EXPIRY]))
    assert later.quote(request(D1, dt.time(10, 0), 22_600, "PE", 23_050)).price == pytest.approx(price)

    # The previous session is what it reads.
    richer = anchored(ChainHistory({D0: market_day(D0, 23_000, scale=1.3),
                                    D1: market_day(D1, 23_050, spread=0.99)}, expiries=[EXPIRY]))
    assert richer.quote(request(D1, dt.time(10, 0), 22_600, "PE", 23_050)).price > price * 1.1


def test_an_anchored_price_matches_a_market_that_kept_its_smile():
    provider = anchored(two_days(spread=0.99))
    quote = provider.quote(request(D1, dt.time(11, 0), 22_600, "PE", 23_050))
    when = dt.datetime.combine(D1, dt.time(11, 0), tzinfo=IST)
    years = years_to_expiry(when, EXPIRY)
    forward = 23_050 * math.exp(0.05 * years)
    truth = black76(forward, 22_600, years, true_iv(math.log(22_600 / forward)), RATE, "PE")
    assert quote.source is PriceSource.MODELED
    assert quote.price == pytest.approx(truth, rel=0.01)
    assert provider.anchored == 1 and provider.vix_only == 0


def test_a_modelled_price_is_held_inside_the_days_real_range():
    history = two_days(scale1=1.0)
    provider = anchored(history, vix=lambda when: 16.0 if when.date() == D0 else 32.0)
    contract = history.contract(D1, EXPIRY, 22_600.0, "PE")
    quote = provider.quote(request(D1, dt.time(10, 0), 22_600, "PE", 23_050))
    # VIX doubled, the market did not: the model runs far above anything traded.
    assert quote.price == pytest.approx(contract.high)
    assert provider.clamped == 1


def test_the_closing_bar_takes_the_real_last_trade():
    history = two_days()
    provider = anchored(history)
    contract = history.contract(D1, EXPIRY, 22_600.0, "PE")
    at_close = provider.quote(request(D1, dt.time(15, 29, 59), 22_600, "PE", 23_050))
    assert at_close.source is PriceSource.EXCHANGE and at_close.price == contract.last
    before = provider.quote(request(D1, dt.time(15, 28, 59), 22_600, "PE", 23_050))
    assert before.source is PriceSource.MODELED


def test_no_closing_trade_for_a_contract_that_did_not_trade():
    frame = market_day(D1, 23_050)
    frame.loc[(frame["strike"] == 22_600) & (frame["right"] == "PE"), "volume"] = 0
    provider = anchored(ChainHistory({D0: market_day(D0, 23_000), D1: frame}, expiries=[EXPIRY]))
    quote = provider.quote(request(D1, dt.time(15, 29, 59), 22_600, "PE", 23_050))
    assert quote.source is PriceSource.MODELED and provider.clamped == 0


def test_no_closing_trade_where_the_file_has_none():
    frame = market_day(D1, 23_050)
    frame["last"] = math.nan                  # the legacy format carries no last trade
    provider = anchored(ChainHistory({D0: market_day(D0, 23_000), D1: frame}, expiries=[EXPIRY]))
    quote = provider.quote(request(D1, dt.time(15, 29, 59), 22_600, "PE", 23_050))
    assert quote.source is PriceSource.MODELED


def test_a_daily_bar_takes_the_days_close():
    history = two_days()
    provider = anchored(history, daily_bars=True)
    quote = provider.quote(request(D1, dt.time(0, 0), 22_600, "PE", 23_050))
    assert quote.source is PriceSource.EXCHANGE
    assert quote.price == history.contract(D1, EXPIRY, 22_600.0, "PE").close


def test_without_a_previous_session_the_vix_model_prices_it():
    history = two_days(spread=0.99)
    provider = anchored(history)
    plain = ModelPriceProvider(surface=from_vix(16.0), vix_at=vix_constant())
    req = request(D0, dt.time(10, 0), 22_600, "PE", 23_000)
    assert provider.quote(req).price == pytest.approx(plain.quote(req).price)
    assert provider.vix_only == 1 and provider.anchored == 0


def test_a_stale_anchor_is_not_used():
    history = ChainHistory({D0: market_day(D0, 23_000)}, expiries=[EXPIRY])
    provider = anchored(history)
    provider.quote(request(D0 + dt.timedelta(days=12), dt.time(10, 0), 22_600, "PE", 23_000))
    assert provider.vix_only == 1, "a record twelve days old says nothing about today"


def test_the_fallback_counts_a_closing_trade_as_real():
    provider = FallbackPriceProvider(primary=CandlePriceProvider(), fallback=anchored(two_days()))
    provider.quote(request(D1, dt.time(15, 29, 59), 22_600, "PE", 23_050))
    provider.quote(request(D1, dt.time(10, 0), 22_600, "PE", 23_050))
    summary = provider.summary()
    assert summary["exchange_quotes"] == 1 and summary["modeled_quotes"] == 1
    assert summary["real_quotes"] == 1 and summary["real_fraction"] == 0.5
    assert summary["anchored_quotes"] == 1 and summary["anchored_fraction"] == 1.0


def test_accuracy_is_measured_on_the_runs_own_legs():
    days = [dt.date(2026, 4, n) for n in (1, 2, 6, 7, 8)]
    history = ChainHistory({d: market_day(d, 23_000 + 40 * i) for i, d in enumerate(days)},
                           expiries=[EXPIRY])
    provider = anchored(history, vix=vix_constant(16.0))
    report = provider.measure([(EXPIRY, 22_600.0, "PE", days[0]), (EXPIRY, 23_400.0, "CE", days[0])],
                              last_day=days[-1])
    assert report["contracts"] == 2 and report["checks"] == 8     # four anchored days each
    # The synthetic market keeps its smile, so the anchored model is exact...
    assert report["anchored"]["median_abs"] < 0.01
    # ...and the VIX model, with its assumed skew, is not.
    assert report["vix_model"]["median_abs"] > report["anchored"]["median_abs"]


def test_accuracy_is_none_with_nothing_to_compare():
    provider = anchored(ChainHistory({D0: market_day(D0, 23_000)}, expiries=[EXPIRY]))
    assert provider.measure([(EXPIRY, 22_600.0, "PE", D0)], last_day=D0) is None


# ================================================================ in the job


class FakeArchive:
    def __init__(self, frames, note=None):
        self.frames = frames
        self.note = note
        self.downloads = 0
        self.asked: list[dt.date] = []

    def load(self, days, progress=None):
        days = list(days)
        self.asked = days
        return {d: f for d, f in self.frames.items() if d in set(days)}, self.note

    def is_cached(self, day):
        return True


def job_with_archive(monkeypatch, archive):
    from engine.backtest.jobs import BacktestJob, BacktestRunner
    from engine.data.expiry_calendar import MAX_WEEKLY_DTE, expiry_calendar
    from engine.tests.test_jobs import FakeMarket, params

    market = FakeMarket()
    spot = market._spot
    first, last = spot["ts"].min().date(), spot["ts"].max().date()
    expiries, _ = expiry_calendar(first, last + dt.timedelta(days=MAX_WEEKLY_DTE),
                                  market.master.expiries("NIFTY"), cadence="weekly")
    if archive is not None and archive.frames == "build":
        closes = {row.ts.date(): float(row.close) for row in spot.itertuples()}
        frames = {}
        day = first - dt.timedelta(days=10)
        level = float(spot["close"].iloc[0])
        while day <= last:
            if day.weekday() < 5:
                level = closes.get(day, level)
                live = [e for e in expiries if 0 < (e - day).days <= 60]
                frames[day] = market_day(day, level, expiries=live, strikes=range(21_500, 25_550, 100))
            day += dt.timedelta(days=1)
        archive.frames = frames
    monkeypatch.setattr(nse_bhavcopy, "shared_archive", lambda: archive)
    job = BacktestJob(job_id="x-1", user_id="u1", params=params())
    BacktestRunner(market, job).run()
    return job


def test_a_daily_run_prices_its_legs_at_the_exchanges_closes(monkeypatch):
    job = job_with_archive(monkeypatch, FakeArchive("build"))
    assert job.status == "done", job.error
    prov = job.result["provenance"]
    ex = prov["exchange"]
    assert ex["used"] and ex["days"] > 0
    assert prov["provider"]["exchange_quotes"] > 0
    assert "exchange:closing-trades" in prov["premium_source"]
    assert prov["verified"] is False, "closing trades from the exchange are not Choice prices"
    sources = {leg["source"] for c in job.result["condors"] for leg in c["legs"]}
    assert "exchange" in sources


def test_the_run_reads_from_before_its_first_bar(monkeypatch):
    archive = FakeArchive("build")
    job = job_with_archive(monkeypatch, archive)
    assert job.status == "done", job.error
    first_bar = min(dt.date.fromisoformat(c["entry_time"][:10]) for c in job.result["condors"])
    assert min(archive.asked) <= first_bar - dt.timedelta(days=7)


def test_a_refused_archive_costs_accuracy_not_the_run(monkeypatch):
    job = job_with_archive(monkeypatch, FakeArchive({}, note="the exchange's archive refused the request (HTTP 403)"))
    assert job.status == "done", job.error
    prov = job.result["provenance"]
    assert prov["exchange"]["used"] is False
    assert prov["premium_source"] == "modeled:black76"
    assert any("refused" in w for w in job.result["warnings"])


def test_with_the_archive_off_nothing_changes(monkeypatch):
    job = job_with_archive(monkeypatch, None)
    assert job.status == "done", job.error
    prov = job.result["provenance"]
    assert prov["exchange"]["configured"] is False and prov["exchange"]["accuracy"] is None
    assert prov["premium_source"] == "modeled:black76"


def test_a_run_on_the_exchanges_record_names_no_source(monkeypatch):
    import json
    import re

    job = job_with_archive(monkeypatch, FakeArchive("build"))
    assert job.status == "done", job.error
    payload = json.dumps({"result": job.result, "public": job.public()}, default=str)
    found = re.search(r"nse|nseindia|bhavcopy|groww|yahoo|dhan", payload, re.IGNORECASE)
    assert found is None, found
