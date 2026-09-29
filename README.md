# HFT Arbitrage Lab

An educational Flask dashboard for exploring Indian equity market data and
cross-exchange price comparisons. The app uses Twelve Data on the server when
`TWELVE_DATA_API_KEY` is configured, and keeps a separate demo/simulation mode
for local exploration when no key is present.

> **Important:** This project is a market-data monitor only. It does not
> connect to a broker, place orders, or guarantee profit.

## Features

- 4-5 major Indian stocks (Reliance, TCS, Infosys, HDFC Bank, ICICI Bank by
  default) load automatically on the homepage — no search required.
- **Manual Refresh Only.** There is no background polling or timer. Market
  data is fetched once on page load and otherwise only when you click
  🔄 Manual Refresh, search/select a stock, or change a chart range.
- **A local catalog of 100+ major Indian stocks** (`data/stocks.json`) powers
  the search box. Typing/browsing that catalog **never** calls Twelve Data —
  only selecting a stock (or the 5 homepage defaults) triggers a live
  request. Search understands symbols, company names, common aliases, and
  tolerates a trailing "NSE"/"BSE" in the query (e.g. "HDFC BANK NSE"
  correctly resolves to `HDFCBANK:NSE`, not a mangled "HDFCBANKNSE").
- **Live -> cached fallback.** Every quote/chart request tries Twelve Data
  first; if it's unavailable, rate-limited, or errors out, the app
  automatically falls back to locally downloaded history
  (`data/history/<SYMBOL>.json`, produced by `download_data.py`). Every
  price and chart is tagged **LIVE DATA**, **CACHED DATA**, **DEMO DATA**, or
  **DATA UNAVAILABLE** — cached data is never shown as if it were live, and
  nothing is ever fabricated.
- See symbol, company, exchange, country, instrument type, latest price,
  change, open/high/low, previous close, volume, 52-week range and timestamp.
- Compare the latest available NSE and BSE quotes where both are supported.
- Chart intraday or historical prices for 1D, 1W, 1M, 3M and 1Y using
  Chart.js, with a small in-memory cache (server-side) and per-view cache
  (client-side) so reopening the same stock/range doesn't re-hit the API.
- Add and remove searched instruments from the in-memory watchlist.
- Market open/closed status for the regular Indian session (09:15-15:30 IST,
  weekdays).
- Keep the API key exclusively in Flask; it is never sent to frontend code.
- Friendly handling for missing keys, rate limits, invalid symbols and
  temporary API failures — the browser never shows a Python stack trace.

## Tech stack

- Python 3.9+
- Flask
- `requests`
- `python-dotenv`
- HTML5, CSS3 and vanilla JavaScript
- Chart.js from CDN
- JSON seed data and in-memory session state

No React, TypeScript, Vite, Node.js, Express, Next.js or Tailwind is used.

## Twelve Data setup

1. Create a Twelve Data account at <https://twelvedata.com/>.
2. Create or copy an API key from the Twelve Data dashboard.
3. Copy `.env.example` to `.env`:

   ```bash
   cp .env.example .env
   ```

4. Replace the placeholder value:

   ```env
   TWELVE_DATA_API_KEY=your_real_key_here
   ```

Never put the real key in `static/js/app.js`, HTML, JSON, screenshots or a
committed file. `.env` is ignored by Git.

## Installation

```bash
python -m pip install -r requirements.txt
```

## (Optional but recommended) Download historical data

Populate the local cache so the dashboard still has real data to fall back
on if Twelve Data is ever unavailable:

```bash
python download_data.py
```

This reads every stock in `data/stocks.json`, downloads its daily OHLCV
history from Twelve Data for the configured date range, and saves each one
to `data/history/<SYMBOL>.json`. It:

- throttles requests (default 8s apart) to stay under Twelve Data's free-tier
  rate limit, and retries a rate-limited stock once with a longer backoff
  before giving up on it and moving on (never an infinite loop);
- prints per-stock progress and a final Successful/Failed/Skipped summary
  with a reason for every failure;
- never writes fake or random prices — only real values returned by Twelve
  Data are saved, and a stock is skipped/reported as failed rather than
  partially filled in.

The download window is configurable via `START_DATE`/`END_DATE` at the top
of `download_data.py` (or the `HISTORY_START_DATE`/`HISTORY_END_DATE`
environment variables) — dates are not hard-coded anywhere else in the file.
Re-run the script any time to refresh the cache.

