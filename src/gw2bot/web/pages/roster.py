"""The guild roster history dashboard page.

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

ROSTER_PAGE = (
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Guild Roster</title>
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
/* The legend names what each colour of dot means. Unlike the feast page there
   are only three, and they are fixed, so the names are always shown. */
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
.chart-svg .count-line { fill: none; stroke-width: 2; }
.chart-svg .event-dot { stroke: var(--panel); stroke-width: 1; }
.chart-svg .overlay { fill: transparent; }
/* A thin, translucent gray line the hover snaps to the nearest event. */
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
/* A Guild Wars 2 account name has no spaces to break at, so a long one used
   to widen the table past the card it sits in and carry the right-hand
   columns off the page. Breaking inside the word is what bounds the column,
   and `anywhere` rather than `break-word` because only `anywhere` also
   shrinks the column's minimum width - which is the width the table lays
   itself out from, so it is what keeps the table itself inside the card. */
table.changes td.name, table.changes td.by { overflow-wrap: anywhere; }
table.changes td.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
table.changes td.change { white-space: nowrap; }
table.changes .dot {
  display: inline-block;
  width: 0.6rem;
  height: 0.6rem;
  border-radius: 50%;
  margin-right: 0.4rem;
}
.empty { color: var(--muted); padding: 0.6rem; }
.note {
  color: var(--muted);
  font-size: 0.85rem;
  margin: 0 0 0.75rem;
}
/* A Discord display name has no spaces to break at either, so it is bounded
   the same way an account name is. */
table.changes td.discord { overflow-wrap: anywhere; }
/* A short date is two words with a space in the middle, and wrapping at it
   would cost the row a second line for the sake of three characters. The cell
   is also the positioning context for the box the date opens. */
table.changes td.invited { position: relative; white-space: nowrap; }
/* An account with no recorded invitation has no date to show, so the word
   standing in for one reads as an absence. */
table.changes td.undated { color: var(--muted); }
/* The date is a button rather than plain text because a tooltip a mouse has
   to hover reaches nobody on a phone and nobody working by keyboard. It is
   styled back down to the text it replaced, keeping the dotted underline that
   says there is more behind it. */
table.changes .tip-trigger {
  appearance: none;
  -webkit-appearance: none;
  background: none;
  border: 0;
  margin: 0;
  padding: 0;
  color: inherit;
  font: inherit;
  cursor: pointer;
  text-decoration: underline dotted;
  text-underline-offset: 0.2rem;
}
/* Hangs below its cell rather than over the next row's text, and grows to the
   left from the cell's right edge, which is what keeps the last column's box
   inside the card on a phone. */
.cell-tip {
  position: absolute;
  z-index: 3;
  top: calc(100% - 0.25rem);
  right: 0;
  width: max-content;
  max-width: min(15rem, 62vw);
  padding: 0.4rem 0.55rem;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 0.78rem;
  color: var(--text);
  white-space: normal;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.35);
}
/* Above its cell instead, for a row with no room under it. */
.cell-tip.above { top: auto; bottom: calc(100% - 0.25rem); }
/* An invited account that no application post matched has no Discord name to
   show, and the reason reads as an absence rather than as a name. */
table.changes td.unmatched { color: var(--muted); }
#pending-status { color: var(--muted); font-size: 0.85rem; padding-top: 0.5rem; }
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
  /* The account a change is about is the column worth the width on a phone;
     who did the kicking is shown in the chart tooltip instead. */
  table.changes .by { display: none; }
  /* The dot's colour already says which kind of change it was, and the word
     beside it costs the account column width it has none of to spare. The
     word stays in the table, out of sight rather than out of the document,
     so a screen reader still reads each row's change out. */
  table.changes .change-label {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
  table.changes .change .dot { margin-right: 0; }
  /* With nothing but the dot left in it, the column reads as a column of dots
     rather than one dot per row hanging off a ragged left edge. The heading
     goes with them so the two stay over each other. */
  table.changes .change { text-align: center; }
}
</style>
</head>
<body>
<header>
  <h1 id="brand">Guild Roster</h1>
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
      <h2>Guild members over time<span id="now-count" class="now"></span></h2>
      <button type="button" id="chart-mode" class="chart-mode"
        aria-label="Switch to staircase graph" title="Regular line graph">╱</button>
    </div>
    <div id="chart"></div>
    <div id="legend" class="legend" role="list" aria-label="Change kinds"></div>
    <div id="chart-status" role="status" aria-live="polite"></div>
  </section>
  <section class="card">
    <h2>Membership changes</h2>
    <div id="totals" class="totals"></div>
    <div id="table"></div>
    <div id="pager" class="pager"></div>
  </section>
  <section class="card">
    <h2>Pending invites<span id="pending-count" class="now"></span></h2>
    <p class="note">These accounts have been invited in-game but have not
      accepted yet, so they hold a place against the guild's 500 without
      being members.</p>
    <div id="pending"></div>
    <div id="pending-status" role="status" aria-live="polite"></div>
  </section>
</main>
<script>
"use strict";
(function () {
  // Okabe-Ito colourblind-safe palette: one hue per kind of change, and a
  // fourth for the member count line itself.
  var KINDS = {
    join: { color: "#009E73", label: "Joined" },
    leave: { color: "#E69F00", label: "Left" },
    kick: { color: "#D55E00", label: "Kicked" }
  };
  var KIND_ORDER = ["join", "leave", "kick"];
  var LINE_COLOR = "#56B4E9";
  var SVG_NS = "http://www.w3.org/2000/svg";
  var TABLE_PAGE_SIZE = 10;
  // Smallest number of members the y axis ever spans, so a quiet week does not
  // turn a single departure into a cliff.
  var MIN_SPAN = 6;

  var mobileQuery = window.matchMedia("(max-width: 640px)");
  function isMobile() { return mobileQuery.matches; }

  // The chart uses a wide viewBox on desktop and a taller one on mobile, where
  // it scales to the narrow screen width; the extra height makes the graph
  // read large on a phone. Coordinates are computed against whichever set is
  // active, so M is refreshed at the start of every chart render.
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
  var nowCount = document.getElementById("now-count");
  var pendingBox = document.getElementById("pending");
  var pendingStatus = document.getElementById("pending-status");
  var pendingCount = document.getElementById("pending-count");
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
  function events() { return (state.data && state.data.events) || []; }

  function traceMode(mode, count) {
    console.debug("roster chart mode:", mode, count);
  }
  function kindOf(kind) { return KINDS[kind] || KINDS.leave; }

  // The axis covers the counts actually reached in the window, padded out to
  // MIN_SPAN and rounded to whole members, so the line uses the full height
  // instead of hugging the 500-member ceiling.
  function computeScale() {
    var values = points().map(function (point) { return point.count; });
    if (!values.length) { return null; }
    var low = Math.min.apply(null, values);
    var high = Math.max.apply(null, values);
    var pad = Math.max(1, Math.round((high - low) * 0.15));
    low -= pad;
    high += pad;
    if (high - low < MIN_SPAN) {
      var grow = Math.ceil((MIN_SPAN - (high - low)) / 2);
      low -= grow;
      high += grow;
    }
    if (low < 0) { low = 0; }
    var step = Math.max(1, Math.ceil((high - low) / 4));
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
  function scaleY(count) {
    var scale = state.scale;
    var span = scale.high - scale.low;
    var value = count;
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
  // "Jun 17": as much of a moment as a narrow column has room for. What it
  // leaves out is on the cell's tooltip rather than lost.
  function formatShortDate(t) {
    return new Date(t * 1000).toLocaleDateString(
      undefined, { month: "short", day: "numeric" });
  }
  function countedAge(value, unit) {
    return value + " " + unit + (value === 1 ? "" : "s") + " ago";
  }
  // "3 minutes ago", "7 days ago". Coarse on purpose: the moment itself is
  // beside it, and what an age is read for is how stale something is rather
  // than a count to the second. Days are the largest unit, so a wait of weeks
  // is still counted in a unit a reader can compare against the 14-day Trial
  // clock without converting it back.
  function formatAge(seconds) {
    if (seconds < 60) { return "just now"; }
    if (seconds < 3600) {
      return countedAge(Math.floor(seconds / 60), "minute");
    }
    if (seconds < 86400) {
      return countedAge(Math.floor(seconds / 3600), "hour");
    }
    return countedAge(Math.floor(seconds / 86400), "day");
  }
  // The same moment, with the year when it is not this one. An invitation
  // can sit unanswered across New Year, and "Jun 17, 9:30 PM" would then be
  // read as this June rather than last.
  function formatMomentWithYear(t) {
    var date = new Date(t * 1000);
    if (date.getFullYear() === new Date().getFullYear()) {
      return formatMoment(t);
    }
    return date.toLocaleString(
      undefined,
      {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit"
      });
  }
  // "Sep 20, 9:30 PM | 7 days ago": the moment the membership table shows in
  // full, and how long ago it was by the reader's own clock. A clock running
  // a little behind the server's would otherwise date an invite in the
  // future, which formatAge() reads as "just now" rather than as a negative
  // age.
  function formatMomentWithAge(t) {
    return formatMomentWithYear(t) + " | " +
      formatAge(Math.max(0, Math.floor(Date.now() / 1000 - t)));
  }

  function renderChart() {
    M = metrics();
    state.scale = computeScale();
    if (detachHover) { detachHover(); detachHover = null; }
    chart.replaceChildren();
    if (!state.scale) {
      chartStatus.textContent = state.data
        ? "No guild member count has been recorded yet, so the roster line " +
          "cannot be drawn. The changes below are still listed."
        : "";
      return;
    }
    var canvas = svg("svg", {
      "class": "chart-svg",
      viewBox: "0 0 " + M.w + " " + M.h,
      role: "img",
      "aria-label": "Guild member count over time, with one dot per change"
    });

    // Horizontal gridlines and y labels at every step of the computed scale.
    for (var value = state.scale.low;
         value <= state.scale.high;
         value += state.scale.step) {
      var y = scaleY(value);
      canvas.appendChild(svg("line", {
        "class": value === state.scale.low ? "axis" : "grid",
        x1: M.left, y1: y, x2: M.left + plotW(), y2: y
      }));
      var yLabel = svg("text", {
        "class": "y-label", x: M.left - 6, y: y + 4
      });
      yLabel.textContent = String(value);
      canvas.appendChild(yLabel);
    }

    // Left axis, plus x labels spaced evenly across the whole window so the
    // range spans the full width even when little happened.
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
      // The outermost labels sit on the plot's own edges, so centring them
      // would hang half of each one off the chart and the browser would clip
      // it. They are tucked inwards instead - by class, because the
      // stylesheet's text-anchor would win over a presentation attribute.
      if (i === 0) { xLabel.classList.add("first"); }
      if (i === M.ticks) { xLabel.classList.add("last"); }
      xLabel.textContent = formatTick(t);
      canvas.appendChild(xLabel);
    }

    // Regular mode connects recorded counts directly. Staircase mode inserts
    // the previous count at each new timestamp before drawing the jump.
    var coords = [];
    points().forEach(function (point, index) {
      var px = scaleX(point.t);
      var py = scaleY(point.count);
      if (state.staircase && index > 0) {
        coords.push(px.toFixed(1) + "," +
          scaleY(points()[index - 1].count).toFixed(1));
      }
      coords.push(px.toFixed(1) + "," + py.toFixed(1));
    });
    if (coords.length > 1) {
      canvas.appendChild(svg("polyline", {
        "class": "count-line", stroke: LINE_COLOR, points: coords.join(" ")
      }));
    }

    // One dot per change, in its kind's colour. Every change is drawn; the
    // series is never downsampled. Each dot is collected so the hover can
    // snap to it.
    var plotted = [];
    events().forEach(function (event) {
      if (event.count === null) { return; }
      var px = scaleX(event.t);
      var py = scaleY(event.count);
      canvas.appendChild(svg("circle", {
        "class": "event-dot",
        cx: px.toFixed(1),
        cy: py.toFixed(1),
        r: 4,
        fill: kindOf(event.kind).color
      }));
      plotted.push({ x: px, y: py, event: event });
    });

    detachHover = attachHover(canvas, plotted);
    chart.appendChild(canvas);
    chartStatus.textContent = plotted.length
      ? ""
      : "No members joined or left in this period.";
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

  // Changes that share a moment form a single column, so the crosshair snaps
  // to one x and the tooltip lists everything recorded there.
  function groupColumns(plotted) {
    var byTime = {};
    var columns = [];
    plotted.forEach(function (point) {
      var key = String(point.event.t);
      var column = byTime[key];
      if (!column) {
        column = { t: point.event.t, x: point.x, points: [] };
        byTime[key] = column;
        columns.push(column);
      }
      column.points.push(point);
    });
    return columns;
  }

  // Tells a hovering pointer from a finger or a pen. A touch has no hover
  // state: the browser sends one pointermove at the tap point and then a
  // pointerleave as the finger lifts. Touch selects by tapping instead and
  // never reaches the move or leave handlers.
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
        name === "keydown" || name === "blur" ||
        name === "scroll" || name === "resize") {
      return name;
    }
    return "other";
  }

  // Sanitized tracing for the tap selection lifecycle, so a console trace can
  // explain why a selection opened, moved or went away. Every call passes a
  // fixed action name, one of the narrowed reason names above, and a count of
  // drawn elements. Account names, timestamps and member counts are never
  // passed, so no part of the payload or of the reader's gesture reaches the
  // console. debug keeps it out of the default console view.
  function traceSelection(action, reason, count) {
    console.debug("roster chart selection:", action, reason, count);
  }

  function attachHover(canvas, plotted) {
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
      var imported = false;
      column.points.forEach(function (point) {
        var change = point.event;
        var row = el("div",
          "tip-row" + (point === emphasized ? " em" : ""));
        var swatch = el("span", "swatch");
        swatch.style.background = kindOf(change.kind).color;
        row.appendChild(swatch);
        row.appendChild(el("span", "name", describe(change)));
        row.appendChild(el("span", "val", String(change.count)));
        tooltip.appendChild(row);
        if (change.imported) { imported = true; }
      });
      if (imported) {
        tooltip.appendChild(el("div", "tip-note",
          "Time taken from the log channel message."));
      }
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
          stroke: kindOf(point.event.kind).color
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
      if (!columns.length) { return { column: null, reason: "no-changes" }; }
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

  // What one change reads as in a tooltip row: the account, and for a kick the
  // officer who did it.
  function describe(change) {
    if (change.kind === "kick" && change.actor) {
      return change.name + " \\u2014 kicked by " + change.actor;
    }
    return change.name;
  }

  function renderLegend() {
    legend.replaceChildren();
    KIND_ORDER.forEach(function (kind) {
      var item = el("span", "item");
      item.setAttribute("role", "listitem");
      var swatch = el("span", "swatch");
      swatch.style.background = KINDS[kind].color;
      item.appendChild(swatch);
      item.appendChild(el("span", "legend-name", KINDS[kind].label));
      legend.appendChild(item);
    });
  }

  function renderTotals() {
    totals.replaceChildren();
    if (!state.data) { return; }
    [
      ["Joined", state.data.joins],
      ["Left", state.data.leaves],
      ["Kicked", state.data.kicks]
    ].forEach(function (pair) {
      var item = el("span", null);
      item.appendChild(el("span", "num", String(pair[1] || 0)));
      item.appendChild(document.createTextNode(" " + pair[0].toLowerCase()));
      totals.appendChild(item);
    });
    // A preset window runs to the moment of the request, so its closing
    // count is the count now; a picked one can close months ago, where the
    // only thing that count is true of is the window's own end.
    nowCount.textContent = state.data.member_count === null ||
      state.data.member_count === undefined
      ? ""
      : "\\u2014 " + state.data.member_count +
        (state.range === "custom" ? " at the end" : " now");
  }

  function renderTable() {
    tableBox.replaceChildren();
    pager.replaceChildren();
    var changes = events();
    if (!changes.length) {
      tableBox.appendChild(
        el("div", "empty", "No members joined or left in this period."));
      return;
    }
    var pageCount = Math.ceil(changes.length / TABLE_PAGE_SIZE);
    if (state.tablePage > pageCount - 1) { state.tablePage = pageCount - 1; }
    var start = state.tablePage * TABLE_PAGE_SIZE;
    var pageRows = changes.slice(start, start + TABLE_PAGE_SIZE);

    var table = el("table", "changes");
    var head = el("tr");
    head.appendChild(el("th", null, "Time"));
    head.appendChild(el("th", null, "Account"));
    head.appendChild(el("th", "change", "Change"));
    var byHead = el("th", "by", "By");
    head.appendChild(byHead);
    head.appendChild(el("th", "num", "Members"));
    table.appendChild(head);
    pageRows.forEach(function (change) {
      var row = el("tr");
      row.appendChild(el("td", null, formatMoment(change.t)));
      row.appendChild(el("td", "name", change.name));
      var kindCell = el("td", "change");
      var dot = el("span", "dot");
      dot.style.background = kindOf(change.kind).color;
      kindCell.appendChild(dot);
      // The name of the change is its own element so the phone layout can put
      // it out of sight and leave the dot standing for it.
      kindCell.appendChild(
        el("span", "change-label", kindOf(change.kind).label));
      row.appendChild(kindCell);
      row.appendChild(el("td", "by", change.actor || ""));
      row.appendChild(el("td", "num",
        change.count === null ? "" : String(change.count)));
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
      " (" + changes.length + " changes)"));
  }

  function render() {
    renderLegend();
    renderTotals();
    renderChart();
    renderTable();
  }

"""
    + range_picker_js("roster")
    + """
  // Sanitized tracing for the pending invite section: a fixed action name and
  // a count of rows. No account name, Discord name or payload is ever passed.
  function tracePending(action, count) {
    console.debug("roster pending invites:", action, count);
  }

  // The moment-and-age box the "Invite sent" column opens: one node, moved
  // into whichever cell is showing it. A title attribute was a desktop-only
  // answer - a phone has no hover to give, and neither has a keyboard.
  var inviteTip = el("div", "cell-tip");
  inviteTip.id = "invite-tip";
  inviteTip.setAttribute("role", "tooltip");
  var inviteTipTrigger = null;

  // Sanitized tracing for the box, so a console trace can explain why one
  // opened or went away: a fixed action name and, for a dismissal, one of the
  // narrowed event names above. No date, account name or payload is passed.
  function traceInviteTip(action, reason) {
    console.debug("roster invite tip:", action, reason || "");
  }

  function showInviteTip(trigger, text) {
    hideInviteTip("replaced");
    inviteTip.textContent = text;
    trigger.parentNode.appendChild(inviteTip);
    placeInviteTip(trigger);
    // Described by the box only while the box is on screen, which is what a
    // screen reader expects of a tooltip.
    trigger.setAttribute("aria-describedby", inviteTip.id);
    inviteTipTrigger = trigger;
    document.addEventListener("pointerdown", dismissInviteTip, true);
    document.addEventListener("wheel", dismissInviteTip, true);
    document.addEventListener("scroll", dismissInviteTip, true);
    document.addEventListener("keydown", dismissInviteTip, true);
    window.addEventListener("blur", dismissInviteTip);
    window.addEventListener("resize", dismissInviteTip);
    traceInviteTip("open");
  }

  // A box below the last rows of a phone screen would hang off the bottom,
  // and the reader cannot scroll it into view because scrolling is one of the
  // things that puts it away. It opens upwards instead, unless there is no
  // room up there either, where below is still the lesser of the two.
  function placeInviteTip(trigger) {
    inviteTip.classList.remove("above");
    var viewport = window.innerHeight ||
      document.documentElement.clientHeight;
    var box = inviteTip.getBoundingClientRect();
    if (box.bottom <= viewport) { return; }
    var cell = trigger.getBoundingClientRect();
    if (cell.top - box.height >= 0) { inviteTip.classList.add("above"); }
  }

  function hideInviteTip(reason) {
    if (!inviteTipTrigger) { return; }
    inviteTipTrigger.removeAttribute("aria-describedby");
    inviteTipTrigger = null;
    inviteTip.remove();
    document.removeEventListener("pointerdown", dismissInviteTip, true);
    document.removeEventListener("wheel", dismissInviteTip, true);
    document.removeEventListener("scroll", dismissInviteTip, true);
    document.removeEventListener("keydown", dismissInviteTip, true);
    window.removeEventListener("blur", dismissInviteTip);
    window.removeEventListener("resize", dismissInviteTip);
    traceInviteTip("close", reason);
  }

  // Anything but the box itself closes it: a tap or click elsewhere on the
  // page, a scroll either way, a wheel, a key, a resize, or the window losing
  // focus.
  function dismissInviteTip(event) {
    var target = event && event.target;
    // A press inside the box is someone reading it, not dismissing it, so it
    // is the one place a press leaves it standing.
    if (target && target.nodeType && inviteTip.contains(target)) { return; }
    // A press on a date is left to that date's own click, which opens its box
    // or closes the one it opened. Closing here first would let that click
    // reopen what the press meant to dismiss.
    if (event && event.type === "pointerdown" && target && target.closest &&
        target.closest(".tip-trigger")) {
      return;
    }
    // Enter and Space on the open date reach here before the click they turn
    // into, and are left to it for the same reason. Every other key, Escape
    // and Tab included, closes the box.
    if (event && event.type === "keydown" && target === inviteTipTrigger &&
        (event.key === "Enter" || event.key === " " ||
          event.key === "Spacebar")) {
      return;
    }
    hideInviteTip("page-" + eventKind(event));
  }

  // The cell for an invitation that has a date: the short date as a button,
  // which opens the box on a tap, a click or a keypress and on a mouse's
  // hover. The age is worked out as the box opens rather than as the row is
  // drawn, so a page left open overnight cannot still call the invitation
  // three minutes old.
  function inviteSentCell(sentAt) {
    var cell = el("td", "invited");
    var trigger = el("button", "tip-trigger", formatShortDate(sentAt));
    trigger.type = "button";
    trigger.addEventListener("click", function () {
      if (inviteTipTrigger === trigger) {
        hideInviteTip("toggle");
        return;
      }
      showInviteTip(trigger, formatMomentWithAge(sentAt));
    });
    trigger.addEventListener("pointerenter", function (event) {
      if (isHoverPointer(event)) {
        showInviteTip(trigger, formatMomentWithAge(sentAt));
      }
    });
    trigger.addEventListener("pointerleave", function (event) {
      // A finger's pointerleave arrives as it lifts off the glass, and acting
      // on it would close the box the tap had just opened.
      if (isHoverPointer(event) && inviteTipTrigger === trigger) {
        hideInviteTip("pointer-leave");
      }
    });
    cell.appendChild(trigger);
    return cell;
  }

  function renderPending(invites, matched) {
    // The rows the open box was anchored to are about to be replaced, and a
    // box left behind would hang off a cell that is no longer on the page.
    hideInviteTip("redraw");
    pendingBox.replaceChildren();
    pendingCount.textContent = invites.length
      ? "\\u2014 " + invites.length + " waiting"
      : "";
    if (!invites.length) {
      pendingBox.appendChild(el("div", "empty",
        "No invites are waiting to be accepted."));
      return;
    }
    // Its own class, because the membership table's "By" column is hidden on
    // a phone and this table's second column is the section's whole point.
    var table = el("table", "changes pending");
    var head = el("tr");
    head.appendChild(el("th", null, "Account"));
    head.appendChild(el("th", null, "Discord"));
    head.appendChild(el("th", null, "Invite sent"));
    table.appendChild(head);
    invites.forEach(function (invite) {
      var row = el("tr");
      row.appendChild(el("td", "name", invite.name));
      // An account nobody matched to an application post is named as
      // unmatched rather than left blank, so the empty cell cannot read as a
      // Discord account with no name. When the forum could not be read in
      // full, a row without a name may be missing it only because the post
      // naming it was one of the unread ones, and "no application matched"
      // would be an assertion the server never made. A name that is shown was
      // matched either way.
      row.appendChild(invite.discord_name
        ? el("td", "discord", invite.discord_name)
        : el("td", "discord unmatched", matched
          ? "No application matched"
          : "Could not be checked"));
      // The column has room for the date alone, so the rest of the moment and
      // how long ago it was are in the box the date opens. An invitation the
      // bot never recorded a guild-log event for has no date to show, and the
      // row says so rather than standing in a date of its own.
      row.appendChild(typeof invite.invited_at === "number"
        ? inviteSentCell(invite.invited_at)
        : el("td", "invited undated", "Unknown"));
      table.appendChild(row);
    });
    pendingBox.appendChild(table);
  }

  // The pending invites are the guild's state right now rather than a window
  // of history, so they are loaded once with the page and are not reloaded
  // when the range changes.
  function loadPending() {
    pendingStatus.textContent = "Loading\\u2026";
    fetch("/api/pending")
      .then(function (response) {
        if (response.status === 401) {
          location.href = "/login";
          throw new Error("unauthorized");
        }
        if (!response.ok) { throw new Error("failed"); }
        return response.json();
      })
      .then(function (payload) {
        if (!payload.available) {
          pendingBox.replaceChildren();
          pendingCount.textContent = "";
          // The server names the settings that are unset, so the reader is
          // told which /settings subcommands turn the section on rather than
          // being sent to the README for them.
          var missing = payload.missing || [];
          pendingStatus.textContent = missing.length
            ? "The pending invites are off until " +
              missing.map(function (name) { return "/settings " + name; })
                .join(" and ") + " " +
              (missing.length === 1 ? "is" : "are") + " set."
            : "The pending invites are off until the Guild Wars 2 API " +
              "settings are set.";
          tracePending("unavailable", missing.length);
          return;
        }
        var invites = payload.invites || [];
        var matched = payload.matched !== false;
        renderPending(invites, matched);
        pendingStatus.textContent = matched
          ? ""
          : "The Trial application forum could not be read in full, so an " +
            "account without a Discord name here may still have applied.";
        tracePending(matched ? "render" : "unmatched", invites.length);
      })
      .catch(function (error) {
        // renderPending() runs inside this chain, so a drawing fault lands
        // here too. Only the error's type and message are logged; no request,
        // response or payload is ever passed through.
        console.error(
          "pending invite load failed:",
          error && error.name, error && error.message);
        pendingStatus.textContent = "Could not load the pending invites.";
      });
  }

  function refresh() {
    chartStatus.textContent = "Loading\\u2026";
    fetch("/api/roster" + rangeQuery())
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
          "roster history load failed:",
          error && error.name, error && error.message);
        if (chartStatus.textContent === "Loading\\u2026") {
          chartStatus.textContent = "Could not load the roster history.";
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
  loadPending();
})();
</script>
</body>
</html>
"""
)
