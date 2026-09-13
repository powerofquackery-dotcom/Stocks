#!/usr/bin/env python3
"""
Scan the US stock market for low-priced tickers with two consecutive
weeks of 20%+ gains, using the Massive.com (Polygon-compatible) REST API.

Strategy
--------
For three week-ending trading dates (W0 < W1 < W2), pull the "grouped
daily" end-of-day bar for every US ticker on each date. Join the three
snapshots on ticker symbol and compute:

    pct_change_1 = (close_W1 - close_W0) / close_W0 * 100
    pct_change_2 = (close_W2 - close_W1) / close_W1 * 100

A ticker qualifies if:
    - close_W2 (current price) < price_ceiling (default $3.00)
    - pct_change_1 >= min_gain_pct (default 20%)
    - pct_change_2 >= min_gain_pct (default 20%)

i.e. it gained 20%+ in each of the last two consecutive weeks and is
still trading under the price ceiling as of the most recent week.

Usage
-----
    export MASSIVE_API_KEY=your_key_here   # (aka POLYGON_API_KEY)
    python3 scan_low_price_momentum.py --end-date 2026-09-11

    # Or supply explicit week-ending (Friday) dates:
    python3 scan_low_price_momentum.py \
        --week-dates 2026-08-28 2026-09-04 2026-09-11

Requires: requests (pip install requests)
"""

import argparse
import csv
import os
import sys
from datetime import date, timedelta

import requests

BASE_URL = "https://api.polygon.io"  # Massive.com serves a Polygon-compatible API
GROUPED_DAILY_PATH = "/v2/aggs/grouped/locale/us/market/stocks/{date}"


def previous_fridays(end_date: date, count: int) -> list[date]:
    """Return `count` Friday dates, in ascending order, ending on/before end_date."""
    d = end_date
    d -= timedelta(days=(d.weekday() - 4) % 7)  # roll back to the Friday on/before end_date
    fridays = [d - timedelta(weeks=i) for i in range(count)]
    return list(reversed(fridays))


def fetch_grouped_daily(session: requests.Session, api_key: str, trade_date: date) -> dict[str, dict]:
    """Fetch every US stock's OHLC bar for one date, keyed by ticker."""
    url = BASE_URL + GROUPED_DAILY_PATH.format(date=trade_date.isoformat())
    resp = session.get(url, params={"adjusted": "true", "apiKey": api_key}, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    results = payload.get("results", [])
    return {row["T"]: row for row in results}


def scan(
    week_dates: list[date],
    price_ceiling: float,
    min_gain_pct: float,
    api_key: str,
) -> list[dict]:
    if len(week_dates) != 3:
        raise ValueError("Exactly 3 week-ending dates are required (W0, W1, W2)")

    session = requests.Session()
    w0, w1, w2 = (fetch_grouped_daily(session, api_key, d) for d in week_dates)

    hits = []
    for ticker, bar2 in w2.items():
        bar1 = w1.get(ticker)
        bar0 = w0.get(ticker)
        if not bar1 or not bar0:
            continue

        close0, close1, close2 = bar0["c"], bar1["c"], bar2["c"]
        if not close0 or not close1 or close2 >= price_ceiling:
            continue

        pct_change_1 = (close1 - close0) / close0 * 100.0
        pct_change_2 = (close2 - close1) / close1 * 100.0

        if pct_change_1 >= min_gain_pct and pct_change_2 >= min_gain_pct:
            hits.append(
                {
                    "ticker": ticker,
                    "close_week0": close0,
                    "close_week1": close1,
                    "close_week2": close2,
                    "pct_change_week0_to_week1": round(pct_change_1, 2),
                    "pct_change_week1_to_week2": round(pct_change_2, 2),
                }
            )

    hits.sort(key=lambda r: r["pct_change_week1_to_week2"], reverse=True)
    return hits


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--end-date", type=str, help="Most recent week-ending date (YYYY-MM-DD); defaults to the most recent Friday.")
    parser.add_argument("--week-dates", type=str, nargs=3, metavar=("W0", "W1", "W2"), help="Explicit ascending week-ending dates, overrides --end-date.")
    parser.add_argument("--price-ceiling", type=float, default=3.0, help="Max current price to include (default: 3.00)")
    parser.add_argument("--min-gain-pct", type=float, default=20.0, help="Minimum weekly gain percent required in both weeks (default: 20.0)")
    parser.add_argument("--out", type=str, default=None, help="Optional path to write results as CSV")
    args = parser.parse_args()

    api_key = os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY")
    if not api_key:
        sys.exit("Set MASSIVE_API_KEY (or POLYGON_API_KEY) in the environment before running.")

    if args.week_dates:
        week_dates = [date.fromisoformat(d) for d in args.week_dates]
    else:
        end = date.fromisoformat(args.end_date) if args.end_date else date.today()
        week_dates = previous_fridays(end, 3)

    print(f"Scanning week-ending dates: {[d.isoformat() for d in week_dates]}", file=sys.stderr)

    results = scan(week_dates, args.price_ceiling, args.min_gain_pct, api_key)

    if not results:
        print("No tickers matched.")
        return

    fieldnames = list(results[0].keys())
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(results)

    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(results)
        print(f"Wrote {len(results)} rows to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
