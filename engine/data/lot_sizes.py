"""NIFTY's lot size, contract by contract.

A backtest over years used today's lot (65) for every trade. NIFTY's lot has
been 75, 50, 25, 75 and 65 since 2018, so a 2022 run overstated every rupee
figure -- P&L, max loss, peak risk, drawdown -- by 30%, and a mid-2024 run by
2.6 times, while the flat brokerage per order was spread over the wrong
quantity.

The size belongs to the contract, not the trading day: when the exchange
changes it, contracts already listed usually keep theirs (the 30 Jan 2025
monthly stayed at 25 while the weeklies around it were 75). So this is keyed
by expiry. Every boundary below was measured from the exchange's own records:
units traded (the recorded one-minute history) over contracts traded (the
exchange's daily file) for each expiry, which gives the lot size exactly --
and agrees with the exchange's circulars where those are unambiguous.
"""

from __future__ import annotations

import datetime as dt

#: (last expiry at this size, lot size), in order. Past the last row, today's
#: listed contracts say what the lot is.
_HISTORY: tuple[tuple[dt.date, int], ...] = (
    (dt.date(2021, 7, 22), 75),
    (dt.date(2024, 4, 25), 50),
    (dt.date(2024, 12, 26), 25),
    (dt.date(2025, 12, 30), 75),
)
#: Contracts that kept an earlier size after the change around them.
_EXCEPTIONS: dict[dt.date, int] = {
    dt.date(2025, 1, 30): 25,        # the Jan 2025 monthly, listed before the change
}
#: From the 6 Jan 2026 weekly and the 27 Jan 2026 monthly.
_LATEST = 65


def nifty_lot_size(expiry: dt.date, current: int | None = None, today: dt.date | None = None) -> int:
    """The NIFTY lot size of the contract expiring on `expiry`.

    `current` is today's listed lot size; it answers for contracts still
    listed, so a future revision is followed without editing this table.
    """
    today = today or dt.date.today()
    if current is not None and expiry >= today:
        return int(current)
    if expiry in _EXCEPTIONS:
        return _EXCEPTIONS[expiry]
    for last, size in _HISTORY:
        if expiry <= last:
            return size
    return _LATEST
