"""nifty.db kept current: rows in the delivered format, no duplicates, and an
evening job that never logs in."""

from __future__ import annotations

import datetime as dt

import duckdb
import pandas as pd
import pytest

from engine.config import IST
from engine.data import history_job, history_store as hs

EXPIRY = dt.date(2026, 9, 29)


def choice_frame(day=dt.date(2026, 9, 11), minutes=3, price=240.0):
    """Choice's shape: stamped with each bar's last trade, IST."""
    ts = [dt.datetime.combine(day, dt.time(9, 15 + i, 59), tzinfo=IST) for i in range(minutes)]
    return pd.DataFrame({"ts": pd.to_datetime(ts), "open": price, "high": price + 1, "low": price - 1,
                         "close": [price + i for i in range(minutes)], "volume": 65 * 100, "oi": 1000.0})


def test_symbols_follow_the_delivered_naming():
    assert hs.symbol_for(EXPIRY, 23300.0, "CE") == "NIFTY29SEP2623300CE"
    assert hs.symbol_for(dt.date(2026, 10, 6), 22950, "PE") == "NIFTY06OCT2622950PE"


def test_rows_are_stamped_at_the_minute_they_start():
    rows = hs.option_rows(choice_frame(), EXPIRY, 23300.0, "CE")
    assert list(rows["datetime"]) == ["2026-09-11 09:15:00", "2026-09-11 09:16:00", "2026-09-11 09:17:00"]
    assert set(rows["exp"]) == {"2026-09-29 00:00:00"} and set(rows["type"]) == {"CE"}
    assert list(rows.columns)[:12] == ["datetime", "open", "high", "low", "close", "volume", "oi",
                                       "name", "symbol", "exp", "strike", "type"]


def test_the_same_day_twice_is_stored_once(tmp_path):
    path = tmp_path / "h.duckdb"
    rows = hs.option_rows(choice_frame(), EXPIRY, 23300.0, "CE")
    hs.append_options(rows, path)
    hs.append_options(rows.assign(close=rows["close"] + 5), path)      # a re-run, corrected
    con = duckdb.connect(str(path), read_only=True)
    stored = con.execute("SELECT datetime, close, source FROM choice_bars ORDER BY datetime").fetchall()
    con.close()
    assert len(stored) == 3 and stored[0][1] == 245.0 and stored[0][2] == "choice"


def test_the_delivered_table_is_never_touched(tmp_path):
    path = tmp_path / "h.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE dtable (datetime VARCHAR, close DOUBLE)")
    con.execute("INSERT INTO dtable VALUES ('2026-09-11 09:15:00', 242.25)")
    con.close()
    hs.append_options(hs.option_rows(choice_frame(), EXPIRY, 23300.0, "CE"), path)
    con = duckdb.connect(str(path), read_only=True)
    assert con.execute("SELECT count(*), max(close) FROM dtable").fetchone() == (1, 242.25)
    con.close()
    info = hs.coverage(path)
    assert info["delivered_until"] == "2026-09-11 09:15:00" and info["choice_days"] == ["2026-09-11"]


class Master:
    def expiries(self, underlying):
        return [dt.date(2026, 9, 22), EXPIRY, dt.date(2026, 10, 6)]

    def strikes(self, underlying, expiry, right):
        return [float(k) for k in range(15_000, 33_001, 50)]


def test_the_plan_is_near_the_money_and_nearest_expiry_first():
    legs = hs.plan(Master(), 23_000.0, dt.date(2026, 9, 23))
    assert {leg.expiry for leg in legs} == {EXPIRY, dt.date(2026, 10, 6)}, "an expired one is not asked for"
    assert legs[0].expiry == EXPIRY
    assert all(23_000 * 0.85 <= leg.strike <= 23_000 * 1.15 for leg in legs)


# ------------------------------------------------------------------ the job


class Choice:
    def __init__(self, login_date, rejected=None):
        self._login_date, self.session_id, self.rejected_since = login_date, "sess", rejected
        self.logins = 0

    def login(self, *a, **k):
        self.logins += 1
        raise AssertionError("the history job must never log in")


