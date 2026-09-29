/* HFT Arbitrage Lab - vanilla frontend. API keys never enter this file. */
let quotes = [];
let selectedResult = null;
let activeSymbol = null;
let activeRange = "1M";
let assetChart = null;
let searchDebounceTimer = null;

// Client-side cache for stock detail + chart requests so reopening the
// same stock/range shortly after does not re-hit the Flask/Twelve Data
// APIs (Requirement 14). Key: "SYMBOL|RANGE" -> { time, data }.
const stockCache = new Map();
const STOCK_CACHE_TTL_MS = 45 * 1000;
const SEARCH_DEBOUNCE_MS = 350;

const $ = (id) => document.getElementById(id);
const money = (value) => value == null || Number.isNaN(Number(value)) ? "N/A" : `₹${Number(value).toLocaleString("en-IN", {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
const signedPercent = (value) => value == null ? "N/A" : `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)}%`;
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char]));

// LIVE data is never presented as CACHED and vice versa — this is the one
// place that label text/styling is decided, so every badge in the app
// (watchlist rows, detail modal, chart) stays consistent.
const SOURCE_LABEL = { LIVE: "LIVE DATA", CACHED: "CACHED DATA", DEMO: "DEMO DATA", UNAVAILABLE: "DATA UNAVAILABLE" };
const SOURCE_CLASS = { LIVE: "live", CACHED: "cached", DEMO: "demo", UNAVAILABLE: "unavailable" };
function sourceBadge(source) {
  const key = source || "UNAVAILABLE";
  const label = SOURCE_LABEL[key] || key;
  const cls = SOURCE_CLASS[key] || "unavailable";
  return `<span class="sourceBadge ${cls}" title="${escapeHtml(label)}">${escapeHtml(label)}</span>`;
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || "Unable to refresh market data.");
  return payload;
}

function setNotice(message, error = false) {
  const node = $("notice");
  node.textContent = message || "";
  node.classList.toggle("hidden", !message);
  node.classList.toggle("error", error);
}

function updateStatus(status) {
  const market = status.market || {};
  $("marketBadge").classList.toggle("open", Boolean(market.open));
  $("marketBadge").classList.toggle("closed", !market.open);
  $("marketBadge").querySelector("span:last-child").textContent = market.label || "MARKET CLOSED";
  $("modeBadge").innerHTML = `● <span>${status.real_data ? "REAL MARKET DATA MODE" : "DEMO / SIMULATION MODE"}</span>`;
  $("refreshRate").textContent = "Manual";
  $("marketSession").textContent = `${market.timezone || "India"} · ${market.session || "09:15–15:30 IST"}`;
  $("dataDescription").textContent = status.real_data
    ? `${status.notice || "Real market data from Twelve Data."} API credentials stay on the Flask server.`
    : "Real market data is not configured. The app is using isolated demo values only; no demo prices are mixed with real responses.";
  if (!status.real_data) setNotice("Real market data is not configured. Please add your Twelve Data API key.");
  else if (!market.open) setNotice("Market closed — showing the latest available market data.");
  else setNotice("");
}

function renderRows(items) {
  const rows = $("rows");
  if (!items.length) {
    rows.innerHTML = `<tr><td colspan="10" class="muted" style="text-align:center;padding:24px">No stocks are currently available.</td></tr>`;
    return;
  }
  rows.innerHTML = items.map((x) => {
    const changeClass = Number(x.percent_change) >= 0 ? "up" : "down";
    const signalClass = x.signal === "OPPORTUNITY" ? "hot" : x.signal === "WATCH" ? "watch" : x.signal === "UNAVAILABLE" ? "bad" : "";
    const comparison = x.comparison || {};
    return `<tr data-symbol="${escapeHtml(x.symbol)}">
      <td><b>${escapeHtml(x.symbol)}</b><small>${escapeHtml(x.name)} · ${escapeHtml(x.exchange)}</small></td>
      <td>${sourceBadge(x.data_source)}</td>
      <td>${money(x.price)}</td>
      <td class="${changeClass}">${signedPercent(x.percent_change)}</td>
      <td>${money(comparison.nse_price)}</td>
      <td>${money(comparison.bse_price)}</td>
      <td>${money(x.spread)}</td>
      <td>${comparison.spread_percent == null ? "N/A" : `${Number(comparison.spread_percent).toFixed(3)}%`}</td>
      <td><span class="signal ${signalClass}">${escapeHtml(x.signal || "UNAVAILABLE")}</span></td>
      <td><button class="removeBtn" data-remove="${escapeHtml(x.symbol)}" title="Remove from watchlist">×</button></td>
    </tr>`;
  }).join("");
  rows.querySelectorAll("tr[data-symbol]").forEach((row) => row.addEventListener("click", (event) => {
    if (!event.target.closest("[data-remove]")) openStock(row.dataset.symbol);
  }));
  rows.querySelectorAll("[data-remove]").forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    removeFromWatchlist(button.dataset.remove);
  }));
}

function updateCards(data) {
  quotes = data.data || [];
  $("count").textContent = data.count ?? quotes.length;
  const opportunities = quotes.filter((x) => x.signal === "OPPORTUNITY").length;
  $("signals").textContent = opportunities;
  const spreads = quotes.map((x) => Number(x.spread)).filter(Number.isFinite);
  $("spread").textContent = spreads.length ? money(Math.max(...spreads)) : "N/A";
  $("updated").textContent = data.timestamp ? new Date(data.timestamp).toLocaleTimeString("en-IN", {hour: "2-digit", minute: "2-digit", second: "2-digit"}) : "—";
  $("source").textContent = data.source || "—";
  $("thresholdLabel").textContent = data.threshold == null ? "Educational comparison" : `Threshold ₹${Number(data.threshold).toFixed(2)}`;
  $("thresholdVal").textContent = Number(data.threshold || 0).toFixed(2);
  $("txCost").textContent = Number(data.transaction_cost || 0).toFixed(2);
  renderRows(quotes);
}

async function loadStatus() {
  try {
    const status = await api("/api/status");
    updateStatus(status);
  } catch (error) {
    setNotice(error.message, true);
  }
}

// Called on initial page load and whenever the user clicks "Refresh".
// There is no automatic polling — this is the only place market-data
// requests are triggered from besides search/select/chart actions.
async function loadMarketData() {
  const button = $("refreshButton");
  if (button) button.disabled = true;
  try {
    const data = await api("/api/market-data");
    updateCards(data);
    if (data.errors?.length) setNotice(data.errors[0], true);
    else {
      const status = await api("/api/status");
      updateStatus(status);
    }
  } catch (error) {
    setNotice(error.message || "Unable to refresh market data.", true);
  } finally {
    if (button) button.disabled = false;
  }
}

function scheduleSearch(query) {
  clearTimeout(searchDebounceTimer);
  if (query.trim().length < 2) {
    $("searchResults").classList.add("hidden");
    selectedResult = null;
    $("addSelected").disabled = true;
    return;
  }
  // Debounce keystrokes so a search API call only fires once typing pauses,
  // conserving Twelve Data credits (Requirement 8).
  searchDebounceTimer = setTimeout(() => searchStocks(query), SEARCH_DEBOUNCE_MS);
}

async function searchStocks(query) {
  try {
    const result = await api(`/api/search?q=${encodeURIComponent(query.trim())}`);
    const node = $("searchResults");
    if (!result.data?.length) {
      node.innerHTML = `<div class="result"><span class="muted">No matching stock found.</span></div>`;
    } else {
      node.innerHTML = result.data.map((item, index) => `<div class="result" data-result-index="${index}">
        <span><b>${escapeHtml(item.symbol)}</b><small>${escapeHtml(item.name)}</small></span>
        <span class="resultMeta">${escapeHtml(item.exchange)}<br>${escapeHtml(item.country)}</span>
      </div>`).join("");
      node.querySelectorAll("[data-result-index]").forEach((row) => row.addEventListener("click", () => selectSearchResult(result.data[Number(row.dataset.resultIndex)])));
    }
    node.classList.remove("hidden");
  } catch (error) {
    setNotice(error.message, true);
  }
}

function selectSearchResult(item) {
  selectedResult = item;
  $("stockSearch").value = `${item.symbol} · ${item.name}`;
  $("addSelected").disabled = false;
  $("searchResults").classList.add("hidden");
}

async function addSelectedToWatchlist() {
  if (!selectedResult) return;
  try {
    await api("/api/watchlist", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({symbol: selectedResult.symbol})});
    $("stockSearch").value = "";
    selectedResult = null;
    $("addSelected").disabled = true;
    await loadMarketData();
  } catch (error) {
    setNotice(error.message, true);
  }
}

async function removeFromWatchlist(symbol) {
  try {
    await api(`/api/watchlist/${encodeURIComponent(symbol)}`, {method: "DELETE"});
    await loadMarketData();
  } catch (error) {
    setNotice(error.message, true);
  }
}

async function openStock(symbol) {
  activeSymbol = symbol;
  $("modalOverlay").classList.add("open");

  const cacheKey = `${symbol}|${activeRange}`;
  const cached = stockCache.get(cacheKey);
  if (cached && (Date.now() - cached.time) < STOCK_CACHE_TTL_MS) {
    renderStock(cached.data);
    drawChart(cached.data.history || [], cached.data.history_message, cached.data.history_source);
    return;
  }

  $("modalBody").innerHTML = `<div class="loading">Loading stock details…</div>`;
  try {
    const stock = await api(`/api/stock/${encodeURIComponent(symbol)}?range=${activeRange}`);
    stockCache.set(cacheKey, {time: Date.now(), data: stock});
    renderStock(stock);
    drawChart(stock.history || [], stock.history_message, stock.history_source);
  } catch (error) {
    $("modalBody").innerHTML = `<div class="loading">${escapeHtml(error.message)}</div>`;
  }
}

function renderStock(stock) {
  const changeClass = Number(stock.percent_change) >= 0 ? "up" : "down";
  const c = stock.comparison || {};
  const fallbackNote = stock.data_source === "CACHED" && stock.fallback_reason
    ? `<p class="note">Live data unavailable (${escapeHtml(stock.fallback_reason)}) — showing the last locally saved price.</p>`
    : "";
  $("modalBody").innerHTML = `<div class="detailTop">
    <h2 id="detailTitle">${escapeHtml(stock.symbol)} <span class="muted">· ${escapeHtml(stock.name)}</span></h2>
    <p>${escapeHtml(stock.exchange)} · ${escapeHtml(stock.country)} · Updated ${escapeHtml(stock.last_updated || "N/A")} · ${sourceBadge(stock.data_source)}</p>
    ${fallbackNote}
    <div class="priceLine"><strong>${money(stock.price)}</strong><b class="${changeClass}">${signedPercent(stock.percent_change)}</b></div>
    <div class="detailGrid">
      <div><small>Open</small><strong>${money(stock.open)}</strong></div><div><small>High</small><strong>${money(stock.high)}</strong></div>
      <div><small>Low</small><strong>${money(stock.low)}</strong></div><div><small>Previous close</small><strong>${money(stock.previous_close)}</strong></div>
      <div><small>Volume</small><strong>${stock.volume == null ? "N/A" : Number(stock.volume).toLocaleString("en-IN")}</strong></div>
      <div><small>52W high</small><strong>${money(stock.fifty_two_week_high)}</strong></div><div><small>52W low</small><strong>${money(stock.fifty_two_week_low)}</strong></div>
      <div><small>Market</small><strong>${stock.market?.open ? "OPEN" : "CLOSED"}</strong></div>
    </div>
    <div class="comparison"><h3>Educational Arbitrage Calculation</h3>
      <div class="comparisonGrid">
        <div><small>NSE price</small><b>${money(c.nse_price)}</b></div><div><small>BSE price</small><b>${money(c.bse_price)}</b></div>
        <div><small>Gross difference</small><b>${money(c.potential_gross_difference)}</b></div>
      </div>
      <p class="note">${escapeHtml(c.direction || c.message || "Comparison unavailable.")}</p>
      <p class="note">Educational Arbitrage Calculation: gross price difference only. It does not account for liquidity, transaction costs, taxes, slippage, latency, or execution risk.</p>
    </div>
  </div>`;
}

function drawChart(history, historyMessage, historySource) {
  const badge = $("historySourceBadge");
  if (badge) badge.innerHTML = historySource ? sourceBadge(historySource) : "";

  const context = $("assetChart").getContext("2d");
  if (assetChart) { assetChart.destroy(); assetChart = null; }
  if (!history.length) {
    const emptyNote = $("chartNote");
    if (emptyNote) emptyNote.textContent = "";
    context.clearRect(0, 0, context.canvas.width, context.canvas.height);
    const box = $("assetChart").parentElement;
    let msg = box.querySelector(".chartEmpty");
    if (!msg) {
      msg = document.createElement("div");
      msg.className = "chartEmpty muted";
      msg.style.textAlign = "center";
      msg.style.paddingTop = "90px";
      box.appendChild(msg);
    }
    msg.textContent = historyMessage || "Historical chart data is currently unavailable.";
    msg.style.display = "block";
    return;
  }
  const box = $("assetChart").parentElement;
  const msg = box.querySelector(".chartEmpty");
  if (msg) msg.style.display = "none";
  const note = $("chartNote");
  if (note) note.textContent = historyMessage || "";

  assetChart = new Chart(context, {type: "line", data: {labels: history.map((point) => point.datetime || ""), datasets: [{
    label: `${activeSymbol} price`, data: history.map((point) => point.price), borderColor: "#6366f1", backgroundColor: "rgba(99,102,241,.09)", tension: .3, fill: true, pointRadius: 0
  }]}, options: {responsive: true, maintainAspectRatio: false, interaction: {intersect: false, mode: "index"}, plugins: {legend: {display: false}, tooltip: {callbacks: {label: (context) => money(context.parsed.y)}}}, scales: {x: {display: false}, y: {ticks: {callback: (value) => money(value)}}}}});
}

function closeModal() {
  $("modalOverlay").classList.remove("open");
  activeSymbol = null;
}

document.addEventListener("DOMContentLoaded", () => {
  $("stockSearch").addEventListener("input", (event) => scheduleSearch(event.target.value));
  $("addSelected").addEventListener("click", addSelectedToWatchlist);
  $("refreshButton").addEventListener("click", loadMarketData);
  $("modalClose").addEventListener("click", closeModal);
  $("modalOverlay").addEventListener("click", (event) => { if (event.target === $("modalOverlay")) closeModal(); });
  $("rangeButtons").querySelectorAll("button").forEach((button) => button.addEventListener("click", async () => {
    activeRange = button.dataset.range;
    $("rangeButtons").querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
    if (activeSymbol) await openStock(activeSymbol);
  }));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeModal(); });

  // Initial load only. No background polling — refresh happens only via the
  // manual Refresh button, search/select, or a chart range change.
  loadStatus();
  loadMarketData();
});
