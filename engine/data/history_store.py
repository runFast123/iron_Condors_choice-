"""NIFTY option history in nifty.db, kept current from Choice.

nifty.db holds one-minute bars of every NIFTY option contract from January
2018 in a table called ``dtable`` -- exchange-exact: on every contract checked
its daily open, high, low and last trade equal the exchange's own figures. It
ends where it was delivered (11 Sep 2026). This module carries it forward.

Choice's candles go into a table of their own, ``choice_bars``, with the same
columns plus where each row came from, so the delivered data is never touched
and a reader can always tell the two apart. They are close but not identical:
Choice's first bar of the day, in particular, can open well away from the
exchange's official open. For closes -- what a backtest prices from -- the
two agree to the paisa on most bars.

Why daily, and why now: Choice serves history only for contracts that are
still listed. The day after a contract expires its token belongs to something
else, and its bars are gone for good. So each trading day's bars are taken
that evening, while every contract that traded that day still exists.

Rows follow ``dtable`` exactly: ``datetime`` is the minute the bar *starts*,
as text, IST (``2026-09-11 09:15:00``; Choice stamps the same bar with its
last trade, 09:15:59); ``exp`` is ``YYYY-MM-DD 00:00:00``; ``symbol`` is
``NIFTY29SEP2623300CE``; volume is in shares, as both sources give it.

Nothing here is ever uploaded anywhere; nifty.db is kept out of git.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import pathlib
import threading
from dataclasses import dataclass
from typing import Callable, Iterable

import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_DB = pathlib.Path(__file__).resolve().parents[2] / "nifty.db"
ENV_DB = "NIFTY_HISTORY_DB"

OPTIONS_TABLE = "choice_bars"
INDEX_TABLE = "choice_index_bars"
DELIVERED_TABLE = "dtable"

#: Strikes collected, as a fraction of spot either side. The delivered data
#: reaches further out on long-dated contracts, but nothing this platform
#: trades is beyond about 3% of spot, and every extra strike is a request.
STRIKE_BAND = 0.15

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

# One writer at a time within this process; DuckDB allows one writing process.
_write_lock = threading.Lock()


def db_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get(ENV_DB) or DEFAULT_DB)


def symbol_for(expiry: dt.date, strike: float, right: str) -> str:
    """``NIFTY29SEP2623300CE``, the delivered data's own naming."""
    return f"NIFTY{expiry:%d}{_MONTHS[expiry.month - 1]}{expiry:%y}{int(round(strike))}{right}"


def bar_start(ts: pd.Series) -> pd.Series:
    """Choice stamps a one-minute bar with its last trade (09:15:59); the
    delivered data stamps it with the minute it starts (09:15:00)."""
    local = ts.dt.tz_convert("Asia/Kolkata") if ts.dt.tz is not None else ts
    return local.dt.floor("min").dt.strftime("%Y-%m-%d %H:%M:%S")


