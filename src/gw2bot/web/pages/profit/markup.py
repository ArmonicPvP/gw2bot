"""The page body: the header, the range picker, and the empty report tables.

Every table is rendered empty here and filled by the script from the JSON
API, so no report data is ever interpolated into this markup.
"""

from gw2bot.web.pages.shared import CUSTOM_RANGE_PANEL, RANGE_PICKER_NAV

# Rows per page a paginated table opens on, and the largest it will accept.
PAGE_SIZE_DEFAULT = 10
PAGE_SIZE_LIMIT = 90


def _hidden_items_dialog(group: str, subject: str) -> str:
    """Return the Hidden items window for one table.

    Open Orders and Realized Profit by Item are each hidden from on their
    own, and each keeps its own list of what it is hiding, so the window is
    written once here and stamped per table. ``subject`` names the table in
    the prose inside it.
    """
    return f"""  <dialog id="{group}-hidden-dialog" class="hidden-dialog"
    aria-labelledby="{group}-hidden-title">
    <div class="modal-head">
      <h2 id="{group}-hidden-title">Hidden items</h2>
      <button id="{group}-hidden-close" class="icon-button" type="button"
        aria-label="Close hidden {subject} items">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none"
          stroke="currentColor" stroke-width="2" stroke-linecap="round"
          aria-hidden="true">
          <line x1="6" y1="6" x2="18" y2="18"></line>
          <line x1="18" y1="6" x2="6" y2="18"></line>
        </svg>
      </button>
    </div>
    <p class="note hidden-count" id="{group}-hidden-count"></p>
    <label class="modal-search" for="{group}-hidden-search">
      <span class="visually-hidden">Search hidden {subject} items</span>
      <input id="{group}-hidden-search" type="search"
        placeholder="Search hidden items" autocomplete="off">
    </label>
    <div class="modal-scroll"><table id="{group}-hidden-table"
      class="hidden-table">
      <thead><tr><th>Item</th><th>Restore</th></tr></thead>
      <tbody id="{group}-hidden-body"></tbody>
    </table></div>
  </dialog>
"""


def _pagination_nav(group: str, position: str, label: str) -> str:
    """Return one pagination bar for ``group``, above or below its table.

    Every paginated table carries the same control twice, so it is written
    once here rather than four times in the template: jump to the first or
    last page, step one page either way, or type the page wanted into the box
    between them. ``position`` says which of the two bars this is - the page
    box needs an id of its own for its label, and the bottom bar is the one
    that carries the rows-per-page control.
    """

    def step(name: str, glyph: str, description: str) -> str:
        return (
            '          <button class="page-step" type="button"'
            f' data-page-group="{group}" data-page-step="{name}"'
            f' aria-label="{description}">{glyph}</button>\n'
        )

    box = f"{group}-page-{position}"
    size = (
        f'        <label class="page-size" for="{group}-page-size">'
        "Rows per page\n"
        f'          <input id="{group}-page-size" type="number" min="1"'
        f' max="{PAGE_SIZE_LIMIT}" value="{PAGE_SIZE_DEFAULT}"'
        f' class="page-size-input" data-page-group="{group}">\n'
        "        </label>\n"
    )
    return (
        f'      <nav class="pagination" aria-label="{label}">\n'
        + (size if position == "bottom" else "")
        + f'        <span class="pagination-pages" id="{group}-pages-'
        f'{position}">\n'
        + step("first", "&#171;", "First page")
        + step("previous", "&#8249;", "Previous page")
        + '          <span class="page-current">\n'
        f'            <label class="visually-hidden" for="{box}">'
        "Page number</label>\n"
        f'            <input id="{box}" class="page-input" type="number"'
        f' min="1" value="1" inputmode="numeric" data-page-group="{group}">\n'
        f'            <span class="page-total" data-page-group="{group}">'
        "of 1</span>\n"
        "          </span>\n"
        + step("next", "&#8250;", "Next page")
        + step("last", "&#187;", "Last page")
        + "        </span>\n"
        "      </nav>"
    )


