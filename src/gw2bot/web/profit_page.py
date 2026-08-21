"""Static browser dashboard for a member's Trading Post profit reports."""

from gw2bot.web.page import _SHARED_STYLE

PROFIT_PAGE = (
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Trading Post Profit</title>
<style>"""
    + _SHARED_STYLE
    + """
body { display: flex; flex-direction: column; }
header {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
  padding: 0.75rem 1rem;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}
header h1 { font-size: 1.05rem; }
header a { font-size: 0.85rem; }
.spacer { flex: 1; }
#whoami { color: var(--muted); font-size: 0.85rem; }
header form { display: flex; align-items: center; gap: 0.45rem; }
label { color: var(--muted); font-size: 0.85rem; }
input {
  width: 4.6rem;
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.4rem 0.5rem;
  font: inherit;
}
button {
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.4rem 0.75rem;
  font: inherit;
  cursor: pointer;
}
button:hover { background: var(--border); }
.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
main { width: min(100%, 88rem); margin: 0 auto; padding: 1rem; }
#status {
  color: var(--muted);
  min-height: 1.5rem;
  margin-bottom: 0.75rem;
}
#status.error { color: #ff8f86; }
#key-help {
  display: none;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1rem;
  margin-bottom: 1rem;
}
#key-help.open { display: block; }
#key-help code { color: var(--text); }
.cards { display: grid; gap: 1rem; }
.card {
  min-width: 0;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}
