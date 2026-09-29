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
import re
import threading
from dataclasses import dataclass
from typing import Callable, Iterable

import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_DB = pathlib.Path(__file__).resolve().parents[2] / "nifty.db"
ENV_DB = "NIFTY_HISTORY_DB"

OPTIONS_TABLE = "choice_bars"
INDEX_TABLE = "choice_index_bars"
#: One row per finished collection. A day is done when it is recorded here,
#: not when some of its bars exist: bars are written in batches, so a run cut
#: short leaves a day part-filled.
COLLECTIONS_TABLE = "choice_collections"
DELIVERED_TABLE = "dtable"

#: Strikes collected, as a fraction of spot either side. The delivered data
#: reaches further out on long-dated contracts, but nothing this platform
#: trades is beyond about 3% of spot, and every extra strike is a request.
STRIKE_BAND = 0.15

_SYMBOL = re.compile(r"NIFTY\d{2}[A-Z]{3}\d{2}\d+(CE|PE)")
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
        reports = getattr(market, "reports", None)
        mark = len(reports) if reports is not None else 0
        try:
            frame = market.option_candles(NIFTY, leg.expiry, leg.strike, leg.right, start, end, "1")
        except ChoiceAuthError:
            raise
        except ChoiceError as exc:
            report.failed += 1
            log.info("No history for %s %g %s: %s", leg.expiry, leg.strike, leg.right, exc)
            continue
        # A window Choice failed to serve comes back as no bars, not an
        # error. Counted as a failure, or an outage at collection time would
        # read as a closed day and the day would never be asked for again.
        if reports is not None and any(r.windows_failed for r in reports[mark:]):
            report.failed += 1
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
        CREATE TABLE IF NOT EXISTS {COLLECTIONS_TABLE} (
            first_day VARCHAR, last_day VARCHAR, legs BIGINT, with_bars BIGINT,
            rows BIGINT, failed BIGINT, finished_at VARCHAR
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
    columns = ", ".join(frame.columns)

    def write(con) -> None:
        _ensure(con)
        con.register("incoming", frame)
        con.execute("BEGIN")
        con.execute(f"DELETE FROM {table} WHERE ({key_list}) IN (SELECT {key_list} FROM incoming)")
        con.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM incoming")
        con.execute("COMMIT")
        con.unregister("incoming")

    _with_connection(write, path or db_path(), read_only=False)
    return len(frame)


def append_options(frame: pd.DataFrame, path: pathlib.Path | None = None) -> int:
    return _append(OPTIONS_TABLE, ("symbol", "datetime"), frame, path)


def append_index(frame: pd.DataFrame, path: pathlib.Path | None = None) -> int:
    return _append(INDEX_TABLE, ("name", "datetime"), frame, path)


# ------------------------------------------------------------------ reading

#: The last bar a backtest may price from: 15:29 holds the session's closing
#: trades. The bars after it (to 15:40) are the closing session, not trading
#: a strategy could have done.
LAST_BAR = "15:29:00"
#: Another process (a backfill) may hold the file for writing; DuckDB then
#: refuses readers. Short waits cover a batch being written.
READ_ATTEMPTS = 8
READ_WAIT = 1.5


# What DuckDB says when another process holds the file: Windows' sharing
# violation, or the lock conflict it reports elsewhere.
_LOCKED = re.compile(r"being used by another process|could not set lock|conflicting lock", re.IGNORECASE)


class HistoryUnavailable(Exception):
    """nifty.db is absent, locked or unreadable."""


def available(path: pathlib.Path | None = None) -> bool:
    return (path or db_path()).is_file()


def option_bars(
    legs: Iterable[tuple[dt.date, float, str, dt.date]],
    end: dt.date,
    *,
    path: pathlib.Path | None = None,
) -> dict[tuple[dt.date, float, str], pd.DataFrame]:
    """One-minute bars for each ``(expiry, strike, right, from_day)`` through
    `end`, keyed by ``(expiry, strike, right)``.

    The delivered table first; Choice's own rows only for the days after it
    ends, so the two never overlap. Frames carry ``ts`` (IST) at each bar's
    last second -- the convention Choice's candles use, so an as-of lookup
    treats both alike -- and ``close``. Bars after LAST_BAR are left out.
    One query for every leg: the table is not ordered by contract, so each
    query is a scan and a query per leg would take minutes.
    """
    path = path or db_path()
    if not available(path):
        raise HistoryUnavailable("no recorded history on this machine")
    # A contract filed under its pre-holiday expiry for part of its life is
    # read under both names.
    aliases = _calendar(path)[1]
    keys: dict[str, tuple[dt.date, float, str]] = {}
    starts: dict[str, dt.date] = {}
    for e, k, r, d in legs:
        for label in [e, *aliases.get(e, [])]:
            symbol = symbol_for(label, k, r)
            keys[symbol] = (e, float(k), r)
            starts[symbol] = min(d, starts.get(symbol, d))
    if not keys:
        return {}
    symbols = list(keys)
    if not all(_SYMBOL.fullmatch(s) for s in symbols):
        raise ValueError("unexpected option symbol")
    since = f"{min(starts.values()):%Y-%m-%d} 00:00:00"
    until = f"{end:%Y-%m-%d} 23:59:59"
    # The symbols as an inline table, each with a small number, and only that
    # number, the bar's time and its close come back: an all-data run reads
    # millions of bars, and as text columns they took gigabytes. Not a pandas
    # frame registered with DuckDB, nor a UNION of the two tables: both hung
    # DuckDB 1.5 outright. One date range for all legs; each leg's own start
    # is applied here.
    values = ", ".join(f"('{s}', {i})" for i, s in enumerate(symbols))
    select = ("SELECT v.leg, strptime(t.datetime, '%Y-%m-%d %H:%M:%S') AS bar, t.close "
              "FROM {table} t JOIN (VALUES " + values + ") v(symbol, leg) ON t.symbol = v.symbol "
              f"WHERE t.datetime >= '{since}' AND t.datetime <= '{until}' "
              f"AND substr(t.datetime, 12) <= '{LAST_BAR}' AND t.close > 0")

    def read(con) -> pd.DataFrame:
        tables = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
        parts = []
        delivered_until = None
        if DELIVERED_TABLE in tables:
            parts.append(con.execute(select.format(table=DELIVERED_TABLE)).df())
            delivered_until = con.execute(f"SELECT max(datetime) FROM {DELIVERED_TABLE}").fetchone()[0]
        if OPTIONS_TABLE in tables:
            after = f" AND t.datetime > '{delivered_until}'" if delivered_until else ""
            parts.append(con.execute(select.format(table=OPTIONS_TABLE) + after).df())
        parts = [part for part in parts if not part.empty]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

    rows = _with_connection(read, path, read_only=True)
    if rows.empty:
        return {}
    first = pd.to_datetime(pd.Series([starts[s] for s in symbols])).to_numpy()
    rows = rows[rows["bar"].to_numpy() >= first[rows["leg"].to_numpy()]]
    # Stamped at the bar's last second, as Choice stamps its candles.
    frame = pd.DataFrame({
        "leg": rows["leg"].to_numpy(),
        "ts": (rows["bar"] + pd.Timedelta(seconds=59)).dt.tz_localize("Asia/Kolkata").reset_index(drop=True),
        "close": rows["close"].to_numpy(),
    })
    frame["key"] = frame["leg"].map(lambda i: keys[symbols[int(i)]])
    out: dict[tuple[dt.date, float, str], pd.DataFrame] = {}
    for key, bars in frame.groupby("key", sort=False):
        out[key] = (bars[["ts", "close"]].sort_values("ts", kind="stable")
                    .drop_duplicates("ts", keep="last").reset_index(drop=True))
    return out


_calendar_cache: dict[tuple[str, float], tuple[list[dt.date], dict[dt.date, list[dt.date]]]] = {}

#: How far a holiday, or the 2025 switch from Thursday to Tuesday, moved an
#: expiry -- and how soon after the old name stops the new one must start.
MOVED_WITHIN = dt.timedelta(days=10)
RENAMED_WITHIN = dt.timedelta(days=5)


def _calendar(path: pathlib.Path) -> tuple[list[dt.date], dict[dt.date, list[dt.date]]]:
    """The real expiries, and for each the labels its contracts were first
    filed under.

    When an expiry moves -- a holiday, or the switch from Thursday to Tuesday
    in September 2025 -- contracts listed before the move keep their original
    date until the exchange renames them: the March 2026 monthlies were
    NIFTY31MAR26... until 26 Dec 2025 and NIFTY30MAR26... from 29 Dec. A label
    is such a stale name when the same strikes carry on under a nearby label,
    starting right where it stopped. A label that merely stops early -- a gap
    in what was recorded -- stays a real expiry.
    """
    key = (str(path), path.stat().st_mtime)
    if key not in _calendar_cache:
        def read(con) -> list[tuple]:
            tables = {r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables").fetchall()}
            out: list[tuple] = []
            for table in (DELIVERED_TABLE, OPTIONS_TABLE):
                if table in tables:
                    out += con.execute(
                        f"SELECT exp, strike, CAST(type AS VARCHAR), min(datetime), max(datetime) "
                        f"FROM {table} WHERE type <> 'F' GROUP BY 1, 2, 3").fetchall()
            return out

        spans: dict[dt.date, dict[tuple[float, str], tuple[dt.datetime, dt.datetime]]] = {}
        for exp, strike, right, first, last in _with_connection(read, path, read_only=True):
            if not exp:
                continue
            label = dt.date.fromisoformat(str(exp)[:10])
            k = (float(strike), str(right))
            f, l = dt.datetime.fromisoformat(str(first)), dt.datetime.fromisoformat(str(last))
            if k in spans.setdefault(label, {}):
                f0, l0 = spans[label][k]
                f, l = min(f, f0), max(l, l0)
            spans[label][k] = (f, l)

        today = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date()
        renamed: dict[dt.date, dt.date] = {}
        for label, contracts in spans.items():
            if label >= today or max(l for _, l in contracts.values()).date() >= label:
                continue                               # traded to its own expiry
            ended = max(l for _, l in contracts.values())
            for other, theirs in spans.items():
                if other == label or abs(other - label) > MOVED_WITHIN:
                    continue
                began = min(f for f, _ in theirs.values())
                shared = sum(1 for k in contracts if k in theirs)
                # The new name starts right where the old one stopped, and
                # carries most of its strikes.
                if dt.timedelta(0) < began - ended <= RENAMED_WITHIN and shared >= len(contracts) / 2:
                    renamed[label] = other
                    break

        real = sorted(e for e in spans if e not in renamed)
        aliases: dict[dt.date, list[dt.date]] = {}
        for label, target in sorted(renamed.items()):
            while target in renamed:                  # renamed twice
                target = renamed[target]
            aliases.setdefault(target, []).append(label)
        _calendar_cache.clear()
        _calendar_cache[key] = (real, aliases)
    return _calendar_cache[key]


def expiries(path: pathlib.Path | None = None) -> list[dt.date]:
    """Every real NIFTY option expiry the recorded history holds -- the
    exchange's calendar since 2018, holiday moves and the old Thursday expiries
    included, stale pre-move labels left out. Choice's dated contract lists
    before about 2022 are refused, so for those years this is the only record
    of which dates were real.

    Empty when there is no file. Cached until the file changes.
    """
    path = path or db_path()
    if not available(path):
        return []
    return list(_calendar(path)[0])


def _with_connection(work, path: pathlib.Path, *, read_only: bool):
    """Run `work(con)` on the file, waiting out another process that holds it.

    On Windows DuckDB locks the file for the whole life of a connection, so a
    backfill writing from one process and a backtest reading in the engine
    turn each other away. Within one process `_write_lock` orders them.
    """
    import time as _time

    last_error: Exception | None = None
    for attempt in range(READ_ATTEMPTS):
        try:
            with _write_lock:
                con = _connect(path, read_only=read_only)
                try:
                    return work(con)
                finally:
                    con.close()
        except Exception as exc:                # noqa: BLE001 - locked by another process, most likely
            if not _LOCKED.search(str(exc)):
                raise
            last_error = exc
            _time.sleep(READ_WAIT * (attempt + 1))
    # The message reaches the dashboard; the path and process stay in the log.
    log.warning("History file stayed locked: %s", last_error)
    raise HistoryUnavailable("the recorded history was in use by another process") from last_error


def mark_collected(first: dt.date, last: dt.date, report: CollectReport,
                   path: pathlib.Path | None = None) -> None:
    """Record that [first, last] was collected in full."""
    def write(con) -> None:
        _ensure(con)
        con.execute(
            f"INSERT INTO {COLLECTIONS_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?)",
            [first.isoformat(), last.isoformat(), report.legs, report.with_bars, report.rows,
             report.failed, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")],
        )

    _with_connection(write, path or db_path(), read_only=False)


def coverage(path: pathlib.Path | None = None) -> dict:
    """What the file holds: the delivered data's end and Choice's days."""
    return _with_connection(_coverage, path or db_path(), read_only=True)


def _coverage(con) -> dict:
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    out: dict = {}
    if DELIVERED_TABLE in tables:
        out["delivered_until"] = con.execute(f"SELECT max(datetime) FROM {DELIVERED_TABLE}").fetchone()[0]
    if OPTIONS_TABLE in tables:
        out["choice_days"] = [r[0] for r in con.execute(
            f"SELECT DISTINCT substr(datetime, 1, 10) FROM {OPTIONS_TABLE} ORDER BY 1").fetchall()]
        out["choice_rows"] = con.execute(f"SELECT count(*) FROM {OPTIONS_TABLE}").fetchone()[0]
    if COLLECTIONS_TABLE in tables:
        out["collected_until"] = con.execute(
            f"SELECT max(last_day) FROM {COLLECTIONS_TABLE}").fetchone()[0]
    return out
