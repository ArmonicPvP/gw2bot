"""The feast usage dashboard page.

Dynamic data reaches the page only through JSON APIs and is inserted
with ``textContent`` on the client, never as an HTML string.
"""

from __future__ import annotations

from gw2bot.web.pages.shared import (
    CUSTOM_RANGE_PANEL,
    DASHBOARD_HEADER_STYLE,
    RANGE_PICKER_LISTENERS_JS,
    RANGE_PICKER_NAV,
    RANGE_PICKER_STYLE,
    SHARED_STYLE,
    range_picker_js,
)

FOOD_PAGE = (
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Feast Usage</title>
<style>"""
    + SHARED_STYLE
    + DASHBOARD_HEADER_STYLE
    + RANGE_PICKER_STYLE
    + """
body {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}
main {
  flex: 1;
  width: 100%;
  max-width: 62rem;
  margin: 0 auto;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 1rem;
}
.card h2 { font-size: 0.95rem; }
.chart-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  margin-bottom: 0.6rem;
}
.chart-mode {
  min-width: 2rem;
  padding: 0.25rem 0.45rem;
  font-size: 1rem;
  line-height: 1;
}
/* The legend sits under the chart as a row of colour swatches. Each swatch is
   a button so a tap can reveal the feast it stands for. */
.legend {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 0.5rem 1.25rem;
  margin-top: 0.6rem;
}
.legend .item {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.82rem;
  background: none;
  border: none;
  padding: 0.2rem 0.25rem;
  color: var(--text);
  cursor: pointer;
}
.legend .swatch { width: 0.9rem; height: 0.9rem; border-radius: 3px; flex-shrink: 0; }
.legend .legend-name { display: inline; }
/* A feast switched off keeps its place in the legend, dimmed and with its
   colour reduced to an outline, so what is missing from the chart is still
   named and one more click puts it back. */
.legend .item.off { opacity: 0.55; }
/* The chart is a fixed-viewBox SVG that scales to its container width, so
   every plotted coordinate is computed once against the viewBox and the
   browser handles resizing without a re-render. */
.chart-svg { width: 100%; height: auto; display: block; }
.chart-svg .axis { stroke: var(--border); stroke-width: 1; }
.chart-svg .grid { stroke: var(--border); stroke-width: 1; opacity: 0.35; }
.chart-svg text { fill: var(--muted); font-size: 11px; font-family: inherit; }
.chart-svg .y-label { text-anchor: end; }
.chart-svg .x-label { text-anchor: middle; }
.chart-svg .series-line { fill: none; stroke-width: 2; }
.chart-svg .series-dot { stroke: var(--panel); stroke-width: 1; }
.chart-svg .overlay { fill: transparent; }
/* A thin, translucent gray line the hover snaps to the nearest sample. */
.chart-svg .crosshair {
  stroke: rgba(128, 128, 128, 0.45);
  stroke-width: 1;
  pointer-events: none;
}
.chart-svg .hover-ring { fill: none; stroke-width: 2; pointer-events: none; }
/* #chart is the positioning context for the hover tooltip, which is an HTML
   box overlaid on the SVG so its text wraps and inherits page styling. */
#chart { position: relative; }
/* Every chart below the stock graph is the same box: the positioning context
   its hover tooltip is placed in. */
.chart-box { position: relative; }
/* One line under a heading saying what the chart below it plots, because a
   rolling average and a daily total look alike and are not. */
.chart-note {
  color: var(--muted);
  font-size: 0.8rem;
  margin: -0.35rem 0 0.6rem;
}
.chart-status { color: var(--muted); font-size: 0.85rem; padding-top: 0.5rem; }
.chart-tooltip {
  position: absolute;
  z-index: 2;
  min-width: 8rem;
  padding: 0.45rem 0.55rem;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 0.78rem;
  color: var(--text);
  pointer-events: none;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.35);
}
.chart-tooltip .tip-time { color: var(--muted); margin-bottom: 0.3rem; }
.chart-tooltip .tip-row { display: flex; align-items: center; gap: 0.4rem; }
.chart-tooltip .tip-row .swatch {
  width: 0.7rem;
  height: 0.7rem;
  border-radius: 2px;
  flex-shrink: 0;
}
.chart-tooltip .tip-row .val {
  margin-left: auto;
  padding-left: 0.75rem;
  font-variant-numeric: tabular-nums;
}
.chart-tooltip .tip-row.em { font-weight: 600; }
#chart-status { color: var(--muted); font-size: 0.85rem; padding-top: 0.5rem; }
.tabs { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-bottom: 0.75rem; }
.tabs button { font-size: 0.8rem; }
table.removals, table.additions {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}
table.removals th, table.removals td,
table.additions th, table.additions td {
  text-align: left;
  padding: 0.4rem 0.6rem;
  border-bottom: 1px solid var(--border);
}
table.removals th, table.additions th {
  color: var(--muted);
  font-weight: 600;
}
table.removals td.num, table.additions td.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
/* Additions are that table with three more columns, so they share every rule
   above and add only what the extra columns need. */
/* Six narrow columns read as a row, so only the account names wrap; the
   rest keep their line and the table scrolls instead. */
table.additions th, table.additions td { white-space: nowrap; }
table.additions td.user { white-space: normal; }
table.additions th.actions, table.additions td.actions { text-align: right; }
/* A restock nobody has priced yet is the row an officer opened this section
   to act on, so it is tinted rather than left to be found by reading down
   the column. */
table.additions tr.unpriced td { background: rgba(231, 76, 60, 0.22); }
/* Six columns are more than a phone has room for, so the table scrolls
   sideways inside its card rather than pushing the page wider. */
.table-scroll { overflow-x: auto; }
.table-scroll table { min-width: 34rem; }
.icon-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0.25rem;
  background: transparent;
  border-color: transparent;
  color: var(--muted);
  line-height: 0;
}
.icon-button:hover { color: var(--text); background: var(--panel-2); }
.icon-button svg { pointer-events: none; }
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
/* The cost editor. The shared reset zeroes every margin, which takes the
   centring a modal dialog normally gets from the user agent with it. */
.cost-dialog {
  margin: auto;
  width: min(22rem, calc(100vw - 2rem));
  padding: 1rem;
  background: var(--panel);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 10px;
}
.cost-dialog::backdrop { background: rgba(0, 0, 0, 0.55); }
.cost-dialog h2 { font-size: 1rem; margin-bottom: 0.35rem; }
.cost-subject { color: var(--muted); font-size: 0.82rem; }
/* The three boxes read as one price: {n}g {n}s {n}c on a single line. */
.cost-row {
  display: flex;
  align-items: baseline;
  justify-content: center;
  gap: 0.5rem;
  margin: 0.9rem 0 0.4rem;
}
.coin-field { display: inline-flex; align-items: baseline; gap: 0.15rem; }
.coin-field input {
  width: 4.5rem;
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.35rem 0.4rem;
  font: inherit;
  font-size: 0.9rem;
  text-align: right;
  font-variant-numeric: tabular-nums;
  color-scheme: dark;
}
/* The spinner arrows sit on top of a three-digit figure in a box this
   narrow, and a price is typed rather than stepped to. */
.coin-field input {
  appearance: textfield;
  -moz-appearance: textfield;
}
.coin-field input::-webkit-outer-spin-button,
.coin-field input::-webkit-inner-spin-button {
  -webkit-appearance: none;
  margin: 0;
}
.coin-unit { color: var(--muted); font-size: 0.85rem; }
.cost-error { color: var(--full); font-size: 0.82rem; min-height: 1.2em; }
.cost-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-top: 0.6rem;
}
.empty { color: var(--muted); padding: 0.6rem; }
.pager {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-top: 0.75rem;
  color: var(--muted);
  font-size: 0.85rem;
}
button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
@media (max-width: 640px) {
  main { padding: 0.6rem 0.5rem; }
  .card { padding: 0.6rem; }
  /* Names are hidden, leaving a compact colour key. A feast switched off
     names itself, so a tap still answers "which one is this?" - it answers by
     taking the line away and labelling what went. */
  .legend .legend-name { display: none; }
  .legend .item.off .legend-name { display: inline; }
}
</style>
</head>
<body>
<header>
  <h1 id="brand">Feast Usage</h1>
