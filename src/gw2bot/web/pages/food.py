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
table.removals { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
table.removals th, table.removals td {
  text-align: left;
  padding: 0.4rem 0.6rem;
  border-bottom: 1px solid var(--border);
}
table.removals th { color: var(--muted); font-weight: 600; }
table.removals td.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
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
    <h2>Removals</h2>
    <div id="tabs" class="tabs"></div>
    <div id="table"></div>
    <div id="pager" class="pager"></div>
  </section>
</main>
<script>
"use strict";
(function () {
  // Okabe-Ito colourblind-safe categorical palette, one hue per tracked feast.
  var COLORS = ["#56B4E9", "#E69F00", "#009E73", "#CC79A7"];
  var SVG_NS = "http://www.w3.org/2000/svg";
  var TABLE_PAGE_SIZE = 5;
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
    staircase: false, scale: null
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
      tooltip.appendChild(el("div", "tip-time", formatMoment(column.t)));
      column.points.forEach(function (point) {
        var row = el("div",
          "tip-row" + (point === emphasized ? " em" : ""));
        var swatch = el("span", "swatch");
        swatch.style.background = point.color;
        row.appendChild(swatch);
        row.appendChild(el("span", "name", point.name));
        row.appendChild(el("span", "val", String(point.count)));
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

  function renderLegend() {
    legend.replaceChildren();
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
      });
      legend.appendChild(item);
    });
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

  function render() {
    renderLegend();
    renderChart();
    renderTabs();
    renderTable();
  }

"""
    + range_picker_js("feast")
    + """
  function refresh() {
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
        state.tablePage = 0;
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