PROFIT_MARKUP = (
    """<header>
  <h1 id="brand">Trading Post Profit</h1>
  <div class="controls">
"""
    + RANGE_PICKER_NAV
    + """
    <button class="primary" type="button" id="reload">Reload</button>
  </div>
  <span class="spacer"></span>
  <a href="/">Calendar</a>
  <span id="whoami"></span>
  <form method="post" action="/logout">
    <button type="submit" class="signout" aria-label="Sign out">
      <svg class="signout-icon" viewBox="0 0 24 24" width="18" height="18"
        fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path>
        <polyline points="16 17 21 12 16 7"></polyline>
        <line x1="21" y1="12" x2="9" y2="12"></line>
      </svg>
      <span class="signout-label">Sign out</span>
    </button>
  </form>
"""
    + CUSTOM_RANGE_PANEL
    + """
</header>
<main>
  <div id="status" role="status" aria-live="polite">Loading&hellip;</div>
  <div id="key-help">
    No Trading Post API key is saved for this Discord account. Run
    <code>/profit setkey</code> in the guild server, then reload this page.
  </div>
  <div class="cards" id="reports" hidden>
    <section class="card loading" data-source="report">
      <h2>Summary</h2>
      <p class="note">FIFO-matched realized results and current-listing projections for the selected window.</p>
      <div class="table-scroll"><table>
        <thead><tr><th>Measure</th><th>Value</th></tr></thead>
        <tbody id="summary-body"></tbody>
      </table></div>
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
    <section class="card loading" data-source="report">
      <h2>Daily Profit Trends</h2>
      <p class="note">UTC sale dates; days without matched sales count as zero profit.</p>
      <div class="chart-grid">
        <figure class="chart-panel">
          <h3>Daily Profit and Average</h3>
          <p>Realized profit each day with the whole-window daily average.</p>
          <div class="profit-chart"><svg id="daily-profit-chart" viewBox="0 0 640 220" role="img" aria-label="Daily realized profit and average"></svg></div>
        </figure>
        <figure class="chart-panel">
          <h3>7-Day Rolling Average</h3>
          <p>Trailing mean across seven UTC date buckets, including the six dates before the window.</p>
          <div class="profit-chart"><svg id="rolling-profit-chart" viewBox="0 0 640 220" role="img" aria-label="Seven-day rolling average realized profit"></svg></div>
        </figure>
        <figure class="chart-panel">
          <h3>Cumulative Profit</h3>
          <p>Running realized profit across the selected window.</p>
          <div class="profit-chart"><svg id="cumulative-profit-chart" viewBox="0 0 640 220" role="img" aria-label="Cumulative realized profit"></svg></div>
        </figure>
      </div>
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
    <section class="card loading" data-source="report">
      <h2>Your Picks</h2>
      <p class="note">Current buy-order and sell-listing returns for items you flipped in the selected window. Prices include Trading Post fees, and items whose current return is negative are left out. Sorting a column ranks every pick by it and shows the ten rows at the top, so sorting by Profit / Unit lists the highest profit and sorting by ROI the highest ROI.</p>
      <div class="table-scroll"><table id="picks-table" data-sort-table="picks">
        <thead><tr>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="0" data-sort-kind="text" data-sort-key="item" data-sort-default="ascending">Item</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="1" data-sort-kind="number" data-sort-key="buy-price" data-sort-default="descending">Buy Order</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="2" data-sort-kind="number" data-sort-key="sell-price" data-sort-default="descending">Sell Price</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="3" data-sort-kind="number" data-sort-key="pick-profit" data-sort-default="descending">Profit / Unit</button></th>
          <th aria-sort="descending"><button class="sort-button" type="button" data-sort-index="4" data-sort-kind="number" data-sort-key="pick-roi" data-sort-default="descending">ROI</button></th>
        </tr></thead>
        <tbody id="picks-body"></tbody>
      </table></div>
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
    <section class="card loading" data-source="report">
      <div class="card-heading">
        <h2>Realized Profit by Item</h2>
        <button id="items-menu" class="icon-button" type="button"
          aria-haspopup="dialog" title="Hidden items">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"
            aria-hidden="true">
            <circle cx="12" cy="5" r="2"></circle>
            <circle cx="12" cy="12" r="2"></circle>
            <circle cx="12" cy="19" r="2"></circle>
          </svg>
        </button>
      </div>
      <p class="note">Avg Hold is the mean time those units were held, weighted by units. Profit Share is signed item profit divided by total realized profit. The crossed-out eye on a row leaves that item out of every figure on this page; the three dots above put a hidden item back.</p>
__ITEMS_PAGES_TOP__
      <div class="table-scroll"><table id="items-table" data-sort-table="items">
        <thead><tr>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="0" data-sort-kind="text" data-sort-key="item" data-sort-default="ascending">Item</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="1" data-sort-kind="number" data-sort-key="units" data-sort-default="descending">Units</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="2" data-sort-kind="number" data-sort-key="cost" data-sort-default="descending">Cost</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="3" data-sort-kind="number" data-sort-key="net-revenue" data-sort-default="descending">Net Revenue</button></th>
          <th aria-sort="descending"><button class="sort-button" type="button" data-sort-index="4" data-sort-kind="number" data-sort-key="profit" data-sort-default="descending">Profit</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="5" data-sort-kind="number" data-sort-key="roi" data-sort-default="descending">ROI</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="6" data-sort-kind="number" data-sort-key="profit-per-unit" data-sort-default="descending">Profit / Unit</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="7" data-sort-kind="number" data-sort-key="avg-hold" data-sort-default="ascending">Avg Hold</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="8" data-sort-kind="number" data-sort-key="profit-share" data-sort-default="descending">Profit Share</button></th>
          <th class="actions">Hide</th>
        </tr></thead>
        <tbody id="items-body"></tbody>
        <tfoot id="items-foot"></tfoot>
      </table></div>
__ITEMS_PAGES_BOTTOM__
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
    <section class="card loading" data-source="report">
      <h2>Realized Profit by Day</h2>
__DAYS_PAGES_TOP__
      <div class="table-scroll"><table id="days-table" data-sort-table="days">
        <thead><tr>
          <th aria-sort="descending"><button class="sort-button" type="button" data-sort-index="0" data-sort-kind="text" data-sort-key="date" data-sort-default="descending">Date</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="1" data-sort-kind="number" data-sort-key="units" data-sort-default="descending">Units</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="2" data-sort-kind="number" data-sort-key="cost" data-sort-default="descending">Cost</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="3" data-sort-kind="number" data-sort-key="net-revenue" data-sort-default="descending">Net Revenue</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="4" data-sort-kind="number" data-sort-key="profit" data-sort-default="descending">Profit</button></th>
        </tr></thead>
        <tbody id="days-body"></tbody>
        <tfoot id="days-foot"></tfoot>
      </table></div>
__DAYS_PAGES_BOTTOM__
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
    <section class="card loading" data-source="report">
      <h2>Unrealized Profit</h2>
      <p class="note">Purchases you still hold that are currently listed for sale, from all your stored history rather than only the selected window. Stock listed at two prices is two rows, as in Open Orders. Your Price is what you listed at; Lowest Sell is the cheapest anyone is asking now, so a higher Your Price means someone is undercutting you. Projected ROI is projected profit divided by their matched cost.</p>
      <div class="table-scroll"><table id="unrealized-table" data-sort-table="unrealized">
        <thead><tr>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="0" data-sort-kind="text" data-sort-key="item" data-sort-default="ascending">Item</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="1" data-sort-kind="number" data-sort-key="units" data-sort-default="descending">Units</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="2" data-sort-kind="number" data-sort-key="listing-price" data-sort-default="descending">Your Price</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="3" data-sort-kind="number" data-sort-key="cost" data-sort-default="descending">Cost</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="4" data-sort-kind="number" data-sort-key="lowest-sell" data-sort-default="descending">Lowest Sell</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="5" data-sort-kind="number" data-sort-key="projected-sale" data-sort-default="descending">Projected Sale</button></th>
          <th aria-sort="descending"><button class="sort-button" type="button" data-sort-index="6" data-sort-kind="number" data-sort-key="projected-profit" data-sort-default="descending">Projected Profit</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="7" data-sort-kind="number" data-sort-key="projected-roi" data-sort-default="descending">Projected ROI</button></th>
        </tr></thead>
        <tbody id="unrealized-body"></tbody>
        <tfoot id="unrealized-foot"></tfoot>
      </table></div>
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
    <section class="card loading" data-source="orders">
      <div class="card-heading">
        <h2>Open Orders</h2>
        <button id="orders-menu" class="icon-button" type="button"
          aria-haspopup="dialog" title="Hidden items">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"
            aria-hidden="true">
            <circle cx="12" cy="5" r="2"></circle>
            <circle cx="12" cy="12" r="2"></circle>
            <circle cx="12" cy="19" r="2"></circle>
          </svg>
        </button>
      </div>
      <p class="note">Items you are currently buying on the Trading Post. Orders for the same item at the same price are shown as one row. Profit assumes selling the whole order at the current lowest sell after the 5% listing and 10% exchange fees, and ROI is that profit over what the order costs you.</p>
      <p class="note" id="orders-unpriced" hidden>Rows with no current Trading Post price show dashes and are left out of every total below, so the totals cover the same orders throughout.</p>
      <p class="note" id="orders-key-help" hidden>Open orders are unavailable for this saved key. Run <code>/profit setkey</code> again with a key that allows <code>/v2/commerce/transactions/current/buys</code>.</p>
      <div class="table-scroll"><table id="orders-table" data-sort-table="orders">
        <thead><tr>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="0" data-sort-kind="text" data-sort-key="item" data-sort-default="ascending">Item</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="1" data-sort-kind="number" data-sort-key="units" data-sort-default="descending">Units</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="2" data-sort-kind="number" data-sort-key="order-price" data-sort-default="descending">Your Price</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="3" data-sort-kind="number" data-sort-key="order-cost" data-sort-default="descending">Order Cost</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="4" data-sort-kind="number" data-sort-key="highest-buy" data-sort-default="descending">Highest Buy</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="5" data-sort-kind="number" data-sort-key="lowest-sell" data-sort-default="descending">Lowest Sell</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="6" data-sort-kind="number" data-sort-key="order-profit" data-sort-default="descending">Profit / Unit</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="7" data-sort-kind="number" data-sort-key="order-total-profit" data-sort-default="descending">Total Profit</button></th>
          <th aria-sort="descending"><button class="sort-button" type="button" data-sort-index="8" data-sort-kind="number" data-sort-key="order-roi" data-sort-default="descending">ROI</button></th>
          <th class="actions">Hide</th>
        </tr></thead>
        <tbody id="orders-body"></tbody>
        <tfoot id="orders-foot"></tfoot>
      </table></div>
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
    <section class="card loading" data-source="delivery">
      <h2>Unclaimed Trading Post</h2>
      <p class="note">Coins and items waiting for pickup in your Trading Post delivery box.</p>
      <p class="note" id="delivery-key-help" hidden>Delivery access is unavailable for this saved key. Run <code>/profit setkey</code> again with a key that allows the Trading Post delivery endpoint.</p>
      <div class="table-scroll"><table>
        <thead><tr><th>Measure</th><th>Value</th></tr></thead>
        <tbody>
          <tr><td>Gold available to collect</td><td id="unclaimed-coins">0c</td></tr>
        </tbody>
      </table></div>
      <h3 class="subheading">Items available to collect</h3>
      <p class="note">Your Price is a cost basis, not a receipt: it averages your newest purchases of that item that no sale has been matched against. Collecting empties the box in one go, so what is waiting is normally the run of buys you have made since — and stock of one item is interchangeable, so a stack that arrived another way, such as a cancelled sell listing, is priced from the same purchases. Projected Sale assumes selling the whole stack at the current lowest sell after the 5% listing and 10% exchange fees, and Projected ROI is the profit over that cost.</p>
      <p class="note" id="delivery-unpriced" hidden>Rows with no current price, or with purchases that cover only part of the stack, show dashes and are left out of the totals below, so the totals cover the same stacks throughout.</p>
      <div class="table-scroll"><table id="delivery-table" data-sort-table="delivery">
        <thead><tr>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="0" data-sort-kind="text" data-sort-key="item" data-sort-default="ascending">Item</button></th>
          <th aria-sort="descending"><button class="sort-button" type="button" data-sort-index="1" data-sort-kind="number" data-sort-key="quantity" data-sort-default="descending">Quantity</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="2" data-sort-kind="number" data-sort-key="delivery-price" data-sort-default="descending">Your Price</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="3" data-sort-kind="number" data-sort-key="delivery-cost" data-sort-default="descending">Cost</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="4" data-sort-kind="number" data-sort-key="highest-buy" data-sort-default="descending">Highest Buy</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="5" data-sort-kind="number" data-sort-key="lowest-sell" data-sort-default="descending">Lowest Sell</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="6" data-sort-kind="number" data-sort-key="delivery-sale" data-sort-default="descending">Projected Sale</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="7" data-sort-kind="number" data-sort-key="delivery-profit" data-sort-default="descending">Projected Profit</button></th>
          <th aria-sort="none"><button class="sort-button" type="button" data-sort-index="8" data-sort-kind="number" data-sort-key="delivery-roi" data-sort-default="descending">Projected ROI</button></th>
        </tr></thead>
        <tbody id="delivery-body"></tbody>
        <tfoot id="delivery-foot"></tfoot>
      </table></div>
      <div class="section-spinner" role="status"><span class="spinner"></span><span class="section-message">Loading\u2026</span></div>
    </section>
  </div>
__ORDERS_HIDDEN_DIALOG__
__ITEMS_HIDDEN_DIALOG__
</main>
"""
)
