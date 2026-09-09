"""Tests for the forward runner's log and quote handling.

These cover the paths that only execute against a live feed, which is exactly
where two crashes hid: emit() colliding with a `level=` detail kwarg, and the
touchline payload shape.
"""

from __future__ import annotations

import threading

import pytest

from engine.choice.errors import ChoiceError
from engine.data.market import TOUCHLINE_FORMATS, ChoiceMarketData
from engine.choice.instruments import Contract


def _contract(token: int, segment: int = 2) -> Contract:
    return Contract(token=token, segment_id=segment, symbol="NIFTY", description="", lot_size=65)


# Two contracts, so every candidate format produces a distinguishable string.
# With one contract "1,26000" is ambiguous between the pipe- and comma-joined
# shapes, which made the fixture -- not the code -- the thing under test.
PAIR = [_contract(26000, 1), _contract(42632, 2)]


def _shape_name(payload: str) -> str:
    if "@" in payload:
        return "segment@token,"
    if "|" in payload:
        return "segment,token|"
    parts = payload.split(",")
    return "segment,token," if len(parts) == 4 else "token,"


class _Session:
    """Accepts exactly one payload shape, like the real endpoint."""

    def __init__(self, accepts: str | None, rows=None):
        self.accepts = accepts
        self.rows = rows if rows is not None else [{"Token": 26000, "LTP": 24_000}]
        self.seen: list[str] = []

    def request(self, method, endpoint, data=None, **kw):
        payload = data["MultipleSegToken"]
        self.seen.append(payload)
        if self.accepts and _shape_name(payload) == self.accepts:
            return {"Status": "Success", "Response": self.rows}
        return {"Status": "Success", "Response": []}


def _market(session) -> ChoiceMarketData:
    return ChoiceMarketData(session=session, master=None, history=None)  # type: ignore[arg-type]


# ------------------------------------------------------------------ emit


def test_emit_accepts_a_level_detail_without_colliding():
    """The regression: `level` is a strike, not a log severity.

    `emit("trade", msg, level=24000)` used to raise TypeError because the
    first parameter was also called `level` -- and that fired on the main
    path, the moment a condor opened.
    """
    from engine.forward.runner import ForwardRunner

    runner = ForwardRunner.__new__(ForwardRunner)
    runner._lock = threading.RLock()
    runner.events = []
    runner.max_events = 100

    runner.emit("trade", "Opened condor at 24,000", level=24_000, credit=5250.0)

    assert len(runner.events) == 1
    event = runner.events[0]
    assert event.level == "trade"                 # severity
    assert event.detail["level"] == 24_000        # strike, preserved
    assert event.detail["credit"] == 5250.0


def test_emit_trims_the_log_to_its_cap():
    from engine.forward.runner import ForwardRunner

    runner = ForwardRunner.__new__(ForwardRunner)
    runner._lock = threading.RLock()
    runner.events = []
    runner.max_events = 5
    for i in range(20):
        runner.emit("info", f"tick {i}", level=i)
    assert len(runner.events) == 5
    assert runner.events[-1].detail["level"] == 19


# ------------------------------------------------------------- touchline


@pytest.mark.parametrize("shape", [f[0] for f in TOUCHLINE_FORMATS])
def test_touchline_finds_whichever_shape_the_endpoint_accepts(shape):
    """The SDK documents three contradictory formats; probe rather than guess."""
    session = _Session(accepts=shape)
    market = _market(session)
    quotes = market.touchline(PAIR)
    assert quotes == {26000: 24_000.0}
    assert market.touchline_format == shape


def test_touchline_remembers_the_working_shape():
    session = _Session(accepts="segment,token|")
    market = _market(session)
    market.touchline(PAIR)
    first_attempts = len(session.seen)
    session.seen.clear()
    market.touchline(PAIR)
    # Second call should hit the known-good shape immediately.
    assert len(session.seen) == 1 < first_attempts


