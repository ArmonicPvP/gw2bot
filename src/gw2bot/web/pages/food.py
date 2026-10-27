"""The feast usage dashboard page.

Dynamic data reaches the page only through JSON APIs and is inserted
with ``textContent`` on the client, never as an HTML string.
"""

from __future__ import annotations

from gw2bot.web.pages.shared import (
    CHART_MODE_BUTTON,
    CHART_MODE_JS,
    CHART_STATUS_LINE,
    COIN_JS,
    UTILITY_STYLE,
    chart_dashboard,
)


def _daily_card(
    chart_id: str,
    heading: str,
    note: str,
    *,
    legend: bool = True,
) -> str:
    """One of the daily charts below the stock graph.

    Each is a card with its heading, the line saying what it plots, its box,
    a legend of its own unless it sums the others, and its status line.
    """
    legend_box = (
        f'    <div id="legend-{chart_id}" class="legend" role="list"\n'
        '      aria-label="Feast colours"></div>\n'
        if legend
        else ""
    )
    return f"""  <section class="card">
    <div class="chart-heading">
      <h2>{heading}</h2>
    </div>
    <p class="chart-note">{note}</p>
    <div id="chart-{chart_id}" class="chart-box"></div>
{legend_box}    <div id="status-{chart_id}" class="status-line" role="status"
      aria-live="polite"></div>
  </section>
"""