class Market:
    master = Master()

    def __init__(self):
        self.asked = []

    def nifty(self, start, end, resolution="D", strict=True):
        return pd.DataFrame({"close": [23_000.0]})

    def option_candles(self, underlying, expiry, strike, right, start, end, resolution, instruments=None):
        self.asked.append((expiry, strike, right, start, end))
        return choice_frame(day=end)

    def index_candles(self, name, start, end, resolution, strict=True):
        return choice_frame(day=end, price=23_000.0)


class Session:
    def __init__(self, choice, market):
        self.choice, self.market, self.user_id = choice, market, "u1"


class Registry:
    def __init__(self, *sessions):
        import threading

        self._lock = threading.RLock()
        self._sessions = {f"t{i}": s for i, s in enumerate(sessions)}


def evening(day=dt.date(2026, 9, 29)):
    return dt.datetime.combine(day, dt.time(16, 5), tzinfo=IST)


def test_the_job_collects_the_day_on_a_session_opened_today(monkeypatch):
    market = Market()
    today = evening().date()
    monkeypatch.setattr(history_job, "_last_collected", lambda: today - dt.timedelta(days=1))
    summary = history_job.run_once(Registry(Session(Choice(today), market)), now=evening())
    assert summary and summary["from"] == summary["to"] == "2026-09-29"
    assert summary["rows"] > 0 and market.asked


def test_the_job_never_logs_in(monkeypatch):
    """Only a session opened today can be used; yesterday's would need a
    login, which texts someone an OTP. It waits instead."""
    stale = Choice(dt.date(2026, 9, 28))
    monkeypatch.setattr(history_job, "_last_collected", lambda: dt.date(2026, 9, 28))
    assert history_job.run_once(Registry(Session(stale, Market())), now=evening()) is None
    assert stale.logins == 0


def test_the_job_waits_for_the_close_and_skips_holidays(monkeypatch):
    monkeypatch.setattr(history_job, "_last_collected", lambda: None)
    reg = Registry()
    assert history_job.run_once(reg, now=dt.datetime(2026, 9, 29, 15, 0, tzinfo=IST)) is None
    assert history_job.run_once(reg, now=dt.datetime(2026, 9, 27, 17, 0, tzinfo=IST)) is None   # Sunday


def test_missed_days_are_caught_up(monkeypatch):
    market = Market()
    today = evening().date()
    monkeypatch.setattr(history_job, "_last_collected", lambda: dt.date(2026, 9, 24))
    summary = history_job.run_once(Registry(Session(Choice(today), market)), now=evening())
    assert summary["from"] == "2026-09-25" and summary["to"] == "2026-09-29"


@pytest.mark.parametrize("value", ["off", "0", "false"])
def test_it_can_be_switched_off(monkeypatch, value):
    monkeypatch.setenv(history_job.ENV_SWITCH, value)
    assert history_job._enabled() is False


def test_a_day_with_no_bars_at_all_is_asked_once(monkeypatch):
    """An unlisted holiday: the first evening pass finds nothing, and the job
    does not spend the rest of the evening asking again."""
    class Closed(Market):
        def option_candles(self, *a, **k):
            self.asked.append(a)
            return pd.DataFrame()

        def index_candles(self, *a, **k):
            return pd.DataFrame()

    market = Closed()
    today = evening().date()
    monkeypatch.setattr(history_job, "_empty_days", set())
    monkeypatch.setattr(history_job, "_last_collected", lambda: today - dt.timedelta(days=1))
    reg = Registry(Session(Choice(today), market))
    assert history_job.run_once(reg, now=evening())["rows"] == 0
    asked = len(market.asked)
    assert history_job.run_once(reg, now=evening()) is None and len(market.asked) == asked


def test_known_2026_closures_are_not_trading_days():
    from engine.data.market_calendar import MarketCalendar

    calendar = MarketCalendar.load()
    for day in (dt.date(2026, 9, 14), dt.date(2026, 10, 20), dt.date(2026, 11, 24)):
        assert not calendar.is_trading_day(day)
    assert calendar.is_trading_day(dt.date(2026, 10, 19))