.card h2 { font-size: 1rem; padding: 0.85rem 1rem 0.25rem; }
.card p.note { color: var(--muted); font-size: 0.82rem; padding: 0 1rem 0.8rem; }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
th, td {
  padding: 0.65rem 0.8rem;
  text-align: right;
  border-top: 1px solid var(--border);
  white-space: nowrap;
}
th { color: var(--muted); font-weight: 600; background: var(--panel-2); }
th:first-child, td:first-child { text-align: left; }
td.name { white-space: normal; overflow-wrap: anywhere; min-width: 12rem; }
tfoot td { font-weight: 700; background: var(--panel-2); }
.positive { color: #74dc9a; }
.negative { color: #ff8f86; }
.empty { color: var(--muted); text-align: center !important; padding: 1.2rem; }
@media (max-width: 640px) {
  header { align-items: flex-start; }
  header h1 { width: 100%; }
  .spacer { display: none; }
  #whoami { margin-left: auto; }
  main { padding: 0.75rem; }
  th, td { padding: 0.55rem 0.65rem; }
}
</style>
</head>
<body>
<header>
  <h1>Trading Post Profit</h1>
  <form id="range-form">
    <label for="days">Days</label>
    <input id="days" name="days" type="number" min="1" max="90" value="30" required>
    <button class="primary" type="submit">Load</button>
  </form>
  <span class="spacer"></span>
  <a href="/">Calendar</a>
  <span id="whoami"></span>
  <form method="post" action="/logout">
    <button type="submit">Sign out</button>
  </form>
</header>
<main>
  <div id="status" role="status" aria-live="polite">Loading&hellip;</div>
  <div id="key-help">
    No Trading Post API key is saved for this Discord account. Run
    <code>/profit setkey</code> in the guild server, then reload this page.
  </div>
  <div class="cards" id="reports" hidden>
    <section class="card">
      <h2>Summary</h2>
      <p class="note">Realized profit from FIFO-matched purchases and sales in the selected window.</p>
      <div class="table-scroll"><table>
        <thead><tr><th>Measure</th><th>Value</th></tr></thead>
        <tbody id="summary-body"></tbody>
      </table></div>
    </section>
    <section class="card">
      <h2>Realized Profit by Item</h2>
      <div class="table-scroll"><table>
        <thead><tr><th>Item</th><th>Units</th><th>Cost</th><th>Net Revenue</th><th>Profit</th><th>Profit / Unit</th></tr></thead>
        <tbody id="items-body"></tbody>
        <tfoot id="items-foot"></tfoot>
      </table></div>
    </section>
    <section class="card">
      <h2>Realized Profit by Day</h2>
      <div class="table-scroll"><table>
        <thead><tr><th>Date</th><th>Units</th><th>Cost</th><th>Net Revenue</th><th>Profit</th></tr></thead>
        <tbody id="days-body"></tbody>
        <tfoot id="days-foot"></tfoot>
      </table></div>
    </section>
    <section class="card">
      <h2>Unrealized Profit</h2>
      <p class="note">Unmatched purchases from the selected window that are currently listed for sale.</p>
      <div class="table-scroll"><table>
        <thead><tr><th>Item</th><th>Units</th><th>Cost</th><th>Projected Sale</th><th>Projected Profit</th></tr></thead>
        <tbody id="unrealized-body"></tbody>
        <tfoot id="unrealized-foot"></tfoot>
      </table></div>
    </section>
  </div>
</main>
<script>
(function () {
  "use strict";
  var rangeForm = document.getElementById("range-form");
  var daysInput = document.getElementById("days");
  var status = document.getElementById("status");
  var reports = document.getElementById("reports");
  var keyHelp = document.getElementById("key-help");

  function trace(action, rows) {
    console.debug("Profit dashboard", action, "rows=" + rows);
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

  function cell(row, value, className) {
    var node = document.createElement("td");
    node.textContent = String(value);
    if (className) { node.className = className; }
    row.appendChild(node);
    return node;
  }

  function profitCell(row, value) {
    return cell(row, coin(value), value < 0 ? "negative" : "positive");
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

  function renderSummary(data) {
    var summary = data.summary;
    var rows = [
      ["Window", "Last " + data.days + " day" + (data.days === 1 ? "" : "s")],
      ["Buy transactions", summary.buy_transactions],
      ["Sell transactions", summary.sell_transactions],
      ["Matched units", summary.matched_units],
      ["Matched cost", coin(summary.cost)],
      ["Net revenue", coin(summary.net_revenue)],
      ["Estimated profit", coin(summary.profit)],
      ["Profit / unit", average(summary.profit, summary.matched_units)]
    ];
    var body = document.getElementById("summary-body");
    body.replaceChildren();
    rows.forEach(function (values) {
      var row = document.createElement("tr");
      cell(row, values[0]);
      var valueCell = cell(row, values[1]);
      if (values[0] === "Estimated profit" || values[0] === "Profit / unit") {
        valueCell.className = summary.profit < 0 ? "negative" : "positive";
      }
      body.appendChild(row);
    });
  }

  function renderItems(data) {
    var body = document.getElementById("items-body");
    body.replaceChildren();
    data.items.forEach(function (item) {
      var row = document.createElement("tr");
      cell(row, item.name, "name");
      cell(row, item.units);
      cell(row, coin(item.cost));
      cell(row, coin(item.net_revenue));
      profitCell(row, item.profit);
      profitCell(row, Math.round(item.profit / item.units));
      body.appendChild(row);
    });
    if (!data.items.length) {
      emptyRow(body, 6, "No matched flips were found in this window.");
    }
    totalRow(document.getElementById("items-foot"), [
      "Total", data.summary.matched_units, coin(data.summary.cost),
      coin(data.summary.net_revenue), data.summary.profit,
      average(data.summary.profit, data.summary.matched_units)
    ], 4);
  }

  function renderDays(data) {
    var body = document.getElementById("days-body");
    body.replaceChildren();
    data.days_table.forEach(function (day) {
      var row = document.createElement("tr");
      cell(row, day.date);
      cell(row, day.units);
      cell(row, coin(day.cost));
      cell(row, coin(day.net_revenue));
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
  }

  function renderUnrealized(data) {
    var unrealized = data.unrealized;
    var body = document.getElementById("unrealized-body");
    body.replaceChildren();
    unrealized.items.forEach(function (item) {
      var row = document.createElement("tr");
      cell(row, item.name, "name");
      cell(row, item.units);
      cell(row, coin(item.cost));
      cell(row, coin(item.projected_net_revenue));
      profitCell(row, item.projected_profit);
      body.appendChild(row);
    });
    if (!unrealized.items.length) {
      emptyRow(body, 5, "No currently listed unmatched purchases were found.");
    }
    totalRow(document.getElementById("unrealized-foot"), [
      "Total", unrealized.units, coin(unrealized.cost),
      coin(unrealized.projected_net_revenue), unrealized.projected_profit
    ], 4);
  }

  function render(data) {
    renderSummary(data);
    renderItems(data);
    renderDays(data);
    renderUnrealized(data);
    reports.hidden = false;
    keyHelp.classList.remove("open");
    status.className = "";
    status.textContent = "Updated from your private Trading Post data.";
    trace("render", data.items.length + data.days_table.length + data.unrealized.items.length);
  }

  function selectedDays() {
    var days = Number(daysInput.value);
    return Number.isInteger(days) && days >= 1 && days <= 90 ? days : null;
  }

  function load() {
    var days = selectedDays();
    if (days === null) {
      status.className = "error";
      status.textContent = "Choose a number of days from 1 through 90.";
      trace("refuse-range", 0);
      return;
    }
    status.className = "";
    status.textContent = "Loading\u2026";
    reports.hidden = true;
    keyHelp.classList.remove("open");
    history.replaceState(null, "", "/profit?days=" + encodeURIComponent(String(days)));
    fetch("/api/profit?days=" + encodeURIComponent(String(days)))
      .then(function (response) {
        if (response.status === 401) {
          location.href = "/login";
          return null;
        }
        if (response.status === 409) {
          keyHelp.classList.add("open");
          status.className = "error";
          status.textContent = "A Trading Post API key is required.";
          trace("missing-key", 0);
          return null;
        }
        if (!response.ok) { throw new Error("profit request failed"); }
        return response.json();
      })
      .then(function (data) { if (data) { render(data); } })
      .catch(function () {
        status.className = "error";
        status.textContent = "The profit report could not be loaded. Try again in a moment.";
        trace("failure", 0);
      });
  }

  rangeForm.addEventListener("submit", function (event) {
    event.preventDefault();
    load();
  });
  var initial = Number(new URLSearchParams(location.search).get("days"));
  if (Number.isInteger(initial) && initial >= 1 && initial <= 90) {
    daysInput.value = String(initial);
  }
  fetch("/api/me")
    .then(function (response) { return response.ok ? response.json() : null; })
    .then(function (identity) {
      if (identity) { document.getElementById("whoami").textContent = identity.name; }
    });
  load();
}());
</script>
</body>
</html>
"""
)
