"""The chrome every served page is built from.

The dashboard pages differ in their data and their tables, not in their frame:
they share one stylesheet, one header, and one time-range picker, and the four
that chart a history share the whole document around what is their own - how
the chart is drawn and read, how a table pages, and how the window is loaded.
Those live here so a change to any of it is one edit rather than four.
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
button.active {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--on-accent);
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

# The controls the feast and profit pages both carry.
UTILITY_STYLE = """
/* Text a screen reader announces and the page does not show. */
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
/* A bare icon that reads as part of its row until a pointer is on it. */
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
/* A table wider than a phone scrolls sideways inside its card rather than
   pushing the page wider. */
.table-scroll { overflow-x: auto; }
"""

# The layout, chart, tooltip and table rules the four charted dashboards share.
# A page appends its own rules after these, so it can still override any of
# them.
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
.card h2 { font-size: 0.95rem; }
/* A chart's heading shares its row with the button that changes how the line
   is drawn. */
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
/* The legend sits under its chart as a centred row of colour swatches. */
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
.chart-svg .series-line { fill: none; stroke-width: 2; }
.chart-svg .series-dot { stroke: var(--panel); stroke-width: 1; }
.chart-svg .overlay { fill: transparent; }
/* A thin, translucent gray line the hover snaps to. */
.chart-svg .crosshair {
  stroke: rgba(128, 128, 128, 0.45);
  stroke-width: 1;
  pointer-events: none;
}
.chart-svg .hover-ring { fill: none; stroke-width: 2; pointer-events: none; }
/* A chart's box is the positioning context for its hover tooltip, which is an
   HTML box overlaid on the SVG so its text wraps and inherits page styling. */
.chart-box { position: relative; }
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
/* A tooltip row: a swatch, what it stands for, and its figure against the
   right edge. The row nearest the pointer is set in bold. */
.chart-tooltip .tip-row { display: flex; align-items: center; gap: 0.4rem; }
.chart-tooltip .tip-row .swatch {
  width: 0.7rem;
  height: 0.7rem;
  border-radius: 50%;
  flex-shrink: 0;
}
.chart-tooltip .tip-row .val {
  margin-left: auto;
  padding-left: 0.75rem;
  font-variant-numeric: tabular-nums;
}
.chart-tooltip .tip-row.em { font-weight: 600; }
.chart-tooltip .tip-note { color: var(--muted); margin-top: 0.25rem; }
/* The line under a chart or a section saying what it is doing: loading,
   empty, or failed. */
.status-line { color: var(--muted); font-size: 0.85rem; padding-top: 0.5rem; }
.empty { color: var(--muted); padding: 0.6rem; }
.pager {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-top: 0.75rem;
  color: var(--muted);
  font-size: 0.85rem;
}
table.data-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
table.data-table th, table.data-table td {
  text-align: left;
  padding: 0.4rem 0.6rem;
  border-bottom: 1px solid var(--border);
}
table.data-table th { color: var(--muted); font-weight: 600; }
/* A Guild Wars 2 account name, an event title or a Discord name can be one
   long word, which would otherwise widen the table past the card it sits in
   and carry the right-hand columns off the page. Breaking inside the word is
   what bounds the column, and `anywhere` rather than `break-word` because
   only `anywhere` also shrinks the column's minimum width - which is the
   width the table lays itself out from, so it is what keeps the table itself
   inside the card. */
table.data-table td.name { overflow-wrap: anywhere; }
table.data-table th.num, table.data-table td.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
@media (max-width: 640px) {
  main { padding: 0.6rem 0.5rem; }
  .card { padding: 0.6rem; }
}
"""

