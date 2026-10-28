# The web site

*Read before changing a served page, the chrome they share, or how a document
is assembled.*

`web/` is the optional aiohttp site: `auth.py` for Discord OAuth, `server.py`
for the routes and JSON APIs, `calendar.py` for the calendar entries the
calendar page draws, and `pages/` for the documents themselves.

## Every path is absolute from the host root

The pages link to each other and fetch their APIs with root-relative paths
(`/calendar`, `/api/profit`, `/login`), so the site has to be served from the
root of its host; there is no base-path setting to mount it under a prefix. The
calendar is one page among the others at `/calendar`, and `/` is only a
redirect to it, kept for bookmarks of the days the site had a host of its own.
That redirect is in `PUBLIC_PATHS` because it carries nothing about a member -
`/calendar` asks for the sign-in itself.

`auth.CALENDAR_PATH` is where an unrecognised sign-in target falls back to, and
`auth._RETURN_TARGET_PATHS` is the closed set a `?next=` may name. A new page
needs its path added there or sign-in will drop the reader on the calendar.

The dashboards gated behind a `/settings roles` role live under `/admin`:
`/admin/food`, `/admin/roster`, `/admin/gold` and `/admin/events`. The first
three answered at `/food`, `/roster` and `/gold` before; `LEGACY_DASHBOARD_PATHS`
in `server.py` keeps those as redirects, public for the same reason `/` is.
Their JSON APIs stayed where they were. The events page's API is
`/api/admin/events` because `/api/events` was already the calendar's.

## Documents are built once, at import time

Every page is a fixed string assembled when its module is imported, so serving
one is a lookup rather than a render. Nothing about a member or a report is
interpolated into the HTML.

Dynamic data reaches a page only through the JSON APIs in `server.py` and is
inserted client-side with `textContent`. Event descriptions additionally pass
through a small Discord-markdown renderer that only ever builds DOM nodes and
text nodes, never HTML strings. Its links - Discord's masked `[text](address)`,
an address in angle brackets, and a bare one - are the one place event text
reaches an attribute: an address becomes an `href` only when its rule matched
`http` or `https` and the browser's `URL` parser agrees, and anything else stays
the text it was typed as. The calendar's details let the pointer through to the
chips beneath them while hovered and take it only while pinned, which is what
lets those links be clicked, and a long description scrolls inside them rather
than past the bottom of the page. The details sit after the whole grid in the
document, so Tab from a pinned event is steered into them - the box itself
first when a long description runs past it, so the arrow keys can scroll it,
then each link - and stepping past either end lands beside the event again;
without that, a keyboard reader would reach them only after every later
event. Pinned, they are a `dialog` named for the event rather than a `tooltip`,
which assistive tech treats as inert text, and a click inside them is not the
click elsewhere that closes them. The profit page's item names are links built
the same way: the anchor is a node, its text is still set with `textContent`,
and the only server value that reaches its `href` is the item's id. A change
that puts server data into page markup breaks that property and needs a much
harder look than a layout tweak.

A page writes back through a JSON API too. The feast dashboard's cost editor
posts to `/api/food/cost` as whole copper against the stock log row the restock
was observed as, gated by the same role the page is; the profit dashboard's
hidden-item and hidden-trade controls post the same way.

The profit dashboard's Matched Trades table is the one table on the site paged
on the server: `/api/profit/trades` answers one page in the order and search
asked for, with totals across every page. A busy trader matches thousands of
pairs a month, which is more rows than are worth sending to be sorted and
hidden in the browser. It shares the other tables' pagination controls through
the `server` flag on its entry in `pagers`, which turns a page move into a
request, and its headings carry `data-server-sort` rather than
`data-sort-table` so the in-page sorter leaves them alone. It is requested
only after the realized report renders, because it reads the matches that
report's load brings up to date; two loads matching the same member's history
at once would add the same sales twice.

## `pages/` - one module per document

| Module | What it holds |
| --- | --- |
| `shared.py` | The frame every page is built from: the document head, the stylesheet, the header with its sign-out controls, the time-range picker, and the script every signed-in page opens with. The four charted dashboards are assembled whole by `chart_dashboard`, around their own rules, markup and script, with the chart, table, loading and hover code they share. A change to any of it is one edit here rather than four. |
| `notices.py` | The one-card pages: sign-in, signed-out, and the access refusals. Fixed strings except the sign-in page's login URL, which is escaped after `server.py` has validated it as a local path. |
| `calendar.py` | The guild event calendar. |
| `food.py` | The feast usage dashboard, including the Additions table and the cost editor its Edit pencil opens. |
| `roster.py` | The guild roster history dashboard. |
| `gold.py` | The guild bank gold history dashboard. |
| `events.py` | The guild event statistics dashboard: four headline tiles, the cumulative runs line, and four paginated tables. |
| `profit/` | The Trading Post profit dashboard, split further - see below. |

`pages/__init__.py` re-exports every document, so `server.py` imports them all
from one place.

The names in `shared.py` without a leading underscore are public because they
are the page package's API: other modules import them. They were
underscore-prefixed while already being imported across modules, which was only
ever a mislabel.