def option_rows(frame: pd.DataFrame, expiry: dt.date, strike: float, right: str) -> pd.DataFrame:
    """Choice candles for one contract, as rows of the delivered table."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "datetime": bar_start(frame["ts"]),
        "open": frame["open"].astype(float),
        "high": frame["high"].astype(float),
        "low": frame["low"].astype(float),
        "close": frame["close"].astype(float),
        "volume": frame["volume"].fillna(0).astype("int64"),
        "oi": frame["oi"].astype(float) if "oi" in frame else float("nan"),
        "name": "NIFTY",
        "symbol": symbol_for(expiry, strike, right),
        "exp": f"{expiry:%Y-%m-%d} 00:00:00",
        "strike": float(strike),
        "type": right,
    })
    # Two Choice bars inside one minute (a stray print) collapse to the last.
    return out.drop_duplicates(subset=["datetime"], keep="last")


def index_rows(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "datetime": bar_start(frame["ts"]),
        "open": frame["open"].astype(float),
        "high": frame["high"].astype(float),
        "low": frame["low"].astype(float),
        "close": frame["close"].astype(float),
        "volume": frame["volume"].fillna(0).astype("int64"),
        "name": name,
    })
    return out.drop_duplicates(subset=["datetime"], keep="last")


@dataclass(frozen=True)
class Leg:
    expiry: dt.date
    strike: float
    right: str


def plan(master, spot: float, day: dt.date, *, band: float = STRIKE_BAND,
         expiries: Iterable[dt.date] | None = None) -> list[Leg]:
    """Every listed NIFTY option to collect: expiring on or after `day`,
    struck within `band` of spot. Nearest expiry first, so the one about to
    disappear is never the one a failure leaves out."""
    wanted = sorted(expiries) if expiries is not None else sorted(
        e for e in master.expiries("NIFTY") if e >= day
    )
    lo, hi = spot * (1 - band), spot * (1 + band)
    legs: list[Leg] = []
    for expiry in wanted:
        for right in ("CE", "PE"):
            for strike in master.strikes("NIFTY", expiry, right):
                if lo <= strike <= hi:
                    legs.append(Leg(expiry, float(strike), right))
    return legs


@dataclass
class CollectReport:
    legs: int = 0
    with_bars: int = 0
    rows: int = 0
    failed: int = 0
    index_rows: int = 0


def collect(
    market,
    legs: list[Leg],
    start: dt.date,
    end: dt.date,
    *,
    progress: Callable[[int, int, Leg], None] | None = None,
    write_every: int = 100,
    path: pathlib.Path | None = None,
) -> CollectReport:
    """Fetch one-minute bars for `legs` over [start, end] and append them.

    Written in batches, so a failure part-way keeps what was fetched. A leg
    Choice refuses is counted and skipped; the next run fills it if it still
    can.
    """
    from engine.choice.errors import ChoiceAuthError, ChoiceError
    from engine.data.market import INDIA_VIX, NIFTY

    report = CollectReport(legs=len(legs))
    batch: list[pd.DataFrame] = []
    for i, leg in enumerate(legs, 1):
        if progress is not None:
            progress(i, len(legs), leg)
        try:
            frame = market.option_candles(NIFTY, leg.expiry, leg.strike, leg.right, start, end, "1")
        except ChoiceAuthError:
            raise
        except ChoiceError as exc:
            report.failed += 1
            log.info("No history for %s %g %s: %s", leg.expiry, leg.strike, leg.right, exc)
            continue
        rows = option_rows(frame, leg.expiry, leg.strike, leg.right)
        if not rows.empty:
            report.with_bars += 1
            batch.append(rows)
        if len(batch) >= write_every:
            report.rows += append_options(pd.concat(batch, ignore_index=True), path)
            batch = []
    if batch:
        report.rows += append_options(pd.concat(batch, ignore_index=True), path)

    index_batch = []
    for name in (NIFTY, INDIA_VIX):
        try:
            index_batch.append(index_rows(market.index_candles(name, start, end, "1", strict=False), name))
        except ChoiceAuthError:
            raise
        except ChoiceError as exc:
            log.info("No one-minute %s for %s..%s: %s", name, start, end, exc)
    index_batch = [f for f in index_batch if not f.empty]
    if index_batch:
        report.index_rows = append_index(pd.concat(index_batch, ignore_index=True), path)
    return report


# ------------------------------------------------------------------ storage


def _connect(path: pathlib.Path | None = None, *, read_only: bool = False):
    import duckdb

    return duckdb.connect(str(path or db_path()), read_only=read_only)


def _ensure(con) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {OPTIONS_TABLE} (
            datetime VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
            volume BIGINT, oi DOUBLE, name VARCHAR, symbol VARCHAR, exp VARCHAR,
            strike DOUBLE, type VARCHAR, source VARCHAR, fetched_at VARCHAR
        )""")
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {INDEX_TABLE} (
            datetime VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
            volume BIGINT, name VARCHAR, source VARCHAR, fetched_at VARCHAR
        )""")


def _append(table: str, keys: tuple[str, ...], frame: pd.DataFrame, path: pathlib.Path | None) -> int:
    """Replace-then-insert on the natural key, in one transaction, so running
    a day twice leaves it exactly once."""
    if frame.empty:
        return 0
    frame = frame.copy()
    frame["source"] = "choice"
    frame["fetched_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    key_list = ", ".join(keys)
    with _write_lock:
        con = _connect(path)
        try:
            _ensure(con)
            con.register("incoming", frame)
            con.execute("BEGIN")
            con.execute(
                f"DELETE FROM {table} WHERE ({key_list}) IN (SELECT {key_list} FROM incoming)"
            )
            columns = ", ".join(frame.columns)
            con.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM incoming")
            con.execute("COMMIT")
            con.unregister("incoming")
        finally:
            con.close()
    return len(frame)


def append_options(frame: pd.DataFrame, path: pathlib.Path | None = None) -> int:
    return _append(OPTIONS_TABLE, ("symbol", "datetime"), frame, path)


def append_index(frame: pd.DataFrame, path: pathlib.Path | None = None) -> int:
    return _append(INDEX_TABLE, ("name", "datetime"), frame, path)


def coverage(path: pathlib.Path | None = None) -> dict:
    """What the file holds: the delivered data's end and Choice's days."""
    con = _connect(path, read_only=True)
    try:
        tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        out: dict = {}
        if DELIVERED_TABLE in tables:
            out["delivered_until"] = con.execute(f"SELECT max(datetime) FROM {DELIVERED_TABLE}").fetchone()[0]
        if OPTIONS_TABLE in tables:
            out["choice_days"] = [r[0] for r in con.execute(
                f"SELECT DISTINCT substr(datetime, 1, 10) FROM {OPTIONS_TABLE} ORDER BY 1").fetchall()]
            out["choice_rows"] = con.execute(f"SELECT count(*) FROM {OPTIONS_TABLE}").fetchone()[0]
        return out
    finally:
        con.close()