"""
    + RANGE_PICKER_NAV
    + """
  <span class="spacer"></span>
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
"""
    + CUSTOM_RANGE_PANEL
    + """
</header>
<main>
  <section class="card">
    <div class="chart-heading">
      <h2>Stock on hand over time</h2>
      <button type="button" id="chart-mode" class="chart-mode"
        aria-label="Switch to staircase graph" title="Regular line graph">╱</button>
    </div>
    <div id="chart"></div>
    <div id="legend" class="legend" role="list" aria-label="Feast colours"></div>
    <div id="chart-status" role="status" aria-live="polite"></div>
  </section>
  <section class="card">
    <div class="chart-heading">
      <h2>Food usage &ndash; 7-day rolling average</h2>
    </div>
    <p class="chart-note">Feasts taken per day, averaged over the seven days
      ending on each date. Days are cut in UTC.</p>
    <div id="chart-usage-avg" class="chart-box"></div>
    <div id="legend-usage-avg" class="legend" role="list"
      aria-label="Feast colours"></div>
    <div id="status-usage-avg" class="chart-status" role="status"
      aria-live="polite"></div>
  </section>
  <section class="card">
    <div class="chart-heading">
      <h2>Food cost &ndash; 7-day rolling average</h2>
    </div>
    <p class="chart-note">What the feasts used each day had cost, averaged
      over the seven days ending on each date. Each feast is priced at the
      restock it came from, oldest stock first, so a new deposit is not a
      spike: it only raises the cost once the older stock is used up.</p>
    <div id="chart-cost-avg" class="chart-box"></div>
    <div id="legend-cost-avg" class="legend" role="list"
      aria-label="Feast colours"></div>
    <div id="status-cost-avg" class="chart-status" role="status"
      aria-live="polite"></div>
  </section>
  <section class="card">
    <div class="chart-heading">
      <h2>Average food cost by day</h2>
    </div>
    <p class="chart-note">What the feasts used that day had cost, priced the
      same way, with no averaging: two feasts used from a restock of thirty
      bought for thirty gold is two gold. The hover names how many were used
      and what each cost.</p>
    <div id="chart-cost-by-day" class="chart-box"></div>
    <div id="legend-cost-by-day" class="legend" role="list"
      aria-label="Feast colours"></div>
    <div id="status-cost-by-day" class="chart-status" role="status"
      aria-live="polite"></div>
  </section>
  <section class="card">
    <div class="chart-heading">
      <h2>Total food cost</h2>
    </div>
    <p class="chart-note">What every drawn feast used in the window had cost,
      added up. Each point is the running total to the end of that day, and
      the hover also names what the day alone cost.</p>
    <div id="chart-total-cost" class="chart-box"></div>
    <div id="status-total-cost" class="chart-status" role="status"
      aria-live="polite"></div>
  </section>
  <section class="card">
    <div class="chart-heading">
      <h2>Food cost by food</h2>
    </div>
    <p class="chart-note">The same running total, one line per feast, so the
      shelf the gold went on is the line it is read off.</p>
    <div id="chart-cost-by-food" class="chart-box"></div>
    <div id="legend-cost-by-food" class="legend" role="list"
      aria-label="Feast colours"></div>
    <div id="status-cost-by-food" class="chart-status" role="status"
      aria-live="polite"></div>
  </section>
  <section class="card">
    <h2>Removals</h2>
    <div id="tabs" class="tabs"></div>
    <div id="table"></div>
    <div id="pager" class="pager"></div>
  </section>
  <section class="card">
    <h2>Additions</h2>
    <div id="add-tabs" class="tabs"></div>
    <div id="add-table"></div>
    <div id="add-pager" class="pager"></div>
  </section>
</main>
<dialog id="cost-dialog" class="cost-dialog" aria-labelledby="cost-title">
  <form id="cost-form">
    <h2 id="cost-title">Cost</h2>
    <p id="cost-subject" class="cost-subject"></p>
    <div class="cost-row">
      <span class="coin-field">
        <label class="visually-hidden" for="cost-gold">Gold</label>
        <input id="cost-gold" type="number" min="0" step="1"
          inputmode="numeric" autocomplete="off">
        <span class="coin-unit" aria-hidden="true">g</span>
      </span>
      <span class="coin-field">
        <label class="visually-hidden" for="cost-silver">Silver</label>
        <input id="cost-silver" type="number" min="0" step="1"
          inputmode="numeric" autocomplete="off">
        <span class="coin-unit" aria-hidden="true">s</span>
      </span>
      <span class="coin-field">
        <label class="visually-hidden" for="cost-copper">Copper</label>
        <input id="cost-copper" type="number" min="0" step="1"
          inputmode="numeric" autocomplete="off">
        <span class="coin-unit" aria-hidden="true">c</span>
      </span>
    </div>
    <p id="cost-error" class="cost-error" role="status" aria-live="polite"></p>
    <div class="cost-actions">
      <button type="button" id="cost-cancel">Cancel</button>
      <button type="submit" id="cost-save">Save</button>
    </div>
  </form>
