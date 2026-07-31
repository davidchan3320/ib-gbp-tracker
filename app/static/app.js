const state = {
  bars: [],
  aggregateBars: {},
  metrics: null,
  status: null,
  range: "5D",
  hoverIndex: null,
  geometry: null,
  lastDigits: null,
  refreshTimer: null,
};

const elements = {
  canvas: document.querySelector("#price-chart"),
  chartStage: document.querySelector("#chart-stage"),
  chartEmpty: document.querySelector("#chart-empty"),
  tooltip: document.querySelector("#chart-tooltip"),
  syncButton: document.querySelector("#sync-button"),
  connectionPill: document.querySelector("#connection-pill"),
  connectionLabel: document.querySelector("#connection-label"),
  toastRegion: document.querySelector("#toast-region"),
};

const colors = {
  ink: "#15272e",
  inkSoft: "#65777b",
  grid: "rgba(90, 115, 120, 0.18)",
  gridStrong: "rgba(90, 115, 120, 0.28)",
  teal: "#177b75",
  tealFill: "rgba(23, 123, 117, 0.2)",
  coral: "#d65d46",
  coralFill: "rgba(214, 93, 70, 0.18)",
  ochre: "#b28736",
  surface: "#f7faf9",
};

const timeZones = [
  ["clock-hkg", "Asia/Hong_Kong"],
  ["clock-ldn", "Europe/London"],
  ["clock-nyc", "America/New_York"],
];

function parseApiDate(value) {
  if (!value) return null;
  const hasZone = /(?:Z|[+-]\d\d:\d\d)$/.test(value);
  return new Date(hasZone ? value : `${value}Z`);
}

function formatPrice(value, digits = 5) {
  if (value === null || value === undefined || value === "") return "—";
  return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—";
}

function formatQuantity(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return new Intl.NumberFormat("en", { maximumFractionDigits: 0 }).format(number);
}

function formatTimestamp(value, options = {}) {
  const date = value instanceof Date ? value : parseApiDate(value);
  if (!date || Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    ...options,
  }).format(date);
}

function relativeTime(value) {
  const date = parseApiDate(value);
  if (!date) return "never";
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
  return formatter.format(Math.round(minutes / 60), "hour");
}

