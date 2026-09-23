"""The guild event calendar page.

Dynamic data reaches the page only through JSON APIs and is inserted
with ``textContent`` on the client, never as an HTML string.
"""

from __future__ import annotations

from gw2bot.web.pages.shared import (
    DASHBOARD_HEADER_STYLE,
    SHARED_STYLE,
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
    + SHARED_STYLE
    + DASHBOARD_HEADER_STYLE
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
.day-link:hover, .more:hover { background: transparent; }
.day-link:hover .daynum { color: var(--accent); }
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
/* The month overflow pass toggles the hidden attribute while measuring. This
   explicit rule must outrank .chip's display:flex or every chip stays in the
   layout and the +N counter incorrectly reaches the day's total. */
.chip[hidden] { display: none; }
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
    var reqs = el("div", "desc");
    reqs.appendChild(el("div", "md-h3", "Requirements"));
    appendMarkdown(reqs, entry.requirements || "None");
    tooltip.appendChild(reqs);
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
