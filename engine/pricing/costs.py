"""Transaction costs for NSE index options.

The ladder places four legs per rung, so a twenty-rung campaign is eighty
legs.  Ignoring costs would materially overstate a credit strategy whose whole
edge is a few tens of points of premium, so they are modelled per leg.

Rates are defaults as of the 2024-25 NSE/SEBI schedule and are all
configurable — verify them against your own contract notes before trusting a
P&L figure, since brokerage and exchange slabs change.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.strategy.condor import Side


@dataclass(frozen=True)
class CostModel:
    """Per-leg charges for NSE F&O options."""

    brokerage_per_order: float = 20.0        # flat discount-broker rate
    stt_sell_rate: float = 0.001             # 0.1% of premium, SELL side only
    exchange_txn_rate: float = 0.00035       # NSE options, on premium turnover
    sebi_turnover_rate: float = 0.000001     # Rs 10 per crore
    stamp_duty_buy_rate: float = 0.00003     # 0.003%, BUY side only
    gst_rate: float = 0.18                   # on brokerage + txn + SEBI
    slippage_points: float = 0.0             # per share, applied per leg if desired

    def leg_cost(self, side: Side, price: float, qty: int) -> float:
        """Total charges for one option leg.

        ``price`` is per share; ``qty`` is in shares (lots x lot size), which
        is what Choice expects on the wire.
        """
        turnover = abs(price) * qty
        brokerage = self.brokerage_per_order
        stt = self.stt_sell_rate * turnover if side is Side.SELL else 0.0
        exchange = self.exchange_txn_rate * turnover
        sebi = self.sebi_turnover_rate * turnover
        stamp = self.stamp_duty_buy_rate * turnover if side is Side.BUY else 0.0
        gst = self.gst_rate * (brokerage + exchange + sebi)
        return brokerage + stt + exchange + sebi + stamp + gst

    def slippage(self, qty: int) -> float:
        return self.slippage_points * qty

    def breakdown(self, side: Side, price: float, qty: int) -> dict[str, float]:
        """Itemised charges, for the cost panel in the UI."""
        turnover = abs(price) * qty
        brokerage = self.brokerage_per_order
        exchange = self.exchange_txn_rate * turnover
        sebi = self.sebi_turnover_rate * turnover
        items = {
            "brokerage": brokerage,
            "stt": self.stt_sell_rate * turnover if side is Side.SELL else 0.0,
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
    exchange_txn_rate=0.0,
    sebi_turnover_rate=0.0,
    stamp_duty_buy_rate=0.0,
    gst_rate=0.0,
)
"""A frictionless model, for isolating strategy behaviour from cost drag."""