async function apiFetch(path, options = {}) {
  const response = await fetch(path, {
    headers: { Accept: "application/json", ...options.headers },
    ...options,
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch (_) {
      // Keep the HTTP fallback when a proxy returns a non-JSON error page.
    }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function loadDashboard({ quiet = false } = {}) {
  const [statusResult, barsResult, metricsResult] = await Promise.allSettled([
    apiFetch("/api/v1/status"),
    apiFetch("/api/v1/bars?price_type=midpoint&limit=7500"),
    apiFetch("/api/v1/metrics"),
  ]);

  if (statusResult.status === "fulfilled") {
    state.status = statusResult.value;
    renderStatus();
  } else if (!quiet) {
    renderConnectionError(statusResult.reason.message);
  }

  if (barsResult.status === "fulfilled") {
    state.bars = barsResult.value.bars;
    state.aggregateBars = {};
    renderInstrumentMetadata(barsResult.value);
    renderTable();
    try {
      await loadAggregateBars(state.range);
    } catch (error) {
      if (!quiet) showToast(error.message, "error");
    }
    drawChart();
  } else if (!quiet) {
    showToast(barsResult.reason.message, "error");
  }

  if (metricsResult.status === "fulfilled") {
    state.metrics = metricsResult.value;
    renderMetrics();
  } else if (metricsResult.reason?.status !== 404 && !quiet) {
    showToast(metricsResult.reason.message, "error");
  }

  if (!state.bars.length && state.status?.collector_running) {
    clearTimeout(state.refreshTimer);
    state.refreshTimer = setTimeout(() => loadDashboard({ quiet: true }), 1400);
  }
}

function renderInstrumentMetadata(envelope) {
  const [base, quote] = envelope.pair.split("/");
  document.querySelector("#base-currency").textContent = base;
  document.querySelector("#quote-currency").textContent = quote;
  document.querySelector("#bar-size-label").textContent = `${envelope.bar_size} bars`;
  document.title = `FX Tape · ${envelope.pair}`;
}

function renderMetrics() {
  const metrics = state.metrics;
  if (!metrics) return;

  updateRateDrum(metrics.latest_close);
  const change = metrics.change_24h_pct;
  const changeElement = document.querySelector("#rate-change");
  const direction = change > 0 ? "positive" : change < 0 ? "negative" : "neutral";
  const arrow = change > 0 ? "↗" : change < 0 ? "↘" : "→";
  changeElement.className = `rate-change ${direction}`;
  changeElement.textContent = Number.isFinite(change)
    ? `${arrow} ${change >= 0 ? "+" : ""}${change.toFixed(3)}% over 24h`
    : "24h move unavailable";

  document.querySelector("#metric-range").textContent =
    `${formatPrice(metrics.low_24h)} — ${formatPrice(metrics.high_24h)}`;
  const rangePips = (metrics.high_24h - metrics.low_24h) * 10_000;
  document.querySelector("#metric-range-width").textContent = `${rangePips.toFixed(1)} pips wide`;
  document.querySelector("#metric-sma").textContent = formatPrice(metrics.sma_20);
  document.querySelector("#metric-atr").textContent = formatPrice(metrics.atr_14);
  document.querySelector("#metric-volatility").textContent = Number.isFinite(
    metrics.realized_volatility_20_pct,
  )
    ? `${metrics.realized_volatility_20_pct.toFixed(2)}%`
    : "—";
  document.querySelector("#chart-as-of").textContent = `${formatTimestamp(metrics.as_of)} UTC`;
}

function updateRateDrum(value) {
  const price = Number(value).toFixed(4);
  const nextDigits = price.replace(".", "").split("");
  const digitElements = document.querySelectorAll("#rate-digits span");
  digitElements.forEach((element, index) => {
    const changed = state.lastDigits && state.lastDigits[index] !== nextDigits[index];
    element.textContent = nextDigits[index] || "0";
    element.classList.toggle("changed", Boolean(changed));
    if (changed) setTimeout(() => element.classList.remove("changed"), 420);
  });
  state.lastDigits = nextDigits;
  document.querySelector("#rate-readable").textContent = `Latest midpoint ${price}`;
}

function renderStatus() {
  const status = state.status;
  if (!status) return;
  document.title = `FX Tape · ${status.pair}`;
  const lastSync = status.last_sync;
  const failed = lastSync?.status === "failed";
  const isRunning = status.collector_running;

  elements.connectionPill.dataset.state = failed ? "error" : isRunning ? "syncing" : "healthy";
  elements.connectionLabel.textContent = failed
    ? "Last sync failed"
    : isRunning
      ? "Collecting bars"
      : status.provider === "demo"
        ? "Demo feed active"
        : "IB sync healthy";

  document.querySelector("#mode-badge").textContent =
    status.provider === "demo" ? "Demo mode" : "IB Gateway";
  document.querySelector("#source-name").textContent =
    status.provider === "demo" ? "CSV market replay" : "IB Gateway";
  document.querySelector("#source-detail").textContent =
    status.provider === "demo"
      ? `fx-chart-nuxt daily OHLC · ${status.pair}`
      : `${status.gateway_host}:${status.gateway_port} · socket API`;

  const collectorStage = document.querySelector('[data-stage="collector"]');
  collectorStage.dataset.state = failed ? "error" : isRunning ? "running" : "healthy";
  document.querySelector("#collector-state").textContent = failed
    ? "Sync needs attention"
    : isRunning
      ? "Collecting now"
      : lastSync
        ? "Collector ready"
        : "Waiting for first run";
  document.querySelector("#collector-detail").textContent = failed
    ? lastSync.message || "The provider request failed"
    : lastSync?.completed_at
      ? `Last wrote ${lastSync.bars_written} bars ${relativeTime(lastSync.completed_at)}`
      : "No completed sync recorded";

  document.querySelector("#stored-bars").textContent =
    `${new Intl.NumberFormat("en").format(status.stored_bars)} bars`;
  document.querySelector("#database-detail").textContent =
    `${status.database === "postgresql" ? "PostgreSQL" : "SQLite"} · ${status.bar_size}`;
  document.querySelector('[data-stage="store"]').dataset.state =
    status.stored_bars > 0 ? "healthy" : "running";

  updateNextSync();
}

function renderConnectionError(message) {
  elements.connectionPill.dataset.state = "error";
  elements.connectionLabel.textContent = "API unavailable";
  document.querySelector("#collector-state").textContent = "API unavailable";
  document.querySelector("#collector-detail").textContent = message;
  document.querySelector('[data-stage="collector"]').dataset.state = "error";
}

function updateNextSync() {
  const output = document.querySelector("#next-sync");
  const status = state.status;
  if (!status) {
    output.textContent = "—";
    return;
  }
  if (status.collector_running) {
    output.textContent = "In progress";
    return;
  }
  if (!status.scheduler_enabled) {
    output.textContent = "Manual only";
    return;
  }
  const completed = parseApiDate(status.last_sync?.completed_at);
  if (!completed) {
    output.textContent = "Queued";
    return;
  }
  const remaining = Math.max(
    0,
    Math.round(
      (completed.getTime() + status.sync_interval_seconds * 1000 - Date.now()) / 1000,
    ),
  );
  const minutes = Math.floor(remaining / 60);
  const seconds = remaining % 60;
  output.textContent = remaining ? `${minutes}:${String(seconds).padStart(2, "0")}` : "Due now";
}

function renderTable() {
  const body = document.querySelector("#bars-body");
  const recent = state.bars.slice(-8).reverse();
  document.querySelector("#row-count").textContent =
    `${new Intl.NumberFormat("en").format(state.bars.length)} rows loaded`;
  if (!recent.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="9">No stored bars yet.</td></tr>';
    return;
  }

  body.replaceChildren(
    ...recent.map((bar) => {
      const row = document.createElement("tr");
      const move = (bar.close - bar.open) * 10_000;
      const directionClass = move > 0 ? "move-up" : move < 0 ? "move-down" : "";
      const values = [
        `${formatTimestamp(bar.timestamp)} UTC`,
        formatPrice(bar.open),
        formatPrice(bar.high),
        formatPrice(bar.low),
        formatPrice(bar.close),
        formatPrice(bar.weighted_average_price),
        formatQuantity(bar.volume),
        formatQuantity(bar.trade_count),
      ];
      values.forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      });
      const moveCell = document.createElement("td");
      moveCell.className = directionClass;
      moveCell.textContent = `${move >= 0 ? "+" : ""}${move.toFixed(1)} pips`;
      row.append(moveCell);
      return row;
    }),
  );
}

function visibleBars() {
  if (!state.bars.length) return [];
  if (state.aggregateBars[state.range]) return state.aggregateBars[state.range];
  const hours = { "1D": 24, "5D": 24 * 5, "1M": 24 * 31 }[state.range];
  const latest = parseApiDate(state.bars.at(-1).timestamp).getTime();
  const cutoff = latest - hours * 60 * 60 * 1000;
  const selected = state.bars.filter((bar) => parseApiDate(bar.timestamp).getTime() >= cutoff);
  return selected.length >= 2 ? selected : state.bars.slice(-Math.min(80, state.bars.length));
}

async function loadAggregateBars(range) {
  const days = { "1M": 31, "180D": 180 }[range];
  if (!days || state.aggregateBars[range] || !state.bars.length) return;

  const latest = parseApiDate(state.bars.at(-1).timestamp);
  const end = new Date(
    Date.UTC(latest.getUTCFullYear(), latest.getUTCMonth(), latest.getUTCDate() + 1),
  );
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - days);
  const params = new URLSearchParams({
    start: start.toISOString().slice(0, 10),
    end: end.toISOString().slice(0, 10),
    limit: String(days),
    price_type: "midpoint",
  });
  const response = await apiFetch(`/api/v1/metrics/daily?${params}`);
  state.aggregateBars[range] = response.metrics.map((metric) => ({
    price_type: metric.price_type,
    timestamp: `${metric.day}T00:00:00Z`,
    open: metric.open,
    high: metric.high,
    low: metric.low,
    close: metric.close,
    volume: null,
    weighted_average_price: null,
    trade_count: null,
  }));
}

