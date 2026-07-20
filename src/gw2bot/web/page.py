"""Static HTML documents served by the web calendar.

Every document is a fixed string with no server-side interpolation, so
user-authored content can never be injected into markup. Dynamic data
reaches the calendar page only through the JSON API and is inserted with
``textContent`` on the client; event descriptions additionally pass
through a small client-side Discord-markdown renderer that only ever
builds DOM nodes and text nodes, never HTML strings.
"""

from __future__ import annotations

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

SIGN_IN_PAGE = _simple_page(
    "Guild Events",
    "Guild Events Calendar",
    "Sign in with Discord to view the guild event calendar.",
    _SIGN_IN_ACTION,
)

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
    + """
body {
  display: flex;
  flex-direction: column;
  height: 100vh;
  /* The layout is a fixed-height app: the header is pinned and only <main>
     scrolls, so the page itself must never grow a scrollbar of its own. */
  overflow: hidden;
}
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
.controls, .views { display: flex; gap: 0.25rem; }
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
button.active { background: var(--accent); border-color: var(--accent); }
#period { font-weight: 600; font-size: 0.95rem; min-width: 11rem; }
.spacer { flex: 1; }
#whoami { color: var(--muted); font-size: 0.85rem; }
header a { font-size: 0.85rem; }
header form { display: flex; }
.signout { display: inline-flex; align-items: center; gap: 0.35rem; }
.signout-icon { display: none; }
.signout-icon, .signout-icon * { pointer-events: none; }
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
  flex-direction: column;
  align-items: flex-start;
  gap: 0;
  margin: 0;
  padding: 0.1rem 0.3rem;
  line-height: 1.25;
  z-index: 1;
}
.chip.tg-ev .time { font-size: 0.7rem; }
.chip.tg-ev .name { max-width: 100%; }
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
  overflow-y: auto;
  min-height: 0;
}
.cell.outside { opacity: 0.45; }
.cell.today { border-color: var(--accent); }
.daynum {
  font-size: 0.75rem;
  color: var(--muted);
  padding: 0 0.2rem 0.15rem;
}
.cell.today .daynum { color: var(--accent); font-weight: 700; }
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
  cursor: default;
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
button:focus-visible, .cell:focus-visible, .chip:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
@media (max-width: 640px) {
  /* Two-row header: the title is centred with the sign-out control pinned to
     the right, and the view switch sits on its own row beneath. */
  header {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    row-gap: 0.4rem;
    column-gap: 0.4rem;
    padding: 0.5rem 0.6rem;
  }
  #brand { grid-column: 2; grid-row: 1; justify-self: center; }
  header form { grid-column: 3; grid-row: 1; justify-self: end; }
  .views { grid-column: 1 / -1; grid-row: 2; justify-self: center; }
  /* Navigation is by swipe on mobile, and the date already shows in the grid,
     so the stepper, period label and username are all dropped. */
  .controls, #period, #whoami { display: none; }
  .signout-icon { display: inline-block; }
  .signout-label { display: none; }
  .signout { padding: 0.35rem 0.5rem; }
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
  .cell.tappable { cursor: pointer; }
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
      <span class="signout-label">Log out</span>
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
    if (!hideTime) {
      chip.appendChild(el("span", "time", formatTime(start)));
    }
    chip.appendChild(el("span", "name", entry.title));
    return chip;
  }

  // On mobile the whole month cell is the tap target: tapping it opens that
  // day, where the full breakdown lives. Times are dropped and the chips
  // become plain labels so a whole month fits without scrolling.
  function openDay(date) {
    state.view = "day";
    state.anchor = startOfDay(date);
    syncViewButtons();
    refresh();
  }

  function buildCell(date, monthIndex) {
    var cell = el("div", "cell");
    if (date.getMonth() !== monthIndex) { cell.classList.add("outside"); }
    if (sameDay(date, new Date())) { cell.classList.add("today"); }
    var mobile = isMobile();
    if (mobile) {
      cell.classList.add("tappable");
      cell.setAttribute("role", "button");
      cell.setAttribute("tabindex", "0");
      cell.setAttribute("aria-label", date.toLocaleDateString(
        undefined, { weekday: "long", month: "long", day: "numeric" }));
      var target = date;
      cell.addEventListener("click", function () { openDay(target); });
      cell.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openDay(target);
        }
      });
    }
    cell.appendChild(el("div", "daynum", String(date.getDate())));
    var next = addDays(date, 1);
    entries.forEach(function (entry, index) {
      var start = new Date(entry.start_epoch * 1000);
      if (start >= date && start < next) {
        var chip = chipFor(entry, index, mobile);
        // The cell itself handles the tap on mobile, so the chip is not a
        // separate focus stop there.
        if (mobile) { chip.removeAttribute("tabindex"); }
        cell.appendChild(chip);
      }
    });
    return cell;
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

  function dayHeader(date, longName) {
    var head = el("div", "tg-head");
    if (sameDay(date, new Date())) { head.classList.add("today"); }
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
      grid.appendChild(dayHeader(date, days === 1));
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
    hideTooltip();
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
    } else {
      renderTimeGrid(range, state.view === "day" ? 1 : weekSpan());
    }
    renderPeriodLabel(range);
    statusLine.textContent = entries.length
      ? ""
      : "No events in this period.";
  }

  function renderPeriodLabel(range) {
    if (state.view === "month") {
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
      "Participants: " + entry.active_count + "/" + entry.capacity_total));
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
  }

  grid.addEventListener("mouseover", function (event) {
    var chip = event.target.closest(".chip");
    if (chip) { showTooltip(chip); }
  });
  grid.addEventListener("mouseout", function (event) {
    if (event.target.closest(".chip")) { hideTooltip(); }
  });
  grid.addEventListener("focusin", function (event) {
    var chip = event.target.closest(".chip");
    if (chip) { showTooltip(chip); }
  });
  grid.addEventListener("focusout", function (event) {
    if (event.target.closest(".chip")) { hideTooltip(); }
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
    + """
