"""Paper fill pricing.

With no live order path, the fill model is not an approximation of the real
result -- it *is* the result. Filling every leg at the last traded price would
report a number nobody could have traded: an iron condor crosses four spreads
going in and four coming out, and on wings 400 points from spot that spread is
a large fraction of the premium. So a fill always crosses the touch.

Two honesty rules follow from that:

* When Choice returns real depth, the fill uses it -- buys lift the offer,
  sells hit the bid. No guessing.
* When it does not, the spread is *modelled* rather than assumed to be zero,
  and the fill is flagged. A run then reports how much of itself was priced on
  a real book, so an optimistic number is visible as optimistic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from engine.data.market import Quote
from engine.strategy.condor import Side

# NSE quotes NIFTY options in 5-paisa ticks, and no premium trades below it.
TICK = 0.05

# Binary float noise must not push an exact tick onto the next one.
_TICK_EPS = 1e-9


def _round_to_tick(price: float, side: Side) -> float:
    """Snap to the tick grid, always against the trader.

    Symmetric rounding gave a better price than the market showed on roughly
    40% of fills -- a sell rounded *up* through the bid, a buy rounded *down*
    through the offer. Small per share, free money in aggregate, and always in
    the direction that flatters the result. A buy pays the next tick up, a sell
    receives the next tick down.
    """
    ticks = price / TICK
    snapped = math.ceil(ticks - _TICK_EPS) if side is Side.BUY else math.floor(ticks + _TICK_EPS)
    return max(TICK, snapped * TICK)


@dataclass(frozen=True)
class FillPrice:
    """What a leg actually filled at, and what that cost against fair value."""

    price: float
    reference: float          # mid when there is a book, else last traded
    slippage: float           # always >= 0: what crossing the spread cost
    spread_modelled: bool     # True when no real depth backed this fill

    def as_detail(self) -> dict[str, float | bool]:
        return {
            "price": round(self.price, 2),
            "reference": round(self.reference, 2),
            "slippage": round(self.slippage, 2),
            "modelled": self.spread_modelled,
        }


@dataclass(frozen=True)
class FillModel:
    """How to cross the spread, including when the book is missing.

    ``modelled_spread_pct`` is the full bid-ask width as a fraction of premium,
    so a leg pays half of it. The default is deliberately not generous: cheap
    far-OTM wings are exactly where real spreads are widest in percentage
    terms, which is why the floor is expressed in ticks as well.
    """

    modelled_spread_pct: float = 0.02
    min_half_spread: float = TICK
    # A book wider than this is treated as untradeable rather than filled at a
    # price that would never have been hit.
    max_spread_pct: float = 0.50

    def fill(self, quote: Quote, side: Side) -> FillPrice | None:
        """Price one leg, or None if the book is too wide to trade through."""
        if quote.has_depth:
            spread = quote.spread or 0.0
            if quote.mid > 0 and spread / quote.mid > self.max_spread_pct:
                return None
            touch = float(quote.ask if side is Side.BUY else quote.bid)
            reference = quote.mid
            price = _round_to_tick(touch, side)
            return FillPrice(
                price=price,
                # Measured against the price actually paid, not the unrounded
                # touch -- otherwise the reported cost describes a fill the
                # user did not get.
                reference=reference,
                slippage=abs(price - reference),
                spread_modelled=False,
            )

        reference = quote.ltp
        half = max(self.min_half_spread, reference * self.modelled_spread_pct / 2.0)
        raw = reference + half if side is Side.BUY else reference - half
        price = _round_to_tick(raw, side)
        # The one-tick floor exists so a fill is never nonsensical, but on a
        # 5-paisa wing it would otherwise let a sale print *above* the last
        # trade -- turning the modelled spread into a modelled profit.
        if side is Side.SELL:
            price = min(price, _round_to_tick(reference, side))
        return FillPrice(
            price=price,
            reference=reference,
            slippage=abs(price - reference),
            spread_modelled=True,
        )

    def exit_fill(self, quote: Quote, entry_side: Side) -> FillPrice | None:
        """Price the closing leg, which crosses the spread the other way."""
        return self.fill(quote, Side.BUY if entry_side is Side.SELL else Side.SELL)
