"""HTML documents served by the web calendar.

Documents are fixed strings except for the sign-in page's escaped, validated
local login URL. Dynamic data reaches dashboard pages only through JSON APIs
and is inserted with ``textContent`` on the client; event descriptions
additionally pass through a small client-side Discord-markdown renderer that
only ever builds DOM nodes and text nodes, never HTML strings.
"""

from __future__ import annotations

from html import escape

_SHARED_STYLE = """
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

_DASHBOARD_HEADER_STYLE = """
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


def _simple_page(title: str, heading: str, body: str, action: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>{title}</title>
<style>{_SHARED_STYLE}{_SIMPLE_PAGE_STYLE}</style>
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


_SIGN_IN_ACTION = '<a class="button" href="/login">Sign in with Discord</a>'


def sign_in_page(login_url: str = "/login") -> str:
    action = (
        '<a class="button" href="'
        + escape(login_url, quote=True)
        + '">Sign in with Discord</a>'
    )
    return _simple_page(
        "Guild Events",
        "Guild Events Calendar",
        "Sign in with Discord to view the guild event calendar.",
        action,
    )


SIGN_IN_PAGE = sign_in_page()

SIGNED_OUT_PAGE = _simple_page(
    "Signed out",
    "You are signed out",
    "Sign back in with Discord to view the guild event calendar.",
    _SIGN_IN_ACTION,
)

MEMBERS_ONLY_PAGE = _simple_page(
    "Members only",
    "Members only",
    "This calendar is only available to members of the Discord server.",
    _SIGN_IN_ACTION,
)

LOGIN_FAILED_PAGE = _simple_page(
    "Sign-in failed",
    "Sign-in failed",
    "The Discord sign-in could not be completed. Please try again.",
    _SIGN_IN_ACTION,
)

SERVICE_UNAVAILABLE_PAGE = _simple_page(
    "Temporarily unavailable",
    "Temporarily unavailable",
    "The calendar cannot reach Discord right now. Please try again in a "
    "moment.",
    _SIGN_IN_ACTION,
)

OFFICER_ONLY_PAGE = _simple_page(
    "Officers only",
    "Officers only",
    "The feast usage dashboard is only available to raffle officers.",
    '<a class="button" href="/">Back to the calendar</a>',
)

ROSTER_OFFICER_ONLY_PAGE = _simple_page(
    "Officers only",
    "Officers only",
    "The guild roster history is only available to raffle officers.",
    '<a class="button" href="/">Back to the calendar</a>',
)

GOLD_OFFICER_ONLY_PAGE = _simple_page(
    "Officers only",
    "Officers only",
    "The guild bank gold history is only available to raffle officers.",
    '<a class="button" href="/">Back to the calendar</a>',
)


CALENDAR_PAGE = (
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Guild Events</title>
<style>"""
    + _SHARED_STYLE
    + _DASHBOARD_HEADER_STYLE
    + """
body {
  display: flex;
  flex-direction: column;
  height: 100vh;
  /* The layout is a fixed-height app: the header is pinned and only <main>
     scrolls, so the page itself must never grow a scrollbar of its own. */
  overflow: hidden;
}
.controls, .views { display: flex; gap: 0.25rem; }
button.active { background: var(--accent); border-color: var(--accent); }
#period { font-weight: 600; font-size: 0.95rem; min-width: 11rem; }
main {
  /* min-height:0 lets this flex child shrink to the viewport so its own
     overflow scrolls, instead of pushing the page past 100vh. A column flex
     box so the month grid can flex to leave room for the status line. */
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 0 1rem;
  display: flex;
  flex-direction: column;
}
#grid { display: grid; }
#grid.month {
  gap: 4px;
  /* Take the space left after the status line instead of a fixed height, so
     the grid and the status line together never spill past main. */
  flex: 1;
  margin: 0.75rem 0;
  min-height: 24rem;
  grid-template-columns: repeat(7, minmax(6rem, 1fr));
  grid-template-rows: auto repeat(6, minmax(5.5rem, 1fr));
}
/* Day and week are time grids: an hour gutter down the left, one column per
   day, and every event positioned and sized from its own start and duration.
   --hour-h is the height of one hour; the script converts minutes to pixels
   against it, so the two must stay in step. */
#grid.timegrid {
  --hour-h: 48px;
  --gutter: 3.75rem;
  grid-template-rows: auto 1fr;
  align-content: start;
}
#grid.timegrid.day { grid-template-columns: var(--gutter) 1fr; }
#grid.timegrid.week {
  grid-template-columns: var(--gutter) repeat(7, minmax(4.5rem, 1fr));
}
/* In day view a single column is offset by the hour gutter, which pushes its
   header off-centre. Drop the empty corner and let the header span the whole
   width so the date sits centred over the view. */
