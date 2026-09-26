"""The guild event statistics dashboard page.

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

EVENTS_PAGE = (
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Event Statistics</title>
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
.card h2 { font-size: 0.95rem; margin-bottom: 0.6rem; }
/* The four headline figures are tiles rather than a chart: each is one
   number, and a number is read faster than a bar. */
.tiles {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 1rem;
}
.tile {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 0.8rem 1rem;
  min-width: 0;
}
.tile .label { color: var(--muted); font-size: 0.8rem; }
.tile .value {
  font-size: 1.5rem;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  margin-top: 0.2rem;
  overflow-wrap: anywhere;
}
/* A commander's name is longer than a count, so it steps down a size
   rather than breaking the row of tiles. */
.tile .value.name { font-size: 1.1rem; }
.tile .note { color: var(--muted); font-size: 0.8rem; margin-top: 0.2rem; }
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
.chart-svg .runs-line { fill: none; stroke-width: 2; }
.chart-svg .run-dot { stroke: var(--panel); stroke-width: 2; }
.chart-svg .overlay { fill: transparent; }
/* A thin, translucent gray line the hover snaps to the nearest run. */
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
.chart-tooltip .tip-row { overflow-wrap: anywhere; }
.chart-tooltip .tip-row .by { color: var(--muted); }
.chart-tooltip .tip-note { color: var(--muted); margin-top: 0.25rem; }
#chart-status { color: var(--muted); font-size: 0.85rem; padding-top: 0.5rem; }
table.stats { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
table.stats th, table.stats td {
  text-align: left;
  padding: 0.4rem 0.6rem;
  border-bottom: 1px solid var(--border);
}
table.stats th { color: var(--muted); font-weight: 600; }
/* Event titles and Discord names can be long words, so they break inside
   the word rather than widening the table past its card. */
table.stats td.name { overflow-wrap: anywhere; }
table.stats td .sub { color: var(--muted); font-size: 0.78rem; }
table.stats th.num, table.stats td.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
table.stats td.unknown { color: var(--muted); }
.empty { color: var(--muted); padding: 0.6rem; }
.pager {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-top: 0.75rem;
  color: var(--muted);
  font-size: 0.85rem;
}
.pager:empty { display: none; }
button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
@media (max-width: 640px) {
  main { padding: 0.6rem 0.5rem; }
  .card { padding: 0.6rem; }
  .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.5rem; }
  .tile { padding: 0.6rem; }
  /* The date of the last run is the column that gives way on a phone: the
     event and its count are what the table is read for. */
  table.stats .last { display: none; }
}
</style>
</head>
<body>
<header>
  <h1 id="brand">Event Statistics</h1>
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
  <section id="tiles" class="tiles" aria-label="Event run totals">
    <div class="tile">
      <div class="label">Event runs</div>
      <div id="tile-runs" class="value">&mdash;</div>
    </div>
    <div class="tile">
      <div class="label">Time run</div>
      <div id="tile-time" class="value">&mdash;</div>
    </div>
    <div class="tile">
      <div class="label">Participants</div>
      <div id="tile-participants" class="value">&mdash;</div>
      <div class="note">members, commanders included</div>
    </div>
    <div class="tile">
      <div class="label">Top commander</div>
      <div id="tile-commander" class="value name">&mdash;</div>
      <div id="tile-commander-note" class="note"></div>
    </div>
  </section>
  <section class="card">
    <h2>Cumulative event runs</h2>
    <div id="chart"></div>
    <div id="chart-status" role="status" aria-live="polite"></div>
  </section>
  <section class="card">
    <h2>Mentees</h2>
    <div id="mentees-table"></div>
    <div id="mentees-pager" class="pager"></div>
  </section>
  <section class="card">
    <h2>Events without a mentee</h2>
    <div id="no-mentee-table"></div>
    <div id="no-mentee-pager" class="pager"></div>
  </section>
  <section class="card">
    <h2>Events with no requirements</h2>
    <div id="no-requirements-table"></div>
    <div id="no-requirements-pager" class="pager"></div>
  </section>
</main>
<script>
"use strict";
(function () {
  // The cumulative line is the page's one series, drawn in the colour the
  // roster page gives its own line.
  var LINE_COLOR = "#56B4E9";
  var SVG_NS = "http://www.w3.org/2000/svg";
  var TABLE_PAGE_SIZE = 10;

  var mobileQuery = window.matchMedia("(max-width: 640px)");
  function isMobile() { return mobileQuery.matches; }

  // The chart uses a wide viewBox on desktop and a taller one on mobile,
  // where it scales to the narrow screen width. Coordinates are computed
  // against whichever set is active, so M is refreshed at the start of every
  // chart render.
  function metrics() {
    if (isMobile()) {
      return {
        w: 480, h: 520, top: 16, right: 14, bottom: 36, left: 40, ticks: 4
      };
    }
    return {
      w: 960, h: 320, top: 16, right: 16, bottom: 32, left: 44, ticks: 6
    };
  }
  var M = metrics();
  function plotW() { return M.w - M.left - M.right; }
  function plotH() { return M.h - M.top - M.bottom; }

  var state = {
    // No range until the server answers: the first load asks for the window
    // this member last picked rather than naming one over the top of it.
    range: null, data: null, scale: null,
    pages: { mentees: 0, noMentee: 0, noRequirements: 0 }
  };

  // A pinned touch selection listens on the whole page, so the chart it
  // belongs to is torn down before another one is drawn.
  var detachHover = null;

  var chart = document.getElementById("chart");
  var chartStatus = document.getElementById("chart-status");

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
  function total() { return (state.data && state.data.runs) || 0; }

  function formatCount(value) { return Number(value).toLocaleString(); }

  // Minutes as the hours and minutes a reader adds up in their head. Hours
  // are not rolled into days: a month of raids is read as hours played.
  function formatDuration(minutes) {
    var whole = Math.max(0, Math.round(minutes || 0));
    var hours = Math.floor(whole / 60);
    var rest = whole % 60;
    if (!hours) { return rest + "m"; }
    return hours.toLocaleString() + "h" + (rest ? " " + rest + "m" : "");
  }

  function runsWord(count) { return count === 1 ? "run" : "runs"; }

  // A gridline step of 1, 2 or 5 times a power of ten, never below one run:
  // a count has no fractions to land a gridline on.
  function niceStep(span, target) {
    var raw = span / target;
    if (!(raw > 1)) { return 1; }
    var magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
    var normalized = raw / magnitude;
    var step = 10;
    if (normalized <= 1) { step = 1; }
    else if (normalized <= 2) { step = 2; }
    else if (normalized <= 5) { step = 5; }
    return step * magnitude;
  }

  // A running count starts at zero, so the axis does too, and it reaches at
  // least one run so an empty window still draws a readable baseline.
  function computeScale() {
    var high = Math.max(1, total());
    var step = niceStep(high, 4);
    return { low: 0, high: step * Math.ceil(high / step), step: step };
  }

  function scaleX(t) {
    var since = state.data.since;
    var span = state.data.now - since;
    var frac = span > 0 ? (t - since) / span : 0;
    if (frac < 0) { frac = 0; }
    if (frac > 1) { frac = 1; }
    return M.left + frac * plotW();
  }
  function scaleY(count) {
    var scale = state.scale;
    var value = Math.max(scale.low, Math.min(scale.high, count));
    return M.top + (1 - (value - scale.low) / (scale.high - scale.low)) *
      plotH();
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

  // Runs that ended in the same clock minute share one dot, at the count
  // after the last of them, so a burst of runs is one point to hover rather
  // than a stack of dots on top of each other.
  function runMinutes() {
    var byMinute = {};
    var minutes = [];
    points().forEach(function (point) {
      var key = String(Math.floor(point.t / 60));
      var minute = byMinute[key];
      if (!minute) {
        minute = { t: point.t, count: point.count, runs: [] };
        byMinute[key] = minute;
        minutes.push(minute);
      }
      minute.t = point.t;
      minute.count = point.count;
      minute.runs.push(point);
    });
    return minutes;
  }

  function renderChart() {
    M = metrics();
    state.scale = computeScale();
    if (detachHover) { detachHover(); detachHover = null; }
    chart.replaceChildren();
    if (!state.data) { chartStatus.textContent = ""; return; }
    var canvas = svg("svg", {
      "class": "chart-svg",
      viewBox: "0 0 " + M.w + " " + M.h,
      role: "img",
      "aria-label":
        "Event runs so far over the period, one step per run as it ended"
    });

    // Horizontal gridlines and y labels at every step of the scale. The loop
    // counts steps rather than accumulating them.
    var lines = Math.round(
      (state.scale.high - state.scale.low) / state.scale.step);
    for (var i = 0; i <= lines; i += 1) {
      var value = state.scale.low + state.scale.step * i;
      var y = scaleY(value);
      canvas.appendChild(svg("line", {
        "class": i === 0 ? "axis" : "grid",
        x1: M.left, y1: y, x2: M.left + plotW(), y2: y
      }));
      var yLabel = svg("text", {
        "class": "y-label", x: M.left - 6, y: y + 4
      });
      yLabel.textContent = formatCount(value);
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
      var xLabel = svg("text", {
        "class": "x-label", x: scaleX(t), y: M.top + plotH() + 18
      });
      // The outermost labels sit on the plot's own edges, so they are tucked
      // inwards rather than centred half off the chart.
      if (tick === 0) { xLabel.classList.add("first"); }
      if (tick === M.ticks) { xLabel.classList.add("last"); }
      xLabel.textContent = formatTick(t);
      canvas.appendChild(xLabel);
    }

    // A running count only ever steps: it holds flat until a run ends and
    // rises there. So the line is a staircase from nothing at the window's
    // opening edge to the window's total at its closing one.
    var coords = [];
    var previous = 0;
    function vertex(at, count) {
      coords.push(scaleX(at).toFixed(1) + "," + scaleY(count).toFixed(1));
    }
    vertex(state.data.since, 0);
    points().forEach(function (point) {
      vertex(point.t, previous);
      vertex(point.t, point.count);
      previous = point.count;
    });
    vertex(state.data.now, previous);
    canvas.appendChild(svg("polyline", {
      "class": "runs-line",
      stroke: LINE_COLOR,
      points: coords.join(" ")
    }));

    // Dots go on after the line so the line cannot cover their fill.
    var plotted = runMinutes().map(function (minute) {
      var point = {
        x: scaleX(minute.t), y: scaleY(minute.count), t: minute.t,
        count: minute.count, runs: minute.runs
      };
      canvas.appendChild(svg("circle", {
        "class": "run-dot",
        cx: point.x.toFixed(1),
        cy: point.y.toFixed(1),
        r: 4,
        fill: LINE_COLOR
      }));
      return point;
    });

    detachHover = attachHover(canvas, plotted);
    chart.appendChild(canvas);
    chartStatus.textContent = plotted.length
      ? ""
      : "No event runs ended in this period.";
  }

  // Tells a hovering pointer from a finger or a pen. Touch selects by
  // tapping instead and never reaches the move or leave handlers.
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

  // Sanitized tracing for the tap selection lifecycle. Every call passes a
  // fixed action name, one of the narrowed reason names above, and a count
  // of drawn elements. Event titles, names and times are never passed.
  function traceSelection(action, reason, count) {
    console.debug("events chart selection:", action, reason, count);
  }

  function attachHover(canvas, plotted) {
    // The viewBox differs between the mobile and desktop layouts, so the
    // hover is pinned to the metrics this canvas was drawn with.
    var m = M;
    var innerW = m.w - m.left - m.right;
    var innerH = m.h - m.top - m.bottom;
    var pinned = false;
    var pinOrigin = null;

    var crosshair = svg("line", {
      "class": "crosshair",
      y1: m.top,
      y2: m.top + innerH
    });
    crosshair.style.visibility = "hidden";
    var ring = svg("circle", {
      "class": "hover-ring", r: 7, stroke: LINE_COLOR
    });
    ring.style.visibility = "hidden";
    var overlay = svg("rect", {
      "class": "overlay",
      x: m.left,
      y: m.top,
      width: innerW,
      height: innerH
    });
    overlay.style.cursor = "crosshair";
    canvas.appendChild(crosshair);
    canvas.appendChild(ring);
    canvas.appendChild(overlay);

    var tooltip = el("div", "chart-tooltip");
    tooltip.style.visibility = "hidden";
    chart.appendChild(tooltip);

    // Picks the dot nearest the pointer in both directions, so two runs a
    // few minutes apart can still be told apart by height.
    function nearest(vbX, vbY) {
      var best = null;
      var bestDist = Infinity;
      plotted.forEach(function (point) {
        var dx = point.x - vbX;
        var dy = point.y - vbY;
        var dist = dx * dx + dy * dy;
        if (dist < bestDist) { bestDist = dist; best = point; }
      });
      return best;
    }

    function show(point) {
      crosshair.setAttribute("x1", point.x);
      crosshair.setAttribute("x2", point.x);
      crosshair.style.visibility = "visible";
      ring.setAttribute("cx", point.x);
      ring.setAttribute("cy", point.y);
      ring.style.visibility = "visible";
      tooltip.replaceChildren();
      tooltip.appendChild(el("div", "tip-time", formatMoment(point.t)));
      point.runs.forEach(function (run) {
        var row = el("div", "tip-row", run.title);
        row.appendChild(el("span", "by", " \\u2014 " + run.commander));
        tooltip.appendChild(row);
      });
      tooltip.appendChild(el("div", "tip-note",
        formatCount(point.count) + " " + runsWord(point.count) +
        " so far."));
      var leftPct = Math.max(10, Math.min(90, point.x / m.w * 100));
      var topPct = point.y / m.h * 100;
      tooltip.style.left = leftPct + "%";
      tooltip.style.top = topPct + "%";
      tooltip.style.transform = topPct < 32
        ? "translate(-50%, 14px)"
        : "translate(-50%, calc(-100% - 14px))";
      tooltip.style.visibility = "visible";
    }

    function hide() {
      crosshair.style.visibility = "hidden";
      ring.style.visibility = "hidden";
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

    function release(reason) {
      var wasPinned = pinned;
      hide();
      if (wasPinned) { traceSelection("release", reason, plotted.length); }
    }

    // Anything other than another tap on the plot clears a pinned selection.
    function dismiss(event) {
      if (event && event.type === "pointerdown" &&
          event.target === overlay && !isHoverPointer(event)) {
        traceSelection("keep", "retarget-on-plot", plotted.length);
        return;
      }
      release("page-" + eventKind(event));
    }

    function pin(event, kind) {
      pinOrigin = { x: event.clientX, y: event.clientY };
      if (pinned) { return; }
      pinned = true;
      document.addEventListener("pointerdown", dismiss, true);
      document.addEventListener("wheel", dismiss, true);
      document.addEventListener("keydown", dismiss, true);
      window.addEventListener("blur", dismiss);
      traceSelection("pin", kind, plotted.length);
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

    // Resolves the dot a pointer is over. When there is nothing to show,
    // reason names why with a fixed string.
    function resolve(event) {
      if (!plotted.length) { return { point: null, reason: "no-runs" }; }
      var at = pointFromEvent(event);
      if (!at) { return { point: null, reason: "unsized-canvas" }; }
      return { point: nearest(at.x, at.y), reason: "ok" };
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
      var hovered = resolve(event);
      if (hovered.point) { show(hovered.point); }
    });
    overlay.addEventListener("pointerleave", function (event) {
      if (isHoverPointer(event)) { release("pointer-leave"); }
    });
    // Touch and pen select by tapping: the nearest dot opens and stays up
    // until the next interaction, and a tap on another dot moves it there.
    overlay.addEventListener("pointerdown", function (event) {
      if (isHoverPointer(event)) { return; }
      var kind = pointerKind(event);
      var tapped = resolve(event);
      if (!tapped.point) {
        traceSelection("skip", tapped.reason, plotted.length);
        release("skipped-tap");
        return;
      }
      var moved = pinned;
      pin(event, kind);
      show(tapped.point);
      traceSelection(moved ? "move" : "open", kind, tapped.point.runs.length);
    });
    overlay.addEventListener("pointercancel", function (event) {
      if (!isHoverPointer(event)) { release("pointer-cancel"); }
    });

    // Lets a re-render drop this canvas's page-level listeners with it.
    return function () { release("redraw"); };
  }

  function renderTiles() {
    var data = state.data;
    document.getElementById("tile-runs").textContent =
      data ? formatCount(data.runs) : "\\u2014";
    document.getElementById("tile-time").textContent =
      data ? formatDuration(data.minutes) : "\\u2014";
    document.getElementById("tile-participants").textContent =
      data ? formatCount(data.participants) : "\\u2014";
    var commanders = (data && data.top_commanders) || [];
    document.getElementById("tile-commander").textContent =
      commanders.length ? commanders.join(", ") : "\\u2014";
    var note = "";
    if (commanders.length) {
      note = formatCount(data.top_commander_runs) + " " +
        runsWord(data.top_commander_runs) +
        (commanders.length > 1 ? " each" : "");
    } else if (data) {
      note = "No runs in this period";
    }
    document.getElementById("tile-commander-note").textContent = note;
  }

  // One paginated table. Each column names its heading, the cell it builds
  // for a row, and an optional class that also lets the phone layout hide
  // it. Cells are built from text nodes only.
  function renderTable(key, boxId, pagerId, rows, columns, emptyText, noun) {
    var box = document.getElementById(boxId);
    var pager = document.getElementById(pagerId);
    box.replaceChildren();
    pager.replaceChildren();
    if (!rows.length) {
      box.appendChild(el("div", "empty", emptyText));
      return;
    }
    var pageCount = Math.ceil(rows.length / TABLE_PAGE_SIZE);
    if (state.pages[key] > pageCount - 1) { state.pages[key] = pageCount - 1; }
    var start = state.pages[key] * TABLE_PAGE_SIZE;

    var table = el("table", "stats");
    var head = el("tr");
    columns.forEach(function (column) {
      head.appendChild(el("th", column.className || null, column.heading));
    });
    table.appendChild(head);
    rows.slice(start, start + TABLE_PAGE_SIZE).forEach(function (row) {
      var line = el("tr");
      columns.forEach(function (column) {
        var cell = el("td", column.className || null);
        column.fill(cell, row);
        line.appendChild(cell);
      });
      table.appendChild(line);
    });
    box.appendChild(table);
    if (pageCount < 2) { return; }

    function step(delta) {
      state.pages[key] += delta;
      renderTables();
    }
    var prev = el("button", null, "Prev");
    prev.type = "button";
    prev.disabled = state.pages[key] <= 0;
    prev.addEventListener("click", function () {
      if (state.pages[key] > 0) { step(-1); }
    });
    var next = el("button", null, "Next");
    next.type = "button";
    next.disabled = state.pages[key] >= pageCount - 1;
    next.addEventListener("click", function () {
      if (state.pages[key] < pageCount - 1) { step(1); }
    });
    pager.appendChild(prev);
    pager.appendChild(next);
    pager.appendChild(el("span", null,
      "Page " + (state.pages[key] + 1) + " of " + pageCount +
      " (" + rows.length + " " + noun + ")"));
  }

  function text(value) {
    return function (cell, row) { cell.textContent = value(row); };
  }

  // The event's title with its category beneath it in the muted ink.
  function eventCell(cell, row) {
    cell.appendChild(document.createTextNode(row.title));
    cell.appendChild(el("div", "sub", row.category));
  }

  var EVENT_COLUMN = { heading: "Event", className: "name", fill: eventCell };
  var COMMANDER_COLUMN = {
    heading: "Commander",
    className: "name",
    fill: text(function (row) { return row.commander; })
  };
  var RUNS_COLUMN = {
    heading: "Runs",
    className: "num",
    fill: text(function (row) { return formatCount(row.runs); })
  };
  var LAST_COLUMN = {
    heading: "Last run",
    className: "last",
    fill: text(function (row) { return formatMoment(row.last); })
  };

  function renderTables() {
    var data = state.data || {};
    // A table of what was missing from each run says nothing when there
    // were no runs, so its empty state says that instead of claiming every
    // run had what it asks about.
    var noRuns = !data.runs;
    var NO_RUNS_TEXT = "No event runs ended in this period.";
    renderTable("mentees", "mentees-table", "mentees-pager",
      data.mentees || [],
      [
        {
          heading: "Member",
          className: "name",
          fill: text(function (row) { return row.name; })
        },
        {
          heading: "Asked",
          className: "num",
          fill: text(function (row) { return formatCount(row.asked); })
        },
        {
          heading: "Completed",
          className: "num",
          fill: text(function (row) { return formatCount(row.completed); })
        },
        {
          heading: "All time",
          className: "num",
          fill: function (cell, row) {
            // Null means the whole history could not be read, which is not
            // the same as a member who has never held the slot.
            if (row.completed_all_time === null ||
                row.completed_all_time === undefined) {
              cell.classList.add("unknown");
              cell.textContent = "\\u2014";
              return;
            }
            cell.textContent = formatCount(row.completed_all_time);
          }
        }
      ],
      "Nobody asked to be a mentee on a run in this period.",
      "members");
    renderTable("noMentee", "no-mentee-table", "no-mentee-pager",
      data.without_mentee || [],
      [
        EVENT_COLUMN,
        COMMANDER_COLUMN,
        {
          heading: "Mentee slot",
          fill: text(function (row) {
            return row.mentee_enabled ? "Offered" : "Not offered";
          })
        },
        RUNS_COLUMN,
        LAST_COLUMN
      ],
      noRuns ? NO_RUNS_TEXT : "Every run in this period had a mentee.",
      "events");
    renderTable("noRequirements", "no-requirements-table",
      "no-requirements-pager",
      data.without_requirements || [],
      [EVENT_COLUMN, COMMANDER_COLUMN, RUNS_COLUMN, LAST_COLUMN],
      noRuns
        ? NO_RUNS_TEXT
        : "Every run in this period listed its requirements.",
      "events");
  }

  function render() {
    renderTiles();
    renderChart();
    renderTables();
  }

"""
    + range_picker_js("events")
    + """
  function refresh() {
    chartStatus.textContent = "Loading\\u2026";
    fetch("/api/admin/events" + rangeQuery())
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
        state.pages = { mentees: 0, noMentee: 0, noRequirements: 0 };
        render();
      })
      .catch(function (error) {
        // render() runs inside this chain, so a drawing fault lands here and
        // otherwise reads as a failed request with nothing in the console to
        // trace. Only the error's type and message are logged; no request,
        // response or payload is ever passed through.
        console.error(
          "event statistics load failed:",
          error && error.name, error && error.message);
        if (chartStatus.textContent === "Loading\\u2026") {
          chartStatus.textContent = "Could not load the event statistics.";
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