function drawChart() {
  const bars = visibleBars();
  elements.chartEmpty.hidden = bars.length > 0;
  if (!bars.length) return;

  const canvas = elements.canvas;
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return;
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(rect.width * ratio);
  canvas.height = Math.round(rect.height * ratio);
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);

  const margin = { top: 24, right: rect.width < 540 ? 53 : 67, bottom: 34, left: 16 };
  const plot = {
    x: margin.left,
    y: margin.top,
    width: rect.width - margin.left - margin.right,
    height: rect.height - margin.top - margin.bottom,
  };
  const rawLow = Math.min(...bars.map((bar) => bar.low));
  const rawHigh = Math.max(...bars.map((bar) => bar.high));
  const pricePadding = Math.max((rawHigh - rawLow) * 0.1, 0.0001);
  const low = rawLow - pricePadding;
  const high = rawHigh + pricePadding;
  const priceToY = (price) => plot.y + ((high - price) / (high - low)) * plot.height;
  const step = plot.width / bars.length;
  const indexToX = (index) => plot.x + step * index + step / 2;

  state.geometry = { bars, plot, low, high, priceToY, indexToX, step };
  context.clearRect(0, 0, rect.width, rect.height);
  drawGrid(context, bars, plot, low, high, priceToY, indexToX);
  drawCandles(context, bars, priceToY, indexToX, step);
  drawMovingAverage(context, bars, priceToY, indexToX);
  drawLatestMarker(context, bars.at(-1), plot, priceToY);
  if (state.hoverIndex !== null && state.hoverIndex < bars.length) {
    drawCrosshair(context, state.hoverIndex);
  }
}