#grid.timegrid.day .tg-corner { display: none; }
#grid.timegrid.day .tg-head { grid-column: 1 / -1; }
/* The day headers stay put while the 24-hour body scrolls under them. */
.tg-corner, .tg-head {
  position: sticky;
  top: 0;
  z-index: 3;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  padding: 0.3rem 0.25rem 0.35rem;
  text-align: center;
}
.tg-dow {
  color: var(--muted);
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.tg-daynum { font-size: 1rem; font-weight: 600; }
.tg-head.today .tg-dow, .tg-head.today .tg-daynum { color: var(--accent); }
.tg-head.clickable {
  border: 0;
  border-bottom: 1px solid var(--border);
  border-radius: 0;
  color: inherit;
  font: inherit;
  cursor: pointer;
}
.tg-hour { height: var(--hour-h); border-top: 1px solid var(--border); }
.tg-gutter .tg-hour {
  border-top-color: transparent;
  color: var(--muted);
  font-size: 0.7rem;
  text-align: right;
  padding: 0.1rem 0.4rem 0 0;
  white-space: nowrap;
}
.tg-col {
  position: relative;
  background: var(--panel);
  border-left: 1px solid var(--border);
}
.tg-col:last-child { border-right: 1px solid var(--border); }
.tg-col.today { background: var(--panel-2); }
.chip.tg-ev {
  position: absolute;
  /* A short event has room for only one line. Keep the title beside the time
     so both survive the minimum-height clamp. */
  flex-direction: row;
  align-items: center;
  gap: 0.3rem;
  margin: 0;
  padding: 0.1rem 0.3rem;
  line-height: 1.25;
  z-index: 1;
}
.chip.tg-ev .time { font-size: 0.7rem; }
.chip.tg-ev .name { min-width: 0; max-width: 100%; }
.tg-now {
  position: absolute;
  left: 0;
  right: 0;
  border-top: 2px solid var(--full);
  z-index: 2;
  pointer-events: none;
}
.tg-now::before {
  content: "";
  position: absolute;
  left: -3px;
  top: -4px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--full);
}
.dow {
  text-align: center;
  color: var(--muted);
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  padding: 0.2rem 0;
}
.cell {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.25rem;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  overflow: hidden;
  min-height: 0;
}
.cell.outside { opacity: 0.45; }
.cell.today { border-color: var(--accent); }
.cell-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.25rem;
  min-width: 0;
}
.day-link {
  border: 0;
  border-radius: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  padding: 0;
  cursor: pointer;
}
.daynum {
  font-size: 0.75rem;
  color: var(--muted);
  padding: 0 0.2rem 0.15rem;
}
.more {
  border: 0;
  border-radius: 0;
  background: transparent;
  color: var(--accent);
  font-size: 0.7rem;
  font-weight: 600;
  padding: 0 0.2rem 0.15rem;
  white-space: nowrap;
}
.cell.today .daynum { color: var(--accent); font-weight: 700; }
.cell-events { min-height: 0; overflow: hidden; }
.chip {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  width: 100%;
  text-align: left;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-left-width: 4px;
  border-radius: 5px;
  padding: 0.15rem 0.35rem;
  margin-bottom: 0.2rem;
  font-size: 0.78rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
}
.chip .time { color: var(--muted); flex-shrink: 0; }
.chip .name { overflow: hidden; text-overflow: ellipsis; }
/* The stripe mirrors the Discord embed color for the event's status. */
.chip.st-open { border-left-color: var(--open); }
.chip.st-ongoing { border-left-color: var(--ongoing); }
.chip.st-full { border-left-color: var(--full); }
.chip.st-over { border-left-color: var(--over-embed); }
.chip.st-scheduled { border-left-color: var(--scheduled); }
.chip.over { opacity: 0.45; }
.chip.projected { border-style: dashed; border-left-style: solid; }
#tooltip {
  position: fixed;
  z-index: 10;
  max-width: 22rem;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.6rem 0.75rem;
  font-size: 0.82rem;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.45);
  pointer-events: none;
  display: none;
}
#tooltip h2 { font-size: 0.95rem; margin-bottom: 0.15rem; }
#tooltip .meta { color: var(--muted); margin-bottom: 0.35rem; }
#tooltip .desc { margin-bottom: 0.35rem; white-space: pre-wrap; }
#tooltip .sep { border-top: 1px solid var(--border); margin: 0.45rem 0; }
#tooltip .row { color: var(--text); }
#tooltip .desc code,
#tooltip .desc pre {
  font-family: ui-monospace, Consolas, "Courier New", monospace;
  font-size: 0.78rem;
  background: var(--bg);
  border-radius: 4px;
}
#tooltip .desc code { padding: 0 0.25rem; }
#tooltip .desc pre {
  padding: 0.35rem 0.5rem;
  margin: 0.25rem 0;
  overflow-x: auto;
  white-space: pre-wrap;
}
#tooltip .desc .md-h1 { font-size: 1rem; font-weight: 700; }
#tooltip .desc .md-h2 { font-size: 0.95rem; font-weight: 700; }
#tooltip .desc .md-h3 { font-size: 0.88rem; font-weight: 700; }
#tooltip .desc .md-li { padding-left: 0.9rem; position: relative; }
#tooltip .desc .md-li::before {
  content: "\\2022";
  position: absolute;
  left: 0.25rem;
  color: var(--muted);
}
#tooltip .desc .md-quote {
  border-left: 3px solid var(--border);
  padding-left: 0.5rem;
  color: var(--muted);
}
#tooltip .desc .md-subtext {
  color: var(--muted);
  font-size: 0.74rem;
  line-height: 1.25;
}
#tooltip .desc .md-gap { height: 0.4rem; }
#tooltip .desc .spoiler {
  background: var(--bg);
  border-radius: 3px;
  padding: 0 0.2rem;
}
.badge {
  display: inline-block;
  border-radius: 4px;
  padding: 0 0.35rem;
  font-size: 0.72rem;
  font-weight: 700;
  color: #1b1e21;
  margin-left: 0.35rem;
  vertical-align: 1px;
}
.badge.open { background: var(--open); }
.badge.ongoing { background: var(--ongoing); }
.badge.full { background: var(--full); }
.badge.over { background: var(--over); }
.badge.scheduled { background: var(--scheduled); }
#status { color: var(--muted); font-size: 0.85rem; padding: 0.5rem 0.2rem; }
button:focus-visible, .chip:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
@media (max-width: 640px) {
  /* Two-row header: the title is centred with the sign-out control pinned to
     the right, and the view switch sits on its own row beneath. */
  .views { grid-column: 1 / -1; grid-row: 2; justify-self: center; }
  /* Swiping changes the period with no other cue in the month grid, which
     shows bare day numbers, so the period label keeps the top-left corner in
     a compact form. It must not widen past its column or it would push the
     centred title off centre. */
  #period {
    grid-column: 1;
    grid-row: 1;
    justify-self: start;
    min-width: 0;
    max-width: 100%;
    font-size: 0.78rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  /* Navigation is by swipe on mobile and the username adds nothing on a
     narrow screen, so the stepper and username are dropped. */
  .controls { display: none; }
  main { padding: 0 0.3rem; overflow-x: hidden; }
  #grid.month {
    grid-template-columns: repeat(7, minmax(0, 1fr));
    grid-template-rows: auto repeat(6, minmax(0, 1fr));
    gap: 2px;
    margin: 0.4rem 0;
    min-height: 0;
    /* The whole month fits the viewport, so nothing scrolls. */
    overflow: hidden;
  }
  #grid.timegrid { --gutter: 2.5rem; }
  #grid.timegrid.day {
    grid-template-columns: var(--gutter) minmax(0, 1fr);
  }
  #grid.timegrid.week {
    grid-template-columns: var(--gutter) repeat(3, minmax(0, 1fr));
  }
  .cell { padding: 0.1rem; border-radius: 5px; overflow: hidden; }
  .daynum { font-size: 0.72rem; padding: 0 0.15rem 0.1rem; }
  .dow { font-size: 0.72rem; padding: 0.15rem 0; }
  #grid.month .chip {
    font-size: 0.62rem;
    padding: 0.05rem 0.2rem;
    margin-bottom: 0.1rem;
  }
}
</style>
</head>
<body>
<header>
  <h1 id="brand">Guild Events</h1>
  <nav class="views" aria-label="Calendar view">
    <button type="button" data-view="day">Day</button>
    <button type="button" data-view="week" id="week-view">Week</button>
    <button type="button" data-view="month">Month</button>
  </nav>
  <div class="controls">
    <button type="button" id="prev" aria-label="Previous period">&lsaquo;</button>
    <button type="button" id="today">Today</button>
    <button type="button" id="next" aria-label="Next period">&rsaquo;</button>
  </div>
  <span id="period" aria-live="polite"></span>
  <span class="spacer"></span>
  <a href="/profit">Profit</a>
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
  <div id="grid" class="month" aria-label="Guild event calendar"></div>
  <div id="status" role="status" aria-live="polite"></div>
