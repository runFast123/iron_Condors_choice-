"""Tests for the forward runner's log and quote handling.

These cover the paths that only execute against a live feed, which is exactly
where two crashes hid: emit() colliding with a `level=` detail kwarg, and the
touchline payload shape.
"""

from __future__ import annotations

import pytest

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
        self.rows = rows if rows is not None else [{"Token": 26000, "LTP": 2400000}]
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
    from engine.choice.errors import ChoiceError

    session = _Session(accepts=None)
    market = _market(session)
    with pytest.raises(ChoiceError) as exc:
        market.touchline(PAIR)
    assert "payload shapes" in str(exc.value)
    assert market.last_touchline_error


def test_prices_are_converted_from_paisa():
    session = _Session(accepts="segment@token,", rows=[{"Token": 42632, "LTP": 12345}])
    assert _market(session).touchline([_contract(42632)]) == {42632: 123.45}


def test_alternative_field_names_are_understood():
    session = _Session(accepts="segment@token,", rows=[{"scripcode": 42632, "LastTradedPrice": 5000}])
    assert _market(session).touchline([_contract(42632)]) == {42632: 50.0}


def test_rows_nested_under_an_envelope_key_are_found():
    session = _Session(accepts="segment@token,")
    session.rows = {"Touchline": [{"Token": 26000, "LTP": 2400000}]}  # type: ignore[assignment]
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
        "MultipleTouchline": {"26000": {"Token": 26000, "LTP": 2400000}}
    }
    assert _market(session).touchline([_contract(26000, 1)]) == {26000: 24_000.0}


def test_a_lone_row_under_an_envelope_key_is_found():
    session = _Session(accepts="segment@token,")
    session.rows = {"MultipleTouchline": {"Token": 26000, "LTP": 2400000}}  # type: ignore[assignment]
    assert _market(session).touchline([_contract(26000, 1)]) == {26000: 24_000.0}


def test_deeply_wrapped_rows_are_still_found():
    session = _Session(accepts="segment@token,")
    session.rows = {"Response": {"data": [{"Token": 26000, "LTP": 2400000}]}}  # type: ignore[assignment]
    assert _market(session).touchline([_contract(26000, 1)]) == {26000: 24_000.0}


def test_the_shape_diagnostic_names_the_inner_payload():
    """A failure message has to say what was actually inside the envelope.

    "dict keys=['MultipleTouchline']" is exactly as unhelpful as silence.
    """
    from engine.choice.errors import ChoiceError

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
    session.rows = [{"Token": 42632, "LTP": 12345, "BestBidPrice": 12300, "BestAskPrice": 12400}]
    quote = _market(session).quotes([_contract(42632)])[42632]
    assert (quote.bid, quote.ask) == (123.0, 124.0)
    assert quote.has_depth
    assert quote.mid == pytest.approx(123.5)
    assert quote.spread == pytest.approx(1.0)


def test_a_quote_without_depth_reports_no_spread_and_falls_back_to_ltp():
    """Modelling a spread is a decision the fill model must make knowingly."""
    session = _Session(accepts="segment@token,", rows=[{"Token": 42632, "LTP": 12345}])
    quote = _market(session).quotes([_contract(42632)])[42632]
    assert not quote.has_depth
    assert quote.spread is None
    assert quote.mid == quote.ltp == pytest.approx(123.45)


def test_a_crossed_or_zero_book_is_not_treated_as_depth():
    session = _Session(accepts="segment@token,")
    session.rows = [{"Token": 42632, "LTP": 12345, "BestBidPrice": 12400, "BestAskPrice": 12300}]
    quote = _market(session).quotes([_contract(42632)])[42632]
    assert not quote.has_depth
    assert quote.mid == pytest.approx(123.45)