function drawGrid(context, bars, plot, low, high, priceToY, indexToX) {
  context.save();
  context.font = '9px "SFMono-Regular", Consolas, monospace';
  context.textBaseline = "middle";
  context.lineWidth = 1;

  for (let index = 0; index <= 5; index += 1) {
    const price = high - ((high - low) * index) / 5;
    const y = priceToY(price);
    context.strokeStyle = index === 5 ? colors.gridStrong : colors.grid;
    context.beginPath();
    context.moveTo(plot.x, Math.round(y) + 0.5);
    context.lineTo(plot.x + plot.width, Math.round(y) + 0.5);
    context.stroke();
    context.fillStyle = colors.inkSoft;
    context.fillText(price.toFixed(5), plot.x + plot.width + 8, y);
  }

  const labelCount = Math.min(plot.width < 500 ? 3 : 5, bars.length);
  for (let index = 0; index < labelCount; index += 1) {
    const barIndex = Math.round((index * (bars.length - 1)) / Math.max(1, labelCount - 1));
    const x = indexToX(barIndex);
    context.strokeStyle = colors.grid;
    context.beginPath();
    context.moveTo(Math.round(x) + 0.5, plot.y);
    context.lineTo(Math.round(x) + 0.5, plot.y + plot.height);
    context.stroke();
    context.textAlign = index === 0 ? "left" : index === labelCount - 1 ? "right" : "center";
    context.textBaseline = "top";
    context.fillStyle = colors.inkSoft;
    context.fillText(
      formatTimestamp(bars[barIndex].timestamp, { day: "2-digit", month: "short" }),
      x,
      plot.y + plot.height + 11,
    );
    context.textBaseline = "middle";
  }
  context.restore();
}

function drawCandles(context, bars, priceToY, indexToX, step) {
  const width = Math.max(1, Math.min(9, step * 0.58));
  bars.forEach((bar, index) => {
    const x = indexToX(index);
    const openY = priceToY(bar.open);
    const closeY = priceToY(bar.close);
    const highY = priceToY(bar.high);
    const lowY = priceToY(bar.low);
    const positive = bar.close >= bar.open;
    context.strokeStyle = positive ? colors.teal : colors.coral;
    context.fillStyle = positive ? colors.tealFill : colors.coralFill;
    context.lineWidth = width <= 1 ? 0.75 : 1;
    context.beginPath();
    context.moveTo(Math.round(x) + 0.5, highY);
    context.lineTo(Math.round(x) + 0.5, lowY);
    context.stroke();
    const top = Math.min(openY, closeY);
    const height = Math.max(1, Math.abs(closeY - openY));
    context.fillRect(x - width / 2, top, width, height);
    context.strokeRect(x - width / 2, top, width, height);
  });
}

