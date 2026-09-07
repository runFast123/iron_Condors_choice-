"""Tests for the hardened ChartData client.

These pin down the exact behaviours that were broken upstream: a bad date must
raise rather than become 1980, a broker error must raise rather than become an
empty DataFrame, long ranges must be chunked, and decimal volume must not kill
the batch.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from engine.choice.errors import ChoiceDateError, ChoiceHistoryError, ChoiceNoDataError
from engine.choice.history import (
    EPOCH_1980,
    HistoryClient,
    from_choice_epoch,
    max_span_days,
    normalise_resolution,
    to_choice_epoch,
    to_ist,
)
from engine.config import IST


class FakeSession:
    """Stands in for ChoiceSession; records payloads and replays scripted responses."""

    def __init__(self, responses):
        self._responses = list(responses) if isinstance(responses, list) else responses
        self.payloads = []

    def request(self, method, endpoint, data=None, **kw):
        self.payloads.append(data)
        if callable(self._responses):
            return self._responses(data)
        if isinstance(self._responses, list):
            return self._responses.pop(0) if self._responses else _ok([])
        return self._responses


def _ok(rows, divisor=1):
    return {"Status": "Success", "Response": {"lstChartHistory": rows, "PriceDivisor": divisor}}


def _bar(when: dt.datetime, o=100.0, h=101.0, l=99.0, c=100.5, v=1000, oi=50, divisor=1):
    secs = to_choice_epoch(when)
    return f"{secs},{o * divisor},{h * divisor},{l * divisor},{c * divisor},{v},{oi}"


def _client(responses) -> HistoryClient:
    return HistoryClient(FakeSession(responses))


# ----------------------------------------------------------------- date parsing


def test_to_ist_accepts_common_forms():
    expected = dt.datetime(2026, 3, 4, tzinfo=IST)
    for value in ["2026-03-04", "04-03-2026", "04/03/2026", dt.date(2026, 3, 4), pd.Timestamp("2026-03-04")]:
        assert to_ist(value) == expected, value


def test_to_ist_attaches_ist_to_naive_datetimes():
    assert to_ist(dt.datetime(2026, 3, 4, 9, 15)).tzinfo is IST


def test_bad_date_raises_instead_of_silently_becoming_1980():
    # The upstream bug: `except Exception: return 0` turned any unparseable
    # value into the 1980 epoch, so callers unknowingly requested 45 years.
    for bad in ["not-a-date", "", None, object()]:
        with pytest.raises(ChoiceDateError):
            to_ist(bad)


def test_epoch_roundtrip_is_lossless():
    when = dt.datetime(2026, 3, 4, 9, 15, tzinfo=IST)
    assert from_choice_epoch(to_choice_epoch(when)) == when


def test_epoch_matches_the_documented_1980_origin():
    assert to_choice_epoch(dt.datetime(1980, 1, 1, tzinfo=IST)) == 0
    assert to_choice_epoch(dt.datetime(1980, 1, 2, tzinfo=IST)) == 86_400


def test_epoch_offset_shifts_both_directions():
    when = dt.datetime(2026, 3, 4, 9, 15, tzinfo=IST)
    assert to_choice_epoch(when, 19_800) - to_choice_epoch(when, 0) == 19_800
    assert from_choice_epoch(to_choice_epoch(when, 19_800), 19_800) == when


# ------------------------------------------------------------------ resolution


def test_resolution_aliases_normalise():
    assert normalise_resolution("5m") == "5"
    assert normalise_resolution("1h") == "60"
    assert normalise_resolution("daily") == "D"
    assert normalise_resolution("15") == "15"


def test_unknown_resolution_raises():
    with pytest.raises(ChoiceDateError):
        normalise_resolution("fortnightly")


def test_intraday_spans_are_shorter_than_daily():
    assert max_span_days("1") < max_span_days("5") < max_span_days("D")


# --------------------------------------------------------- error propagation


def test_broker_error_raises_rather_than_returning_empty_frame():
    """The single most important fix.

    Upstream returned an empty DataFrame here, making a failed fetch
    indistinguishable from a holiday and silently corrupting a backtest.
    """
    client = _client({"Status": "Failure", "Message": "Invalid token"})
    with pytest.raises(ChoiceHistoryError) as excinfo:
        client.fetch_or_raise(2, 12345, "2026-03-01", "2026-03-02", "5")
    assert "Invalid token" in str(excinfo.value)


def test_broker_error_is_reported_not_swallowed_in_partial_mode():
    client = _client({"Status": "Failure", "Message": "Token not subscribed"})
    df, report = client.fetch(2, 12345, "2026-03-01", "2026-03-02", "5", allow_partial=True)
    assert df.empty
    assert report.status == "error"
    assert "Token not subscribed" in (report.error_message or "")
    assert report.windows_failed


def test_genuinely_empty_window_is_no_data_not_error():
    client = _client(_ok([]))
    df, report = client.fetch(2, 12345, "2026-03-01", "2026-03-02", "5")
    assert df.empty
    assert report.status == "no_data"
    with pytest.raises(ChoiceNoDataError):
        _client(_ok([])).fetch_or_raise(2, 12345, "2026-03-01", "2026-03-02", "5")


def test_null_response_body_does_not_crash():
    # kkunal would raise AttributeError on `.get` here.
    client = _client({"Status": "Success", "Response": None})
    df, report = client.fetch(2, 1, "2026-03-01", "2026-03-02", "5")
    assert df.empty and report.status == "no_data"


# ------------------------------------------------------------------- parsing


def test_decimal_volume_does_not_kill_the_batch():
    """Upstream `int(parts[5])` raised ValueError on "1234.0"."""
    when = dt.datetime(2026, 3, 4, 9, 15, tzinfo=IST)
    rows = [f"{to_choice_epoch(when)},100,101,99,100.5,1234.0,55.0"]
    df, report = _client(_ok(rows)).fetch(2, 1, "2026-03-04", "2026-03-04", "5")
    assert len(df) == 1
    assert df.iloc[0]["volume"] == 1234
    assert df.iloc[0]["oi"] == 55
    assert report.bad_rows == 0


def test_price_divisor_is_applied():
    when = dt.datetime(2026, 3, 4, 9, 15, tzinfo=IST)
    rows = [f"{to_choice_epoch(when)},10000,10100,9900,10050,10,1"]
    df, _ = _client(_ok(rows, divisor=100)).fetch(2, 1, "2026-03-04", "2026-03-04", "5")
    assert df.iloc[0]["close"] == pytest.approx(100.50)


def test_zero_divisor_is_treated_as_one_not_a_zero_division():
    when = dt.datetime(2026, 3, 4, 9, 15, tzinfo=IST)
    rows = [f"{to_choice_epoch(when)},100,101,99,100.5,10,1"]
    df, _ = _client(_ok(rows, divisor=0)).fetch(2, 1, "2026-03-04", "2026-03-04", "5")
    assert df.iloc[0]["close"] == pytest.approx(100.5)


def test_malformed_rows_are_counted_not_fatal():
    when = dt.datetime(2026, 3, 4, 9, 15, tzinfo=IST)
    rows = [_bar(when), "garbage", "1,2", _bar(when + dt.timedelta(minutes=5))]
    df, report = _client(_ok(rows)).fetch(2, 1, "2026-03-04", "2026-03-04", "5")
    assert len(df) == 2
    assert report.bad_rows == 2


def test_duplicate_timestamps_are_deduped_and_sorted():
    t0 = dt.datetime(2026, 3, 4, 9, 15, tzinfo=IST)
    t1 = t0 + dt.timedelta(minutes=5)
    rows = [_bar(t1), _bar(t0), _bar(t0)]
    df, _ = _client(_ok(rows)).fetch(2, 1, "2026-03-04", "2026-03-04", "5")
    assert len(df) == 2
    assert list(df["ts"]) == [t0, t1]


# ------------------------------------------------------------------ chunking


def test_long_range_is_split_into_multiple_requests():
    """Upstream made one unbounded request, which is why wide ranges came back empty."""
    session = FakeSession(lambda payload: _ok([]))
    client = HistoryClient(session)
    client.fetch(2, 1, "2026-01-01", "2026-06-30", "1")  # 180 days at a 7-day cap
    assert len(session.payloads) >= 20


def test_short_range_makes_a_single_request():
    session = FakeSession(lambda payload: _ok([]))
    HistoryClient(session).fetch(2, 1, "2026-03-01", "2026-03-02", "D")
    assert len(session.payloads) == 1


def test_payload_uses_the_documented_field_names():
    session = FakeSession(lambda payload: _ok([]))
    HistoryClient(session).fetch(2, 4242, "2026-03-01", "2026-03-02", "5")
    payload = session.payloads[0]
    assert set(payload) == {"SegmentId", "Token", "FromDate", "ToDate", "Interval"}
    assert payload["Token"] == 4242 and payload["SegmentId"] == 2
    assert payload["Interval"] == "5"
    assert isinstance(payload["FromDate"], int)


def test_start_after_end_is_rejected():
    with pytest.raises(ChoiceDateError):
        _client(_ok([])).fetch(2, 1, "2026-03-10", "2026-03-01", "5")


def test_failing_window_is_bisected_before_giving_up():
    calls = {"n": 0}

    def responder(payload):
        calls["n"] += 1
        return {"Status": "Failure", "Message": "range too wide"}

    session = FakeSession(responder)
    df, report = HistoryClient(session).fetch(2, 1, "2026-01-01", "2026-01-20", "D", allow_partial=True)
    assert df.empty and report.status == "error"
    # One failed window should provoke halving probes, not a single give-up.
    assert calls["n"] > 1
    assert report.requests_made == calls["n"]


def test_bisect_recovers_a_window_that_was_merely_too_wide():
    """A too-wide window should be rescued by halving, not reported as failed."""
    full_window_start = to_choice_epoch(dt.datetime(2026, 1, 1, tzinfo=IST))
    when = dt.datetime(2026, 1, 3, 9, 15, tzinfo=IST)

    def responder(payload):
        # Only the full-width first window fails; its halves succeed.
        if payload["FromDate"] == full_window_start and payload["ToDate"] > full_window_start + 4 * 86_400:
            return {"Status": "Failure", "Message": "range too wide"}
        return _ok([_bar(when)])

    df, report = HistoryClient(FakeSession(responder)).fetch(
        2, 1, "2026-01-01", "2026-02-20", "1", allow_partial=True
    )
    assert not df.empty
    assert report.status == "ok"
    assert report.windows_failed == []  # bisection recovered it


def test_partial_success_keeps_the_windows_that_worked():
    """A window that fails at every width is dropped, but the rest survive."""
    jan_8 = to_choice_epoch(dt.datetime(2026, 1, 8, tzinfo=IST))
    when = dt.datetime(2026, 1, 20, 9, 15, tzinfo=IST)

    def responder(payload):
        if payload["FromDate"] < jan_8:  # the entire first window, at any width
            return {"Status": "Failure", "Message": "nope"}
        return _ok([_bar(when)])

    df, report = HistoryClient(FakeSession(responder)).fetch(
        2, 1, "2026-01-01", "2026-02-20", "1", allow_partial=True
    )
    assert not df.empty
    assert report.status == "ok"
    assert report.windows_failed
    assert "nope" in report.windows_failed[0][2]
