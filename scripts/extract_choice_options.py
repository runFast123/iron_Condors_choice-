#!/usr/bin/env python3
"""
Extract the latest 15 trading days NIFTY options data directly from Choice Broker API.

Features:
- Authenticates using existing live Choice session from local engine state vault.
- Fetches 1-minute historical candles directly from Choice ChartData API.
- Generates separate CSV files for each Strike and Option Type (e.g. nifty_22600_CE.csv).
- Standardized columns: datetime, symbol, exp, strike, type, open, high, low, close, volume, oi.
- Outputs comprehensive overview table showing row counts, date ranges, and file sizes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import pathlib
import sqlite3
import sys
import time
from typing import Any, List, Optional

# Ensure project root is in sys.path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pandas as pd

from engine.auth.persistence import SessionVault
from engine.choice.session import ChoiceSession
from engine.config import ChoiceConfig, IST
from engine.data.history_store import symbol_for
from engine.data.market import ChoiceMarketData, NIFTY

logging.basicConfig(level=logging.WARNING)


def get_live_choice_session() -> ChoiceSession:
    """Load active Choice session from local engine state."""
    root = pathlib.Path(__file__).resolve().parents[1]
    db_file = root / "engine" / "state" / "engine.db"
    if not db_file.exists():
        raise FileNotFoundError(f"Engine database not found at {db_file}")

    secret = os.environ.get("ENGINE_SHARED_SECRET")
    if not secret:
        env_file = root / ".env.engine.local"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8-sig").splitlines():
                if line.startswith("ENGINE_SHARED_SECRET="):
                    secret = line.split("=", 1)[1].strip()
                    break

    if not secret:
        raise ValueError("ENGINE_SHARED_SECRET not found in environment or .env.engine.local")

    vault = SessionVault(secret)
    con = sqlite3.connect(str(db_file))
    cur = con.cursor()
    cur.execute("SELECT payload, vendor_id FROM auth_sessions ORDER BY created_at DESC LIMIT 1")
    row = cur.fetchone()
    con.close()

    if not row:
        raise ValueError("No active auth session found in engine.db")

    dec = vault.open(row[0])
    if not dec or not dec.get("session_id"):
        raise ValueError("Could not decrypt live session or session_id is missing")

    cfg = ChoiceConfig(
        vendor_id=row[1],
        api_key=dec.get("api_key", ""),
        mobile_no=dec.get("mobile_no", ""),
    )
    s = ChoiceSession(cfg)
    s.session_id = dec.get("session_id")
    s.bcast_ip = dec.get("bcast_ip")
    s.bcast_port = dec.get("bcast_port")
    s._login_date = dt.date.today()
    s.last_ok = dt.datetime.now(IST)
    return s


def format_size(size_bytes: int) -> str:
    """Format bytes into readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def strike_to_str(strike: float) -> str:
    """Format strike as integer string if whole number."""
    if strike.is_integer() or round(strike) == strike:
        return str(int(round(strike)))
    return f"{strike:g}"


def print_table(headers: List[str], rows: List[List[str]], alignments: List[str]) -> None:
    """Print ASCII table."""
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, col in enumerate(row):
            widths[idx] = max(widths[idx], len(col))

    def format_row(cols: List[str]) -> str:
        parts = []
        for col, width, align in zip(cols, widths, alignments):
            if align == "right":
                parts.append(col.rjust(width))
            elif align == "center":
                parts.append(col.center(width))
            else:
                parts.append(col.ljust(width))
        return "| " + " | ".join(parts) + " |"

    separator = "|-" + "-|-".join(["-" * w for w in widths]) + "-|"
    print(format_row(headers))
    print(separator)
    for r in rows:
        print(format_row(r))