</main>
<div id="tooltip" role="tooltip"></div>
<script>
"use strict";
(function () {
  var grid = document.getElementById("grid");
  var scroller = document.querySelector("main");
  var tooltip = document.getElementById("tooltip");
  var periodLabel = document.getElementById("period");
  var statusLine = document.getElementById("status");
  var state = { view: "month", anchor: startOfDay(new Date()) };
  var entries = [];
  var pinnedChip = null;
  var tooltipChip = null;
  var collapseFrame = 0;

  // A single breakpoint drives every behavioural difference on small screens:
  // the 3-day week, single-letter month, tap-to-open days and swipe steps.
  var mobileQuery = window.matchMedia("(max-width: 640px)");
  function isMobile() { return mobileQuery.matches; }
  // The week view collapses to three days on mobile so it never scrolls
  // sideways; the step size follows the same span.
  function weekSpan() { return isMobile() ? 3 : 7; }

  function startOfDay(date) {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate());
  }
  function addDays(date, days) {
    return new Date(
      date.getFullYear(), date.getMonth(), date.getDate() + days);
  }
  function startOfWeek(date) {
    return addDays(startOfDay(date), -date.getDay());
  }
  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() &&
      a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }
  function pad(number) {
    return (number < 10 ? "0" : "") + number;
  }
  function isoDate(date) {
    return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" +
      pad(date.getDate());
  }

  function rangeFor() {
    if (state.view === "day") {
      var dayStart = startOfDay(state.anchor);
      return { start: dayStart, end: addDays(dayStart, 1) };
    }
    if (state.view === "week") {
      if (isMobile()) {
        var base = startOfDay(state.anchor);
        return { start: base, end: addDays(base, weekSpan()) };
      }
      var weekStart = startOfWeek(state.anchor);
      return { start: weekStart, end: addDays(weekStart, 7) };
    }
    var first = new Date(
      state.anchor.getFullYear(), state.anchor.getMonth(), 1);
    var gridStart = startOfWeek(first);
    return { start: gridStart, end: addDays(gridStart, 42) };
  }

  function readHash() {
    var match = /^#(day|week|month)\\/(\\d{4})-(\\d{2})(?:-(\\d{2}))?$/
      .exec(location.hash);
    if (!match) { return; }
    state.view = match[1];
    state.anchor = new Date(
      Number(match[2]), Number(match[3]) - 1, Number(match[4] || 1));
  }
  function writeHash() {
    var value = state.view === "month"
      ? state.anchor.getFullYear() + "-" + pad(state.anchor.getMonth() + 1)
      : isoDate(state.anchor);
    var hash = "#" + state.view + "/" + value;
    if (location.hash !== hash) {
      history.replaceState(null, "", hash);
    }
  }

  function step(direction) {
    if (state.view === "day") {
      state.anchor = addDays(state.anchor, direction);
    } else if (state.view === "week") {
      state.anchor = addDays(state.anchor, weekSpan() * direction);
    } else {
      state.anchor = new Date(
        state.anchor.getFullYear(),
        state.anchor.getMonth() + direction,
        1);
    }
    refresh();
  }

  function formatTime(date) {
    return date.toLocaleTimeString(
      undefined, { hour: "numeric", minute: "2-digit" });
  }
  function formatDuration(minutes) {
    var hours = Math.floor(minutes / 60);
    var rest = minutes % 60;
    if (hours && rest) { return hours + "h " + rest + "m"; }
    if (hours) { return hours + "h"; }
    return rest + "m";
  }
  function statusLabel(status) {
    return status.charAt(0).toUpperCase() + status.slice(1);
  }
  var statusClasses = {
    open: "st-open",
    ongoing: "st-ongoing",
    full: "st-full",
    over: "st-over",
    scheduled: "st-scheduled"
  };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  // Renders the Discord markdown subset used in event descriptions by
  // building DOM nodes directly. Event text only ever becomes text nodes,
  // so descriptions cannot inject markup.
  var inlineRules = [
    { re: /^`([^`]+)`/, tag: "code", raw: true },
    { re: /^\\*\\*([\\s\\S]+?)\\*\\*(?!\\*)/, tag: "strong" },
    { re: /^__([\\s\\S]+?)__(?!_)/, tag: "u" },
    { re: /^~~([\\s\\S]+?)~~/, tag: "s" },
    { re: /^\\|\\|([\\s\\S]+?)\\|\\|/, tag: "span", cls: "spoiler" },
    { re: /^\\*([^*\\n]+)\\*/, tag: "em" },
    { re: /^_([^_\\n]+)_/, tag: "em" }
  ];

  function appendInline(parent, text) {
    var plain = "";
    var i = 0;
    while (i < text.length) {
      var matched = null;
      var rest = text.slice(i);
      for (var r = 0; r < inlineRules.length; r += 1) {
        var m = inlineRules[r].re.exec(rest);
        if (m) { matched = { rule: inlineRules[r], groups: m }; break; }
      }
      if (!matched) {
        plain += text.charAt(i);
        i += 1;
        continue;
      }
      if (plain) {
        parent.appendChild(document.createTextNode(plain));
        plain = "";
      }
      var node = el(matched.rule.tag, matched.rule.cls || null);
      if (matched.rule.raw) {
        node.textContent = matched.groups[1];
      } else {
        appendInline(node, matched.groups[1]);
      }
      parent.appendChild(node);
      i += matched.groups[0].length;
    }
    if (plain) { parent.appendChild(document.createTextNode(plain)); }
  }

  function appendMarkdown(parent, text) {
    var lines = text.replace(/\\r\\n/g, "\\n").split("\\n");
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      if (/^\\s*```/.test(line)) {
        var code = [];
        i += 1;
        while (i < lines.length && !/^\\s*```/.test(lines[i])) {
          code.push(lines[i]);
          i += 1;
        }
        i += 1;
        parent.appendChild(el("pre", null, code.join("\\n")));
        continue;
      }
      var heading = /^(#{1,3})\\s+(.*)$/.exec(line);
      var subtext = /^-#\\s+(.*)$/.exec(line);
      var listItem = /^\\s*[-*]\\s+(.*)$/.exec(line);
      var quote = /^>\\s?(.*)$/.exec(line);
      var row;
      if (line.trim() === "") {
        parent.appendChild(el("div", "md-gap"));
      } else if (heading) {
        row = el("div", "md-h" + heading[1].length);
        appendInline(row, heading[2]);
        parent.appendChild(row);
      } else if (subtext) {
        row = el("div", "md-subtext");
        appendInline(row, subtext[1]);
        parent.appendChild(row);
      } else if (listItem) {
        row = el("div", "md-li");
        appendInline(row, listItem[1]);
        parent.appendChild(row);
      } else if (quote) {
        row = el("div", "md-quote");
        appendInline(row, quote[1]);
        parent.appendChild(row);
      } else {
        row = el("div", "md-line");
        appendInline(row, line);
        parent.appendChild(row);
      }
      i += 1;
    }
  }

  function chipFor(entry, index, hideTime) {
    var start = new Date(entry.start_epoch * 1000);
    var chip = el("div",
      "chip " + (statusClasses[entry.status] || "st-scheduled"));
    if (entry.status === "over") { chip.classList.add("over"); }
    if (entry.projected) { chip.classList.add("projected"); }
    chip.setAttribute("data-i", String(index));
    chip.setAttribute("tabindex", "0");
    chip.setAttribute("role", "button");
    chip.setAttribute("aria-haspopup", "true");
    chip.setAttribute("aria-expanded", "false");
    if (!hideTime) {
      chip.appendChild(el("span", "time", formatTime(start)));
    }
    chip.appendChild(el("span", "name", entry.title));
    return chip;
  }

  // Month cells and week headers open the complete day breakdown.
  function openDay(date) {
    state.view = "day";
    state.anchor = startOfDay(date);
    syncViewButtons();
    refresh();
  }

  // On mobile the chips are hidden from assistive tech, so the day button's
  // label includes their titles. A screen-reader user can therefore tell
  // which dates hold events without opening all 42 cells.
  function monthCellLabel(date, dayEntries) {
    var dateName = date.toLocaleDateString(
      undefined, { weekday: "long", month: "long", day: "numeric" });
    if (dayEntries.length === 0) {
      return dateName + ", no events";
    }
    var count = dayEntries.length === 1
      ? "1 event"
      : dayEntries.length + " events";
    var titles = dayEntries.map(function (entry) { return entry.title; });
    return dateName + ", " + count + ": " + titles.join(", ");
  }

  function buildCell(date, monthIndex) {
    var cell = el("div", "cell");
    if (date.getMonth() !== monthIndex) { cell.classList.add("outside"); }
    if (sameDay(date, new Date())) { cell.classList.add("today"); }
    var mobile = isMobile();
    var next = addDays(date, 1);
    var dayEntries = entries.filter(function (entry) {
      var start = new Date(entry.start_epoch * 1000);
      return start >= date && start < next;
    });
    var target = date;
    var cellHead = el("div", "cell-head");
    var dayLink = el("button", "day-link");
    dayLink.type = "button";
    dayLink.setAttribute("aria-label", "Open " + monthCellLabel(
      date, dayEntries));
    dayLink.appendChild(el("span", "daynum", String(date.getDate())));
    dayLink.addEventListener("click", function () { openDay(target); });
    cellHead.appendChild(dayLink);
    var more = el("button", "more");
    more.type = "button";
    more.hidden = true;
    more.addEventListener("click", function () { openDay(target); });
    cellHead.appendChild(more);
    cell.appendChild(cellHead);
    var eventList = el("div", "cell-events");
    entries.forEach(function (entry, index) {
      var start = new Date(entry.start_epoch * 1000);
      if (start >= date && start < next) {
        var chip = chipFor(entry, index, mobile);
        // The date button's label already names these events on mobile, so the
        // chip is neither a focus stop nor a separate node exposed to
        // assistive tech there.
        if (mobile) {
          chip.removeAttribute("tabindex");
          chip.setAttribute("aria-hidden", "true");
        }
        eventList.appendChild(chip);
      }
    });
    cell.appendChild(eventList);
    return cell;
  }

  // Month row heights change with the viewport. Measure each event list after
  // layout, hide only the chips that do not fit, and expose their count beside
  // the day number instead of allowing a nested scrollbar.
  function collapseMonthCell(cell) {
    var eventList = cell.querySelector(".cell-events");
    var more = cell.querySelector(".more");
    var chips = Array.prototype.slice.call(
      eventList.querySelectorAll(".chip"));
    chips.forEach(function (chip) { chip.hidden = false; });
    more.hidden = true;
    var hiddenCount = 0;
    while (chips.length - hiddenCount > 0 &&
        eventList.scrollHeight > eventList.clientHeight + 1) {
      hiddenCount += 1;
      var hiddenChip = chips[chips.length - hiddenCount];
      if (hiddenChip === pinnedChip) { unpinTooltip(); }
      hiddenChip.hidden = true;
    }
    if (hiddenCount) {
      more.textContent = "+" + hiddenCount;
      more.title = hiddenCount + (hiddenCount === 1
        ? " more event"
        : " more events");
      more.setAttribute("aria-label", more.title + "; open day view");
      more.hidden = false;
    }
  }

  function collapseMonthCells() {
    if (state.view !== "month") { return; }
    grid.querySelectorAll(".cell").forEach(collapseMonthCell);
  }

  function scheduleMonthCollapse() {
    if (collapseFrame) { cancelAnimationFrame(collapseFrame); }
    collapseFrame = requestAnimationFrame(function () {
      collapseFrame = 0;
      collapseMonthCells();
    });
  }

  var dayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  var dayInitials = ["S", "M", "T", "W", "T", "F", "S"];
  var dayFull = [
    "Sunday", "Monday", "Tuesday", "Wednesday",
    "Thursday", "Friday", "Saturday"
  ];

  // Must match --hour-h in the stylesheet: an event's offset and height are
  // computed in pixels against the hour rows drawn from it.
  var HOUR_PX = 48;
  var MINUTES_PER_DAY = 1440;
  // A 15-minute event would otherwise be too short to read its own title.
  var MIN_EVENT_PX = 20;
  // The same floor in minutes. A block is never drawn shorter than this, so
  // lane-packing must treat a short event as occupying at least this span or
  // two back-to-back short events get full width yet overlap on screen.
  var MIN_EVENT_MIN = MIN_EVENT_PX * 60 / HOUR_PX;
  var DEFAULT_SCROLL_HOUR = 8;

  function minutesIntoDay(date) {
    return date.getHours() * 60 + date.getMinutes();
  }
  function pixelsFor(minutes) {
    return minutes * HOUR_PX / 60;
  }
  function formatHour(hour) {
    // Local hour labels, in the browser's own 12/24-hour convention.
    return new Date(2000, 0, 1, hour).toLocaleTimeString(
      undefined, { hour: "numeric" });
  }

  // The events that start on this day, each with the span it occupies in the
  // column. start_epoch is an absolute instant, so every offset below is the
  // event's local wall-clock time in the viewer's own time zone. An event
  // running past midnight is clipped to the end of the day; it is only ever
  // drawn in the column it starts in.
  function dayItems(date) {
    var next = addDays(date, 1);
    var items = [];
    entries.forEach(function (entry, index) {
      var start = new Date(entry.start_epoch * 1000);
      if (start < date || start >= next) { return; }
      var startMin = minutesIntoDay(start);
      var endMin = Math.min(
        MINUTES_PER_DAY,
        startMin + Math.max(1, entry.duration_minutes));
      items.push({
        entry: entry,
        index: index,
        startMin: startMin,
        endMin: endMin,
        // The extent the block occupies once clamped to the minimum height.
        // Clustering, lane-packing and the rendered height all read this, so
        // the reserved and drawn spans match (except that the render clips to
        // the day boundary, which only ever reserves a little extra at the very
        // end of the day, where nothing starts after it).
        layoutEnd: Math.max(endMin, startMin + MIN_EVENT_MIN),
        column: 0,
        columns: 1
      });
    });
    items.sort(function (a, b) {
      return a.startMin - b.startMin || b.layoutEnd - a.layoutEnd;
    });
    return items;
  }

  // Pack one run of transitively overlapping events into as few side-by-side
  // lanes as it needs, reusing a lane as soon as its last event has ended.
  // Every event in the run is then drawn at the same width, so no lane hangs
  // over an event that does not overlap it.
  function assignLanes(cluster) {
    var laneEnds = [];
    cluster.forEach(function (item) {
      var lane = 0;
      while (lane < laneEnds.length && laneEnds[lane] > item.startMin) {
        lane += 1;
      }
      laneEnds[lane] = item.layoutEnd;
      item.column = lane;
    });
    cluster.forEach(function (item) { item.columns = laneEnds.length; });
  }

  function layoutDay(items) {
    var cluster = [];
    var clusterEnd = -1;
    items.forEach(function (item) {
      if (cluster.length && item.startMin >= clusterEnd) {
        assignLanes(cluster);
        cluster = [];
        clusterEnd = -1;
      }
      cluster.push(item);
      clusterEnd = Math.max(clusterEnd, item.layoutEnd);
    });
    if (cluster.length) { assignLanes(cluster); }
    return items;
  }

  function timeBlock(item) {
    var chip = chipFor(item.entry, item.index);
    chip.classList.add("tg-ev");
    var width = 100 / item.columns;
    chip.style.top = pixelsFor(item.startMin) + "px";
    // layoutEnd carries the minimum-height floor, but that floor can push a
    // late event past the end of the day; clip the drawn height at the day
    // boundary so the block never bleeds below the 24-hour column.
    chip.style.height = pixelsFor(
      Math.min(item.layoutEnd, MINUTES_PER_DAY) - item.startMin) + "px";
    chip.style.left = "calc(" + (item.column * width) + "% + 2px)";
    chip.style.width = "calc(" + width + "% - 4px)";
    return chip;
  }

  function hourGutter() {
    var gutter = el("div", "tg-gutter");
    for (var hour = 0; hour < 24; hour += 1) {
      var cell = el("div", "tg-hour");
      cell.appendChild(el("span", null, formatHour(hour)));
      gutter.appendChild(cell);
    }
    return gutter;
  }

  function dayHeader(date, longName, clickable) {
    var head = el(clickable ? "button" : "div", "tg-head");
    if (sameDay(date, new Date())) { head.classList.add("today"); }
    if (clickable) {
      head.type = "button";
      head.classList.add("clickable");
      head.setAttribute("aria-label", "Open " + date.toLocaleDateString(
        undefined, { weekday: "long", month: "long", day: "numeric" }));
      head.addEventListener("click", function () { openDay(date); });
    }
    head.appendChild(el("div", "tg-dow", date.toLocaleDateString(
      undefined, { weekday: longName ? "long" : "short" })));
    head.appendChild(el("div", "tg-daynum", String(date.getDate())));
    return head;
  }

  function dayColumn(date, items) {
    var column = el("div", "tg-col");
    var now = new Date();
    for (var hour = 0; hour < 24; hour += 1) {
      column.appendChild(el("div", "tg-hour"));
    }
    items.forEach(function (item) {
      column.appendChild(timeBlock(item));
    });
    if (sameDay(date, now)) {
      column.classList.add("today");
      var marker = el("div", "tg-now");
      marker.style.top = pixelsFor(minutesIntoDay(now)) + "px";
      column.appendChild(marker);
    }
    return column;
  }

  function renderTimeGrid(range, days) {
    var dates = [];
    for (var offset = 0; offset < days; offset += 1) {
      dates.push(addDays(range.start, offset));
    }
    grid.appendChild(el("div", "tg-corner"));
    dates.forEach(function (date) {
      grid.appendChild(dayHeader(date, days === 1, days > 1));
    });
    grid.appendChild(hourGutter());
    var earliest = null;
    dates.forEach(function (date) {
      var items = layoutDay(dayItems(date));
      grid.appendChild(dayColumn(date, items));
      items.forEach(function (item) {
        if (earliest === null || item.startMin < earliest) {
          earliest = item.startMin;
        }
      });
    });
    // A 24-hour day is taller than the viewport, so open it where the events
    // are rather than at midnight.
    var target = earliest === null ? DEFAULT_SCROLL_HOUR * 60 : earliest;
    scroller.scrollTop = Math.max(0, pixelsFor(target) - HOUR_PX / 2);
  }

  function render() {
    grid.className = state.view === "month"
      ? "month"
      : "timegrid " + state.view;
    grid.replaceChildren();
    unpinTooltip();
    var range = rangeFor();
    if (state.view === "month") {
      var mobile = isMobile();
      dayNames.forEach(function (name, index) {
        var cell = el("div", "dow", mobile ? dayInitials[index] : name);
        // The single-letter mobile heading stays legible to assistive tech.
        cell.setAttribute("aria-label", dayFull[index]);
        grid.appendChild(cell);
      });
      for (var offset = 0; offset < 42; offset += 1) {
        grid.appendChild(buildCell(
          addDays(range.start, offset), state.anchor.getMonth()));
      }
      scheduleMonthCollapse();
    } else {
      renderTimeGrid(range, state.view === "day" ? 1 : weekSpan());
    }
    renderPeriodLabel(range);
    statusLine.textContent = entries.length
      ? ""
      : "No events in this period.";
  }

  function renderPeriodLabel(range) {
    if (isMobile()) {
      renderMobilePeriodLabel(range);
    } else if (state.view === "month") {
      periodLabel.textContent = state.anchor.toLocaleDateString(
        undefined, { month: "long", year: "numeric" });
    } else if (state.view === "week") {
      var last = addDays(range.start, weekSpan() - 1);
      periodLabel.textContent = range.start.toLocaleDateString(
        undefined, { month: "short", day: "numeric" }) + " \\u2013 " +
        last.toLocaleDateString(
          undefined, { month: "short", day: "numeric", year: "numeric" });
    } else {
      periodLabel.textContent = state.anchor.toLocaleDateString(
        undefined,
        { weekday: "long", month: "long", day: "numeric", year: "numeric" });
    }
    // The mobile label is ellipsised when the column is too narrow, so the
    // full text stays reachable on a long press. Reassigning on every render
    // keeps a desktop label from holding a stale mobile tooltip.
    periodLabel.title = periodLabel.textContent;
  }

  // The mobile label shares row one with the title and the sign-out button, so
  // it is abbreviated to fit: the month always shows, and the year is dropped
  // from the day and week views where the grid already carries the dates.
  function renderMobilePeriodLabel(range) {
    var text;
    if (state.view === "month") {
      text = state.anchor.toLocaleDateString(
        undefined, { month: "short", year: "numeric" });
    } else if (state.view === "week") {
      var last = addDays(range.start, weekSpan() - 1);
      var tail = last.getMonth() === range.start.getMonth()
        ? String(last.getDate())
        : last.toLocaleDateString(undefined, { month: "short", day: "numeric" });
      text = range.start.toLocaleDateString(
        undefined, { month: "short", day: "numeric" }) + " \\u2013 " + tail;
    } else {
      text = state.anchor.toLocaleDateString(
        undefined, { weekday: "short", month: "short", day: "numeric" });
    }
    periodLabel.textContent = text;
  }

  function tooltipContent(entry) {
    tooltip.replaceChildren();
    var title = el("h2", null, entry.title);
    var badge = el("span", "badge " + entry.status,
      statusLabel(entry.status));
    title.appendChild(badge);
    tooltip.appendChild(title);
    var start = new Date(entry.start_epoch * 1000);
    var end = new Date(
      (entry.start_epoch + entry.duration_minutes * 60) * 1000);
    tooltip.appendChild(el("div", "meta",
      entry.category + " \\u00b7 " + start.toLocaleDateString(
        undefined,
        { weekday: "short", month: "short", day: "numeric" }) +
      " " + formatTime(start) + " \\u2013 " + formatTime(end) +
      " (" + formatDuration(entry.duration_minutes) + ")"));
    if (entry.description) {
      var desc = el("div", "desc");
      appendMarkdown(desc, entry.description);
      tooltip.appendChild(desc);
    }
    tooltip.appendChild(el("div", "sep"));
    tooltip.appendChild(el("div", "row",
      "Leader: " + entry.leader_name));
    if (entry.projected) {
      tooltip.appendChild(el("div", "row",
        "Projected \\u2014 signups open when posted."));
      return;
    }
    tooltip.appendChild(el("div", "row",
      "Participants: " + entry.active_count +
      (entry.capacity_total === null ? "" : "/" + entry.capacity_total)));
    if (entry.has_roles) {
      tooltip.appendChild(el("div", "row",
        "Healers " + entry.healers + " \\u00b7 DPS " + entry.dps +
        " \\u00b7 Quickness " + entry.quickness +
        " \\u00b7 Alacrity " + entry.alacrity));
    }
    if (entry.waitlist_count > 0) {
      tooltip.appendChild(el("div", "row",
        "Waitlist: " + entry.waitlist_count));
    }
  }

  function showTooltip(chip) {
    var entry = entries[Number(chip.getAttribute("data-i"))];
    if (!entry) { return; }
    if (tooltipChip && tooltipChip !== chip) {
      tooltipChip.setAttribute("aria-expanded", "false");
    }
    tooltipChip = chip;
    chip.setAttribute("aria-expanded", "true");
    tooltipContent(entry);
    tooltip.style.display = "block";
    var rect = chip.getBoundingClientRect();
    var box = tooltip.getBoundingClientRect();
    var left = Math.min(
      rect.left, window.innerWidth - box.width - 12);
    var top = rect.bottom + 6;
    if (top + box.height > window.innerHeight - 8) {
      top = Math.max(8, rect.top - box.height - 6);
    }
    tooltip.style.left = Math.max(8, left) + "px";
    tooltip.style.top = top + "px";
  }
  function hideTooltip() {
    tooltip.style.display = "none";
    if (tooltipChip) {
      tooltipChip.setAttribute("aria-expanded", "false");
      tooltipChip = null;
    }
  }
  function pinTooltip(chip) {
    if (pinnedChip && pinnedChip !== chip) {
      pinnedChip.classList.remove("pinned");
    }
    pinnedChip = chip;
    pinnedChip.classList.add("pinned");
    showTooltip(chip);
  }
  function unpinTooltip() {
    if (pinnedChip) { pinnedChip.classList.remove("pinned"); }
    pinnedChip = null;
    hideTooltip();
  }

  grid.addEventListener("mouseover", function (event) {
    var chip = event.target.closest(".chip");
    if (chip && !chip.contains(event.relatedTarget) && !pinnedChip) {
      showTooltip(chip);
    }
  });
  grid.addEventListener("mouseout", function (event) {
    var chip = event.target.closest(".chip");
    if (chip && !chip.contains(event.relatedTarget) && !pinnedChip) {
      hideTooltip();
    }
  });
  grid.addEventListener("focusin", function (event) {
    var chip = event.target.closest(".chip");
    if (chip && !pinnedChip) { showTooltip(chip); }
  });
  grid.addEventListener("focusout", function (event) {
    if (event.target.closest(".chip") && !pinnedChip) { hideTooltip(); }
  });
  grid.addEventListener("click", function (event) {
    var chip = event.target.closest(".chip");
    if (!chip) { return; }
    event.stopPropagation();
    if (pinnedChip === chip) {
      unpinTooltip();
    } else {
      pinTooltip(chip);
    }
  });
  grid.addEventListener("keydown", function (event) {
    var chip = event.target.closest(".chip");
    if (chip && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      chip.click();
    }
  });
  document.addEventListener("click", function () {
    if (pinnedChip) { unpinTooltip(); }
  });

  // A horizontal swipe steps to the previous or next set of events. The
  // gesture is only claimed when it is clearly horizontal, so vertical
  // scrolling of the day and 3-day time grids is left untouched.
  var swipeStartX = 0;
  var swipeStartY = 0;
  var swipeStartTime = 0;
  var swipeTracking = false;
  scroller.addEventListener("touchstart", function (event) {
    if (event.touches.length !== 1) { swipeTracking = false; return; }
    var touch = event.touches[0];
    swipeStartX = touch.clientX;
    swipeStartY = touch.clientY;
    swipeStartTime = Date.now();
    swipeTracking = true;
  }, { passive: true });
  scroller.addEventListener("touchend", function (event) {
    if (!swipeTracking) { return; }
    swipeTracking = false;
    var touch = event.changedTouches[0];
    var dx = touch.clientX - swipeStartX;
    var dy = touch.clientY - swipeStartY;
    if (Date.now() - swipeStartTime > 700) { return; }
    if (Math.abs(dx) < 60) { return; }
    if (Math.abs(dx) < Math.abs(dy) * 1.5) { return; }
    hideTooltip();
    step(dx < 0 ? 1 : -1);
  }, { passive: true });

  function refresh() {
    writeHash();
    var range = rangeFor();
    statusLine.textContent = "Loading\\u2026";
    fetch("/api/events?start=" +
      Math.floor(range.start.getTime() / 1000) + "&end=" +
      Math.floor(range.end.getTime() / 1000))
      .then(function (response) {
        if (response.status === 401) {
          location.href = "/login";
          throw new Error("unauthorized");
        }
        if (!response.ok) { throw new Error("failed"); }
        return response.json();
      })
      .then(function (payload) {
        entries = payload.entries || [];
        render();
      })
      .catch(function () {
        if (statusLine.textContent === "Loading\\u2026") {
          statusLine.textContent = "Could not load events.";
        }
      });
  }

  document.querySelectorAll("[data-view]").forEach(function (button) {
    button.addEventListener("click", function () {
      state.view = button.getAttribute("data-view");
      syncViewButtons();
      refresh();
    });
  });
  function syncViewButtons() {
    document.querySelectorAll("[data-view]").forEach(function (button) {
      var active = button.getAttribute("data-view") === state.view;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
    var weekButton = document.getElementById("week-view");
    if (weekButton) {
      weekButton.textContent = isMobile() ? "3 Day" : "Week";
    }
  }
  document.getElementById("prev").addEventListener("click", function () {
    step(-1);
  });
  document.getElementById("next").addEventListener("click", function () {
    step(1);
  });
  document.getElementById("today").addEventListener("click", function () {
    state.anchor = startOfDay(new Date());
    refresh();
  });
  window.addEventListener("hashchange", function () {
    readHash();
    syncViewButtons();
    refresh();
  });
  // Crossing the breakpoint changes the week span, the month layout and the
  // view labels, so re-sync and reload whenever it flips.
  mobileQuery.addEventListener("change", function () {
    syncViewButtons();
    refresh();
  });
  window.addEventListener("resize", scheduleMonthCollapse);

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

  readHash();
  syncViewButtons();
  refresh();
})();
</script>
</body>
</html>
"""
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
    + _SHARED_STYLE
    + _DASHBOARD_HEADER_STYLE
    + """
body {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}
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
  .ranges { grid-column: 1 / -1; grid-row: 2; justify-self: center; }
  .custom { grid-column: 1 / -1; grid-row: 3; justify-content: center; }
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
  <nav class="ranges" aria-label="Time range">
    <button type="button" data-range="24h">24h</button>
    <button type="button" data-range="7d">7d</button>
    <button type="button" data-range="30d">30d</button>
    <button type="button" data-range="custom">Custom</button>
  </nav>
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
  <div id="custom-range" class="custom">
    <label for="custom-start">From</label>
    <input type="date" id="custom-start">
    <label for="custom-end">To</label>
    <input type="date" id="custom-end">
    <button type="button" id="custom-apply">Apply</button>
    <span id="custom-error" class="custom-error" role="status"
      aria-live="polite"></span>
  </div>
</header>
<main>
  <section class="card">
    <h2>Stock on hand over time</h2>
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
  var Y_MAX = 50;
  var SVG_NS = "http://www.w3.org/2000/svg";
  var TABLE_PAGE_SIZE = 5;

  var mobileQuery = window.matchMedia("(max-width: 640px)");
  function isMobile() { return mobileQuery.matches; }

  // The chart uses a wide viewBox on desktop and a taller one on mobile, where
  // it scales to the narrow screen width; the extra height makes the graph
  // read large on a phone. Coordinates are computed against whichever set is
  // active, so M is refreshed at the start of every chart render.
  function metrics() {
    if (isMobile()) {
      return {
        w: 480, h: 620, top: 16, right: 14, bottom: 36, left: 34, ticks: 4
      };
    }
    return {
      w: 960, h: 380, top: 16, right: 16, bottom: 32, left: 34, ticks: 6
    };
  }
  var M = metrics();
  function plotW() { return M.w - M.left - M.right; }
  function plotH() { return M.h - M.top - M.bottom; }

  // hidden holds the feasts the reader has switched off in the legend, keyed
  // by guild storage id so the choice outlives a range change and the redraw
  // it brings.
  var state = {
    range: "24h", data: null, activeFeast: 0, tablePage: 0, hidden: {}
  };

  // A pinned touch selection listens on the whole page, so the chart it
  // belongs to is torn down before another one is drawn.
  var detachHover = null;

  var legend = document.getElementById("legend");
  var chart = document.getElementById("chart");
  var chartStatus = document.getElementById("chart-status");
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
    var value = count;
    if (value < 0) { value = 0; }
    if (value > Y_MAX) { value = Y_MAX; }
    return M.top + (1 - value / Y_MAX) * plotH();
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
    if (detachHover) { detachHover(); detachHover = null; }
    chart.replaceChildren();
    var canvas = svg("svg", {
      "class": "chart-svg",
      viewBox: "0 0 " + M.w + " " + M.h,
      role: "img",
      "aria-label": "Stock on hand over time, one line per feast"
    });

    // Horizontal gridlines and y labels every ten counts, 0 through Y_MAX.
    for (var value = 0; value <= Y_MAX; value += 10) {
      var y = scaleY(value);
      canvas.appendChild(svg("line", {
        "class": value === 0 ? "axis" : "grid",
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
        var coords = points.map(function (point) {
          return scaleX(point.t).toFixed(1) + "," +
            scaleY(point.count).toFixed(1);
        }).join(" ");
        canvas.appendChild(svg("polyline", {
          "class": "series-line", stroke: color, points: coords
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

  // The longest custom window the server will serve, mirrored here so a
  // range too wide to draw is named as such instead of coming back as a
  // failed load.
  var MAX_CUSTOM_DAYS = 366;

  // The window a pair of applied dates asks for, as whole epoch seconds, or
  // null while the reader is still on one of the presets.
  var customWindow = null;

  var customPanel = document.getElementById("custom-range");
  var customStart = document.getElementById("custom-start");
  var customEnd = document.getElementById("custom-end");
  var customError = document.getElementById("custom-error");

  // A local calendar day in the spelling a date input reads and writes.
  function dayValue(date) {
    return date.getFullYear() + "-" +
      String(date.getMonth() + 1).padStart(2, "0") + "-" +
      String(date.getDate()).padStart(2, "0");
  }

  // Reads one date input as a local calendar day. The parts are re-read off
  // the Date afterwards, so a day that does not exist - the 31st of a 30-day
  // month, typed into the field - is refused rather than silently rolled into
  // the month after it.
  function parseDay(value) {
    var parts = /^(\\d{4})-(\\d{2})-(\\d{2})$/.exec(value || "");
    if (!parts) { return null; }
    var year = Number(parts[1]);
    var month = Number(parts[2]);
    var day = Number(parts[3]);
    var date = new Date(year, month - 1, day);
    if (date.getFullYear() !== year || date.getMonth() !== month - 1 ||
        date.getDate() !== day) {
      return null;
    }
    return date;
  }

  // The window the two fields describe, or the reason it cannot be drawn. A
  // picked pair covers whole local days: it opens at midnight on the first and
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
    var until = Math.floor(new Date(
      to.getFullYear(), to.getMonth(), to.getDate() + 1).getTime() / 1000) - 1;
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

  // Opening the picker for the first time fills it with the whole local days
  // the window on screen falls inside, which is the closest a pair of dates
  // can come to the range already drawn: the fields hold days and nothing
  // finer, so a rolling preset cannot be reproduced exactly. Applying an
  // untouched 24h default therefore asks for yesterday from midnight rather
  // than this time yesterday, and reads a few hours wider than the button it
  // came from. Wider is the right way to miss: the narrower pair would drop
  // hours the reader can already see.
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
    console.debug("feast chart range:", action, reason, days);
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

  // The query the current selection asks for: a preset window by name, or the
  // applied pair of epoch seconds.
  function rangeQuery() {
    if (state.range === "custom" && customWindow) {
      return "?range=custom&start=" +
        encodeURIComponent(String(customWindow.since)) +
        "&end=" + encodeURIComponent(String(customWindow.until));
    }
    return "?range=" + encodeURIComponent(state.range);
  }

  function syncRangeButtons() {
    document.querySelectorAll("[data-range]").forEach(function (button) {
      var active = button.getAttribute("data-range") === state.range;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

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

  document.querySelectorAll("[data-range]").forEach(function (button) {
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


ROSTER_PAGE = (
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Guild Roster</title>
<style>"""
    + _SHARED_STYLE
    + _DASHBOARD_HEADER_STYLE
    + """
body {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}
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
  .ranges { grid-column: 1 / -1; grid-row: 2; justify-self: center; }
  .custom { grid-column: 1 / -1; grid-row: 3; justify-content: center; }
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
  <nav class="ranges" aria-label="Time range">
    <button type="button" data-range="24h">24h</button>
    <button type="button" data-range="7d">7d</button>
    <button type="button" data-range="30d">30d</button>
    <button type="button" data-range="custom">Custom</button>
  </nav>
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
  <div id="custom-range" class="custom">
    <label for="custom-start">From</label>
    <input type="date" id="custom-start">
    <label for="custom-end">To</label>
    <input type="date" id="custom-end">
    <button type="button" id="custom-apply">Apply</button>
    <span id="custom-error" class="custom-error" role="status"
      aria-live="polite"></span>
  </div>
</header>
<main>
  <section class="card">
    <h2>Guild members over time<span id="now-count" class="now"></span></h2>
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

  var state = { range: "24h", data: null, tablePage: 0, scale: null };

  // A pinned touch selection listens on the whole page, so the chart it
  // belongs to is torn down before another one is drawn.
  var detachHover = null;

  var legend = document.getElementById("legend");
  var chart = document.getElementById("chart");
  var chartStatus = document.getElementById("chart-status");
  var nowCount = document.getElementById("now-count");
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

    // Membership is a step: the count holds until somebody joins or leaves,
    // then jumps. Each pair of vertices is therefore drawn as a horizontal
    // run followed by a vertical jump, never as a diagonal, which would claim
    // members trickled in over the hours between two changes.
    var coords = [];
    points().forEach(function (point, index) {
      var px = scaleX(point.t);
      var py = scaleY(point.count);
      if (index > 0) {
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
        name === "keydown" || name === "blur") {
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

  // The longest custom window the server will serve, mirrored here so a
  // range too wide to draw is named as such instead of coming back as a
  // failed load.
  var MAX_CUSTOM_DAYS = 366;

  // The window a pair of applied dates asks for, as whole epoch seconds, or
  // null while the reader is still on one of the presets.
  var customWindow = null;

  var customPanel = document.getElementById("custom-range");
  var customStart = document.getElementById("custom-start");
  var customEnd = document.getElementById("custom-end");
  var customError = document.getElementById("custom-error");

  // A local calendar day in the spelling a date input reads and writes.
  function dayValue(date) {
    return date.getFullYear() + "-" +
      String(date.getMonth() + 1).padStart(2, "0") + "-" +
      String(date.getDate()).padStart(2, "0");
  }

  // Reads one date input as a local calendar day. The parts are re-read off
  // the Date afterwards, so a day that does not exist - the 31st of a 30-day
  // month, typed into the field - is refused rather than silently rolled into
  // the month after it.
  function parseDay(value) {
    var parts = /^(\\d{4})-(\\d{2})-(\\d{2})$/.exec(value || "");
    if (!parts) { return null; }
    var year = Number(parts[1]);
    var month = Number(parts[2]);
    var day = Number(parts[3]);
    var date = new Date(year, month - 1, day);
    if (date.getFullYear() !== year || date.getMonth() !== month - 1 ||
        date.getDate() !== day) {
      return null;
    }
    return date;
  }

  // The window the two fields describe, or the reason it cannot be drawn. A
  // picked pair covers whole local days: it opens at midnight on the first and
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
    var until = Math.floor(new Date(
      to.getFullYear(), to.getMonth(), to.getDate() + 1).getTime() / 1000) - 1;
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

  // Opening the picker for the first time fills it with the whole local days
  // the window on screen falls inside, which is the closest a pair of dates
  // can come to the range already drawn: the fields hold days and nothing
  // finer, so a rolling preset cannot be reproduced exactly. Applying an
  // untouched 24h default therefore asks for yesterday from midnight rather
  // than this time yesterday, and reads a few hours wider than the button it
  // came from. Wider is the right way to miss: the narrower pair would drop
  // hours the reader can already see.
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
    console.debug("roster chart range:", action, reason, days);
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

  // The query the current selection asks for: a preset window by name, or the
  // applied pair of epoch seconds.
  function rangeQuery() {
    if (state.range === "custom" && customWindow) {
      return "?range=custom&start=" +
        encodeURIComponent(String(customWindow.since)) +
        "&end=" + encodeURIComponent(String(customWindow.until));
    }
    return "?range=" + encodeURIComponent(state.range);
  }

  function syncRangeButtons() {
    document.querySelectorAll("[data-range]").forEach(function (button) {
      var active = button.getAttribute("data-range") === state.range;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
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

  document.querySelectorAll("[data-range]").forEach(function (button) {
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


GOLD_PAGE = (
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Guild Bank</title>
<style>"""
    + _SHARED_STYLE
    + _DASHBOARD_HEADER_STYLE
    + """
body {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}
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
  .ranges { grid-column: 1 / -1; grid-row: 2; justify-self: center; }
  .custom { grid-column: 1 / -1; grid-row: 3; justify-content: center; }
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
  <nav class="ranges" aria-label="Time range">
    <button type="button" data-range="24h">24h</button>
    <button type="button" data-range="7d">7d</button>
    <button type="button" data-range="30d">30d</button>
    <button type="button" data-range="custom">Custom</button>
  </nav>
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
  <div id="custom-range" class="custom">
    <label for="custom-start">From</label>
    <input type="date" id="custom-start">
    <label for="custom-end">To</label>
    <input type="date" id="custom-end">
    <button type="button" id="custom-apply">Apply</button>
    <span id="custom-error" class="custom-error" role="status"
      aria-live="polite"></span>
  </div>
</header>
<main>
  <section class="card">
    <h2>Gold in the guild bank<span id="now-balance" class="now"></span></h2>
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
  // Okabe-Ito colourblind-safe palette: one hue per direction the gold went,
  // and a third for the balance line itself.
  var OPERATIONS = {
    deposit: { color: "#009E73", label: "Deposited" },
    withdraw: { color: "#D55E00", label: "Withdrew" }
  };
  var OPERATION_ORDER = ["deposit", "withdraw"];
  var LINE_COLOR = "#56B4E9";
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
  var M = metrics();
  function plotW() { return M.w - M.left - M.right; }
  function plotH() { return M.h - M.top - M.bottom; }

  var state = { range: "24h", data: null, tablePage: 0, scale: null };

  // A pinned touch selection listens on the whole page, so the chart it
  // belongs to is torn down before another one is drawn.
  var detachHover = null;

  var legend = document.getElementById("legend");
  var chart = document.getElementById("chart");
  var chartStatus = document.getElementById("chart-status");
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
  function operationOf(operation) {
    return OPERATIONS[operation] || OPERATIONS.withdraw;
  }

  function gold(copper) { return copper / COPPER_PER_GOLD; }

  // Spell an exact copper value in GW2's denominations, omitting only trailing
  // zero denominations: 1g, 1g1s, 1g0s1c and 10c all remain unambiguous.
  function formatCoins(copper) {
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
        "Gold in the guild bank over time, with one dot per movement"
    });

    // Horizontal gridlines and y labels at every step of the computed scale.
    // The loop counts steps rather than accumulating them, because a
    // fractional step accumulated in floating point drifts off the value the
    // label claims.
    var lines = Math.round(
      (state.scale.high - state.scale.low) / state.scale.step);
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
      yLabel.textContent = formatCoins(value * COPPER_PER_GOLD);
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

    // A balance is a step: it holds until somebody deposits or withdraws,
    // then jumps. Each pair of vertices is therefore drawn as a horizontal
    // run followed by a vertical jump, never as a diagonal, which would
    // claim the gold trickled in over the hours between two movements.
    var coords = [];
    points().forEach(function (point, index) {
      var px = scaleX(point.t);
      var py = scaleY(point.coins);
      if (index > 0) {
        coords.push(px.toFixed(1) + "," +
          scaleY(points()[index - 1].coins).toFixed(1));
      }
      coords.push(px.toFixed(1) + "," + py.toFixed(1));
    });
    if (coords.length > 1) {
      canvas.appendChild(svg("polyline", {
        "class": "balance-line", stroke: LINE_COLOR, points: coords.join(" ")
      }));
    }

    // One dot per movement, in its direction's colour. Every movement is
    // drawn; the series is never downsampled. Each dot is collected so the
    // hover can snap to it.
    var plotted = [];
    movements().forEach(function (movement) {
      if (movement.after === null) { return; }
      var px = scaleX(movement.t);
      var py = scaleY(movement.after);
      canvas.appendChild(svg("circle", {
        "class": "event-dot",
        cx: px.toFixed(1),
        cy: py.toFixed(1),
        r: 4,
        fill: operationOf(movement.operation).color
      }));
      plotted.push({ x: px, y: py, movement: movement });
    });

    detachHover = attachHover(canvas, plotted);
    chart.appendChild(canvas);
    chartStatus.textContent = plotted.length
      ? ""
      : "No gold moved in or out of the bank in this period.";
  }

  // Movements that share a moment form a single column, so the crosshair
  // snaps to one x and the tooltip lists everything recorded there.
  function groupColumns(plotted) {
    var byTime = {};
    var columns = [];
    plotted.forEach(function (point) {
      var key = String(point.movement.t);
      var column = byTime[key];
      if (!column) {
        column = { t: point.movement.t, x: point.x, points: [] };
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
      column.points.forEach(function (point) {
        var movement = point.movement;
        var row = el("div",
          "tip-row" + (point === emphasized ? " em" : ""));
        var swatch = el("span", "swatch");
        swatch.style.background = operationOf(movement.operation).color;
        row.appendChild(swatch);
        row.appendChild(el("span", "name", movement.name));
        row.appendChild(el("span", "val",
          formatSigned(movement.coins, movement.operation)));
        tooltip.appendChild(row);
      });
      tooltip.appendChild(el("div", "tip-note",
        "Bank held " + formatCoins(emphasized.movement.after) + "."));
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
          stroke: operationOf(point.movement.operation).color
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
    OPERATION_ORDER.forEach(function (operation) {
      var item = el("span", "item");
      item.setAttribute("role", "listitem");
      var swatch = el("span", "swatch");
      swatch.style.background = OPERATIONS[operation].color;
      item.appendChild(swatch);
      item.appendChild(el("span", "legend-name", OPERATIONS[operation].label));
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

  // The longest custom window the server will serve, mirrored here so a
  // range too wide to draw is named as such instead of coming back as a
  // failed load.
  var MAX_CUSTOM_DAYS = 366;

  // The window a pair of applied dates asks for, as whole epoch seconds, or
  // null while the reader is still on one of the presets.
  var customWindow = null;

  var customPanel = document.getElementById("custom-range");
  var customStart = document.getElementById("custom-start");
  var customEnd = document.getElementById("custom-end");
  var customError = document.getElementById("custom-error");

  // A local calendar day in the spelling a date input reads and writes.
  function dayValue(date) {
    return date.getFullYear() + "-" +
      String(date.getMonth() + 1).padStart(2, "0") + "-" +
      String(date.getDate()).padStart(2, "0");
  }

  // Reads one date input as a local calendar day. The parts are re-read off
  // the Date afterwards, so a day that does not exist - the 31st of a 30-day
  // month, typed into the field - is refused rather than silently rolled
  // into the month after it.
  function parseDay(value) {
    var parts = /^(\\d{4})-(\\d{2})-(\\d{2})$/.exec(value || "");
    if (!parts) { return null; }
    var year = Number(parts[1]);
    var month = Number(parts[2]);
    var day = Number(parts[3]);
    var date = new Date(year, month - 1, day);
    if (date.getFullYear() !== year || date.getMonth() !== month - 1 ||
        date.getDate() !== day) {
      return null;
    }
    return date;
  }

  // The window the two fields describe, or the reason it cannot be drawn. A
  // picked pair covers whole local days: it opens at midnight on the first
  // and closes at the last second of the second, so picking one day twice is
  // that whole day rather than an empty instant.
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
    var until = Math.floor(new Date(
      to.getFullYear(), to.getMonth(), to.getDate() + 1).getTime() / 1000) - 1;
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

  // Opening the picker for the first time fills it with the whole local days
  // the window on screen falls inside, which is the closest a pair of dates
  // can come to the range already drawn: the fields hold days and nothing
  // finer, so a rolling preset cannot be reproduced exactly. Applying an
  // untouched 24h default therefore asks for yesterday from midnight rather
  // than this time yesterday, and reads a few hours wider than the button it
  // came from. Wider is the right way to miss: the narrower pair would drop
  // hours the reader can already see.
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
    console.debug("gold chart range:", action, reason, days);
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

  // The query the current selection asks for: a preset window by name, or the
  // applied pair of epoch seconds.
  function rangeQuery() {
    if (state.range === "custom" && customWindow) {
      return "?range=custom&start=" +
        encodeURIComponent(String(customWindow.since)) +
        "&end=" + encodeURIComponent(String(customWindow.until));
    }
    return "?range=" + encodeURIComponent(state.range);
  }

  function syncRangeButtons() {
    document.querySelectorAll("[data-range]").forEach(function (button) {
      var active = button.getAttribute("data-range") === state.range;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

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

  document.querySelectorAll("[data-range]").forEach(function (button) {
    button.addEventListener("click", function () {
      var picked = button.getAttribute("data-range");
      if (picked === "custom") {
        // The Custom button only reveals the picker; the range itself does
        // not move until a pair of dates is applied, so a stray tap costs
        // nothing.
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
