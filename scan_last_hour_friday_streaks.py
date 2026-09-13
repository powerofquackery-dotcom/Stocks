#!/usr/bin/env python3
"""
Scan US stocks for tickers whose last hour of trading (3:00-4:00pm ET)
moved $2.00 or more, up or down, on 4 or more CONSECUTIVE Fridays, using
the Massive.com (Polygon-compatible) REST API.

Strategy
--------
For each candidate ticker, pull hourly OHLC bars spanning the requested
Friday window in one call (multiplier=1, timespan=hour). For every Friday
in the window, find the hourly bar whose start time is 3:00pm US/Eastern
(the last hour of the regular session) and compute:

    last_hour_move = hour_close - hour_open

A Friday "qualifies" for a ticker if abs(last_hour_move) >= --min-move
(default $2.00). A ticker is reported if it has a run of 4 or more
qualifying Fridays in a row (configurable via --min-streak), with no
non-qualifying Friday breaking the run.

Universe
--------
By default the scan runs over a curated list of ~100 liquid, large/mid-cap
US common stocks (see DEFAULT_UNIVERSE below) to keep runtime and API
usage reasonable. You can instead supply your own list with --tickers, or
build one automatically from the most recent Friday's grouped-daily bars
with --auto-universe (filtered to common stock/ADR tickers via the
reference/tickers endpoint, ranked by dollar volume, capped at
--max-tickers). Scanning the entire market (8,000+ tickers) is possible
with --auto-universe and a high --max-tickers, but will make many API
calls and take a while - mind your plan's rate limits.

Usage
-----
    export MASSIVE_API_KEY=your_key_here   # (aka POLYGON_API_KEY)

    # Curated default universe, last 10 Fridays ending on the most recent one:
    python3 scan_last_hour_friday_streaks.py

    # Explicit end date and lookback window:
    python3 scan_last_hour_friday_streaks.py --end-date 2026-09-11 --weeks 12

    # Your own ticker list:
    python3 scan_last_hour_friday_streaks.py --tickers AAPL MSFT NVDA

    # Auto-built universe of the 300 most liquid common stocks/ADRs:
    python3 scan_last_hour_friday_streaks.py --auto-universe --max-tickers 300

Requires: requests (pip install requests)
"""

import argparse
import csv
import os
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://api.polygon.io"  # Massive.com serves a Polygon-compatible API
GROUPED_DAILY_PATH = "/v2/aggs/grouped/locale/us/market/stocks/{date}"
HOURLY_AGGS_PATH = "/v2/aggs/ticker/{ticker}/range/1/hour/{from_}/{to}"
REFERENCE_TICKERS_PATH = "/v3/reference/tickers"

EASTERN = ZoneInfo("America/New_York")
LAST_HOUR_START = 15  # 3:00pm ET - start of the final regular-session hour

DEFAULT_UNIVERSE = [
    "MU", "NVDA", "AAPL", "SNDK", "ORCL", "SPCX", "TSLA", "META", "AMD", "INTC",
    "GOOGL", "DELL", "AVGO", "MSFT", "AMZN", "GOOG", "TSM", "MRVL", "BE", "STX",
    "LITE", "KHC", "SKHY", "UNH", "LRCX", "ADBE", "PLTR", "MSTR", "NBIS", "HPE",
    "CRM", "WDC", "COHR", "HOOD", "MRNA", "QCOM", "LLY", "AMAT", "JPM", "XOM",
    "SMCI", "WMT", "UBER", "BAC", "NFLX", "PG", "COST", "CSCO", "BMNR", "CRWV",
    "PANW", "ASML", "CAT", "SNOW", "NOW", "TXN", "CVX", "SHOP", "CRWD", "COIN",
    "KLAC", "V", "IREN", "JNJ", "MRK", "ISRG", "VRT", "APP", "ACVA", "GS",
    "IBM", "PEP", "VLO", "WFC", "MA", "BSX", "ARM", "GEV", "T", "ABBV",
    "ADI", "TMO", "HD", "MCD", "C", "SYK", "AZO", "PM", "BKNG", "SWKS",
    "RKLB", "NOK", "NU", "CRDO", "CRCL", "HPQ", "MPC", "ANET", "APH", "TER",
    "DE", "CLS", "KO", "BA", "ON", "AMGN", "HWM", "ABT",
]


