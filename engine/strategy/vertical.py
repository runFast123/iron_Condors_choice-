"""A two-leg vertical spread: same right, one leg bought, one sold.

Exists because the condor's risk formulas are the credit-spread ones, and
applying them to a bought vertical is not approximately wrong, it is wrong by a
multiple. A real HIC put debit spread -- buy 23,000 PE, sell 22,800 PE, 65 lots
of a 65 lot size, 65 a share paid -- came out of `Condor` as:

    max loss   17,225   against a true 4,225   (4.08x)
    max profit -4,225   against a true 8,775   (a loss, as the best case)

and those figures feed `capital_at_risk`, which every risk-adjusted metric
divides by. So the shapes are kept apart rather than branched between.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.strategy.condor import CALL, PUT, PositionUnit, Side, UnitKind


@dataclass
class VerticalSpread(PositionUnit):
    """Two legs on one right, opened for a net debit or a net credit.

    The kind is not inferred from the sign of the fill. A structure declares
    what it was built to be, and a fill that contradicts the declaration is a
    pricing fault worth reporting -- which is a thing the runner can only say
    if the intent was recorded separately from the outcome.
    """

    kind: UnitKind = UnitKind.PUT_DEBIT_SPREAD

    def __post_init__(self) -> None:
        if not self.legs:
            return                      # a bare shell, built by restore paths
        if len(self.legs) != 2:
            raise ValueError(f"a vertical has two legs, got {len(self.legs)}")
        rights = {fl.leg.right for fl in self.legs}
        if len(rights) != 1:
            raise ValueError(f"a vertical is one right, got {sorted(rights)}")
        if {fl.leg.side for fl in self.legs} != {Side.BUY, Side.SELL}:
            raise ValueError("a vertical buys one leg and sells the other")
        if len({fl.leg.strike for fl in self.legs}) != 1:
            return
        # Both legs on one strike is a structure worth nothing that cannot lose
        # anything, which is never what was intended -- it means strike_step
        # swallowed the distance between them.
        raise ValueError("a vertical needs two different strikes")

    # ----------------------------------------------------------- the shape

    @property
    def right(self) -> str:
        return self.legs[0].leg.right

    @property
    def long_strike(self) -> float:
        return next(fl.leg.strike for fl in self.legs if fl.leg.side is Side.BUY)

    @property
    def short_strike(self) -> float:
        return next(fl.leg.strike for fl in self.legs if fl.leg.side is Side.SELL)

    @property
    def width(self) -> float:
        """Points between the two strikes, as actually traded."""
        return abs(self.long_strike - self.short_strike)

    @property
    def far_plateau_intrinsic(self) -> float:
        """What the spread is worth if both legs finish in the money.

        Signed, and that sign is what makes one formula cover every case. It is
        positive when the bought leg is the nearer one to the money -- a debit
        structure, which pays out at the far end -- and negative when the sold
        leg is nearer, which is a credit structure that pays nothing there.
        Written this way so no code below has to ask "is this a debit?", a
        question whose two branches are where the arithmetic goes wrong.
        """
        span = (
            self.long_strike - self.short_strike
            if self.right == PUT
            else self.short_strike - self.long_strike
        )
        return span * self.config.qty

    # ---------------------------------------------------------------- risk

    @property
    def _plateaus(self) -> tuple[float, float]:
        """P&L at the two ends of the payoff, where it stops changing.

        A vertical has exactly two: both legs worthless, and both in the money.
        Everything between is the straight line joining them.
        """
        costs = self.entry_costs + self.exit_costs
        near = self.credit - costs
        return near, near + self.far_plateau_intrinsic

    @property
    def max_profit(self) -> float:
        return max(self._plateaus)

    @property
    def max_loss(self) -> float:
        """Reported positive, matching the condor's convention."""
        return -min(self._plateaus)

    @property
    def is_debit(self) -> bool:
        """Whether this was opened by paying rather than by receiving."""
        return self.credit < 0

    @property
    def risk_reference(self) -> float:
        """What an exit threshold is a multiple of.

        The credit for a credit spread, the debit paid for a debit one. Always
        positive, so take-profit and stop-loss work on both rather than being
        silently switched off for half of them.
        """
        return abs(self.credit)

    @property
    def risk_reference_noun(self) -> str:
        """A bought spread has no credit, so the message must not say "credit".

        Note a stop-loss multiple above 1 can never trigger on a debit spread:
        the most it can lose is the debit, so `pnl <= -sl x debit` is out of
        reach. That is honest rather than broken -- the position simply has no
        room to lose more -- but it means a 2x stop, sensible on a condor, is
        inert here and a fraction is what does anything.
        """
        return "the debit paid" if self.is_debit else "credit"

    @property
    def breakevens(self) -> tuple[float, ...]:
        """The single point where the payoff crosses zero, if it crosses.

        One, not two: the payoff is flat, then straight, then flat, so it can
        only cross once. It crosses on the sloped segment between the strikes,
        and if the arithmetic puts the crossing outside that range the structure
        never breaks even at all -- a debit at least as large as the width, or
        costs exceeding the best case. Reporting no breakeven is the truth
        there; reporting a number off the curve would not be.
        """
        qty = self.config.qty
        if not qty:
            return ()
        high, low = max(self.long_strike, self.short_strike), min(self.long_strike, self.short_strike)
        signed_at_high = sum(
            fl.leg.signed_qty for fl in self.legs if fl.leg.strike == high
        )
        signed_at_low = sum(fl.leg.signed_qty for fl in self.legs if fl.leg.strike == low)
        net = self.credit - self.entry_costs - self.exit_costs
        if self.right == PUT:
            if not signed_at_high:
                return ()
            point = high + net / signed_at_high
        else:
            if not signed_at_low:
                return ()
            point = low - net / signed_at_low
        return (point,) if low <= point <= high else ()


def put_debit_spread_kind(right: str, *, debit: bool) -> UnitKind:
    """Name the shape, so it can be persisted and rendered."""
    if right == PUT:
        return UnitKind.PUT_DEBIT_SPREAD if debit else UnitKind.PUT_CREDIT_SPREAD
    if right == CALL:
        return UnitKind.CALL_DEBIT_SPREAD if debit else UnitKind.CALL_CREDIT_SPREAD
    raise ValueError(f"unknown right {right!r}")
