"""The guild bank gold history dashboard page.

Dynamic data reaches the page only through JSON APIs and is inserted
with ``textContent`` on the client, never as an HTML string.
"""

from __future__ import annotations

from gw2bot.web.pages.shared import (
    CHART_MODE_JS,
    COIN_JS,
    HISTORY_JS,
    HISTORY_STYLE,
    chart_dashboard,
    history_main,
)

GOLD_PAGE = chart_dashboard(
    "Guild Bank",
    "gold",
    style=HISTORY_STYLE
    + """
table.changes td.amount { white-space: nowrap; }
@media (max-width: 640px) {
  /* The balance after each movement is the column that gives way on a phone:
     the amount and its sign are what a reader is scanning for, and the line
     above already shows where the balance went. It is still in the chart
     tooltip. */
  table.changes .balance { display: none; }
}
""",
    body=history_main(
        "Gold in the guild bank",
        "now-balance",
        "Movement kinds",
        "Deposits and withdrawals",
    ),
    parts=(COIN_JS, CHART_MODE_JS, HISTORY_JS),
    script="""
  // Okabe-Ito colourblind-safe palette: one hue per direction the gold went.
  var OPERATIONS = {
    deposit: { color: "#009E73", label: "Deposited" },
    withdraw: { color: "#D55E00", label: "Withdrew" }
  };
  var NET_CHANGES = [
    { color: OPERATIONS.deposit.color, label: "Increase" },
    { color: OPERATIONS.withdraw.color, label: "Decrease" },
    { color: "#D9D9D9", label: "Net Zero" }
  ];
  var TABLE_PAGE_SIZE = 10;
  // Smallest number of gold the y axis ever spans, so a quiet week does not
  // turn one small withdrawal into a cliff.
  var MIN_SPAN_GOLD = 10;

  // The left margin is wider than the roster page's because a gold figure is
  // a longer label than a member count.
  var CHART_METRICS = chartMetrics({ left: 56 }, { left: 64 });
  // How wide one character of an axis label runs at the 11px the chart
  // draws them in, rounded up from the widest digit: the label is measured
  // by its length because the chart is laid out before it is on the page,
  // and an unrendered text node reports no width at all.
  var Y_LABEL_CHAR_WIDTH = 6.6;

  var state = windowState({ tablePage: 0, staircase: false });

  var nowBalance = document.getElementById("now-balance");

  function movements() { return (state.data && state.data.movements) || []; }

  function operationOf(operation) {
    return OPERATIONS[operation] || OPERATIONS.withdraw;
  }
  function netChangeColor(before, after) {
    if (after === before) { return NET_CHANGES[2].color; }
    return operationOf(after > before ? "deposit" : "withdraw").color;
  }

  // The chart's scale is in gold, while every amount written out keeps its
  // exact coins.
  function gold(copper) { return copper / COPPER_PER_GOLD; }
  function coinY(copper) { return scaleY(gold(copper)); }

  // An axis label in the coins it reaches and no others, unspaced so it fits
  // beside the plot: 12g, 3s50c, 0c.
  function formatAxisCoins(copper) {
    var coins = splitCoins(copper);
    var text = coins.gold ? coins.gold.toLocaleString() + "g" : "";
    if (coins.silver || (coins.gold && coins.copper)) {
      text += coins.silver + "s";
    }
    if (coins.copper || !text) { text += coins.copper + "c"; }
    return text;
  }
  function formatSigned(copper, operation) {
    var sign = operation === "withdraw" ? "-" : "+";
    return sign + formatCoins(copper);
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

  function renderChart() {
    resetChart();
    if (!state.scale) {
      chartStatus.textContent = state.data
        ? "No guild bank balance has been recorded yet, so the gold line " +
          "cannot be drawn. The movements below are still listed."
        : "";
      return;
    }
    var canvas = chartCanvas(
      "Gold in the guild bank over time, with one dot per active minute");

    // A bank in six figures of gold carries a label wider than the margin
    // left for it, and one drawn past the edge of the viewBox is cut rather
    // than merely cramped. The margin is widened to whatever the labels
    // need, never narrowed, so an ordinary balance is laid out as before.
    // Everything positioned reads M.left at call time, so this has to land
    // before the first coordinate is computed.
    var lines = Math.round(
      (state.scale.high - state.scale.low) / state.scale.step);
    var widest = 0;
    for (var line = 0; line <= lines; line += 1) {
      widest = Math.max(widest, formatAxisCoins(
        (state.scale.low + state.scale.step * line) * COPPER_PER_GOLD
      ).length);
    }
    M.left = Math.max(M.left, Math.ceil(widest * Y_LABEL_CHAR_WIDTH) + 10);
    drawGrid(canvas, state.scale,
      function (value) { return coinY(value * COPPER_PER_GOLD); },
      function (value) { return formatAxisCoins(value * COPPER_PER_GOLD); });
    drawTimeAxis(canvas, true);

    // Movements in the same clock minute collapse into one cumulative dot at
    // the balance after that minute's final transaction. The dot retains the
    // complete group so its tooltip can still itemize everything that moved.
    var plotted = [];
    var previousCoins = points()[0].coins;
    movementMinutes().forEach(function (minute) {
      var px = scaleX(minute.t);
      var py = coinY(minute.after);
      var color = netChangeColor(previousCoins, minute.after);
      plotted.push({
        x: px, y: py, t: minute.t, after: minute.after,
        movements: minute.movements, color: color
      });
      previousCoins = minute.after;
    });

    // Draw from the window boundary through only the cumulative minute dots
    // and back to the other boundary. A separate segment gives each combined
    // interval the deposit/withdrawal colour of its net balance change.
    var linePoints = [{ t: points()[0].t, coins: points()[0].coins }];
    plotted.forEach(function (point) {
      linePoints.push({ t: point.t, coins: point.after });
    });
    var finalPoint = points()[points().length - 1];
    linePoints.push({ t: finalPoint.t, coins: finalPoint.coins });
    linePoints.slice(1).forEach(function (point, index) {
      var previous = linePoints[index];
      var coords = [
        scaleX(previous.t).toFixed(1) + "," +
          coinY(previous.coins).toFixed(1)
      ];
      if (state.staircase) {
        coords.push(scaleX(point.t).toFixed(1) + "," +
          coinY(previous.coins).toFixed(1));
      }
      coords.push(scaleX(point.t).toFixed(1) + "," +
        coinY(point.coins).toFixed(1));
      drawLine(canvas, coords, netChangeColor(previous.coins, point.coins));
    });

    // SVG paints later elements on top. Add the combined event dots only
    // after every segment so a following segment cannot cover a dot's fill.
    plotted.forEach(function (point) {
      drawDot(canvas, point.x, point.y, 4, point.color);
    });

    showChart(canvas, plotted,
      "No gold moved in or out of the bank in this period.");
  }

  bindChartMode(function () { return points().length; }, renderChart);

  function movementMinutes() {
    var byMinute = {};
    var minutes = [];
    // The API serializes newest-first. Reversing, rather than sorting only by
    // timestamp, restores its exact chronological order including tied times.
    movements().slice().reverse().forEach(function (movement) {
      if (movement.after === null) { return; }
      var minuteAt = Math.floor(movement.t / 60) * 60;
      var key = String(minuteAt);
      var minute = byMinute[key];
      if (!minute) {
        minute = { t: movement.t, after: movement.after, movements: [] };
        byMinute[key] = minute;
        minutes.push(minute);
      }
      minute.t = movement.t;
      minute.after = movement.after;
      minute.movements.push(movement);
    });
    return minutes;
  }

  // Each plotted minute is already a single column.
  function attachHover(canvas, plotted) {
    return attachChartHover(canvas, pointColumns(plotted), {
      host: chart,
      emptyReason: "no-movements",
      ringColor: function (point) { return point.color; },
      fillTooltip: function (tooltip, column, emphasized) {
        emphasized.movements.forEach(function (movement) {
          var row = el("div", "tip-row");
          var swatch = el("span", "swatch");
          swatch.style.background = operationOf(movement.operation).color;
          row.appendChild(swatch);
          row.appendChild(el("span", "name", movement.name));
          row.appendChild(el("span", "val",
            formatSigned(movement.coins, movement.operation)));
          tooltip.appendChild(row);
        });
        tooltip.appendChild(el("div", "tip-note",
          "Bank held " + formatCoins(emphasized.after) + "."));
      }
    });
  }

  function renderLegend() { renderKeyLegend(NET_CHANGES); }

  function renderTotals() {
    var net = state.data.net || 0;
    // A net of nothing is written plainly: a sign in front of a zero claims a
    // direction the window did not go.
    var netText = net === 0
      ? formatCoins(0)
      : formatSigned(Math.abs(net), net < 0 ? "withdraw" : "deposit");
    renderTotalsLine([
      [formatCoins(state.data.deposited || 0), "deposited"],
      [formatCoins(state.data.withdrawn || 0), "withdrawn"],
      [netText, "net"]
    ]);
    nowBalance.textContent = state.data.coins === null ||
      state.data.coins === undefined
      ? ""
      : closingFigure(formatCoins(state.data.coins));
  }

  var MOVEMENT_COLUMNS = [
    TIME_COLUMN,
    ACCOUNT_COLUMN,
    {
      heading: "Gold",
      className: "num amount",
      fill: function (cell, movement) {
        cell.appendChild(el("span", null,
          formatSigned(movement.coins, movement.operation)));
      }
    },
    {
      heading: "Balance",
      className: "num balance",
      fill: textFill(function (movement) {
        return movement.after === null ? "" : formatCoins(movement.after);
      })
    }
  ];

  function renderTable() {
    renderHistoryTable(movements(), MOVEMENT_COLUMNS,
      "No gold moved in or out of the bank in this period.", "movements");
  }

  function refresh() {
    loadHistory("/api/gold", "gold history",
      "Could not load the gold history.");
  }
""",
)