def previous_fridays(end_date: date, count: int) -> list[date]:
    """Return `count` calendar Fridays, ascending, ending on/before end_date."""
    d = end_date
    d -= timedelta(days=(d.weekday() - 4) % 7)  # roll back to the Friday on/before end_date
    fridays = [d - timedelta(weeks=i) for i in range(count)]
    return list(reversed(fridays))


def api_get(session: requests.Session, api_key: str, path: str, params: dict) -> dict:
    params = dict(params)
    params["apiKey"] = api_key
    resp = session.get(BASE_URL + path, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_grouped_daily(session: requests.Session, api_key: str, trade_date: date) -> dict[str, dict]:
    payload = api_get(session, api_key, GROUPED_DAILY_PATH.format(date=trade_date.isoformat()),
                       {"adjusted": "true"})
    return {row["T"]: row for row in payload.get("results", [])}


def fetch_common_stock_tickers(session: requests.Session, api_key: str) -> set[str]:
    """Page through reference/tickers to collect active common-stock and ADR symbols."""
    tickers = set()
    for asset_type in ("CS", "ADRC"):
        params = {"type": asset_type, "market": "stocks", "active": "true", "limit": 1000}
        path = REFERENCE_TICKERS_PATH
        while True:
            payload = api_get(session, api_key, path, params)
            for row in payload.get("results", []):
                tickers.add(row["ticker"])
            next_url = payload.get("next_url")
            if not next_url:
                break
            # next_url already carries the cursor; strip the base URL and re-issue via api_get
            path = next_url.replace(BASE_URL, "")
            params = {}
    return tickers


def build_auto_universe(session: requests.Session, api_key: str, as_of: date, max_tickers: int) -> list[str]:
    grouped = fetch_grouped_daily(session, api_key, as_of)
    common_stocks = fetch_common_stock_tickers(session, api_key)
    ranked = sorted(
        (row for T, row in grouped.items() if T in common_stocks and row.get("c") and row.get("v")),
        key=lambda row: row["c"] * row["v"],
        reverse=True,
    )
    return [row["T"] for row in ranked[:max_tickers]]


def fetch_hourly_bars(session: requests.Session, api_key: str, ticker: str,
                       from_date: date, to_date: date) -> list[dict]:
    bars = []
    path = HOURLY_AGGS_PATH.format(ticker=ticker, from_=from_date.isoformat(), to=to_date.isoformat())
    params = {"adjusted": "true", "sort": "asc", "limit": 50000}
    while True:
        payload = api_get(session, api_key, path, params)
        bars.extend(payload.get("results", []))
        next_url = payload.get("next_url")
        if not next_url:
            break
        path = next_url.replace(BASE_URL, "")
        params = {}
    return bars


def last_hour_moves_by_friday(bars: list[dict], fridays: list[date]) -> dict[date, float]:
    """Map each Friday to its 3:00-4:00pm ET close-minus-open, if that bar exists."""
    friday_set = set(fridays)
    moves = {}
    for bar in bars:
        dt_et = datetime.fromtimestamp(bar["t"] / 1000, tz=EASTERN)
        if dt_et.date() in friday_set and dt_et.hour == LAST_HOUR_START:
            moves[dt_et.date()] = bar["c"] - bar["o"]
    return moves


def longest_qualifying_streak(fridays: list[date], moves: dict[date, float], min_move: float):
    best_len, best_start, best_end = 0, None, None
    cur_len, cur_start = 0, None
    for d in fridays:
        move = moves.get(d)
        qualifies = move is not None and abs(move) >= min_move
        if qualifies:
            if cur_len == 0:
                cur_start = d
            cur_len += 1
            if cur_len > best_len:
                best_len, best_start, best_end = cur_len, cur_start, d
        else:
            cur_len = 0
    return best_len, best_start, best_end


def scan(tickers: list[str], fridays: list[date], min_move: float, min_streak: int,
         api_key: str, delay: float) -> list[dict]:
    session = requests.Session()
    from_date, to_date = fridays[0] - timedelta(days=3), fridays[-1]
    hits = []
    for i, ticker in enumerate(tickers, 1):
        try:
            bars = fetch_hourly_bars(session, api_key, ticker, from_date, to_date)
        except requests.HTTPError as exc:
            print(f"  [{i}/{len(tickers)}] {ticker}: skipped ({exc})", file=sys.stderr)
            continue
        moves = last_hour_moves_by_friday(bars, fridays)
        streak_len, streak_start, streak_end = longest_qualifying_streak(fridays, moves, min_move)
        print(f"  [{i}/{len(tickers)}] {ticker}: best streak = {streak_len}", file=sys.stderr)
        if streak_len >= min_streak:
            streak_dates = [d for d in fridays if streak_start <= d <= streak_end]
            hits.append({
                "ticker": ticker,
                "streak_len": streak_len,
                "streak_start": streak_start.isoformat(),
                "streak_end": streak_end.isoformat(),
                "moves": [round(moves[d], 2) for d in streak_dates],
            })
        if delay:
            time.sleep(delay)
    hits.sort(key=lambda r: r["streak_len"], reverse=True)
    return hits


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--end-date", type=str, help="Most recent Friday to scan through (YYYY-MM-DD); defaults to the most recent Friday on/before today.")
    parser.add_argument("--weeks", type=int, default=10, help="Number of consecutive Fridays to examine, ending at --end-date (default: 10).")
    parser.add_argument("--min-move", type=float, default=2.0, help="Minimum absolute last-hour dollar move to qualify a Friday (default: 2.00).")
    parser.add_argument("--min-streak", type=int, default=4, help="Minimum consecutive qualifying Fridays required to report a ticker (default: 4).")
    parser.add_argument("--tickers", type=str, nargs="+", help="Explicit ticker list to scan, overrides the default/auto universe.")
    parser.add_argument("--auto-universe", action="store_true", help="Build the universe automatically from the most recent Friday's grouped-daily bars, ranked by dollar volume.")
    parser.add_argument("--max-tickers", type=int, default=200, help="Cap on universe size when using --auto-universe (default: 200).")
    parser.add_argument("--delay", type=float, default=0.0, help="Seconds to sleep between per-ticker API calls, to stay under rate limits (default: 0).")
    parser.add_argument("--out", type=str, default=None, help="Optional path to write results as CSV")
    args = parser.parse_args()

    api_key = os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY")
    if not api_key:
        sys.exit("Set MASSIVE_API_KEY (or POLYGON_API_KEY) in the environment before running.")

    end = date.fromisoformat(args.end_date) if args.end_date else date.today()
    fridays = previous_fridays(end, args.weeks)

    session = requests.Session()
    if args.tickers:
        tickers = args.tickers
    elif args.auto_universe:
        print(f"Building auto-universe as of {fridays[-1]} (top {args.max_tickers} by dollar volume)...", file=sys.stderr)
        tickers = build_auto_universe(session, api_key, fridays[-1], args.max_tickers)
    else:
        tickers = DEFAULT_UNIVERSE

    print(f"Scanning {len(tickers)} tickers across Fridays: {[d.isoformat() for d in fridays]}", file=sys.stderr)
    print(f"Rule: last-hour (3:00-4:00pm ET) move >= ${args.min_move:.2f}, {args.min_streak}+ consecutive Fridays", file=sys.stderr)

    results = scan(tickers, fridays, args.min_move, args.min_streak, api_key, args.delay)

    if not results:
        print("No tickers matched.")
        return

    fieldnames = ["ticker", "streak_len", "streak_start", "streak_end", "moves"]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    for row in results:
        writer.writerow(row)

    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for row in results:
                w.writerow(row)
        print(f"Wrote {len(results)} rows to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
