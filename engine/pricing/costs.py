"""Transaction costs for NSE index options.

The ladder places four legs per rung, so a twenty-rung campaign is eighty
legs.  Ignoring costs would materially overstate a credit strategy whose whole
edge is a few tens of points of premium, so they are modelled per leg.

The statutory and exchange rates are the ones in force on the trade date, not
today's for every year: a backtest over 2019-2026 spans four STT rates on
option premium and five exchange transaction charges. Each is from the
exchange's circulars and the Finance Acts:

* STT on option premium, sell side: 0.05% to 31 Mar 2023, 0.0625% to 30 Sep
  2024, 0.1% to 31 Mar 2026, 0.15% from 1 Apr 2026 (Union Budget 2026-27).
* STT on an exercised (in-the-money) option at expiry, paid by the holder of
  the long on its settlement value: 0.125%, 0.15% from 1 Apr 2026. The writer
  pays none, and a cash-settled expiry places no order, so there is no
  brokerage, exchange charge or GST on it.
* NSE options transaction charge on premium: 0.05%; 0.053% from 1 Jan 2021
  (NSE/FA/46730); 0.05% from 1 Apr 2023 (NSE/FA/56129); 0.0495% from 1 Apr
  2024; 0.03503% from 1 Oct 2024.
* SEBI turnover fee Rs 10 per crore; stamp duty 0.003% on the buy side (the
  uniform rate since 1 Jul 2020; before that it varied by state); GST 18% on
  brokerage, exchange charge and SEBI fee.

Brokerage is the one broker-specific figure: Rs 20 per order, a discount
broker's flat rate. Any rate set explicitly on a CostModel overrides the
schedule, which is how the frictionless model and the tests pin theirs.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from engine.strategy.condor import Side

#: (first day in force, rate), in order.
STT_OPTION_SALE: tuple[tuple[dt.date, float], ...] = (
    (dt.date(2016, 6, 1), 0.0005),
    (dt.date(2023, 4, 1), 0.000625),
    (dt.date(2024, 10, 1), 0.001),
    (dt.date(2026, 4, 1), 0.0015),
)
STT_EXERCISE: tuple[tuple[dt.date, float], ...] = (
    (dt.date(2016, 6, 1), 0.00125),
    (dt.date(2026, 4, 1), 0.0015),
)
NSE_OPTION_TXN: tuple[tuple[dt.date, float], ...] = (
    (dt.date(2016, 1, 1), 0.0005),
    (dt.date(2021, 1, 1), 0.00053),
    (dt.date(2023, 4, 1), 0.0005),
    (dt.date(2024, 4, 1), 0.000495),
    (dt.date(2024, 10, 1), 0.0003503),
)


def rate_on(schedule: tuple[tuple[dt.date, float], ...], on: dt.date) -> float:
    """The rate in force on `on`."""
    rate = schedule[0][1]
    for since, value in schedule:
        if on >= since:
            rate = value
    return rate


@dataclass(frozen=True)
class CostModel:
    """Per-leg charges for NSE F&O options.

    A rate left as None is the one in force on the trade date.
    """

    brokerage_per_order: float = 20.0        # flat discount-broker rate
    stt_sell_rate: float | None = None       # of premium, SELL side only
    exercise_stt_rate: float | None = None   # of settlement value, ITM longs at expiry
    exchange_txn_rate: float | None = None   # NSE options, on premium turnover
    sebi_turnover_rate: float = 0.000001     # Rs 10 per crore
    stamp_duty_buy_rate: float = 0.00003     # 0.003%, BUY side only
    gst_rate: float = 0.18                   # on brokerage + txn + SEBI
    slippage_points: float = 0.0             # per share, applied per leg if desired

    def _stt(self, on: dt.date) -> float:
        return self.stt_sell_rate if self.stt_sell_rate is not None else rate_on(STT_OPTION_SALE, on)

    def _exchange(self, on: dt.date) -> float:
        return self.exchange_txn_rate if self.exchange_txn_rate is not None else rate_on(NSE_OPTION_TXN, on)

    def _exercise(self, on: dt.date) -> float:
        return self.exercise_stt_rate if self.exercise_stt_rate is not None else rate_on(STT_EXERCISE, on)

    def leg_cost(self, side: Side, price: float, qty: int, on: dt.date | None = None) -> float:
        """Total charges for one option leg traded on `on` (today if omitted).

        ``price`` is per share; ``qty`` is in shares (lots x lot size), which
        is what Choice expects on the wire.
        """
        return self.breakdown(side, price, qty, on)["total"]

    def settlement_cost(self, side: Side, intrinsic: float, qty: int, on: dt.date | None = None) -> float:
        """Charges when a leg is cash-settled at expiry.

        Only STT on exercise, on an in-the-money long, on its settlement value.
        No order is placed, so no brokerage, exchange charge or GST; the short
        side of an exercised option pays nothing.
        """
        if side is not Side.BUY or intrinsic <= 0:
            return 0.0
        return self._exercise(on or dt.date.today()) * intrinsic * qty

    def slippage(self, qty: int) -> float:
        return self.slippage_points * qty

    def breakdown(self, side: Side, price: float, qty: int, on: dt.date | None = None) -> dict[str, float]:
        """Itemised charges, for the cost panel in the UI."""
        on = on or dt.date.today()
        turnover = abs(price) * qty
        brokerage = self.brokerage_per_order
        exchange = self._exchange(on) * turnover
        sebi = self.sebi_turnover_rate * turnover
        items = {
            "brokerage": brokerage,
            "stt": self._stt(on) * turnover if side is Side.SELL else 0.0,
            "exchange": exchange,
            "sebi": sebi,
            "stamp_duty": self.stamp_duty_buy_rate * turnover if side is Side.BUY else 0.0,
            "gst": self.gst_rate * (brokerage + exchange + sebi),
        }
        items["total"] = sum(items.values())
        items["turnover"] = turnover
        return items


ZERO_COST = CostModel(
    brokerage_per_order=0.0,
    stt_sell_rate=0.0,
    exercise_stt_rate=0.0,
    exchange_txn_rate=0.0,
    sebi_turnover_rate=0.0,
    stamp_duty_buy_rate=0.0,
    gst_rate=0.0,
)
"""A frictionless model, for isolating strategy behaviour from cost drag."""