FOOD_PAGE = chart_dashboard(
    "Feast Usage",
    "feast",
    style=UTILITY_STYLE
    + """
/* Each legend swatch is a button, so a tap can switch the feast it stands
   for, and its square corners set the feasts apart from the round dots of
   the history pages. */
.legend .item {
  background: none;
  border: none;
  padding: 0.2rem 0.25rem;
  cursor: pointer;
}
.legend .swatch { border-radius: 3px; }
.legend .legend-name { display: inline; }
/* A feast switched off keeps its place in the legend, dimmed and with its
   colour reduced to an outline, so what is missing from the chart is still
   named and one more click puts it back. */
.legend .item.off { opacity: 0.55; }
/* One line under a heading saying what the chart below it plots, because a
   rolling average and a daily total look alike and are not. */
.chart-note {
  color: var(--muted);
  font-size: 0.8rem;
  margin: -0.35rem 0 0.6rem;
}
/* A feast tooltip keeps its own sizing: it is not capped at the shared width,
   so a long feast name has room beside its count. */
.chart-tooltip { min-width: 8rem; max-width: none; }
.chart-tooltip .tip-row .swatch { border-radius: 2px; }
.tabs { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-bottom: 0.75rem; }
.tabs button { font-size: 0.8rem; }
/* Additions are the removals table with three more columns, so they add only
   what the extra columns need. */
/* Six narrow columns read as a row, so only the account names wrap; the
   rest keep their line and the table scrolls instead. */
table.additions th, table.additions td { white-space: nowrap; }
table.additions td.user { white-space: normal; }
table.additions th.actions, table.additions td.actions { text-align: right; }
/* A restock nobody has priced yet is the row an officer opened this section
   to act on, so it is tinted rather than left to be found by reading down
   the column. */
table.additions tr.unpriced td { background: rgba(231, 76, 60, 0.22); }
/* Six columns are more than a phone has room for, so the table scrolls
   sideways inside its card. */
.table-scroll table { min-width: 34rem; }
/* The cost editor. The shared reset zeroes every margin, which takes the
   centring a modal dialog normally gets from the user agent with it. */
.cost-dialog {
  margin: auto;
  width: min(22rem, calc(100vw - 2rem));
  padding: 1rem;
  background: var(--panel);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 10px;
}
.cost-dialog::backdrop { background: rgba(0, 0, 0, 0.55); }
.cost-dialog h2 { font-size: 1rem; margin-bottom: 0.35rem; }
.cost-subject { color: var(--muted); font-size: 0.82rem; }
/* The three boxes read as one price: {n}g {n}s {n}c on a single line. */
.cost-row {
  display: flex;
  align-items: baseline;
  justify-content: center;
  gap: 0.5rem;
  margin: 0.9rem 0 0.4rem;
}
.coin-field { display: inline-flex; align-items: baseline; gap: 0.15rem; }
.coin-field input {
  width: 4.5rem;
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.35rem 0.4rem;
  font: inherit;
  font-size: 0.9rem;
  text-align: right;
  font-variant-numeric: tabular-nums;
  color-scheme: dark;
}
/* The spinner arrows sit on top of a three-digit figure in a box this
   narrow, and a price is typed rather than stepped to. */
.coin-field input {
  appearance: textfield;
  -moz-appearance: textfield;
}
.coin-field input::-webkit-outer-spin-button,
.coin-field input::-webkit-inner-spin-button {
  -webkit-appearance: none;
  margin: 0;
}
.coin-unit { color: var(--muted); font-size: 0.85rem; }
.cost-error { color: var(--full); font-size: 0.82rem; min-height: 1.2em; }
.cost-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-top: 0.6rem;
}
@media (max-width: 640px) {
  /* Names are hidden, leaving a compact colour key. A feast switched off
     names itself, so a tap still answers "which one is this?" - it answers by
     taking the line away and labelling what went. */
  .legend .legend-name { display: none; }
  .legend .item.off .legend-name { display: inline; }
}
""",
    body="""<main>
  <section class="card">
    <div class="chart-heading">
      <h2>Stock on hand over time</h2>
"""
    + CHART_MODE_BUTTON
    + """
    </div>
    <div id="chart" class="chart-box"></div>
    <div id="legend" class="legend" role="list" aria-label="Feast colours"></div>
"""
    + CHART_STATUS_LINE
    + """
  </section>
"""
    + _daily_card(
        "usage-avg",
        "Food usage &ndash; 7-day rolling average",
        """Feasts taken per day, averaged over the seven days
      ending on each date. Days are cut in UTC.""",
    )
    + _daily_card(
        "cost-avg",
        "Food cost &ndash; 7-day rolling average",
        """What the feasts used each day had cost, averaged
      over the seven days ending on each date.""",
    )
    + _daily_card(
        "cost-by-day",
        "Food cost by day",
        """What the feasts used each day had cost, with no
      averaging.""",
    )
    + _daily_card(
        "total-cost",
        "Total food cost",
        """What every drawn feast used in the window had cost,
      added up. Each point is the running total to the end of that day.""",
        legend=False,
    )
    + _daily_card(
        "cost-by-food",
        "Food cost by food",
        """The same running total, one line per feast, so the
      shelf the gold went on is the line it is read off.""",
    )
    + """
  <section class="card">
    <h2>Removals</h2>
    <div id="tabs" class="tabs"></div>
    <div id="table"></div>
    <div id="pager" class="pager"></div>
  </section>
  <section class="card">
    <h2>Additions</h2>
    <div id="add-tabs" class="tabs"></div>
    <div id="add-table"></div>
    <div id="add-pager" class="pager"></div>
  </section>
</main>
<dialog id="cost-dialog" class="cost-dialog" aria-labelledby="cost-title">
  <form id="cost-form">
    <h2 id="cost-title">Cost</h2>
    <p id="cost-subject" class="cost-subject"></p>
    <div class="cost-row">
      <span class="coin-field">
        <label class="visually-hidden" for="cost-gold">Gold</label>
        <input id="cost-gold" type="number" min="0" step="1"
          inputmode="numeric" autocomplete="off">
        <span class="coin-unit" aria-hidden="true">g</span>
      </span>
      <span class="coin-field">
        <label class="visually-hidden" for="cost-silver">Silver</label>
        <input id="cost-silver" type="number" min="0" step="1"
          inputmode="numeric" autocomplete="off">
        <span class="coin-unit" aria-hidden="true">s</span>
      </span>
      <span class="coin-field">
        <label class="visually-hidden" for="cost-copper">Copper</label>
        <input id="cost-copper" type="number" min="0" step="1"
          inputmode="numeric" autocomplete="off">
        <span class="coin-unit" aria-hidden="true">c</span>
      </span>
    </div>
    <p id="cost-error" class="cost-error" role="status" aria-live="polite"></p>
    <div class="cost-actions">
      <button type="button" id="cost-cancel">Cancel</button>
      <button type="submit" id="cost-save">Save</button>
    </div>
  </form>
</dialog>
""",
    parts=(COIN_JS, CHART_MODE_JS),
    script="""
  // Okabe-Ito colourblind-safe categorical palette, one hue per tracked feast.
  var COLORS = ["#56B4E9", "#E69F00", "#009E73", "#CC79A7"];
  var TABLE_PAGE_SIZE = 5;
  // The ceiling the server holds a recorded cost to, mirrored here so a
  // mistyped figure is named at the keyboard rather than coming back as a
  // rejected save.
  var MAX_COST_COPPER = 2000000 * COPPER_PER_GOLD;
  // Smallest count the y axis ever reaches, so a window that never rose above
  // a couple of feasts still gets readable gridlines rather than a scale
  // squeezed onto one or two of them.
  var MIN_TOP = 10;

  // The left margin matches the roster page's, because the axis is no longer
  // capped at two digits and a four-figure stock has to fit beside it.
  var CHART_METRICS = chartMetrics({}, {});

  // hidden holds the feasts the reader has switched off in the legend, keyed
  // by guild storage id so the choice outlives a range change and the redraw
  // it brings.
  var state = windowState({
    activeFeast: 0, tablePage: 0, hidden: {}, staircase: false,
    // The Additions table keeps its own feast and page: an officer pricing a
    // restock is reading a different question than the removals above it.
    activeAddition: 0, additionPage: 0,
    // The restock the cost dialog is open over, or null while it is closed.
    editing: null
  });

  var legend = document.getElementById("legend");
  var tabs = document.getElementById("tabs");
  var tableBox = document.getElementById("table");
  var pager = document.getElementById("pager");
  var addTabs = document.getElementById("add-tabs");
  var addTableBox = document.getElementById("add-table");
  var addPager = document.getElementById("add-pager");
  var costDialog = document.getElementById("cost-dialog");
  var costForm = document.getElementById("cost-form");
  var costSubject = document.getElementById("cost-subject");
  var costError = document.getElementById("cost-error");
  var costSave = document.getElementById("cost-save");
  var costFields = {
    gold: document.getElementById("cost-gold"),
    silver: document.getElementById("cost-silver"),
    copper: document.getElementById("cost-copper")
  };

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

  // The axis covers the counts actually reached in the window, padded out to
  // MIN_TOP and rounded up to a readable step, so a stock the guild has grown
  // past fifty is drawn in full instead of flattened against a fixed ceiling.
  // The baseline stays at zero: several feasts share the axis, and running out
  // is the thing the page is read for, so an empty shelf has to sit on the
  // floor of the chart rather than somewhere up its side.
  function computeScale() {
    var high = MIN_TOP;
    visibleFeasts().forEach(function (feast) {
      (feast.points || []).forEach(function (point) {
        if (point.count > high) { high = point.count; }
      });
    });
    // Headroom above the highest sample, so the peak of a line is not drawn
    // on the topmost gridline.
    var span = high * 1.1;
    var step = niceStep(span, 6);
    return { low: 0, high: step * Math.ceil(span / step), step: step };
  }

  function renderChart() {
    resetChart();
    var canvas = chartCanvas("Stock on hand over time, one line per feast");
    drawGrid(canvas, state.scale, scaleY, String);
    // This chart has always centred its outermost time labels.
    drawTimeAxis(canvas, false);

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
        var coords = [];
        points.forEach(function (point, pointIndex) {
          var px = scaleX(point.t);
          if (state.staircase && pointIndex > 0) {
            coords.push(px.toFixed(1) + "," +
              scaleY(points[pointIndex - 1].count).toFixed(1));
          }
          coords.push(px.toFixed(1) + "," +
            scaleY(point.count).toFixed(1));
        });
        drawLine(canvas, coords, color);
      }
      points.forEach(function (point) {
        var px = scaleX(point.t);
        var py = scaleY(point.count);
        drawDot(canvas, px, py, 3, color);
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

    showChart(canvas, plotted, chartStatusText(plotted.length));
  }

  bindChartMode(function () { return feasts().length; }, function () {
    if (state.data) { renderChart(); }
  });

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
    var columns = columnsByTime(plotted);
    columns.forEach(function (column) {
      column.points.sort(function (a, b) { return a.feast - b.feast; });
    });
    return columns;
  }

  // host is the positioning box the tooltip is placed in and headline names a
  // column; both default to the stock chart's, so its own call site reads the
  // way it always has while the daily charts below pass their own. Every
  // chart on this page picks the column nearest in x alone, as it always has.
  function attachHover(canvas, plotted, host, headline) {
    return attachChartHover(canvas, groupColumns(plotted), {
      host: host || chart,
      headline: headline || formatMoment,
      emptyReason: "no-samples",
      nearestByX: true,
      ringRadius: 5,
      ringColor: function (point) { return point.color; },
      fillTooltip: function (tooltip, column, emphasized) {
        column.points.forEach(function (point) {
          var row = el("div",
            "tip-row" + (point === emphasized ? " em" : ""));
          var swatch = el("span", "swatch");
          swatch.style.background = point.color;
          row.appendChild(swatch);
          row.appendChild(el("span", "name", point.name));
          row.appendChild(el("span", "val", point.text !== undefined
            ? point.text
            : String(point.count)));
          tooltip.appendChild(row);
        });
      }
    });
  }

  // Sanitized tracing for the legend, so a console trace can explain why the
  // chart is drawing fewer lines than the window holds. Only a fixed action
  // name and a count of the feasts left on are passed; no feast name, count
  // or timestamp reaches the console.
  function traceLegend(action, count) {
    console.debug("feast chart legend:", action, count);
  }

  // host is the box the swatches are drawn into, defaulting to the stock
  // chart's legend. Every legend on the page switches the same feasts, so a
  // click redraws all of them along with the charts they belong to.
  function renderLegend(host) {
    var box = host || legend;
    box.replaceChildren();
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
        renderDailyCharts();
      });
      box.appendChild(item);
    });
  }

  // --- The daily charts ---------------------------------------------------

  // Every chart below the stock graph is the same drawing: one point per UTC
  // day of the drawn window, read off an axis the values themselves set. Each
  // entry names the section it fills, the value it takes from a day, and
  // whether that value is a price, which decides both the axis labels and the
  // reading in the tooltip.
  //
  // The days arrive bucketed and averaged: a seven-day average on the
  // window's first day is worked out over the week before it, which is a week
  // the page is never sent.
  var DAILY_CHARTS = [
    {
      id: "usage-avg",
      label: "Feasts used per day, 7-day rolling average, one line per feast",
      empty: "No feasts were used in this period.",
      money: false,
      value: function (day) { return day.used_avg; }
    },
    {
      id: "cost-avg",
      label: "Cost of the feasts used per day, 7-day rolling average, " +
        "one line per feast",
      empty: "No feasts were used in this period.",
      money: true,
      value: function (day) { return day.cost_avg; }
    },
    {
      id: "cost-by-day",
      label: "Cost of the feasts used each day, one line per feast",
      empty: "No feasts were used in this period.",
      money: true,
      value: function (day) { return day.cost; }
    },
    {
      id: "total-cost",
      label: "Running total of the cost of feasts used, across every " +
        "drawn feast",
      empty: "No feasts were used in this period.",
      money: true,
      total: true,
      value: function (day) { return day.cost; }
    },
    {
      id: "cost-by-food",
      label: "Running total of the cost of feasts used, one line per feast",
      empty: "No feasts were used in this period.",
      money: true,
      cumulative: true,
      value: function (day) { return day.cost; }
    }
  ];

  // The total line stands for every feast at once, so it takes a hue none of
  // them are drawn in; it is the fifth Okabe-Ito colour.
  var TOTAL_COLOR = "#D55E00";
  var SECONDS_PER_DAY = 24 * 60 * 60;

  // Each daily chart's hover teardown, so a redraw drops the page-level
  // listeners a pinned selection left behind on the canvas it replaces.
  var dailyHandles = {};

  function daysOf(feast) {
    return (feast && feast.days) || [];
  }

  // The day grid the window was bucketed into. The server cuts one grid for
  // the whole window and gives every feast all of it, quiet days included, so
  // the longest series any feast carries is that grid.
  function dayGrid() {
    var grid = [];
    feasts().forEach(function (feast) {
      var days = daysOf(feast);
      if (days.length > grid.length) {
        grid = days.map(function (day) { return day.t; });
      }
    });
    return grid;
  }

  // A day is named by the UTC date it opens, the way the server cut it, so a
  // reader west of Greenwich is not shown the day before.
  function formatDay(t) {
    return new Date(t * 1000).toLocaleDateString(
      undefined, { month: "short", day: "numeric", timeZone: "UTC" });
  }

  // Money on an axis is written in the largest coin it reaches, so the label
  // stays short enough to sit beside the plot. The baseline is bare: "0c"
  // reads as a price where the floor of the axis is not one.
  function formatCoinAxis(copper) {
    if (copper <= 0) { return "0"; }
    if (copper >= COPPER_PER_GOLD) {
      var gold = copper / COPPER_PER_GOLD;
      var shown = gold >= 10 ? Math.round(gold) : Math.round(gold * 10) / 10;
      return shown.toLocaleString() + "g";
    }
    if (copper >= COPPER_PER_SILVER) {
      return Math.round(copper / COPPER_PER_SILVER) + "s";
    }
    return Math.round(copper) + "c";
  }

  // How many decimals an axis needs to tell its own gridlines apart: a step
  // of 2 reads as whole feasts, a step of 0.2 does not.
  function decimalsFor(step) {
    if (step >= 1) { return 0; }
    return Math.min(3, Math.ceil(-Math.log10(step)));
  }

  function formatAxisValue(spec, value, decimals) {
    if (spec.money) { return formatCoinAxis(value); }
    return value.toFixed(decimals);
  }

  // What one plotted point reads as in the tooltip: a price in coins, and a
  // count to one decimal because a rolling average rarely lands on a whole
  // feast.
  function formatDailyValue(spec, value) {
    if (spec.money) { return formatCoins(value); }
    return (Math.round(value * 10) / 10).toLocaleString();
  }

  // The lines one chart draws, in legend order. Every chart has a value on
  // every day today, but a day that ever arrives without one is a gap rather
  // than a zero, so it is left out of the line instead of pulling it to the
  // floor.
  //
  // A cumulative chart draws each feast's running total instead, and its
  // hover names what the day alone added to it, the way the total's does.
  function perFeastSeries(spec) {
    var series = [];
    feasts().forEach(function (feast, index) {
      if (isHidden(feast)) { return; }
      var points = [];
      var running = 0;
      daysOf(feast).forEach(function (day) {
        var value = spec.value(day);
        if (typeof value !== "number") { return; }
        if (spec.cumulative) {
          running += value;
          points.push({
            t: day.t,
            v: running,
            text: formatCoins(running) + " (+" + formatCoins(value) + ")"
          });
          return;
        }
        points.push({
          t: day.t,
          v: value,
          text: formatDailyValue(spec, value)
        });
      });
      series.push({
        name: feast.name,
        color: COLORS[index % COLORS.length],
        points: points
      });
    });
    return series;
  }

  // The total is the running sum of the feasts still switched on, so
  // switching one off answers "what did the rest cost?" rather than leaving
  // the total unchanged. Every day of the grid gets a point, including the
  // quiet ones, because a running total that skips a day reads as though the
  // window were shorter than it is.
  function totalSeries(spec, grid) {
    if (!visibleFeasts().length) { return []; }
    var perDay = {};
    visibleFeasts().forEach(function (feast) {
      daysOf(feast).forEach(function (day) {
        var value = spec.value(day);
        if (typeof value !== "number") { return; }
        perDay[day.t] = (perDay[day.t] || 0) + value;
      });
    });
    var running = 0;
    var points = grid.map(function (t) {
      var spent = perDay[t] || 0;
      running += spent;
      return {
        t: t,
        v: running,
        text: formatCoins(running) + " (+" + formatCoins(spent) + ")"
      };
    });
    return [{ name: "All feasts", color: TOTAL_COLOR, points: points }];
  }

  function dailySeries(spec, grid) {
    return spec.total ? totalSeries(spec, grid) : perFeastSeries(spec);
  }

  // The axis covers the values actually drawn, from zero up and rounded to a
  // readable step the way the stock chart's is. Zero is the floor on every
  // one of these charts: a day nothing was used or spent on is the reading
  // they are read for, so it has to sit on the floor rather than part-way up.
  function dailyScale(series) {
    var high = 0;
    series.forEach(function (item) {
      item.points.forEach(function (point) {
        if (point.v > high) { high = point.v; }
      });
    });
    if (!(high > 0)) { return { low: 0, high: 1, step: 1 }; }
    var span = high * 1.1;
    var step = niceStep(span, 5);
    return { low: 0, high: step * Math.ceil(span / step), step: step };
  }

  // The window's first and last day. A window that covers a single day is
  // given half a day either side, so its one point sits in the middle of the
  // plot rather than on the axis.
  function dayDomain(grid) {
    var from = grid[0];
    var to = grid[grid.length - 1];
    if (!(to > from)) {
      return {
        from: from - SECONDS_PER_DAY / 2,
        to: from + SECONDS_PER_DAY / 2
      };
    }
    return { from: from, to: to };
  }

  // The daily charts share the stock chart's proportions, a little shorter
  // because there are several of them down the page now, and with a wider
  // left margin where the axis carries a coin unit as well as a figure.
  function dailyMetrics(spec) {
    var base = metrics();
    return {
      w: base.w,
      h: Math.round(base.h * 0.8),
      top: base.top,
      right: base.right,
      bottom: base.bottom,
      left: spec.money ? base.left + 18 : base.left,
      ticks: base.ticks
    };
  }

  // What a daily chart says when it has drawn nothing. An empty window and a
  // legend switched all the way off are different states, and only one of
  // them is worth waiting for more data over. The total has no legend of its
  // own, so it points at the ones it is summing.
  function dailyStatusText(spec, plottedCount) {
    if (plottedCount) { return ""; }
    if (feasts().length && !visibleFeasts().length) {
      return spec.total
        ? "Every feast is switched off in the legends above."
        : "Every feast is switched off. Click one in the legend to draw " +
          "it again.";
    }
    return spec.empty;
  }

  function renderDailyChart(spec) {
    var host = document.getElementById("chart-" + spec.id);
    var statusBox = document.getElementById("status-" + spec.id);
    var legendBox = document.getElementById("legend-" + spec.id);
    if (!host || !statusBox || !state.data) { return; }
    var detach = dailyHandles[spec.id];
    if (detach) { detach(); }
    dailyHandles[spec.id] = null;
    if (legendBox) { renderLegend(legendBox); }
    host.replaceChildren();

    // attachHover reads whichever metrics are current, so this chart's are
    // made current before it is drawn; the stock chart sets its own back at
    // the top of every render of its own.
    var m = dailyMetrics(spec);
    M = m;
    var innerW = m.w - m.left - m.right;
    var innerH = m.h - m.top - m.bottom;
    var grid = dayGrid();
    var series = grid.length ? dailySeries(spec, grid) : [];
    var scale = dailyScale(series);
    var decimals = decimalsFor(scale.step);
    var domain = dayDomain(grid.length ? grid : [state.data.since]);

    function x(t) {
      var span = domain.to - domain.from;
      var frac = span > 0 ? (t - domain.from) / span : 0;
      if (frac < 0) { frac = 0; }
      if (frac > 1) { frac = 1; }
      return m.left + frac * innerW;
    }
    function y(value) {
      var plotValue = value;
      if (plotValue < 0) { plotValue = 0; }
      if (plotValue > scale.high) { plotValue = scale.high; }
      return m.top + (1 - plotValue / scale.high) * innerH;
    }

    var canvas = chartCanvas(spec.label);
    drawGrid(canvas, scale, y, function (value) {
      return formatAxisValue(spec, value, decimals);
    });
    drawLeftAxis(canvas);

    // Labels name real days rather than points along the window, and they are
    // spread evenly across the grid so a thirty-day window is not labelled
    // thirty times over.
    if (grid.length) {
      var ticks = Math.min(m.ticks, grid.length - 1);
      for (var tick = 0; tick <= ticks; tick += 1) {
        var at = ticks > 0
          ? Math.round(tick * (grid.length - 1) / ticks)
          : 0;
        var xLabel = svg("text", {
          "class": "x-label", x: x(grid[at]), y: m.top + innerH + 18
        });
        xLabel.textContent = formatDay(grid[at]);
        canvas.appendChild(xLabel);
      }
    }

    var plotted = [];
    series.forEach(function (item, index) {
      // A line is drawn only between days that sit next to each other in the
      // grid. Where a day is missing the line is left broken rather than
      // carried across it, because the segment would assert a value on days
      // that have none. A chart with a point on every day is one run and is
      // drawn unbroken.
      var runs = [];
      var run = [];
      var previousDay = null;
      item.points.forEach(function (point) {
        var px = x(point.t);
        var py = y(point.v);
        if (previousDay !== null && point.t - previousDay > SECONDS_PER_DAY) {
          runs.push(run);
          run = [];
        }
        run.push(px.toFixed(1) + "," + py.toFixed(1));
        previousDay = point.t;
        plotted.push({
          x: px,
          y: py,
          t: point.t,
          count: point.v,
          text: point.text,
          name: item.name,
          color: item.color,
          feast: index
        });
      });
      runs.push(run);
      runs.forEach(function (coords) {
        if (coords.length > 1) { drawLine(canvas, coords, item.color); }
      });
      item.points.forEach(function (point) {
        drawDot(canvas, x(point.t), y(point.v), 3, item.color);
      });
    });

    dailyHandles[spec.id] = attachHover(canvas, plotted, host, formatDay);
    host.appendChild(canvas);
    statusBox.textContent = dailyStatusText(spec, plotted.length);
  }

  function renderDailyCharts() {
    DAILY_CHARTS.forEach(function (spec) { renderDailyChart(spec); });
  }

  // A row of feast buttons over a table, the one it shows pressed.
  // choose(index) switches the table to another feast.
  function renderFeastTabs(box, activeIndex, choose) {
    box.replaceChildren();
    feasts().forEach(function (feast, index) {
      var button = el("button", null, feast.name);
      button.type = "button";
      var active = index === activeIndex;
      if (active) { button.classList.add("active"); }
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.addEventListener("click", function () { choose(index); });
      box.appendChild(button);
    });
  }

  function renderTabs() {
    renderFeastTabs(tabs, state.activeFeast, function (index) {
      state.activeFeast = index;
      state.tablePage = 0;
      renderTabs();
      renderTable();
    });
  }

  // A count read off the row as it is. Its heading stays at the left of the
  // column, as every heading in these tables but Edit has.
  function countColumn(heading, key) {
    return {
      heading: heading,
      head: null,
      className: "num",
      fill: textFill(function (row) { return String(row[key]); })
    };
  }

  var REMOVAL_COLUMNS = [
    TIME_COLUMN,
    countColumn("Removed", "amount"),
    countColumn("Remaining", "remaining")
  ];

  function renderTable() {
    var feast = activeFeast();
    state.tablePage = renderPagedTable({
      box: tableBox,
      pager: pager,
      rows: (feast && feast.removals) || [],
      page: state.tablePage,
      size: TABLE_PAGE_SIZE,
      className: "removals",
      columns: REMOVAL_COLUMNS,
      empty: "No removals were recorded in this period.",
      noun: "removals",
      go: function (page) {
        state.tablePage = page;
        renderTable();
      }
    });
  }

  // --- Additions ----------------------------------------------------------

  function pencilIcon() {
    var icon = svg("svg", {
      viewBox: "0 0 24 24",
      width: "16",
      height: "16",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "2",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": "true"
    });
    icon.appendChild(svg("path", {
      d: "M12 20h9"
    }));
    icon.appendChild(svg("path", {
      d: "M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"
    }));
    return icon;
  }

  function additionFeast() {
    return feasts()[state.activeAddition] || null;
  }

  function additionsOf(feast) {
    return (feast && feast.additions) || [];
  }

  // Sanitized tracing for a cost save: the action and the outcome only. No
  // account name, price or restock ever reaches the console.
  function traceCost(action, outcome) {
    console.debug("feast cost:", action, outcome);
  }

  function renderAdditionTabs() {
    renderFeastTabs(addTabs, state.activeAddition, function (index) {
      state.activeAddition = index;
      state.additionPage = 0;
      renderAdditionTabs();
      renderAdditions();
    });
  }

  var ADDITION_COLUMNS = [
    TIME_COLUMN,
    {
      heading: "User",
      head: null,
      className: "user",
      fill: textFill(function (row) {
        var users = row.users || [];
        return users.length ? users.join(", ") : "\u2014";
      })
    },
    countColumn("Added", "amount"),
    countColumn("Remaining", "remaining"),
    {
      heading: "Cost",
      head: null,
      className: "num",
      fill: textFill(function (row) {
        return typeof row.cost === "number" ? formatCoins(row.cost) : "\u2014";
      })
    },
    {
      heading: "Edit",
      className: "actions",
      fill: function (cell, row) {
        var edit = el("button", "icon-button");
        edit.type = "button";
        edit.setAttribute("aria-haspopup", "dialog");
        edit.setAttribute("aria-label", "Edit cost");
        edit.title = "Edit cost";
        edit.appendChild(pencilIcon());
        edit.addEventListener("click", function () {
          openCostDialog(additionFeast(), row);
        });
        cell.appendChild(edit);
      }
    }
  ];

  function renderAdditions() {
    state.additionPage = renderPagedTable({
      box: addTableBox,
      pager: addPager,
      rows: additionsOf(additionFeast()),
      page: state.additionPage,
      size: TABLE_PAGE_SIZE,
      className: "additions",
      columns: ADDITION_COLUMNS,
      // A restock nobody has priced yet is the row an officer opened this
      // section to act on.
      rowClass: function (row) {
        return typeof row.cost === "number" ? null : "unpriced";
      },
      wrap: "table-scroll",
      empty: "No additions were recorded in this period.",
      noun: "additions",
      go: function (page) {
        state.additionPage = page;
        renderAdditions();
      }
    });
  }

  // --- The cost dialog ------------------------------------------------------

  // A recorded price fills the boxes it reaches and leaves the rest blank, so
  // a cost of 1g opens as 1g rather than as 1g 0s 0c; a restock nobody has
  // priced opens empty.
  function fillCostFields(cost) {
    if (typeof cost !== "number") {
      costFields.gold.value = "";
      costFields.silver.value = "";
      costFields.copper.value = "";
      return;
    }
    var coins = splitCoins(cost);
    costFields.gold.value = coins.gold ? String(coins.gold) : "";
    costFields.silver.value = coins.silver ? String(coins.silver) : "";
    costFields.copper.value = coins.copper ? String(coins.copper) : "";
  }

  function openCostDialog(feast, addition) {
    state.editing = { feastId: feast ? feast.id : null, addition: addition };
    costError.textContent = "";
    costSave.disabled = false;
    costSubject.textContent =
      (feast ? feast.name : "") + " \u00b7 " + formatMoment(addition.t) +
      " \u00b7 " + addition.amount + " added";
    fillCostFields(addition.cost);
    traceCost("open", typeof addition.cost === "number" ? "priced" : "empty");
    costDialog.showModal();
    costFields.gold.focus();
  }

  function closeCostDialog() {
    state.editing = null;
    if (costDialog.open) { costDialog.close(); }
  }

  // A blank box is zero, which is what makes 5c a price a reader can type as
  // one box rather than three. Anything that is not a whole count of coins is
  // refused instead of being rounded into a price nobody entered.
  function readCoinField(input) {
    var raw = input.value.trim();
    if (raw === "") { return 0; }
    if (!/^[0-9]+$/.test(raw)) { return null; }
    var value = Number(raw);
    return Number.isSafeInteger(value) ? value : null;
  }

  function readCost() {
    var goldCoins = readCoinField(costFields.gold);
    var silverCoins = readCoinField(costFields.silver);
    var copperCoins = readCoinField(costFields.copper);
    if (goldCoins === null || silverCoins === null || copperCoins === null) {
      return { error: "Enter whole numbers of coins, or leave a box blank." };
    }
    var total = goldCoins * COPPER_PER_GOLD +
      silverCoins * COPPER_PER_SILVER + copperCoins;
    if (total > MAX_COST_COPPER) {
      return { error: "That is more gold than an account can hold." };
    }
    return { cost: total };
  }

  function saveCost() {
    var editing = state.editing;
    if (!editing) { return; }
    var read = readCost();
    if (read.error) {
      costError.textContent = read.error;
      traceCost("reject", "fields");
      return;
    }
    var addition = editing.addition;
    costError.textContent = "";
    costSave.disabled = true;
    fetchJson("/api/food/cost", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ log_id: addition.log_id, cost: read.cost })
    })
      .then(function (payload) {
        addition.cost = payload.cost;
        traceCost("save", "stored");
        closeCostDialog();
        renderAdditions();
        // The daily cost charts are drawn from days the server bucketed and
        // averaged, so a price recorded here only reaches them through
        // another read of the window. Patching them in the page would mean
        // working out a seven-day average over a week the page was never
        // sent. The officer who saved it is still reading the table it came
        // from, so their tab and page are kept across the fetch.
        refresh({ keepPlace: true });
      })
      .catch(function (error) {
        logFailure("feast cost save", error);
        costSave.disabled = false;
        costError.textContent = "Could not save that cost.";
      });
  }

  costForm.addEventListener("submit", function (event) {
    event.preventDefault();
    saveCost();
  });
  var costCancel = document.getElementById("cost-cancel");
  costCancel.addEventListener("click", function () {
    traceCost("cancel", "button");
    closeCostDialog();
  });
  // Escape closes a dialog itself; this keeps the page's own record of what
  // is open in step with it.
  costDialog.addEventListener("close", function () { state.editing = null; });
  costDialog.addEventListener("click", function (event) {
    // A modal dialog's backdrop is part of the dialog element, so a click
    // that lands on the element itself landed outside the form.
    if (event.target === costDialog) {
      traceCost("cancel", "backdrop");
      closeCostDialog();
    }
  });

  function render() {
    renderLegend();
    renderChart();
    renderDailyCharts();
    renderTabs();
    renderTable();
    renderAdditionTabs();
    renderAdditions();
  }

  // options.keepPlace holds the reader's tab and page across the fetch, for
  // the re-read a saved cost asks for: the rows are the ones they were just
  // working down, not a window they have only now opened.
  function refresh(options) {
    var keepPlace = !!(options && options.keepPlace);
    loadWindow("/api/food", "feast usage", "Could not load feast usage.",
      function (payload) {
        if (state.activeFeast >= (payload.feasts || []).length) {
          state.activeFeast = 0;
        }
        if (state.activeAddition >= (payload.feasts || []).length) {
          state.activeAddition = 0;
        }
        if (!keepPlace) {
          state.tablePage = 0;
          state.additionPage = 0;
        }
        // The rows behind the dialog have just been replaced, so whatever it
        // was opened over is gone.
        closeCostDialog();
        render();
      });
  }
""",
)
