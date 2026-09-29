"""HFT Arbitrage Lab - real-data Flask dashboard.

The application uses Twelve Data when TWELVE_DATA_API_KEY is configured.
Without a key it deliberately stays in a clearly-labelled demo mode so the
dashboard can still be explored offline. It never places trades.

Refresh model: there is NO background polling. The frontend fetches data
once on load and otherwise only when the user clicks "Refresh", searches,
selects a stock, or changes a chart range. A small in-memory cache further
cuts down on repeated Twelve Data requests (and helps avoid rate limits).

Search: the search box is backed entirely by the local catalog in
data/stocks.json (100+ major Indian stocks). Browsing/typing through that
catalog never calls Twelve Data. A live quote is only ever requested for a
stock the user actually selects (or the 5 default homepage stocks).

Live -> cached fallback: every quote/history request tries Twelve Data
first (when configured) and, on any failure (rate limit, network error,
timeout, etc.), falls back to locally downloaded data in data/history/
(produced by running download_data.py). Every quote/chart response is
tagged with its real source - LIVE, CACHED or DEMO - and the frontend
displays that tag; cached data is never presented as live.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

with (BASE_DIR / "data" / "market_data.json").open(encoding="utf-8") as handle:
    SEED = json.load(handle)

THRESHOLD = float(SEED.get("threshold", 0.75))
TRANSACTION_COST = float(SEED.get("transaction_cost", 0.30))
ASSETS = SEED.get("assets", [])
# The homepage watchlist is intentionally a short, editable list (4-5 major
# Indian stocks), separate from the larger demo/simulation asset list above.
DEFAULT_WATCHLIST = SEED.get("default_watchlist") or [asset["symbol"] for asset in ASSETS][:5]

# --- Local stock catalog (data/stocks.json) -------------------------------
# 100+ major Indian stocks used for instant, API-free search suggestions.
# Entries use a bare symbol ("RELIANCE") plus its exchange, per the catalog
# file format; composite "SYMBOL:EXCHANGE" ids (used everywhere else in the
# app, e.g. for quotes/watchlist) are built from these two fields.
with (BASE_DIR / "data" / "stocks.json").open(encoding="utf-8") as handle:
    _stocks_raw = json.load(handle)
STOCKS: list[dict[str, Any]] = _stocks_raw if isinstance(_stocks_raw, list) else _stocks_raw.get("stocks", [])
STOCKS_BY_BASE: dict[str, dict[str, Any]] = {item["symbol"].upper(): item for item in STOCKS}

# --- Local historical data (data/history/<BASE>.json) ---------------------
# Produced by running download_data.py. Used as the fallback source whenever
# a live Twelve Data request fails.
HISTORY_DIR = BASE_DIR / "data" / "history"

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
TWELVE_DATA_BASE_URL = "https://api.twelvedata.com"
INDIA_TZ = ZoneInfo("Asia/Kolkata")
START_TIME = time.time()
MAX_HISTORY_POINTS = 80

# --- Simple in-memory response cache -------------------------------------
# Cuts down duplicate Twelve Data calls (e.g. reopening the same chart
# range, or the comparison panel re-requesting a quote already fetched)
# without letting data go stale for long. Not shared across processes -
# fine for this single-instance educational deployment.
_CACHE: dict[str, tuple[float, Any]] = {}
QUOTE_CACHE_TTL = 20        # seconds
HISTORY_CACHE_TTL = 90      # seconds
SEARCH_CACHE_TTL = 300      # seconds
LOCAL_HISTORY_FILE_CACHE_TTL = 3600  # seconds - local files don't change while the app runs


def cache_get(key: str, ttl: float) -> Any | None:
    entry = _CACHE.get(key)
    if entry and (time.time() - entry[0]) < ttl:
        return entry[1]
    return None


def cache_set(key: str, value: Any) -> None:
    _CACHE[key] = (time.time(), value)


class MarketDataError(Exception):
    """A friendly, user-facing market-data failure."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def now_ist() -> datetime:
    return datetime.now(INDIA_TZ)