## One dashboard frame

The feast, roster, gold and events dashboards differ in what they chart and
list, not in how. `chart_dashboard(title, subject, style=, body=, script=)`
writes everything they share around what is their own: the document head, the
stylesheets (`CHART_PAGE_STYLE` and its predecessors, then the page's `style`,
which can override them), the header with the range picker and sign-out
controls, and a script that opens with the shared helpers, runs the page's
`script`, and ends by loading the window. `subject` names the page in every
console trace the shared code writes and in nothing else.

The shared script comes in parts, in the order they run:

| Part | What it is |
| --- | --- |
| `PAGE_JS` | What every signed-in page opens with, the calendar's included: the phone breakpoint, `el`, `fetchJson` (which sends a signed-out reader to sign in, so no page writes that check itself), and `showWhoami`. |
| `DASHBOARD_JS` | The charted dashboards' helpers: the stock chart's box and status line, `chartMetrics` (the shared proportions a page adjusts), `windowState`, `scaleX` and `scaleY`, `niceStep`, the tick and moment formats, the pointer narrowing, `logFailure`, `resetChart`, `chartCanvas`, the grid, axes, lines and dots, `showChart`, `renderPagedTable` with its pager, and `loadWindow`. Its header lists what a page must supply. |
| `CHART_HOVER_JS` | The hover and tap selection, `attachChartHover`, and the two ways of grouping dots into columns. |
| `CHART_MODE_JS`, `COIN_JS`, `HISTORY_JS` | Parts a page asks for in `parts`: the staircase toggle (feast, gold, roster), coin arithmetic (feast, gold), and what the two history pages share beyond the rest - the boxes `history_main` lays out, the legend, the totals line, the closing figure, the account column, the table of changes, and how the page loads and draws. `HISTORY_STYLE` and `history_main` are that pair's rules and markup. |

A page's own `attachHover` passes `attachChartHover` the tooltip's host, the
fixed reason a skipped tap is traced with, the ring colour, and a
`fillTooltip` that adds the page's rows. Every table is a
`renderPagedTable` spec: its columns, its rows, and where it pages to. A
column's class is its heading's too unless it names a `head` of its own, which
is how the feast and roster tables keep their numeric headings at the left
where gold and events right-align theirs.

The calendar and the profit page are not charted dashboards and are built by
hand, but they take the same `document_head` and `SIGNOUT_CONTROLS`, the
calendar takes `PAGE_JS`, and the profit page shares `UTILITY_STYLE` with the
feast page. The profit page's script, with its own requests and its own chart,
is its own.

## Two purples

The palette lives once, in `SHARED_STYLE`'s `:root`, and the purple there has
two shades with separate jobs. `--accent` is the fill behind a selected or
primary control, with `--on-accent` (white) as its text. Anything purple drawn
straight onto a dark surface (a link, today's date, the roster outline and
check mark, a focus ring) takes `--accent-on-dark`, because `--accent` is too
dark to read as text there. `TestPalette` in `tests/test_web_page.py` keeps
`var(--accent)` to fills and borders and checks each pairing against WCAG AA,
so a new shade or a new use fails there before it ships.

## The events page reads the member list

The events page's commander table counts everyone holding the role
`/event new` needs, and only Discord knows who that is. The bot connects
without the members intent, so discord.py's `Guild.fetch_members` refuses;
`server.py` pages through the REST member list behind it with
`bot.http.get_members` instead, which only needs the Server Members Intent
switched on for the application in the Developer Portal. Do not ask for the
intent in `bot.py` to get at the list: a deployment without the switch would
then fail to connect at all, and `TestBotIntent` holds it off. When the list
is refused the table falls back to the commanders who led a run, and the page
says so. The holders are cached for five minutes, and the names the list
carries seed the display-name cache so the tables need no lookup apiece.

## `pages/profit/` - style, markup and script

The profit dashboard was twice the size of any sibling, so its three parts are
separate modules assembled in `__init__.py`:

- `style.py` - the page's own CSS on top of the shared chrome.
- `markup.py` - the header, the range picker, and the report tables rendered
  empty. Also `PAGE_SIZE_DEFAULT`, `PAGE_SIZE_LIMIT`, and the helpers that
  build the pagination bars and the hidden-item and hidden-trade windows.
- `script.py` - the client-side application: one IIFE that fetches each
  section, renders rows, and keeps the sort, pagination and hidden-item state
  the reader sets.

`__init__.py` concatenates them and stamps in the pagination bars and
hidden-item and hidden-trade dialogs, so one control definition serves every
paginated table above and below its rows.

The sibling pages keep their single module. They are around a thousand lines
each, which is a size worth leaving alone.

## Watch the escaping

The page modules hold raw HTML, CSS and JavaScript inside Python string
literals, and some of it contains backslashes that matter - `content: "\\2195"`
in the CSS is a Python-escaped backslash, not a unicode escape. When editing
these files by hand this is invisible; when editing them with a script, a
round trip through a decoded string will silently corrupt them. Compare the
rendered page before and after, not just the test results.
