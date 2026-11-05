"""The guild event statistics dashboard page.

Dynamic data reaches the page only through JSON APIs and is inserted
with ``textContent`` on the client, never as an HTML string.
"""

from __future__ import annotations

from gw2bot.web.pages.shared import CHART_STATUS_LINE, chart_dashboard

EVENTS_PAGE = chart_dashboard(
    "Event Statistics",
    "events",
    style="""
.card h2 { margin-bottom: 0.6rem; }
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
/* A run's dot carries a heavier ring than the history pages' dots, because
   it sits on a single line of one colour. */
.chart-svg .series-dot { stroke-width: 2; }
/* A run's tooltip row is its title and commander, so it wraps as text rather
   than lining a figure up against the edge. */
.chart-tooltip .tip-run { overflow-wrap: anywhere; }
.chart-tooltip .tip-run .by { color: var(--muted); }
table.stats td .sub { color: var(--muted); font-size: 0.78rem; }
table.stats td.unknown { color: var(--muted); }
.pager:empty { display: none; }
.table-note { color: var(--muted); font-size: 0.85rem; margin-top: 0.6rem; }
.table-note:empty { display: none; }
@media (max-width: 640px) {
  .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.5rem; }
  .tile { padding: 0.6rem; }
  /* The date of the last run is the column that gives way on a phone: the
     event and its count are what the table is read for. */
  table.stats .last { display: none; }
}
""",
    body="""<main>
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
    <div id="chart" class="chart-box"></div>
"""
    + CHART_STATUS_LINE
    + """
  </section>
  <section class="card">
    <h2>Event runs by commander</h2>
    <div id="commanders-table"></div>
    <div id="commanders-pager" class="pager"></div>
    <div id="commanders-note" class="table-note"></div>
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
""",
    script="""
  // The cumulative line is the page's one series, drawn in the colour the
  // roster page gives its own line.
  var LINE_COLOR = "#56B4E9";
  var TABLE_PAGE_SIZE = 10;

  var CHART_METRICS = chartMetrics({ h: 520 }, { h: 320, left: 44 });

  var state = windowState({
    pages: { commanders: 0, mentees: 0, noMentee: 0, noRequirements: 0 }
  });

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

  // A running count starts at zero, so the axis does too, and it reaches at
  // least one run so an empty window still draws a readable baseline. The
  // step never drops below one run: a count has no fractions to land a
  // gridline on.
  function computeScale() {
    var high = Math.max(1, total());
    var step = Math.max(1, niceStep(high, 4));
    return { low: 0, high: step * Math.ceil(high / step), step: step };
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
    resetChart();
    if (!state.data) { chartStatus.textContent = ""; return; }
    var canvas = chartCanvas(
      "Event runs so far over the period, one step per run as it ended");
    drawGrid(canvas, state.scale, scaleY, formatCount);
    drawTimeAxis(canvas, true);

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
    drawLine(canvas, coords, LINE_COLOR);

    // Dots go on after the line so the line cannot cover their fill.
    var plotted = runMinutes().map(function (minute) {
      var point = {
        x: scaleX(minute.t), y: scaleY(minute.count), t: minute.t,
        count: minute.count, runs: minute.runs
      };
      drawDot(canvas, point.x, point.y, 4, LINE_COLOR);
      return point;
    });

    showChart(canvas, plotted, "No event runs ended in this period.");
  }

  // Every point is its own column: two runs a few minutes apart are still
  // told apart by height, because the nearest dot is picked in both
  // directions.
  function attachHover(canvas, plotted) {
    return attachChartHover(canvas, pointColumns(plotted), {
      host: chart,
      emptyReason: "no-runs",
      ringColor: function () { return LINE_COLOR; },
      fillTooltip: function (tooltip, column, point) {
        point.runs.forEach(function (run) {
          var row = el("div", "tip-run", run.title);
          row.appendChild(el("span", "by", " \\u2014 " + run.commander));
          tooltip.appendChild(row);
        });
        tooltip.appendChild(el("div", "tip-note",
          formatCount(point.count) + " " + runsWord(point.count) +
          " so far."));
      }
    });
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

  // One of the page's tables, filling the "<prefix>-table" and
  // "<prefix>-pager" boxes and paging by state.pages[key]. A table that fits
  // on one page has no pager. A column's class is its heading's too, which
  // is what lets the phone layout hide the column whole.
  function renderTable(key, prefix, rows, columns, emptyText, noun) {
    state.pages[key] = renderPagedTable({
      box: document.getElementById(prefix + "-table"),
      pager: document.getElementById(prefix + "-pager"),
      rows: rows,
      page: state.pages[key],
      size: TABLE_PAGE_SIZE,
      className: "stats",
      columns: columns,
      empty: emptyText,
      noun: noun,
      singlePagePager: false,
      go: function (page) {
        state.pages[key] = page;
        renderTables();
      }
    });
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
    fill: textFill(function (row) { return row.commander; })
  };
  var RUNS_COLUMN = {
    heading: "Runs",
    className: "num",
    fill: textFill(function (row) { return formatCount(row.runs); })
  };
  var LAST_COLUMN = {
    heading: "Last run",
    className: "last",
    fill: textFill(function (row) { return formatMoment(row.last); })
  };

  function renderTables() {
    var data = state.data || {};
    // A table of what was missing from each run says nothing when there
    // were no runs, so its empty state says that instead of claiming every
    // run had what it asks about.
    var noRuns = !data.runs;
    var NO_RUNS_TEXT = "No event runs ended in this period.";
    renderTable("commanders", "commanders",
      data.commanders || [],
      [
        COMMANDER_COLUMN,
        {
          heading: "Count",
          className: "num",
          fill: textFill(function (row) { return formatCount(row.runs); })
        }
      ],
      NO_RUNS_TEXT,
      "commanders");
    // False when Discord would not list who holds the commander role, so
    // only the commanders who led a run could be counted.
    document.getElementById("commanders-note").textContent =
      data.commanders_complete === false
        ? "Only commanders who led a run are listed: Discord did not " +
          "share the server's member list."
        : "";
    renderTable("mentees", "mentees",
      data.mentees || [],
      [
        {
          heading: "Member",
          className: "name",
          fill: textFill(function (row) { return row.name; })
        },
        {
          heading: "Asked",
          className: "num",
          fill: textFill(function (row) { return formatCount(row.asked); })
        },
        {
          heading: "Completed",
          className: "num",
          fill: textFill(function (row) { return formatCount(row.completed); })
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
    renderTable("noMentee", "no-mentee",
      data.without_mentee || [],
      [EVENT_COLUMN, COMMANDER_COLUMN, RUNS_COLUMN, LAST_COLUMN],
      noRuns
        ? NO_RUNS_TEXT
        : "Every run in this period offered a mentee slot.",
      "events");
    renderTable("noRequirements", "no-requirements",
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

  function refresh() {
    loadWindow("/api/admin/events", "event statistics",
      "Could not load the event statistics.",
      function () {
        state.pages = {
          commanders: 0, mentees: 0, noMentee: 0, noRequirements: 0
        };
        render();
      });
  }
""",
)
