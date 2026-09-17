"""The profit dashboard's stylesheet, on top of the shared page chrome."""

from gw2bot.web.pages.shared import (
    DASHBOARD_HEADER_STYLE,
    RANGE_PICKER_STYLE,
    SHARED_STYLE,
)

PROFIT_STYLE = (
    SHARED_STYLE
    + DASHBOARD_HEADER_STYLE
    + RANGE_PICKER_STYLE
    + """
body { display: flex; flex-direction: column; }
/* The range buttons and the reload beside them travel together, so they move
   to their own header row on a narrow screen as one block. */
.controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.45rem;
}
label { color: var(--muted); font-size: 0.85rem; }
input {
  width: 4.6rem;
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.35rem 0.5rem;
  font: inherit;
  font-size: 0.85rem;
}
.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
/* The narrow width above is meant for the paginator's number boxes; a date
   field needs whatever its browser's spelling of a date takes. */
.custom input[type="date"] { width: auto; }
main { width: 100%; margin: 0; padding: 1rem; }
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
.pagination {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.35rem;
  padding: 0.65rem 0.8rem;
}
.pagination-pages {
  display: flex;
  align-items: center;
  gap: 0.25rem;
  margin-left: auto;
}
.pagination button { min-width: 2rem; padding: 0.3rem 0.5rem; }
/* The first and last steps sit at the ends of the run, so they stop being
   offered once the reader is already there rather than moving nothing. */
.pagination button:disabled { opacity: 0.4; cursor: default; }
.pagination button:disabled:hover { background: var(--panel-2); }
.page-step { line-height: 1; }
.page-current {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  color: var(--muted);
  font-size: 0.85rem;
}
.page-current input { width: 3.6rem; text-align: center; }
.page-size { display: flex; align-items: center; gap: 0.4rem; }
.page-size input { width: 4rem; }
/* Narrowing the item table: a word to match and a category to pick, on the
   row between the table's prose and its first pagination bar. */
.table-filters {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.5rem;
  padding: 0 1rem 0.2rem;
}
.filter-field {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
}
/* The bare `input` width above is meant for the paginator's number boxes. */
.table-filters input[type="search"] { width: min(16rem, 60vw); }
.table-filters select {
  max-width: min(14rem, 60vw);
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.35rem 0.5rem;
  font: inherit;
  font-size: 0.85rem;
}
.filter-count { color: var(--muted); font-size: 0.82rem; }
.filter-empty { padding-top: 0.4rem; }
.card-heading { display: flex; align-items: center; gap: 0.75rem; padding-right: 1rem; }
.card-heading h2 { flex: 1; }
.card h3.subheading { font-size: 0.92rem; padding: 0.85rem 1rem 0.15rem; }
/* A loading card keeps its heading and shows a spinner where its body will
   be, so the page reads as a list of named sections filling in rather than a
   blank screen that appears all at once. */
.card.loading { min-height: 7.5rem; }
.card.loading > :not(.section-spinner):not(h2):not(.card-heading) {
  display: none;
}
.section-spinner { display: none; }
.card.loading .section-spinner, .card.failed .section-spinner {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.6rem;
  padding: 2rem 1rem;
  color: var(--muted);
  font-size: 0.85rem;
}
.card.failed > :not(.section-spinner):not(h2):not(.card-heading) {
  display: none;
}
.card.failed .spinner { display: none; }
.spinner {
  width: 1.5rem;
  height: 1.5rem;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: profit-spin 0.8s linear infinite;
}
@keyframes profit-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
  .spinner { animation-duration: 2.4s; }
}
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}
.icon-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0.3rem;
  background: transparent;
  border-color: transparent;
  color: var(--muted);
  line-height: 0;
}
.icon-button:hover { color: var(--text); background: var(--panel-2); }
.icon-button svg { pointer-events: none; }
.row-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0.25rem 0.4rem;
  line-height: 0;
  color: var(--muted);
}
.row-action:hover { color: var(--text); }
.row-action svg { pointer-events: none; }
/* The row actions are otherwise icons, and an icon button is drawn with no
   line box at all so the glyph decides its height. A word needs one back, or
   the button closes to a sliver and the text spills past its edges. */
.text-action {
  padding: 0.35rem 0.7rem;
  line-height: 1.2;
  white-space: nowrap;
}
/* The heading and the icon under it are centred together, or the icon reads
   as sitting off to one side of a right-aligned label. */
th.actions, td.actions { text-align: center; }
.hidden-dialog {
  /* The shared reset zeroes every margin, which takes the centring a modal
     dialog normally gets from the user agent's `margin: auto` with it. */
  margin: auto;
  width: min(30rem, calc(100vw - 2rem));
  /* A list of hidden items is a list to scan, so the window takes the height
     it can get rather than the height its rows happen to need: a short list
     is not worth a taller window, but a long one was being read four rows at
     a time through a panel a third of the screen. */
  height: calc(100vh - 4rem);
  max-height: calc(100vh - 4rem);
  padding: 0;
  background: var(--panel);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}
.hidden-dialog::backdrop { background: rgba(0, 0, 0, 0.55); }
.hidden-dialog[open] { display: flex; flex-direction: column; }
.modal-head {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.85rem 0.7rem 0.25rem 1rem;
}
.modal-head h2 { flex: 1; font-size: 1rem; }
.hidden-count { padding: 0 1rem 0.6rem; }
.modal-search { display: block; padding: 0 1rem 0.85rem; }
.modal-search input { width: 100%; }
/* The head, the count and the search box are as tall as they are; the table
   takes everything the window has left, so the rows are what grows. */
.modal-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  border-top: 1px solid var(--border);
}
.hidden-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  border-top: 0;
}
.hidden-table th:last-child, .hidden-table td:last-child {
  width: 1%;
  text-align: right;
}
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
.sort-button {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.35rem;
  width: 100%;
  padding: 0;
  color: inherit;
  background: transparent;
  border: 0;
  border-radius: 3px;
  font: inherit;
  font-weight: inherit;
}
th:first-child .sort-button { justify-content: flex-start; }
.sort-button:hover { color: var(--text); background: transparent; }
.sort-button:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.sort-button::after { content: "\\2195"; color: var(--muted); font-size: 0.75rem; }
th[aria-sort="ascending"] .sort-button::after { content: "\\25B2"; color: var(--text); }
th[aria-sort="descending"] .sort-button::after { content: "\\25BC"; color: var(--text); }
td.name { white-space: normal; overflow-wrap: anywhere; min-width: 12rem; }
tfoot td { font-weight: 700; background: var(--panel-2); }
.positive { color: #74dc9a; }
.negative { color: #ff8f86; }
.empty { color: var(--muted); text-align: center !important; padding: 1.2rem; }
.chart-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0.75rem;
  padding: 0 1rem 1rem;
}
.chart-panel {
  min-width: 0;
  padding: 0.75rem;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.chart-panel h3 { font-size: 0.88rem; margin-bottom: 0.1rem; }
.chart-panel p { color: var(--muted); font-size: 0.75rem; min-height: 2.1rem; }
.profit-chart { position: relative; margin-top: 0.4rem; }
.chart-panel svg { display: block; width: 100%; height: auto; }
.chart-gridline { stroke: var(--border); stroke-width: 1; }
.chart-zero { stroke: var(--muted); stroke-width: 1; }
.chart-average { stroke: #f1c40f; stroke-width: 2; stroke-dasharray: 6 4; }
.chart-rolling { fill: none; stroke: #58a6ff; stroke-width: 2.5; }
.chart-cumulative { fill: none; stroke: #74dc9a; stroke-width: 2.5; }
.chart-point-rolling { fill: #58a6ff; }
.chart-point-cumulative { fill: #74dc9a; }
.chart-bar-positive { fill: #74dc9a; }
.chart-bar-negative { fill: #ff8f86; }
.chart-label { fill: var(--muted); font-size: 11px; }
.chart-empty { fill: var(--muted); font-size: 13px; text-anchor: middle; }
.chart-overlay { fill: transparent; cursor: crosshair; }
.chart-crosshair {
  stroke: rgba(128, 128, 128, 0.45);
  stroke-width: 1;
  pointer-events: none;
}
.chart-hover-ring { fill: none; stroke-width: 2; pointer-events: none; }
.chart-tooltip {
  position: absolute;
  z-index: 2;
  width: max-content;
  min-width: min(9rem, calc(100% - 1rem));
  max-width: min(16rem, calc(100% - 1rem));
  padding: 0.45rem 0.55rem;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  font-size: 0.78rem;
  pointer-events: none;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.35);
}
.chart-tooltip .tip-date { color: var(--muted); margin-bottom: 0.3rem; }
.chart-tooltip .tip-row { display: flex; align-items: center; gap: 0.4rem; }
.chart-tooltip .swatch {
  width: 0.7rem;
  height: 0.7rem;
  border-radius: 2px;
  flex-shrink: 0;
}
.chart-tooltip .tip-value {
  margin-left: auto;
  padding-left: 0.75rem;
  font-variant-numeric: tabular-nums;
}
@media (max-width: 640px) {
  .controls {
    grid-column: 1 / -1;
    grid-row: 2;
    justify-self: center;
  }
  header a { grid-column: 1; grid-row: 1; justify-self: start; }
  .spacer { display: none; }
  main { padding: 0.75rem; }
  th, td { padding: 0.55rem 0.65rem; }
  .chart-grid { grid-template-columns: 1fr; padding: 0 0.75rem 0.75rem; }
}
"""
)