# The rules the gold and roster history pages add: the figure the window
# closed on beside the chart heading, account names in tooltip rows, and the
# line of totals above the table.
HISTORY_STYLE = """
.card h2 .now {
  color: var(--muted);
  font-weight: 400;
  font-size: 0.85rem;
  margin-left: 0.4rem;
}
.chart-tooltip .tip-row .name { overflow-wrap: anywhere; }
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

# The signed-in member's name and the sign-out button that end every header.
# The button is an icon alone on a phone, where the header has no room for the
# word.
SIGNOUT_CONTROLS = """  <span id="whoami"></span>
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
  </form>"""

# The button beside a stock chart's heading that CHART_MODE_JS switches.
CHART_MODE_BUTTON = """      <button type="button" id="chart-mode" class="chart-mode"
        aria-label="Switch to staircase graph" title="Regular line graph">╱</button>"""

# The status line under a dashboard's stock chart, which loadWindow() writes.
CHART_STATUS_LINE = """    <div id="chart-status" class="status-line" role="status"
      aria-live="polite"></div>"""

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


PAGE_JS = """  // What every signed-in page's script opens with: the one breakpoint its
  // layout changes at, the DOM builder its text goes through, its JSON reads,
  // and the reader's name in its header.
  var mobileQuery = window.matchMedia("(max-width: 640px)");
  function isMobile() { return mobileQuery.matches; }

  // An element, with any text it carries set as text rather than parsed as
  // markup.
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  // Reads one of the site's JSON endpoints. A session that has run out is
  // sent back to sign in, and any other refusal rejects, so a caller has one
  // failure path whatever went wrong.
  function fetchJson(url, init) {
    return fetch(url, init).then(function (response) {
      if (response.status === 401) {
        location.href = "/login";
        throw new Error("unauthorized");
      }
      if (!response.ok) { throw new Error("failed"); }
      return response.json();
    });
  }

  // Puts the signed-in member's name in the header. A failed read leaves it
  // blank: the name is a courtesy, and nothing on the page depends on it.
  function showWhoami() {
    fetchJson("/api/me")
      .then(function (payload) {
        document.getElementById("whoami").textContent = payload.name || "";
      })
      .catch(function () {});
  }
"""

DASHBOARD_JS = """  // The chart, table and loading helpers the four charted dashboards share.
  // A page supplies:
  //   CHART_METRICS        its stock chart's proportions, from chartMetrics()
  //   state                from windowState(), with the scale ({ low, high,
  //                        step }) its chart is drawn on
  //   computeScale()       the stock chart's scale for the data on screen
  //   attachHover()        the stock chart's hover, through attachChartHover
  //   render(), refresh()  which draw the page and reload its window
  // and the markup holds the stock chart's #chart box and #chart-status line.
  var SVG_NS = "http://www.w3.org/2000/svg";

  var chart = document.getElementById("chart");
  var chartStatus = document.getElementById("chart-status");

  // The stock chart's proportions: a wide viewBox on desktop, and a taller
  // one on a phone, where the chart scales to the narrow screen and the
  // extra height makes it read large. A page changes only what its own
  // labels or layout need, as mobile and desktop.
  function chartMetrics(mobile, desktop) {
    return {
      mobile: Object.assign({
        w: 480, h: 620, top: 16, right: 14, bottom: 36, left: 40, ticks: 4
      }, mobile),
      desktop: Object.assign({
        w: 960, h: 380, top: 16, right: 16, bottom: 32, left: 40, ticks: 6
      }, desktop)
    };
  }

  // The page's state, with what every dashboard keeps beside its own. No
  // range until the server answers: the first load asks for the window this
  // member last picked rather than naming one over the top of it.
  function windowState(own) {
    return Object.assign({ range: null, data: null, scale: null }, own);
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

  // The metrics for the current breakpoint. A copy, because a page may widen a
  // margin to fit the labels it is about to draw.
  function metrics() {
    return Object.assign(
      {}, isMobile() ? CHART_METRICS.mobile : CHART_METRICS.desktop);
  }
  // The metrics in use, which resetChart() sets before every drawing.
  var M = null;
  function plotW() { return M.w - M.left - M.right; }
  function plotH() { return M.h - M.top - M.bottom; }

  function points() { return (state.data && state.data.points) || []; }

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
  // Where a value falls up the plot on the scale in use, held to its ends.
  function scaleY(value) {
    var scale = state.scale;
    var held = Math.max(scale.low, Math.min(scale.high, value));
    return M.top + (1 - (held - scale.low) / (scale.high - scale.low)) *
      plotH();
  }

  // A gridline step of 1, 2 or 5 times a power of ten for a span to be cut
  // into about target pieces, which is what makes an axis read as round
  // figures rather than arbitrary divisions. Steps under one are allowed,
  // because a rolling average can sit well below a whole feast; a chart of
  // whole things holds its step to one itself.
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

  // Logs a failed read or save by the error's type and message alone. what
  // is a fixed name for the workflow; no request, response or payload is ever
  // passed through.
  function logFailure(what, error) {
    console.error(
      what + " failed:", error && error.name, error && error.message);
  }

  // A pinned touch selection listens on the whole page, so the stock chart's
  // is torn down before another drawing replaces it.
  var detachHover = null;

  // Clears the stock chart for a new drawing, at the current breakpoint's
  // metrics and on a scale for the data now on screen.
  function resetChart() {
    M = metrics();
    state.scale = computeScale();
    if (detachHover) { detachHover(); detachHover = null; }
    chart.replaceChildren();
  }

  // An empty chart in the metrics in use, which the browser scales to the
  // width of its box.
  function chartCanvas(label) {
    return svg("svg", {
      "class": "chart-svg",
      viewBox: "0 0 " + M.w + " " + M.h,
      role: "img",
      "aria-label": label
    });
  }

  // A line through coords, the "x,y" pairs it joins, in stroke.
  function drawLine(canvas, coords, stroke) {
    canvas.appendChild(svg("polyline", {
      "class": "series-line", stroke: stroke, points: coords.join(" ")
    }));
  }
  // A dot at x, y, ringed in the panel colour so it stands off its line.
  function drawDot(canvas, x, y, radius, fill) {
    canvas.appendChild(svg("circle", {
      "class": "series-dot",
      cx: x.toFixed(1),
      cy: y.toFixed(1),
      r: radius,
      fill: fill
    }));
  }

  // Puts a finished stock chart on the page with its hover, and says in the
  // status line when nothing was plotted.
  function showChart(canvas, plotted, emptyText) {
    detachHover = attachHover(canvas, plotted);
    chart.appendChild(canvas);
    chartStatus.textContent = plotted.length ? "" : emptyText;
  }

  // Horizontal gridlines and y labels at every step of scale, the lowest drawn
  // as the axis. yOf places a value and label writes it. The loop counts steps
  // rather than accumulating them, because a fractional step accumulated in
  // floating point drifts off the value its label claims.
  function drawGrid(canvas, scale, yOf, label) {
    var lines = Math.round((scale.high - scale.low) / scale.step);
    for (var line = 0; line <= lines; line += 1) {
      var value = scale.low + scale.step * line;
      var y = yOf(value);
      canvas.appendChild(svg("line", {
        "class": line === 0 ? "axis" : "grid",
        x1: M.left, y1: y, x2: M.left + plotW(), y2: y
      }));
      var yLabel = svg("text", {
        "class": "y-label", x: M.left - 6, y: y + 4
      });
      yLabel.textContent = label(value);
      canvas.appendChild(yLabel);
    }
  }

  function drawLeftAxis(canvas) {
    canvas.appendChild(svg("line", {
      "class": "axis",
      x1: M.left, y1: M.top, x2: M.left, y2: M.top + plotH()
    }));
  }

  // The left axis, and x labels spread evenly across the whole window so the
  // range spans the full width however little happened in it. The outermost
  // labels sit on the plot's own edges, so centring them would hang half of
  // each one off the chart and the browser would clip it; tuckEdges sets them
  // inwards instead - by class, because the stylesheet's text-anchor would
  // win over a presentation attribute.
  function drawTimeAxis(canvas, tuckEdges) {
    drawLeftAxis(canvas);
    for (var tick = 0; tick <= M.ticks; tick += 1) {
      var t = state.data.since +
        (state.data.now - state.data.since) * (tick / M.ticks);
      var xLabel = svg("text", {
        "class": "x-label", x: scaleX(t), y: M.top + plotH() + 18
      });
      if (tuckEdges && tick === 0) { xLabel.classList.add("first"); }
      if (tuckEdges && tick === M.ticks) { xLabel.classList.add("last"); }
      xLabel.textContent = formatTick(t);
      canvas.appendChild(xLabel);
    }
  }

  // A table cell holding one piece of text read off its row.
  function textFill(read) {
    return function (cell, row) { cell.textContent = read(row); };
  }
  var TIME_COLUMN = {
    heading: "Time",
    fill: textFill(function (row) { return formatMoment(row.t); })
  };

  // The Prev and Next buttons under a table, and the line saying where they
  // have got to. go(page) shows another page.
  function renderPager(box, page, count, total, noun, go) {
    var prev = el("button", null, "Prev");
    prev.type = "button";
    prev.disabled = page <= 0;
    prev.addEventListener("click", function () {
      if (page > 0) { go(page - 1); }
    });
    var next = el("button", null, "Next");
    next.type = "button";
    next.disabled = page >= count - 1;
    next.addEventListener("click", function () {
      if (page < count - 1) { go(page + 1); }
    });
    box.appendChild(prev);
    box.appendChild(next);
    box.appendChild(el("span", null,
      "Page " + (page + 1) + " of " + count +
      " (" + total + " " + noun + ")"));
  }

  // One paginated table: a heading row from its columns, the rows of the page
  // asked for, and the pager under them. A column names its heading, the
  // class its cells take, the class its heading takes where that differs
  // (head), and how a cell is filled from a row; cells are built from text
  // nodes only. spec names the box and pager it fills, the rows, the page and
  // how many rows one holds (size), the table's own class, what to say when
  // there are no rows (empty), the noun the pager counts them in, and
  // go(page). rowClass(row), wrap (the class of a box the table scrolls
  // inside) and singlePagePager: false, which leaves a table that fits on one
  // page without a pager, are optional. Returns the page shown: the one asked
  // for, held inside the pages there are.
  function renderPagedTable(spec) {
    spec.box.replaceChildren();
    spec.pager.replaceChildren();
    if (!spec.rows.length) {
      spec.box.appendChild(el("div", "empty", spec.empty));
      return spec.page;
    }
    var count = Math.ceil(spec.rows.length / spec.size);
    var page = Math.min(spec.page, count - 1);
    var start = page * spec.size;
    var table = el("table", "data-table " + spec.className);
    var head = el("tr");
    spec.columns.forEach(function (column) {
      var headClass = column.head === undefined
        ? column.className
        : column.head;
      head.appendChild(el("th", headClass || null, column.heading));
    });
    table.appendChild(head);
    spec.rows.slice(start, start + spec.size).forEach(function (row) {
      var line = el("tr", spec.rowClass ? spec.rowClass(row) : null);
      spec.columns.forEach(function (column) {
        var cell = el("td", column.className || null);
        column.fill(cell, row);
        line.appendChild(cell);
      });
      table.appendChild(line);
    });
    if (spec.wrap) {
      var scroll = el("div", spec.wrap);
      scroll.appendChild(table);
      spec.box.appendChild(scroll);
    } else {
      spec.box.appendChild(table);
    }
    if (count > 1 || spec.singlePagePager !== false) {
      renderPager(
        spec.pager, page, count, spec.rows.length, spec.noun, spec.go);
    }
    return page;
  }

  // Reads the window on screen from path, adopts the window the answer covers,
  // and hands the payload to show. what names the read in the console, and
  // failure is what the status line says when it fails. show draws inside
  // this chain, so a drawing fault lands here too, where it would otherwise
  // read as a failed request with nothing in the console to trace.
  function loadWindow(path, what, failure, show) {
    chartStatus.textContent = "Loading\\u2026";
    fetchJson(path + rangeQuery())
      .then(function (payload) {
        state.data = payload;
        adoptRange(payload.range, payload.since, payload.now);
        show(payload);
      })
      .catch(function (error) {
        logFailure(what + " load", error);
        if (chartStatus.textContent === "Loading\\u2026") {
          chartStatus.textContent = failure;
        }
      });
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
  // It draws with M, the metrics the canvas was drawn with, and traces through
  // traceSelection, which takes a fixed action name, a fixed reason name and a
  // count. The returned function tears the selection down so a redraw cannot
  // leave its page listeners behind.
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

  // Every dot its own column, for a chart whose dots are each a moment of
  // their own.
  function pointColumns(plotted) {
    return plotted.map(function (point) {
      return { t: point.t, x: point.x, points: [point] };
    });
  }

  // Dots that share a moment form one column, so the crosshair snaps to one x
  // and the tooltip lists everything recorded there.
  function columnsByTime(plotted) {
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
    return columns;
  }
"""

# The selection trace every chart's hover reports through. chart_dashboard
# names the page in it.
_CHART_TRACES_JS = """  // Sanitized tracing for the tap selection lifecycle, so a console trace can
  // explain why a selection opened, moved or went away. Every call passes a
  // fixed action name, one of the narrowed reason names, and a count of drawn
  // elements; nothing read off the payload or the reader's gesture reaches
  // the console. debug keeps it out of the default console view.
  function traceSelection(action, reason, count) {
    console.debug("__SUBJECT__ chart selection:", action, reason, count);
  }
"""

# The staircase toggle the feast, gold and roster charts carry, with its trace.
CHART_MODE_JS = """  // Sanitized tracing for the line style: a fixed mode name and a count.
  function traceMode(mode, count) {
    console.debug("__SUBJECT__ chart mode:", mode, count);
  }

  // The button beside the stock chart's heading, which switches the line
  // between joining each reading straight to the next and a staircase that
  // holds each reading until the next one lands. count() is what the trace
  // reports, and redraw() draws the chart again in the new style.
  function bindChartMode(count, redraw) {
    var button = document.getElementById("chart-mode");
    button.addEventListener("click", function () {
      state.staircase = !state.staircase;
      button.textContent = state.staircase ? "⎿" : "╱";
      button.title = state.staircase
        ? "Staircase graph"
        : "Regular line graph";
      button.setAttribute("aria-label", state.staircase
        ? "Switch to regular line graph" : "Switch to staircase graph");
      traceMode(state.staircase ? "staircase" : "regular", count());
      redraw();
    });
  }
"""

# The coin arithmetic the feast and gold pages write amounts with.
COIN_JS = """  // Copper is what the guild log, the API and the ledger all deal in.
  var COPPER_PER_SILVER = 100;
  var COPPER_PER_GOLD = 100 * COPPER_PER_SILVER;

  // A coin amount as the whole gold, silver and copper it is made of.
  function splitCoins(copper) {
    var remaining = Math.max(0, Math.round(copper));
    var goldCoins = Math.floor(remaining / COPPER_PER_GOLD);
    remaining %= COPPER_PER_GOLD;
    return {
      gold: goldCoins,
      silver: Math.floor(remaining / COPPER_PER_SILVER),
      copper: remaining % COPPER_PER_SILVER
    };
  }

  // A coin amount in the profit dashboard's spaced format. Copper is always
  // written, and silver whenever gold is: 1g 0s 5c keeps its silver, and 5c
  // does not gain one.
  function formatCoins(copper) {
    var coins = splitCoins(copper);
    var parts = [];
    if (coins.gold) { parts.push(coins.gold.toLocaleString() + "g"); }
    if (coins.silver || coins.gold) { parts.push(coins.silver + "s"); }
    parts.push(coins.copper + "c");
    return parts.join(" ");
  }
"""

# What the gold and roster history pages share beyond the other dashboards:
# the boxes history_main lays out, the legend, the totals line, the figure the
# window closed on, the account column, the table, and how the page is loaded
# and drawn. A page supplies TABLE_PAGE_SIZE, and renderLegend, renderTotals,
# renderChart and renderTable.
HISTORY_JS = """  var legend = document.getElementById("legend");
  var totals = document.getElementById("totals");
  var tableBox = document.getElementById("table");
  var pager = document.getElementById("pager");

  // A history legend: one swatch per kind of dot, each named, because there
  // are only a few kinds and they are fixed.
  function renderKeyLegend(entries) {
    legend.replaceChildren();
    entries.forEach(function (entry) {
      var item = el("span", "item");
      item.setAttribute("role", "listitem");
      var swatch = el("span", "swatch");
      swatch.style.background = entry.color;
      item.appendChild(swatch);
      item.appendChild(el("span", "legend-name", entry.label));
      legend.appendChild(item);
    });
  }

  // The line of totals above a history table: each figure, then what it
  // counts.
  function renderTotalsLine(pairs) {
    totals.replaceChildren();
    pairs.forEach(function (pair) {
      var item = el("span", null);
      item.appendChild(el("span", "num", pair[0]));
      item.appendChild(document.createTextNode(" " + pair[1]));
      totals.appendChild(item);
    });
  }

  // The figure a window closed on, as it reads after the chart's heading. A
  // preset window runs to the moment of the request, so its figure is the one
  // now; a picked one can close months ago, where the figure is true only of
  // the window's own end.
  function closingFigure(text) {
    return "\\u2014 " + text +
      (state.range === "custom" ? " at the end" : " now");
  }

  var ACCOUNT_COLUMN = {
    heading: "Account",
    head: null,
    className: "name",
    fill: textFill(function (row) { return row.name; })
  };

  // The table of changes under the chart, paged by state.tablePage.
  function renderHistoryTable(rows, columns, empty, noun) {
    state.tablePage = renderPagedTable({
      box: tableBox,
      pager: pager,
      rows: rows,
      page: state.tablePage,
      size: TABLE_PAGE_SIZE,
      className: "changes",
      columns: columns,
      empty: empty,
      noun: noun,
      go: function (page) {
        state.tablePage = page;
        renderTable();
      }
    });
  }

  // Reloads the window from path, back on the table's first page.
  function loadHistory(path, what, failure) {
    loadWindow(path, what, failure, function () {
      state.tablePage = 0;
      render();
    });
  }

  function render() {
    renderLegend();
    renderTotals();
    renderChart();
    renderTable();
  }
"""

_DASHBOARD_START_JS = """
  // Redraw when the breakpoint flips so the chart adopts the layout for the
  // new width.
  mobileQuery.addEventListener("change", function () {
    if (state.data) { render(); }
  });

  showWhoami();
  syncRangeButtons();
  refresh();
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


def document_head(title: str) -> str:
    """The opening every served document shares, up to its open ``<style>``.

    ``title`` is a fixed string a page module chooses, never anything a request
    carried, so it is written in as it is.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>{title}</title>
<style>"""


def history_main(
    chart_heading: str,
    figure_id: str,
    legend_label: str,
    table_heading: str,
    *,
    after: str = "",
) -> str:
    """The two cards a history page opens with.

    The first holds the chart, with the figure the window closed on beside its
    heading, the staircase toggle, the legend and the status line; the second
    holds the table of changes with its totals and pager. ``after`` is any
    further card the page adds below them.
    """
    return f"""<main>
  <section class="card">
    <div class="chart-heading">
      <h2>{chart_heading}<span id="{figure_id}" class="now"></span></h2>
{CHART_MODE_BUTTON}
    </div>
    <div id="chart" class="chart-box"></div>
    <div id="legend" class="legend" role="list"
      aria-label="{legend_label}"></div>
{CHART_STATUS_LINE}
  </section>
  <section class="card">
    <h2>{table_heading}</h2>
    <div id="totals" class="totals"></div>
    <div id="table"></div>
    <div id="pager" class="pager"></div>
  </section>
{after}</main>
"""


def chart_dashboard(
    title: str,
    subject: str,
    *,
    style: str,
    body: str,
    script: str,
    parts: tuple[str, ...] = (),
    start: str = "",
) -> str:
    """One of the four charted dashboards, around what is its own.

    The document, the header with its range picker, and the script's shared
    opening and closing are the same on all four, so a page supplies its own
    rules (``style``, after the shared ones so it can override them), the
    markup between the header and the script (``body``), and its own script
    (``script``, which runs after the shared helpers and the range picker).
    ``parts`` are the optional shared scripts it draws on, such as
    ``COIN_JS``, and ``start`` runs once its first load has been asked for.
    ``title`` is both the document's title and the header's, and ``subject``
    names the page in its console traces and nothing else.
    """
    traced = (_CHART_TRACES_JS + "".join(parts)).replace("__SUBJECT__", subject)
    return (
        document_head(title)
        + SHARED_STYLE
        + DASHBOARD_HEADER_STYLE
        + RANGE_PICKER_STYLE
        + CHART_PAGE_STYLE
        + style
        + "</style>\n</head>\n<body>\n<header>\n"
        + f'  <h1 id="brand">{title}</h1>\n'
        + RANGE_PICKER_NAV
        + '\n  <span class="spacer"></span>\n'
        + SIGNOUT_CONTROLS
        + "\n"
        + CUSTOM_RANGE_PANEL
        + "\n</header>\n"
        + body
        + '<script>\n"use strict";\n(function () {\n'
        + PAGE_JS
        + DASHBOARD_JS
        + CHART_HOVER_JS
        + traced
        + range_picker_js(subject)
        + script
        + RANGE_PICKER_LISTENERS_JS
        + _DASHBOARD_START_JS
        + start
        + "})();\n</script>\n</body>\n</html>\n"
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
    return document_head(title) + f"""{SHARED_STYLE}{_SIMPLE_PAGE_STYLE}</style>
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
