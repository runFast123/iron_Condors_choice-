#!/usr/bin/env python3
"""
Extract NIFTY options 1-minute historical data from DuckDB (`nifty.db`, table `dtable`).

Features:
- Configurable strike selection (start strike, end strike, step, option types CE/PE).
- Configurable date range (latest N trading sessions, or specific end date / date window).
- Exports separate CSV files per strike and option type (e.g. `nifty_22600_CE.csv`).
- Strict column schema: datetime, symbol, exp, strike, type, open, high, low, close, volume, oi.
- Direct DuckDB streaming export for maximum performance and low memory overhead.
- Generates a comprehensive summary overview table of exported files, row counts, and date ranges.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time
from typing import Any, List, Optional, Tuple

import duckdb


def format_size(size_bytes: int) -> str:
    """Format bytes into human-readable string (KB, MB, GB)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def resolve_trading_days(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    days: int,
    end_date: Optional[str] = None,
    start_date: Optional[str] = None,
) -> Tuple[str, str, List[str]]:
    """
    Resolve the trading date range from DuckDB table.
    Returns (start_date_str, end_date_str, list_of_dates).
    """
    if start_date and end_date:
        query = f"""
            SELECT DISTINCT SUBSTRING(datetime, 1, 10) AS dt
            FROM {table}
            WHERE SUBSTRING(datetime, 1, 10) >= '{start_date}'
              AND SUBSTRING(datetime, 1, 10) <= '{end_date}'
            ORDER BY dt ASC
        """
        rows = conn.execute(query).fetchall()
        trading_dates = [r[0] for r in rows]
        if not trading_dates:
            raise ValueError(f"No trading data found between {start_date} and {end_date}")
        return trading_dates[0], trading_dates[-1], trading_dates

    # Otherwise determine by latest N trading sessions
    condition = f"WHERE SUBSTRING(datetime, 1, 10) <= '{end_date}'" if end_date else ""
    query = f"""
        SELECT DISTINCT SUBSTRING(datetime, 1, 10) AS dt
        FROM {table}
        {condition}
        ORDER BY dt DESC
        LIMIT {days}
    """
    rows = conn.execute(query).fetchall()
    if not rows:
        raise ValueError(f"No trading days found in table '{table}' with filter {condition}")
    
    trading_dates = sorted([r[0] for r in rows])
    return trading_dates[0], trading_dates[-1], trading_dates


def generate_strikes(start: float, end: float, step: float) -> List[float]:
    """Generate inclusive strike list from start to end with step."""
    if step <= 0:
        raise ValueError("Strike step must be positive")
    if start > end:
        raise ValueError("Start strike cannot be greater than end strike")

    strikes = []
    current = start
    # Using small epsilon to handle floating point rounding
    while current <= end + 1e-6:
        strikes.append(round(current, 2))
        current += step
    return strikes


def strike_to_str(strike: float) -> str:
    """Format strike as integer string if whole number, else decimal."""
    if strike.is_integer() or round(strike) == strike:
        return str(int(round(strike)))
    return f"{strike:g}"


def print_table(headers: List[str], rows: List[List[str]], alignments: List[str]) -> None:
    """Print an ASCII/Markdown-style formatted table."""
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


