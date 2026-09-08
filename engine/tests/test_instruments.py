"""Tests for scrip-master parsing and option resolution."""

from __future__ import annotations

import datetime as dt

import pytest

from engine.choice.errors import ChoiceInstrumentError
from engine.choice.instruments import ScripMaster, _fmt_master_date

EXPIRY = dt.date(2026, 3, 26)
NEXT_EXPIRY = dt.date(2026, 4, 2)


def _csv(rows: list[str], header: str | None = None) -> str:
    head = header or "Token,Exchange,Segment,Symbol,SecDesc,Series,MarketLot,ExpiryDate,StrikePrice,OptionType"
    return "\n".join([head, *rows])


def _option_rows(strikes=range(23000, 25001, 50), expiry="26-Mar-2026", start_token=40000) -> list[str]:
    rows = []
    token = start_token
    for strike in strikes:
        for right in ("CE", "PE"):
            rows.append(
                f"{token},NSE,2,NIFTY,NIFTY {expiry} {strike} {right},OPTIDX,75,{expiry},{strike},{right}"
            )
            token += 1
    return rows


@pytest.fixture
def master() -> ScripMaster:
    sm = ScripMaster()
    sm.load_csv(_csv(_option_rows() + _option_rows(expiry="02-Apr-2026", start_token=90000)))
    return sm


# ------------------------------------------------------------------ loading


def test_master_date_is_locale_independent():
    # kkunal uses %b, which follows the OS locale and 404s on non-English hosts.
    assert _fmt_master_date(dt.date(2026, 9, 7)) == "07Sep2026"
    assert _fmt_master_date(dt.date(2026, 12, 31)) == "31Dec2026"


def test_options_are_indexed_with_strike_expiry_and_right(master):
    contract = master.option("NIFTY", EXPIRY, 23800, "PE")
    assert contract.strike == 23800
    assert contract.option_type == "PE"
    assert contract.expiry == EXPIRY
    assert contract.lot_size == 75
    assert contract.is_option


def test_reload_replaces_rather_than_appends():
    """kkunal appends on every fetch, silently duplicating the whole master."""
    sm = ScripMaster()
    sm.load_csv(_csv(_option_rows()))
    first = len(sm.contracts)
    sm.load_csv(_csv(_option_rows()))
    assert len(sm.contracts) == first


def test_missing_token_column_fails_loudly():
    sm = ScripMaster()
    with pytest.raises(ChoiceInstrumentError, match="token"):
        sm.load_csv("Foo,Bar\n1,2")


def test_column_detection_tolerates_alternative_header_names():
    sm = ScripMaster()
    sm.load_csv(
        _csv(
            ["7001,NSE,2,NIFTY,NIFTY 26-Mar-2026 23800 PE,OPTIDX,75,26-Mar-2026,23800,PE"],
            header="InstrumentToken,Exch,SegmentId,TradingSymbol,Description,Instrument,LotSize,Expiry,Strike,OptType",
        )
    )
    assert sm.option("NIFTY", EXPIRY, 23800, "PE").token == 7001


def test_blank_lot_size_is_zero_not_one():
    """A silent 1 would place a 1-share order instead of one 75-share lot."""
    sm = ScripMaster()
    sm.load_csv(_csv(["7002,NSE,2,NIFTY,NIFTY 26-Mar-2026 23800 PE,OPTIDX,,26-Mar-2026,23800,PE"]))
    assert sm.by_token[7002].lot_size == 0


# ------------------------------------------------------------------ queries


def test_unknown_strike_raises_with_nearby_strikes_in_the_message(master):
    with pytest.raises(ChoiceInstrumentError) as excinfo:
        master.option("NIFTY", EXPIRY, 23_812, "PE")
    assert "Nearest available strikes" in str(excinfo.value)


def test_find_option_returns_none_instead_of_raising(master):
    assert master.find_option("NIFTY", EXPIRY, 23_812, "PE") is None


def test_expiries_are_sorted_and_filterable(master):
    assert master.expiries("NIFTY") == [EXPIRY, NEXT_EXPIRY]
    assert master.expiries("NIFTY", after=dt.date(2026, 3, 27)) == [NEXT_EXPIRY]


def test_nearest_expiry_picks_the_front_week(master):
    assert master.nearest_expiry("NIFTY", dt.date(2026, 3, 23)) == EXPIRY
    assert master.nearest_expiry("NIFTY", dt.date(2026, 3, 27)) == NEXT_EXPIRY


def test_nearest_expiry_honours_min_days(master):
    # On expiry day itself, min_days=1 must roll to the following week.
    assert master.nearest_expiry("NIFTY", EXPIRY, min_days=1) == NEXT_EXPIRY


def test_nearest_expiry_raises_when_master_is_stale(master):
    with pytest.raises(ChoiceInstrumentError, match="stale"):
        master.nearest_expiry("NIFTY", dt.date(2027, 1, 1))


def test_strike_step_is_detected_from_the_listed_chain(master):
    assert master.strike_step("NIFTY", EXPIRY) == 50.0


