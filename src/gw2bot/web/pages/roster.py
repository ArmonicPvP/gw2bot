"""The guild roster history dashboard page.

Dynamic data reaches the page only through JSON APIs and is inserted
with ``textContent`` on the client, never as an HTML string.
"""

from __future__ import annotations

from gw2bot.web.pages.shared import (
    CHART_MODE_JS,
    HISTORY_JS,
    HISTORY_STYLE,
    chart_dashboard,
    history_main,
)

ROSTER_PAGE = chart_dashboard(
    "Guild Roster",
    "roster",
    style=HISTORY_STYLE
    + """
/* Who did the kicking is an account name too, so it is bounded the same way. */
table.changes td.by { overflow-wrap: anywhere; }
table.changes td.change { white-space: nowrap; }
table.changes .dot {
  display: inline-block;
  width: 0.6rem;
  height: 0.6rem;
  border-radius: 50%;
  margin-right: 0.4rem;
}
.note {
  color: var(--muted);
  font-size: 0.85rem;
  margin: 0 0 0.75rem;
}
/* A Discord display name has no spaces to break at either, so it is bounded
   the same way an account name is. */
table.changes td.discord { overflow-wrap: anywhere; }
/* A short date is two words with a space in the middle, and wrapping at it
   would cost the row a second line for the sake of three characters. The cell
   is also the positioning context for the box the date opens. */
table.changes td.invited { position: relative; white-space: nowrap; }
/* An account with no recorded invitation has no date to show, so the word
   standing in for one reads as an absence. */
table.changes td.undated { color: var(--muted); }
/* The date is a button rather than plain text because a tooltip a mouse has
   to hover reaches nobody on a phone and nobody working by keyboard. It is
   styled back down to the text it replaced, keeping the dotted underline that
   says there is more behind it. */
table.changes .tip-trigger {
  appearance: none;
  -webkit-appearance: none;
  background: none;
  border: 0;
  margin: 0;
  padding: 0;
  color: inherit;
  font: inherit;
  cursor: pointer;
  text-decoration: underline dotted;
  text-underline-offset: 0.2rem;
}
/* Hangs below its cell rather than over the next row's text, and grows to the
   left from the cell's right edge, which is what keeps the last column's box
   inside the card on a phone. */
.cell-tip {
  position: absolute;
  z-index: 3;
  top: calc(100% - 0.25rem);
  right: 0;
  width: max-content;
  max-width: min(15rem, 62vw);
  padding: 0.4rem 0.55rem;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 0.78rem;
  color: var(--text);
  white-space: normal;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.35);
}
/* Above its cell instead, for a row with no room under it. */
.cell-tip.above { top: auto; bottom: calc(100% - 0.25rem); }
/* An invited account that no application post matched has no Discord name to
   show, and the reason reads as an absence rather than as a name. */
table.changes td.unmatched { color: var(--muted); }
@media (max-width: 640px) {
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
""",
    body=history_main(
        "Guild members over time",
        "now-count",
        "Change kinds",
        "Membership changes",
        after="""  <section class="card">
    <h2>Pending invites<span id="pending-count" class="now"></span></h2>
    <p class="note">These accounts have been invited in-game but have not
      accepted yet, so they hold a place against the guild's 500 without
      being members.</p>
    <div id="pending"></div>
    <div id="pending-status" class="status-line" role="status"
      aria-live="polite"></div>
  </section>
""",
    ),
    parts=(CHART_MODE_JS, HISTORY_JS),
    script="""
  // Okabe-Ito colourblind-safe palette: one hue per kind of change, and a
  // fourth for the member count line itself.
  var KINDS = {
    join: { color: "#009E73", label: "Joined" },
    leave: { color: "#E69F00", label: "Left" },
    kick: { color: "#D55E00", label: "Kicked" }
  };
  var KIND_ORDER = ["join", "leave", "kick"];
  var LINE_COLOR = "#56B4E9";
  var TABLE_PAGE_SIZE = 10;
  // Smallest number of members the y axis ever spans, so a quiet week does not
  // turn a single departure into a cliff.
  var MIN_SPAN = 6;
  // The most members a guild can hold, so the y axis never reaches past it.
  var CEILING = 500;

  var CHART_METRICS = chartMetrics({}, {});

  var state = windowState({ tablePage: 0, staircase: false });

  var nowCount = document.getElementById("now-count");
  var pendingBox = document.getElementById("pending");
  var pendingStatus = document.getElementById("pending-status");
  var pendingCount = document.getElementById("pending-count");

  function events() { return (state.data && state.data.events) || []; }

  function kindOf(kind) { return KINDS[kind] || KINDS.leave; }

  // The axis covers the counts actually reached in the window, padded out to
  // MIN_SPAN and rounded to whole members, so the line uses the full height
  // instead of hugging the ceiling. It never reaches below an empty guild or
  // above the CEILING a guild can hold.
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
    // Space that would fall outside the limits moves to the other side, so a
    // full or empty guild keeps the same span as any other.
    if (high > CEILING) { low -= high - CEILING; high = CEILING; }
    if (low < 0) { high = Math.min(CEILING, high - low); low = 0; }
    var step = Math.max(1, Math.ceil((high - low) / 4));
    low = Math.floor(low / step) * step;
    high = low + step * Math.ceil((high - low) / step);
    if (high > CEILING) {
      // Rounding overshot the ceiling: count the ticks down from it instead.
      high = CEILING;
      low = CEILING - step * Math.ceil((CEILING - low) / step);
      if (low < 0) { low = 0; step = CEILING / 4; }
    }
    return { low: low, high: high, step: step };
  }

  // "Jun 17": as much of a moment as a narrow column has room for. What it
  // leaves out is on the cell's tooltip rather than lost.
  function formatShortDate(t) {
    return new Date(t * 1000).toLocaleDateString(
      undefined, { month: "short", day: "numeric" });
  }
  function countedAge(value, unit) {
    return value + " " + unit + (value === 1 ? "" : "s") + " ago";
  }
  // "3 minutes ago", "7 days ago". Coarse on purpose: the moment itself is
  // beside it, and what an age is read for is how stale something is rather
  // than a count to the second. Days are the largest unit, so a wait of weeks
  // is still counted in a unit a reader can compare against the 14-day Trial
  // clock without converting it back.
  function formatAge(seconds) {
    if (seconds < 60) { return "just now"; }
    if (seconds < 3600) {
      return countedAge(Math.floor(seconds / 60), "minute");
    }
    if (seconds < 86400) {
      return countedAge(Math.floor(seconds / 3600), "hour");
    }
    return countedAge(Math.floor(seconds / 86400), "day");
  }
  // The same moment, with the year when it is not this one. An invitation
  // can sit unanswered across New Year, and "Jun 17, 9:30 PM" would then be
  // read as this June rather than last.
  function formatMomentWithYear(t) {
    var date = new Date(t * 1000);
    if (date.getFullYear() === new Date().getFullYear()) {
      return formatMoment(t);
    }
    return date.toLocaleString(
      undefined,
      {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit"
      });
  }
  // "Sep 20, 9:30 PM | 7 days ago": the moment the membership table shows in
  // full, and how long ago it was by the reader's own clock. A clock running
  // a little behind the server's would otherwise date an invite in the
  // future, which formatAge() reads as "just now" rather than as a negative
  // age.
  function formatMomentWithAge(t) {
    return formatMomentWithYear(t) + " | " +
      formatAge(Math.max(0, Math.floor(Date.now() / 1000 - t)));
  }


  function renderChart() {
    resetChart();
    if (!state.scale) {
      chartStatus.textContent = state.data
        ? "No guild member count has been recorded yet, so the roster line " +
          "cannot be drawn. The changes below are still listed."
        : "";
      return;
    }
    var canvas = chartCanvas(
      "Guild member count over time, with one dot per change");
    drawGrid(canvas, state.scale, scaleY, String);
    drawTimeAxis(canvas, true);

    // Regular mode connects recorded counts directly. Staircase mode inserts
    // the previous count at each new timestamp before drawing the jump.
    var coords = [];
    points().forEach(function (point, index) {
      var px = scaleX(point.t);
      var py = scaleY(point.count);
      if (state.staircase && index > 0) {
        coords.push(px.toFixed(1) + "," +
          scaleY(points()[index - 1].count).toFixed(1));
      }
      coords.push(px.toFixed(1) + "," + py.toFixed(1));
    });
    if (coords.length > 1) { drawLine(canvas, coords, LINE_COLOR); }

    // One dot per change, in its kind's colour. Every change is drawn; the
    // series is never downsampled. Each dot is collected so the hover can
    // snap to it.
    var plotted = [];
    events().forEach(function (event) {
      if (event.count === null) { return; }
      var px = scaleX(event.t);
      var py = scaleY(event.count);
      drawDot(canvas, px, py, 4, kindOf(event.kind).color);
      plotted.push({ x: px, y: py, t: event.t, event: event });
    });

    showChart(canvas, plotted, "No members joined or left in this period.");
  }

  bindChartMode(function () { return points().length; }, renderChart);

  function attachHover(canvas, plotted) {
    return attachChartHover(canvas, columnsByTime(plotted), {
      host: chart,
      emptyReason: "no-changes",
      ringColor: function (point) { return kindOf(point.event.kind).color; },
      fillTooltip: function (tooltip, column, emphasized) {
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
      }
    });
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
    renderKeyLegend(KIND_ORDER.map(function (kind) { return KINDS[kind]; }));
  }

  function renderTotals() {
    renderTotalsLine([
      [String(state.data.joins || 0), "joined"],
      [String(state.data.leaves || 0), "left"],
      [String(state.data.kicks || 0), "kicked"]
    ]);
    nowCount.textContent = state.data.member_count === null ||
      state.data.member_count === undefined
      ? ""
      : closingFigure(String(state.data.member_count));
  }

  var CHANGE_COLUMNS = [
    TIME_COLUMN,
    ACCOUNT_COLUMN,
    {
      heading: "Change",
      className: "change",
      fill: function (cell, change) {
        var dot = el("span", "dot");
        dot.style.background = kindOf(change.kind).color;
        cell.appendChild(dot);
        // The name of the change is its own element so the phone layout can
        // put it out of sight and leave the dot standing for it.
        cell.appendChild(
          el("span", "change-label", kindOf(change.kind).label));
      }
    },
    {
      heading: "By",
      className: "by",
      fill: textFill(function (change) { return change.actor || ""; })
    },
    {
      heading: "Members",
      className: "num",
      fill: textFill(function (change) {
        return change.count === null ? "" : String(change.count);
      })
    }
  ];

  function renderTable() {
    renderHistoryTable(events(), CHANGE_COLUMNS,
      "No members joined or left in this period.", "changes");
  }

  // Sanitized tracing for the pending invite section: a fixed action name and
  // a count of rows. No account name, Discord name or payload is ever passed.
  function tracePending(action, count) {
    console.debug("roster pending invites:", action, count);
  }

  // The moment-and-age box the "Invite sent" column opens: one node, moved
  // into whichever cell is showing it. A title attribute was a desktop-only
  // answer - a phone has no hover to give, and neither has a keyboard.
  var inviteTip = el("div", "cell-tip");
  inviteTip.id = "invite-tip";
  inviteTip.setAttribute("role", "tooltip");
  var inviteTipTrigger = null;

  // Sanitized tracing for the box, so a console trace can explain why one
  // opened or went away: a fixed action name and, for a dismissal, one of the
  // narrowed event names above. No date, account name or payload is passed.
  function traceInviteTip(action, reason) {
    console.debug("roster invite tip:", action, reason || "");
  }

  function showInviteTip(trigger, text) {
    hideInviteTip("replaced");
    inviteTip.textContent = text;
    trigger.parentNode.appendChild(inviteTip);
    placeInviteTip(trigger);
    // Described by the box only while the box is on screen, which is what a
    // screen reader expects of a tooltip.
    trigger.setAttribute("aria-describedby", inviteTip.id);
    inviteTipTrigger = trigger;
    document.addEventListener("pointerdown", dismissInviteTip, true);
    document.addEventListener("wheel", dismissInviteTip, true);
    document.addEventListener("scroll", dismissInviteTip, true);
    document.addEventListener("keydown", dismissInviteTip, true);
    window.addEventListener("blur", dismissInviteTip);
    window.addEventListener("resize", dismissInviteTip);
    traceInviteTip("open");
  }

  // A box below the last rows of a phone screen would hang off the bottom,
  // and the reader cannot scroll it into view because scrolling is one of the
  // things that puts it away. It opens upwards instead, unless there is no
  // room up there either, where below is still the lesser of the two.
  function placeInviteTip(trigger) {
    inviteTip.classList.remove("above");
    var viewport = window.innerHeight ||
      document.documentElement.clientHeight;
    var box = inviteTip.getBoundingClientRect();
    if (box.bottom <= viewport) { return; }
    var cell = trigger.getBoundingClientRect();
    if (cell.top - box.height >= 0) { inviteTip.classList.add("above"); }
  }

  function hideInviteTip(reason) {
    if (!inviteTipTrigger) { return; }
    inviteTipTrigger.removeAttribute("aria-describedby");
    inviteTipTrigger = null;
    inviteTip.remove();
    document.removeEventListener("pointerdown", dismissInviteTip, true);
    document.removeEventListener("wheel", dismissInviteTip, true);
    document.removeEventListener("scroll", dismissInviteTip, true);
    document.removeEventListener("keydown", dismissInviteTip, true);
    window.removeEventListener("blur", dismissInviteTip);
    window.removeEventListener("resize", dismissInviteTip);
    traceInviteTip("close", reason);
  }

  // Anything but the box itself closes it: a tap or click elsewhere on the
  // page, a scroll either way, a wheel, a key, a resize, or the window losing
  // focus.
  function dismissInviteTip(event) {
    var target = event && event.target;
    // A press inside the box is someone reading it, not dismissing it, so it
    // is the one place a press leaves it standing.
    if (target && target.nodeType && inviteTip.contains(target)) { return; }
    // A press on a date is left to that date's own click, which opens its box
    // or closes the one it opened. Closing here first would let that click
    // reopen what the press meant to dismiss.
    if (event && event.type === "pointerdown" && target && target.closest &&
        target.closest(".tip-trigger")) {
      return;
    }
    // Enter and Space on the open date reach here before the click they turn
    // into, and are left to it for the same reason. Every other key, Escape
    // and Tab included, closes the box.
    if (event && event.type === "keydown" && target === inviteTipTrigger &&
        (event.key === "Enter" || event.key === " " ||
          event.key === "Spacebar")) {
      return;
    }
    hideInviteTip("page-" + eventKind(event));
  }

  // The cell for an invitation that has a date: the short date as a button,
  // which opens the box on a tap, a click or a keypress and on a mouse's
  // hover. The age is worked out as the box opens rather than as the row is
  // drawn, so a page left open overnight cannot still call the invitation
  // three minutes old.
  function inviteSentCell(sentAt) {
    var cell = el("td", "invited");
    var trigger = el("button", "tip-trigger", formatShortDate(sentAt));
    trigger.type = "button";
    trigger.addEventListener("click", function () {
      if (inviteTipTrigger === trigger) {
        hideInviteTip("toggle");
        return;
      }
      showInviteTip(trigger, formatMomentWithAge(sentAt));
    });
    trigger.addEventListener("pointerenter", function (event) {
      if (isHoverPointer(event)) {
        showInviteTip(trigger, formatMomentWithAge(sentAt));
      }
    });
    trigger.addEventListener("pointerleave", function (event) {
      // A finger's pointerleave arrives as it lifts off the glass, and acting
      // on it would close the box the tap had just opened.
      if (isHoverPointer(event) && inviteTipTrigger === trigger) {
        hideInviteTip("pointer-leave");
      }
    });
    cell.appendChild(trigger);
    return cell;
  }

  function renderPending(invites, matched) {
    // The rows the open box was anchored to are about to be replaced, and a
    // box left behind would hang off a cell that is no longer on the page.
    hideInviteTip("redraw");
    pendingBox.replaceChildren();
    pendingCount.textContent = invites.length
      ? "\\u2014 " + invites.length + " waiting"
      : "";
    if (!invites.length) {
      pendingBox.appendChild(el("div", "empty",
        "No invites are waiting to be accepted."));
      return;
    }
    // Its own class, because the membership table's "By" column is hidden on
    // a phone and this table's second column is the section's whole point.
    var table = el("table", "data-table changes pending");
    var head = el("tr");
    head.appendChild(el("th", null, "Account"));
    head.appendChild(el("th", null, "Discord"));
    head.appendChild(el("th", null, "Invite sent"));
    table.appendChild(head);
    invites.forEach(function (invite) {
      var row = el("tr");
      row.appendChild(el("td", "name", invite.name));
      // An account nobody matched to an application post is named as
      // unmatched rather than left blank, so the empty cell cannot read as a
      // Discord account with no name. When the forum could not be read in
      // full, a row without a name may be missing it only because the post
      // naming it was one of the unread ones, and "no application matched"
      // would be an assertion the server never made. A name that is shown was
      // matched either way.
      row.appendChild(invite.discord_name
        ? el("td", "discord", invite.discord_name)
        : el("td", "discord unmatched", matched
          ? "No application matched"
          : "Could not be checked"));
      // The column has room for the date alone, so the rest of the moment and
      // how long ago it was are in the box the date opens. An invitation the
      // bot never recorded a guild-log event for has no date to show, and the
      // row says so rather than standing in a date of its own.
      row.appendChild(typeof invite.invited_at === "number"
        ? inviteSentCell(invite.invited_at)
        : el("td", "invited undated", "Unknown"));
      table.appendChild(row);
    });
    pendingBox.appendChild(table);
  }

  // The pending invites are the guild's state right now rather than a window
  // of history, so they are loaded once with the page and are not reloaded
  // when the range changes.
  function loadPending() {
    pendingStatus.textContent = "Loading\\u2026";
    fetchJson("/api/pending")
      .then(function (payload) {
        if (!payload.available) {
          pendingBox.replaceChildren();
          pendingCount.textContent = "";
          // The server names the settings that are unset, so the reader is
          // told which /settings subcommands turn the section on rather than
          // being sent to the README for them.
          var missing = payload.missing || [];
          pendingStatus.textContent = missing.length
            ? "The pending invites are off until " +
              missing.map(function (name) { return "/settings " + name; })
                .join(" and ") + " " +
              (missing.length === 1 ? "is" : "are") + " set."
            : "The pending invites are off until the Guild Wars 2 API " +
              "settings are set.";
          tracePending("unavailable", missing.length);
          return;
        }
        var invites = payload.invites || [];
        var matched = payload.matched !== false;
        renderPending(invites, matched);
        pendingStatus.textContent = matched
          ? ""
          : "The Trial application forum could not be read in full, so an " +
            "account without a Discord name here may still have applied.";
        tracePending(matched ? "render" : "unmatched", invites.length);
      })
      .catch(function (error) {
        // renderPending() runs inside this chain, so a drawing fault lands
        // here too.
        logFailure("pending invite load", error);
        pendingStatus.textContent = "Could not load the pending invites.";
      });
  }

  function refresh() {
    loadHistory("/api/roster", "roster history",
      "Could not load the roster history.");
  }
""",
    start="""
  loadPending();
""",
)
