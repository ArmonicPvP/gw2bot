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
body { display: flex; flex-direction: column; height: 100vh; }
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
main { flex: 1; overflow: auto; padding: 0.75rem 1rem 1rem; }
#grid { display: grid; gap: 4px; height: 100%; min-height: 24rem; }
#grid.month {
  grid-template-columns: repeat(7, minmax(6rem, 1fr));
  grid-template-rows: auto repeat(6, minmax(5.5rem, 1fr));
}
#grid.week {
  grid-template-columns: repeat(7, minmax(6rem, 1fr));
  grid-template-rows: auto minmax(20rem, 1fr);
}
#grid.day {
  grid-template-columns: minmax(12rem, 1fr);
  grid-template-rows: auto minmax(20rem, 1fr);
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
      var listItem = /^\\s*[-*]\\s+(.*)$/.exec(line);
      var quote = /^>\\s?(.*)$/.exec(line);
      var row;
      if (line.trim() === "") {
        parent.appendChild(el("div", "md-gap"));
      } else if (heading) {
        row = el("div", "md-h" + heading[1].length);
        appendInline(row, heading[2]);
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

  function buildCell(date, monthIndex, showDayNumber) {
    var cell = el("div", "cell");
    if (monthIndex !== null && date.getMonth() !== monthIndex) {
      cell.classList.add("outside");
    }
    if (sameDay(date, new Date())) { cell.classList.add("today"); }
    if (showDayNumber) {
      cell.appendChild(el("div", "daynum", String(date.getDate())));
    }
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

  function render() {
    grid.className = state.view;
    grid.replaceChildren();
    hideTooltip();
    var range = rangeFor();
    if (state.view === "day") {
      grid.appendChild(el("div", "dow", state.anchor.toLocaleDateString(
        undefined, { weekday: "long" })));
      grid.appendChild(buildCell(range.start, null, false));
    } else {
      dayNames.forEach(function (name) {
        grid.appendChild(el("div", "dow", name));
      });
      var days = state.view === "week" ? 7 : 42;
      var monthIndex =
        state.view === "month" ? state.anchor.getMonth() : null;
      for (var offset = 0; offset < days; offset += 1) {
        grid.appendChild(
          buildCell(addDays(range.start, offset), monthIndex, true));
      }
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

  readHash();
  syncViewButtons();
  refresh();
})();
</script>
</body>
</html>
"""
)
