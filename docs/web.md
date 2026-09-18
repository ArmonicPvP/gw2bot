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

## Documents are built once, at import time

Every page is a fixed string assembled when its module is imported, so serving
one is a lookup rather than a render. Nothing about a member or a report is
interpolated into the HTML.

Dynamic data reaches a page only through the JSON APIs in `server.py` and is
inserted client-side with `textContent`. Event descriptions additionally pass
through a small Discord-markdown renderer that only ever builds DOM nodes and
text nodes, never HTML strings. A change that puts server data into page
markup breaks that property and needs a much harder look than a layout tweak.

## `pages/` - one module per document

| Module | What it holds |
| --- | --- |
| `shared.py` | The frame every dashboard is built from: the stylesheet, the header, and the time-range picker. A change to the frame is one edit here rather than four. |
| `notices.py` | The one-card pages: sign-in, signed-out, and the access refusals. Fixed strings except the sign-in page's login URL, which is escaped after `server.py` has validated it as a local path. |
| `calendar.py` | The guild event calendar. |
| `food.py` | The feast usage dashboard. |
| `roster.py` | The guild roster history dashboard. |
| `gold.py` | The guild bank gold history dashboard. |
| `profit/` | The Trading Post profit dashboard, split further - see below. |

`pages/__init__.py` re-exports every document, so `server.py` imports them all
from one place.

The names in `shared.py` are public (`SHARED_STYLE`, `DASHBOARD_HEADER_STYLE`,
`RANGE_PICKER_STYLE`, `RANGE_PICKER_NAV`, `CUSTOM_RANGE_PANEL`,
`RANGE_PICKER_LISTENERS_JS`, `range_picker_js`, `simple_page`,
`MAX_CUSTOM_DAYS`) because they are the page package's API. They were
underscore-prefixed while already being imported across modules, which was
only ever a mislabel.

## `pages/profit/` - style, markup and script

The profit dashboard was twice the size of any sibling, so its three parts are
separate modules assembled in `__init__.py`:

- `style.py` - the page's own CSS on top of the shared chrome.
- `markup.py` - the header, the range picker, and the report tables rendered
  empty. Also `PAGE_SIZE_DEFAULT`, `PAGE_SIZE_LIMIT`, and the two helpers that
  build the pagination bars and hidden-item windows.
- `script.py` - the client-side application: one IIFE that fetches each
  section, renders rows, and keeps the sort, pagination and hidden-item state
  the reader sets.

`__init__.py` concatenates them and stamps in the pagination bars and
hidden-item dialogs, so one control definition serves both paginated tables
above and below their rows.

The sibling pages keep their single module. They are around a thousand lines
each, which is a size worth leaving alone.

## Watch the escaping

The page modules hold raw HTML, CSS and JavaScript inside Python string
literals, and some of it contains backslashes that matter - `content: "\\2195"`
in the CSS is a Python-escaped backslash, not a unicode escape. When editing
these files by hand this is invisible; when editing them with a script, a
round trip through a decoded string will silently corrupt them. Compare the
rendered page before and after, not just the test results.