def test_nfo_segment_is_inferred_from_data_not_docs(master):
    # kkunal's README claims both 2 and 13; we read it off real option rows.
    assert master.infer_nfo_segment("NIFTY") == 2


def test_lot_size_is_read_from_the_master(master):
    assert master.lot_size_for("NIFTY") == 75


def test_option_type_falls_back_to_the_description():
    sm = ScripMaster()
    sm.load_csv(_csv(["7003,NSE,2,NIFTY,NIFTY 26-Mar-2026 23800 PE,OPTIDX,75,26-Mar-2026,23800,"]))
    assert sm.by_token[7003].option_type == "PE"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("26-Mar-2026", dt.date(2026, 3, 26)),
        ("2026-03-26", dt.date(2026, 3, 26)),
        ("26/03/2026", dt.date(2026, 3, 26)),
        ("26Mar2026", dt.date(2026, 3, 26)),
    ],
)
def test_expiry_formats(raw, expected):
    sm = ScripMaster()
    sm.load_csv(_csv([f"7100,NSE,2,NIFTY,NIFTY {raw} 23800 PE,OPTIDX,75,{raw},23800,PE"]))
    assert sm.by_token[7100].expiry == expected


# ============================================ the REAL Choice master format
#
# The live CSV differs from what the synthetic fixtures above assumed, and two
# of those differences broke option resolution outright until they were found
# by running against the real file:
#   * Expiry is DDMONYY ("23NOV26"), which no configured format parsed.
#   * StrikePrice is scaled by PriceDivisor (2325000 / 100 = 23250), so every
#     strike was 100x too large and nothing ever resolved.

REAL_HEADER = (
    "Exchange,Segment,ISIN,Token,UnderlyingToken,Symbol,SecName,SecDesc,Series,"
    "Instrument,Expiry,OptionType,StrikePrice,PriceDivisor,MarketLot,PriceTick"
)


def real_row(token, strike_scaled, right, expiry="08SEP26", lot=65, divisor=100):
    return (
        f"NSEFO,2,,{token},26000,NIFTY,NIFTY|{expiry} {right} x,NIFTY26SEP{right},XX,"
        f"OPTIDX,{expiry},{right},{strike_scaled},{divisor},{lot},5"
    )


@pytest.fixture
def real_master() -> ScripMaster:
    rows = []
    token = 40000
    for strike in range(23000, 24001, 50):
        for right in ("CE", "PE"):
            rows.append(real_row(token, strike * 100, right))
            token += 1
    rows.append("NSE,1,,26000,,NIFTY,Nifty 50,Nifty 50,,,,,0,100,0,5")
    rows.append("NSE,1,,26017,,INDIAVIX,India VIX,India VIX,,,,,0,10000,0,5")
    sm = ScripMaster()
    sm.load_csv("\n".join([REAL_HEADER, *rows]))
    return sm


def test_ddmonyy_expiry_is_parsed():
    """'23NOV26' is the live format; nothing else in the list matches it."""
    sm = ScripMaster()
    sm.load_csv("\n".join([REAL_HEADER, real_row(70001, 2325000, "PE", expiry="23NOV26")]))
    assert sm.by_token[70001].expiry == dt.date(2026, 11, 23)


def test_strike_is_divided_by_the_price_divisor():
    """2325000 with divisor 100 is strike 23250, not 2,325,000."""
    sm = ScripMaster()
    sm.load_csv("\n".join([REAL_HEADER, real_row(70002, 2325000, "PE", expiry="23NOV26")]))
    assert sm.by_token[70002].strike == 23250.0


def test_a_divisor_of_one_leaves_the_strike_alone():
    sm = ScripMaster()
    sm.load_csv("\n".join([REAL_HEADER, real_row(70003, 23250, "PE", expiry="23NOV26", divisor=1)]))
    assert sm.by_token[70003].strike == 23250.0


def test_real_format_resolves_a_contract_end_to_end(real_master):
    expiry = dt.date(2026, 9, 8)
    assert real_master.expiries("NIFTY") == [expiry]
    contract = real_master.option("NIFTY", expiry, 23800, "PE")
    assert contract.strike == 23800.0
    assert contract.lot_size == 65
    assert contract.segment_id == 2


def test_lot_size_comes_from_the_master_not_a_constant(real_master):
    """NSE revised NIFTY 25 -> 50 -> 75 -> 65; a hardcoded value goes stale."""
    assert real_master.lot_size_for("NIFTY") == 65


def test_strike_grid_is_read_correctly(real_master):
    expiry = dt.date(2026, 9, 8)
    strikes = real_master.strikes("NIFTY", expiry)
    assert strikes[0] == 23000 and strikes[-1] == 24000
    assert real_master.strike_step("NIFTY", expiry) == 50.0


def test_indices_resolve_from_the_real_format(real_master):
    assert real_master.index("NIFTY").token == 26000
    assert real_master.index("INDIAVIX").token == 26017
