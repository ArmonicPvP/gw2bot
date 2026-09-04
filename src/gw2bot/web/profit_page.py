"""Static browser dashboard for a member's Trading Post profit reports."""

from gw2bot.profit.store import MAX_REPORT_DAYS
from gw2bot.web.page import _DASHBOARD_HEADER_STYLE, _SHARED_STYLE

_PROFIT_PAGE_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Trading Post Profit</title>
<style>"""
    + _SHARED_STYLE
    + _DASHBOARD_HEADER_STYLE
    + """
body { display: flex; flex-direction: column; }
#range-form { align-items: center; gap: 0.45rem; }
label { color: var(--muted); font-size: 0.85rem; }
input {
  width: 4.6rem;
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.35rem 0.5rem;
  font: inherit;
  font-size: 0.85rem;
}
.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
main { width: 100%; margin: 0; padding: 1rem; }
#status {
  color: var(--muted);
  min-height: 1.5rem;
  margin-bottom: 0.75rem;
}
#status.error { color: #ff8f86; }
#key-help {
  display: none;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1rem;
  margin-bottom: 1rem;
}
#key-help.open { display: block; }
#key-help code { color: var(--text); }
.cards { display: grid; gap: 1rem; }
.card {
  min-width: 0;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}
.card h2 { font-size: 1rem; padding: 0.85rem 1rem 0.25rem; }
.card p.note { color: var(--muted); font-size: 0.82rem; padding: 0 1rem 0.8rem; }
.pagination {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.65rem 0.8rem;
}
.pagination-pages { display: flex; gap: 0.25rem; margin-left: auto; }
.pagination button { min-width: 2rem; padding: 0.3rem 0.5rem; }
.pagination button[aria-current="page"] {
  background: var(--accent); border-color: var(--accent); color: #fff;
}
.page-size { display: flex; align-items: center; gap: 0.4rem; }
.page-size input { width: 4rem; }
.card-heading { display: flex; align-items: center; gap: 0.75rem; padding-right: 1rem; }
.card-heading h2 { flex: 1; }
.pick-toggle { display: inline-flex; margin-top: 0.6rem; }
.pick-toggle button { border-radius: 0; }
.pick-toggle button:first-child { border-radius: 6px 0 0 6px; }
.pick-toggle button:last-child { border-radius: 0 6px 6px 0; }
.pick-toggle button[aria-pressed="true"] { background: var(--accent); color: #fff; }
.card h3.subheading { font-size: 0.92rem; padding: 0.85rem 1rem 0.15rem; }
/* A loading card keeps its heading and shows a spinner where its body will
   be, so the page reads as a list of named sections filling in rather than a
   blank screen that appears all at once. */
.card.loading { min-height: 7.5rem; }
.card.loading > :not(.section-spinner):not(h2):not(.card-heading) {
  display: none;
}
.section-spinner { display: none; }
.card.loading .section-spinner, .card.failed .section-spinner {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.6rem;
  padding: 2rem 1rem;
  color: var(--muted);
  font-size: 0.85rem;
}
.card.failed > :not(.section-spinner):not(h2):not(.card-heading) {
  display: none;
}
.card.failed .spinner { display: none; }
.spinner {
  width: 1.5rem;
  height: 1.5rem;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: profit-spin 0.8s linear infinite;
}
@keyframes profit-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
  .spinner { animation-duration: 2.4s; }
}
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}
.icon-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0.3rem;
  background: transparent;
  border-color: transparent;
  color: var(--muted);
  line-height: 0;
}
.icon-button:hover { color: var(--text); background: var(--panel-2); }
.icon-button svg { pointer-events: none; }
.row-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0.25rem 0.4rem;
  line-height: 0;
  color: var(--muted);
}
.row-action:hover { color: var(--text); }
.row-action svg { pointer-events: none; }
td.actions { text-align: center; }
#hidden-dialog {
  /* The shared reset zeroes every margin, which takes the centring a modal
     dialog normally gets from the user agent's `margin: auto` with it. */
  margin: auto;
  width: min(30rem, calc(100vw - 2rem));
  max-height: min(32rem, calc(100vh - 4rem));
  padding: 0;
  background: var(--panel);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}
