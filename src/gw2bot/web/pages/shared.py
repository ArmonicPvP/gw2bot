"""The chrome every served page is built from.

The dashboard pages differ in their data and their tables, not in their frame:
they share one stylesheet, one header, and one time-range picker, and the four
that chart a history share how the chart is drawn and read. Those live here so
a change to the frame is one edit rather than four.
"""

from __future__ import annotations

SHARED_STYLE = """
:root {
  --bg: #1e2124;
  --panel: #282b30;
  --panel-2: #2f3338;
  --border: #3d4249;
  --text: #e8eaed;
  --muted: #9aa0a6;
  /* The purple is spelled twice because no one shade does both jobs on
     these panels. --accent is the fill behind a selected or primary control:
     white on it reads at 4.6:1, and it stands out from the header panel at
     3.1:1.
     Drawn as text or a thin line it drops to 2.8:1 on --panel-2, so a link,
     today's date, a roster outline or a focus ring takes --accent-on-dark,
     which reads at 4.9:1 or better on every panel. */
  --accent: #964cf0;
  --accent-on-dark: #bc8bec;
  --on-accent: #ffffff;
  /* The brand purple and its light tint. The brand purple sits at 1.1:1
     against --bg, so it can back light text but never be the text or the
     edge itself. */
  --accent-dark: #330d62;
  --accent-light: #edb9ff;
  --open: #2ecc71;
  --ongoing: #f1c40f;
  --full: #e74c3c;
  --over: #6b7178;
  /* Discord's over-embed color; too dark for the badge, so the badge
     keeps the lighter --over above. */
  --over-embed: #31373d;
  --scheduled: #7289da;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: system-ui, "Segoe UI", sans-serif;
  min-height: 100vh;
}
a { color: var(--accent-on-dark); }
"""

DASHBOARD_HEADER_STYLE = """
header {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
  padding: 0.6rem 1rem;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}
header h1 { font-size: 1.05rem; margin-right: 0.5rem; }
button {
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.35rem 0.7rem;
  font: inherit;
  font-size: 0.85rem;
  cursor: pointer;
}
button:hover { background: var(--border); }
button:focus-visible {
  outline: 2px solid var(--accent-on-dark);
  outline-offset: 1px;
}
.spacer { flex: 1; }
#whoami { color: var(--muted); font-size: 0.85rem; }
header a { font-size: 0.85rem; }
header form { display: flex; }
.signout { display: inline-flex; align-items: center; gap: 0.35rem; }
.signout-icon { display: none; }
.signout-icon, .signout-icon * { pointer-events: none; }
@media (max-width: 640px) {
  header {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    row-gap: 0.4rem;
    column-gap: 0.4rem;
    padding: 0.5rem 0.6rem;
  }
  #brand { grid-column: 2; grid-row: 1; justify-self: center; }
  header form[action="/logout"] {
    grid-column: 3;
    grid-row: 1;
    justify-self: end;
  }
  #whoami { display: none; }
  .signout-icon { display: inline-block; }
  .signout-label { display: none; }
  .signout { padding: 0.35rem 0.5rem; }
}
"""

RANGE_PICKER_STYLE = """
.ranges { display: flex; gap: 0.25rem; }
button:disabled { opacity: 0.4; cursor: default; }
button.active {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--on-accent);
}
/* The date picker is a second header row that stays out of the way until the
   Custom button reveals it, so the preset windows remain one tap apart. */
.custom {
  display: none;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.4rem;
  flex-basis: 100%;
  font-size: 0.85rem;
  color: var(--muted);
}
.custom.open { display: flex; }
.custom input[type="date"] {
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.3rem 0.4rem;
  font: inherit;
  font-size: 0.85rem;
  /* Asks the browser for the dark spelling of its own calendar popup, which
     would otherwise open as a white sheet over a dark page. */
  color-scheme: dark;
}
.custom .custom-error { color: var(--full); }
@media (max-width: 640px) {
  .ranges { grid-column: 1 / -1; grid-row: 2; justify-self: center; }
  .custom { grid-column: 1 / -1; grid-row: 3; justify-content: center; }
}
"""

# The layout, axis and hover rules the four charted dashboards share. A page
# appends its own rules after these, so it can still override any of them.
CHART_PAGE_STYLE = """
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
/* Each chart is a fixed-viewBox SVG that scales to its container width, so
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
.chart-svg .overlay { fill: transparent; }
/* A thin, translucent gray line the hover snaps to. */
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
#chart-status { color: var(--muted); font-size: 0.85rem; padding-top: 0.5rem; }
.empty { color: var(--muted); padding: 0.6rem; }
.pager {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-top: 0.75rem;
  color: var(--muted);
  font-size: 0.85rem;
}
"""