def test_touchline_reports_what_it_tried_when_nothing_works():
    session = _Session(accepts=None)
    market = _market(session)
    with pytest.raises(ChoiceError) as exc:
        market.touchline(PAIR)
    assert "payload shapes" in str(exc.value)
    assert market.last_touchline_error


def test_a_raw_price_is_taken_at_face_value_until_calibration_says_otherwise():
    session = _Session(accepts="segment@token,", rows=[{"Token": 42632, "LTP": 123.45}])
    assert _market(session).touchline([_contract(42632)]) == {42632: 123.45}


def test_alternative_field_names_are_understood():
    session = _Session(accepts="segment@token,", rows=[{"scripcode": 42632, "LastTradedPrice": 50.0}])
    assert _market(session).touchline([_contract(42632)]) == {42632: 50.0}


def test_rows_nested_under_an_envelope_key_are_found():
    session = _Session(accepts="segment@token,")
    session.rows = {"Touchline": [{"Token": 26000, "LTP": 24_000}]}  # type: ignore[assignment]
    assert _market(session).touchline([_contract(26000, 1)]) == {26000: 24_000.0}


def test_no_contracts_is_not_a_request():
    session = _Session(accepts="segment@token,")
    assert _market(session).touchline([]) == {}
    assert session.seen == []


# ------------------------------------------- envelopes seen in production


def test_rows_nested_as_a_dict_keyed_by_token_are_found():
    """The shape that broke a live run.

    Choice answered Success with ``Response: {"MultipleTouchline": {...}}``
    where the inner value was a mapping rather than a list, so the old parser
    -- which only ever looked for a list -- reported "no rows parsed" against a
    perfectly good response and the ladder never received a price.
    """
    session = _Session(accepts="segment@token,")
    session.rows = {  # type: ignore[assignment]
        "MultipleTouchline": {"26000": {"Token": 26000, "LTP": 24_000}}
    }
    assert _market(session).touchline([_contract(26000, 1)]) == {26000: 24_000.0}


def test_a_lone_row_under_an_envelope_key_is_found():
    session = _Session(accepts="segment@token,")
    session.rows = {"MultipleTouchline": {"Token": 26000, "LTP": 24_000}}  # type: ignore[assignment]
    assert _market(session).touchline([_contract(26000, 1)]) == {26000: 24_000.0}


def test_deeply_wrapped_rows_are_still_found():
    session = _Session(accepts="segment@token,")
    session.rows = {"Response": {"data": [{"Token": 26000, "LTP": 24_000}]}}  # type: ignore[assignment]
    assert _market(session).touchline([_contract(26000, 1)]) == {26000: 24_000.0}


def test_the_shape_diagnostic_names_the_inner_payload():
    """A failure message has to say what was actually inside the envelope.

    "dict keys=['MultipleTouchline']" is exactly as unhelpful as silence.
    """
    session = _Session(accepts="segment@token,")
    session.rows = {"MultipleTouchline": "26000|2400000"}  # type: ignore[assignment]
    with pytest.raises(ChoiceError) as exc:
        _market(session).touchline([_contract(26000, 1)])
    message = str(exc.value)
    assert "MultipleTouchline" in message
    assert "26000|2400000" in message  # the actual content, not just the key


# ------------------------------------------------------------- bid / ask


def test_bid_and_ask_are_captured_when_the_response_carries_depth():
    session = _Session(accepts="segment@token,")
    session.rows = [{"Token": 42632, "LTP": 123.45, "BestBidPrice": 123.00, "BestAskPrice": 124.00}]
    quote = _market(session).quotes([_contract(42632)])[42632]
    assert (quote.bid, quote.ask) == (123.0, 124.0)
    assert quote.has_depth
    assert quote.mid == pytest.approx(123.5)
    assert quote.spread == pytest.approx(1.0)