#hidden-dialog::backdrop { background: rgba(0, 0, 0, 0.55); }
#hidden-dialog[open] { display: flex; flex-direction: column; }
.modal-head {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.85rem 0.7rem 0.25rem 1rem;
}
.modal-head h2 { flex: 1; font-size: 1rem; }
#hidden-count { padding-bottom: 0.6rem; }
.modal-search { display: block; padding: 0 1rem 0.85rem; }
.modal-search input { width: 100%; }
.modal-scroll { overflow: auto; border-top: 1px solid var(--border); }
#hidden-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  border-top: 0;
}
#hidden-table th:last-child, #hidden-table td:last-child {
  width: 1%;
  text-align: right;
}
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
th, td {
  padding: 0.65rem 0.8rem;
  text-align: right;
  border-top: 1px solid var(--border);
  white-space: nowrap;
}
th { color: var(--muted); font-weight: 600; background: var(--panel-2); }
th:first-child, td:first-child { text-align: left; }
.sort-button {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.35rem;
  width: 100%;
  padding: 0;
  color: inherit;
  background: transparent;
  border: 0;
  border-radius: 3px;
  font: inherit;
  font-weight: inherit;
}
th:first-child .sort-button { justify-content: flex-start; }
.sort-button:hover { color: var(--text); background: transparent; }
.sort-button:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.sort-button::after { content: "\\2195"; color: var(--muted); font-size: 0.75rem; }
th[aria-sort="ascending"] .sort-button::after { content: "\\25B2"; color: var(--text); }
th[aria-sort="descending"] .sort-button::after { content: "\\25BC"; color: var(--text); }
td.name { white-space: normal; overflow-wrap: anywhere; min-width: 12rem; }
tfoot td { font-weight: 700; background: var(--panel-2); }
.positive { color: #74dc9a; }
.negative { color: #ff8f86; }
.empty { color: var(--muted); text-align: center !important; padding: 1.2rem; }
.chart-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0.75rem;
  padding: 0 1rem 1rem;
}
.chart-panel {
  min-width: 0;
  padding: 0.75rem;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.chart-panel h3 { font-size: 0.88rem; margin-bottom: 0.1rem; }
.chart-panel p { color: var(--muted); font-size: 0.75rem; min-height: 2.1rem; }
.profit-chart { position: relative; margin-top: 0.4rem; }
.chart-panel svg { display: block; width: 100%; height: auto; }
.chart-gridline { stroke: var(--border); stroke-width: 1; }
.chart-zero { stroke: var(--muted); stroke-width: 1; }
.chart-average { stroke: #f1c40f; stroke-width: 2; stroke-dasharray: 6 4; }
.chart-rolling { fill: none; stroke: #58a6ff; stroke-width: 2.5; }
.chart-cumulative { fill: none; stroke: #74dc9a; stroke-width: 2.5; }
.chart-point-rolling { fill: #58a6ff; }
.chart-point-cumulative { fill: #74dc9a; }
.chart-bar-positive { fill: #74dc9a; }
.chart-bar-negative { fill: #ff8f86; }
.chart-label { fill: var(--muted); font-size: 11px; }
.chart-empty { fill: var(--muted); font-size: 13px; text-anchor: middle; }
.chart-overlay { fill: transparent; cursor: crosshair; }
.chart-crosshair {
  stroke: rgba(128, 128, 128, 0.45);
  stroke-width: 1;
  pointer-events: none;
}
.chart-hover-ring { fill: none; stroke-width: 2; pointer-events: none; }
.chart-tooltip {
  position: absolute;
  z-index: 2;
  width: max-content;
  min-width: min(9rem, calc(100% - 1rem));
  max-width: min(16rem, calc(100% - 1rem));
  padding: 0.45rem 0.55rem;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  font-size: 0.78rem;
  pointer-events: none;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.35);
}
.chart-tooltip .tip-date { color: var(--muted); margin-bottom: 0.3rem; }
.chart-tooltip .tip-row { display: flex; align-items: center; gap: 0.4rem; }
.chart-tooltip .swatch {
  width: 0.7rem;
  height: 0.7rem;
  border-radius: 2px;
  flex-shrink: 0;
}
.chart-tooltip .tip-value {
  margin-left: auto;
  padding-left: 0.75rem;
  font-variant-numeric: tabular-nums;
}
@media (max-width: 640px) {
  #range-form {
    grid-column: 1 / -1;
    grid-row: 2;
    justify-self: center;
  }
  header a { grid-column: 1; grid-row: 1; justify-self: start; }
  .spacer { display: none; }
  main { padding: 0.75rem; }
  th, td { padding: 0.55rem 0.65rem; }
  .chart-grid { grid-template-columns: 1fr; padding: 0 0.75rem 0.75rem; }
}
</style>
</head>
<body>
<header>
  <h1 id="brand">Trading Post Profit</h1>
  <form id="range-form">
    <label for="days">Days</label>
    <input id="days" name="days" type="number" min="1" max="__MAX_DAYS__" value="30" required>
    <button class="primary" type="submit">Load</button>
  </form>
  <span class="spacer"></span>
  <a href="/">Calendar</a>
  <span id="whoami"></span>
  <form method="post" action="/logout">
    <button type="submit" class="signout" aria-label="Sign out">
      <svg class="signout-icon" viewBox="0 0 24 24" width="18" height="18"
        fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path>
        <polyline points="16 17 21 12 16 7"></polyline>
        <line x1="21" y1="12" x2="9" y2="12"></line>
      </svg>
      <span class="signout-label">Sign out</span>
    </button>
  </form>
</header>
<main>
  <div id="status" role="status" aria-live="polite">Loading&hellip;</div>
  <div id="key-help">
    No Trading Post API key is saved for this Discord account. Run
    <code>/profit setkey</code> in the guild server, then reload this page.
  </div>
  <div class="cards" id="reports" hidden>
    <section class="card loading" data-source="report">
      <h2>Summary</h2>
      <p class="note">FIFO-matched realized results and current-listing projections for the selected window.</p>
      <div class="table-scroll"><table>
        <thead><tr><th>Measure</th><th>Value</th></tr></thead>
        <tbody id="summary-body"></tbody>
      </table></div>
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
    <section class="card loading" data-source="report">
      <h2>Daily Profit Trends</h2>
      <p class="note">UTC sale dates; days without matched sales count as zero profit.</p>
      <div class="chart-grid">
        <figure class="chart-panel">
          <h3>Daily Profit and Average</h3>
          <p>Realized profit each day with the whole-window daily average.</p>
          <div class="profit-chart"><svg id="daily-profit-chart" viewBox="0 0 640 220" role="img" aria-label="Daily realized profit and average"></svg></div>
        </figure>
        <figure class="chart-panel">
          <h3>7-Day Rolling Average</h3>
          <p>Trailing mean across seven UTC date buckets.</p>
          <div class="profit-chart"><svg id="rolling-profit-chart" viewBox="0 0 640 220" role="img" aria-label="Seven-day rolling average realized profit"></svg></div>
        </figure>
        <figure class="chart-panel">
          <h3>Cumulative Profit</h3>
          <p>Running realized profit across the selected window.</p>
          <div class="profit-chart"><svg id="cumulative-profit-chart" viewBox="0 0 640 220" role="img" aria-label="Cumulative realized profit"></svg></div>
        </figure>
      </div>
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
    <section class="card loading" data-source="report">
      <div class="card-heading">
        <h2>Your Picks</h2>
        <div class="pick-toggle" aria-label="Rank picks by">
          <button id="picks-roi" type="button" aria-pressed="true">ROI</button>
          <button id="picks-profit" type="button" aria-pressed="false">Profit</button>
        </div>
      </div>
      <p class="note">Current buy-order and sell-listing returns for items you flipped in the selected window. Prices include Trading Post fees, and items whose current return is negative are left out.</p>
      <div class="table-scroll"><table>
        <thead><tr><th>Item</th><th>Buy Order</th><th>Sell Price</th><th>Profit / Unit</th><th>ROI</th></tr></thead>
        <tbody id="picks-body"></tbody>
      </table></div>
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
    <section class="card loading" data-source="report">
      <h2>Realized Profit by Item</h2>
      <p class="note">Median Hold is weighted by matched units. Profit Share is signed item profit divided by total realized profit.</p>
      <div class="table-scroll"><table id="items-table" data-sort-table="items">
        <thead><tr>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="0" data-sort-kind="text" data-sort-key="item" data-sort-default="ascending">Item</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="1" data-sort-kind="number" data-sort-key="units" data-sort-default="descending">Units</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="2" data-sort-kind="number" data-sort-key="cost" data-sort-default="descending">Cost</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="3" data-sort-kind="number" data-sort-key="net-revenue" data-sort-default="descending">Net Revenue</button></th>
          <th aria-sort="descending"><button class="sort-button" type="button" data-sort-index="4" data-sort-kind="number" data-sort-key="profit" data-sort-default="descending">Profit</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="5" data-sort-kind="number" data-sort-key="roi" data-sort-default="descending">ROI</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="6" data-sort-kind="number" data-sort-key="profit-per-unit" data-sort-default="descending">Profit / Unit</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="7" data-sort-kind="number" data-sort-key="median-hold" data-sort-default="ascending">Median Hold</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="8" data-sort-kind="number" data-sort-key="profit-share" data-sort-default="descending">Profit Share</button></th>
        </tr></thead>
        <tbody id="items-body"></tbody>
        <tfoot id="items-foot"></tfoot>
      </table></div>
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
    <section class="card loading" data-source="report">
      <h2>Realized Profit by Day</h2>
      <nav class="pagination" aria-label="Daily profit pages">
        <span class="pagination-pages" id="days-pages-top"></span>
      </nav>
      <div class="table-scroll"><table id="days-table" data-sort-table="days">
        <thead><tr>
          <th aria-sort="descending"><button class="sort-button" type="button" data-sort-index="0" data-sort-kind="text" data-sort-key="date" data-sort-default="descending">Date</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="1" data-sort-kind="number" data-sort-key="units" data-sort-default="descending">Units</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="2" data-sort-kind="number" data-sort-key="cost" data-sort-default="descending">Cost</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="3" data-sort-kind="number" data-sort-key="net-revenue" data-sort-default="descending">Net Revenue</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="4" data-sort-kind="number" data-sort-key="profit" data-sort-default="descending">Profit</button></th>
        </tr></thead>
        <tbody id="days-body"></tbody>
        <tfoot id="days-foot"></tfoot>
      </table></div>
      <nav class="pagination" aria-label="Daily profit pages and page size">
        <label class="page-size" for="days-page-size">Rows per page
          <input id="days-page-size" type="number" min="1" max="90" value="10">
        </label>
        <span class="pagination-pages" id="days-pages-bottom"></span>
      </nav>
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
    <section class="card loading" data-source="report">
      <h2>Unrealized Profit</h2>
      <p class="note">Unmatched purchases from the selected window that are currently listed for sale. Projected ROI is projected profit divided by their matched cost.</p>
      <div class="table-scroll"><table id="unrealized-table" data-sort-table="unrealized">
        <thead><tr>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="0" data-sort-kind="text" data-sort-key="item" data-sort-default="ascending">Item</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="1" data-sort-kind="number" data-sort-key="units" data-sort-default="descending">Units</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="2" data-sort-kind="number" data-sort-key="cost" data-sort-default="descending">Cost</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="3" data-sort-kind="number" data-sort-key="projected-sale" data-sort-default="descending">Projected Sale</button></th>
          <th aria-sort="descending"><button class="sort-button" type="button" data-sort-index="4" data-sort-kind="number" data-sort-key="projected-profit" data-sort-default="descending">Projected Profit</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="5" data-sort-kind="number" data-sort-key="projected-roi" data-sort-default="descending">Projected ROI</button></th>
        </tr></thead>
        <tbody id="unrealized-body"></tbody>
        <tfoot id="unrealized-foot"></tfoot>
      </table></div>
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
    <section class="card loading" data-source="orders">
      <div class="card-heading">
        <h2>Open Orders</h2>
        <button id="orders-menu" class="icon-button" type="button"
          aria-haspopup="dialog" title="Hidden items">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"
            aria-hidden="true">
            <circle cx="12" cy="5" r="2"></circle>
            <circle cx="12" cy="12" r="2"></circle>
            <circle cx="12" cy="19" r="2"></circle>
          </svg>
        </button>
      </div>
      <p class="note">Items you are currently buying on the Trading Post. Orders for the same item at the same price are shown as one row. Profit assumes selling the whole order at the current lowest sell listing after the 5% listing and 10% exchange fees, and ROI is that profit over what the order costs you.</p>
      <p class="note" id="orders-unpriced" hidden>Rows with no current Trading Post price show dashes and are left out of every total below, so the totals cover the same orders throughout.</p>
      <p class="note" id="orders-key-help" hidden>Open orders are unavailable for this saved key. Run <code>/profit setkey</code> again with a key that allows <code>/v2/commerce/transactions/current/buys</code>.</p>
      <div class="table-scroll"><table id="orders-table" data-sort-table="orders">
        <thead><tr>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="0" data-sort-kind="text" data-sort-key="item" data-sort-default="ascending">Item</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="1" data-sort-kind="number" data-sort-key="units" data-sort-default="descending">Units</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="2" data-sort-kind="number" data-sort-key="order-price" data-sort-default="descending">Your Price</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="3" data-sort-kind="number" data-sort-key="order-cost" data-sort-default="descending">Order Cost</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="4" data-sort-kind="number" data-sort-key="highest-buy" data-sort-default="descending">Highest Buy Order</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="5" data-sort-kind="number" data-sort-key="lowest-sell" data-sort-default="descending">Lowest Sell Listing</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="6" data-sort-kind="number" data-sort-key="order-profit" data-sort-default="descending">Profit / Unit</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="7" data-sort-kind="number" data-sort-key="order-total-profit" data-sort-default="descending">Total Profit</button></th>
          <th aria-sort="descending"><button class="sort-button" type="button" data-sort-index="8" data-sort-kind="number" data-sort-key="order-roi" data-sort-default="descending">ROI</button></th>
          <th>Hide</th>
        </tr></thead>
        <tbody id="orders-body"></tbody>
        <tfoot id="orders-foot"></tfoot>
      </table></div>
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
    <section class="card loading" data-source="delivery">
      <h2>Unclaimed Trading Post</h2>
      <p class="note">Coins and items waiting for pickup in your Trading Post delivery box.</p>
      <p class="note" id="delivery-key-help" hidden>Delivery access is unavailable for this saved key. Run <code>/profit setkey</code> again with a key that allows the Trading Post delivery endpoint.</p>
      <div class="table-scroll"><table>
        <thead><tr><th>Measure</th><th>Value</th></tr></thead>
        <tbody>
          <tr><td>Gold available to collect</td><td id="unclaimed-coins">0c</td></tr>
        </tbody>
      </table></div>
      <h3 class="subheading">Items available to collect</h3>
      <div class="table-scroll"><table id="delivery-table" data-sort-table="delivery">
        <thead><tr>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="0" data-sort-kind="text" data-sort-key="item" data-sort-default="ascending">Item</button></th>
          <th aria-sort="descending"><button class="sort-button" type="button" data-sort-index="1" data-sort-kind="number" data-sort-key="quantity" data-sort-default="descending">Quantity</button></th>
        </tr></thead>
        <tbody id="delivery-body"></tbody>
        <tfoot id="delivery-foot"></tfoot>
      </table></div>
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
  </div>
  <dialog id="hidden-dialog" aria-labelledby="hidden-title">
    <div class="modal-head">
      <h2 id="hidden-title">Hidden items</h2>
      <button id="hidden-close" class="icon-button" type="button"
        aria-label="Close hidden items">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none"
          stroke="currentColor" stroke-width="2" stroke-linecap="round"
          aria-hidden="true">
          <line x1="6" y1="6" x2="18" y2="18"></line>
          <line x1="18" y1="6" x2="6" y2="18"></line>
        </svg>
      </button>
    </div>
    <p class="note" id="hidden-count"></p>
    <label class="modal-search" for="hidden-search">
      <span class="visually-hidden">Search hidden items</span>
      <input id="hidden-search" type="search" placeholder="Search hidden items"
        autocomplete="off">
    </label>
    <div class="modal-scroll"><table id="hidden-table">
      <thead><tr><th>Item</th><th>Restore</th></tr></thead>
      <tbody id="hidden-body"></tbody>
    </table></div>
  </dialog>
</main>
<script>
(function () {
  "use strict";
  var rangeForm = document.getElementById("range-form");
  var daysInput = document.getElementById("days");
  var status = document.getElementById("status");
  var reports = document.getElementById("reports");
  var keyHelp = document.getElementById("key-help");
  var sortStates = {};
  var daysPage = 1;
  var daysPageSize = 10;
  var maxDays = __MAX_DAYS__;
  var historyStart = null;
  var missingKey = false;
  var restoredWindow = false;
  // How often the open orders follow the market. The GW2 API declares its
  // prices good for two minutes, and the server holds each reading for one,
  // so this is as live as the data can honestly be.
  var PRICE_REFRESH_MS = 60000;
  // The chosen window lives against the Discord account. This copy is a
  // repair kit: if the account ever comes back without one - a rebuilt
  // database, or a release that overwrote it - the browser puts back what
  // the member last picked instead of dropping them on the default.
  var STORED_DAYS_KEY = "gw2bot-profit-days";

  function readStoredDays() {
    try {
      var stored = Number(localStorage.getItem(STORED_DAYS_KEY));
      return Number.isInteger(stored) && stored >= 1 && stored <= maxDays
        ? stored : null;
    } catch (error) {
      return null;
    }
  }

  function writeStoredDays(days) {
    try {
      localStorage.setItem(STORED_DAYS_KEY, String(days));
    } catch (error) {
      trace("window-not-stored", 0);
    }
  }

  function trace(action, rows) {
    console.debug("Profit dashboard", action, "rows=" + rows);
  }

  function traceSort(table, column, direction, rows) {
    console.debug(
      "Profit dashboard sort", table, column, direction, "rows=" + rows);
  }

  function coin(value) {
    var sign = value < 0 ? "-" : "";
    var coins = Math.abs(value);
    var gold = Math.floor(coins / 10000);
    var silver = Math.floor((coins % 10000) / 100);
    var copper = coins % 100;
    var parts = [];
    if (gold) { parts.push(gold + "g"); }
    if (silver || gold) { parts.push(silver + "s"); }
    parts.push(copper + "c");
    return sign + parts.join(" ");
  }

  function average(profit, units) {
    return units > 0 ? coin(Math.round(profit / units)) : "0c";
  }

  function percent(value) {
    return value === null ? "\u2014" : value.toFixed(1) + "%";
  }

  function duration(seconds) {
    if (seconds < 60) { return Math.round(seconds) + "s"; }
    if (seconds < 3600) { return Math.round(seconds / 60) + "m"; }
    if (seconds < 172800) {
      return (seconds / 3600).toFixed(1).replace(".0", "") + "h";
    }
    return (seconds / 86400).toFixed(1).replace(".0", "") + "d";
  }

  function tone(value) {
    return value < 0 ? "negative" : "positive";
  }

  function cell(row, value, className, sortValue) {
    var node = document.createElement("td");
    node.textContent = String(value);
    if (className) { node.className = className; }
    if (sortValue !== undefined) {
      node.dataset.sortValue = String(sortValue);
    }
    row.appendChild(node);
    return node;
  }

  function profitCell(row, value) {
    return cell(row, coin(value), tone(value), value);
  }

  function optionalCoinCell(row, value) {
    return value === null
      ? cell(row, "\u2014", "", 0) : cell(row, coin(value), "", value);
  }

  function optionalProfitCell(row, value) {
    return value === null
      ? cell(row, "\u2014", "", 0) : profitCell(row, value);
  }

  function percentCell(row, value, toneValue) {
    return cell(
      row, percent(value), value === null ? ""
        : tone(toneValue === undefined ? value : toneValue),
      value === null ? 0 : value);
  }

  function sortableRow(index) {
    var row = document.createElement("tr");
    row.dataset.sortRow = "true";
    row.dataset.renderOrder = String(index);
    return row;
  }

  function compareValues(left, right, kind) {
    if (kind === "number") {
      return Number(left) - Number(right);
    }
    return String(left).localeCompare(
      String(right), undefined, { sensitivity: "base", numeric: true });
  }

  function sortTable(table, state) {
    var body = table.tBodies[0];
    var rows = Array.prototype.slice.call(
      body.querySelectorAll("tr[data-sort-row]"));
    var multiplier = state.direction === "ascending" ? 1 : -1;
    rows.sort(function (left, right) {
      var compared = compareValues(
        left.cells[state.column].dataset.sortValue,
        right.cells[state.column].dataset.sortValue,
        state.kind);
      if (compared) { return compared * multiplier; }
      return Number(left.dataset.renderOrder) - Number(right.dataset.renderOrder);
    });
    rows.forEach(function (row) { body.appendChild(row); });
    table.querySelectorAll("th[aria-sort]").forEach(function (heading) {
      heading.setAttribute("aria-sort", "none");
    });
    table.querySelector(
      '[data-sort-index="' + state.column + '"]').parentElement.setAttribute(
        "aria-sort", state.direction);
    return rows.length;
  }

  function applySort(tableId) {
    var table = document.getElementById(tableId);
    sortTable(table, sortStates[table.dataset.sortTable]);
  }

  function initializeSorters() {
    document.querySelectorAll("table[data-sort-table]").forEach(
      function (table) {
        var selected = table.querySelector(
          'th[aria-sort="ascending"], th[aria-sort="descending"]');
        var selectedButton = selected.querySelector(".sort-button");
        sortStates[table.dataset.sortTable] = {
          column: Number(selectedButton.dataset.sortIndex),
          direction: selected.getAttribute("aria-sort"),
          kind: selectedButton.dataset.sortKind
        };
        table.querySelectorAll(".sort-button").forEach(function (button) {
          button.addEventListener("click", function () {
            var key = table.dataset.sortTable;
            var column = Number(button.dataset.sortIndex);
            var previous = sortStates[key];
            var direction = button.dataset.sortDefault;
            if (previous.column === column) {
              direction = previous.direction === "ascending"
                ? "descending" : "ascending";
            }
            sortStates[key] = {
              column: column,
              direction: direction,
              kind: button.dataset.sortKind
            };
            if (key === "days") { daysPage = 1; }
            var rows = sortTable(table, sortStates[key]);
            if (key === "days") { paginateDays(); }
            traceSort(key, button.dataset.sortKey, direction, rows);
          });
        });
      });
  }

  var SVG_NS = "http://www.w3.org/2000/svg";
  var chartHoverCleanups = [];

  function svgNode(name, attributes, textValue) {
    var node = document.createElementNS(SVG_NS, name);
    Object.keys(attributes).forEach(function (key) {
      node.setAttribute(key, String(attributes[key]));
    });
    if (textValue !== undefined) { node.textContent = String(textValue); }
    return node;
  }

  function isoDay(date) {
    return date.toISOString().slice(0, 10);
  }

  function buildDailySeries(data) {
    var profitByDate = Object.create(null);
    data.days_table.forEach(function (day) {
      profitByDate[day.date] = day.profit;
    });
    var start = new Date(data.window.start_date + "T00:00:00Z");
    var end = new Date(data.window.end_date + "T00:00:00Z");
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())
      || start > end) {
      trace("charts-invalid-window", 0);
      return [];
    }
    var expectedStart = new Date(end.getTime());
    expectedStart.setUTCDate(expectedStart.getUTCDate() - data.days + 1);
    if (isoDay(start) !== isoDay(expectedStart)) {
      trace("charts-window-mismatch", 0);
      return [];
    }
    var points = [];
    var cursor = new Date(start.getTime());
    for (var bucket = 0; bucket < data.days; bucket += 1) {
      var date = isoDay(cursor);
      points.push({
        date: date,
        profit: Object.prototype.hasOwnProperty.call(profitByDate, date)
          ? profitByDate[date] : 0,
        rolling: null,
        cumulative: 0
      });
      cursor.setUTCDate(cursor.getUTCDate() + 1);
    }
    var cumulative = 0;
    points.forEach(function (point, index) {
      cumulative += point.profit;
      point.cumulative = cumulative;
      if (index >= 6) {
        var rollingTotal = 0;
        for (var offset = index - 6; offset <= index; offset += 1) {
          rollingTotal += points[offset].profit;
        }
        point.rolling = rollingTotal / 7;
      }
    });
    return points;
  }

  function emptyChart(svg, message) {
    var tooltip = svg.parentElement.querySelector(".chart-tooltip");
    if (tooltip) { tooltip.remove(); }
    svg.replaceChildren();
    svg.appendChild(svgNode("text", {
      x: 320,
      y: 110,
      "class": "chart-empty"
    }, message));
  }

  function chartFrame(svg, points, values, title) {
    var width = 640;
    var height = 220;
    var left = 62;
    var right = 16;
    var top = 14;
    var bottom = 34;
    var plotWidth = width - left - right;
    var plotHeight = height - top - bottom;
    var minimum = Math.min.apply(null, [0].concat(values));
    var maximum = Math.max.apply(null, [0].concat(values));
    if (minimum === maximum) {
      minimum -= 1;
      maximum += 1;
    }
    var y = function (value) {
      return top + (maximum - value) / (maximum - minimum) * plotHeight;
    };
    var x = function (index) {
      return left + (index + 0.5) / points.length * plotWidth;
    };

    svg.replaceChildren();
    svg.appendChild(svgNode("title", {}, title));
    [maximum, (maximum + minimum) / 2, minimum].forEach(function (value) {
      svg.appendChild(svgNode("line", {
        x1: left,
        y1: y(value),
        x2: width - right,
        y2: y(value),
        "class": Math.abs(value) < 0.0001
          ? "chart-zero" : "chart-gridline"
      }));
      svg.appendChild(svgNode("text", {
        x: left - 7,
        y: y(value) + 4,
        "text-anchor": "end",
        "class": "chart-label"
      }, coin(Math.round(value))));
    });
    if (minimum < 0 && maximum > 0) {
      svg.appendChild(svgNode("line", {
        x1: left,
        y1: y(0),
        x2: width - right,
        y2: y(0),
        "class": "chart-zero"
      }));
    }
    svg.appendChild(svgNode("text", {
      x: left,
      y: height - 8,
      "class": "chart-label"
    }, points[0].date));
    svg.appendChild(svgNode("text", {
      x: width - right,
      y: height - 8,
      "text-anchor": "end",
      "class": "chart-label"
    }, points[points.length - 1].date));
    return {
      width: width,
      height: height,
      left: left,
      top: top,
      bottom: bottom,
      plotWidth: plotWidth,
      plotHeight: plotHeight,
      right: width - right,
      x: x,
      y: y
    };
  }

  function tooltipNode(className, textValue) {
    var node = document.createElement("div");
    node.className = className;
    if (textValue !== undefined) { node.textContent = String(textValue); }
    return node;
  }

  function attachChartHover(svg, columns, frame) {
    var container = svg.parentElement;
    var previousTooltip = container.querySelector(".chart-tooltip");
    if (previousTooltip) { previousTooltip.remove(); }

    var crosshair = svgNode("line", {
      x1: frame.left,
      x2: frame.left,
      y1: frame.top,
      y2: frame.top + frame.plotHeight,
      "class": "chart-crosshair"
    });
    crosshair.style.visibility = "hidden";
    var rings = svgNode("g", {});
    var overlay = svgNode("rect", {
      x: frame.left,
      y: frame.top,
      width: frame.plotWidth,
      height: frame.plotHeight,
      "class": "chart-overlay"
    });
    svg.appendChild(crosshair);
    svg.appendChild(rings);
    svg.appendChild(overlay);

    var tooltip = tooltipNode("chart-tooltip");
    tooltip.style.visibility = "hidden";
    container.appendChild(tooltip);
    var pinned = false;
    var tapOrigin = null;
    var tapSlop = 12;

    function traceChartSelection(action, reason) {
      console.debug(
        "profit chart selection:", action, reason, columns.length);
    }

    function nearestColumn(vbX) {
      var nearest = null;
      var distance = Infinity;
      columns.forEach(function (column) {
        var candidateDistance = Math.abs(column.x - vbX);
        if (candidateDistance < distance) {
          nearest = column;
          distance = candidateDistance;
        }
      });
      return nearest;
    }

    function showHover(column, vbY) {
      crosshair.setAttribute("x1", String(column.x));
      crosshair.setAttribute("x2", String(column.x));
      crosshair.style.visibility = "visible";
      rings.replaceChildren();
      tooltip.replaceChildren();
      tooltip.appendChild(tooltipNode("tip-date", column.date));

      var anchorY = column.rows[0].y;
      var anchorDistance = Infinity;
      column.rows.forEach(function (reading) {
        rings.appendChild(svgNode("circle", {
          cx: column.x,
          cy: reading.y,
          r: 7,
          stroke: reading.color,
          "class": "chart-hover-ring"
        }));
        var row = tooltipNode("tip-row");
        var swatch = tooltipNode("swatch");
        swatch.style.background = reading.color;
        row.appendChild(swatch);
        row.appendChild(tooltipNode("tip-name", reading.label));
        row.appendChild(tooltipNode("tip-value", reading.value));
        tooltip.appendChild(row);
        var candidateDistance = Math.abs(reading.y - vbY);
        if (candidateDistance < anchorDistance) {
          anchorY = reading.y;
          anchorDistance = candidateDistance;
        }
      });

      // Keep the rendered box inside the chart rather than clamping only its
      // centre. The latter still clips a 9rem tooltip at either edge when
      // three charts share an ordinary desktop row or on a narrow phone.
      var containerWidth = container.getBoundingClientRect().width;
      var tooltipWidth = tooltip.getBoundingClientRect().width;
      var edgePadding = 8;
      var anchorPixels = column.x / frame.width * containerWidth;
      var minimumLeft = edgePadding + tooltipWidth / 2;
      var maximumLeft = containerWidth - edgePadding - tooltipWidth / 2;
      var leftPixels = Math.max(
        minimumLeft, Math.min(maximumLeft, anchorPixels));
      var topPercent = anchorY / frame.height * 100;
      tooltip.style.left = leftPixels + "px";
      tooltip.style.top = topPercent + "%";
      tooltip.style.transform = topPercent < 32
        ? "translate(-50%, 14px)"
        : "translate(-50%, calc(-100% - 14px))";
      tooltip.style.visibility = "visible";
    }

    function hideHover() {
      crosshair.style.visibility = "hidden";
      rings.replaceChildren();
      tooltip.style.visibility = "hidden";
    }

    function dismissPinned(reason) {
      if (!pinned) { return; }
      pinned = false;
      hideHover();
      traceChartSelection("dismiss", reason);
    }

    function pointFromEvent(event) {
      var bounds = svg.getBoundingClientRect();
      if (!bounds.width || !bounds.height) { return null; }
      return {
        x: (event.clientX - bounds.left) / bounds.width * frame.width,
        y: (event.clientY - bounds.top) / bounds.height * frame.height
      };
    }

    overlay.addEventListener("pointermove", function (event) {
      if (event.pointerType && event.pointerType !== "mouse") { return; }
      var point = pointFromEvent(event);
      if (!point) { return; }
      var column = nearestColumn(point.x);
      if (column) { showHover(column, point.y); }
    });
    overlay.addEventListener("pointerleave", function (event) {
      if (!event.pointerType || event.pointerType === "mouse") { hideHover(); }
    });
    overlay.addEventListener("pointerdown", function (event) {
      if (!event.pointerType || event.pointerType === "mouse") { return; }
      tapOrigin = {
        id: event.pointerId, x: event.clientX, y: event.clientY
      };
    });
    overlay.addEventListener("pointerup", function (event) {
      if (!tapOrigin || tapOrigin.id !== event.pointerId) { return; }
      var moved = Math.hypot(
        event.clientX - tapOrigin.x, event.clientY - tapOrigin.y);
      tapOrigin = null;
      if (moved > tapSlop) {
        traceChartSelection("skip", "moved");
        return;
      }
      var point = pointFromEvent(event);
      if (!point) {
        traceChartSelection("skip", "unmeasurable");
        return;
      }
      var column = nearestColumn(point.x);
      if (!column) {
        traceChartSelection("skip", "empty");
        return;
      }
      pinned = true;
      showHover(column, point.y);
      traceChartSelection("pin", "tap");
    });
    overlay.addEventListener("pointercancel", function () {
      tapOrigin = null;
      if (pinned) { dismissPinned("cancelled"); }
      else { traceChartSelection("skip", "cancelled"); }
    });

    function dismissFromPage(event) {
      if (overlay.contains(event.target)) { return; }
      dismissPinned("outside");
    }
    function dismissFromWheel() { dismissPinned("wheel"); }
    function dismissFromScroll() { dismissPinned("scroll"); }
    function dismissFromKey(event) {
      if (event.key === "Escape") { dismissPinned("escape"); }
    }
    function dismissFromBlur() { dismissPinned("blur"); }
    document.addEventListener("pointerdown", dismissFromPage);
    document.addEventListener("wheel", dismissFromWheel, { passive: true });
    document.addEventListener("scroll", dismissFromScroll, { passive: true });
    document.addEventListener("keydown", dismissFromKey);
    window.addEventListener("blur", dismissFromBlur);

    return function () {
      document.removeEventListener("pointerdown", dismissFromPage);
      document.removeEventListener("wheel", dismissFromWheel);
      document.removeEventListener("scroll", dismissFromScroll);
      document.removeEventListener("keydown", dismissFromKey);
      window.removeEventListener("blur", dismissFromBlur);
    };
  }

  function renderDailyProfitChart(points, dailyAverage) {
    var svg = document.getElementById("daily-profit-chart");
    var values = points.map(function (point) { return point.profit; });
    values.push(dailyAverage);
    var frame = chartFrame(
      svg, points, values, "Daily realized profit and window average");
    var barWidth = Math.max(
      2, Math.min(18, frame.plotWidth / points.length * 0.68));
    points.forEach(function (point, index) {
      var profitY = frame.y(point.profit);
      var zeroY = frame.y(0);
      var bar = svgNode("rect", {
        x: frame.x(index) - barWidth / 2,
        y: Math.min(profitY, zeroY),
        width: barWidth,
        height: point.profit === 0
          ? 0 : Math.max(1, Math.abs(profitY - zeroY)),
        "class": point.profit < 0
          ? "chart-bar-negative" : "chart-bar-positive"
      });
      bar.appendChild(svgNode(
        "title", {}, point.date + ": " + coin(point.profit)));
      svg.appendChild(bar);
    });
    var averageLine = svgNode("line", {
      x1: frame.left,
      y1: frame.y(dailyAverage),
      x2: frame.right,
      y2: frame.y(dailyAverage),
      "class": "chart-average"
    });
    averageLine.appendChild(svgNode(
      "title", {}, "Daily average: " + coin(Math.round(dailyAverage))));
    svg.appendChild(averageLine);
    chartHoverCleanups.push(attachChartHover(svg, points.map(function (point, index) {
      return {
        date: point.date,
        x: frame.x(index),
        rows: [
          {
            label: "Daily profit",
            value: coin(point.profit),
            color: point.profit < 0 ? "#ff8f86" : "#74dc9a",
            y: frame.y(point.profit)
          },
          {
            label: "Daily average",
            value: coin(Math.round(dailyAverage)),
            color: "#f1c40f",
            y: frame.y(dailyAverage)
          }
        ]
      };
    }), frame));
  }

  function renderLineChart(
    svgId, points, field, lineClass, pointClass, title, valueLabel, color,
    emptyMessage
  ) {
    var svg = document.getElementById(svgId);
    var plotted = [];
    points.forEach(function (point, index) {
      if (typeof point[field] === "number") {
        plotted.push({ index: index, point: point, value: point[field] });
      }
    });
    if (!plotted.length) {
      emptyChart(svg, emptyMessage);
      return;
    }
    var values = plotted.map(function (entry) { return entry.value; });
    var frame = chartFrame(svg, points, values, title);
    var pathData = plotted.map(function (entry, index) {
      return (index ? "L" : "M") + frame.x(entry.index) + " "
        + frame.y(entry.value);
    }).join(" ");
    svg.appendChild(svgNode("path", {
      d: pathData,
      "class": lineClass
    }));
    plotted.forEach(function (entry) {
      var point = svgNode("circle", {
        cx: frame.x(entry.index),
        cy: frame.y(entry.value),
        r: 3,
        "class": pointClass
      });
      point.appendChild(svgNode(
        "title", {}, entry.point.date + ": "
        + coin(Math.round(entry.value))));
      svg.appendChild(point);
    });
    chartHoverCleanups.push(attachChartHover(svg, plotted.map(function (entry) {
      return {
        date: entry.point.date,
        x: frame.x(entry.index),
        rows: [{
          label: valueLabel,
          value: coin(Math.round(entry.value)),
          color: color,
          y: frame.y(entry.value)
        }]
      };
    }), frame));
  }

  function renderCharts(data) {
    chartHoverCleanups.forEach(function (cleanup) { cleanup(); });
    chartHoverCleanups = [];
    var points = buildDailySeries(data);
    if (!points.length || !data.days_table.length) {
      emptyChart(
        document.getElementById("daily-profit-chart"),
        "No realized profit in this window.");
      emptyChart(
        document.getElementById("rolling-profit-chart"),
        "No realized profit in this window.");
      emptyChart(
        document.getElementById("cumulative-profit-chart"),
        "No realized profit in this window.");
      trace("charts-empty", points.length);
      return;
    }
    renderDailyProfitChart(points, data.summary.profit / data.days);
    renderLineChart(
      "rolling-profit-chart", points, "rolling", "chart-rolling",
      "chart-point-rolling", "Seven-day rolling average realized profit",
      "7-day average", "#58a6ff",
      "Seven date buckets are needed.");
    renderLineChart(
      "cumulative-profit-chart", points, "cumulative", "chart-cumulative",
      "chart-point-cumulative", "Cumulative realized profit",
      "Cumulative profit", "#74dc9a",
      "No cumulative profit in this window.");
    trace("charts-render", points.length);
  }

  function emptyRow(body, columns, message) {
    var row = document.createElement("tr");
    var node = cell(row, message, "empty");
    node.colSpan = columns;
    body.appendChild(row);
  }

  function totalRow(foot, values, profitIndex) {
    foot.replaceChildren();
    var row = document.createElement("tr");
    values.forEach(function (value, index) {
      if (index === profitIndex && typeof value === "number") {
        profitCell(row, value);
      } else {
        cell(row, value);
      }
    });
    foot.appendChild(row);
  }

  function extreme(rows, best) {
    if (!rows.length) { return null; }
    return rows.reduce(function (selected, candidate) {
      if (best ? candidate.profit > selected.profit
        : candidate.profit < selected.profit) {
        return candidate;
      }
      return selected;
    });
  }

  function highlight(entry, labelKey) {
    return entry === null
      ? "\u2014"
      : entry[labelKey] + " (" + coin(entry.profit) + ")";
  }

  function renderSummary(data) {
    var summary = data.summary;
    var unrealized = data.unrealized;
    var bestItem = extreme(data.items, true);
    var worstItem = extreme(data.items, false);
    var bestDay = extreme(data.days_table, true);
    var worstDay = extreme(data.days_table, false);
    var rows = [
      ["Window", "Last " + data.days + " day" + (data.days === 1 ? "" : "s")],
      ["Buy transactions", summary.buy_transactions],
      ["Sell transactions", summary.sell_transactions],
      ["Matched units", summary.matched_units],
      ["Matched cost", coin(summary.cost)],
      ["Net revenue", coin(summary.net_revenue)],
      ["Realized profit", coin(summary.profit), summary.profit],
      ["Realized ROI", percent(summary.roi_percent), summary.roi_percent],
      ["Profit / unit", average(summary.profit, summary.matched_units), summary.profit],
      ["Average daily profit", coin(Math.round(summary.profit / data.days)), summary.profit],
      ["Unrealized profit", coin(unrealized.projected_profit), unrealized.projected_profit],
      ["Unrealized ROI", percent(unrealized.roi_percent), unrealized.roi_percent],
      ["Best item", highlight(bestItem, "name"), bestItem && bestItem.profit],
      ["Worst item", highlight(worstItem, "name"), worstItem && worstItem.profit],
      ["Best trading day", highlight(bestDay, "date"), bestDay && bestDay.profit],
      ["Worst trading day", highlight(worstDay, "date"), worstDay && worstDay.profit]
    ];
    var body = document.getElementById("summary-body");
    body.replaceChildren();
    rows.forEach(function (values) {
      var row = document.createElement("tr");
      cell(row, values[0]);
      var valueCell = cell(row, values[1]);
      if (typeof values[2] === "number") {
        valueCell.className = tone(values[2]);
      }
      body.appendChild(row);
    });
  }

  function renderItems(data) {
    var body = document.getElementById("items-body");
    body.replaceChildren();
    data.items.forEach(function (item, index) {
      var row = sortableRow(index);
      cell(row, item.name, "name", item.name);
      cell(row, item.units, "", item.units);
      cell(row, coin(item.cost), "", item.cost);
      cell(row, coin(item.net_revenue), "", item.net_revenue);
      profitCell(row, item.profit);
      percentCell(row, item.roi_percent);
      profitCell(row, Math.round(item.profit / item.units));
      cell(
        row, duration(item.median_hold_seconds), "",
        item.median_hold_seconds);
      percentCell(row, item.profit_share_percent, item.profit);
      body.appendChild(row);
    });
    if (!data.items.length) {
      emptyRow(body, 9, "No matched flips were found in this window.");
    }
    totalRow(document.getElementById("items-foot"), [
      "Total", data.summary.matched_units, coin(data.summary.cost),
      coin(data.summary.net_revenue), data.summary.profit,
      percent(data.summary.roi_percent),
      average(data.summary.profit, data.summary.matched_units), "\u2014",
      data.summary.profit === 0 ? "\u2014" : "100.0%"
    ], 4);
    applySort("items-table");
  }

  var picksData = [];
  var picksMetric = "roi_percent";

  function renderPicks() {
    var body = document.getElementById("picks-body");
    body.replaceChildren();
    picksData.slice().sort(function (left, right) {
      var difference = right[picksMetric] - left[picksMetric];
      return difference || right.profit - left.profit
        || left.name.localeCompare(right.name);
    }).slice(0, 10).forEach(function (item) {
      var row = document.createElement("tr");
      cell(row, item.name, "name");
      cell(row, coin(item.buy_price));
      cell(row, coin(item.sell_price));
      profitCell(row, item.profit);
      percentCell(row, item.roi_percent);
      body.appendChild(row);
    });
    if (!picksData.length) {
      emptyRow(body, 5,
        "No current prices showed a positive return for your previous flips.");
    }
    trace("picks-" + (picksMetric === "roi_percent" ? "roi" : "profit"),
      Math.min(10, picksData.length));
  }

  function renderDays(data) {
    var body = document.getElementById("days-body");
    body.replaceChildren();
    data.days_table.forEach(function (day, index) {
      var row = sortableRow(index);
      cell(row, day.date, "", day.date);
      cell(row, day.units, "", day.units);
      cell(row, coin(day.cost), "", day.cost);
      cell(row, coin(day.net_revenue), "", day.net_revenue);
      profitCell(row, day.profit);
      body.appendChild(row);
    });
    if (!data.days_table.length) {
      emptyRow(body, 5, "No realized profit was found in this window.");
    }
    totalRow(document.getElementById("days-foot"), [
      "Total", data.summary.matched_units, coin(data.summary.cost),
      coin(data.summary.net_revenue), data.summary.profit
    ], 4);
    daysPage = 1;
    applySort("days-table");
    paginateDays();
  }

  function pageButton(page) {
    var button = document.createElement("button");
    button.type = "button";
    button.textContent = String(page);
    button.setAttribute("aria-label", "Page " + page);
    if (page === daysPage) { button.setAttribute("aria-current", "page"); }
    button.addEventListener("click", function () {
      daysPage = page;
      paginateDays();
      trace("days-page", page);
    });
    return button;
  }

  function paginateDays() {
    var rows = Array.prototype.slice.call(
      document.querySelectorAll("#days-body tr[data-sort-row]"));
    var pageCount = Math.max(1, Math.ceil(rows.length / daysPageSize));
    daysPage = Math.min(daysPage, pageCount);
    rows.forEach(function (row, index) {
      row.hidden = index < (daysPage - 1) * daysPageSize
        || index >= daysPage * daysPageSize;
    });
    ["days-pages-top", "days-pages-bottom"].forEach(function (id) {
      var pages = document.getElementById(id);
      pages.replaceChildren();
      for (var page = 1; page <= pageCount; page += 1) {
        pages.appendChild(pageButton(page));
      }
    });
  }

  function renderUnrealized(data) {
    var unrealized = data.unrealized;
    var body = document.getElementById("unrealized-body");
    body.replaceChildren();
    unrealized.items.forEach(function (item, index) {
      var row = sortableRow(index);
      cell(row, item.name, "name", item.name);
      cell(row, item.units, "", item.units);
      cell(row, coin(item.cost), "", item.cost);
      cell(
        row, coin(item.projected_net_revenue), "",
        item.projected_net_revenue);
      profitCell(row, item.projected_profit);
      percentCell(row, item.roi_percent);
      body.appendChild(row);
    });
    if (!unrealized.items.length) {
      emptyRow(body, 6, "No currently listed unmatched purchases were found.");
    }
    totalRow(document.getElementById("unrealized-foot"), [
      "Total", unrealized.units, coin(unrealized.cost),
      coin(unrealized.projected_net_revenue), unrealized.projected_profit,
      percent(unrealized.roi_percent)
    ], 4);
    applySort("unrealized-table");
  }

  var ordersRows = [];
  var ordersAvailable = true;

  // "Hidden" is what the dashboard calls these items; the stored rows and the
  // API keep calling them exclusions, so both words appear here.
  function hideButton(order) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "row-action";
    button.title = "Hide " + order.name;
    button.setAttribute("aria-label", "Hide " + order.name);
    var icon = svgNode("svg", {
      viewBox: "0 0 24 24",
      width: 16,
      height: 16,
      fill: "none",
      stroke: "currentColor",
      "stroke-width": 2,
      "stroke-linecap": "round",
      "stroke-linejoin": "round"
    });
    // An eye with a line through it: hidden, not deleted.
    icon.appendChild(svgNode("path", {
      d: "M9.9 4.24A9.1 9.1 0 0 1 12 4c7 0 10 8 10 8a18.5 18.5 0 0 1-2.16 3.19"
        + "M6.61 6.61A18.4 18.4 0 0 0 2 12s3 8 10 8a9 9 0 0 0 5.39-1.61"
    }));
    icon.appendChild(svgNode("path", {
      d: "M14.12 14.12a3 3 0 1 1-4.24-4.24"
    }));
    icon.appendChild(svgNode("line", {x1: 2, y1: 2, x2: 22, y2: 22}));
    button.appendChild(icon);
    button.addEventListener("click", function () {
      setOrderExclusion(order.item_id, true, button);
    });
    return button;
  }

  function restoreButton(order) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "row-action";
    button.textContent = "Restore";
    button.setAttribute("aria-label", "Restore " + order.name);
    button.addEventListener("click", function () {
      setOrderExclusion(order.item_id, false, button);
    });
    return button;
  }

  function setOrderExclusion(itemId, excluded, button) {
    button.disabled = true;
    fetch("/api/profit/exclusions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_id: itemId, excluded: excluded })
    }).then(function (response) {
      if (response.status === 401) {
        location.href = "/login?next=" + encodeURIComponent(
          location.pathname + location.search);
        return false;
      }
      if (!response.ok) { throw new Error("exclusion request failed"); }
      return true;
    }).then(function (stored) {
      if (!stored) { return; }
      ordersRows.forEach(function (row) {
        if (row.item_id === itemId) { row.excluded = excluded; }
      });
      if (!excluded) {
        // A restored item with no live order has nothing left to show.
        ordersRows = ordersRows.filter(function (row) {
          return row.has_order || row.item_id !== itemId;
        });
      }
      status.className = "";
      status.textContent = excluded
        ? "Hid that item from your open orders."
        : "Restored that item to your open orders.";
      renderOrders();
      trace(excluded ? "order-hidden" : "order-restored", 1);
    }).catch(function () {
      button.disabled = false;
      status.className = "error";
      status.textContent =
        "That change could not be saved. Try again in a moment.";
      trace("hidden-item-failure", 0);
    });
  }

  function openHiddenItems() {
    var dialog = document.getElementById("hidden-dialog");
    var search = document.getElementById("hidden-search");
    search.value = "";
    renderHiddenOrders();
    dialog.showModal();
    search.focus();
    trace("hidden-items-open", hiddenOrders().length);
  }

  function hiddenOrders() {
    // One entry per item, however many order rows that item has.
    var seen = Object.create(null);
    var hidden = [];
    ordersRows.forEach(function (row) {
      if (!row.excluded || seen[row.item_id]) { return; }
      seen[row.item_id] = true;
      hidden.push(row);
    });
    return hidden.sort(function (left, right) {
      return left.name.localeCompare(
        right.name, undefined, { sensitivity: "base", numeric: true });
    });
  }

  function renderHiddenOrders() {
    var body = document.getElementById("hidden-body");
    var count = document.getElementById("hidden-count");
    var search = document.getElementById("hidden-search").value
      .trim().toLowerCase();
    var hidden = hiddenOrders();
    var shown = search
      ? hidden.filter(function (order) {
        return order.name.toLowerCase().indexOf(search) !== -1;
      })
      : hidden;
    body.replaceChildren();
    shown.forEach(function (order) {
      var row = document.createElement("tr");
      cell(row, order.name, "name");
      cell(row, "", "actions").appendChild(restoreButton(order));
      body.appendChild(row);
    });
    if (!shown.length) {
      emptyRow(body, 2, hidden.length
        ? "No hidden items match that search."
        : "You have not hidden any items yet.");
    }
    count.textContent = hidden.length === 1
      ? "1 item is hidden from your open orders."
      : hidden.length + " items are hidden from your open orders.";
    document.getElementById("orders-menu").title = hidden.length
      ? "Hidden items (" + hidden.length + ")"
      : "Hidden items";
    trace("open-orders-hidden", shown.length);
  }

  function renderOrders() {
    var body = document.getElementById("orders-body");
    var help = document.getElementById("orders-key-help");
    help.hidden = ordersAvailable;
    body.replaceChildren();
    var kept = ordersRows.filter(function (row) {
      return row.has_order && !row.excluded;
    });
    // Every footer figure covers the same rows: the ones with a usable
    // current price. Adding an unpriced order's units and cost to totals its
    // profit and ROI cannot include would leave a footer that does not
    // reconcile with itself.
    var units = 0;
    var cost = 0;
    var profit = 0;
    var unpriced = 0;
    kept.forEach(function (order, index) {
      var row = sortableRow(index);
      cell(row, order.name, "name", order.name);
      cell(row, order.quantity, "", order.quantity);
      cell(row, coin(order.unit_price), "", order.unit_price);
      cell(row, coin(order.cost), "", order.cost);
      optionalCoinCell(row, order.buy_price);
      optionalCoinCell(row, order.sell_price);
      optionalProfitCell(row, order.profit);
      optionalProfitCell(row, order.total_profit);
      percentCell(row, order.roi_percent);
      cell(row, "", "actions").appendChild(hideButton(order));
      body.appendChild(row);
      if (order.total_profit === null) {
        unpriced += 1;
        return;
      }
      units += order.quantity;
      cost += order.cost;
      profit += order.total_profit;
    });
    if (!kept.length) {
      emptyRow(body, 10, ordersAvailable
        ? "No open buy orders were found."
        : "Open buy orders are unavailable for this saved key.");
    }
    document.getElementById("orders-unpriced").hidden = unpriced === 0;
    totalRow(document.getElementById("orders-foot"), [
      "Total", units, "\u2014", coin(cost), "\u2014", "\u2014", "\u2014",
      profit, percent(cost ? profit / cost * 100 : null), ""
    ], 7);
    applySort("orders-table");
    renderHiddenOrders();
    trace("open-orders", kept.length);
  }

  function renderDelivery(delivery) {
    var coins = document.getElementById("unclaimed-coins");
    var body = document.getElementById("delivery-body");
    var help = document.getElementById("delivery-key-help");
    body.replaceChildren();
    if (delivery.coins === null) {
      coins.textContent = "Unavailable";
      coins.className = "";
      emptyRow(body, 2, "Delivery is unavailable for this saved key.");
      totalRow(
        document.getElementById("delivery-foot"),
        ["Total", "\u2014"], -1);
      help.hidden = false;
      trace("delivery-unavailable", 0);
      return;
    }
    coins.textContent = coin(delivery.coins);
    coins.className = delivery.coins > 0 ? "positive" : "";
    help.hidden = true;
    var items = delivery.items;
    var quantity = 0;
    items.forEach(function (item, index) {
      var row = sortableRow(index);
      cell(row, item.name, "name", item.name);
      cell(row, item.quantity, "positive", item.quantity);
      body.appendChild(row);
      quantity += item.quantity;
    });
    if (!items.length) {
      emptyRow(body, 2, "No items are waiting for pickup.");
    }
    totalRow(document.getElementById("delivery-foot"),
      ["Total", quantity], -1);
    applySort("delivery-table");
    trace("delivery", items.length);
  }

  function renderReport(data) {
    // The window the server served is the one it remembered, so the control
    // and the address bar follow it rather than the other way round.
    daysInput.value = String(data.days);
    if (data.remembered_days) {
      writeStoredDays(data.days);
    } else if (!restoredWindow) {
      var local = readStoredDays();
      if (local !== null && local !== data.days) {
        restoredWindow = true;
        daysInput.value = String(local);
        trace("window-restored", local);
        load(false);
        return;
      }
      writeStoredDays(data.days);
    }
    if (data.max_days) {
      maxDays = data.max_days;
      daysInput.max = String(maxDays);
    }
    historyStart = data.history_start_date;
    history.replaceState(
      null, "", "/profit?days=" + encodeURIComponent(String(data.days)));
    renderSummary(data);
    renderCharts(data);
    picksData = data.picks;
    renderPicks();
    renderItems(data);
    renderDays(data);
    renderUnrealized(data);
    trace(
      "render-report",
      data.items.length + data.days_table.length
        + data.unrealized.items.length);
  }

  function renderOrdersSection(data) {
    ordersAvailable = data.available;
    ordersRows = data.orders.map(function (order) {
      order.excluded = false;
      return order;
    }).concat(data.excluded.map(function (order) {
      order.excluded = true;
      return order;
    }));
    renderOrders();
  }

  function sectionCards(source) {
    return document.querySelectorAll(
      '.card[data-source="' + source + '"]');
  }

  function markSection(source, state, message) {
    sectionCards(source).forEach(function (card) {
      card.classList.toggle("loading", state === "loading");
      card.classList.toggle("failed", state === "failed");
      var note = card.querySelector(".section-message");
      if (note) {
        note.textContent = message || "Loading\u2026";
      }
    });
    trace("section-" + source, state === "ready" ? 1 : 0);
  }

  function selectedDays() {
    var days = Number(daysInput.value);
    return Number.isInteger(days) && days >= 1 && days <= maxDays
      ? days : null;
  }

  function fetchSection(source, url, render) {
    markSection(source, "loading");
    return fetch(url).then(function (response) {
      if (response.status === 401) {
        location.href = "/login?next=" + encodeURIComponent(
          location.pathname + location.search);
        return null;
      }
      if (response.status === 409) {
        keyHelp.classList.add("open");
        markSection(source, "failed", "A Trading Post API key is required.");
        missingKey = true;
        return null;
      }
      if (!response.ok) { throw new Error(source + " request failed"); }
      return response.json();
    }).then(function (data) {
      if (!data) { return false; }
      render(data);
      markSection(source, "ready");
      return true;
    }).catch(function () {
      markSection(
        source, "failed",
        "This section could not be loaded. Try again in a moment.");
      return false;
    });
  }

  function load(useRemembered) {
    // Asking without a window lets the server answer with the one this member
    // last chose; the page only names a window when they just picked one.
    var days = null;
    if (!useRemembered) {
      days = selectedDays();
      if (days === null) {
        status.className = "error";
        status.textContent =
          "Choose a number of days from 1 through " + maxDays + ".";
        trace("refuse-range", 0);
        return;
      }
    }
    status.className = "";
    status.textContent = "Loading\u2026";
    reports.hidden = false;
    keyHelp.classList.remove("open");
    missingKey = false;
    // Three independent requests, in flight together. Each section draws as
    // soon as its own answer arrives instead of waiting for the slowest.
    // A member who presses Load is asking for the current state, not the
    // one the five-minute snapshot still holds.
    var refresh = useRemembered ? "" : "refresh=1";
    var reportQuery = [
      days === null ? "" : "days=" + encodeURIComponent(String(days)),
      refresh
    ].filter(Boolean).join("&");
    Promise.all([
      fetchSection("report", "/api/profit"
        + (reportQuery ? "?" + reportQuery : ""), renderReport),
      fetchSection("orders", "/api/profit/orders"
        + (refresh ? "?" + refresh : ""), renderOrdersSection),
      fetchSection("delivery", "/api/profit/delivery", renderDelivery)
    ]).then(function (loaded) {
      var ready = loaded.filter(Boolean).length;
      if (missingKey) {
        status.className = "error";
        status.textContent = "A Trading Post API key is required.";
      } else if (ready === loaded.length) {
        status.className = "";
        status.textContent = historyStart
          ? "Updated from your private Trading Post data, held since "
            + historyStart + "."
          : "Updated from your private Trading Post data.";
      } else {
        status.className = "error";
        status.textContent =
          "Some sections could not be loaded. Try again in a moment.";
      }
      trace("load", ready);
    });
  }

  function refreshPrices() {
    // A quiet re-read on the cached path: the server holds each price for a
    // minute and shares it between members, so this beat costs almost
    // nothing and never forces a fetch of its own.
    if (missingKey || document.hidden) { return; }
    fetch("/api/profit/orders").then(function (response) {
      return response.ok ? response.json() : null;
    }).then(function (data) {
      if (!data) { return; }
      renderOrdersSection(data);
      markSection("orders", "ready");
      trace("prices-refreshed", data.orders.length);
    }).catch(function () {
      // A dropped beat is not worth telling the reader about; the next one
      // is a minute away and the numbers on screen are still the last good
      // ones.
      trace("prices-refresh-failed", 0);
    });
  }

  setInterval(refreshPrices, PRICE_REFRESH_MS);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) { refreshPrices(); }
  });

  rangeForm.addEventListener("submit", function (event) {
    event.preventDefault();
    load(false);
  });
  document.getElementById("days-page-size").addEventListener(
    "change", function (event) {
      var value = Number(event.target.value);
      if (!Number.isInteger(value) || value < 1 || value > 90) {
        event.target.value = String(daysPageSize);
        trace("refuse-page-size", 0);
        return;
      }
      daysPageSize = value;
      daysPage = 1;
      paginateDays();
      trace("days-page-size", value);
    });
  document.getElementById("orders-menu").addEventListener(
    "click", openHiddenItems);
  document.getElementById("hidden-close").addEventListener(
    "click", function () {
      document.getElementById("hidden-dialog").close();
    });
  document.getElementById("hidden-search").addEventListener(
    "input", renderHiddenOrders);
  document.getElementById("hidden-dialog").addEventListener(
    "click", function (event) {
      // A modal dialog's backdrop is part of the dialog element, so a click
      // landing on it and not on the panel inside means "outside".
      if (event.target === this) {
        this.close();
        trace("hidden-items-dismiss", 0);
      }
    });
  document.getElementById("picks-roi").addEventListener("click", function () {
    picksMetric = "roi_percent";
    this.setAttribute("aria-pressed", "true");
    document.getElementById("picks-profit").setAttribute("aria-pressed", "false");
    renderPicks();
  });
  document.getElementById("picks-profit").addEventListener("click", function () {
    picksMetric = "profit";
    this.setAttribute("aria-pressed", "true");
    document.getElementById("picks-roi").setAttribute("aria-pressed", "false");
    renderPicks();
  });
  initializeSorters();
  var initial = Number(new URLSearchParams(location.search).get("days"));
  var requested = Number.isInteger(initial)
    && initial >= 1 && initial <= maxDays;
  if (requested) {
    daysInput.value = String(initial);
  }
  fetch("/api/me")
    .then(function (response) { return response.ok ? response.json() : null; })
    .then(function (identity) {
      if (identity) { document.getElementById("whoami").textContent = identity.name; }
    });
  load(!requested);
}());
</script>
</body>
</html>
"""
)

# The window the page offers has to match the one the API will accept, so the
# bound is written once, in the store, and stamped into the page here.
PROFIT_PAGE = _PROFIT_PAGE_TEMPLATE.replace(
    "__MAX_DAYS__",
    str(MAX_REPORT_DAYS),
)