# The three preset buttons and the Custom one that reveals the date fields.
# Every dashboard carries the same four, so a reader who learns one header has
# learned all of them.
RANGE_PICKER_NAV = """  <nav class="ranges" aria-label="Time range">
    <button type="button" data-range="24h">24h</button>
    <button type="button" data-range="7d">7d</button>
    <button type="button" data-range="30d">30d</button>
    <button type="button" data-range="custom">Custom</button>
  </nav>"""

CUSTOM_RANGE_PANEL = """  <div id="custom-range" class="custom">
    <label for="custom-start">From</label>
    <input type="date" id="custom-start">
    <label for="custom-end">To</label>
    <input type="date" id="custom-end">
    <button type="button" id="custom-apply">Apply</button>
    <span id="custom-error" class="custom-error" role="status"
      aria-live="polite"></span>
  </div>"""

# The longest custom window the history dashboards will serve, mirrored from
# the server so a range too wide to draw is named as such instead of coming
# back as a failed load.
MAX_CUSTOM_DAYS = 366

_LOCAL_DAY_JS = """
  // A local calendar day in the spelling a date input reads and writes, the
  // midnight that opens one, and the midnight that opens the next. Days are
  // 23 or 25 hours long where clocks change, so the day after is built from
  // its own parts rather than added on in seconds.
  function dayValue(date) {
    return date.getFullYear() + "-" +
      String(date.getMonth() + 1).padStart(2, "0") + "-" +
      String(date.getDate()).padStart(2, "0");
  }

  function dayStart(year, month, day) {
    return new Date(year, month - 1, day);
  }

  function nextDay(date) {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate() + 1);
  }
"""

_UTC_DAY_JS = """
  // The same three, kept in UTC. The profit report groups every table and
  // chart by UTC sale date, so a date picked here is the date those rows are
  // grouped by rather than the reader's own local day, which would start and
  // end hours away from the buckets on screen.
  function dayValue(date) {
    return date.getUTCFullYear() + "-" +
      String(date.getUTCMonth() + 1).padStart(2, "0") + "-" +
      String(date.getUTCDate()).padStart(2, "0");
  }

  function dayStart(year, month, day) {
    return new Date(Date.UTC(year, month - 1, day));
  }

  function nextDay(date) {
    return new Date(Date.UTC(
      date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate() + 1));
  }
"""