# ------------------------------------------------------- read by a backtest


def delivered(path, symbol="NIFTY29SEP2623300PE", day="2026-09-10", closes=(100.0, 101.0, 102.0),
              first="09:15"):
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE IF NOT EXISTS dtable (datetime VARCHAR, open DOUBLE, high DOUBLE,
                   low DOUBLE, close DOUBLE, volume INTEGER, oi DOUBLE, name VARCHAR, symbol VARCHAR,
                   exp VARCHAR, strike DOUBLE, type VARCHAR)""")
    start = dt.datetime.fromisoformat(f"{day} {first}")
    for i, close in enumerate(closes):
        stamp = (start + dt.timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
        con.execute("INSERT INTO dtable VALUES (?, ?, ?, ?, ?, 650, 1, 'NIFTY', ?, '2026-09-29 00:00:00', 23300, ?)",
                    [stamp, close, close, close, close, symbol, symbol[-2:]])
    con.close()


def test_a_backtest_reads_bars_stamped_like_choices(tmp_path):
    path = tmp_path / "h.duckdb"
    delivered(path)
    bars = hs.option_bars([(EXPIRY, 23300.0, "PE", dt.date(2026, 9, 1))], dt.date(2026, 9, 28), path=path)
    frame = bars[(EXPIRY, 23300.0, "PE")]
    # The 09:15 bar is known at 09:15:59, as Choice stamps it.
    assert frame["ts"].iloc[0] == pd.Timestamp("2026-09-10 09:15:59", tz="Asia/Kolkata")
    assert list(frame["close"]) == [100.0, 101.0, 102.0]


def test_the_closing_session_is_not_a_price_a_strategy_could_trade(tmp_path):
    path = tmp_path / "h.duckdb"
    delivered(path, closes=(90.0, 91.0, 92.0), first="15:28")
    frame = hs.option_bars([(EXPIRY, 23300.0, "PE", dt.date(2026, 9, 1))], dt.date(2026, 9, 28),
                           path=path)[(EXPIRY, 23300.0, "PE")]
    assert list(frame["close"]) == [90.0, 91.0]                  # 15:28 and 15:29; not 15:30


def test_choices_rows_extend_the_delivered_data_without_overlapping(tmp_path):
    path = tmp_path / "h.duckdb"
    delivered(path, day="2026-09-11")
    # Choice's copy of 11 Sep differs and must not replace the delivered one;
    # its 15 Sep is new.
    hs.append_options(hs.option_rows(choice_frame(day=dt.date(2026, 9, 11), price=500.0), EXPIRY, 23300.0, "PE"), path)
    hs.append_options(hs.option_rows(choice_frame(day=dt.date(2026, 9, 15), price=120.0), EXPIRY, 23300.0, "PE"), path)
    frame = hs.option_bars([(EXPIRY, 23300.0, "PE", dt.date(2026, 9, 1))], dt.date(2026, 9, 28),
                           path=path)[(EXPIRY, 23300.0, "PE")]
    by_day = frame.groupby(frame["ts"].dt.date)["close"].first()
    assert by_day[dt.date(2026, 9, 11)] == 100.0 and by_day[dt.date(2026, 9, 15)] == 120.0


def test_each_leg_starts_on_its_own_day(tmp_path):
    path = tmp_path / "h.duckdb"
    delivered(path, day="2026-09-08", closes=(70.0,))
    delivered(path, day="2026-09-10", closes=(80.0,))
    frame = hs.option_bars([(EXPIRY, 23300.0, "PE", dt.date(2026, 9, 9))], dt.date(2026, 9, 28),
                           path=path)[(EXPIRY, 23300.0, "PE")]
    assert list(frame["close"]) == [80.0]


def test_no_history_file_is_said_plainly(tmp_path):
    with pytest.raises(hs.HistoryUnavailable, match="no recorded history"):
        hs.option_bars([(EXPIRY, 23300.0, "PE", dt.date(2026, 9, 1))], dt.date(2026, 9, 28),
                       path=tmp_path / "absent.duckdb")