def extract_options(
    db_path: pathlib.Path,
    table: str,
    start_strike: float,
    end_strike: float,
    strike_step: float,
    option_types: List[str],
    days: int,
    start_date: Optional[str],
    end_date: Optional[str],
    output_dir: pathlib.Path,
) -> List[dict[str, Any]]:
    """
    Main extraction workflow.
    """
    if not db_path.is_file():
        raise FileNotFoundError(f"Database file not found: {db_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    strikes = generate_strikes(start_strike, end_strike, strike_step)
    option_types = [t.upper() for t in option_types]

    print("=" * 80)
    print("NIFTY OPTIONS EXTRACTION")
    print("=" * 80)
    print(f"Database:      {db_path.resolve()}")
    print(f"Table:         {table}")
    print(f"Strikes:       {len(strikes)} strikes ({strike_to_str(strikes[0])} to {strike_to_str(strikes[-1])}, step {strike_to_str(strike_step)})")
    print(f"Option Types:  {', '.join(option_types)}")
    print(f"Output Dir:    {output_dir.resolve()}")

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        # 1. Resolve trading date window
        first_day, last_day, trading_dates = resolve_trading_days(
            conn=conn,
            table=table,
            days=days,
            end_date=end_date,
            start_date=start_date,
        )
        print(f"Date Range:    {first_day} to {last_day} ({len(trading_dates)} trading sessions)")
        print(f"Sessions:      {', '.join(trading_dates[:3])} ... {', '.join(trading_dates[-3:])}")
        print("-" * 80)

        start_dt = f"{first_day} 00:00:00"
        end_dt = f"{last_day} 23:59:59"

        results: List[dict[str, Any]] = []
        total_start_time = time.time()
        exported_files_count = 0
        total_rows_written = 0

        for strike in strikes:
            strike_label = strike_to_str(strike)
            for opt_type in option_types:
                filename = f"nifty_{strike_label}_{opt_type}.csv"
                dest_file = output_dir / filename
                dest_posix = dest_file.resolve().as_posix()

                # Optimized DuckDB direct streaming export to CSV
                export_sql = f"""
                    COPY (
                        SELECT 
                            datetime,
                            symbol,
                            exp,
                            TRY_CAST(strike AS BIGINT) AS strike,
                            type,
                            open,
                            high,
                            low,
                            close,
                            volume,
                            TRY_CAST(oi AS BIGINT) AS oi
                        FROM {table}
                        WHERE datetime >= '{start_dt}'
                          AND datetime <= '{end_dt}'
                          AND strike = {strike}
                          AND type = '{opt_type}'
                        ORDER BY datetime ASC, symbol ASC
                    ) TO '{dest_posix}' (HEADER, DELIMITER ',')
                """

                conn.execute(export_sql)

                # Check file output and stats
                if dest_file.exists():
                    file_size = dest_file.stat().st_size
                else:
                    file_size = 0

                # Query stats for this strike/type in the date range
                stat_sql = f"""
                    SELECT 
                        COUNT(*) AS cnt,
                        MIN(datetime) AS min_dt,
                        MAX(datetime) AS max_dt,
                        COUNT(DISTINCT SUBSTRING(datetime, 1, 10)) AS days_cnt
                    FROM {table}
                    WHERE datetime >= '{start_dt}'
                      AND datetime <= '{end_dt}'
                      AND strike = {strike}
                      AND type = '{opt_type}'
                """
                stat_row = conn.execute(stat_sql).fetchone()
                cnt, min_dt, max_dt, days_cnt = stat_row if stat_row else (0, None, None, 0)

                record = {
                    "filename": filename,
                    "filepath": str(dest_file),
                    "strike": strike_label,
                    "type": opt_type,
                    "rows": cnt or 0,
                    "min_datetime": min_dt or "N/A",
                    "max_datetime": max_dt or "N/A",
                    "trading_days": days_cnt or 0,
                    "file_size": file_size,
                }
                results.append(record)
                exported_files_count += 1
                total_rows_written += (cnt or 0)

        elapsed = time.time() - total_start_time
        print(f"Export completed in {elapsed:.2f}s! ({exported_files_count} files, {total_rows_written:,} total rows)")
        print("=" * 80)
        print()

        # Display formatted overview table
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
        print(f"Total Files: {len(results)} | Total Records: {total_rows_written:,} | Total Size: {format_size(total_bytes)}")
        print(f"Destination: {output_dir.resolve()}")
        print("=" * 80)

        return results

    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract NIFTY option data from DuckDB (table dtable) into separate CSV files."
    )
    parser.add_argument(
        "--db-path",
        type=pathlib.Path,
        default=pathlib.Path("nifty.db"),
        help="Path to DuckDB database file (default: nifty.db)",
    )
    parser.add_argument(
        "--table",
        type=str,
        default="dtable",
        help="Name of table in DuckDB (default: dtable)",
    )
    parser.add_argument(
        "--start-strike",
        type=float,
        default=22600.0,
        help="Start strike price (default: 22600)",
    )
    parser.add_argument(
        "--end-strike",
        type=float,
        default=23400.0,
        help="End strike price (default: 23400)",
    )
    parser.add_argument(
        "--strike-step",
        type=float,
        default=100.0,
        help="Strike interval step (default: 100)",
    )
    parser.add_argument(
        "--types",
        nargs="+",
        default=["CE", "PE"],
        help="Option types to extract (default: CE PE)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=15,
        help="Number of latest trading sessions to extract (default: 15)",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Optional explicit start date YYYY-MM-DD",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="Optional explicit end date YYYY-MM-DD (defaults to latest available)",
    )
    parser.add_argument(
        "--output-dir",
        type=pathlib.Path,
        default=pathlib.Path("data/options_export"),
        help="Output directory to save CSV files (default: data/options_export)",
    )

    args = parser.parse_args()

    extract_options(
        db_path=args.db_path,
        table=args.table,
        start_strike=args.start_strike,
        end_strike=args.end_strike,
        strike_step=args.strike_step,
        option_types=args.types,
        days=args.days,
        start_date=args.start_date,
        end_date=args.end_date,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