## Run

Start the application through Flask:

```bash
python app.py
```

Open <http://127.0.0.1:5000>. Do not open `templates/index.html` directly; the
page needs Flask routes for its API and static assets.

### Windows

```bat
cd /d "C:\path\to\HFT-Arbitrage-Lab"
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
copy .env.example .env
python app.py
```

If you have a Twelve Data key, edit `.env` before running the app and set
`TWELVE_DATA_API_KEY=your_real_key_here`.

## API routes

| Route | Purpose |
|---|---|
| `GET /api/status` | App mode, market status (no refresh timer — manual only) |
| `GET /api/search?q=HDFC BANK` | Search the local 100+ stock catalog (zero API calls); falls back to Twelve Data's symbol search only for names not in the catalog |
| `GET /api/quote/INFY:NSE` | Latest quote, live if possible else cached from `data/history/` |
| `GET /api/history/INFY:NSE?range=1M` | Chart history, live if possible else cached |
| `GET /api/stock/INFY:NSE?range=1M` | Quote, comparison and history together |
| `GET /api/market-data` | Quotes for the current watchlist |
| `POST /api/watchlist` | Add `{ "symbol": "INFY:NSE" }` to this server session |
| `DELETE /api/watchlist/INFY:NSE` | Remove a symbol from this server session |

The watchlist is intentionally in memory and resets when the Flask process
restarts. Add SQLite or user accounts only if persistence is needed later.
The default watchlist (`data/market_data.json` -> `default_watchlist`) is the
one place to edit which stocks appear automatically on load.

## How search works

1. Every keystroke is matched, locally and instantly, against
   `data/stocks.json` (100+ stocks: symbol, name, exchange, country, and a
   few common aliases). This is a Python dict/list scan — **zero** Twelve
   Data credits are spent no matter how much you type or how many results
   are shown.
2. A trailing "NSE" or "BSE" in the query is recognized and stripped before
   matching, and can filter the exchange, so "RELIANCE NSE" or
   "HDFC BANK NSE" resolve correctly instead of being mis-concatenated.
3. Only if the local catalog has **zero** matches does the app fall back to
   Twelve Data's `symbol_search` endpoint (cached for a few minutes) — this
   only happens for stocks outside the curated 100+ list.
4. Selecting a result adds it to the in-memory watchlist and fetches its
   live quote at that point — never before.

## How the live -> cached fallback works

For every quote or chart request:

1. If `TWELVE_DATA_API_KEY` is set, try Twelve Data first.
2. If that succeeds -> label the response **LIVE DATA**.
3. If it fails for any reason (rate limit, network error, timeout, symbol
   error) -> look for `data/history/<SYMBOL>.json`. If found, build the quote
   from the most recent saved day and label it **CACHED DATA** (the reason
   the live call failed is shown alongside it).
4. If there's no local file either -> return "Data unavailable for this
   stock." Nothing is ever invented.
5. If no API key is configured, the app first uses any downloaded local history
   as **CACHED DATA**, then uses the small seeded watchlist in
   `data/market_data.json` as clearly-labelled **DEMO DATA**. A searched stock
   with neither source remains visible as **DATA UNAVAILABLE** rather than
   receiving an invented price.

Because the downloaded history is daily (not intraday) and per-symbol
(not per-exchange), a CACHED fallback for a 1D chart shows the latest saved
day rather than true intraday points, and a CACHED NSE/BSE comparison may
show a ₹0 spread if only one exchange's history was downloaded — both are
honestly derived from real saved data, not fabricated numbers.

## Twelve Data plans and rate limits

This dashboard minimizes calls by using the local catalog for search
suggestions, caching quotes (~20s), search results (~5min) and chart history
(~90s) in memory, reusing a quote already fetched for the NSE/BSE comparison
instead of re-requesting it, and falling back to local data instead of
retrying. If a limit is still hit, the UI reports "Twelve Data API limit
reached. Please try again later." without retrying automatically or
crashing the server — and, if you've run `download_data.py`, it keeps
working off cached data instead.

## Arbitrage disclaimer

The NSE/BSE section is labelled **Educational Arbitrage Calculation**. A spread
is only a gross difference between returned reference prices. It does not
account for bid/ask spread, liquidity, taxes, brokerage, slippage, latency,
settlement or execution risk, and it is not a guaranteed profit signal. No
trades are executed by this application.