def extract_from_choice(
    start_strike: float,
    end_strike: float,
    strike_step: float,
    option_types: List[str],
    trading_days_count: int,
    expiry_date: Optional[dt.date],
    output_dir: pathlib.Path,
) -> List[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("FETCHING LATEST NIFTY OPTIONS DATA FROM CHOICE BROKER API")
    print("=" * 80)

    # 1. Connect Choice Market Data
    session = get_live_choice_session()
    market = ChoiceMarketData.connect(session)
    today = dt.datetime.now(IST).date()

    # Determine expiry: default to nearest monthly expiry (e.g. Oct 27, 2026)
    expiries = market.master.expiries(NIFTY)
    if expiry_date is None:
        # Pick the monthly expiry for current month
        monthlies = [e for e in expiries if e.month == today.month and e.year == today.year]
        expiry_date = max(monthlies) if monthlies else expiries[0]

    # Generate strikes
    strikes = []
    curr = start_strike
    while curr <= end_strike + 1e-6:
        strikes.append(curr)
        curr += strike_step

    # Look back enough calendar days to capture requested trading sessions
    lookback_calendar_days = int(trading_days_count * 1.8) + 5
    start_date = today - dt.timedelta(days=lookback_calendar_days)

    print(f"Target Expiry:  {expiry_date} ({expiry_date.strftime('%d-%b-%Y')})")
    print(f"Today (IST):    {today}")
    print(f"Fetch Window:   {start_date} to {today}")
    print(f"Strikes:        {len(strikes)} strikes ({strike_to_str(strikes[0])} to {strike_to_str(strikes[-1])}, step {strike_to_str(strike_step)})")
    print(f"Option Types:   {', '.join(option_types)}")
    print(f"Output Dir:     {output_dir.resolve()}")
    print("-" * 80)

    results: List[dict[str, Any]] = []
    total_start = time.time()
    total_rows = 0

    for strike in strikes:
        strike_label = strike_to_str(strike)
        for opt_type in option_types:
            t0 = time.time()
            filename = f"nifty_{strike_label}_{opt_type}.csv"
            dest_file = output_dir / filename

            # Fetch 1-minute candles from Choice API
            candles = market.option_candles(
                underlying=NIFTY,
                expiry=expiry_date,
                strike=strike,
                right=opt_type,
                start=start_date,
                end=today,
                resolution="1",
            )

            if candles.empty:
                print(f"Warning: No bars returned from Choice for {strike_label} {opt_type}")
                df_out = pd.DataFrame(columns=[
                    "datetime", "symbol", "exp", "strike", "type",
                    "open", "high", "low", "close", "volume", "oi"
                ])
                cnt = 0
                min_dt = "N/A"
                max_dt = "N/A"
                days_cnt = 0
            else:
                # Filter to only the latest `trading_days_count` trading sessions
                distinct_dates = sorted(candles["ts"].dt.date.unique())
                if len(distinct_dates) > trading_days_count:
                    selected_dates = set(distinct_dates[-trading_days_count:])
                    candles = candles[candles["ts"].dt.date.isin(selected_dates)].copy()
                else:
                    selected_dates = set(distinct_dates)

                # Format DataFrame to exact schema
                sym = symbol_for(expiry_date, strike, opt_type)
                exp_str = f"{expiry_date:%Y-%m-%d} 00:00:00"
                local_ts = candles["ts"].dt.tz_convert(IST) if candles["ts"].dt.tz is not None else candles["ts"]

                df_out = pd.DataFrame({
                    "datetime": local_ts.dt.floor("min").dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol": sym,
                    "exp": exp_str,
                    "strike": int(round(strike)),
                    "type": opt_type,
                    "open": candles["open"].round(2),
                    "high": candles["high"].round(2),
                    "low": candles["low"].round(2),
                    "close": candles["close"].round(2),
                    "volume": candles["volume"].fillna(0).astype("int64"),
                    "oi": candles["oi"].fillna(0).astype("int64") if "oi" in candles else 0,
                })

                df_out = df_out.sort_values(by=["datetime", "symbol"]).reset_index(drop=True)
                cnt = len(df_out)
                min_dt = df_out["datetime"].iloc[0] if cnt > 0 else "N/A"
                max_dt = df_out["datetime"].iloc[-1] if cnt > 0 else "N/A"
                days_cnt = len(selected_dates)

            # Write to CSV
            df_out.to_csv(dest_file, index=False)
            file_size = dest_file.stat().st_size if dest_file.exists() else 0
            total_rows += cnt
            elapsed_leg = time.time() - t0

            print(f"Exported {filename:<20} | {cnt:>6,} rows | {days_cnt:>2} days | {format_size(file_size):>8} ({elapsed_leg:.2f}s)")

            results.append({
                "filename": filename,
                "filepath": str(dest_file),
                "strike": strike_label,
                "type": opt_type,
                "rows": cnt,
                "min_datetime": min_dt,
                "max_datetime": max_dt,
                "trading_days": days_cnt,
                "file_size": file_size,
            })

    total_time = time.time() - total_start
    print("=" * 80)
    print(f"Extraction completed in {total_time:.2f}s! ({len(results)} files, {total_rows:,} total rows)")
    print()

    # Print summary table
    headers = ["File Name", "Strike", "Type", "Rows", "Min Datetime", "Max Datetime", "Days", "Size"]
    alignments = ["left", "right", "center", "right", "center", "center", "right", "right"]
    table_rows = []
    for r in results:
        table_rows.append([
            r["filename"],
            r["strike"],
            r["type"],
            f"{r['rows']:,}",
            str(r["min_datetime"]),
            str(r["max_datetime"]),
            str(r["trading_days"]),
            format_size(r["file_size"]),
        ])

    print("OVERVIEW TABLE:")
    print_table(headers, table_rows, alignments)

    total_bytes = sum(r["file_size"] for r in results)
    print("-" * 80)
    print(f"Total Files: {len(results)} | Total Records: {total_rows:,} | Total Size: {format_size(total_bytes)}")
    print(f"Destination: {output_dir.resolve()}")
    print("=" * 80)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract latest N trading days NIFTY options from Choice API.")
    parser.add_argument("--start-strike", type=float, default=22600.0, help="Start strike (default: 22600)")
    parser.add_argument("--end-strike", type=float, default=23400.0, help="End strike (default: 23400)")
    parser.add_argument("--strike-step", type=float, default=100.0, help="Strike step (default: 100)")
    parser.add_argument("--types", nargs="+", default=["CE", "PE"], help="Option types (default: CE PE)")
    parser.add_argument("--days", type=int, default=15, help="Number of latest trading days (default: 15)")
    parser.add_argument("--expiry", type=str, default=None, help="Expiry YYYY-MM-DD (default: current monthly)")
    parser.add_argument(
        "--output-dir",
        type=pathlib.Path,
        default=pathlib.Path("data/options_export_choice"),
        help="Target directory (default: data/options_export_choice)",
    )

    args = parser.parse_args()
    exp_date = dt.date.fromisoformat(args.expiry) if args.expiry else None

    extract_from_choice(
        start_strike=args.start_strike,
        end_strike=args.end_strike,
        strike_step=args.strike_step,
        option_types=args.types,
        trading_days_count=args.days,
        expiry_date=exp_date,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
