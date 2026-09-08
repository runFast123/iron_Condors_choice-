"""Performance metrics for a completed backtest."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass
from typing import Sequence

TRADING_DAYS = 252


@dataclass
class EquityPoint:
    ts: dt.datetime
    equity: float           # cumulative P&L, net of costs
    drawdown: float = 0.0
    open_condors: int = 0
    net_delta: float = 0.0
    spot: float | None = None


@dataclass
class Metrics:
    net_pnl: float = 0.0
    gross_pnl: float = 0.0
    total_costs: float = 0.0
    total_credit: float = 0.0

    condors: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    best: float = 0.0
    worst: float = 0.0

    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    cagr: float = 0.0

    max_concurrent: int = 0
    avg_days_held: float = 0.0
    capital_at_risk: float = 0.0     # peak sum of per-condor max loss

    real_price_fraction: float = 0.0
    modeled_quotes: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mu = _mean(values)
    return math.sqrt(sum((v - mu) ** 2 for v in values) / (len(values) - 1))


def drawdown_series(equity: Sequence[float]) -> list[float]:
    """Peak-to-trough drawdown at each point (negative or zero)."""
    out: list[float] = []
    peak = float("-inf")
    for value in equity:
        peak = max(peak, value)
        out.append(value - peak)
    return out


def daily_returns(points: Sequence[EquityPoint], capital: float) -> list[float]:
    """Day-over-day P&L as a fraction of deployed capital.

    A credit strategy has no natural "price series", so returns are expressed
    against the capital actually put at risk rather than against equity, which
    starts at zero and would make ratios undefined.
    """
    if not points or capital <= 0:
        return []
    by_day: dict[dt.date, float] = {}
    for point in points:
        by_day[point.ts.date()] = point.equity
    days = sorted(by_day)
    out: list[float] = []
    previous = 0.0
    for day in days:
        out.append((by_day[day] - previous) / capital)
        previous = by_day[day]
    return out


def compute(
    *,
    realised: Sequence[float],
    equity: Sequence[EquityPoint],
    total_credit: float,
    total_costs: float,
    capital_at_risk: float,
    max_concurrent: int,
    holding_days: Sequence[float] = (),
    real_price_fraction: float = 0.0,
    modeled_quotes: int = 0,
) -> Metrics:
    """Assemble the metric set from per-condor P&L and the equity curve."""
    metrics = Metrics(
        condors=len(realised),
        total_credit=total_credit,
        total_costs=total_costs,
        capital_at_risk=capital_at_risk,
        max_concurrent=max_concurrent,
        real_price_fraction=real_price_fraction,
        modeled_quotes=modeled_quotes,
    )
    if not realised:
        return metrics

    wins = [p for p in realised if p > 0]
    losses = [p for p in realised if p < 0]

    metrics.net_pnl = sum(realised)
    metrics.gross_pnl = metrics.net_pnl + total_costs
    metrics.wins = len(wins)
    metrics.losses = len(losses)
    metrics.win_rate = len(wins) / len(realised)
    metrics.avg_win = _mean(wins)
    metrics.avg_loss = _mean(losses)
    metrics.expectancy = _mean(realised)
    metrics.best = max(realised)
    metrics.worst = min(realised)

    gross_loss = abs(sum(losses))
    metrics.profit_factor = (sum(wins) / gross_loss) if gross_loss > 0 else float("inf") if wins else 0.0
    metrics.avg_days_held = _mean(list(holding_days))

    if equity:
        curve = [p.equity for p in equity]
        draws = drawdown_series(curve)
        metrics.max_drawdown = min(draws) if draws else 0.0
        if capital_at_risk > 0:
            metrics.max_drawdown_pct = metrics.max_drawdown / capital_at_risk

        rets = daily_returns(equity, capital_at_risk)
        if len(rets) > 1:
            sigma = _stdev(rets)
            mu = _mean(rets)
            metrics.sharpe = (mu / sigma) * math.sqrt(TRADING_DAYS) if sigma > 0 else 0.0
            downside = [r for r in rets if r < 0]
            dsigma = _stdev(downside) if len(downside) > 1 else 0.0
            metrics.sortino = (mu / dsigma) * math.sqrt(TRADING_DAYS) if dsigma > 0 else 0.0

        span_days = (equity[-1].ts - equity[0].ts).days
        if span_days > 0 and capital_at_risk > 0:
            total_return = metrics.net_pnl / capital_at_risk
            years = span_days / 365.0
            if years > 0 and total_return > -1:
                metrics.cagr = (1.0 + total_return) ** (1.0 / years) - 1.0
        if metrics.max_drawdown < 0:
            metrics.calmar = metrics.cagr / abs(metrics.max_drawdown_pct) if metrics.max_drawdown_pct else 0.0

    return metrics