</dialog>
<script>
"use strict";
(function () {
  // Okabe-Ito colourblind-safe categorical palette, one hue per tracked feast.
  var COLORS = ["#56B4E9", "#E69F00", "#009E73", "#CC79A7"];
  var SVG_NS = "http://www.w3.org/2000/svg";
  var TABLE_PAGE_SIZE = 5;
  var COPPER_PER_SILVER = 100;
  var COPPER_PER_GOLD = 100 * COPPER_PER_SILVER;
  // The ceiling the server holds a recorded cost to, mirrored here so a
  // mistyped figure is named at the keyboard rather than coming back as a
  // rejected save.
  var MAX_COST_COPPER = 2000000 * COPPER_PER_GOLD;
  // Smallest count the y axis ever reaches, so a window that never rose above
  // a couple of feasts still gets readable gridlines rather than a scale
  // squeezed onto one or two of them.
  var MIN_TOP = 10;

  var mobileQuery = window.matchMedia("(max-width: 640px)");
  function isMobile() { return mobileQuery.matches; }

  // The chart uses a wide viewBox on desktop and a taller one on mobile, where
  // it scales to the narrow screen width; the extra height makes the graph
  // read large on a phone. Coordinates are computed against whichever set is
  // active, so M is refreshed at the start of every chart render. The left
  // margin matches the roster page's, because the axis is no longer capped at
  // two digits and a four-figure stock has to fit beside it.
  function metrics() {
    if (isMobile()) {
      return {
        w: 480, h: 620, top: 16, right: 14, bottom: 36, left: 40, ticks: 4
      };
    }
    return {
      w: 960, h: 380, top: 16, right: 16, bottom: 32, left: 40, ticks: 6
    };
  }
  var M = metrics();
  function plotW() { return M.w - M.left - M.right; }
  function plotH() { return M.h - M.top - M.bottom; }

  // hidden holds the feasts the reader has switched off in the legend, keyed
  // by guild storage id so the choice outlives a range change and the redraw
  // it brings.
  var state = {
    // No range until the server answers: the first load asks for the window
    // this member last picked rather than naming one over the top of it.
    range: null, data: null, activeFeast: 0, tablePage: 0, hidden: {},
    staircase: false, scale: null,
    // The Additions table keeps its own feast and page: an officer pricing a
    // restock is reading a different question than the removals above it.
    activeAddition: 0, additionPage: 0,
    // The restock the cost dialog is open over, or null while it is closed.
    editing: null
  };

  // A pinned touch selection listens on the whole page, so the chart it
  // belongs to is torn down before another one is drawn.
  var detachHover = null;

  var legend = document.getElementById("legend");
  var chart = document.getElementById("chart");
  var chartStatus = document.getElementById("chart-status");
  var chartMode = document.getElementById("chart-mode");
  var tabs = document.getElementById("tabs");
  var tableBox = document.getElementById("table");
  var pager = document.getElementById("pager");
  var addTabs = document.getElementById("add-tabs");
  var addTableBox = document.getElementById("add-table");
  var addPager = document.getElementById("add-pager");
  var costDialog = document.getElementById("cost-dialog");
  var costForm = document.getElementById("cost-form");
  var costSubject = document.getElementById("cost-subject");
  var costError = document.getElementById("cost-error");
  var costSave = document.getElementById("cost-save");
  var costFields = {
    gold: document.getElementById("cost-gold"),
    silver: document.getElementById("cost-silver"),
    copper: document.getElementById("cost-copper")
  };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }
  function svg(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        node.setAttribute(key, attrs[key]);
      });
    }
    return node;
  }

  function feasts() {
    return (state.data && state.data.feasts) || [];
  }
  function activeFeast() {
    return feasts()[state.activeFeast] || null;
  }
  function isHidden(feast) {
    return state.hidden[feast.id] === true;
  }
  function visibleFeasts() {
    return feasts().filter(function (feast) { return !isHidden(feast); });
  }

  function traceMode(mode, count) {
    console.debug("feast chart mode:", mode, count);
  }

  function scaleX(t) {
    var since = state.data.since;
    var now = state.data.now;
    var span = now - since;
    var frac = span > 0 ? (t - since) / span : 0;
    if (frac < 0) { frac = 0; }
    if (frac > 1) { frac = 1; }
    return M.left + frac * plotW();
  }
  // A gridline step of 1, 2 or 5 times a power of ten, which is what makes
  // the axis read as round counts rather than as arbitrary divisions.
  function niceStep(span, target) {
    var raw = span / target;
    if (!(raw > 0)) { return 1; }
    var magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
    var normalized = raw / magnitude;
    var step = 10;
    if (normalized <= 1) { step = 1; }
    else if (normalized <= 2) { step = 2; }
    else if (normalized <= 5) { step = 5; }
    return step * magnitude;
  }

  // The axis covers the counts actually reached in the window, padded out to
  // MIN_TOP and rounded up to a readable step, so a stock the guild has grown
  // past fifty is drawn in full instead of flattened against a fixed ceiling.
  // The baseline stays at zero: several feasts share the axis, and running out
  // is the thing the page is read for, so an empty shelf has to sit on the
  // floor of the chart rather than somewhere up its side.
  function computeScale() {
    var high = MIN_TOP;
    visibleFeasts().forEach(function (feast) {
      (feast.points || []).forEach(function (point) {
        if (point.count > high) { high = point.count; }
      });
    });
    // Headroom above the highest sample, so the peak of a line is not drawn
    // on the topmost gridline.
    var span = high * 1.1;
    var step = niceStep(span, 6);
    return { high: step * Math.ceil(span / step), step: step };
  }

  function scaleY(count) {
    var high = state.scale.high;
    var value = count;
    if (value < 0) { value = 0; }
    if (value > high) { value = high; }
    return M.top + (1 - value / high) * plotH();
  }

  // How wide the drawn window is, in seconds.
  function windowSpan() {
    return state.data ? state.data.now - state.data.since : 0;
  }

  function formatTick(t) {
    var date = new Date(t * 1000);
    // A window of about a day or less is read off the clock and a wider one
    // off the calendar. The span decides rather than the range's name, so a
    // custom pair of dates is labelled like the preset it resembles.
    if (windowSpan() <= 48 * 60 * 60) {
      return date.toLocaleTimeString(
        undefined, { hour: "numeric", minute: "2-digit" });
    }
    return date.toLocaleDateString(
      undefined, { month: "numeric", day: "numeric" });
  }
  function formatMoment(t) {
    return new Date(t * 1000).toLocaleString(
      undefined,
      {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit"
      });
  }

  function renderChart() {
    M = metrics();
    state.scale = computeScale();
    if (detachHover) { detachHover(); detachHover = null; }
    chart.replaceChildren();
    var canvas = svg("svg", {
      "class": "chart-svg",
      viewBox: "0 0 " + M.w + " " + M.h,
      role: "img",
      "aria-label": "Stock on hand over time, one line per feast"
    });

    // Horizontal gridlines and y labels at every step of the computed scale.
    var lines = Math.round(state.scale.high / state.scale.step);
    for (var line = 0; line <= lines; line += 1) {
      var value = state.scale.step * line;
      var y = scaleY(value);
      canvas.appendChild(svg("line", {
        "class": line === 0 ? "axis" : "grid",
        x1: M.left, y1: y, x2: M.left + plotW(), y2: y
      }));
      var yLabel = svg("text", {
        "class": "y-label", x: M.left - 6, y: y + 4
      });
      yLabel.textContent = String(value);
      canvas.appendChild(yLabel);
    }

    // Left axis, plus x labels spaced evenly across the whole window so the
    // range spans the full width even when few points were recorded.
    canvas.appendChild(svg("line", {
      "class": "axis",
      x1: M.left, y1: M.top, x2: M.left, y2: M.top + plotH()
    }));
    for (var i = 0; i <= M.ticks; i += 1) {
      var t = state.data.since +
        (state.data.now - state.data.since) * (i / M.ticks);
      var x = scaleX(t);
      var xLabel = svg("text", {
        "class": "x-label", x: x, y: M.top + plotH() + 18
      });
      xLabel.textContent = formatTick(t);
      canvas.appendChild(xLabel);
    }

    // One polyline plus point markers per feast, each in its own colour. Every
    // recorded sample is drawn; the series is never downsampled. Each plotted
    // marker is also collected so the hover can snap to it.
    var plotted = [];
    feasts().forEach(function (feast, index) {
      // A feast switched off in the legend is left out of the drawing
      // entirely, so it is absent from the hover and the tooltip too rather
      // than invisible but still selectable.
      if (isHidden(feast)) { return; }
      var color = COLORS[index % COLORS.length];
      var points = feast.points || [];
      if (points.length > 1) {
        var coords = [];
        points.forEach(function (point, pointIndex) {
          var px = scaleX(point.t);
          if (state.staircase && pointIndex > 0) {
            coords.push(px.toFixed(1) + "," +
              scaleY(points[pointIndex - 1].count).toFixed(1));
          }
          coords.push(px.toFixed(1) + "," +
            scaleY(point.count).toFixed(1));
        });
        canvas.appendChild(svg("polyline", {
          "class": "series-line", stroke: color, points: coords.join(" ")
        }));
      }
      points.forEach(function (point) {
        var px = scaleX(point.t);
        var py = scaleY(point.count);
        canvas.appendChild(svg("circle", {
          "class": "series-dot",
          cx: px.toFixed(1),
          cy: py.toFixed(1),
          r: 3,
          fill: color
        }));
        plotted.push({
          x: px,
          y: py,
          t: point.t,
          count: point.count,
          name: feast.name,
          color: color,
          feast: index
        });
      });
    });

    detachHover = attachHover(canvas, plotted);
    chart.appendChild(canvas);

    chartStatus.textContent = chartStatusText(plotted.length);
  }

  chartMode.addEventListener("click", function () {
    state.staircase = !state.staircase;
    chartMode.textContent = state.staircase ? "⎿" : "╱";
    chartMode.title = state.staircase ? "Staircase graph" : "Regular line graph";
    chartMode.setAttribute("aria-label", state.staircase
      ? "Switch to regular line graph" : "Switch to staircase graph");
    traceMode(state.staircase ? "staircase" : "regular", feasts().length);
    if (state.data) { renderChart(); }
  });

  // What the chart says about itself when it has drawn nothing: an empty
  // window and a legend switched all the way off are different states, and
  // only one of them is worth waiting for more data over.
  function chartStatusText(plottedCount) {
    if (plottedCount) { return ""; }
    if (feasts().length && !visibleFeasts().length) {
      return "Every feast is switched off. Click one in the legend to draw " +
        "it again.";
    }
    return "No feast counts were recorded in this period.";
  }

  // Samples that share a timestamp (one storage poll can log several feasts at
  // once) form a single column, so the crosshair snaps to one x and the
  // tooltip lists every value recorded there.
  function groupColumns(plotted) {
    var byTime = {};
    var columns = [];
    plotted.forEach(function (point) {
      var key = String(point.t);
      var column = byTime[key];
      if (!column) {
        column = { t: point.t, x: point.x, points: [] };
        byTime[key] = column;
        columns.push(column);
      }
      column.points.push(point);
    });
    columns.forEach(function (column) {
      column.points.sort(function (a, b) { return a.feast - b.feast; });
    });
    return columns;
  }

  // Tells a hovering pointer from a finger or a pen. A touch has no hover
  // state: the browser sends one pointermove at the tap point and then a
  // pointerleave as the finger lifts, which is why a tap used to flash the
  // crosshair and lose it again. Touch selects by tapping instead and never
  // reaches the move or leave handlers.
  function isHoverPointer(event) {
    return !event.pointerType || event.pointerType === "mouse";
  }

  // How far a finger may travel from where it landed and still count as a tap
  // rather than the start of a scroll, in CSS pixels.
  var TAP_SLOP = 12;

  // Pointer and event types are narrowed to the names the spec defines before
  // they are traced, so an exotic value cannot ride into the console.
  function pointerKind(event) {
    var kind = event && event.pointerType;
    if (kind === "mouse" || kind === "pen" || kind === "touch") {
      return kind;
    }
    return "other";
  }
  function eventKind(event) {
    var name = event && event.type;
    if (name === "pointerdown" || name === "wheel" ||
        name === "keydown" || name === "blur") {
      return name;
    }
    return "other";
  }

  // Sanitized tracing for the tap selection lifecycle, so a console trace can
  // explain why a selection opened, moved or went away. Every call passes a
  // fixed action name, one of the narrowed reason names above, and a count of
  // drawn elements. Coordinates, timestamps, stock values and feast names are
  // never passed, so no part of the payload or of the reader's gesture reaches
  // the console. debug keeps it out of the default console view.
  function traceSelection(action, reason, count) {
    console.debug("feast chart selection:", action, reason, count);
  }

  // host is the positioning box the tooltip is placed in and headline names a
  // column; both default to the stock chart's, so its own call site reads the
  // way it always has while the daily charts below pass their own.
  function attachHover(canvas, plotted, host, headline) {
    var box = host || chart;
    var head = headline || formatMoment;
    var columns = groupColumns(plotted);
    // The viewBox differs between the mobile and desktop layouts, so the hover
    // is pinned to the metrics this canvas was drawn with rather than to
    // whichever set is current when a pointer event arrives.
    var m = M;
    var innerW = m.w - m.left - m.right;
    var innerH = m.h - m.top - m.bottom;
    // Set while a tap holds a column open, together with the page listeners
    // that dismiss it. A mouse hover never arms them.
    var pinned = false;
    var pinOrigin = null;

    var crosshair = svg("line", {
      "class": "crosshair",
      y1: m.top,
      y2: m.top + innerH
    });
    crosshair.style.visibility = "hidden";
    var rings = svg("g");
    var overlay = svg("rect", {
      "class": "overlay",
      x: m.left,
      y: m.top,
      width: innerW,
      height: innerH
    });
    overlay.style.cursor = "crosshair";
    canvas.appendChild(crosshair);
    canvas.appendChild(rings);
    canvas.appendChild(overlay);

    var tooltip = el("div", "chart-tooltip");
    tooltip.style.visibility = "hidden";
    box.appendChild(tooltip);

    function nearestColumn(vbX) {
      var best = null;
      var bestDist = Infinity;
      columns.forEach(function (column) {
        var dist = Math.abs(column.x - vbX);
        if (dist < bestDist) { bestDist = dist; best = column; }
      });
      return best;
    }

    function showTooltip(column, emphasized) {
      tooltip.replaceChildren();
      tooltip.appendChild(el("div", "tip-time", head(column.t)));
      column.points.forEach(function (point) {
        var row = el("div",
          "tip-row" + (point === emphasized ? " em" : ""));
        var swatch = el("span", "swatch");
        swatch.style.background = point.color;
        row.appendChild(swatch);
        row.appendChild(el("span", "name", point.name));
        row.appendChild(el("span", "val", point.text !== undefined
          ? point.text
          : String(point.count)));
        tooltip.appendChild(row);
      });
      // Anchor to the point nearest the cursor and flip below the axis top
      // when there is no room to sit above it.
      var leftPct = Math.max(10, Math.min(90, emphasized.x / m.w * 100));
      var topPct = emphasized.y / m.h * 100;
      tooltip.style.left = leftPct + "%";
      tooltip.style.top = topPct + "%";
      tooltip.style.transform = topPct < 32
        ? "translate(-50%, 14px)"
        : "translate(-50%, calc(-100% - 14px))";
      tooltip.style.visibility = "visible";
    }

    function showHover(column, vbY) {
      crosshair.setAttribute("x1", column.x);
      crosshair.setAttribute("x2", column.x);
      crosshair.style.visibility = "visible";
      rings.replaceChildren();
      var emphasized = column.points[0];
      var bestDy = Infinity;
      column.points.forEach(function (point) {
        rings.appendChild(svg("circle", {
          "class": "hover-ring",
          cx: point.x,
          cy: point.y,
          r: 5,
          stroke: point.color
        }));
        var dy = Math.abs(point.y - vbY);
        if (dy < bestDy) { bestDy = dy; emphasized = point; }
      });
      showTooltip(column, emphasized);
    }

    function hideHover() {
      crosshair.style.visibility = "hidden";
      rings.replaceChildren();
      tooltip.style.visibility = "hidden";
      unpin();
    }

    // Translates a pointer position into viewBox coordinates, or null while
    // the canvas has no laid-out size to measure against.
    function pointFromEvent(event) {
      var rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) { return null; }
      return {
        x: (event.clientX - rect.left) / rect.width * m.w,
        y: (event.clientY - rect.top) / rect.height * m.h
      };
    }

    // Clears the selection and traces why it went away, but only when a tap
    // was holding it open. A mouse hover follows the pointer continuously, so
    // tracing every move that ends one would bury the trace it exists to give.
    function release(reason) {
      var wasPinned = pinned;
      hideHover();
      if (wasPinned) { traceSelection("release", reason, columns.length); }
    }

    // Anything other than another tap on the plot clears a pinned selection: a
    // tap elsewhere on the page, a wheel, a key, or the window losing focus.
    function dismiss(event) {
      // A tap that moves the selection to another column reaches the overlay
      // after this capture listener has already run, so it is left to the
      // overlay's own handler. Only a tap earns that exemption: a wheel over
      // the plot, or a mouse press on it, is aimed at the overlay too, but the
      // handler there acts on neither, so waving those through would strand
      // the selection on screen with nothing left to clear it.
      if (isRetargetingTap(event)) {
        traceSelection("keep", "retarget-on-plot", columns.length);
        return;
      }
      release("page-" + eventKind(event));
    }

    // True only for the events the overlay's pointerdown handler will act on,
    // which is what makes leaving them to it safe.
    function isRetargetingTap(event) {
      return !!event && event.type === "pointerdown" &&
        event.target === overlay && !isHoverPointer(event);
    }

    function pin(event, kind) {
      pinOrigin = { x: event.clientX, y: event.clientY };
      if (pinned) { return; }
      pinned = true;
      document.addEventListener("pointerdown", dismiss, true);
      document.addEventListener("wheel", dismiss, true);
      document.addEventListener("keydown", dismiss, true);
      window.addEventListener("blur", dismiss);
      traceSelection("pin", kind, columns.length);
    }

    function unpin() {
      if (!pinned) { return; }
      pinned = false;
      pinOrigin = null;
      document.removeEventListener("pointerdown", dismiss, true);
      document.removeEventListener("wheel", dismiss, true);
      document.removeEventListener("keydown", dismiss, true);
      window.removeEventListener("blur", dismiss);
    }

    // Resolves the column a pointer is over. When there is nothing to show,
    // reason names why so the caller can trace the skip; the names are fixed
    // strings, never anything read off the event or the payload.
    function resolveColumn(event) {
      if (!columns.length) { return { column: null, reason: "no-samples" }; }
      var at = pointFromEvent(event);
      if (!at) { return { column: null, reason: "unsized-canvas" }; }
      var column = nearestColumn(at.x);
      if (!column) { return { column: null, reason: "no-nearest" }; }
      return { column: column, at: at, reason: "ok" };
    }

    overlay.addEventListener("pointermove", function (event) {
      if (!isHoverPointer(event)) {
        // A finger that travels past the tap slop is scrolling the page, not
        // picking a point, so the selection it opened is dropped.
        if (pinned && pinOrigin) {
          var dx = event.clientX - pinOrigin.x;
          var dy = event.clientY - pinOrigin.y;
          if (Math.sqrt(dx * dx + dy * dy) > TAP_SLOP) { release("drag"); }
        }
        return;
      }
      var hovered = resolveColumn(event);
      if (hovered.column) { showHover(hovered.column, hovered.at.y); }
    });
    overlay.addEventListener("pointerleave", function (event) {
      // A finger's pointerleave arrives as it lifts off the glass; only a
      // mouse leaving the plot means its hover is over.
      if (isHoverPointer(event)) { release("pointer-leave"); }
    });
    // Touch and pen select by tapping: the nearest column opens and stays up
    // until the next interaction, and a tap on another point moves it there.
    overlay.addEventListener("pointerdown", function (event) {
      if (isHoverPointer(event)) { return; }
      var kind = pointerKind(event);
      var tapped = resolveColumn(event);
      if (!tapped.column) {
        // Nothing to open, so the tap is reported and any selection already
        // showing is cleared rather than left behind as a stale reading.
        traceSelection("skip", tapped.reason, columns.length);
        release("skipped-tap");
        return;
      }
      // Tracing the move apart from the open is what shows a trace reader that
      // a second tap replaced the first selection instead of adding to it.
      var moved = pinned;
      pin(event, kind);
      showHover(tapped.column, tapped.at.y);
      traceSelection(
        moved ? "move" : "open", kind, tapped.column.points.length);
    });
    // The browser claims the gesture once it decides a touch is a scroll.
    overlay.addEventListener("pointercancel", function (event) {
      if (!isHoverPointer(event)) { release("pointer-cancel"); }
    });

    // Lets a re-render drop this canvas's page-level listeners with it.
    return function () { release("redraw"); };
  }

  // Sanitized tracing for the legend, so a console trace can explain why the
  // chart is drawing fewer lines than the window holds. Only a fixed action
  // name and a count of the feasts left on are passed; no feast name, count
  // or timestamp reaches the console.
  function traceLegend(action, count) {
    console.debug("feast chart legend:", action, count);
  }

  // host is the box the swatches are drawn into, defaulting to the stock
  // chart's legend. Every legend on the page switches the same feasts, so a
  // click redraws all of them along with the charts they belong to.
  function renderLegend(host) {
    var box = host || legend;
    box.replaceChildren();
    feasts().forEach(function (feast, index) {
      // Each entry is a button that switches its feast off and back on. The
      // name is always exposed to assistive tech through aria-label, and
      // aria-pressed carries whether the feast is currently drawn.
      var hidden = isHidden(feast);
      var item = el("button", hidden ? "item off" : "item");
      item.type = "button";
      item.setAttribute("aria-label", feast.name);
      item.setAttribute("aria-pressed", hidden ? "false" : "true");
      item.title = feast.name;
      var color = COLORS[index % COLORS.length];
      var swatch = el("span", "swatch");
      if (hidden) {
        swatch.style.background = "transparent";
        swatch.style.boxShadow = "inset 0 0 0 2px " + color;
      } else {
        swatch.style.background = color;
      }
      item.appendChild(swatch);
      item.appendChild(el("span", "legend-name", feast.name));
      item.addEventListener("click", function () {
        if (hidden) {
          delete state.hidden[feast.id];
        } else {
          state.hidden[feast.id] = true;
        }
        traceLegend(hidden ? "show" : "hide", visibleFeasts().length);
        renderLegend();
        renderChart();
        renderDailyCharts();
      });
      box.appendChild(item);
    });
  }

  // --- The daily charts ---------------------------------------------------

  // Every chart below the stock graph is the same drawing: one point per UTC
  // day of the drawn window, read off an axis the values themselves set. Each
  // entry names the section it fills, the value it takes from a day, and
  // whether that value is a price, which decides both the axis labels and the
  // reading in the tooltip.
  //
  // The days arrive bucketed and averaged: a seven-day average on the
  // window's first day is worked out over the week before it, which is a week
  // the page is never sent.
  var DAILY_CHARTS = [
    {
      id: "usage-avg",
      label: "Feasts used per day, 7-day rolling average, one line per feast",
      empty: "No feasts were used in this period.",
      money: false,
      value: function (day) { return day.used_avg; }
    },
    {
      id: "cost-avg",
      label: "Cost of the feasts used per day, 7-day rolling average, " +
        "one line per feast",
      empty: "No feasts were used in this period.",
      money: true,
      value: function (day) { return day.cost_avg; }
    },
    {
      id: "cost-by-day",
      label: "Cost of the feasts used each day, one line per feast",
      empty: "No feasts were used in this period.",
      money: true,
      value: function (day) { return day.cost; },
      // The day's cost alone does not say whether it was many cheap feasts
      // or a few dear ones, so the hover names both halves of it.
      text: function (day, value) {
        if (!day.used) { return formatCoins(value); }
        return formatCoins(value) + " (" + day.used + " \u00d7 " +
          formatCoins(value / day.used) + ")";
      }
    },
    {
      id: "total-cost",
      label: "Running total of the cost of feasts used, across every " +
        "drawn feast",
      empty: "No feasts were used in this period.",
      money: true,
      total: true,
      value: function (day) { return day.cost; }
    },
    {
      id: "cost-by-food",
      label: "Running total of the cost of feasts used, one line per feast",
      empty: "No feasts were used in this period.",
      money: true,
      cumulative: true,
      value: function (day) { return day.cost; }
    }
  ];

  // The total line stands for every feast at once, so it takes a hue none of
  // them are drawn in; it is the fifth Okabe-Ito colour.
  var TOTAL_COLOR = "#D55E00";
  var SECONDS_PER_DAY = 24 * 60 * 60;

  // Each daily chart's hover teardown, so a redraw drops the page-level
  // listeners a pinned selection left behind on the canvas it replaces.
  var dailyHandles = {};

  function daysOf(feast) {
    return (feast && feast.days) || [];
  }

  // The day grid the window was bucketed into. The server cuts one grid for
  // the whole window and gives every feast all of it, quiet days included, so
  // the longest series any feast carries is that grid.
  function dayGrid() {
    var grid = [];
    feasts().forEach(function (feast) {
      var days = daysOf(feast);
      if (days.length > grid.length) {
        grid = days.map(function (day) { return day.t; });
      }
    });
    return grid;
  }

  // A day is named by the UTC date it opens, the way the server cut it, so a
  // reader west of Greenwich is not shown the day before.
  function formatDay(t) {
    return new Date(t * 1000).toLocaleDateString(
      undefined, { month: "short", day: "numeric", timeZone: "UTC" });
  }

  // Money on an axis is written in the largest coin it reaches, so the label
  // stays short enough to sit beside the plot. The baseline is bare: "0c"
  // reads as a price where the floor of the axis is not one.
  function formatCoinAxis(copper) {
    if (copper <= 0) { return "0"; }
    if (copper >= COPPER_PER_GOLD) {
      var gold = copper / COPPER_PER_GOLD;
      var shown = gold >= 10 ? Math.round(gold) : Math.round(gold * 10) / 10;
      return shown.toLocaleString() + "g";
    }
    if (copper >= COPPER_PER_SILVER) {
      return Math.round(copper / COPPER_PER_SILVER) + "s";
    }
    return Math.round(copper) + "c";
  }

  // How many decimals an axis needs to tell its own gridlines apart: a step
  // of 2 reads as whole feasts, a step of 0.2 does not.
  function decimalsFor(step) {
    if (step >= 1) { return 0; }
    return Math.min(3, Math.ceil(-Math.log10(step)));
  }

  function formatAxisValue(spec, value, decimals) {
    if (spec.money) { return formatCoinAxis(value); }
    return value.toFixed(decimals);
  }

  // What one plotted point reads as in the tooltip: a price in coins, and a
  // count to one decimal because a rolling average rarely lands on a whole
  // feast.
  function formatDailyValue(spec, value) {
    if (spec.money) { return formatCoins(value); }
    return (Math.round(value * 10) / 10).toLocaleString();
  }

  // The lines one chart draws, in legend order. Every chart has a value on
  // every day today, but a day that ever arrives without one is a gap rather
  // than a zero, so it is left out of the line instead of pulling it to the
  // floor.
  //
  // A cumulative chart draws each feast's running total instead, and its
  // hover names what the day alone added to it, the way the total's does.
  function perFeastSeries(spec) {
    var series = [];
    feasts().forEach(function (feast, index) {
      if (isHidden(feast)) { return; }
      var points = [];
      var running = 0;
      daysOf(feast).forEach(function (day) {
        var value = spec.value(day);
        if (typeof value !== "number") { return; }
        if (spec.cumulative) {
          running += value;
          points.push({
            t: day.t,
            v: running,
            text: formatCoins(running) + " (+" + formatCoins(value) + ")"
          });
          return;
        }
        points.push({
          t: day.t,
          v: value,
          text: spec.text
            ? spec.text(day, value)
            : formatDailyValue(spec, value)
        });
      });
      series.push({
        name: feast.name,
        color: COLORS[index % COLORS.length],
        points: points
      });
    });
    return series;
  }

  // The total is the running sum of the feasts still switched on, so
  // switching one off answers "what did the rest cost?" rather than leaving
  // the total unchanged. Every day of the grid gets a point, including the
  // quiet ones, because a running total that skips a day reads as though the
  // window were shorter than it is.
  function totalSeries(spec, grid) {
    if (!visibleFeasts().length) { return []; }
    var perDay = {};
    visibleFeasts().forEach(function (feast) {
      daysOf(feast).forEach(function (day) {
        var value = spec.value(day);
        if (typeof value !== "number") { return; }
        perDay[day.t] = (perDay[day.t] || 0) + value;
      });
    });
    var running = 0;
    var points = grid.map(function (t) {
      var spent = perDay[t] || 0;
      running += spent;
      return {
        t: t,
        v: running,
        text: formatCoins(running) + " (+" + formatCoins(spent) + ")"
      };
    });
    return [{ name: "All feasts", color: TOTAL_COLOR, points: points }];
  }

  function dailySeries(spec, grid) {
    return spec.total ? totalSeries(spec, grid) : perFeastSeries(spec);
  }

  // The axis covers the values actually drawn, from zero up and rounded to a
  // readable step the way the stock chart's is. Zero is the floor on every
  // one of these charts: a day nothing was used or spent on is the reading
  // they are read for, so it has to sit on the floor rather than part-way up.
  function dailyScale(series) {
    var high = 0;
    series.forEach(function (item) {
      item.points.forEach(function (point) {
        if (point.v > high) { high = point.v; }
      });
    });
    if (!(high > 0)) { return { high: 1, step: 1 }; }
    var span = high * 1.1;
    var step = niceStep(span, 5);
    return { high: step * Math.ceil(span / step), step: step };
  }

  // The window's first and last day. A window that covers a single day is
  // given half a day either side, so its one point sits in the middle of the
  // plot rather than on the axis.
  function dayDomain(grid) {
    var from = grid[0];
    var to = grid[grid.length - 1];
    if (!(to > from)) {
      return {
        from: from - SECONDS_PER_DAY / 2,
        to: from + SECONDS_PER_DAY / 2
      };
    }
    return { from: from, to: to };
  }

  // The daily charts share the stock chart's proportions, a little shorter
  // because there are several of them down the page now, and with a wider
  // left margin where the axis carries a coin unit as well as a figure.
  function dailyMetrics(spec) {
    var base = metrics();
    return {
      w: base.w,
      h: Math.round(base.h * 0.8),
      top: base.top,
      right: base.right,
      bottom: base.bottom,
      left: spec.money ? base.left + 18 : base.left,
      ticks: base.ticks
    };
  }

  // What a daily chart says when it has drawn nothing. An empty window and a
  // legend switched all the way off are different states, and only one of
  // them is worth waiting for more data over. The total has no legend of its
  // own, so it points at the ones it is summing.
  function dailyStatusText(spec, plottedCount) {
    if (plottedCount) { return ""; }
    if (feasts().length && !visibleFeasts().length) {
      return spec.total
        ? "Every feast is switched off in the legends above."
        : "Every feast is switched off. Click one in the legend to draw " +
          "it again.";
    }
    return spec.empty;
  }

  function renderDailyChart(spec) {
    var host = document.getElementById("chart-" + spec.id);
    var statusBox = document.getElementById("status-" + spec.id);
    var legendBox = document.getElementById("legend-" + spec.id);
    if (!host || !statusBox || !state.data) { return; }
    var detach = dailyHandles[spec.id];
    if (detach) { detach(); }
    dailyHandles[spec.id] = null;
    if (legendBox) { renderLegend(legendBox); }
    host.replaceChildren();

    // attachHover reads whichever metrics are current, so this chart's are
    // made current before it is drawn; the stock chart sets its own back at
    // the top of every render of its own.
    var m = dailyMetrics(spec);
    M = m;
    var innerW = m.w - m.left - m.right;
    var innerH = m.h - m.top - m.bottom;
    var grid = dayGrid();
    var series = grid.length ? dailySeries(spec, grid) : [];
    var scale = dailyScale(series);
    var decimals = decimalsFor(scale.step);
    var domain = dayDomain(grid.length ? grid : [state.data.since]);

    function x(t) {
      var span = domain.to - domain.from;
      var frac = span > 0 ? (t - domain.from) / span : 0;
      if (frac < 0) { frac = 0; }
      if (frac > 1) { frac = 1; }
      return m.left + frac * innerW;
    }
    function y(value) {
      var plotValue = value;
      if (plotValue < 0) { plotValue = 0; }
      if (plotValue > scale.high) { plotValue = scale.high; }
      return m.top + (1 - plotValue / scale.high) * innerH;
    }

    var canvas = svg("svg", {
      "class": "chart-svg",
      viewBox: "0 0 " + m.w + " " + m.h,
      role: "img",
      "aria-label": spec.label
    });

    var lines = Math.round(scale.high / scale.step);
    for (var line = 0; line <= lines; line += 1) {
      var value = scale.step * line;
      var gridY = y(value);
      canvas.appendChild(svg("line", {
        "class": line === 0 ? "axis" : "grid",
        x1: m.left, y1: gridY, x2: m.left + innerW, y2: gridY
      }));
      var yLabel = svg("text", {
        "class": "y-label", x: m.left - 6, y: gridY + 4
      });
      yLabel.textContent = formatAxisValue(spec, value, decimals);
      canvas.appendChild(yLabel);
    }
    canvas.appendChild(svg("line", {
      "class": "axis",
      x1: m.left, y1: m.top, x2: m.left, y2: m.top + innerH
    }));

    // Labels name real days rather than points along the window, and they are
    // spread evenly across the grid so a thirty-day window is not labelled
    // thirty times over.
    if (grid.length) {
      var ticks = Math.min(m.ticks, grid.length - 1);
      for (var tick = 0; tick <= ticks; tick += 1) {
        var at = ticks > 0
          ? Math.round(tick * (grid.length - 1) / ticks)
          : 0;
        var xLabel = svg("text", {
          "class": "x-label", x: x(grid[at]), y: m.top + innerH + 18
        });
        xLabel.textContent = formatDay(grid[at]);
        canvas.appendChild(xLabel);
      }
    }

    var plotted = [];
    series.forEach(function (item, index) {
      // A line is drawn only between days that sit next to each other in the
      // grid. Where a day is missing the line is left broken rather than
      // carried across it, because the segment would assert a value on days
      // that have none. A chart with a point on every day is one run and is
      // drawn unbroken.
      var runs = [];
      var run = [];
      var previousDay = null;
      item.points.forEach(function (point) {
        var px = x(point.t);
        var py = y(point.v);
        if (previousDay !== null && point.t - previousDay > SECONDS_PER_DAY) {
          runs.push(run);
          run = [];
        }
        run.push(px.toFixed(1) + "," + py.toFixed(1));
        previousDay = point.t;
        plotted.push({
          x: px,
          y: py,
          t: point.t,
          count: point.v,
          text: point.text,
          name: item.name,
          color: item.color,
          feast: index
        });
      });
      runs.push(run);
      runs.forEach(function (coords) {
        if (coords.length > 1) {
          canvas.appendChild(svg("polyline", {
            "class": "series-line",
            stroke: item.color,
            points: coords.join(" ")
          }));
        }
      });
      item.points.forEach(function (point) {
        canvas.appendChild(svg("circle", {
          "class": "series-dot",
          cx: x(point.t).toFixed(1),
          cy: y(point.v).toFixed(1),
          r: 3,
          fill: item.color
        }));
      });
    });

    dailyHandles[spec.id] = attachHover(canvas, plotted, host, formatDay);
    host.appendChild(canvas);
    statusBox.textContent = dailyStatusText(spec, plotted.length);
  }

  function renderDailyCharts() {
    DAILY_CHARTS.forEach(function (spec) { renderDailyChart(spec); });
  }

  function renderTabs() {
    tabs.replaceChildren();
    feasts().forEach(function (feast, index) {
      var button = el("button", null, feast.name);
      button.type = "button";
      var active = index === state.activeFeast;
      if (active) { button.classList.add("active"); }
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.addEventListener("click", function () {
        state.activeFeast = index;
        state.tablePage = 0;
        renderTabs();
        renderTable();
      });
      tabs.appendChild(button);
    });
  }

  function renderTable() {
    tableBox.replaceChildren();
    pager.replaceChildren();
    var feast = activeFeast();
    var removals = (feast && feast.removals) || [];
    if (!removals.length) {
      tableBox.appendChild(
        el("div", "empty", "No removals were recorded in this period."));
      return;
    }
    var pageCount = Math.ceil(removals.length / TABLE_PAGE_SIZE);
    if (state.tablePage > pageCount - 1) { state.tablePage = pageCount - 1; }
    var start = state.tablePage * TABLE_PAGE_SIZE;
    var pageRows = removals.slice(start, start + TABLE_PAGE_SIZE);

    var table = el("table", "removals");
    var head = el("tr");
    head.appendChild(el("th", null, "Time"));
    head.appendChild(el("th", null, "Removed"));
    head.appendChild(el("th", null, "Remaining"));
    table.appendChild(head);
    pageRows.forEach(function (row) {
      var tr = el("tr");
      tr.appendChild(el("td", null, formatMoment(row.t)));
      tr.appendChild(el("td", "num", String(row.amount)));
      tr.appendChild(el("td", "num", String(row.remaining)));
      table.appendChild(tr);
    });
    tableBox.appendChild(table);

    var prev = el("button", null, "Prev");
    prev.type = "button";
    prev.disabled = state.tablePage <= 0;
    prev.addEventListener("click", function () {
      if (state.tablePage > 0) { state.tablePage -= 1; renderTable(); }
    });
    var next = el("button", null, "Next");
    next.type = "button";
    next.disabled = state.tablePage >= pageCount - 1;
    next.addEventListener("click", function () {
      if (state.tablePage < pageCount - 1) {
        state.tablePage += 1;
        renderTable();
      }
    });
    pager.appendChild(prev);
    pager.appendChild(next);
    pager.appendChild(el("span", null,
      "Page " + (state.tablePage + 1) + " of " + pageCount +
      " (" + removals.length + " removals)"));
  }

  // --- Additions ----------------------------------------------------------

  // The whole coin price as gold, silver and copper, dropping the units a
  // price does not reach: 1g 0s 5c keeps its silver, 5c does not gain one.
  function formatCoins(copper) {
    var remaining = Math.max(0, Math.round(copper));
    var goldCoins = Math.floor(remaining / COPPER_PER_GOLD);
    remaining -= goldCoins * COPPER_PER_GOLD;
    var silverCoins = Math.floor(remaining / COPPER_PER_SILVER);
    var copperCoins = remaining % COPPER_PER_SILVER;
    var parts = [];
    if (goldCoins) { parts.push(goldCoins.toLocaleString() + "g"); }
    if (silverCoins || goldCoins) { parts.push(silverCoins + "s"); }
    parts.push(copperCoins + "c");
    return parts.join(" ");
  }

  function pencilIcon() {
    var icon = svg("svg", {
      viewBox: "0 0 24 24",
      width: "16",
      height: "16",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "2",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": "true"
    });
    icon.appendChild(svg("path", {
      d: "M12 20h9"
    }));
    icon.appendChild(svg("path", {
      d: "M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"
    }));
    return icon;
  }

  function additionFeast() {
    return feasts()[state.activeAddition] || null;
  }

  function additionsOf(feast) {
    return (feast && feast.additions) || [];
  }

  // Sanitized tracing for a cost save: the action and the outcome only. No
  // account name, price or restock ever reaches the console.
  function traceCost(action, outcome) {
    console.debug("feast cost:", action, outcome);
  }

  function renderAdditionTabs() {
    addTabs.replaceChildren();
    feasts().forEach(function (feast, index) {
      var button = el("button", null, feast.name);
      button.type = "button";
      var active = index === state.activeAddition;
      if (active) { button.classList.add("active"); }
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.addEventListener("click", function () {
        state.activeAddition = index;
        state.additionPage = 0;
        renderAdditionTabs();
        renderAdditions();
      });
      addTabs.appendChild(button);
    });
  }

  function renderAdditions() {
    addTableBox.replaceChildren();
    addPager.replaceChildren();
    var feast = additionFeast();
    var additions = additionsOf(feast);
    if (!additions.length) {
      addTableBox.appendChild(
        el("div", "empty", "No additions were recorded in this period."));
      return;
    }
    var pageCount = Math.ceil(additions.length / TABLE_PAGE_SIZE);
    if (state.additionPage > pageCount - 1) {
      state.additionPage = pageCount - 1;
    }
    var start = state.additionPage * TABLE_PAGE_SIZE;
    var pageRows = additions.slice(start, start + TABLE_PAGE_SIZE);

    var table = el("table", "additions");
    var head = el("tr");
    head.appendChild(el("th", null, "Time"));
    head.appendChild(el("th", null, "User"));
    head.appendChild(el("th", null, "Added"));
    head.appendChild(el("th", null, "Remaining"));
    head.appendChild(el("th", null, "Cost"));
    head.appendChild(el("th", "actions", "Edit"));
    table.appendChild(head);
    pageRows.forEach(function (row) {
      var priced = typeof row.cost === "number";
      var tr = el("tr", priced ? null : "unpriced");
      tr.appendChild(el("td", null, formatMoment(row.t)));
      var users = row.users || [];
      tr.appendChild(el("td", "user",
        users.length ? users.join(", ") : "\u2014"));
      tr.appendChild(el("td", "num", String(row.amount)));
      tr.appendChild(el("td", "num", String(row.remaining)));
      tr.appendChild(el("td", "num",
        priced ? formatCoins(row.cost) : "\u2014"));
      var actions = el("td", "actions");
      var edit = el("button", "icon-button");
      edit.type = "button";
      edit.setAttribute("aria-haspopup", "dialog");
      edit.setAttribute("aria-label", "Edit cost");
      edit.title = "Edit cost";
      edit.appendChild(pencilIcon());
      edit.addEventListener("click", function () {
        openCostDialog(feast, row);
      });
      actions.appendChild(edit);
      tr.appendChild(actions);
      table.appendChild(tr);
    });
    var scroll = el("div", "table-scroll");
    scroll.appendChild(table);
    addTableBox.appendChild(scroll);

    var prev = el("button", null, "Prev");
    prev.type = "button";
    prev.disabled = state.additionPage <= 0;
    prev.addEventListener("click", function () {
      if (state.additionPage > 0) {
        state.additionPage -= 1;
        renderAdditions();
      }
    });
    var next = el("button", null, "Next");
    next.type = "button";
    next.disabled = state.additionPage >= pageCount - 1;
    next.addEventListener("click", function () {
      if (state.additionPage < pageCount - 1) {
        state.additionPage += 1;
        renderAdditions();
      }
    });
    addPager.appendChild(prev);
    addPager.appendChild(next);
    addPager.appendChild(el("span", null,
      "Page " + (state.additionPage + 1) + " of " + pageCount +
      " (" + additions.length + " additions)"));
  }

  // --- The cost dialog ------------------------------------------------------

  // A recorded price fills the boxes it reaches and leaves the rest blank, so
  // a cost of 1g opens as 1g rather than as 1g 0s 0c; a restock nobody has
  // priced opens empty.
  function fillCostFields(cost) {
    if (typeof cost !== "number") {
      costFields.gold.value = "";
      costFields.silver.value = "";
      costFields.copper.value = "";
      return;
    }
    var remaining = Math.max(0, Math.round(cost));
    var goldCoins = Math.floor(remaining / COPPER_PER_GOLD);
    remaining -= goldCoins * COPPER_PER_GOLD;
    var silverCoins = Math.floor(remaining / COPPER_PER_SILVER);
    var copperCoins = remaining % COPPER_PER_SILVER;
    costFields.gold.value = goldCoins ? String(goldCoins) : "";
    costFields.silver.value = silverCoins ? String(silverCoins) : "";
    costFields.copper.value = copperCoins ? String(copperCoins) : "";
  }

  function openCostDialog(feast, addition) {
    state.editing = { feastId: feast ? feast.id : null, addition: addition };
    costError.textContent = "";
    costSave.disabled = false;
    costSubject.textContent =
      (feast ? feast.name : "") + " \u00b7 " + formatMoment(addition.t) +
      " \u00b7 " + addition.amount + " added";
    fillCostFields(addition.cost);
    traceCost("open", typeof addition.cost === "number" ? "priced" : "empty");
    costDialog.showModal();
    costFields.gold.focus();
  }

  function closeCostDialog() {
    state.editing = null;
    if (costDialog.open) { costDialog.close(); }
  }

  // A blank box is zero, which is what makes 5c a price a reader can type as
  // one box rather than three. Anything that is not a whole count of coins is
  // refused instead of being rounded into a price nobody entered.
  function readCoinField(input) {
    var raw = input.value.trim();
    if (raw === "") { return 0; }
    if (!/^[0-9]+$/.test(raw)) { return null; }
    var value = Number(raw);
    return Number.isSafeInteger(value) ? value : null;
  }

  function readCost() {
    var goldCoins = readCoinField(costFields.gold);
    var silverCoins = readCoinField(costFields.silver);
    var copperCoins = readCoinField(costFields.copper);
    if (goldCoins === null || silverCoins === null || copperCoins === null) {
      return { error: "Enter whole numbers of coins, or leave a box blank." };
    }
    var total = goldCoins * COPPER_PER_GOLD +
      silverCoins * COPPER_PER_SILVER + copperCoins;
    if (total > MAX_COST_COPPER) {
      return { error: "That is more gold than an account can hold." };
    }
    return { cost: total };
  }

  function saveCost() {
    var editing = state.editing;
    if (!editing) { return; }
    var read = readCost();
    if (read.error) {
      costError.textContent = read.error;
      traceCost("reject", "fields");
      return;
    }
    var addition = editing.addition;
    costError.textContent = "";
    costSave.disabled = true;
    fetch("/api/food/cost", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ log_id: addition.log_id, cost: read.cost })
    })
      .then(function (response) {
        if (response.status === 401) {
          location.href = "/login";
          throw new Error("unauthorized");
        }
        if (!response.ok) { throw new Error("failed"); }
        return response.json();
      })
      .then(function (payload) {
        addition.cost = payload.cost;
        traceCost("save", "stored");
        closeCostDialog();
        renderAdditions();
        // The daily cost charts are drawn from days the server bucketed and
        // averaged, so a price recorded here only reaches them through
        // another read of the window. Patching them in the page would mean
        // working out a seven-day average over a week the page was never
        // sent. The officer who saved it is still reading the table it came
        // from, so their tab and page are kept across the fetch.
        refresh({ keepPlace: true });
      })
      .catch(function (error) {
        // Only the error's type and message are logged; no price, account or
        // response body is ever passed through.
        console.error(
          "feast cost save failed:",
          error && error.name, error && error.message);
        costSave.disabled = false;
        costError.textContent = "Could not save that cost.";
      });
  }

  costForm.addEventListener("submit", function (event) {
    event.preventDefault();
    saveCost();
  });
  var costCancel = document.getElementById("cost-cancel");
  costCancel.addEventListener("click", function () {
    traceCost("cancel", "button");
    closeCostDialog();
  });
  // Escape closes a dialog itself; this keeps the page's own record of what
  // is open in step with it.
  costDialog.addEventListener("close", function () { state.editing = null; });
  costDialog.addEventListener("click", function (event) {
    // A modal dialog's backdrop is part of the dialog element, so a click
    // that lands on the element itself landed outside the form.
    if (event.target === costDialog) {
      traceCost("cancel", "backdrop");
      closeCostDialog();
    }
  });

  function render() {
    renderLegend();
    renderChart();
    renderDailyCharts();
    renderTabs();
    renderTable();
    renderAdditionTabs();
    renderAdditions();
  }