def market_status() -> dict[str, Any]:
    """Return the weekday NSE/BSE regular-session status.

    Exchange holidays are not bundled into this small educational app, so a
    weekday is treated as a regular session day.
    """

    current = now_ist()
    session_start = dt_time(9, 15)
    session_end = dt_time(15, 30)
    is_open = current.weekday() < 5 and session_start <= current.time() <= session_end
    if current.weekday() >= 5:
        reason = "Weekend"
    elif current.time() < session_start:
        reason = "Before regular session"
    elif current.time() > session_end:
        reason = "After regular session"
    else:
        reason = "Regular session"
    return {
        "open": is_open,
        "label": "MARKET OPEN" if is_open else "MARKET CLOSED",
        "timezone": "Asia/Kolkata",
        "local_time": current.isoformat(),
        "session": "09:15–15:30 IST",
        "reason": reason,
    }


def as_float(value: Any) -> float | None:
    try:
        if value in (None, "", "null", "None"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def money(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def display_time(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), INDIA_TZ).isoformat()
        except (ValueError, OSError, OverflowError):
            return str(value)
    return str(value)


class TwelveDataClient:
    """Very small server-side wrapper around the Twelve Data REST API."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        query = {"apikey": self.api_key, **params}
        try:
            response = self.session.get(
                f"{TWELVE_DATA_BASE_URL}/{endpoint.lstrip('/')}",
                params=query,
                timeout=12,
            )
        except requests.RequestException as exc:
            raise MarketDataError("Unable to refresh market data.") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketDataError("Unable to refresh market data.") from exc

        error_text = (
            payload.get("message") or payload.get("code")
            if isinstance(payload, dict)
            else None
        )
        api_rate_limited = (
            isinstance(payload, dict) and payload.get("code") in (429, "429")
        )
        if response.status_code == 429 or api_rate_limited:
            raise MarketDataError("Twelve Data API limit reached. Please try again later.", 429)
        if response.status_code >= 400 or (
            isinstance(payload, dict) and payload.get("status") == "error"
        ):
            if error_text and "symbol" in str(error_text).lower():
                raise MarketDataError("No matching stock found.", 404)
            raise MarketDataError("Unable to refresh market data.", response.status_code or 502)
        return payload


CLIENT = TwelveDataClient(TWELVE_DATA_API_KEY) if TWELVE_DATA_API_KEY else None

# Demo state is only initialized/used when no API key exists.
DEMO_STATE: dict[str, dict[str, Any]] = {}
for asset in ASSETS:
    base = float(asset["base_price"])
    history = []
    current = base
    for _ in range(20):
        current = round(current + random.uniform(-3, 3), 2)
        history.append(current)
    DEMO_STATE[asset["symbol"]] = {"price": base, "history": history}

# The server-side watchlist starts as the default 4-5 major stocks and is
# mutated only by explicit add/remove requests from the frontend.
WATCHLIST = list(DEFAULT_WATCHLIST)


def mode_payload() -> dict[str, Any]:
    real = CLIENT is not None
    return {
        "mode": "REAL" if real else "DEMO",
        "real_data": real,
        "source": "Twelve Data API" if real else "Educational demo data",
        "auto_refresh": False,
        "refresh_mode": "manual",
        "notice": (
            "Latest available market data; not a live trading feed."
            if real and not market_status()["open"]
            else (
                "Real market data is not configured. Please add your Twelve Data API key."
                if not real
                else "Real market data from Twelve Data."
            )
        ),
    }


def demo_quote(symbol: str) -> dict[str, Any]:
    asset = next((item for item in ASSETS if item["symbol"] == symbol), None)
    if not asset:
        raise MarketDataError("No matching stock found.", 404)
    base = float(asset["base_price"])
    state = DEMO_STATE.setdefault(symbol, {"price": base, "history": [base]})
    last = float(state["price"])
    new_price = round(max(base * 0.95, min(base * 1.05, last + random.uniform(-2.5, 2.5))), 2)
    state["price"] = new_price
    state["history"].append(new_price)
    state["history"] = state["history"][-MAX_HISTORY_POINTS:]
    change = round(new_price - base, 2)
    pct = round(change / base * 100, 2) if base else 0
    return {
        "symbol": symbol,
        "name": asset["name"],
        "exchange": asset.get("exchange", "NSE"),
        "country": "India",
        "instrument_type": "Equity",
        "price": new_price,
        "change": change,
        "percent_change": pct,
        "open": base,
        "high": new_price,
        "low": new_price,
        "previous_close": base,
        "volume": None,
        "fifty_two_week_high": None,
        "fifty_two_week_low": None,
        "last_updated": now_ist().isoformat(),
        "market": market_status(),
        "history": [
            {"datetime": now_ist().isoformat(), "price": price}
            for price in state["history"]
        ],
        "is_demo": True,
    }


def unavailable_quote(symbol: str) -> dict[str, Any]:
    """Keep a catalog instrument visible without inventing a price.

    Search covers more instruments than the small offline demo seed. When a
    searched instrument has neither a configured live source nor a downloaded
    history file, return a complete unavailable row instead of dropping it
    from the watchlist or fabricating a quote.
    """
    base = symbol.split(":")[0]
    catalog_entry = STOCKS_BY_BASE.get(base, {})
    exchange = symbol.split(":")[1].upper() if ":" in symbol else catalog_entry.get("exchange", "NSE")
    return {
        "symbol": symbol,
        "name": catalog_entry.get("name", base),
        "exchange": exchange,
        "country": catalog_entry.get("country", "India"),
        "instrument_type": catalog_entry.get("instrument_type", "Common Stock"),
        "price": None,
        "change": None,
        "percent_change": None,
        "open": None,
        "high": None,
        "low": None,
        "previous_close": None,
        "volume": None,
        "fifty_two_week_high": None,
        "fifty_two_week_low": None,
        "last_updated": None,
        "market": market_status(),
        "is_demo": False,
        "data_source": "UNAVAILABLE",
        "fallback_reason": "No live or cached data is available for this stock.",
    }


def normalize_quote(payload: dict[str, Any], requested_symbol: str | None = None) -> dict[str, Any]:
    weekly = payload.get("fifty_two_week") or {}
    symbol = payload.get("symbol") or requested_symbol or ""
    price = as_float(payload.get("close") or payload.get("price"))
    change = as_float(payload.get("change"))
    pct = as_float(payload.get("percent_change"))
    previous_close = as_float(payload.get("previous_close"))
    if change is None and price is not None and previous_close is not None:
        change = price - previous_close
    if pct is None and change is not None and previous_close:
        pct = change / previous_close * 100
    return {
        "symbol": symbol,
        "name": payload.get("name") or symbol.split(":")[0],
        "exchange": payload.get("exchange") or payload.get("mic_code") or "—",
        "country": payload.get("country") or "India",
        "instrument_type": payload.get("type") or payload.get("instrument_type") or "Common Stock",
        "price": money(price),
        "change": money(change),
        "percent_change": money(pct),
        "open": money(as_float(payload.get("open"))),
        "high": money(as_float(payload.get("high"))),
        "low": money(as_float(payload.get("low"))),
        "previous_close": money(previous_close),
        "volume": as_float(payload.get("volume")),
        "fifty_two_week_high": money(as_float(weekly.get("high") or payload.get("fifty_two_week_high"))),
        "fifty_two_week_low": money(as_float(weekly.get("low") or payload.get("fifty_two_week_low"))),
        "last_updated": display_time(payload.get("datetime") or payload.get("timestamp")) or now_ist().isoformat(),
        "market": market_status(),
        "is_demo": False,
    }


def real_quote(symbol: str, use_cache: bool = True) -> dict[str, Any]:
    if CLIENT is None:
        return demo_quote(symbol)
    cache_key = f"quote:{symbol}"
    if use_cache:
        cached = cache_get(cache_key, QUOTE_CACHE_TTL)
        if cached is not None:
            return cached
    payload = CLIENT.get("quote", {"symbol": symbol})
    quote = normalize_quote(payload, symbol)
    if quote["price"] is None:
        raise MarketDataError("No matching stock found.", 404)
    cache_set(cache_key, quote)
    return quote


def _prioritize_india(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort so genuine NSE/BSE Indian common stocks come before unrelated
    international securities that happen to share a name (Requirement 7)."""

    def rank(item: dict[str, Any]) -> tuple[int, int, int]:
        country = str(item.get("country") or "").strip().lower()
        exchange = str(item.get("exchange") or "").strip().upper()
        instrument_type = str(item.get("instrument_type") or "").strip().lower()
        country_score = 0 if country == "india" else 1
        exchange_score = 0 if exchange in ("NSE", "BSE") else 1
        type_score = 0 if instrument_type in ("common stock", "equity") else 1
        return (country_score, exchange_score, type_score)

    return sorted(results, key=rank)


_EXCHANGE_TOKENS = {"NSE", "BSE"}


def _normalize_text(value: str) -> str:
    """Collapse whitespace and uppercase, for tolerant text matching."""
    return re.sub(r"\s+", " ", str(value or "").strip()).upper()


def _split_exchange_suffix(query: str) -> tuple[str, str | None]:
    """Split a trailing "NSE"/"BSE" token off a search query.

    "HDFC BANK NSE" -> ("HDFC BANK", "NSE"). This is what stops a query
    like that from being wrongly concatenated into "HDFCBANKNSE" when
    matched against the catalog, and lets us filter by exchange too.
    """
    tokens = query.strip().split()
    exchange = None
    while tokens and tokens[-1].upper() in _EXCHANGE_TOKENS:
        exchange = tokens.pop().upper()
    return " ".join(tokens).strip(), exchange


def local_catalog_search(query: str) -> list[dict[str, Any]]:
    """Search the local 100+ stock catalog. Zero API calls, ever."""
    cleaned, exchange_filter = _split_exchange_suffix(query)
    if not cleaned:
        return []
    q = _normalize_text(cleaned)
    q_compact = q.replace(" ", "")

    matches = []
    for item in STOCKS:
        symbol = _normalize_text(item["symbol"])
        name = _normalize_text(item["name"])
        aliases = [_normalize_text(a) for a in item.get("aliases", [])]
        haystacks = {symbol, symbol.replace(" ", ""), name, name.replace(" ", "")}
        haystacks.update(aliases)
        haystacks.update(a.replace(" ", "") for a in aliases)
        if any(q in h or q_compact in h for h in haystacks if h):
            if exchange_filter and item.get("exchange", "").upper() != exchange_filter:
                continue
            matches.append(item)
    return _prioritize_india(matches)


def _to_search_result(item: dict[str, Any]) -> dict[str, Any]:
    """Build a composite "SYMBOL:EXCHANGE" search result from a catalog entry."""
    exchange = str(item.get("exchange", "NSE")).upper()
    return {
        "symbol": f"{item['symbol'].upper()}:{exchange}",
        "name": item.get("name", item["symbol"]),
        "exchange": exchange,
        "country": item.get("country", "India"),
        "instrument_type": item.get("instrument_type", "Common Stock"),
    }


def search_assets(query: str) -> list[dict[str, Any]]:
    if not query:
        return []

    # 1) Always check the local 100+ stock catalog first. This costs zero
    #    API credits and covers the vast majority of searches for this app
    #    (Requirements 3 and 17).
    local_matches = [_to_search_result(item) for item in local_catalog_search(query)[:20]]
    if local_matches or CLIENT is None:
        return local_matches

    # Nothing in the local catalog matched - only now do we spend a Twelve
    # Data credit, and only for this specific unmatched query.
    cleaned, _ = _split_exchange_suffix(query)
    lookup_term = cleaned or query
    cache_key = f"search:{lookup_term.lower()}"
    cached = cache_get(cache_key, SEARCH_CACHE_TTL)
    if cached is not None:
        return cached

    payload = CLIENT.get("symbol_search", {"symbol": lookup_term})
    results = (
        payload
        if isinstance(payload, list)
        else payload.get("data", [])
        if isinstance(payload, dict)
        else []
    )
    normalized = [
        {
            "symbol": item.get("symbol"),
            "name": item.get("instrument_name") or item.get("name") or item.get("symbol"),
            "exchange": item.get("exchange") or item.get("mic_code") or "—",
            "country": item.get("country") or "—",
            "instrument_type": item.get("instrument_type") or item.get("type") or "—",
        }
        for item in results
        if item.get("symbol")
    ]
    prioritized = _prioritize_india(normalized)[:30]
    cache_set(cache_key, prioritized)
    return prioritized


# --- Local historical-data fallback (data/history/<BASE>.json) -----------

def load_history_file(base_symbol: str) -> dict[str, Any] | None:
    """Load a downloaded history file for a bare symbol (e.g. "RELIANCE").

    Returns None if the file doesn't exist or can't be parsed - this is a
    normal, expected case (the user hasn't run download_data.py for that
    stock yet), never an error.
    """
    cache_key = f"localhist-file:{base_symbol}"
    cached = cache_get(cache_key, LOCAL_HISTORY_FILE_CACHE_TTL)
    if cached is not None:
        return cached or None

    path = HISTORY_DIR / f"{base_symbol}.json"
    if not path.is_file():
        cache_set(cache_key, {})
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        cache_set(cache_key, {})
        return None
    if not isinstance(payload, dict) or not payload.get("data"):
        cache_set(cache_key, {})
        return None
    cache_set(cache_key, payload)
    return payload


def quote_from_local_history(symbol: str) -> dict[str, Any] | None:
    """Build a normalized quote from the most recent locally saved data
    point for `symbol`, or None if no local history file exists."""
    base = symbol.split(":")[0]
    payload = load_history_file(base)
    if not payload:
        return None
    points = sorted(
        (p for p in payload["data"] if p.get("datetime") is not None),
        key=lambda p: p["datetime"],
    )
    if not points:
        return None
    last = points[-1]
    prev = points[-2] if len(points) > 1 else None
    price = as_float(last.get("close"))
    if price is None:
        return None
    previous_close = as_float(prev.get("close")) if prev else None
    change = round(price - previous_close, 4) if previous_close is not None else None
    pct = round(change / previous_close * 100, 4) if change is not None and previous_close else None
    catalog_entry = STOCKS_BY_BASE.get(base, {})
    exchange = symbol.split(":")[1].upper() if ":" in symbol else catalog_entry.get("exchange", "NSE")
    return {
        "symbol": symbol,
        "name": catalog_entry.get("name", base),
        "exchange": exchange,
        "country": catalog_entry.get("country", "India"),
        "instrument_type": catalog_entry.get("instrument_type", "Common Stock"),
        "price": money(price),
        "change": money(change),
        "percent_change": money(pct),
        "open": money(as_float(last.get("open"))),
        "high": money(as_float(last.get("high"))),
        "low": money(as_float(last.get("low"))),
        "previous_close": money(previous_close),
        "volume": as_float(last.get("volume")),
        "fifty_two_week_high": None,
        "fifty_two_week_low": None,
        "last_updated": last.get("datetime"),
        "market": market_status(),
        "is_demo": False,
    }


def local_history_slice(symbol: str, range_name: str) -> list[dict[str, Any]]:
    """Approximate a chart range from locally saved daily OHLCV data.

    The downloaded data is daily, so 1D falls back to the single latest
    saved day rather than true intraday points.
    """
    base = symbol.split(":")[0]
    payload = load_history_file(base)
    if not payload:
        return []
    points = sorted(
        (p for p in payload["data"] if p.get("datetime") and as_float(p.get("close")) is not None),
        key=lambda p: p["datetime"],
    )
    take = {"1D": 1, "1W": 7, "1M": 22, "3M": 66, "1Y": 252}.get(range_name.upper(), 22)
    selected = points[-take:] if take else points
    return [{"datetime": p["datetime"], "price": as_float(p.get("close"))} for p in selected]


def get_quote_with_fallback(symbol: str) -> dict[str, Any]:
    """Try a live Twelve Data quote; on failure, fall back to locally
    downloaded history. Always tags the result with its real source so the
    frontend never presents cached data as live (Requirements 11 and 18)."""
    if CLIENT is None:
        cached = quote_from_local_history(symbol)
        if cached is not None:
            cached["data_source"] = "CACHED"
            cached["fallback_reason"] = "Live data is not configured."
            return cached
        try:
            quote = demo_quote(symbol)
            quote["data_source"] = "DEMO"
            quote["fallback_reason"] = None
            return quote
        except MarketDataError:
            return unavailable_quote(symbol)

    try:
        quote = real_quote(symbol)
        quote["data_source"] = "LIVE"
        quote["fallback_reason"] = None
        return quote
    except MarketDataError as exc:
        cached = quote_from_local_history(symbol)
        if cached is not None:
            cached["data_source"] = "CACHED"
            cached["fallback_reason"] = exc.message
            return cached
        unavailable = unavailable_quote(symbol)
        unavailable["fallback_reason"] = exc.message
        return unavailable


def get_history_with_fallback(symbol: str, range_name: str) -> dict[str, Any]:
    """Try live Twelve Data history; on failure, fall back to a locally
    downloaded slice. Returns {"data", "source", "message"}."""
    if CLIENT is None:
        local_data = local_history_slice(symbol, range_name)
        if local_data:
            note = (
                "Showing the latest locally saved daily price (intraday history "
                "is not available offline)."
                if range_name.upper() == "1D"
                else "Showing locally saved daily history because live data is not configured."
            )
            return {"data": local_data, "source": "CACHED", "message": note}
        try:
            return {"data": demo_quote(symbol)["history"], "source": "DEMO", "message": None}
        except MarketDataError:
            return {
                "data": [],
                "source": "UNAVAILABLE",
                "message": "Historical chart data is currently unavailable.",
            }

    try:
        data = history_for(symbol, range_name)
        if not data:
            raise MarketDataError("Historical data unavailable for this range.")
        return {"data": data, "source": "LIVE", "message": None}
    except MarketDataError:
        local_data = local_history_slice(symbol, range_name)
        if local_data:
            note = (
                "Showing the latest locally saved daily price (intraday history "
                "is not available offline)."
                if range_name.upper() == "1D"
                else None
            )
            return {"data": local_data, "source": "CACHED", "message": note}
        return {
            "data": [],
            "source": "UNAVAILABLE",
            "message": "Historical chart data is currently unavailable.",
        }


def history_for(symbol: str, range_name: str = "1M") -> list[dict[str, Any]]:
    if CLIENT is None:
        return demo_quote(symbol)["history"]

    range_key = range_name.upper()
    cache_key = f"history:{symbol}:{range_key}"
    cached = cache_get(cache_key, HISTORY_CACHE_TTL)
    if cached is not None:
        return cached

    settings = {
        "1D": ("5min", 78),
        "1W": ("1day", 7),
        "1M": ("1day", 31),
        "3M": ("1day", 93),
        "1Y": ("1day", 260),
    }
    interval, outputsize = settings.get(range_key, settings["1M"])
    payload = CLIENT.get(
        "time_series",
        {"symbol": symbol, "interval": interval, "outputsize": outputsize, "order": "asc"},
    )
    values = payload.get("values", [])
    points = [
        {
            "datetime": item.get("datetime"),
            "price": as_float(item.get("close")),
        }
        for item in values
        if as_float(item.get("close")) is not None
    ]
    cache_set(cache_key, points)
    return points


def compare_exchanges(symbol: str, selected_quote: dict[str, Any]) -> dict[str, Any]:
    base = symbol.split(":")[0]
    requested_exchange = symbol.split(":")[1].upper() if ":" in symbol else None

    if CLIENT is None:
        if selected_quote.get("data_source") == "UNAVAILABLE" or selected_quote.get("price") is None:
            return _comparison(None, None)
        price = selected_quote["price"]
        nse = round(price + random.uniform(-1.2, 1.2), 2)
        bse = round(price + random.uniform(-1.2, 1.2), 2)
        return _comparison(nse, bse)

    exchange_quotes: dict[str, dict[str, Any]] = {}
    # Reuse the quote we already fetched for the selected symbol instead of
    # requesting the same exchange twice (Requirement 15).
    if requested_exchange in ("NSE", "BSE") and selected_quote.get("price") is not None:
        exchange_quotes[requested_exchange] = selected_quote

    for exchange in ("NSE", "BSE"):
        if exchange in exchange_quotes:
            continue
        try:
            exchange_quotes[exchange] = get_quote_with_fallback(f"{base}:{exchange}")
        except MarketDataError:
            continue
    nse = exchange_quotes.get("NSE", {}).get("price")
    bse = exchange_quotes.get("BSE", {}).get("price")
    return _comparison(nse, bse)


def _comparison(nse: float | None, bse: float | None) -> dict[str, Any]:
    if nse is None or bse is None:
        return {
            "available": False,
            "nse_price": nse,
            "bse_price": bse,
            "price_difference": None,
            "spread_percent": None,
            "potential_gross_difference": None,
            "message": "BSE data unavailable for this instrument." if nse is not None
            else "Both exchange quotes are not available for this instrument.",
        }
    difference = round(abs(nse - bse), 4)
    cheaper = min(nse, bse)
    return {
        "available": True,
        "nse_price": nse,
        "bse_price": bse,
        "price_difference": difference,
        "spread_percent": round(difference / cheaper * 100, 4) if cheaper else None,
        "potential_gross_difference": difference,
        "direction": "Buy NSE / Sell BSE" if nse < bse else "Buy BSE / Sell NSE",
    }


def enrich_quote(quote: dict[str, Any]) -> dict[str, Any]:
    comparison = compare_exchanges(quote["symbol"], quote)
    quote = {**quote, "comparison": comparison}
    if quote["price"] is not None and comparison.get("available"):
        spread = comparison["price_difference"]
        quote.update(
            {
                "spread": spread,
                "net_edge": round(spread - TRANSACTION_COST, 4),
                "signal": (
                    "OPPORTUNITY"
                    if spread >= THRESHOLD and spread > TRANSACTION_COST
                    else "WATCH"
                    if spread >= THRESHOLD
                    else "MONITORING"
                ),
            }
        )
    else:
        quote.update({"spread": None, "net_edge": None, "signal": "UNAVAILABLE"})
    return quote


def fetch_watchlist(symbols: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    quotes: list[dict[str, Any]] = []
    errors: list[str] = []
    for symbol in symbols:
        try:
            quotes.append(enrich_quote(get_quote_with_fallback(symbol)))
        except MarketDataError as exc:
            errors.append(exc.message)
    return quotes, errors


@app.route("/")
def home():
    return render_template("index.html")


@app.get("/download")
def download_project():
    """Download the reconstructed Flask project without local secrets."""
    archive = BASE_DIR.parent / "hft-arbitrage-lab.zip"
    if not archive.is_file():
        return jsonify({"error": "Project download is not available yet."}), 404
    return send_file(
        archive,
        as_attachment=True,
        download_name="hft-arbitrage-lab.zip",
        mimetype="application/zip",
    )


@app.get("/api/status")
def api_status():
    status = market_status()
    return jsonify(
        {
            **mode_payload(),
            "market": status,
            "tracked_assets": len(WATCHLIST),
            "threshold": THRESHOLD,
            "transaction_cost": TRANSACTION_COST,
            "uptime_seconds": int(time.time() - START_TIME),
            "server_time": now_ist().isoformat(),
        }
    )


@app.get("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return jsonify({"data": [], "message": "Enter at least 2 characters."})
    try:
        return jsonify({"query": query, "data": search_assets(query)})
    except MarketDataError as exc:
        return jsonify({"error": exc.message}), exc.status_code


@app.get("/api/quote/<path:symbol>")
def api_quote(symbol: str):
    try:
        return jsonify(enrich_quote(get_quote_with_fallback(symbol.upper())))
    except MarketDataError as exc:
        return jsonify({"error": exc.message}), exc.status_code


@app.get("/api/history/<path:symbol>")
def api_history(symbol: str):
    range_name = request.args.get("range", "1M").upper()
    result = get_history_with_fallback(symbol.upper(), range_name)
    return jsonify(
        {
            "symbol": symbol.upper(),
            "range": range_name,
            "data": result["data"],
            "history_source": result["source"],
            "history_message": result["message"],
            **mode_payload(),
        }
    )


@app.get("/api/stock/<path:symbol>")
def api_stock(symbol: str):
    try:
        normalized = symbol.upper()
        quote = enrich_quote(get_quote_with_fallback(normalized))
        range_name = request.args.get("range", "1M")
        history = get_history_with_fallback(normalized, range_name)
        quote["history"] = history["data"]
        quote["history_source"] = history["source"]
        quote["history_message"] = history["message"]
        return jsonify(quote)
    except MarketDataError as exc:
        return jsonify({"error": exc.message}), exc.status_code


@app.get("/api/market-data")
def api_market_data():
    requested = request.args.get("symbols")
    symbols = [item.strip().upper() for item in requested.split(",") if item.strip()] if requested else WATCHLIST
    quotes, errors = fetch_watchlist(symbols[:30])
    response = {
        **mode_payload(),
        "threshold": THRESHOLD,
        "transaction_cost": TRANSACTION_COST,
        "timestamp": now_ist().isoformat(),
        "count": len(quotes),
        "data": quotes,
        "errors": sorted(set(errors)),
    }
    if not quotes and errors:
        response["error"] = errors[0]
    return jsonify(response), 200 if quotes or not errors else 502


@app.route("/api/watchlist", methods=["GET", "POST"])
def api_watchlist():
    global WATCHLIST
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        symbol = str(payload.get("symbol", "")).strip().upper()
        if not symbol:
            return jsonify({"error": "A stock symbol is required."}), 400
        if symbol not in WATCHLIST:
            WATCHLIST.append(symbol)
        return jsonify({"symbols": WATCHLIST})
    return jsonify({"symbols": WATCHLIST})


@app.delete("/api/watchlist/<path:symbol>")
def api_remove_watchlist(symbol: str):
    global WATCHLIST
    symbol = symbol.upper()
    WATCHLIST = [item for item in WATCHLIST if item != symbol]
    return jsonify({"symbols": WATCHLIST})


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "No matching stock found."}), 404
    return error


@app.errorhandler(500)
def internal_error(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Unable to refresh market data."}), 500
    return error


# Catch-all safety net: any unexpected exception on an /api/ route becomes a
# clean JSON error instead of a Python stack trace in the browser
# (Requirement 17). Flask still logs the real exception to the console.
@app.errorhandler(Exception)
def unhandled_error(error):
    if request.path.startswith("/api/"):
        app.logger.exception("Unhandled error on %s", request.path)
        return jsonify({"error": "Unable to refresh market data."}), 500
    raise error


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )
