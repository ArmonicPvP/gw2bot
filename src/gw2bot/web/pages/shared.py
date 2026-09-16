"""The chrome every served page is built from.

The dashboard pages differ in their data and their tables, not in their frame:
they share one stylesheet, one header, and one time-range picker. Those live
here so a change to the frame is one edit rather than four.
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
  --accent: #5865f2;
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
a { color: var(--accent); }
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
button.active { background: var(--accent); border-color: var(--accent); }
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
  color: #fff;
  text-decoration: none;
  padding: 0.6rem 1.4rem;
  border-radius: 8px;
  font-weight: 600;
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