_RANGE_PICKER_JS = """  // The longest custom window the server will serve, mirrored here so a
  // range too wide to draw is named as such instead of coming back as a
  // failed load.
  var MAX_CUSTOM_DAYS = __MAX_CUSTOM_DAYS__;

  // The window a pair of applied dates asks for, as whole epoch seconds, or
  // null while the reader is still on one of the presets.
  var customWindow = null;

  // The preset buttons, by the name the server knows each of them by.
  var PRESET_RANGES = ["24h", "7d", "30d"];

  var customPanel = document.getElementById("custom-range");
  var customStart = document.getElementById("custom-start");
  var customEnd = document.getElementById("custom-end");
  var customError = document.getElementById("custom-error");
__DAY_FUNCTIONS__
  // Reads one date input as a calendar day. A day that does not exist - the
  // 31st of a 30-day month, typed into the field - rolls into the month after
  // it, and spelling the parsed day back out is what catches that rather than
  // silently drawing a window nobody asked for.
  function parseDay(value) {
    var parts = /^(\\d{4})-(\\d{2})-(\\d{2})$/.exec(value || "");
    if (!parts) { return null; }
    var date = dayStart(
      Number(parts[1]), Number(parts[2]), Number(parts[3]));
    return dayValue(date) === value ? date : null;
  }

  // The window the two fields describe, or the reason it cannot be drawn. A
  // picked pair covers whole days: it opens at midnight on the first and
  // closes at the last second of the second, so picking one day twice is that
  // whole day rather than an empty instant.
  // Each refusal carries a fixed reason name beside the sentence the reader
  // sees, because the sentence is prose meant for them and the name is what
  // the console trace is allowed to say about their dates.
  function pickedWindow() {
    var from = parseDay(customStart.value);
    var to = parseDay(customEnd.value);
    if (!from || !to) {
      return {
        reason: "no-dates", error: "Pick a start and an end date."
      };
    }
    var since = Math.floor(from.getTime() / 1000);
    var until = Math.floor(nextDay(to).getTime() / 1000) - 1;
    if (until <= since) {
      return {
        reason: "backwards",
        error: "The end date is before the start date."
      };
    }
    if (until - since > MAX_CUSTOM_DAYS * 86400) {
      return {
        reason: "too-wide",
        error: "Pick a range of " + MAX_CUSTOM_DAYS + " days or fewer."
      };
    }
    if (since > Math.floor(Date.now() / 1000)) {
      return {
        reason: "future-start", error: "The start date is in the future."
      };
    }
    return { since: since, until: until };
  }

  // Opening the picker for the first time fills it with the whole days the
  // window on screen falls inside, which is the closest a pair of dates can
  // come to the range already drawn: the fields hold days and nothing finer,
  // so a rolling preset cannot be reproduced exactly. Applying an untouched
  // 24h default therefore asks for yesterday from midnight rather than this
  // time yesterday, and reads a few hours wider than the button it came from.
  // Wider is the right way to miss: the narrower pair would drop hours the
  // reader can already see.
  function fillCustomDefaults() {
    if (customStart.value && customEnd.value) { return; }
    var today = new Date();
    var span = windowSpan() || 24 * 60 * 60;
    customStart.value = dayValue(new Date(today.getTime() - span * 1000));
    customEnd.value = dayValue(today);
  }

  function toggleCustomPanel(open) {
    customPanel.classList.toggle("open", open);
    if (!open) { return; }
    fillCustomDefaults();
    // Nothing has been recorded for a day that has not happened, so neither
    // field offers one.
    customStart.max = dayValue(new Date());
    customEnd.max = customStart.max;
  }

  // Sanitized tracing for the range picker, so a console trace can explain
  // why a picked window did or did not become a request. Only a fixed action
  // name, one of the fixed reason names above, and a count of days are
  // passed; the dates the reader entered never reach the console.
  function traceRange(action, reason, days) {
    console.debug("__SUBJECT__ chart range:", action, reason, days);
  }

  function applyCustomRange() {
    var picked = pickedWindow();
    if (picked.error) {
      // The refusal ends the workflow here, without a request, so this is the
      // only place a trace can say the reader asked for a window and did not
      // get one.
      traceRange("refuse", picked.reason, 0);
      customError.textContent = picked.error;
      return;
    }
    customError.textContent = "";
    customWindow = picked;
    state.range = "custom";
    traceRange("apply", "ok", Math.round((picked.until - picked.since) / 86400));
    syncRangeButtons();
    refresh();
  }

  // The query the current selection asks for: a preset window by name, the
  // applied pair of epoch seconds, or nothing at all. Nothing is what the
  // first load sends, and it is how the page asks for the window this member
  // last picked instead of naming one over the top of it.
  function rangeQuery() {
    if (state.range === null) { return ""; }
    if (state.range === "custom" && customWindow) {
      return "?range=custom&start=" +
        encodeURIComponent(String(customWindow.since)) +
        "&end=" + encodeURIComponent(String(customWindow.until));
    }
    return "?range=" + encodeURIComponent(state.range);
  }

  // Take the window the server served. The first load names none, so what
  // comes back is the window this member last picked - or the default, when
  // they never have - and the header follows it rather than the other way
  // round. A later answer is one the page asked for by name, so there is
  // nothing to adopt and the reader's own dates are left alone.
  function adoptRange(key, since, until) {
    if (state.range !== null) { return; }
    state.range = key;
    if (key === "custom") {
      customWindow = { since: since, until: until };
      customStart.value = dayValue(new Date(since * 1000));
      customEnd.value = dayValue(new Date(until * 1000));
      // The dates are the whole of what "Custom" means, so a window reopened
      // on a pair opens the panel holding them too.
      toggleCustomPanel(true);
    }
    traceRange(
      "reopen", key === "custom" ? "dates" : "preset",
      Math.round((until - since) / 86400));
    syncRangeButtons();
  }

  function syncRangeButtons() {
    document.querySelectorAll("[data-range]").forEach(function (button) {
      var active = button.getAttribute("data-range") === state.range;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }
"""

RANGE_PICKER_LISTENERS_JS = """  document.querySelectorAll("[data-range]").forEach(function (button) {
    button.addEventListener("click", function () {
      var picked = button.getAttribute("data-range");
      if (picked === "custom") {
        // The Custom button only reveals the picker; the range itself does not
        // move until a pair of dates is applied, so a stray tap costs nothing.
        toggleCustomPanel(!customPanel.classList.contains("open"));
        return;
      }
      toggleCustomPanel(false);
      state.range = picked;
      syncRangeButtons();
      refresh();
    });
  });
  document.getElementById("custom-apply").addEventListener(
    "click", applyCustomRange);
  [customStart, customEnd].forEach(function (input) {
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        applyCustomRange();
      }
    });
  });
"""