"""
    + range_picker_js("feast")
    + """
  // options.keepPlace holds the reader's tab and page across the fetch, for
  // the re-read a saved cost asks for: the rows are the ones they were just
  // working down, not a window they have only now opened.
  function refresh(options) {
    var keepPlace = !!(options && options.keepPlace);
    chartStatus.textContent = "Loading\\u2026";
    fetch("/api/food" + rangeQuery())
      .then(function (response) {
        if (response.status === 401) {
          location.href = "/login";
          throw new Error("unauthorized");
        }
        if (!response.ok) { throw new Error("failed"); }
        return response.json();
      })
      .then(function (payload) {
        state.data = payload;
        adoptRange(payload.range, payload.since, payload.now);
        if (state.activeFeast >= (payload.feasts || []).length) {
          state.activeFeast = 0;
        }
        if (state.activeAddition >= (payload.feasts || []).length) {
          state.activeAddition = 0;
        }
        if (!keepPlace) {
          state.tablePage = 0;
          state.additionPage = 0;
        }
        // The rows behind the dialog have just been replaced, so whatever it
        // was opened over is gone.
        closeCostDialog();
        render();
      })
      .catch(function (error) {
        // render() runs inside this chain, so a drawing fault lands here and
        // otherwise reads as a failed request with nothing in the console to
        // trace. Only the error's type and message are logged; no request,
        // response or payload is ever passed through.
        console.error(
          "feast usage load failed:",
          error && error.name, error && error.message);
        if (chartStatus.textContent === "Loading\\u2026") {
          chartStatus.textContent = "Could not load feast usage.";
        }
      });
  }

"""
    + RANGE_PICKER_LISTENERS_JS
    + """
  // Redraw when the breakpoint flips so the chart adopts the layout for the
  // new width.
  mobileQuery.addEventListener("change", function () {
    if (state.data) { render(); }
  });

  fetch("/api/me")
    .then(function (response) {
      if (response.status === 401) {
        location.href = "/login";
        throw new Error("unauthorized");
      }
      return response.json();
    })
    .then(function (payload) {
      document.getElementById("whoami").textContent = payload.name || "";
    })
    .catch(function () {});

  syncRangeButtons();
  refresh();
})();
</script>
</body>
</html>
"""
)