def test_a_quote_without_depth_reports_no_spread_and_falls_back_to_ltp():
    """Modelling a spread is a decision the fill model must make knowingly."""
    session = _Session(accepts="segment@token,", rows=[{"Token": 42632, "LTP": 123.45}])
    quote = _market(session).quotes([_contract(42632)])[42632]
    assert not quote.has_depth
    assert quote.spread is None
    assert quote.mid == quote.ltp == pytest.approx(123.45)


def test_a_crossed_or_zero_book_is_not_treated_as_depth():
    session = _Session(accepts="segment@token,")
    session.rows = [{"Token": 42632, "LTP": 123.45, "BestBidPrice": 124.00, "BestAskPrice": 123.00}]
    quote = _market(session).quotes([_contract(42632)])[42632]
    assert not quote.has_depth
    assert quote.mid == pytest.approx(123.45)


# ------------------------------------------- ChartData fallback for quotes


class _HistoryStub:
    """Stands in for HistoryClient, returning one candle per token."""

    def __init__(self, closes: dict[int, float] | None = None, raises=False):
        self.closes = closes or {}
        self.raises = raises
        self.asked: list[int] = []

    def fetch(self, segment_id, token, start, end, resolution, allow_partial=True):
        import pandas as pd

        from engine.choice.history import FetchReport

        self.asked.append(token)
        if self.raises:
            raise ChoiceError("ChartData is unhappy too")
        close = self.closes.get(token)
        rows = [] if close is None else [{"ts": None, "open": close, "high": close,
                                         "low": close, "close": close, "volume": 1, "oi": 0}]
        report = FetchReport(
            token=token, segment_id=segment_id, resolution=resolution,
            start=_as_dt(start), end=_as_dt(end),
            status="ok" if rows else "no_data", bars=len(rows),
        )
        return pd.DataFrame(rows), report


def _as_dt(value):
    import datetime as dt

    from engine.config import IST

    if isinstance(value, dt.datetime):
        return value
    return dt.datetime.now(tz=IST)


def _market_with_history(session, history) -> ChoiceMarketData:
    return ChoiceMarketData(session=session, master=None, history=history)  # type: ignore[arg-type]


def test_an_empty_touchline_falls_back_to_the_last_traded_candle():
    """The live failure: Success with an empty row list for the index token.

    Correctly addressed, during market hours, and simply not served -- which
    left the ladder with no spot and a run that could never start.
    """
    session = _Session(accepts="segment@token,")
    session.rows = {"MultipleTouchline": []}  # type: ignore[assignment]
    market = _market_with_history(session, _HistoryStub({26000: 24_137.5}))
    quotes = market.quotes([_contract(26000, 1)])
    assert quotes[26000].ltp == pytest.approx(24_137.5)


def test_a_fallback_price_is_flagged_stale_rather_than_passed_off_as_the_touch():
    session = _Session(accepts="segment@token,")
    session.rows = {"MultipleTouchline": []}  # type: ignore[assignment]
    market = _market_with_history(session, _HistoryStub({26000: 24_137.5}))
    quote = market.quotes([_contract(26000, 1)])[26000]
    assert quote.stale is True
    assert quote.has_depth is False


def test_a_live_quote_is_preferred_and_skips_the_fallback_entirely():
    history = _HistoryStub({42632: 999.0})
    market = _market_with_history(_Session(accepts="segment@token,",
                                           rows=[{"Token": 42632, "LTP": 123.45}]), history)
    quote = market.quotes([_contract(42632)])[42632]
    assert quote.ltp == pytest.approx(123.45)
    assert quote.stale is False
    assert history.asked == []          # no wasted ChartData call


def test_only_the_legs_the_book_missed_fall_back():
    session = _Session(accepts="segment@token,", rows=[{"Token": 42632, "LTP": 123.45}])
    history = _HistoryStub({26000: 24_000.0})
    market = _market_with_history(session, history)
    quotes = market.quotes([_contract(42632), _contract(26000, 1)])
    assert quotes[42632].stale is False
    assert quotes[26000].stale is True
    assert history.asked == [26000]