body {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}
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
.ranges { display: flex; gap: 0.25rem; }
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
button:disabled { opacity: 0.4; cursor: default; }
button.active { background: var(--accent); border-color: var(--accent); }
.spacer { flex: 1; }
#whoami { color: var(--muted); font-size: 0.85rem; }
header a { font-size: 0.85rem; }
header form { display: flex; }
.signout { display: inline-flex; align-items: center; gap: 0.35rem; }
.signout-icon { display: none; }
.signout-icon, .signout-icon * { pointer-events: none; }
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
  header {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    row-gap: 0.4rem;
    column-gap: 0.4rem;
    padding: 0.5rem 0.6rem;
  }
  #brand { grid-column: 2; grid-row: 1; justify-self: center; }
  header form { grid-column: 3; grid-row: 1; justify-self: end; }
  .ranges { grid-column: 1 / -1; grid-row: 2; justify-self: center; }
  #whoami { display: none; }
  .signout-icon { display: inline-block; }
  .signout-label { display: none; }
  .signout { padding: 0.35rem 0.5rem; }
  main { padding: 0.6rem 0.5rem; }
  .card { padding: 0.6rem; }
  /* Names are hidden until a swatch is tapped, leaving a compact colour key. */
  .legend .legend-name { display: none; }
  .legend .item.show-name .legend-name { display: inline; }
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
      <span class="signout-label">Log out</span>
    </button>
  </form>
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

  var state = { range: "24h", data: null, activeFeast: 0, tablePage: 0 };

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

  function formatTick(t) {
    var date = new Date(t * 1000);
    if (state.range === "24h") {
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
    // recorded sample is drawn; the series is never downsampled.
    feasts().forEach(function (feast, index) {
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
        var dot = svg("circle", {
          "class": "series-dot",
          cx: scaleX(point.t).toFixed(1),
          cy: scaleY(point.count).toFixed(1),
          r: 3,
          fill: color
        });
        var title = svg("title");
        title.textContent = feast.name + ": " + point.count +
          " at " + formatMoment(point.t);
        dot.appendChild(title);
        canvas.appendChild(dot);
      });
    });

    chart.appendChild(canvas);

    var total = feasts().reduce(function (sum, feast) {
      return sum + (feast.points ? feast.points.length : 0);
    }, 0);
    chartStatus.textContent = total
      ? ""
      : "No feast counts were recorded in this period.";
  }

  function renderLegend() {
    legend.replaceChildren();
    feasts().forEach(function (feast, index) {
      // Each entry is a button so a tap can reveal which feast a colour is
      // for; the name is always exposed to assistive tech through aria-label.
      var item = el("button", "item");
      item.type = "button";
      item.setAttribute("aria-label", feast.name);
      item.title = feast.name;
      var swatch = el("span", "swatch");
      swatch.style.background = COLORS[index % COLORS.length];
      item.appendChild(swatch);
      item.appendChild(el("span", "legend-name", feast.name));
      item.addEventListener("click", function () {
        item.classList.toggle("show-name");
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

  function syncRangeButtons() {
    document.querySelectorAll("[data-range]").forEach(function (button) {
      var active = button.getAttribute("data-range") === state.range;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function refresh() {
    chartStatus.textContent = "Loading\\u2026";
    fetch("/api/food?range=" + encodeURIComponent(state.range))
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
      .catch(function () {
        if (chartStatus.textContent === "Loading\\u2026") {
          chartStatus.textContent = "Could not load feast usage.";
        }
      });
  }

  document.querySelectorAll("[data-range]").forEach(function (button) {
    button.addEventListener("click", function () {
      state.range = button.getAttribute("data-range");
      syncRangeButtons();
      refresh();
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