function drawMovingAverage(context, bars, priceToY, indexToX) {
  if (bars.length < 20) return;
  context.save();
  context.strokeStyle = colors.ochre;
  context.lineWidth = 1.25;
  context.globalAlpha = 0.9;
  context.beginPath();
  bars.forEach((_, index) => {
    if (index < 19) return;
    const mean = bars.slice(index - 19, index + 1).reduce((sum, bar) => sum + bar.close, 0) / 20;
    const x = indexToX(index);
    const y = priceToY(mean);
    if (index === 19) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.stroke();
  context.restore();
}

function drawLatestMarker(context, bar, plot, priceToY) {
  const y = priceToY(bar.close);
  context.save();
  context.strokeStyle = "rgba(23, 123, 117, 0.55)";
  context.setLineDash([3, 4]);
  context.beginPath();
  context.moveTo(plot.x, y);
  context.lineTo(plot.x + plot.width, y);
  context.stroke();
  context.setLineDash([]);
  context.fillStyle = colors.teal;
  context.fillRect(plot.x + plot.width + 4, y - 9, 58, 18);
  context.fillStyle = "#f7faf9";
  context.font = '8px "SFMono-Regular", Consolas, monospace';
  context.textBaseline = "middle";
  context.fillText(formatPrice(bar.close), plot.x + plot.width + 8, y);
  context.restore();
}

function drawCrosshair(context, index) {
  const { bars, plot, priceToY, indexToX } = state.geometry;
  const bar = bars[index];
  const x = indexToX(index);
  const y = priceToY(bar.close);
  context.save();
  context.strokeStyle = "rgba(21, 39, 46, 0.42)";
  context.lineWidth = 1;
  context.setLineDash([2, 3]);
  context.beginPath();
  context.moveTo(x, plot.y);
  context.lineTo(x, plot.y + plot.height);
  context.moveTo(plot.x, y);
  context.lineTo(plot.x + plot.width, y);
  context.stroke();
  context.restore();
}

function updateTooltip(index) {
  const geometry = state.geometry;
  if (!geometry || index < 0 || index >= geometry.bars.length) {
    elements.tooltip.hidden = true;
    return;
  }
  const bar = geometry.bars[index];
  elements.tooltip.querySelector("time").textContent = `${formatTimestamp(bar.timestamp)} UTC`;
  ["open", "high", "low", "close"].forEach((field) => {
    elements.tooltip.querySelector(`[data-field="${field}"]`).textContent = formatPrice(bar[field]);
  });
  elements.tooltip.hidden = false;
  const x = geometry.indexToX(index);
  elements.tooltip.style.left = x > geometry.plot.width / 2 ? "14px" : "auto";
  elements.tooltip.style.right = x > geometry.plot.width / 2 ? "auto" : "14px";
}

async function runSync() {
  elements.syncButton.disabled = true;
  elements.syncButton.classList.add("syncing");
  elements.syncButton.querySelector("span").textContent = "Syncing";
  elements.connectionPill.dataset.state = "syncing";
  elements.connectionLabel.textContent = "Collecting bars";
  try {
    const result = await apiFetch("/api/v1/sync", { method: "POST" });
    showToast(`Sync completed · ${result.bars_written} bars written`);
    await loadDashboard({ quiet: true });
  } catch (error) {
    showToast(error.message, "error");
    await loadDashboard({ quiet: true });
  } finally {
    elements.syncButton.disabled = false;
    elements.syncButton.classList.remove("syncing");
    elements.syncButton.querySelector("span").textContent = "Sync now";
  }
}

function showToast(message, type = "success") {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  elements.toastRegion.append(toast);
  setTimeout(() => toast.remove(), 5_000);
}

function updateClocks() {
  const now = new Date();
  timeZones.forEach(([id, timeZone]) => {
    document.querySelector(`#${id}`).textContent = new Intl.DateTimeFormat("en-GB", {
      timeZone,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(now);
  });
  updateNextSync();
}

document.querySelectorAll("[data-range]").forEach((button) => {
  button.addEventListener("click", async () => {
    const previousRange = state.range;
    state.range = button.dataset.range;
    state.hoverIndex = null;
    elements.tooltip.hidden = true;
    document.querySelectorAll("[data-range]").forEach((item) => {
      item.classList.toggle("active", item === button);
    });
    try {
      await loadAggregateBars(state.range);
      drawChart();
    } catch (error) {
      state.range = previousRange;
      document.querySelectorAll("[data-range]").forEach((item) => {
        item.classList.toggle("active", item.dataset.range === previousRange);
      });
      showToast(error.message, "error");
    }
  });
});

elements.canvas.addEventListener("pointermove", (event) => {
  if (!state.geometry) return;
  const rect = elements.canvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const { plot, step, bars } = state.geometry;
  if (x < plot.x || x > plot.x + plot.width) {
    state.hoverIndex = null;
    elements.tooltip.hidden = true;
  } else {
    state.hoverIndex = Math.max(0, Math.min(bars.length - 1, Math.floor((x - plot.x) / step)));
    updateTooltip(state.hoverIndex);
  }
  drawChart();
});

elements.canvas.addEventListener("pointerleave", () => {
  state.hoverIndex = null;
  elements.tooltip.hidden = true;
  drawChart();
});

elements.syncButton.addEventListener("click", runSync);
new ResizeObserver(() => window.requestAnimationFrame(drawChart)).observe(elements.chartStage);

updateClocks();
setInterval(updateClocks, 1_000);
loadDashboard();
