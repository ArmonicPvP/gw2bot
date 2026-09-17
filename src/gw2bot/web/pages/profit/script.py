"""The client-side application that draws the report.

One IIFE holding the whole page: it fetches each section from the JSON
API, renders rows with ``textContent``, and keeps the sort, pagination and
hidden-item state the reader sets.
"""

from gw2bot.profit.store import MAX_REPORT_DAYS
from gw2bot.web.pages.shared import (
    RANGE_PICKER_LISTENERS_JS,
    range_picker_js,
)

PROFIT_SCRIPT = (
    """<script>
(function () {
  "use strict";
  // What the shared range picker reads and writes: the window the header is
  // showing, and the bounds of the report drawn under it.
  var state = { range: null, window: null };
  var status = document.getElementById("status");
  var reports = document.getElementById("reports");
  var keyHelp = document.getElementById("key-help");
  var sortStates = {};
  // Every paginated table keeps its page, its rows per page, and how many
  // pages that works out to. The controls themselves are in the page rather
  // than rebuilt on each move, so a page typed into the box keeps the caret
  // where the reader put it. Page sizes are read from the controls at start-up
  // so the page and the markup cannot disagree about the default.
  var pagers = {
    items: { page: 1, size: 10, pages: 1, body: "items-body" },
    days: { page: 1, size: 10, pages: 1, body: "days-body" }
  };
  var historyStart = null;
  var historyStartLabel = null;
  // Whether dates carry their year. Decided once per report from the window:
  // a window inside one year drops it everywhere, one that crosses a year
  // boundary shows it everywhere, so no table mixes the two forms.
  var showYear = false;
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
    "Sep", "Oct", "Nov", "Dec"];

  // Formats a UTC calendar date from the API ("2026-06-13") as "Jun 13" or
  // "Jun 13, 2026". It works on the string rather than through Date, which
  // would read that string as UTC midnight and then print it in the
  // viewer's zone - a day early anywhere west of Greenwich.
  function shortDate(iso, withYear) {
    var parts = String(iso).split("-");
    if (parts.length !== 3) { return String(iso); }
    var month = MONTHS[Number(parts[1]) - 1];
    var day = Number(parts[2]);
    if (!month || !Number.isInteger(day)) { return String(iso); }
    return withYear
      ? month + " " + day + ", " + parts[0]
      : month + " " + day;
  }

  function spansYears(startIso, endIso) {
    return String(startIso).slice(0, 4) !== String(endIso).slice(0, 4);
  }
  var missingKey = false;
  var restoredWindow = false;
  // A window a /profit view link names by its length rather than by one of
  // the buttons. No button can express it, so the page keeps naming it that
  // way - in its requests, in the address bar and in the local copy below -
  // until the reader picks a window of their own. Serializing it as the two
  // dates it happens to cover today would freeze it there: a reload would
  // ask for those dates instead of for the last sixty days.
  var linkedDays = null;

  // The lengths the three buttons stand for, so a link naming one of them by
  // its number of days lights that button instead.
  var PRESET_DAYS = { "24h": 1, "7d": 7, "30d": 30 };
  // How often the open orders follow the market. The GW2 API declares its
  // prices good for two minutes, and the server holds each reading for one,
  // so this is as live as the data can honestly be.
  var PRICE_REFRESH_MS = 60000;
  // The chosen window lives against the Discord account. This copy is a
  // repair kit: if the account ever comes back without one - a rebuilt
  // database, or a release that overwrote it - the browser puts back what
  // the member last picked instead of dropping them on the default.
  var STORED_RANGE_KEY = "gw2bot-profit-range";

  function readStoredRange(keyGeneration) {
    try {
      var saved = JSON.parse(localStorage.getItem(STORED_RANGE_KEY) || "null");
      if (!saved || saved.key !== keyGeneration) {
        // Saved under a key this member has since deleted. /profit deletekey
        // clears the window on purpose, so the copy must not put it back.
        return null;
      }
      // A length stays a length, so a window put back from here rolls on the
      // way the one it copied did.
      if (Number.isInteger(saved.days)) {
        return saved.days >= 1 && saved.days <= MAX_CUSTOM_DAYS
          ? saved : null;
      }
      if (saved.range !== "custom") {
        return typeof saved.range === "string" && saved.range ? saved : null;
      }
      return Number.isInteger(saved.start) && Number.isInteger(saved.end)
        && saved.end > saved.start ? saved : null;
    } catch (error) {
      return null;
    }
  }

  function writeStoredRange(keyGeneration) {
    try {
      localStorage.setItem(STORED_RANGE_KEY, JSON.stringify({
        range: state.range,
        days: linkedDays,
        start: customWindow === null ? null : customWindow.since,
        end: customWindow === null ? null : customWindow.until,
        key: keyGeneration
      }));
    } catch (error) {
      trace("window-not-stored", 0);
    }
  }

  // Whether a saved window and the one the server just served are the same
  // stretch of trading, which is what decides there is nothing to repair.
  function sameStoredRange(saved, data) {
    if (Number.isInteger(saved.days)) { return saved.days === data.days; }
    if (saved.range !== data.range) { return false; }
    return saved.range !== "custom"
      || (saved.start === data.window.start && saved.end === data.window.end);
  }

  // Put a saved window back into the header, so the request that follows
  // names it and the member lands where they left off.
  function restoreStoredRange(saved) {
    linkedDays = Number.isInteger(saved.days) ? saved.days : null;
    state.range = saved.range || null;
    if (saved.range === "custom" && linkedDays === null) {
      customWindow = { since: saved.start, until: saved.end };
      customStart.value = dayValue(new Date(saved.start * 1000));
      customEnd.value = dayValue(new Date(saved.end * 1000));
      toggleCustomPanel(true);
    } else {
      customWindow = null;
    }
    syncRangeButtons();
  }

  function trace(action, rows) {
    console.debug("Profit dashboard", action, "rows=" + rows);
  }

  function traceSort(table, column, direction, rows) {
    console.debug(
      "Profit dashboard sort", table, column, direction, "rows=" + rows);
  }

  function coin(value) {
    var sign = value < 0 ? "-" : "";
    var coins = Math.abs(value);
    var gold = Math.floor(coins / 10000);
    var silver = Math.floor((coins % 10000) / 100);
    var copper = coins % 100;
    var parts = [];
    if (gold) { parts.push(gold + "g"); }
    if (silver || gold) { parts.push(silver + "s"); }
    parts.push(copper + "c");
    return sign + parts.join(" ");
  }

  function average(profit, units) {
    return units > 0 ? coin(Math.round(profit / units)) : "0c";
  }

  function percent(value) {
    return value === null ? "\u2014" : value.toFixed(1) + "%";
  }

  function duration(seconds) {
    if (seconds < 60) { return Math.round(seconds) + "s"; }
    if (seconds < 3600) { return Math.round(seconds / 60) + "m"; }
    if (seconds < 172800) {
      return (seconds / 3600).toFixed(1).replace(".0", "") + "h";
    }
    return (seconds / 86400).toFixed(1).replace(".0", "") + "d";
  }

  function tone(value) {
    return value < 0 ? "negative" : "positive";
  }

  function cell(row, value, className, sortValue) {
    var node = document.createElement("td");
    node.textContent = String(value);
    if (className) { node.className = className; }
    if (sortValue !== undefined) {
      node.dataset.sortValue = String(sortValue);
    }
    row.appendChild(node);
    return node;
  }

  function profitCell(row, value) {
    return cell(row, coin(value), tone(value), value);
  }

  function optionalCoinCell(row, value) {
    return value === null
      ? cell(row, "\u2014", "", 0) : cell(row, coin(value), "", value);
  }

  function optionalProfitCell(row, value) {
    return value === null
      ? cell(row, "\u2014", "", 0) : profitCell(row, value);
  }

  function percentCell(row, value, toneValue) {
    return cell(
      row, percent(value), value === null ? ""
        : tone(toneValue === undefined ? value : toneValue),
      value === null ? 0 : value);
  }

  function sortableRow(index) {
    var row = document.createElement("tr");
    row.dataset.sortRow = "true";
    row.dataset.renderOrder = String(index);
    return row;
  }

  function compareValues(left, right, kind) {
    if (kind === "number") {
      return Number(left) - Number(right);
    }
    return String(left).localeCompare(
      String(right), undefined, { sensitivity: "base", numeric: true });
  }

  function sortTable(table, state) {
    var body = table.tBodies[0];
    var rows = Array.prototype.slice.call(
      body.querySelectorAll("tr[data-sort-row]"));
    var multiplier = state.direction === "ascending" ? 1 : -1;
    rows.sort(function (left, right) {
      var compared = compareValues(
        left.cells[state.column].dataset.sortValue,
        right.cells[state.column].dataset.sortValue,
        state.kind);
      if (compared) { return compared * multiplier; }
      return Number(left.dataset.renderOrder) - Number(right.dataset.renderOrder);
    });
    rows.forEach(function (row) { body.appendChild(row); });
    table.querySelectorAll("th[aria-sort]").forEach(function (heading) {
      heading.setAttribute("aria-sort", "none");
    });
    table.querySelector(
      '[data-sort-index="' + state.column + '"]').parentElement.setAttribute(
        "aria-sort", state.direction);
    return rows.length;
  }

  function pagerParts(key, selector) {
    return document.querySelectorAll(
      selector + '[data-page-group="' + key + '"]');
  }

  function updatePagerControls(key) {
    var pager = pagers[key];
    pagerParts(key, "input.page-input").forEach(function (input) {
      input.max = String(pager.pages);
      // A box the reader is still typing in is left alone; whatever they
      // settle on is written back when the change commits.
      if (document.activeElement !== input) {
        input.value = String(pager.page);
      }
    });
    pagerParts(key, ".page-total").forEach(function (node) {
      node.textContent = "of " + pager.pages;
    });
    pagerParts(key, "button[data-page-step]").forEach(function (button) {
      var backwards = button.dataset.pageStep === "first"
        || button.dataset.pageStep === "previous";
      button.disabled = backwards
        ? pager.page <= 1
        : pager.page >= pager.pages;
    });
  }

  function paginate(key) {
    var pager = pagers[key];
    var all = Array.prototype.slice.call(
      document.querySelectorAll("#" + pager.body + " tr[data-sort-row]"));
    // A row a filter has ruled out is not on any page, so it neither shows
    // nor counts towards how many pages there are.
    var rows = all.filter(function (row) {
      return row.dataset.filtered !== "1";
    });
    all.forEach(function (row) {
      if (row.dataset.filtered === "1") { row.hidden = true; }
    });
    pager.pages = Math.max(1, Math.ceil(rows.length / pager.size));
    pager.page = Math.min(Math.max(1, pager.page), pager.pages);
    rows.forEach(function (row, index) {
      row.hidden = index < (pager.page - 1) * pager.size
        || index >= pager.page * pager.size;
    });
    updatePagerControls(key);
  }

  function goToPage(key, page) {
    var pager = pagers[key];
    var target = Math.min(Math.max(1, page), pager.pages);
    if (target === pager.page) {
      // Nothing moves, but a box typed past the last page still has to be
      // put back to the page actually on screen.
      updatePagerControls(key);
      return;
    }
    pager.page = target;
    paginate(key);
    trace(key + "-page", target);
  }

  function commitTypedPage(input) {
    var key = input.dataset.pageGroup;
    var pager = pagers[key];
    var typed = Number(input.value);
    if (!Number.isInteger(typed) || typed < 1 || typed > pager.pages) {
      input.value = String(pager.page);
      trace(key + "-refuse-page", 0);
      return;
    }
    goToPage(key, typed);
    input.value = String(pager.page);
  }

  function commitPageSize(input) {
    var key = input.dataset.pageGroup;
    var pager = pagers[key];
    var value = Number(input.value);
    var largest = Number(input.max);
    if (!Number.isInteger(value) || value < 1 || value > largest) {
      input.value = String(pager.size);
      trace(key + "-refuse-page-size", 0);
      return;
    }
    pager.size = value;
    pager.page = 1;
    paginate(key);
    trace(key + "-page-size", value);
  }

  function initializePagers() {
    document.querySelectorAll("input.page-size-input").forEach(
      function (input) {
        pagers[input.dataset.pageGroup].size = Number(input.value);
        input.addEventListener("change", function () {
          commitPageSize(input);
        });
      });
    document.querySelectorAll("input.page-input").forEach(function (input) {
      input.addEventListener("change", function () { commitTypedPage(input); });
      input.addEventListener("keydown", function (event) {
        // The page box sits in a nav rather than a form, so Enter would
        // otherwise do nothing at all.
        if (event.key === "Enter") {
          event.preventDefault();
          commitTypedPage(input);
        }
      });
    });
    document.querySelectorAll("button[data-page-step]").forEach(
      function (button) {
        button.addEventListener("click", function () {
          var key = button.dataset.pageGroup;
          var step = button.dataset.pageStep;
          if (step === "first") {
            goToPage(key, 1);
          } else if (step === "previous") {
            goToPage(key, pagers[key].page - 1);
          } else if (step === "next") {
            goToPage(key, pagers[key].page + 1);
          } else {
            goToPage(key, pagers[key].pages);
          }
        });
      });
    Object.keys(pagers).forEach(updatePagerControls);
  }

  // What a table settles into once its rows are in a new order: a paginated
  // table goes back to its first page, and Your Picks re-trims to the rows the
  // new order puts on top, which is how sorting by Profit / Unit or ROI now
  // ranks the picks the way the two buttons used to.
  function afterSort(key) {
    if (Object.prototype.hasOwnProperty.call(pagers, key)) {
      pagers[key].page = 1;
      paginate(key);
    } else if (key === "picks") {
      limitPicks();
    }
  }

  function applySort(tableId) {
    var table = document.getElementById(tableId);
    var key = table.dataset.sortTable;
    sortTable(table, sortStates[key]);
    afterSort(key);
  }

  function initializeSorters() {
    document.querySelectorAll("table[data-sort-table]").forEach(
      function (table) {
        var selected = table.querySelector(
          'th[aria-sort="ascending"], th[aria-sort="descending"]');
        var selectedButton = selected.querySelector(".sort-button");
        sortStates[table.dataset.sortTable] = {
          column: Number(selectedButton.dataset.sortIndex),
          direction: selected.getAttribute("aria-sort"),
          kind: selectedButton.dataset.sortKind
        };
        table.querySelectorAll(".sort-button").forEach(function (button) {
          button.addEventListener("click", function () {
            var key = table.dataset.sortTable;
            var column = Number(button.dataset.sortIndex);
            var previous = sortStates[key];
            var direction = button.dataset.sortDefault;
            if (previous.column === column) {
              direction = previous.direction === "ascending"
                ? "descending" : "ascending";
            }
            sortStates[key] = {
              column: column,
              direction: direction,
              kind: button.dataset.sortKind
            };
            var rows = sortTable(table, sortStates[key]);
            afterSort(key);
            traceSort(key, button.dataset.sortKey, direction, rows);
          });
        });
      });
  }

  var SVG_NS = "http://www.w3.org/2000/svg";
  // The trailing average's width, matching ROLLING_AVERAGE_DAYS on the
  // server: it decides how many dates before the window the report sends.
  var ROLLING_DAYS = 7;
  var chartHoverCleanups = [];

  function svgNode(name, attributes, textValue) {
    var node = document.createElementNS(SVG_NS, name);
    Object.keys(attributes).forEach(function (key) {
      node.setAttribute(key, String(attributes[key]));
    });
    if (textValue !== undefined) { node.textContent = String(textValue); }
    return node;
  }

  function isoDay(date) {
    return date.toISOString().slice(0, 10);
  }

  function buildDailySeries(data) {
    var profitByDate = Object.create(null);
    data.days_table.forEach(function (day) {
      profitByDate[day.date] = day.profit;
    });
    // The trailing average reads its own series, which covers the six dates
    // before the window as well as the window itself so the average has a
    // full week behind its first date rather than behind its seventh. It is
    // summed over that whole stretch in one pass, so it can sit a little
    // above the bars, which are summed over the window alone.
    var trailingByDate = Object.create(null);
    (data.trailing_days || []).forEach(function (day) {
      trailingByDate[day.date] = day.profit;
    });
    var start = new Date(data.window.start_date + "T00:00:00Z");
    var end = new Date(data.window.end_date + "T00:00:00Z");
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())
      || start > end) {
      trace("charts-invalid-window", 0);
      return [];
    }
    var expectedStart = new Date(end.getTime());
    expectedStart.setUTCDate(expectedStart.getUTCDate() - data.days + 1);
    if (isoDay(start) !== isoDay(expectedStart)) {
      trace("charts-window-mismatch", 0);
      return [];
    }
    var points = [];
    var trailing = [];
    var trailingTotal = 0;
    var cumulative = 0;
    // Walk the six dates behind the window first so the trailing sum is
    // already a whole week wide by the time the window's own first date is
    // plotted. Only the window's dates become points; those six feed the
    // sum and are drawn nowhere.
    var cursor = new Date(start.getTime());
    cursor.setUTCDate(cursor.getUTCDate() - (ROLLING_DAYS - 1));
    var buckets = data.days + ROLLING_DAYS - 1;
    for (var bucket = 0; bucket < buckets; bucket += 1) {
      var date = isoDay(cursor);
      var trailed = Object.prototype.hasOwnProperty.call(trailingByDate, date)
        ? trailingByDate[date] : 0;
      trailing.push(trailed);
      trailingTotal += trailed;
      if (trailing.length > ROLLING_DAYS) {
        trailingTotal -= trailing.shift();
      }
      if (bucket >= ROLLING_DAYS - 1) {
        var profit = Object.prototype.hasOwnProperty.call(profitByDate, date)
          ? profitByDate[date] : 0;
        cumulative += profit;
        points.push({
          date: date,
          profit: profit,
          rolling: trailingTotal / ROLLING_DAYS,
          cumulative: cumulative
        });
      }
      cursor.setUTCDate(cursor.getUTCDate() + 1);
    }
    return points;
  }

  function emptyChart(svg, message) {
    var tooltip = svg.parentElement.querySelector(".chart-tooltip");
    if (tooltip) { tooltip.remove(); }
    svg.replaceChildren();
    svg.appendChild(svgNode("text", {
      x: 320,
      y: 110,
      "class": "chart-empty"
    }, message));
  }

  // The denominations an axis can be drawn in, largest first, and what each
  // is worth in copper. A chart is labelled in the largest one its own
  // numbers reach, so a window that never made a gold reads in silver
  // rather than as a column of roundings to "0g".
  var COPPER_PER_SILVER = 100;
  var COPPER_PER_GOLD = 100 * COPPER_PER_SILVER;
  var AXIS_UNITS = [
    { copper: COPPER_PER_GOLD, suffix: "g" },
    { copper: COPPER_PER_SILVER, suffix: "s" },
    { copper: 1, suffix: "c" }
  ];
  // The gaps between the four gridlines every chart draws: zero and three
  // steps, shared out between what the series reached above zero and what
  // it reached below it.
  var AXIS_INTERVALS = 3;
  // What a step may be, at each power of ten of the chart's own
  // denomination. A multiplier that would make the step a fraction of a
  // coin is skipped where it falls, so 1.5g is never a step and 15g is.
  var STEP_MULTIPLIERS = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];

  function axisUnit(reach) {
    for (var index = 0; index < AXIS_UNITS.length; index += 1) {
      if (reach >= AXIS_UNITS[index].copper) { return AXIS_UNITS[index]; }
    }
    return AXIS_UNITS[AXIS_UNITS.length - 1];
  }

  // Every step this denomination allows, in copper and smallest first: a
  // whole number of coins, and a round one, so the gridlines land on 5g or
  // 250s rather than on whatever a third of the highest reading came to.
  function axisSteps(unit) {
    var steps = [];
    for (var power = 0; power <= 9; power += 1) {
      var magnitude = Math.pow(10, power);
      for (var index = 0; index < STEP_MULTIPLIERS.length; index += 1) {
        var coins = STEP_MULTIPLIERS[index] * magnitude;
        if (coins !== Math.round(coins)) { continue; }
        var step = coins * unit.copper;
        if (steps[steps.length - 1] !== step) { steps.push(step); }
      }
    }
    return steps;
  }

  // The scale one chart is drawn on: four gridlines a round step apart with
  // zero always among them. The smallest step that fits the series into
  // three gaps wins, which is what keeps the padding above the highest
  // reading to the rounding up and no more.
  function axisScale(values) {
    var above = Math.max.apply(null, [0].concat(values));
    var below = -Math.min.apply(null, [0].concat(values));
    var unit = axisUnit(Math.max(above, below));
    var steps = axisSteps(unit);
    var step = steps[steps.length - 1];
    for (var index = 0; index < steps.length; index += 1) {
      if (Math.ceil(above / steps[index])
        + Math.ceil(below / steps[index]) <= AXIS_INTERVALS) {
        step = steps[index];
        break;
      }
    }
    var under = Math.ceil(below / step);
    var over = AXIS_INTERVALS - under;
    var ticks = [];
    for (var line = -under; line <= over; line += 1) {
      ticks.push(line * step);
    }
    return {
      unit: unit,
      step: step,
      ticks: ticks,
      minimum: -under * step,
      maximum: over * step
    };
  }

  // A gridline is a whole number of the chart's own coin, so its label is
  // that count and the coin's letter: "5g", "250s", "-40c".
  function axisLabel(value, unit) {
    return Math.round(value / unit.copper).toLocaleString() + unit.suffix;
  }

  // How wide a label actually renders. A chart measured while hidden
  // reports nothing, so an estimate from the character count stands in.
  function labelWidth(svg, text) {
    var node = svgNode(
      "text", { x: 0, y: 0, "class": "chart-label" }, text);
    svg.appendChild(node);
    var measured = node.getComputedTextLength
      ? node.getComputedTextLength() : 0;
    svg.removeChild(node);
    return measured > 0 ? measured : text.length * 6.5;
  }

  function chartFrame(svg, points, values, title) {
    var width = 640;
    var height = 220;
    var right = 16;
    var top = 14;
    var bottom = 34;
    var scale = axisScale(values);

    svg.replaceChildren();
    svg.appendChild(svgNode("title", {}, title));
    var labels = scale.ticks.map(function (value) {
      return axisLabel(value, scale.unit);
    });
    // The gutter is cut to the labels this chart actually has rather than
    // to a fixed width every long reading spilled out of: one wider than
    // the space left for it used to be drawn off the edge of the viewBox
    // and clipped, and the type is already as small as it reads.
    var left = Math.max(40, Math.min(112, Math.ceil(labels.reduce(
      function (measured, text) {
        return Math.max(measured, labelWidth(svg, text));
      }, 0)) + 12));
    var plotWidth = width - left - right;
    var plotHeight = height - top - bottom;
    var y = function (value) {
      return top + (scale.maximum - value)
        / (scale.maximum - scale.minimum) * plotHeight;
    };
    var x = function (index) {
      return left + (index + 0.5) / points.length * plotWidth;
    };

    scale.ticks.forEach(function (value, index) {
      svg.appendChild(svgNode("line", {
        x1: left,
        y1: y(value),
        x2: width - right,
        y2: y(value),
        "class": value === 0 ? "chart-zero" : "chart-gridline"
      }));
      svg.appendChild(svgNode("text", {
        x: left - 7,
        y: y(value) + 4,
        "text-anchor": "end",
        "class": "chart-label"
      }, labels[index]));
    });
    svg.appendChild(svgNode("text", {
      x: left,
      y: height - 8,
      "class": "chart-label"
    }, shortDate(points[0].date, showYear)));
    svg.appendChild(svgNode("text", {
      x: width - right,
      y: height - 8,
      "text-anchor": "end",
      "class": "chart-label"
    }, shortDate(points[points.length - 1].date, showYear)));
    return {
      width: width,
      height: height,
      left: left,
      top: top,
      bottom: bottom,
      plotWidth: plotWidth,
      plotHeight: plotHeight,
      right: width - right,
      x: x,
      y: y
    };
  }

  function tooltipNode(className, textValue) {
    var node = document.createElement("div");
    node.className = className;
    if (textValue !== undefined) { node.textContent = String(textValue); }
    return node;
  }

  function attachChartHover(svg, columns, frame) {
    var container = svg.parentElement;
    var previousTooltip = container.querySelector(".chart-tooltip");
    if (previousTooltip) { previousTooltip.remove(); }

    var crosshair = svgNode("line", {
      x1: frame.left,
      x2: frame.left,
      y1: frame.top,
      y2: frame.top + frame.plotHeight,
      "class": "chart-crosshair"
    });
    crosshair.style.visibility = "hidden";
    var rings = svgNode("g", {});
    var overlay = svgNode("rect", {
      x: frame.left,
      y: frame.top,
      width: frame.plotWidth,
      height: frame.plotHeight,
      "class": "chart-overlay"
    });
    svg.appendChild(crosshair);
    svg.appendChild(rings);
    svg.appendChild(overlay);

    var tooltip = tooltipNode("chart-tooltip");
    tooltip.style.visibility = "hidden";
    container.appendChild(tooltip);
    var pinned = false;
    var tapOrigin = null;
    var tapSlop = 12;

    function traceChartSelection(action, reason) {
      console.debug(
        "profit chart selection:", action, reason, columns.length);
    }

    function nearestColumn(vbX) {
      var nearest = null;
      var distance = Infinity;
      columns.forEach(function (column) {
        var candidateDistance = Math.abs(column.x - vbX);
        if (candidateDistance < distance) {
          nearest = column;
          distance = candidateDistance;
        }
      });
      return nearest;
    }

    function showHover(column, vbY) {
      crosshair.setAttribute("x1", String(column.x));
      crosshair.setAttribute("x2", String(column.x));
      crosshair.style.visibility = "visible";
      rings.replaceChildren();
      tooltip.replaceChildren();
      tooltip.appendChild(
        tooltipNode("tip-date", shortDate(column.date, showYear)));

      var anchorY = column.rows[0].y;
      var anchorDistance = Infinity;
      column.rows.forEach(function (reading) {
        rings.appendChild(svgNode("circle", {
          cx: column.x,
          cy: reading.y,
          r: 7,
          stroke: reading.color,
          "class": "chart-hover-ring"
        }));
        var row = tooltipNode("tip-row");
        var swatch = tooltipNode("swatch");
        swatch.style.background = reading.color;
        row.appendChild(swatch);
        row.appendChild(tooltipNode("tip-name", reading.label));
        row.appendChild(tooltipNode("tip-value", reading.value));
        tooltip.appendChild(row);
        var candidateDistance = Math.abs(reading.y - vbY);
        if (candidateDistance < anchorDistance) {
          anchorY = reading.y;
          anchorDistance = candidateDistance;
        }
      });

      // Keep the rendered box inside the chart rather than clamping only its
      // centre. The latter still clips a 9rem tooltip at either edge when
      // three charts share an ordinary desktop row or on a narrow phone.
      var containerWidth = container.getBoundingClientRect().width;
      var tooltipWidth = tooltip.getBoundingClientRect().width;
      var edgePadding = 8;
      var anchorPixels = column.x / frame.width * containerWidth;
      var minimumLeft = edgePadding + tooltipWidth / 2;
      var maximumLeft = containerWidth - edgePadding - tooltipWidth / 2;
      var leftPixels = Math.max(
        minimumLeft, Math.min(maximumLeft, anchorPixels));
      var topPercent = anchorY / frame.height * 100;
      tooltip.style.left = leftPixels + "px";
      tooltip.style.top = topPercent + "%";
      tooltip.style.transform = topPercent < 32
        ? "translate(-50%, 14px)"
        : "translate(-50%, calc(-100% - 14px))";
      tooltip.style.visibility = "visible";
    }

    function hideHover() {
      crosshair.style.visibility = "hidden";
      rings.replaceChildren();
      tooltip.style.visibility = "hidden";
    }

    function dismissPinned(reason) {
      if (!pinned) { return; }
      pinned = false;
      hideHover();
      traceChartSelection("dismiss", reason);
    }

    function pointFromEvent(event) {
      var bounds = svg.getBoundingClientRect();
      if (!bounds.width || !bounds.height) { return null; }
      return {
        x: (event.clientX - bounds.left) / bounds.width * frame.width,
        y: (event.clientY - bounds.top) / bounds.height * frame.height
      };
    }

    overlay.addEventListener("pointermove", function (event) {
      if (event.pointerType && event.pointerType !== "mouse") { return; }
      var point = pointFromEvent(event);
      if (!point) { return; }
      var column = nearestColumn(point.x);
      if (column) { showHover(column, point.y); }
    });
    overlay.addEventListener("pointerleave", function (event) {
      if (!event.pointerType || event.pointerType === "mouse") { hideHover(); }
    });
    overlay.addEventListener("pointerdown", function (event) {
      if (!event.pointerType || event.pointerType === "mouse") { return; }
      tapOrigin = {
        id: event.pointerId, x: event.clientX, y: event.clientY
      };
    });
    overlay.addEventListener("pointerup", function (event) {
      if (!tapOrigin || tapOrigin.id !== event.pointerId) { return; }
      var moved = Math.hypot(
        event.clientX - tapOrigin.x, event.clientY - tapOrigin.y);
      tapOrigin = null;
      if (moved > tapSlop) {
        traceChartSelection("skip", "moved");
        return;
      }
      var point = pointFromEvent(event);
      if (!point) {
        traceChartSelection("skip", "unmeasurable");
        return;
      }
      var column = nearestColumn(point.x);
      if (!column) {
        traceChartSelection("skip", "empty");
        return;
      }
      pinned = true;
      showHover(column, point.y);
      traceChartSelection("pin", "tap");
    });
    overlay.addEventListener("pointercancel", function () {
      tapOrigin = null;
      if (pinned) { dismissPinned("cancelled"); }
      else { traceChartSelection("skip", "cancelled"); }
    });

    function dismissFromPage(event) {
      if (overlay.contains(event.target)) { return; }
      dismissPinned("outside");
    }
    function dismissFromWheel() { dismissPinned("wheel"); }
    function dismissFromScroll() { dismissPinned("scroll"); }
    function dismissFromKey(event) {
      if (event.key === "Escape") { dismissPinned("escape"); }
    }
    function dismissFromBlur() { dismissPinned("blur"); }
    document.addEventListener("pointerdown", dismissFromPage);
    document.addEventListener("wheel", dismissFromWheel, { passive: true });
    document.addEventListener("scroll", dismissFromScroll, { passive: true });
    document.addEventListener("keydown", dismissFromKey);
    window.addEventListener("blur", dismissFromBlur);

    return function () {
      document.removeEventListener("pointerdown", dismissFromPage);
      document.removeEventListener("wheel", dismissFromWheel);
      document.removeEventListener("scroll", dismissFromScroll);
      document.removeEventListener("keydown", dismissFromKey);
      window.removeEventListener("blur", dismissFromBlur);
    };
  }

  function renderDailyProfitChart(points, dailyAverage) {
    var svg = document.getElementById("daily-profit-chart");
    var values = points.map(function (point) { return point.profit; });
    values.push(dailyAverage);
    var frame = chartFrame(
      svg, points, values, "Daily realized profit and window average");
    var barWidth = Math.max(
      2, Math.min(18, frame.plotWidth / points.length * 0.68));
    points.forEach(function (point, index) {
      var profitY = frame.y(point.profit);
      var zeroY = frame.y(0);
      var bar = svgNode("rect", {
        x: frame.x(index) - barWidth / 2,
        y: Math.min(profitY, zeroY),
        width: barWidth,
        height: point.profit === 0
          ? 0 : Math.max(1, Math.abs(profitY - zeroY)),
        "class": point.profit < 0
          ? "chart-bar-negative" : "chart-bar-positive"
      });
      bar.appendChild(svgNode(
        "title", {},
        shortDate(point.date, showYear) + ": " + coin(point.profit)));
      svg.appendChild(bar);
    });
    var averageLine = svgNode("line", {
      x1: frame.left,
      y1: frame.y(dailyAverage),
      x2: frame.right,
      y2: frame.y(dailyAverage),
      "class": "chart-average"
    });
    averageLine.appendChild(svgNode(
      "title", {}, "Daily average: " + coin(Math.round(dailyAverage))));
    svg.appendChild(averageLine);
    chartHoverCleanups.push(attachChartHover(svg, points.map(function (point, index) {
      return {
        date: point.date,
        x: frame.x(index),
        rows: [
          {
            label: "Daily profit",
            value: coin(point.profit),
            color: point.profit < 0 ? "#ff8f86" : "#74dc9a",
            y: frame.y(point.profit)
          },
          {
            label: "Daily average",
            value: coin(Math.round(dailyAverage)),
            color: "#f1c40f",
            y: frame.y(dailyAverage)
          }
        ]
      };
    }), frame));
  }

  function renderLineChart(
    svgId, points, field, lineClass, pointClass, title, valueLabel, color,
    emptyMessage
  ) {
    var svg = document.getElementById(svgId);
    var plotted = [];
    points.forEach(function (point, index) {
      if (typeof point[field] === "number") {
        plotted.push({ index: index, point: point, value: point[field] });
      }
    });
    if (!plotted.length) {
      emptyChart(svg, emptyMessage);
      return;
    }
    var values = plotted.map(function (entry) { return entry.value; });
    var frame = chartFrame(svg, points, values, title);
    var pathData = plotted.map(function (entry, index) {
      return (index ? "L" : "M") + frame.x(entry.index) + " "
        + frame.y(entry.value);
    }).join(" ");
    svg.appendChild(svgNode("path", {
      d: pathData,
      "class": lineClass
    }));
    plotted.forEach(function (entry) {
      var point = svgNode("circle", {
        cx: frame.x(entry.index),
        cy: frame.y(entry.value),
        r: 3,
        "class": pointClass
      });
      point.appendChild(svgNode(
        "title", {}, shortDate(entry.point.date, showYear) + ": "
        + coin(Math.round(entry.value))));
      svg.appendChild(point);
    });
    chartHoverCleanups.push(attachChartHover(svg, plotted.map(function (entry) {
      return {
        date: entry.point.date,
        x: frame.x(entry.index),
        rows: [{
          label: valueLabel,
          value: coin(Math.round(entry.value)),
          color: color,
          y: frame.y(entry.value)
        }]
      };
    }), frame));
  }

  function renderCharts(data) {
    chartHoverCleanups.forEach(function (cleanup) { cleanup(); });
    chartHoverCleanups = [];
    var points = buildDailySeries(data);
    // The bars and the running total are readings of the window, and the
    // trailing average is a reading of the week behind each of its dates.
    // A quiet window after a profitable one has an average worth drawing
    // and nothing else, so the two are decided apart.
    var hasWindow = points.length && data.days_table.length;
    var hasTrailing = points.length && (data.trailing_days || []).length;
    if (!hasWindow) {
      emptyChart(
        document.getElementById("daily-profit-chart"),
        "No realized profit in this window.");
      emptyChart(
        document.getElementById("cumulative-profit-chart"),
        "No realized profit in this window.");
      trace("charts-window-empty", points.length);
    } else {
      renderDailyProfitChart(points, data.summary.profit / data.days);
      renderLineChart(
        "cumulative-profit-chart", points, "cumulative", "chart-cumulative",
        "chart-point-cumulative", "Cumulative realized profit",
        "Cumulative profit", "#74dc9a",
        "No cumulative profit in this window.");
    }
    if (!hasTrailing) {
      emptyChart(
        document.getElementById("rolling-profit-chart"),
        "No realized profit in the seven days behind this window.");
      trace("charts-trailing-empty", points.length);
    } else {
      renderLineChart(
        "rolling-profit-chart", points, "rolling", "chart-rolling",
        "chart-point-rolling", "Seven-day rolling average realized profit",
        "7-day average", "#58a6ff",
        "No realized profit in the seven days behind this window.");
    }
    trace("charts-render", points.length);
  }

  function emptyRow(body, columns, message) {
    var row = document.createElement("tr");
    var node = cell(row, message, "empty");
    node.colSpan = columns;
    body.appendChild(row);
  }

  function totalRow(foot, values, profitIndex) {
    foot.replaceChildren();
    var row = document.createElement("tr");
    values.forEach(function (value, index) {
      if (index === profitIndex && typeof value === "number") {
        profitCell(row, value);
      } else {
        cell(row, value);
      }
    });
    foot.appendChild(row);
  }

  function extreme(rows, best) {
    if (!rows.length) { return null; }
    return rows.reduce(function (selected, candidate) {
      if (best ? candidate.profit > selected.profit
        : candidate.profit < selected.profit) {
        return candidate;
      }
      return selected;
    });
  }

  function highlight(entry, labelFor) {
    return entry === null
      ? "\u2014"
      : labelFor(entry) + " (" + coin(entry.profit) + ")";
  }

  function itemName(entry) { return entry.name; }
  function dayLabel(entry) { return shortDate(entry.date, showYear); }

  function renderSummary(data) {
    var summary = data.summary;
    var unrealized = data.unrealized;
    var bestItem = extreme(data.items, true);
    var worstItem = extreme(data.items, false);
    var bestDay = extreme(data.days_table, true);
    var worstDay = extreme(data.days_table, false);
    // A rolling window is named by its length, because that is what the
    // member asked for; a picked pair is named by the dates themselves,
    // which is what they asked for instead.
    var windowLabel = data.range === "custom"
      ? shortDate(data.window.start_date, showYear) + " \u2013 "
        + shortDate(data.window.end_date, showYear)
      : "Last " + data.days + " day" + (data.days === 1 ? "" : "s");
    var rows = [
      ["Window", windowLabel],
      ["Buy transactions", summary.buy_transactions],
      ["Sell transactions", summary.sell_transactions],
      ["Matched units", summary.matched_units],
      ["Matched cost", coin(summary.cost)],
      ["Net revenue", coin(summary.net_revenue)],
      ["Realized profit", coin(summary.profit), summary.profit],
      ["Realized ROI", percent(summary.roi_percent), summary.roi_percent],
      ["Profit / unit", average(summary.profit, summary.matched_units), summary.profit],
      ["Average daily profit", coin(Math.round(summary.profit / data.days)), summary.profit],
      ["Unrealized profit", coin(unrealized.projected_profit), unrealized.projected_profit],
      ["Unrealized ROI", percent(unrealized.roi_percent), unrealized.roi_percent],
      ["Best item", highlight(bestItem, itemName), bestItem && bestItem.profit],
      ["Worst item", highlight(worstItem, itemName), worstItem && worstItem.profit],
      ["Best trading day", highlight(bestDay, dayLabel), bestDay && bestDay.profit],
      ["Worst trading day", highlight(worstDay, dayLabel), worstDay && worstDay.profit]
    ];
    var body = document.getElementById("summary-body");
    body.replaceChildren();
    rows.forEach(function (values) {
      var row = document.createElement("tr");
      cell(row, values[0]);
      var valueCell = cell(row, values[1]);
      if (typeof values[2] === "number") {
        valueCell.className = tone(values[2]);
      }
      body.appendChild(row);
    });
  }

  function renderItems(data) {
    var body = document.getElementById("items-body");
    body.replaceChildren();
    excludedItems = data.excluded_items;
    itemsData = data.items;
    itemsSummary = data.summary;
    data.items.forEach(function (item, index) {
      var row = sortableRow(index);
      // What the filter matches on, kept on the row so it survives sorting:
      // the row's place in the data changes every time a column is sorted,
      // and its identity does not.
      row.dataset.itemId = String(item.item_id);
      cell(row, item.name, "name", item.name);
      cell(row, item.units, "", item.units);
      cell(row, coin(item.cost), "", item.cost);
      cell(row, coin(item.net_revenue), "", item.net_revenue);
      profitCell(row, item.profit);
      percentCell(row, item.roi_percent);
      profitCell(row, Math.round(item.profit / item.units));
      cell(row, duration(item.hold_seconds), "", item.hold_seconds);
      percentCell(row, item.profit_share_percent, item.profit);
      cell(row, "", "actions").appendChild(
        hideButton(item.name, function (button) {
          setExclusion("items", item, true, button);
        }));
      body.appendChild(row);
    });
    if (!data.items.length) {
      emptyRow(body, 10, excludedItems.length
        ? "No matched flips were found in this window outside the items "
          + "you have hidden."
        : "No matched flips were found in this window.");
    }
    refreshItemCategories();
    markItemsFilter();
    applySort("items-table");
    renderHidden("items");
  }

  // The rows of Realized Profit by Item as the server sent them, and the
  // window totals it summed for them. Both are kept because the table can be
  // narrowed here after it has been drawn, and a narrowed table's footer is
  // added up from the rows that are left rather than from the window.
  var itemsData = [];
  var itemsSummary = null;
  // What the reader has narrowed the table to: a word to find in the name,
  // and one category to keep. Either alone is a filter, and neither is the
  // table whole.
  var itemsFilter = { search: "", category: "" };

  function itemsFilterActive() {
    return itemsFilter.search !== "" || itemsFilter.category !== "";
  }

  function filteredItems() {
    if (!itemsFilterActive()) { return itemsData.slice(); }
    return itemsData.filter(function (item) {
      if (itemsFilter.category && item.category !== itemsFilter.category) {
        return false;
      }
      return !itemsFilter.search
        || item.name.toLowerCase().indexOf(itemsFilter.search) !== -1;
    });
  }

  // The categories the menu offers are the ones the window actually holds,
  // so it never lists a kind of item the table cannot show. A category the
  // reader had picked that this window has none of is kept in the list and
  // stays picked, rather than silently widening the table under them.
  function refreshItemCategories() {
    var menu = document.getElementById("items-category");
    var seen = Object.create(null);
    var categories = [];
    itemsData.forEach(function (item) {
      if (!item.category || seen[item.category]) { return; }
      seen[item.category] = true;
      categories.push(item.category);
    });
    if (itemsFilter.category && !seen[itemsFilter.category]) {
      categories.push(itemsFilter.category);
    }
    categories.sort(function (left, right) {
      return left.localeCompare(
        right, undefined, { sensitivity: "base", numeric: true });
    });
    menu.replaceChildren();
    var all = document.createElement("option");
    all.value = "";
    all.textContent = "All categories";
    menu.appendChild(all);
    categories.forEach(function (category) {
      var option = document.createElement("option");
      option.value = category;
      option.textContent = category;
      menu.appendChild(option);
    });
    menu.value = itemsFilter.category;
    trace("items-categories", categories.length);
  }

  // Mark the rows the filter keeps and re-add the footer from them. This does
  // not move the table to a page; rendering and filtering each do that for
  // themselves, so a redraw does not paginate twice.
  function markItemsFilter() {
    // The controls are wired up at start-up, before the first report has
    // landed. There is nothing to narrow until it has.
    if (itemsSummary === null) { return; }
    var kept = filteredItems();
    var keptIds = Object.create(null);
    kept.forEach(function (item) { keptIds[item.item_id] = true; });
    document.querySelectorAll("#items-body tr[data-sort-row]").forEach(
      function (row) {
        row.dataset.filtered = keptIds[row.dataset.itemId] ? "" : "1";
      });
    renderItemsTotal(kept);
    var active = itemsFilterActive();
    document.getElementById("items-filter-clear").hidden = !active;
    document.getElementById("items-filter-empty").hidden =
      !active || !itemsData.length || kept.length > 0;
    document.getElementById("items-filter-count").textContent = active
      ? "Showing " + kept.length + " of " + itemsData.length + " item"
        + (itemsData.length === 1 ? "" : "s") + "."
      : "";
  }

  function renderItemsTotal(kept) {
    var foot = document.getElementById("items-foot");
    var summary = itemsSummary;
    if (!itemsFilterActive()) {
      // The whole window, as the server added it up. Its figures are the ones
      // every other section is drawn from, so the unnarrowed table shows them
      // rather than a second reckoning of the same rows.
      totalRow(foot, [
        "Total", summary.matched_units, coin(summary.cost),
        coin(summary.net_revenue), summary.profit,
        percent(summary.roi_percent),
        average(summary.profit, summary.matched_units), "\u2014",
        summary.profit === 0 ? "\u2014" : "100.0%", ""
      ], 4);
      return;
    }
    var units = 0;
    var cost = 0;
    var revenue = 0;
    var profit = 0;
    kept.forEach(function (item) {
      units += item.units;
      cost += item.cost;
      revenue += item.net_revenue;
      profit += item.profit;
    });
    // Profit Share stays a share of the whole window's profit, which is what
    // makes the narrowed footer say something the unnarrowed one does not:
    // how much of everything realized came from these items.
    totalRow(foot, [
      "Filtered total", units, coin(cost), coin(revenue), profit,
      percent(cost ? profit / cost * 100 : null),
      average(profit, units), "\u2014",
      percent(summary.profit ? profit / summary.profit * 100 : null), ""
    ], 4);
  }

  function applyItemsFilter() {
    markItemsFilter();
    // A narrower table is a different run of pages, and the page the reader
    // was on is a page of rows that may be gone.
    pagers.items.page = 1;
    paginate("items");
    trace("items-filtered", filteredItems().length);
  }

  function initializeItemsFilter() {
    var search = document.getElementById("items-search");
    var category = document.getElementById("items-category");
    var clear = document.getElementById("items-filter-clear");
    search.addEventListener("input", function () {
      itemsFilter.search = search.value.trim().toLowerCase();
      applyItemsFilter();
    });
    category.addEventListener("change", function () {
      itemsFilter.category = category.value;
      applyItemsFilter();
    });
    clear.addEventListener("click", function () {
      search.value = "";
      category.value = "";
      itemsFilter.search = "";
      itemsFilter.category = "";
      applyItemsFilter();
      search.focus();
    });
  }

  var picksData = [];
  // Your Picks is a shortlist rather than a full table: every pick is sorted,
  // and only the rows at the top of that order are shown.
  var PICKS_LIMIT = 10;

  function limitPicks() {
    var rows = document.querySelectorAll("#picks-body tr[data-sort-row]");
    rows.forEach(function (row, index) {
      row.hidden = index >= PICKS_LIMIT;
    });
    trace("picks-shown", Math.min(PICKS_LIMIT, rows.length));
  }

  function renderPicks() {
    var body = document.getElementById("picks-body");
    body.replaceChildren();
    // The order rows are built in is the tie-break the sorter falls back on,
    // so picks are built in the profit-then-name order the removed toggle
    // used to break its own ties with.
    picksData.slice().sort(function (left, right) {
      return right.profit - left.profit
        || left.name.localeCompare(right.name);
    }).forEach(function (item, index) {
      var row = sortableRow(index);
      cell(row, item.name, "name", item.name);
      cell(row, coin(item.buy_price), "", item.buy_price);
      cell(row, coin(item.sell_price), "", item.sell_price);
      profitCell(row, item.profit);
      percentCell(row, item.roi_percent);
      body.appendChild(row);
    });
    if (!picksData.length) {
      emptyRow(body, 5,
        "No current prices showed a positive return for your previous flips.");
    }
    applySort("picks-table");
  }

  function renderDays(data) {
    var body = document.getElementById("days-body");
    body.replaceChildren();
    data.days_table.forEach(function (day, index) {
      var row = sortableRow(index);
      cell(row, shortDate(day.date, showYear), "", day.date);
      cell(row, day.units, "", day.units);
      cell(row, coin(day.cost), "", day.cost);
      cell(row, coin(day.net_revenue), "", day.net_revenue);
      profitCell(row, day.profit);
      body.appendChild(row);
    });
    if (!data.days_table.length) {
      emptyRow(body, 5, "No realized profit was found in this window.");
    }
    totalRow(document.getElementById("days-foot"), [
      "Total", data.summary.matched_units, coin(data.summary.cost),
      coin(data.summary.net_revenue), data.summary.profit
    ], 4);
    applySort("days-table");
  }

  function renderUnrealized(data) {
    var unrealized = data.unrealized;
    var body = document.getElementById("unrealized-body");
    body.replaceChildren();
    unrealized.items.forEach(function (item, index) {
      var row = sortableRow(index);
      cell(row, item.name, "name", item.name);
      cell(row, item.units, "", item.units);
      cell(row, coin(item.unit_price), "", item.unit_price);
      cell(row, coin(item.cost), "", item.cost);
      optionalCoinCell(row, item.sell_price);
      cell(
        row, coin(item.projected_net_revenue), "",
        item.projected_net_revenue);
      profitCell(row, item.projected_profit);
      percentCell(row, item.roi_percent);
      body.appendChild(row);
    });
    if (!unrealized.items.length) {
      emptyRow(body, 8, "No currently listed unmatched purchases were found.");
    }
    totalRow(document.getElementById("unrealized-foot"), [
      "Total", unrealized.units, "\u2014", coin(unrealized.cost), "\u2014",
      coin(unrealized.projected_net_revenue), unrealized.projected_profit,
      percent(unrealized.roi_percent)
    ], 6);
    applySort("unrealized-table");
  }

  var ordersRows = [];
  var ordersAvailable = true;
  // The items left out of the realized report, as the server named them.
  // They are not in any of its tables, so this is the only place their names
  // arrive from - including for an item that traded nothing in this window.
  var excludedItems = [];

  // "Hidden" is what the dashboard calls these items; the stored rows and the
  // API keep calling them exclusions, so both words appear here. Open Orders
  // and Realized Profit by Item hide independently: what belongs out of one
  // table is not what belongs out of the other, so each keeps its own stored
  // set, its own eye buttons and its own Hidden items window, and everything
  // below takes the group it is working on.
  var EXCLUSION_GROUPS = {
    orders: {
      path: "/api/profit/exclusions",
      subject: "your open orders",
      applied: applyOrderExclusion
    },
    items: {
      path: "/api/profit/item-exclusions",
      subject: "your realized profit",
      applied: applyItemExclusion
    }
  };

  function hideButton(name, hide) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "row-action";
    button.title = "Hide " + name;
    button.setAttribute("aria-label", "Hide " + name);
    var icon = svgNode("svg", {
      viewBox: "0 0 24 24",
      width: 16,
      height: 16,
      fill: "none",
      stroke: "currentColor",
      "stroke-width": 2,
      "stroke-linecap": "round",
      "stroke-linejoin": "round"
    });
    // An eye with a line through it: hidden, not deleted.
    icon.appendChild(svgNode("path", {
      d: "M9.9 4.24A9.1 9.1 0 0 1 12 4c7 0 10 8 10 8a18.5 18.5 0 0 1-2.16 3.19"
        + "M6.61 6.61A18.4 18.4 0 0 0 2 12s3 8 10 8a9 9 0 0 0 5.39-1.61"
    }));
    icon.appendChild(svgNode("path", {
      d: "M14.12 14.12a3 3 0 1 1-4.24-4.24"
    }));
    icon.appendChild(svgNode("line", {x1: 2, y1: 2, x2: 22, y2: 22}));
    button.appendChild(icon);
    button.addEventListener("click", function () { hide(button); });
    return button;
  }

  function restoreButton(name, restore) {
    var button = document.createElement("button");
    button.type = "button";
    // Every other row action is an icon, and the icon styling gives a button
    // no line box of its own. This one is a word, so it asks for one back.
    button.className = "row-action text-action";
    button.textContent = "Restore";
    button.setAttribute("aria-label", "Restore " + name);
    button.addEventListener("click", function () { restore(button); });
    return button;
  }

  function setExclusion(group, item, excluded, button) {
    var config = EXCLUSION_GROUPS[group];
    button.disabled = true;
    fetch(config.path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_id: item.item_id, excluded: excluded })
    }).then(function (response) {
      if (response.status === 401) {
        location.href = "/login?next=" + encodeURIComponent(
          location.pathname + location.search);
        return false;
      }
      if (!response.ok) { throw new Error("exclusion request failed"); }
      return true;
    }).then(function (stored) {
      if (!stored) { return; }
      status.className = "";
      status.textContent = (excluded
        ? "Hid that item from "
        : "Restored that item to ") + config.subject + ".";
      config.applied(item, excluded);
      trace(group + (excluded ? "-hidden" : "-restored"), 1);
    }).catch(function () {
      button.disabled = false;
      status.className = "error";
      status.textContent =
        "That change could not be saved. Try again in a moment.";
      trace(group + "-hidden-failure", 0);
    });
  }

  function applyOrderExclusion(item, excluded) {
    // Open Orders is summed in the browser from rows already on screen, so
    // the change is drawn from what is held here rather than re-fetched.
    ordersRows.forEach(function (row) {
      if (row.item_id === item.item_id) { row.excluded = excluded; }
    });
    if (!excluded) {
      // A restored item with no live order has nothing left to show.
      ordersRows = ordersRows.filter(function (row) {
        return row.has_order || row.item_id !== item.item_id;
      });
    }
    renderOrders();
  }

  function applyItemExclusion(item, excluded) {
    // The Hidden items window answers at once, the way the Open Orders one
    // does: the member is looking at the list they just changed, and the
    // change is already stored. Waiting on the reload below would leave the
    // row they clicked sitting there, and leave it there for good if that
    // reload failed.
    excludedItems = excludedItems.filter(function (row) {
      return row.item_id !== item.item_id;
    });
    if (excluded) {
      excludedItems = excludedItems.concat([
        { item_id: item.item_id, name: item.name }
      ]);
    }
    renderHidden("items");
    // The tables and totals are a different matter. Hiding an item moves the
    // summary, all three charts, the day table and Your Picks as well, and
    // every one of those is summed by the server, so the report is asked for
    // again rather than re-derived here as a second implementation of the
    // same arithmetic. Its answer replaces the list above when it lands.
    reloadReport();
  }

  function openHidden(group) {
    var dialog = document.getElementById(group + "-hidden-dialog");
    var search = document.getElementById(group + "-hidden-search");
    search.value = "";
    renderHidden(group);
    dialog.showModal();
    search.focus();
    trace(group + "-hidden-open", hiddenItems(group).length);
  }

  function hiddenItems(group) {
    if (group === "items") { return excludedItems.slice(); }
    // One entry per item, however many order rows that item has.
    var seen = Object.create(null);
    var hidden = [];
    ordersRows.forEach(function (row) {
      if (!row.excluded || seen[row.item_id]) { return; }
      seen[row.item_id] = true;
      hidden.push(row);
    });
    return hidden;
  }

  function renderHidden(group) {
    var body = document.getElementById(group + "-hidden-body");
    var count = document.getElementById(group + "-hidden-count");
    var search = document.getElementById(group + "-hidden-search").value
      .trim().toLowerCase();
    var subject = EXCLUSION_GROUPS[group].subject;
    var hidden = hiddenItems(group).sort(function (left, right) {
      return left.name.localeCompare(
        right.name, undefined, { sensitivity: "base", numeric: true });
    });
    var shown = search
      ? hidden.filter(function (item) {
        return item.name.toLowerCase().indexOf(search) !== -1;
      })
      : hidden;
    body.replaceChildren();
    shown.forEach(function (item) {
      var row = document.createElement("tr");
      cell(row, item.name, "name");
      cell(row, "", "actions").appendChild(
        restoreButton(item.name, function (button) {
          setExclusion(group, item, false, button);
        }));
      body.appendChild(row);
    });
    if (!shown.length) {
      emptyRow(body, 2, hidden.length
        ? "No hidden items match that search."
        : "You have not hidden any items yet.");
    }
    count.textContent = (hidden.length === 1
      ? "1 item is hidden from "
      : hidden.length + " items are hidden from ") + subject + ".";
    document.getElementById(group + "-menu").title = hidden.length
      ? "Hidden items (" + hidden.length + ")"
      : "Hidden items";
    trace(group + "-hidden-shown", shown.length);
  }

  function renderOrders() {
    var body = document.getElementById("orders-body");
    var help = document.getElementById("orders-key-help");
    help.hidden = ordersAvailable;
    body.replaceChildren();
    var kept = ordersRows.filter(function (row) {
      return row.has_order && !row.excluded;
    });
    // Every footer figure covers the same rows: the ones with a usable
    // current price. Adding an unpriced order's units and cost to totals its
    // profit and ROI cannot include would leave a footer that does not
    // reconcile with itself.
    var units = 0;
    var cost = 0;
    var profit = 0;
    var unpriced = 0;
    kept.forEach(function (order, index) {
      var row = sortableRow(index);
      cell(row, order.name, "name", order.name);
      cell(row, order.quantity, "", order.quantity);
      cell(row, coin(order.unit_price), "", order.unit_price);
      cell(row, coin(order.cost), "", order.cost);
      optionalCoinCell(row, order.buy_price);
      optionalCoinCell(row, order.sell_price);
      optionalProfitCell(row, order.profit);
      optionalProfitCell(row, order.total_profit);
      percentCell(row, order.roi_percent);
      cell(row, "", "actions").appendChild(
        hideButton(order.name, function (button) {
          setExclusion("orders", order, true, button);
        }));
      body.appendChild(row);
      if (order.total_profit === null) {
        unpriced += 1;
        return;
      }
      units += order.quantity;
      cost += order.cost;
      profit += order.total_profit;
    });
    if (!kept.length) {
      emptyRow(body, 10, ordersAvailable
        ? "No open buy orders were found."
        : "Open buy orders are unavailable for this saved key.");
    }
    document.getElementById("orders-unpriced").hidden = unpriced === 0;
    totalRow(document.getElementById("orders-foot"), [
      "Total", units, "\u2014", coin(cost), "\u2014", "\u2014", "\u2014",
      profit, percent(cost ? profit / cost * 100 : null), ""
    ], 7);
    applySort("orders-table");
    renderHidden("orders");
    trace("open-orders", kept.length);
  }

  function renderDelivery(delivery) {
    var coins = document.getElementById("unclaimed-coins");
    var body = document.getElementById("delivery-body");
    var help = document.getElementById("delivery-key-help");
    body.replaceChildren();
    var unpriced = document.getElementById("delivery-unpriced");
    if (delivery.coins === null) {
      coins.textContent = "Unavailable";
      coins.className = "";
      emptyRow(body, 9, "Delivery is unavailable for this saved key.");
      totalRow(
        document.getElementById("delivery-foot"),
        ["Total", "\u2014", "\u2014", "\u2014", "\u2014", "\u2014",
          "\u2014", "\u2014", "\u2014"], -1);
      help.hidden = false;
      unpriced.hidden = true;
      trace("delivery-unavailable", 0);
      return;
    }
    coins.textContent = coin(delivery.coins);
    coins.className = delivery.coins > 0 ? "positive" : "";
    help.hidden = true;
    var items = delivery.items;
    // The footer covers only the stacks a projection could be built for, so
    // its cost, sale and profit reconcile against each other rather than
    // adding a cost whose sale is missing.
    var quantity = 0;
    var cost = 0;
    var sale = 0;
    var profit = 0;
    var partial = 0;
    items.forEach(function (item, index) {
      var row = sortableRow(index);
      cell(row, item.name, "name", item.name);
      cell(row, item.quantity, "positive", item.quantity);
      optionalCoinCell(row, item.unit_price);
      var costCell = optionalCoinCell(row, item.cost);
      if (item.cost === null && item.costed_quantity) {
        // The row is dashed because the purchases ran out part way, which
        // is worth saying rather than leaving the reader to guess.
        costCell.title = "Your purchases cover only "
          + item.costed_quantity + " of these " + item.quantity + ".";
      }
      optionalCoinCell(row, item.buy_price);
      optionalCoinCell(row, item.sell_price);
      optionalCoinCell(row, item.projected_sale);
      optionalProfitCell(row, item.projected_profit);
      percentCell(row, item.roi_percent);
      body.appendChild(row);
      if (item.projected_profit === null || item.cost === null) {
        partial += 1;
        return;
      }
      quantity += item.quantity;
      cost += item.cost;
      sale += item.projected_sale;
      profit += item.projected_profit;
    });
    if (!items.length) {
      emptyRow(body, 9, "No items are waiting for pickup.");
    }
    unpriced.hidden = partial === 0;
    totalRow(document.getElementById("delivery-foot"),
      ["Total", quantity, "\u2014", coin(cost), "\u2014", "\u2014",
        coin(sale), profit, percent(cost ? profit / cost * 100 : null)], 7);
    applySort("delivery-table");
    trace("delivery", items.length);
  }

  function renderReport(data) {
    // The window the server served is the one it remembered, so the header
    // and the address bar follow it rather than the other way round.
    state.window = { since: data.window.start, until: data.window.end };
    // A window the server remembered as a length has no button to light, so
    // it comes back drawn in the date fields. Keeping the length is what
    // stops the dates it happens to cover today from replacing it: taking
    // the window from the answer works either way round, whether the length
    // arrived in a link or was waiting against this member's account.
    linkedDays = data.rolling_days === undefined ? null : data.rolling_days;
    adoptRange(data.range, data.window.start, data.window.end);
    if (data.remembered_window) {
      writeStoredRange(data.key_generation);
    } else if (!restoredWindow) {
      var saved = readStoredRange(data.key_generation);
      if (saved !== null && !sameStoredRange(saved, data)) {
        restoredWindow = true;
        restoreStoredRange(saved);
        trace("window-restored", 0);
        // Putting a window back is the page correcting itself, not the
        // member asking for fresh data.
        load(false);
        return;
      }
      writeStoredRange(data.key_generation);
    }
    historyStart = data.history_start_date;
    showYear = spansYears(data.window.start_date, data.window.end_date);
    // "Held since" is a single date whose range runs to the window's end,
    // so it decides its year on its own rather than following the table.
    historyStartLabel = historyStart
      ? shortDate(
        historyStart, spansYears(historyStart, data.window.end_date))
      : null;
    history.replaceState(null, "", "/profit" + windowQuery());
    renderSummary(data);
    renderCharts(data);
    picksData = data.picks;
    renderPicks();
    renderItems(data);
    renderDays(data);
    renderUnrealized(data);
    trace(
      "render-report",
      data.items.length + data.days_table.length
        + data.unrealized.items.length);
  }

  function renderOrdersSection(data) {
    ordersAvailable = data.available;
    ordersRows = data.orders.map(function (order) {
      order.excluded = false;
      return order;
    }).concat(data.excluded.map(function (order) {
      order.excluded = true;
      return order;
    }));
    renderOrders();
  }

  function sectionCards(source) {
    return document.querySelectorAll(
      '.card[data-source="' + source + '"]');
  }

  function markSection(source, state, message) {
    sectionCards(source).forEach(function (card) {
      card.classList.toggle("loading", state === "loading");
      card.classList.toggle("failed", state === "failed");
      var note = card.querySelector(".section-message");
      if (note) {
        note.textContent = message || "Loading\u2026";
      }
    });
    trace("section-" + source, state === "ready" ? 1 : 0);
  }

  // What the shared range picker fills its empty date fields from: the span
  // of the report on screen, in seconds.
  function windowSpan() {
    return state.window === null
      ? 0 : state.window.until - state.window.since;
  }

  // How the window on screen is named: by one of the buttons, by the pair of
  // dates behind Custom, or by the length a link gave it.
  function windowQuery() {
    return linkedDays === null
      ? rangeQuery()
      : "?days=" + encodeURIComponent(String(linkedDays));
  }

  // What the picker calls once a window is picked. Naming a window is not
  // asking for a live re-read of the Trading Post, so this takes the cached
  // path; only Reload forces one. A window the reader picks replaces the
  // length a link gave them, which is what stops the two naming different
  // stretches of trading.
  function refresh() {
    linkedDays = null;
    load(false);
  }
"""
    + range_picker_js(
        "profit", max_custom_days=MAX_REPORT_DAYS, utc_days=True
    )
    + """
  // ``quiet`` is for a redraw the page decided on rather than one the reader
  // asked for. A loading section collapses to its heading and a spinner,
  // which is right when there is nothing on screen yet and wrong when there
  // is: every card shrinking and growing again moves the whole document
  // under the reader, who was looking at one row in one table. A quiet fetch
  // leaves the numbers on screen until the new ones are ready to replace
  // them, so nothing moves and nothing is lost if the request fails.
  function fetchSection(source, url, render, quiet) {
    if (!quiet) { markSection(source, "loading"); }
    return fetch(url).then(function (response) {
      if (response.status === 401) {
        location.href = "/login?next=" + encodeURIComponent(
          location.pathname + location.search);
        return null;
      }
      if (response.status === 409) {
        keyHelp.classList.add("open");
        markSection(source, "failed", "A Trading Post API key is required.");
        missingKey = true;
        return null;
      }
      if (!response.ok) { throw new Error(source + " request failed"); }
      return response.json();
    }).then(function (data) {
      if (!data) { return false; }
      render(data);
      markSection(source, "ready");
      return true;
    }).catch(function () {
      if (quiet) {
        // The rows on screen are still the last good ones, and the change
        // that prompted this is already stored, so the reader is told rather
        // than shown an emptied page.
        status.className = "error";
        status.textContent =
          "That change was saved, but the report could not be redrawn. "
          + "Reload the page to catch up.";
        trace("section-" + source + "-quiet-failure", 0);
        return false;
      }
      markSection(
        source, "failed",
        "This section could not be loaded. Try again in a moment.");
      return false;
    });
  }

  // The window the address bar names, if it names one. A link from
  // /profit view, or the address this page wrote itself on its last render,
  // both land here: what they name is asked for and becomes the window this
  // member is put back on next time, exactly as picking it would.
  function readInitialRange() {
    var params = new URLSearchParams(location.search);
    var picked = params.get("range");
    if (picked === "custom") {
      var since = Number(params.get("start"));
      var until = Number(params.get("end"));
      if (Number.isInteger(since) && Number.isInteger(until)
          && until > since) {
        customWindow = { since: since, until: until };
        customStart.value = dayValue(new Date(since * 1000));
        customEnd.value = dayValue(new Date(until * 1000));
        state.range = "custom";
        trace("window-from-link", 0);
      }
      return;
    }
    if (picked !== null) {
      if (PRESET_RANGES.indexOf(picked) !== -1) {
        state.range = picked;
        trace("window-from-link", 0);
      }
      return;
    }
    var days = Number(params.get("days"));
    if (Number.isInteger(days) && days >= 1 && days <= MAX_CUSTOM_DAYS) {
      // A length one of the buttons stands for is that button; one that no
      // button stands for stays a length.
      var button = PRESET_RANGES.filter(function (key) {
        return PRESET_DAYS[key] === days;
      })[0];
      if (button) {
        state.range = button;
      } else {
        linkedDays = days;
      }
      trace("window-from-link", days);
    }
  }

  // The report request for the window on screen. Asking without a window lets
  // the server answer with the one this member last chose; the page only
  // names a window when they just picked one, or when the link they followed
  // named one. windowQuery opens with a "?" and is empty when the page is
  // asking for the remembered window, so the mark comes off and the rest
  // joins the other query values here.
  function reportUrl(forced) {
    var query = [windowQuery().slice(1), forced ? "refresh=1" : ""]
      .filter(Boolean).join("&");
    return "/api/profit" + (query ? "?" + query : "");
  }

  // Re-draw the report sections alone, on the cached path: hiding or
  // restoring an item is a change to what the server sums, not a reason to
  // re-read the Trading Post or to disturb the other two sections. It is
  // quiet for the same reason - the reader is looking at a row they just
  // clicked, and collapsing every card to a spinner underneath them would
  // carry that row off the screen.
  function reloadReport() {
    return fetchSection("report", reportUrl(false), renderReport, true);
  }

  function load(forced) {
    status.className = "";
    status.textContent = "Loading\u2026";
    reports.hidden = false;
    keyHelp.classList.remove("open");
    missingKey = false;
    // Three independent requests, in flight together. Each section draws as
    // soon as its own answer arrives instead of waiting for the slowest.
    // Only pressing Reload asks for a live read. Naming a window does not:
    // the address bar carries one after every render, so treating that as a
    // forced refresh would make each ordinary reload bypass every cache.
    var refresh = forced ? "refresh=1" : "";
    Promise.all([
      fetchSection("report", reportUrl(forced), renderReport),
      fetchSection("orders", "/api/profit/orders"
        + (refresh ? "?" + refresh : ""), renderOrdersSection),
      fetchSection("delivery", "/api/profit/delivery"
        + (refresh ? "?" + refresh : ""), renderDelivery)
    ]).then(function (loaded) {
      var ready = loaded.filter(Boolean).length;
      if (missingKey) {
        status.className = "error";
        status.textContent = "A Trading Post API key is required.";
      } else if (ready === loaded.length) {
        status.className = "";
        status.textContent = historyStartLabel
          ? "Updated from your private Trading Post data, held since "
            + historyStartLabel + "."
          : "Updated from your private Trading Post data.";
      } else {
        status.className = "error";
        status.textContent =
          "Some sections could not be loaded. Try again in a moment.";
      }
      trace("load", ready);
    });
  }

  function refreshPrices() {
    // A quiet re-read on the cached path: the server holds each price for a
    // minute and shares it between members, and holds the delivery box for
    // as long as it holds a transaction snapshot, so this beat costs one
    // public price lookup and never a private read of its own.
    if (missingKey || document.hidden) { return; }
    // Both sections carry live market columns now, so both ride the beat.
    // They are separate requests so a failure in one leaves the other's
    // numbers on screen.
    beat("/api/profit/orders", "orders", function (data) {
      renderOrdersSection(data);
      return data.orders.length;
    });
    beat("/api/profit/delivery", "delivery", function (data) {
      renderDelivery(data);
      return data.items === null ? 0 : data.items.length;
    });
  }

  function beat(path, section, render) {
    fetch(path).then(function (response) {
      if (!response.ok) {
        // A refused beat is a failure, not an empty answer: say so, with
        // the status and the section, rather than returning quietly.
        trace("prices-refresh-refused-" + section, response.status);
        return null;
      }
      return response.json();
    }).then(function (data) {
      if (!data) { return; }
      var rows = render(data);
      markSection(section, "ready");
      trace("prices-refreshed-" + section, rows);
    }).catch(function () {
      // A dropped beat is not worth telling the reader about; the next one
      // is a minute away and the numbers on screen are still the last good
      // ones. It is still worth tracing, and worth saying which section.
      trace("prices-refresh-failed-" + section, 0);
    });
  }

  setInterval(refreshPrices, PRICE_REFRESH_MS);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) { refreshPrices(); }
  });

  document.getElementById("reload").addEventListener("click", function () {
    load(true);
  });
"""
    + RANGE_PICKER_LISTENERS_JS
    + """
  Object.keys(EXCLUSION_GROUPS).forEach(function (group) {
    document.getElementById(group + "-menu").addEventListener(
      "click", function () { openHidden(group); });
    document.getElementById(group + "-hidden-close").addEventListener(
      "click", function () {
        document.getElementById(group + "-hidden-dialog").close();
      });
    document.getElementById(group + "-hidden-search").addEventListener(
      "input", function () { renderHidden(group); });
    document.getElementById(group + "-hidden-dialog").addEventListener(
      "click", function (event) {
        // A modal dialog's backdrop is part of the dialog element, so a click
        // landing on it and not on the panel inside means "outside".
        if (event.target === this) {
          this.close();
          trace(group + "-hidden-dismiss", 0);
        }
      });
  });
  initializePagers();
  initializeSorters();
  initializeItemsFilter();
  readInitialRange();
  syncRangeButtons();
  fetch("/api/me")
    .then(function (response) { return response.ok ? response.json() : null; })
    .then(function (identity) {
      if (identity) { document.getElementById("whoami").textContent = identity.name; }
    });
  load(false);
}());
</script>
"""
)
