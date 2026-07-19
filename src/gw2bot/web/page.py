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
#tz { color: var(--muted); font-size: 0.78rem; }
header a { font-size: 0.85rem; }
header form { display: flex; }
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
</style>
</head>
<body>
<header>
  <h1>Guild Events</h1>
  <div class="views">
    <button type="button" data-view="day">Day</button>
    <button type="button" data-view="week">Week</button>
    <button type="button" data-view="month">Month</button>
  </div>
  <div class="controls">
    <button type="button" id="prev" aria-label="Previous">&lsaquo;</button>
    <button type="button" id="today">Today</button>
    <button type="button" id="next" aria-label="Next">&rsaquo;</button>
  </div>
  <span id="period"></span>
  <span id="tz"></span>
  <span class="spacer"></span>
  <span id="whoami"></span>
  <form method="post" action="/logout">
    <button type="submit">Log out</button>
  </form>
</header>
<main>
  <div id="grid" class="month"></div>
  <div id="status"></div>
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
      state.anchor = addDays(state.anchor, 7 * direction);
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

  function chipFor(entry, index) {
    var start = new Date(entry.start_epoch * 1000);
    var chip = el("div",
      "chip " + (statusClasses[entry.status] || "st-scheduled"));
    if (entry.status === "over") { chip.classList.add("over"); }
    if (entry.projected) { chip.classList.add("projected"); }
    chip.setAttribute("data-i", String(index));
    chip.setAttribute("tabindex", "0");
    chip.appendChild(el("span", "time", formatTime(start)));
    chip.appendChild(el("span", "name", entry.title));
    return chip;
  }

  function buildCell(date, monthIndex) {
    var cell = el("div", "cell");
    if (date.getMonth() !== monthIndex) { cell.classList.add("outside"); }
    if (sameDay(date, new Date())) { cell.classList.add("today"); }
    cell.appendChild(el("div", "daynum", String(date.getDate())));
    var next = addDays(date, 1);
    entries.forEach(function (entry, index) {
      var start = new Date(entry.start_epoch * 1000);
      if (start >= date && start < next) {
        cell.appendChild(chipFor(entry, index));
      }
    });
    return cell;
  }

  var dayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

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
      dayNames.forEach(function (name) {
        grid.appendChild(el("div", "dow", name));
      });
      for (var offset = 0; offset < 42; offset += 1) {
        grid.appendChild(buildCell(
          addDays(range.start, offset), state.anchor.getMonth()));
      }
    } else {
      renderTimeGrid(range, state.view === "day" ? 1 : 7);
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
      var last = addDays(range.start, 6);
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
      button.classList.toggle(
        "active", button.getAttribute("data-view") === state.view);
    });
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

  // Every time on this page is rendered from the event's absolute instant
  // through the browser's own clock, so name the zone that produced them.
  function timezoneLabel() {
    var zone = "";
    try {
      zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (error) {
      zone = "";
    }
    return zone ? "Times in " + zone : "Times in your local time zone";
  }
  document.getElementById("tz").textContent = timezoneLabel();

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
#tz { color: var(--muted); font-size: 0.78rem; }
header a { font-size: 0.85rem; }
header form { display: flex; }
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
.legend { display: flex; flex-wrap: wrap; gap: 0.5rem 1.25rem; margin-bottom: 0.6rem; }
.legend .item { display: flex; align-items: center; gap: 0.4rem; font-size: 0.82rem; }
.legend .swatch { width: 0.9rem; height: 0.9rem; border-radius: 3px; flex-shrink: 0; }
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
</style>
</head>
<body>
<header>
  <h1>Feast Usage</h1>
  <div class="ranges">
    <button type="button" data-range="24h">24h</button>
    <button type="button" data-range="7d">7d</button>
    <button type="button" data-range="30d">30d</button>
  </div>
  <span id="tz"></span>
  <span class="spacer"></span>
  <span id="whoami"></span>
  <a href="/">Calendar</a>
  <form method="post" action="/logout">
    <button type="submit">Log out</button>
  </form>
</header>
<main>
  <section class="card">
    <h2>Stock on hand over time</h2>
    <div id="legend" class="legend"></div>
    <div id="chart"></div>
    <div id="chart-status"></div>
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
  var VB_W = 960;
  var VB_H = 380;
  var PAD_TOP = 16;
  var PAD_RIGHT = 16;
  var PAD_BOTTOM = 32;
  var PAD_LEFT = 34;
  var PLOT_W = VB_W - PAD_LEFT - PAD_RIGHT;
  var PLOT_H = VB_H - PAD_TOP - PAD_BOTTOM;
  var X_TICKS = 6;
  var TABLE_PAGE_SIZE = 5;

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
    return PAD_LEFT + frac * PLOT_W;
  }
  function scaleY(count) {
    var value = count;
    if (value < 0) { value = 0; }
    if (value > Y_MAX) { value = Y_MAX; }
    return PAD_TOP + (1 - value / Y_MAX) * PLOT_H;
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
    chart.replaceChildren();
    var canvas = svg("svg", {
      "class": "chart-svg",
      viewBox: "0 0 " + VB_W + " " + VB_H,
      role: "img"
    });

    // Horizontal gridlines and y labels every ten counts, 0 through Y_MAX.
    for (var value = 0; value <= Y_MAX; value += 10) {
      var y = scaleY(value);
      canvas.appendChild(svg("line", {
        "class": value === 0 ? "axis" : "grid",
        x1: PAD_LEFT, y1: y, x2: PAD_LEFT + PLOT_W, y2: y
      }));
      var yLabel = svg("text", {
        "class": "y-label", x: PAD_LEFT - 6, y: y + 4
      });
      yLabel.textContent = String(value);
      canvas.appendChild(yLabel);
    }

    // Left axis, plus x labels spaced evenly across the whole window so the
    // range spans the full width even when few points were recorded.
    canvas.appendChild(svg("line", {
      "class": "axis",
      x1: PAD_LEFT, y1: PAD_TOP, x2: PAD_LEFT, y2: PAD_TOP + PLOT_H
    }));
    for (var i = 0; i <= X_TICKS; i += 1) {
      var t = state.data.since +
        (state.data.now - state.data.since) * (i / X_TICKS);
      var x = scaleX(t);
      var xLabel = svg("text", {
        "class": "x-label", x: x, y: PAD_TOP + PLOT_H + 18
      });
      xLabel.textContent = formatTick(t);
      canvas.appendChild(xLabel);
    }

    // One polyline plus point markers per feast, each in its own colour. Every
    // recorded sample is drawn; the series is never downsampled. Each plotted
    // marker is also collected so the hover can snap to it.
    var plotted = [];
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

    attachHover(canvas, plotted);
    chart.appendChild(canvas);

    var total = plotted.length;
    chartStatus.textContent = total
      ? ""
      : "No feast counts were recorded in this period.";
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

  function attachHover(canvas, plotted) {
    var columns = groupColumns(plotted);

    var crosshair = svg("line", {
      "class": "crosshair",
      y1: PAD_TOP,
      y2: PAD_TOP + PLOT_H
    });
    crosshair.style.visibility = "hidden";
    var rings = svg("g");
    var overlay = svg("rect", {
      "class": "overlay",
      x: PAD_LEFT,
      y: PAD_TOP,
      width: PLOT_W,
      height: PLOT_H
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
      var leftPct = Math.max(10, Math.min(90, emphasized.x / VB_W * 100));
      var topPct = emphasized.y / VB_H * 100;
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
    }

    overlay.addEventListener("pointermove", function (event) {
      if (!columns.length) { return; }
      var rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) { return; }
      var vbX = (event.clientX - rect.left) / rect.width * VB_W;
      var vbY = (event.clientY - rect.top) / rect.height * VB_H;
      var column = nearestColumn(vbX);
      if (column) { showHover(column, vbY); }
    });
    overlay.addEventListener("pointerleave", hideHover);
  }

  function renderLegend() {
    legend.replaceChildren();
    feasts().forEach(function (feast, index) {
      var item = el("span", "item");
      var swatch = el("span", "swatch");
      swatch.style.background = COLORS[index % COLORS.length];
      item.appendChild(swatch);
      item.appendChild(el("span", null, feast.name));
      legend.appendChild(item);
    });
  }

  function renderTabs() {
    tabs.replaceChildren();
    feasts().forEach(function (feast, index) {
      var button = el("button", null, feast.name);
      button.type = "button";
      if (index === state.activeFeast) { button.classList.add("active"); }
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
      button.classList.toggle(
        "active", button.getAttribute("data-range") === state.range);
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

  // Every time on this page is rendered from an absolute instant through the
  // browser's own clock, so name the zone that produced them.
  function timezoneLabel() {
    var zone = "";
    try {
      zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (error) {
      zone = "";
    }
    return zone ? "Times in " + zone : "Times in your local time zone";
  }
  document.getElementById("tz").textContent = timezoneLabel();

  syncRangeButtons();
  refresh();
})();
</script>
</body>
</html>
"""
)