def test_both_sources_failing_is_an_error_naming_both():
    session = _Session(accepts="segment@token,")
    session.rows = {"MultipleTouchline": []}  # type: ignore[assignment]
    market = _market_with_history(session, _HistoryStub(raises=True))
    with pytest.raises(ChoiceError) as exc:
        market.quotes([_contract(26000, 1)])
    message = str(exc.value)
    assert "MultipleTouchline" in message and "ChartData" in message
    assert "payload shapes" in message


def test_the_fallback_can_be_turned_off_for_callers_that_need_the_touch():
    session = _Session(accepts="segment@token,")
    session.rows = {"MultipleTouchline": []}  # type: ignore[assignment]
    history = _HistoryStub({26000: 24_000.0})
    market = _market_with_history(session, history)
    with pytest.raises(ChoiceError):
        market.quotes([_contract(26000, 1)], allow_history_fallback=False)
    assert history.asked == []


# ------------------------------------------------- touchline price scaling


def test_the_quote_scale_is_measured_against_chartdata():
    """The bug this exists to catch, seen on a live paper run.

    A 29-Sep 23,700 CE filled at Rs2.00 when it was worth about Rs254, so a
    20-DTE 200-point condor showed a credit of Rs62 against Rs13,000 of risk --
    a 0.5% reward on risk, which is not a trade that exists. The premiums were
    a hundredth of their value because the REST endpoint's units were assumed
    from a comment about the *websocket* feed.
    """
    session = _Session(accepts="segment@token,", rows=[{"Token": 42632, "LTP": 254.0}])
    market = _market_with_history(session, _HistoryStub({42632: 254.0}))
    assert market.calibrate_quote_scale(_contract(42632)) == 1.0
    assert market.quotes([_contract(42632)])[42632].ltp == pytest.approx(254.0)


def test_a_paisa_feed_is_detected_and_corrected():
    """If Choice really did send paisa, the same measurement finds that too."""
    session = _Session(accepts="segment@token,", rows=[{"Token": 42632, "LTP": 25_400}])
    market = _market_with_history(session, _HistoryStub({42632: 254.0}))
    assert market.calibrate_quote_scale(_contract(42632)) == pytest.approx(0.01)
    assert market.quotes([_contract(42632)])[42632].ltp == pytest.approx(254.0)


def test_calibration_tolerates_the_two_sources_moving_apart():
    """The book and the last candle are minutes apart in a live market."""
    session = _Session(accepts="segment@token,", rows=[{"Token": 42632, "LTP": 254.0}])
    market = _market_with_history(session, _HistoryStub({42632: 231.0}))   # -9%
    assert market.calibrate_quote_scale(_contract(42632)) == 1.0


def test_a_disagreement_that_is_not_a_unit_error_leaves_the_scale_alone():
    """5x is not a unit difference; it means the two are not the same thing."""
    session = _Session(accepts="segment@token,", rows=[{"Token": 42632, "LTP": 254.0}])
    market = _market_with_history(session, _HistoryStub({42632: 1_270.0}))
    before = market.quote_scale
    assert market.calibrate_quote_scale(_contract(42632)) == (before or 1.0)


def test_calibration_is_a_no_op_when_either_source_is_silent():
    session = _Session(accepts="segment@token,")
    session.rows = {"MultipleTouchline": []}  # type: ignore[assignment]
    market = _market_with_history(session, _HistoryStub({42632: 254.0}))
    assert market.calibrate_quote_scale(_contract(42632)) == 1.0

    live = _Session(accepts="segment@token,", rows=[{"Token": 42632, "LTP": 254.0}])
    no_history = _market_with_history(live, _HistoryStub({}))
    assert no_history.calibrate_quote_scale(_contract(42632)) == 1.0
