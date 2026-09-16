"""The guild bank gold history dashboard page.

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

GOLD_PAGE = (
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Guild Bank</title>
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
.card h2 .now {
  color: var(--muted);
  font-weight: 400;
  font-size: 0.85rem;
  margin-left: 0.4rem;
}
/* The legend names what each colour of dot means. There are only two, and
   they are fixed, so the names are always shown. */
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
  color: var(--text);
}
.legend .swatch {
  width: 0.9rem;
  height: 0.9rem;
  border-radius: 50%;
  flex-shrink: 0;
}
/* The chart is a fixed-viewBox SVG that scales to its container width, so
   every plotted coordinate is computed once against the viewBox and the
   browser handles resizing without a re-render. */
.chart-svg { width: 100%; height: auto; display: block; }
.chart-svg .axis { stroke: var(--border); stroke-width: 1; }
.chart-svg .grid { stroke: var(--border); stroke-width: 1; opacity: 0.35; }
.chart-svg text { fill: var(--muted); font-size: 11px; font-family: inherit; }
.chart-svg .y-label { text-anchor: end; }
.chart-svg .x-label { text-anchor: middle; }
.chart-svg .x-label.first { text-anchor: start; }
.chart-svg .x-label.last { text-anchor: end; }
.chart-svg .balance-line { fill: none; stroke-width: 2; }
.chart-svg .event-dot { stroke: var(--panel); stroke-width: 1; }
.chart-svg .overlay { fill: transparent; }
/* A thin, translucent gray line the hover snaps to the nearest movement. */
.chart-svg .crosshair {
  stroke: rgba(128, 128, 128, 0.45);
  stroke-width: 1;
  pointer-events: none;
}
.chart-svg .hover-ring { fill: none; stroke-width: 2; pointer-events: none; }
/* #chart is the positioning context for the hover tooltip, which is an HTML
   box overlaid on the SVG so its text wraps and inherits page styling. */
#chart { position: relative; }
.chart-tooltip {
  position: absolute;
  z-index: 2;
  min-width: 9rem;
  max-width: 16rem;
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
  border-radius: 50%;
  flex-shrink: 0;
}
.chart-tooltip .tip-row .name { overflow-wrap: anywhere; }
.chart-tooltip .tip-row .val {
  margin-left: auto;
  padding-left: 0.75rem;
  font-variant-numeric: tabular-nums;
}
.chart-tooltip .tip-row.em { font-weight: 600; }
.chart-tooltip .tip-note { color: var(--muted); margin-top: 0.25rem; }
#chart-status { color: var(--muted); font-size: 0.85rem; padding-top: 0.5rem; }
.totals {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem 1.25rem;
  margin-bottom: 0.75rem;
  color: var(--muted);
  font-size: 0.85rem;
}
.totals .num {
  color: var(--text);
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
table.changes { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
table.changes th, table.changes td {
  text-align: left;
  padding: 0.4rem 0.6rem;
  border-bottom: 1px solid var(--border);
}
table.changes th { color: var(--muted); font-weight: 600; }
/* A Guild Wars 2 account name has no spaces to break at, so a long one would
   otherwise widen the table past the card it sits in and carry the right-hand
   columns off the page. Breaking inside the word is what bounds the column,
   and `anywhere` rather than `break-word` because only `anywhere` also
   shrinks the column's minimum width - which is the width the table lays
   itself out from, so it is what keeps the table itself inside the card. */
table.changes td.name { overflow-wrap: anywhere; }
table.changes th.num, table.changes td.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
table.changes td.amount { white-space: nowrap; }
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
  /* The balance after each movement is the column that gives way on a phone:
     the amount and its sign are what a reader is scanning for, and the line
     above already shows where the balance went. It is still in the chart
     tooltip. */
  table.changes .balance { display: none; }
}
</style>
</head>
<body>
<header>
  <h1 id="brand">Guild Bank</h1>
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
      <h2>Gold in the guild bank<span id="now-balance" class="now"></span></h2>
      <button type="button" id="chart-mode" class="chart-mode"
        aria-label="Switch to staircase graph" title="Regular line graph">╱</button>
    </div>
    <div id="chart"></div>
    <div id="legend" class="legend" role="list" aria-label="Movement kinds">
    </div>
    <div id="chart-status" role="status" aria-live="polite"></div>
  </section>
  <section class="card">
    <h2>Deposits and withdrawals</h2>
    <div id="totals" class="totals"></div>
    <div id="table"></div>
    <div id="pager" class="pager"></div>
  </section>
</main>
<script>
"use strict";
(function () {
  // Okabe-Ito colourblind-safe palette: one hue per direction the gold went.
  var OPERATIONS = {
    deposit: { color: "#009E73", label: "Deposited" },
    withdraw: { color: "#D55E00", label: "Withdrew" }
  };
  var OPERATION_ORDER = ["deposit", "withdraw"];
  var NET_CHANGES = [
    { color: OPERATIONS.deposit.color, label: "Increase" },
    { color: OPERATIONS.withdraw.color, label: "Decrease" },
    { color: "#D9D9D9", label: "Net Zero" }
  ];
  var SVG_NS = "http://www.w3.org/2000/svg";
  var TABLE_PAGE_SIZE = 10;
  // Copper is what the guild log, the API and the ledger all deal in. The
  // chart scale uses gold, while displayed amounts retain exact coin values.
  var COPPER_PER_GOLD = 10000;
  var COPPER_PER_SILVER = 100;
  // Smallest number of gold the y axis ever spans, so a quiet week does not
  // turn one small withdrawal into a cliff.
  var MIN_SPAN_GOLD = 10;

  var mobileQuery = window.matchMedia("(max-width: 640px)");
  function isMobile() { return mobileQuery.matches; }

  // The chart uses a wide viewBox on desktop and a taller one on mobile,
  // where it scales to the narrow screen width; the extra height makes the
  // graph read large on a phone. Coordinates are computed against whichever
  // set is active, so M is refreshed at the start of every chart render. The
  // left margin is wider than the roster page's because a gold figure is a
  // longer label than a member count.
  function metrics() {
    if (isMobile()) {
      return {
        w: 480, h: 620, top: 16, right: 14, bottom: 36, left: 56, ticks: 4
      };
    }
    return {
      w: 960, h: 380, top: 16, right: 16, bottom: 32, left: 64, ticks: 6
    };
  }
  // How wide one character of an axis label runs at the 11px the chart
  // draws them in, rounded up from the widest digit: the label is measured
  // by its length because the chart is laid out before it is on the page,
  // and an unrendered text node reports no width at all.
  var Y_LABEL_CHAR_WIDTH = 6.6;
  var M = metrics();
  function plotW() { return M.w - M.left - M.right; }
  function plotH() { return M.h - M.top - M.bottom; }

  var state = {
    // No range until the server answers: the first load asks for the window
    // this member last picked rather than naming one over the top of it.
    range: null, data: null, tablePage: 0, scale: null, staircase: false
  };

  // A pinned touch selection listens on the whole page, so the chart it
  // belongs to is torn down before another one is drawn.
  var detachHover = null;

  var legend = document.getElementById("legend");
  var chart = document.getElementById("chart");
  var chartStatus = document.getElementById("chart-status");
  var chartMode = document.getElementById("chart-mode");
  var nowBalance = document.getElementById("now-balance");
  var totals = document.getElementById("totals");
  var tableBox = document.getElementById("table");
  var pager = document.getElementById("pager");

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

  function points() { return (state.data && state.data.points) || []; }
  function movements() { return (state.data && state.data.movements) || []; }

  function traceMode(mode, count) {
    console.debug("gold chart mode:", mode, count);
  }
  function operationOf(operation) {
    return OPERATIONS[operation] || OPERATIONS.withdraw;
  }
  function netChangeColor(before, after) {
    if (after === before) { return NET_CHANGES[2].color; }
    return operationOf(after > before ? "deposit" : "withdraw").color;
  }

  function gold(copper) { return copper / COPPER_PER_GOLD; }

  // Match the profit dashboard's spaced denomination format. Copper is always
  // present, and silver is retained whenever gold is present: 1g 0s 0c.
  function formatCoins(copper) {
    var remaining = Math.max(0, Math.round(copper));
    var goldCoins = Math.floor(remaining / COPPER_PER_GOLD);
    remaining %= COPPER_PER_GOLD;
    var silverCoins = Math.floor(remaining / COPPER_PER_SILVER);
    var copperCoins = remaining % COPPER_PER_SILVER;
    var parts = [];
    if (goldCoins) { parts.push(goldCoins.toLocaleString() + "g"); }
    if (silverCoins || goldCoins) { parts.push(silverCoins + "s"); }
    parts.push(copperCoins + "c");
    return parts.join(" ");
  }
  function formatAxisCoins(copper) {
    var remaining = Math.max(0, Math.round(copper));
    var goldCoins = Math.floor(remaining / COPPER_PER_GOLD);
    remaining %= COPPER_PER_GOLD;
    var silverCoins = Math.floor(remaining / COPPER_PER_SILVER);
    var copperCoins = remaining % COPPER_PER_SILVER;
    var text = goldCoins ? goldCoins.toLocaleString() + "g" : "";
    if (silverCoins || (goldCoins && copperCoins)) { text += silverCoins + "s"; }
    if (copperCoins || !text) { text += copperCoins + "c"; }
    return text;
  }
  function formatSigned(copper, operation) {
    var sign = operation === "withdraw" ? "-" : "+";
    return sign + formatCoins(copper);
  }

  // A gridline step of 1, 2 or 5 times a power of ten, which is what makes
  // the axis read as gold figures rather than as arbitrary divisions.
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

  // The axis covers the balances actually reached in the window, padded out
  // to MIN_SPAN_GOLD and rounded to a readable step, so the line uses the
  // full height instead of hugging the top of an axis that starts at zero.
  // Everything here is in gold rather than copper: the reader's units are
  // what the gridlines have to land on.
  function computeScale() {
    var values = points().map(function (point) { return gold(point.coins); });
    if (!values.length) { return null; }
    var low = Math.min.apply(null, values);
    var high = Math.max.apply(null, values);
    var pad = Math.max(0.5, (high - low) * 0.15);
    low -= pad;
    high += pad;
    if (high - low < MIN_SPAN_GOLD) {
      var grow = (MIN_SPAN_GOLD - (high - low)) / 2;
      low -= grow;
      high += grow;
    }
    // The bank cannot hold less than nothing, so an axis is never drawn
    // below zero however much padding the span asked for.
    if (low < 0) { low = 0; }
    var step = niceStep(high - low, 4);
    low = Math.floor(low / step) * step;
    high = low + step * Math.ceil((high - low) / step);
    return { low: low, high: high, step: step };
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
  function scaleY(copper) {
    var scale = state.scale;
    var span = scale.high - scale.low;
    var value = gold(copper);
    if (value < scale.low) { value = scale.low; }
    if (value > scale.high) { value = scale.high; }
    return M.top + (1 - (value - scale.low) / span) * plotH();
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
    if (!state.scale) {
      chartStatus.textContent = state.data
        ? "No guild bank balance has been recorded yet, so the gold line " +
          "cannot be drawn. The movements below are still listed."
        : "";
      return;
    }
    var canvas = svg("svg", {
      "class": "chart-svg",
      viewBox: "0 0 " + M.w + " " + M.h,
      role: "img",
      "aria-label":
        "Gold in the guild bank over time, with one dot per active minute"
    });

    // Horizontal gridlines and y labels at every step of the computed scale.
    // The loop counts steps rather than accumulating them, because a
    // fractional step accumulated in floating point drifts off the value the
    // label claims.
    var lines = Math.round(
      (state.scale.high - state.scale.low) / state.scale.step);
    // A bank in six figures of gold carries a label wider than the margin
    // left for it, and one drawn past the edge of the viewBox is cut rather
    // than merely cramped. The margin is widened to whatever the labels
    // need, never narrowed, so an ordinary balance is laid out as before.
    // Everything positioned reads M.left at call time, so this has to land
    // before the first coordinate is computed.
    var widest = 0;
    for (var line = 0; line <= lines; line += 1) {
      widest = Math.max(widest, formatAxisCoins(
        (state.scale.low + state.scale.step * line) * COPPER_PER_GOLD
      ).length);
    }
    M.left = Math.max(M.left, Math.ceil(widest * Y_LABEL_CHAR_WIDTH) + 10);
    for (var i = 0; i <= lines; i += 1) {
      var value = state.scale.low + state.scale.step * i;
      var y = scaleY(value * COPPER_PER_GOLD);
      canvas.appendChild(svg("line", {
        "class": i === 0 ? "axis" : "grid",
        x1: M.left, y1: y, x2: M.left + plotW(), y2: y
      }));
      var yLabel = svg("text", {
        "class": "y-label", x: M.left - 6, y: y + 4
      });
      yLabel.textContent = formatAxisCoins(value * COPPER_PER_GOLD);
      canvas.appendChild(yLabel);
    }

    // Left axis, plus x labels spaced evenly across the whole window so the
    // range spans the full width even when little happened.
    canvas.appendChild(svg("line", {
      "class": "axis",
      x1: M.left, y1: M.top, x2: M.left, y2: M.top + plotH()
    }));
    for (var tick = 0; tick <= M.ticks; tick += 1) {
      var t = state.data.since +
        (state.data.now - state.data.since) * (tick / M.ticks);
      var x = scaleX(t);
      var xLabel = svg("text", {
        "class": "x-label", x: x, y: M.top + plotH() + 18
      });
      // The outermost labels sit on the plot's own edges, so centring them
      // would hang half of each one off the chart and the browser would clip
      // it. They are tucked inwards instead - by class, because the
      // stylesheet's text-anchor would win over a presentation attribute.
      if (tick === 0) { xLabel.classList.add("first"); }
      if (tick === M.ticks) { xLabel.classList.add("last"); }
      xLabel.textContent = formatTick(t);
      canvas.appendChild(xLabel);
    }

    // Movements in the same clock minute collapse into one cumulative dot at
    // the balance after that minute's final transaction. The dot retains the
    // complete group so its tooltip can still itemize everything that moved.
    var plotted = [];
    var previousCoins = points()[0].coins;
    movementMinutes().forEach(function (minute) {
      var px = scaleX(minute.t);
      var py = scaleY(minute.after);
      var color = netChangeColor(previousCoins, minute.after);
      plotted.push({
        x: px, y: py, t: minute.t, after: minute.after,
        movements: minute.movements, color: color
      });
      previousCoins = minute.after;
    });

    // Draw from the window boundary through only the cumulative minute dots
    // and back to the other boundary. A separate segment gives each combined
    // interval the deposit/withdrawal colour of its net balance change.
    var linePoints = [{ t: points()[0].t, coins: points()[0].coins }];
    plotted.forEach(function (point) {
      linePoints.push({ t: point.t, coins: point.after });
    });
    var finalPoint = points()[points().length - 1];
    linePoints.push({ t: finalPoint.t, coins: finalPoint.coins });
    linePoints.slice(1).forEach(function (point, index) {
      var previous = linePoints[index];
      var coords = [
        scaleX(previous.t).toFixed(1) + "," +
          scaleY(previous.coins).toFixed(1)
      ];
      if (state.staircase) {
        coords.push(scaleX(point.t).toFixed(1) + "," +
          scaleY(previous.coins).toFixed(1));
      }
      coords.push(scaleX(point.t).toFixed(1) + "," +
        scaleY(point.coins).toFixed(1));
      canvas.appendChild(svg("polyline", {
        "class": "balance-line",
        stroke: netChangeColor(previous.coins, point.coins),
        points: coords.join(" ")
      }));
    });

    // SVG paints later elements on top. Add the combined event dots only
    // after every segment so a following segment cannot cover a dot's fill.
    plotted.forEach(function (point) {
      canvas.appendChild(svg("circle", {
        "class": "event-dot",
        cx: point.x.toFixed(1),
        cy: point.y.toFixed(1),
        r: 4,
        fill: point.color
      }));
    });

    detachHover = attachHover(canvas, plotted);
    chart.appendChild(canvas);
    chartStatus.textContent = plotted.length
      ? ""
      : "No gold moved in or out of the bank in this period.";
  }

  chartMode.addEventListener("click", function () {
    state.staircase = !state.staircase;
    chartMode.textContent = state.staircase ? "⎿" : "╱";
    chartMode.title = state.staircase ? "Staircase graph" : "Regular line graph";
    chartMode.setAttribute("aria-label", state.staircase
      ? "Switch to regular line graph" : "Switch to staircase graph");
    traceMode(state.staircase ? "staircase" : "regular", points().length);
    renderChart();
  });

  function movementMinutes() {
    var byMinute = {};
    var minutes = [];
    // The API serializes newest-first. Reversing, rather than sorting only by
    // timestamp, restores its exact chronological order including tied times.
    movements().slice().reverse().forEach(function (movement) {
      if (movement.after === null) { return; }
      var minuteAt = Math.floor(movement.t / 60) * 60;
      var key = String(minuteAt);
      var minute = byMinute[key];
      if (!minute) {
        minute = { t: movement.t, after: movement.after, movements: [] };
        byMinute[key] = minute;
        minutes.push(minute);
      }
      minute.t = movement.t;
      minute.after = movement.after;
      minute.movements.push(movement);
    });
    return minutes;
  }

  // Each plotted minute is already a single column. Keeping the column shape
  // shared with the other charts makes pointer and touch selection identical.
  function groupColumns(plotted) {
    return plotted.map(function (point) {
      return { t: point.t, x: point.x, points: [point] };
    });
  }

  // Tells a hovering pointer from a finger or a pen. A touch has no hover
  // state: the browser sends one pointermove at the tap point and then a
  // pointerleave as the finger lifts. Touch selects by tapping instead and
  // never reaches the move or leave handlers.
  function isHoverPointer(event) {
    return !event.pointerType || event.pointerType === "mouse";
  }

  // How far a finger may travel from where it landed and still count as a
  // tap rather than the start of a scroll, in CSS pixels.
  var TAP_SLOP = 12;

  // Pointer and event types are narrowed to the names the spec defines
  // before they are traced, so an exotic value cannot ride into the console.
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

  // Sanitized tracing for the tap selection lifecycle, so a console trace
  // can explain why a selection opened, moved or went away. Every call
  // passes a fixed action name, one of the narrowed reason names above, and
  // a count of drawn elements. Account names, timestamps and amounts are
  // never passed, so no part of the payload or of the reader's gesture
  // reaches the console. debug keeps it out of the default console view.
  function traceSelection(action, reason, count) {
    console.debug("gold chart selection:", action, reason, count);
  }

  function attachHover(canvas, plotted) {
    var columns = groupColumns(plotted);
    // The viewBox differs between the mobile and desktop layouts, so the
    // hover is pinned to the metrics this canvas was drawn with rather than
    // to whichever set is current when a pointer event arrives.
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
    chart.appendChild(tooltip);

    // Pick from the actual dots in two dimensions, then return its time
    // column. Close timestamps can occupy nearly the same x coordinate, so
    // x-only navigation made their different y positions impossible to use.
    function nearestColumn(vbX, vbY) {
      var best = null;
      var bestDist = Infinity;
      columns.forEach(function (column) {
        column.points.forEach(function (point) {
          var dx = point.x - vbX;
          var dy = point.y - vbY;
          var dist = dx * dx + dy * dy;
          if (dist < bestDist) { bestDist = dist; best = column; }
        });
      });
      return best;
    }

    function showTooltip(column, emphasized) {
      tooltip.replaceChildren();
      tooltip.appendChild(el("div", "tip-time", formatMoment(column.t)));
      emphasized.movements.forEach(function (movement) {
        var row = el("div", "tip-row");
        var swatch = el("span", "swatch");
        swatch.style.background = operationOf(movement.operation).color;
        row.appendChild(swatch);
        row.appendChild(el("span", "name", movement.name));
        row.appendChild(el("span", "val",
          formatSigned(movement.coins, movement.operation)));
        tooltip.appendChild(row);
      });
      tooltip.appendChild(el("div", "tip-note",
        "Bank held " + formatCoins(emphasized.after) + "."));
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
          r: 7,
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
    // was holding it open. A mouse hover follows the pointer continuously,
    // so tracing every move that ends one would bury the trace it exists to
    // give.
    function release(reason) {
      var wasPinned = pinned;
      hideHover();
      if (wasPinned) { traceSelection("release", reason, columns.length); }
    }

    // Anything other than another tap on the plot clears a pinned selection:
    // a tap elsewhere on the page, a wheel, a key, or the window losing
    // focus.
    function dismiss(event) {
      // A tap that moves the selection to another column reaches the overlay
      // after this capture listener has already run, so it is left to the
      // overlay's own handler. Only a tap earns that exemption: a wheel over
      // the plot, or a mouse press on it, is aimed at the overlay too, but
      // the handler there acts on neither, so waving those through would
      // strand the selection on screen with nothing left to clear it.
      if (isRetargetingTap(event)) {
        traceSelection("keep", "retarget-on-plot", columns.length);
        return;
      }
      release("page-" + eventKind(event));
    }

    // True only for the events the overlay's pointerdown handler will act
    // on, which is what makes leaving them to it safe.
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
      if (!columns.length) { return { column: null, reason: "no-movements" }; }
      var at = pointFromEvent(event);
      if (!at) { return { column: null, reason: "unsized-canvas" }; }
      var column = nearestColumn(at.x, at.y);
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
      // Tracing the move apart from the open is what shows a trace reader
      // that a second tap replaced the first selection instead of adding to
      // it.
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

  function renderLegend() {
    legend.replaceChildren();
    NET_CHANGES.forEach(function (change) {
      var item = el("span", "item");
      item.setAttribute("role", "listitem");
      var swatch = el("span", "swatch");
      swatch.style.background = change.color;
      item.appendChild(swatch);
      item.appendChild(el("span", "legend-name", change.label));
      legend.appendChild(item);
    });
  }

  function renderTotals() {
    totals.replaceChildren();
    if (!state.data) { return; }
    var net = state.data.net || 0;
    // A net of nothing is written plainly: a sign in front of a zero claims a
    // direction the window did not go.
    var netText = net === 0
      ? formatCoins(0)
      : formatSigned(Math.abs(net), net < 0 ? "withdraw" : "deposit");
    [
      ["deposited", formatCoins(state.data.deposited || 0)],
      ["withdrawn", formatCoins(state.data.withdrawn || 0)],
      ["net", netText]
    ].forEach(function (pair) {
      var item = el("span", null);
      item.appendChild(el("span", "num", pair[1]));
      item.appendChild(document.createTextNode(" " + pair[0]));
      totals.appendChild(item);
    });
    // A preset window runs to the moment of the request, so its closing
    // balance is the balance now; a picked one can close months ago, where
    // the only thing that balance is true of is the window's own end.
    nowBalance.textContent = state.data.coins === null ||
      state.data.coins === undefined
      ? ""
      : "\\u2014 " + formatCoins(state.data.coins) +
        (state.range === "custom" ? " at the end" : " now");
  }

  function renderTable() {
    tableBox.replaceChildren();
    pager.replaceChildren();
    var rows = movements();
    if (!rows.length) {
      tableBox.appendChild(el("div", "empty",
        "No gold moved in or out of the bank in this period."));
      return;
    }
    var pageCount = Math.ceil(rows.length / TABLE_PAGE_SIZE);
    if (state.tablePage > pageCount - 1) { state.tablePage = pageCount - 1; }
    var start = state.tablePage * TABLE_PAGE_SIZE;
    var pageRows = rows.slice(start, start + TABLE_PAGE_SIZE);

    var table = el("table", "changes");
    var head = el("tr");
    head.appendChild(el("th", null, "Time"));
    head.appendChild(el("th", null, "Account"));
    head.appendChild(el("th", "num amount", "Gold"));
    head.appendChild(el("th", "num balance", "Balance"));
    table.appendChild(head);
    pageRows.forEach(function (movement) {
      var row = el("tr");
      row.appendChild(el("td", null, formatMoment(movement.t)));
      row.appendChild(el("td", "name", movement.name));
      var amountCell = el("td", "num amount");
      amountCell.appendChild(el("span", null,
        formatSigned(movement.coins, movement.operation)));
      row.appendChild(amountCell);
      row.appendChild(el("td", "num balance",
        movement.after === null ? "" : formatCoins(movement.after)));
      table.appendChild(row);
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
      " (" + rows.length + " movements)"));
  }

  function render() {
    renderLegend();
    renderTotals();
    renderChart();
    renderTable();
  }

"""
    + range_picker_js("gold")
    + """
  function refresh() {
    chartStatus.textContent = "Loading\\u2026";
    fetch("/api/gold" + rangeQuery())
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
        state.tablePage = 0;
        render();
      })
      .catch(function (error) {
        // render() runs inside this chain, so a drawing fault lands here and
        // otherwise reads as a failed request with nothing in the console to
        // trace. Only the error's type and message are logged; no request,
        // response or payload is ever passed through.
        console.error(
          "gold history load failed:",
          error && error.name, error && error.message);
        if (chartStatus.textContent === "Loading\\u2026") {
          chartStatus.textContent = "Could not load the gold history.";
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