CHART_HELPERS_JS = """  // The DOM, axis and pointer helpers every charted dashboard draws with.
  // A page supplies ``state.data`` with ``since`` and ``now`` in seconds, and
  // ``M`` and ``plotW()``, the metrics and width the plot is drawn at.
  var SVG_NS = "http://www.w3.org/2000/svg";

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

  // How wide the drawn window is, in seconds.
  function windowSpan() {
    return state.data ? state.data.now - state.data.since : 0;
  }

  // Where a moment falls across the plot, held to the drawn window's edges.
  function scaleX(t) {
    var since = state.data.since;
    var now = state.data.now;
    var span = now - since;
    var frac = span > 0 ? (t - since) / span : 0;
    if (frac < 0) { frac = 0; }
    if (frac > 1) { frac = 1; }
    return M.left + frac * plotW();
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
  // they are traced, so an exotic value cannot ride into the console. scroll
  // and resize only ever come from the roster page's invite tip, which closes
  // on them; the charts never listen for either.
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
"""

CHART_HOVER_JS = """  // The hover and tap selection every dashboard chart carries. A page groups
  // its plotted dots into columns, { t, x, points: [{ x, y, ... }] }, and
  // supplies the parts that differ in options:
  //   host          the element the tooltip is laid over
  //   emptyReason   the fixed reason traced when there is nothing to select
  //   ringColor     function (point) giving a hover ring's stroke
  //   fillTooltip   function (tooltip, column, emphasized) adding the rows
  //                 below the tooltip's time line
  //   headline      function (t) for that time line; formatMoment otherwise
  //   ringRadius    the hover ring's radius; 7 otherwise
  //   nearestByX    pick the column nearest in x alone rather than the dot
  //                 nearest in two dimensions
  // The page also supplies M, the metrics the canvas was drawn with, and its
  // own traceSelection, which takes a fixed action name, a fixed reason name
  // and a count. The returned function tears the selection down so a redraw
  // cannot leave its page listeners behind.
  function attachChartHover(canvas, columns, options) {
    var head = options.headline || formatMoment;
    var ringRadius = options.ringRadius || 7;
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
    options.host.appendChild(tooltip);

    // By default this picks from the actual dots in two dimensions, then
    // returns their time column: close timestamps can occupy nearly the same
    // x coordinate, so x-only navigation would make their different y
    // positions impossible to use. The feast page's charts, which have always
    // been read by time alone, ask for x instead.
    function nearestColumn(vbX, vbY) {
      var best = null;
      var bestDist = Infinity;
      columns.forEach(function (column) {
        if (options.nearestByX) {
          var gap = Math.abs(column.x - vbX);
          if (gap < bestDist) { bestDist = gap; best = column; }
          return;
        }
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
      tooltip.appendChild(el("div", "tip-time", head(column.t)));
      options.fillTooltip(tooltip, column, emphasized);
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
          r: ringRadius,
          stroke: options.ringColor(point)
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
      if (!columns.length) {
        return { column: null, reason: options.emptyReason };
      }
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
"""


def range_picker_js(
    subject: str,
    *,
    max_custom_days: int = MAX_CUSTOM_DAYS,
    utc_days: bool = False,
) -> str:
    """The range picker every dashboard header carries.

    The four pages differ in what they draw, not in how a window is picked, so
    the picker is written once here: the preset buttons, the pair of date
    fields behind Custom, the window the page reopens on, and the sanitized
    tracing around all of it. A page supplies ``state.range``, a
    ``windowSpan()`` in seconds, and a ``refresh()`` that reloads it.

    ``subject`` names the page in its console traces and nothing else.
    ``utc_days`` picks the calendar the date fields work in: the history
    dashboards chart local time, and the profit report buckets by UTC date.
    """
    return (
        _RANGE_PICKER_JS.replace("__SUBJECT__", subject)
        .replace("__MAX_CUSTOM_DAYS__", str(max_custom_days))
        .replace("__DAY_FUNCTIONS__", _UTC_DAY_JS if utc_days else _LOCAL_DAY_JS)
    )


_SIMPLE_PAGE_STYLE = """
body { display: flex; align-items: center; justify-content: center; }
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 2.5rem 3rem;
  text-align: center;
  max-width: 26rem;
}
.card h1 { font-size: 1.3rem; margin-bottom: 0.75rem; }
.card p { color: var(--muted); margin-bottom: 1.5rem; }
.button {
  display: inline-block;
  background: var(--accent);
  color: var(--on-accent);
  text-decoration: none;
  padding: 0.6rem 1.4rem;
  border-radius: 8px;
  font-weight: 600;
}
.button:focus-visible {
  outline: 2px solid var(--accent-on-dark);
  outline-offset: 2px;
}
"""


def simple_page(title: str, heading: str, body: str, action: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>{title}</title>
<style>{SHARED_STYLE}{_SIMPLE_PAGE_STYLE}</style>
</head>
<body>
<div class="card">
<h1>{heading}</h1>
<p>{body}</p>
{action}
</div>
</body>
</html>
"""
