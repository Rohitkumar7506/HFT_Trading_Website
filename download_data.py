"""Download historical daily OHLCV data from Twelve Data for the local
stock catalog (data/stocks.json) and save it to data/history/<SYMBOL>.json.

This is a standalone script - it does not import app.py and does not start
Flask. It only ever writes real values returned by Twelve Data; it never
invents prices, and it never silently drops a stock without reporting it.

Usage:

    python download_data.py

Configuration (edit the constants below, or override with environment
variables of the same name - no dates are hard-coded anywhere else in this
file):

    START_DATE, END_DATE          the historical window to request
    REQUEST_DELAY_SECONDS         pause between requests (rate-limit safety)
    MAX_RETRIES_ON_RATE_LIMIT     how many times to retry a single stock
                                   after a 429 before giving up on it
    RETRY_BACKOFF_SECONDS         how long to wait before each retry
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# --- Configuration ---------------------------------------------------------
# Change these two to adjust the download window. Nothing else in this file
# hard-codes a date.
START_DATE = os.getenv("HISTORY_START_DATE", "2026-01-01")
END_DATE = os.getenv("HISTORY_END_DATE", "2026-09-26")

INTERVAL = "1day"
TWELVE_DATA_BASE_URL = "https://api.twelvedata.com"

# Twelve Data's free tier is commonly limited to ~8 requests/minute. A
# multi-second delay between symbols keeps a 100+ stock run well under that
# without needing to lean on retries.
REQUEST_DELAY_SECONDS = float(os.getenv("TWELVE_DATA_REQUEST_DELAY", "8"))
MAX_RETRIES_ON_RATE_LIMIT = int(os.getenv("TWELVE_DATA_MAX_RETRIES", "1"))
RETRY_BACKOFF_SECONDS = float(os.getenv("TWELVE_DATA_RETRY_BACKOFF", "65"))
REQUEST_TIMEOUT_SECONDS = 15

STOCKS_FILE = BASE_DIR / "data" / "stocks.json"
HISTORY_DIR = BASE_DIR / "data" / "history"


class DownloadError(Exception):
    """A specific, human-readable reason a single stock failed."""


def load_catalog() -> list[dict[str, Any]]:
    with STOCKS_FILE.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    stocks = raw if isinstance(raw, list) else raw.get("stocks", [])
    if not stocks:
        raise SystemExit(f"No stocks found in {STOCKS_FILE}. Nothing to download.")
    return stocks


def fetch_time_series(
    session: requests.Session, api_key: str, symbol: str, exchange: str
) -> dict[str, Any]:
    """Call Twelve Data's /time_series endpoint for one symbol.

    Raises DownloadError with a specific, human-readable reason on any
    failure (network error, HTTP error, API error, rate limit, empty
    result). Never returns fabricated data.
    """
    params = {
        "apikey": api_key,
        "symbol": f"{symbol}:{exchange}",
        "interval": INTERVAL,
        "start_date": START_DATE,
        "end_date": END_DATE,
        "order": "asc",
    }
    try:
        response = session.get(
            f"{TWELVE_DATA_BASE_URL}/time_series",
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise DownloadError("Request timed out") from exc
    except requests.RequestException as exc:
        raise DownloadError(f"Network error ({exc.__class__.__name__})") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise DownloadError("Invalid JSON response") from exc

    is_rate_limited = response.status_code == 429 or (
        isinstance(payload, dict) and payload.get("code") in (429, "429")
    )
    if is_rate_limited:
        raise DownloadError("RATE_LIMIT")

    if response.status_code >= 400 or (isinstance(payload, dict) and payload.get("status") == "error"):
        message = payload.get("message") if isinstance(payload, dict) else None
        raise DownloadError(message or f"HTTP {response.status_code}")

    values = payload.get("values") if isinstance(payload, dict) else None
    if not values:
        raise DownloadError("No historical data returned for this symbol/date range")

    return payload


def normalize_points(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep only real, complete OHLCV rows. Never fills in a missing value."""
    points = []
    for row in payload.get("values", []):
        try:
            point = {
                "datetime": row["datetime"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(float(row["volume"])) if row.get("volume") not in (None, "") else None,
            }
        except (KeyError, TypeError, ValueError):
            # Skip a malformed row rather than inventing values for it.
            continue
        points.append(point)
    points.sort(key=lambda p: p["datetime"])
    return points


def save_history(symbol: str, points: list[dict[str, Any]]) -> Path:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    out_path = HISTORY_DIR / f"{symbol}.json"
    document = {
        "symbol": symbol,
        "source": "Twelve Data",
        "interval": INTERVAL,
        "start_date": START_DATE,
        "end_date": END_DATE,
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "data": points,
    }
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return out_path


def download_one(session: requests.Session, api_key: str, entry: dict[str, Any]) -> tuple[bool, str]:
    """Returns (success, message)."""
    symbol = entry["symbol"]
    exchange = entry.get("exchange", "NSE")

    attempts = 0
    while True:
        attempts += 1
        try:
            payload = fetch_time_series(session, api_key, symbol, exchange)
        except DownloadError as exc:
            if str(exc) == "RATE_LIMIT" and attempts <= MAX_RETRIES_ON_RATE_LIMIT:
                print(
                    f"      rate limited - waiting {RETRY_BACKOFF_SECONDS:.0f}s before retry "
                    f"({attempts}/{MAX_RETRIES_ON_RATE_LIMIT})..."
                )
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            reason = "Twelve Data API limit reached" if str(exc) == "RATE_LIMIT" else str(exc)
            return False, reason

        points = normalize_points(payload)
        if not points:
            return False, "No usable OHLCV rows in the response"

        save_history(symbol, points)
        return True, f"{len(points)} days saved"


def main() -> int:
    api_key = os.getenv("TWELVE_DATA_API_KEY", "").strip()
    if not api_key:
        print("TWELVE_DATA_API_KEY is not set in .env - nothing to download.")
        print("Copy .env.example to .env and add your real Twelve Data key first.")
        return 1

    stocks = load_catalog()
    total = len(stocks)
    print("Starting historical data download...")
    print(f"Catalog: {STOCKS_FILE} ({total} stocks)")
    print(f"Range:   {START_DATE} to {END_DATE} ({INTERVAL})")
    print(f"Output:  {HISTORY_DIR}\n")

    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    successes: list[str] = []
    failures: list[tuple[str, str]] = []

    for index, entry in enumerate(stocks, start=1):
        symbol = entry["symbol"]
        label = f"[{index}/{total}] {symbol}"
        padding = "." * max(3, 24 - len(symbol))
        ok, message = download_one(session, api_key, entry)
        if ok:
            successes.append(symbol)
            print(f"{label} {padding} SUCCESS ({message})")
        else:
            failures.append((symbol, message))
            print(f"{label} {padding} FAILED  ({message})")

        if index < total:
            time.sleep(REQUEST_DELAY_SECONDS)

    print("\nDownload complete.\n")
    print(f"Successful: {len(successes)}")
    print(f"Failed:     {len(failures)}")
    print(f"Skipped:    0\n")
    print(f"Files saved to:\n{HISTORY_DIR}\n")

    if failures:
        print("Failures:")
        for symbol, reason in failures:
            print(f"  - {symbol}: {reason}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
