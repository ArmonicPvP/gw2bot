import re

from gw2bot.web.page import (
    CALENDAR_PAGE,
    FOOD_PAGE,
    GOLD_PAGE,
    ROSTER_PAGE,
    sign_in_page,
)
from gw2bot.web.server import MAX_CUSTOM_WINDOW_SECONDS
from gw2bot.web.profit_page import PROFIT_PAGE


def _call_arguments(source: str, name: str) -> list[list[str]]:
    """Return the argument list of every ``name(...)`` call in ``source``.

    Arguments are split on top-level commas with runs of whitespace collapsed,
    so a call spread over several lines reads the same as an inline one.
    """
    calls: list[list[str]] = []
    for match in re.finditer(r"\b" + re.escape(name) + r"\(", source):
        if source[:match.start()].rstrip().endswith("function"):
            continue  # the declaration's parameter list, not a call
        depth = 1
        index = match.end()
        while depth and index < len(source):
            if source[index] == "(":
                depth += 1
            elif source[index] == ")":
                depth -= 1
            index += 1
        assert not depth, "unbalanced call to %s" % name
        inner = source[match.end():index - 1]
        args: list[str] = []
        depth = 0
        current = ""
        for char in inner:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if char == "," and not depth:
                args.append(current)
                current = ""
            else:
                current += char
        args.append(current)
        calls.append([" ".join(arg.split()) for arg in args])
    return calls


class TestCalendarMarkdown:
    def test_renders_discord_subtext_lines(self) -> None:
        assert 'var subtext = /^-#\\s+(.*)$/.exec(line);' in CALENDAR_PAGE
        assert 'el("div", "md-subtext")' in CALENDAR_PAGE
        assert "#tooltip .desc .md-subtext" in CALENDAR_PAGE


class TestProfitPage:
    def test_contains_all_profit_reports_and_unclaimed_delivery(self) -> None:
        assert "Summary" in PROFIT_PAGE
        assert "Realized Profit by Item" in PROFIT_PAGE
        assert "Realized Profit by Day" in PROFIT_PAGE
        assert "Unrealized Profit" in PROFIT_PAGE
        assert "Open Orders" in PROFIT_PAGE
        assert "Unclaimed Trading Post" in PROFIT_PAGE
        assert "Unclaimed Trading Post Gold" not in PROFIT_PAGE
        assert 'id="unclaimed-coins"' in PROFIT_PAGE
        assert 'id="delivery-key-help" hidden' in PROFIT_PAGE
        assert "coin(data.delivery.coins)" in PROFIT_PAGE
        assert "data.delivery.coins === null" in PROFIT_PAGE
        assert 'coins.textContent = "Unavailable";' in PROFIT_PAGE
        assert "help.hidden = false;" in PROFIT_PAGE
        assert "renderDelivery(data);" in PROFIT_PAGE
        assert 'fetch("/api/profit" + (days === null' in PROFIT_PAGE

    def test_delivery_lists_each_waiting_item_instead_of_one_count(
        self,
    ) -> None:
        assert "Items available to collect" in PROFIT_PAGE
        assert 'id="delivery-table" data-sort-table="delivery"' in PROFIT_PAGE
        assert 'id="delivery-body"' in PROFIT_PAGE
        assert 'id="delivery-foot"' in PROFIT_PAGE
        assert "var items = data.delivery.items;" in PROFIT_PAGE
        assert "quantity += item.quantity;" in PROFIT_PAGE
        assert '"No items are waiting for pickup."' in PROFIT_PAGE
        # The old single-count row is gone rather than kept alongside it.
        assert 'id="unclaimed-items"' not in PROFIT_PAGE

    def test_open_orders_shows_both_market_sides_with_profit_and_roi(
        self,
    ) -> None:
        assert 'id="orders-table" data-sort-table="orders"' in PROFIT_PAGE
        assert ">Your Price<" in PROFIT_PAGE
        assert ">Highest Buy Order<" in PROFIT_PAGE
        assert ">Lowest Sell Listing<" in PROFIT_PAGE
        assert ">Profit / Unit<" in PROFIT_PAGE
        assert ">ROI<" in PROFIT_PAGE
        assert "optionalCoinCell(row, order.buy_price);" in PROFIT_PAGE
        assert "optionalCoinCell(row, order.sell_price);" in PROFIT_PAGE
        assert "optionalProfitCell(row, order.profit);" in PROFIT_PAGE
        assert "percentCell(row, order.roi_percent);" in PROFIT_PAGE
        assert 'id="orders-key-help" hidden' in PROFIT_PAGE
        assert "help.hidden = ordersAvailable;" in PROFIT_PAGE

    def test_open_order_totals_cover_the_priced_rows_only(self) -> None:
        assert 'id="orders-unpriced" hidden' in PROFIT_PAGE
        assert "if (order.total_profit === null) {" in PROFIT_PAGE
        assert "unpriced += 1;" in PROFIT_PAGE
        assert 'document.getElementById("orders-unpriced").hidden' in (
            PROFIT_PAGE
        )
        assert "percent(cost ? profit / cost * 100 : null)" in PROFIT_PAGE

    def test_open_orders_hiding_is_saved_and_restorable(self) -> None:
        assert 'fetch("/api/profit/exclusions", {' in PROFIT_PAGE
        assert 'method: "POST",' in PROFIT_PAGE
        assert (
            "JSON.stringify({ item_id: itemId, excluded: excluded })"
            in PROFIT_PAGE
        )
        assert "function renderHiddenOrders()" in PROFIT_PAGE
        assert "function restoreButton(order)" in PROFIT_PAGE
        assert "return row.has_order && !row.excluded;" in PROFIT_PAGE

    def test_hidden_items_live_behind_the_open_orders_menu(self) -> None:
        assert 'id="orders-menu" class="icon-button"' in PROFIT_PAGE
        assert 'aria-haspopup="dialog"' in PROFIT_PAGE
        # Three dots, drawn rather than typed so they line up at any size.
        assert PROFIT_PAGE.count('<circle cx="12" cy="5" r="2">') == 1
        assert PROFIT_PAGE.count('<circle cx="12" cy="12" r="2">') == 1
        assert PROFIT_PAGE.count('<circle cx="12" cy="19" r="2">') == 1
        assert '<dialog id="hidden-dialog"' in PROFIT_PAGE
        assert '<h2 id="hidden-title">Hidden items</h2>' in PROFIT_PAGE
        assert "dialog.showModal();" in PROFIT_PAGE
        assert "function openHiddenItems()" in PROFIT_PAGE
        assert 'document.getElementById("hidden-dialog").close();' in (
            PROFIT_PAGE
        )
        # The old always-on chip list is gone, not merely hidden.
        assert "Excluded items" not in PROFIT_PAGE
        assert 'id="orders-excluded-list"' not in PROFIT_PAGE

    def test_hidden_items_are_searchable_in_a_table(self) -> None:
        assert 'id="hidden-search" type="search"' in PROFIT_PAGE
        assert '<table id="hidden-table">' in PROFIT_PAGE
        assert 'id="hidden-body"' in PROFIT_PAGE
        assert "order.name.toLowerCase().indexOf(search) !== -1" in PROFIT_PAGE
        assert '"No hidden items match that search."' in PROFIT_PAGE
        assert '"You have not hidden any items yet."' in PROFIT_PAGE
        assert 'addEventListener(\n    "input", renderHiddenOrders)' in (
            PROFIT_PAGE
        )

    def test_hiding_a_row_uses_an_icon_rather_than_a_word(self) -> None:
        assert "<th>Hide</th>" in PROFIT_PAGE
        assert "<th>Exclude</th>" not in PROFIT_PAGE
        assert "function hideButton(order)" in PROFIT_PAGE
        assert 'button.title = "Hide " + order.name;' in PROFIT_PAGE
        assert 'button.setAttribute("aria-label", "Hide " + order.name);' in (
            PROFIT_PAGE
        )
        # An eye with a line struck through it, so the row keeps its width.
        assert 'd: "M14.12 14.12a3 3 0 1 1-4.24-4.24"' in PROFIT_PAGE
        assert 'svgNode("line", {x1: 2, y1: 2, x2: 22, y2: 22})' in PROFIT_PAGE

    def test_report_window_follows_the_remembered_choice(self) -> None:
        assert "daysInput.value = String(data.days);" in PROFIT_PAGE
        assert 'history.replaceState(\n      null, "", "/profit?days=" +' in (
            PROFIT_PAGE
        )
        assert "function load(useRemembered)" in PROFIT_PAGE
        assert "load(!requested);" in PROFIT_PAGE
        assert "load(false);" in PROFIT_PAGE

    def test_daily_profit_opens_with_the_most_recent_day_first(self) -> None:
        # The first page of a 90-day window should be this week, not the
        # start of the window, so the date column starts descending.
        assert (
            '<th aria-sort="descending"><button class="sort-button" '
            'type="button" data-sort-index="0" data-sort-kind="text" '
            'data-sort-key="date" data-sort-default="descending">Date</button>'
        ) in PROFIT_PAGE
        assert 'data-sort-key="date"' in PROFIT_PAGE
        assert (
            '<th aria-sort="ascending"><button class="sort-button" '
            'type="button" data-sort-index="0" data-sort-kind="text" '
            'data-sort-key="date"'
        ) not in PROFIT_PAGE

    def test_daily_profit_has_adjustable_pagination(self) -> None:
        assert 'id="days-page-size" type="number" min="1" max="90" value="10"' in PROFIT_PAGE
        assert 'id="days-pages-top"' in PROFIT_PAGE
        assert 'id="days-pages-bottom"' in PROFIT_PAGE
        assert "function paginateDays()" in PROFIT_PAGE
        assert 'button.setAttribute("aria-current", "page")' in PROFIT_PAGE

    def test_dynamic_trading_post_values_never_become_markup(self) -> None:
        assert "innerHTML" not in PROFIT_PAGE
        assert "textContent" in PROFIT_PAGE

    def test_missing_key_points_to_the_prefixed_command(self) -> None:
        assert "/profit setkey" in PROFIT_PAGE

    def test_expired_session_login_preserves_the_profit_window(self) -> None:
        assert 'location.href = "/login?next=" + encodeURIComponent(' in (
            PROFIT_PAGE
        )
        assert "location.pathname + location.search" in PROFIT_PAGE

    def test_detail_tables_have_accessible_sort_buttons(self) -> None:
        assert PROFIT_PAGE.count('data-sort-table="') == 5
        assert PROFIT_PAGE.count('class="sort-button"') == 31
        assert 'id="items-table" data-sort-table="items"' in PROFIT_PAGE
        assert 'id="days-table" data-sort-table="days"' in PROFIT_PAGE
        assert (
            'id="unrealized-table" data-sort-table="unrealized"'
            in PROFIT_PAGE
        )
        assert 'id="orders-table" data-sort-table="orders"' in PROFIT_PAGE
        assert 'id="delivery-table" data-sort-table="delivery"' in PROFIT_PAGE
        assert 'th[aria-sort="ascending"]' in PROFIT_PAGE
        assert 'th[aria-sort="descending"]' in PROFIT_PAGE

    def test_sorting_uses_raw_values_and_leaves_totals_out(self) -> None:
        assert 'node.dataset.sortValue = String(sortValue);' in PROFIT_PAGE
        assert 'body.querySelectorAll("tr[data-sort-row]")' in PROFIT_PAGE
        assert 'row.dataset.sortRow = "true";' in PROFIT_PAGE
        assert 'var rows = Array.prototype.slice.call(' in PROFIT_PAGE
        assert 'rows.forEach(function (row) { body.appendChild(row); });' in (
            PROFIT_PAGE
        )

    def test_a_new_report_keeps_each_tables_selected_sort(self) -> None:
        assert 'applySort("items-table");' in PROFIT_PAGE
        assert 'applySort("days-table");' in PROFIT_PAGE
        assert 'applySort("unrealized-table");' in PROFIT_PAGE
        assert 'var sortStates = {};' in PROFIT_PAGE

    def test_profit_metrics_are_visible_and_sortable(self) -> None:
        for heading in (
            "ROI",
            "Median Hold",
            "Profit Share",
            "Projected ROI",
        ):
            assert f">{heading}</button>" in PROFIT_PAGE
        assert "item.median_hold_seconds" in PROFIT_PAGE
        assert "item.profit_share_percent" in PROFIT_PAGE
        assert "item.roi_percent" in PROFIT_PAGE

    def test_summary_contains_best_and_worst_highlights(self) -> None:
        assert '["Best item", highlight(bestItem, "name")' in PROFIT_PAGE
        assert '["Worst item", highlight(worstItem, "name")' in PROFIT_PAGE
        assert (
            '["Best trading day", highlight(bestDay, "date")'
            in PROFIT_PAGE
        )
        assert (
            '["Worst trading day", highlight(worstDay, "date")'
            in PROFIT_PAGE
        )
        assert '["Realized ROI", percent(summary.roi_percent)' in PROFIT_PAGE
        assert '["Unrealized ROI", percent(unrealized.roi_percent)' in (
            PROFIT_PAGE
        )

    def test_daily_trend_charts_include_zero_days_and_rolling_profit(
        self,
    ) -> None:
        assert "Daily Profit and Average" in PROFIT_PAGE
        assert "7-Day Rolling Average" in PROFIT_PAGE
        assert "Cumulative Profit" in PROFIT_PAGE
        assert 'id="daily-profit-chart"' in PROFIT_PAGE
        assert 'id="rolling-profit-chart"' in PROFIT_PAGE
        assert 'id="cumulative-profit-chart"' in PROFIT_PAGE
        assert 'profitByDate[date] : 0' in PROFIT_PAGE
        assert (
            "expectedStart.setUTCDate("
            "expectedStart.getUTCDate() - data.days + 1);"
        ) in PROFIT_PAGE
        assert (
            "for (var bucket = 0; bucket < data.days; bucket += 1)"
            in PROFIT_PAGE
        )
        assert "rollingTotal / 7" in PROFIT_PAGE
        assert "cumulative += point.profit;" in PROFIT_PAGE
        assert 'document.createElementNS(SVG_NS, name)' in PROFIT_PAGE

    def test_profit_dashboard_and_charts_fill_the_available_width(self) -> None:
        assert "main { width: 100%; margin: 0; padding: 1rem; }" in PROFIT_PAGE
        assert "width: min(100%, 88rem)" not in PROFIT_PAGE
        assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in (
            PROFIT_PAGE
        )

    def test_profit_chart_hover_shows_the_nearest_date_values(self) -> None:
        assert 'class="profit-chart"' in PROFIT_PAGE
        assert '"class": "chart-crosshair"' in PROFIT_PAGE
        assert '"class": "chart-hover-ring"' in PROFIT_PAGE
        assert 'tooltipNode("chart-tooltip")' in PROFIT_PAGE
        assert "function nearestColumn(vbX)" in PROFIT_PAGE
        assert 'overlay.addEventListener("pointermove"' in PROFIT_PAGE
        assert 'overlay.addEventListener("pointerleave"' in PROFIT_PAGE
        for label in ("Daily profit", "Daily average"):
            assert f'label: "{label}"' in PROFIT_PAGE
        assert "label: valueLabel" in PROFIT_PAGE
        assert '"7-day average", "#58a6ff"' in PROFIT_PAGE
        assert '"Cumulative profit", "#74dc9a"' in PROFIT_PAGE

    def test_profit_chart_taps_pin_values_and_can_be_dismissed(self) -> None:
        assert 'overlay.addEventListener("pointerdown"' in PROFIT_PAGE
        assert 'overlay.addEventListener("pointerup"' in PROFIT_PAGE
        assert 'overlay.addEventListener("pointercancel"' in PROFIT_PAGE
        assert 'event.pointerType === "mouse"' in PROFIT_PAGE
        assert "if (moved > tapSlop)" in PROFIT_PAGE
        assert "pinned = true;\n      showHover(column, point.y);" in PROFIT_PAGE
        assert 'dismissPinned("outside")' in PROFIT_PAGE
        assert 'dismissPinned("wheel")' in PROFIT_PAGE
        assert 'dismissPinned("scroll")' in PROFIT_PAGE
        assert 'dismissPinned("escape")' in PROFIT_PAGE
        assert 'dismissPinned("blur")' in PROFIT_PAGE
        assert 'dismissPinned("cancelled")' in PROFIT_PAGE

    def test_profit_chart_tap_listeners_are_released_on_redraw(self) -> None:
        assert "var chartHoverCleanups = [];" in PROFIT_PAGE
        assert "return function () {" in PROFIT_PAGE
        assert "chartHoverCleanups.push(attachChartHover" in PROFIT_PAGE
        assert "chartHoverCleanups.forEach(function (cleanup)" in PROFIT_PAGE

    def test_profit_chart_tap_diagnostics_do_not_log_values(self) -> None:
        assert '"profit chart selection:"' in PROFIT_PAGE
        assert "action, reason, columns.length" in PROFIT_PAGE

    def test_profit_chart_tooltips_stay_inside_their_panel(self) -> None:
        assert "width: max-content;" in PROFIT_PAGE
        assert "min-width: min(9rem, calc(100% - 1rem));" in PROFIT_PAGE
        assert (
            "var tooltipWidth = tooltip.getBoundingClientRect().width;"
            in PROFIT_PAGE
        )
        assert "var minimumLeft = edgePadding + tooltipWidth / 2;" in (
            PROFIT_PAGE
        )
        assert (
            "var maximumLeft = containerWidth - edgePadding "
            "- tooltipWidth / 2;"
        ) in PROFIT_PAGE
        assert 'tooltip.style.left = leftPixels + "px";' in PROFIT_PAGE
        assert "Math.max(\n        10, Math.min(90" not in PROFIT_PAGE

    def test_sign_in_target_is_escaped_before_becoming_markup(self) -> None:
        document = sign_in_page('"><script>target-secret</script>')

        assert "<script>target-secret</script>" not in document
        assert "&quot;&gt;&lt;script&gt;target-secret&lt;/script&gt;" in document

    def test_calendar_and_profit_pages_link_to_each_other(self) -> None:
        assert '<a href="/profit">Profit</a>' in CALENDAR_PAGE
        assert '<a href="/">Calendar</a>' in PROFIT_PAGE


class TestDashboardHeaders:
    def test_every_dashboard_uses_the_same_sign_out_control(self) -> None:
        for page in (
            CALENDAR_PAGE,
            PROFIT_PAGE,
            FOOD_PAGE,
            ROSTER_PAGE,
            GOLD_PAGE,
        ):
            assert page.count(
                '<button type="submit" class="signout" '
                'aria-label="Sign out">'
            ) == 1
            assert page.count(
                '<span class="signout-label">Sign out</span>'
            ) == 1
            assert page.count('class="signout-icon"') == 1
            assert "Log out" not in page

    def test_every_dashboard_uses_the_shared_header_dimensions(self) -> None:
        for page in (
            CALENDAR_PAGE,
            PROFIT_PAGE,
            FOOD_PAGE,
            ROSTER_PAGE,
            GOLD_PAGE,
        ):
            assert "padding: 0.6rem 1rem;" in page
            assert "padding: 0.35rem 0.7rem;" in page
            assert 'header form[action="/logout"] {' in page
            assert '<h1 id="brand">' in page


class TestCalendarTimeGrid:
    def test_day_and_week_render_an_hour_gutter(self) -> None:
        assert (
            'renderTimeGrid(range, state.view === "day" ? 1 : weekSpan())'
            in CALENDAR_PAGE
        )
        assert "function hourGutter()" in CALENDAR_PAGE
        assert "#grid.timegrid.day" in CALENDAR_PAGE
        assert "#grid.timegrid.week" in CALENDAR_PAGE

    def test_events_are_positioned_and_sized_from_their_own_times(
        self,
    ) -> None:
        assert 'chip.style.top = pixelsFor(item.startMin) + "px";' in (
            CALENDAR_PAGE
        )
        assert (
            "Math.min(item.layoutEnd, MINUTES_PER_DAY) - item.startMin"
            in CALENDAR_PAGE
        )

    def test_short_events_keep_the_time_and_title_on_one_line(self) -> None:
        assert (
            ".chip.tg-ev {\n"
            "  position: absolute;\n"
            "  /* A short event has room for only one line. Keep the title "
            in CALENDAR_PAGE
        )
        assert "  flex-direction: row;" in CALENDAR_PAGE
        assert ".chip.tg-ev .name { min-width: 0; max-width: 100%; }" in (
            CALENDAR_PAGE
        )

    def test_late_events_are_clipped_to_the_day_boundary(self) -> None:
        # The minimum-height floor in layoutEnd can exceed MINUTES_PER_DAY for
        # an event that starts in the last few minutes of the day. The rendered
        # height must clip there so the block never bleeds below the 24-hour
        # column into the content underneath the grid.
        assert (
            "chip.style.height = pixelsFor(\n"
            "      Math.min(item.layoutEnd, MINUTES_PER_DAY) - item.startMin)"
            in CALENDAR_PAGE
        )

    def test_overlapping_events_are_placed_side_by_side(self) -> None:
        assert "function assignLanes(cluster)" in CALENDAR_PAGE
        assert 'chip.style.width = "calc(" + width + "% - 4px)";' in (
            CALENDAR_PAGE
        )

    def test_short_events_reserve_their_clamped_height_when_packing(
        self,
    ) -> None:
        # A block is never drawn shorter than the pixel floor, so two
        # back-to-back short events overlap on screen. Clustering and
        # lane-packing must reserve that clamped span (layoutEnd), not the raw
        # end, or they would give both full width and draw one over the other.
        assert (
            "layoutEnd: Math.max(endMin, startMin + MIN_EVENT_MIN)"
            in CALENDAR_PAGE
        )
        assert "var MIN_EVENT_MIN = MIN_EVENT_PX * 60 / HOUR_PX;" in (
            CALENDAR_PAGE
        )
        # Both the lane occupancy and the cluster boundary read layoutEnd.
        assert "laneEnds[lane] = item.layoutEnd;" in CALENDAR_PAGE
        assert "clusterEnd = Math.max(clusterEnd, item.layoutEnd);" in (
            CALENDAR_PAGE
        )

    def test_hour_height_matches_the_stylesheet(self) -> None:
        # The script converts minutes to pixels against the hour rows the
        # stylesheet draws, so the two constants must not drift apart.
        css = re.search(r"--hour-h: (\d+)px;", CALENDAR_PAGE)
        script = re.search(r"var HOUR_PX = (\d+);", CALENDAR_PAGE)
        assert css is not None and script is not None
        assert css.group(1) == script.group(1)


class TestCalendarBrowserTime:
    def test_event_times_come_from_the_browser_clock(self) -> None:
        # start_epoch is an absolute instant; every rendered time is derived
        # from it through the browser's own clock and locale.
        assert "new Date(entry.start_epoch * 1000)" in CALENDAR_PAGE
        assert "date.getHours() * 60 + date.getMinutes()" in CALENDAR_PAGE
        assert 'toLocaleTimeString(\n      undefined, { hour: "numeric" })' in (
            CALENDAR_PAGE
        )

    def test_the_time_zone_banner_is_not_shown(self) -> None:
        # The "Times in ..." banner was removed from both the mobile and the
        # regular layout; times are still rendered in the browser's own zone.
        assert "Intl.DateTimeFormat().resolvedOptions().timeZone" not in (
            CALENDAR_PAGE
        )
        assert "Times in " not in CALENDAR_PAGE
        assert 'id="tz"' not in CALENDAR_PAGE


class TestCalendarInteraction:
    def test_month_overflow_becomes_a_count_without_a_scrollbar(self) -> None:
        assert ".cell-events { min-height: 0; overflow: hidden; }" in (
            CALENDAR_PAGE
        )
        # The event area has a content-independent grid height. Hiding one chip
        # must not shrink the area and make the loop hide every remaining chip.
        assert "grid-template-rows: auto minmax(0, 1fr);" in CALENDAR_PAGE
        assert "function collapseMonthCell(cell)" in CALENDAR_PAGE
        assert (
            "eventList.scrollHeight > eventList.clientHeight + 1"
            in CALENDAR_PAGE
        )
        assert 'more.textContent = "+" + hiddenCount;' in CALENDAR_PAGE
        assert "scheduleMonthCollapse();" in CALENDAR_PAGE
        # .chip's display:flex would otherwise keep hidden chips in layout,
        # making the measuring loop count every event rather than only those
        # that do not fit.
        assert ".chip[hidden] { display: none; }" in CALENDAR_PAGE

    def test_month_dates_and_week_headers_open_day_view(self) -> None:
        assert 'var cellHead = el("div", "cell-head");' in CALENDAR_PAGE
        assert 'var dayLink = el("button", "day-link");' in CALENDAR_PAGE
        assert 'var more = el("button", "more");' in CALENDAR_PAGE
        assert 'dayLink.addEventListener("click", function () {' in CALENDAR_PAGE
        assert 'more.addEventListener("click", function () {' in CALENDAR_PAGE
        assert 'cell.addEventListener("click"' not in CALENDAR_PAGE
        assert "dayHeader(date, days === 1, days > 1)" in CALENDAR_PAGE
        assert 'head.addEventListener("click", function () { openDay(date); });' in (
            CALENDAR_PAGE
        )

    def test_month_date_hover_highlights_only_the_number(self) -> None:
        assert ".day-link:hover, .more:hover { background: transparent; }" in (
            CALENDAR_PAGE
        )
        assert ".day-link:hover .daynum { color: var(--accent); }" in (
            CALENDAR_PAGE
        )

    def test_event_details_can_be_pinned_and_replaced(self) -> None:
        assert "var pinnedChip = null;" in CALENDAR_PAGE
        assert "function pinTooltip(chip)" in CALENDAR_PAGE
        assert "if (pinnedChip === chip)" in CALENDAR_PAGE
        assert "pinTooltip(chip);" in CALENDAR_PAGE
        assert 'document.addEventListener("click", function () {' in (
            CALENDAR_PAGE
        )
        assert "if (chip && !pinnedChip) { showTooltip(chip); }" in (
            CALENDAR_PAGE
        )


class TestCalendarMobile:
    def test_a_single_breakpoint_drives_mobile_behaviour(self) -> None:
        assert 'window.matchMedia("(max-width: 640px)")' in CALENDAR_PAGE
        assert "function isMobile()" in CALENDAR_PAGE
        assert "@media (max-width: 640px)" in CALENDAR_PAGE

    def test_week_collapses_to_three_days_on_mobile(self) -> None:
        # The week view spans three days on mobile so it never scrolls sideways,
        # and the button is relabelled to match.
        assert "function weekSpan() { return isMobile() ? 3 : 7; }" in (
            CALENDAR_PAGE
        )
        assert 'isMobile() ? "3 Day" : "Week"' in CALENDAR_PAGE
        assert (
            "#grid.timegrid.week {\n"
            "    grid-template-columns: var(--gutter) repeat(3, minmax(0, 1fr));"
            in CALENDAR_PAGE
        )

    def test_month_fits_a_single_page_with_single_letter_headings(
        self,
    ) -> None:
        assert 'var dayInitials = ["S", "M", "T", "W", "T", "F", "S"];' in (
            CALENDAR_PAGE
        )
        assert "mobile ? dayInitials[index] : name" in CALENDAR_PAGE
        # Six week rows share the height instead of forcing a scroll.
        assert (
            "grid-template-rows: auto repeat(6, minmax(0, 1fr));"
            in CALENDAR_PAGE
        )

    def test_month_hides_times_and_opens_the_day_on_tap(self) -> None:
        # chipFor drops the time span on mobile month cells, and the date button
        # opens that day.
        assert "chipFor(entry, index, mobile)" in CALENDAR_PAGE
        assert "if (!hideTime) {" in CALENDAR_PAGE
        assert "function openDay(date)" in CALENDAR_PAGE
        assert 'var dayLink = el("button", "day-link");' in CALENDAR_PAGE

    def test_horizontal_swipes_step_the_period(self) -> None:
        assert 'scroller.addEventListener("touchstart"' in CALENDAR_PAGE
        assert 'scroller.addEventListener("touchend"' in CALENDAR_PAGE
        assert "step(dx < 0 ? 1 : -1);" in CALENDAR_PAGE
        # A swipe is only claimed when it is clearly horizontal, so the day and
        # 3-day time grids keep scrolling vertically.
        assert "Math.abs(dx) < Math.abs(dy) * 1.5" in CALENDAR_PAGE

    def test_top_bar_uses_a_sign_out_icon_button(self) -> None:
        assert '<button type="submit" class="signout" aria-label="Sign out">' in (
            CALENDAR_PAGE
        )
        assert 'class="signout-icon"' in CALENDAR_PAGE
        # The stepper and username are hidden on mobile; the period label is
        # not, so the month stays on screen.
        assert ".controls { display: none; }" in CALENDAR_PAGE
        assert "#whoami { display: none; }" in CALENDAR_PAGE

    def test_period_label_keeps_the_top_left_corner_on_mobile(self) -> None:
        # Swiping changes the period and the month grid shows bare day numbers,
        # so the label is the only thing naming the month on a phone. It sits
        # in the first header column, opposite the sign-out button.
        assert (
            "#period {\n"
            "    grid-column: 1;\n"
            "    grid-row: 1;\n"
            "    justify-self: start;" in CALENDAR_PAGE
        )
        # It must not widen its column, or the centred title drifts off centre.
        assert "text-overflow: ellipsis;" in CALENDAR_PAGE

    def test_period_label_is_abbreviated_on_mobile(self) -> None:
        # The label shares a row with the title and sign-out button, so it is
        # rendered in a shorter form there while still naming the month.
        assert "function renderMobilePeriodLabel(range)" in CALENDAR_PAGE
        assert "renderMobilePeriodLabel(range);" in CALENDAR_PAGE
        assert 'undefined, { month: "short", year: "numeric" });' in (
            CALENDAR_PAGE
        )


class TestFoodPage:
    def test_offers_all_three_ranges(self) -> None:
        assert 'data-range="24h"' in FOOD_PAGE
        assert 'data-range="7d"' in FOOD_PAGE
        assert 'data-range="30d"' in FOOD_PAGE

    def test_chart_y_axis_grows_with_the_counts_it_draws(self) -> None:
        # The axis is no longer capped: it is computed from the samples that
        # are actually drawn, so a stock above fifty is not flattened against
        # a fixed ceiling.
        assert "var Y_MAX" not in FOOD_PAGE
        assert "function computeScale()" in FOOD_PAGE
        assert "if (point.count > high) { high = point.count; }" in FOOD_PAGE
        assert "state.scale = computeScale();" in FOOD_PAGE

    def test_chart_y_axis_only_measures_the_visible_feasts(self) -> None:
        # A feast switched off in the legend is left out of the drawing, so it
        # must not hold the axis up either.
        assert (
            "  function computeScale() {\n"
            "    var high = MIN_TOP;\n"
            "    visibleFeasts().forEach(function (feast) {" in FOOD_PAGE
        )

    def test_chart_y_axis_keeps_a_zero_baseline_and_a_floor(self) -> None:
        # Several feasts share the axis and running out is what the page is
        # read for, so zero stays on the floor of the chart, and a quiet window
        # still spans at least MIN_TOP rather than a count or two.
        assert "var MIN_TOP = 10;" in FOOD_PAGE
        assert "if (value < 0) { value = 0; }" in FOOD_PAGE
        assert "return M.top + (1 - value / high) * plotH();" in FOOD_PAGE

    def test_chart_y_axis_lands_on_round_counts(self) -> None:
        # Gridlines step through the computed scale on a 1, 2 or 5 times a
        # power of ten step, so the labels read as round counts.
        assert "function niceStep(span, target)" in FOOD_PAGE
        assert "var step = niceStep(span, 6);" in FOOD_PAGE
        assert (
            "var lines = Math.round(state.scale.high / state.scale.step);"
            in FOOD_PAGE
        )
        assert "for (var line = 0; line <= lines; line += 1)" in FOOD_PAGE

    def test_chart_axis_margin_fits_a_wider_count(self) -> None:
        # An uncapped axis can label counts of three or four digits, which do
        # not fit the margin a two-digit ceiling allowed.
        assert "left: 34" not in FOOD_PAGE
        assert "top: 16, right: 14, bottom: 36, left: 40, ticks: 4" in FOOD_PAGE
        assert "top: 16, right: 16, bottom: 32, left: 40, ticks: 6" in FOOD_PAGE

    def test_chart_times_come_from_the_browser_clock(self) -> None:
        # Every timestamp is an absolute instant rendered through the browser's
        # own clock and locale.
        assert "new Date(t * 1000)" in FOOD_PAGE
        assert "toLocaleString(" in FOOD_PAGE
        assert "toLocaleTimeString(" in FOOD_PAGE

    def test_the_time_zone_banner_is_not_shown(self) -> None:
        assert 'id="tz"' not in FOOD_PAGE
        assert "Times in " not in FOOD_PAGE

    def test_calendar_link_is_removed(self) -> None:
        # The cross-link back to the calendar is dropped from every layout.
        assert '<a href="/">Calendar</a>' not in FOOD_PAGE

    def test_graph_is_taller_on_mobile(self) -> None:
        # A taller viewBox on mobile makes the graph read large on a phone,
        # where the SVG scales to the narrow screen width.
        assert "function metrics()" in FOOD_PAGE
        assert "w: 480, h: 620" in FOOD_PAGE
        assert "w: 960, h: 380" in FOOD_PAGE

    def test_legend_sits_below_the_chart_as_tappable_swatches(self) -> None:
        # The legend follows the chart in the DOM and each entry is a button
        # that switches its feast off and back on.
        chart_index = FOOD_PAGE.index('<div id="chart">')
        legend_index = FOOD_PAGE.index('<div id="legend"')
        assert chart_index < legend_index
        assert 'var item = el("button", hidden ? "item off" : "item");' in (
            FOOD_PAGE
        )
        assert 'item.setAttribute("aria-pressed", hidden ? "false" : "true")' in (
            FOOD_PAGE
        )

    def test_the_legend_switches_a_feast_off_and_back_on(self) -> None:
        # Clicking an entry drops its feast out of the drawing entirely, so
        # the hover and the tooltip lose it too rather than leaving an
        # invisible line still selectable.
        assert "if (isHidden(feast)) { return; }" in FOOD_PAGE
        assert "state.hidden[feast.id] = true;" in FOOD_PAGE
        assert "delete state.hidden[feast.id];" in FOOD_PAGE
        assert "renderLegend();\n        renderChart();" in FOOD_PAGE

    def test_a_switched_off_feast_keeps_its_place_in_the_legend(self) -> None:
        # It is dimmed with its colour reduced to an outline, so what is
        # missing from the chart is still named and one click puts it back.
        assert ".legend .item.off { opacity: 0.55; }" in FOOD_PAGE
        assert 'swatch.style.boxShadow = "inset 0 0 0 2px " + color;' in (
            FOOD_PAGE
        )
        mobile = FOOD_PAGE[FOOD_PAGE.index("@media (max-width: 640px)"):]
        # On a phone the names stay hidden, except on a feast switched off:
        # a tap still answers "which one is this?", by taking the line away
        # and labelling what went.
        assert ".legend .legend-name { display: none; }" in mobile
        assert ".legend .item.off .legend-name { display: inline; }" in mobile
        assert "show-name" not in FOOD_PAGE

    def test_an_all_off_legend_says_so_instead_of_reading_as_no_data(
        self,
    ) -> None:
        # An empty window and a legend switched all the way off are different
        # states, and only one of them is worth waiting for more data over.
        assert "function chartStatusText(plottedCount) {" in FOOD_PAGE
        assert "if (feasts().length && !visibleFeasts().length) {" in FOOD_PAGE
        assert '"Every feast is switched off. Click one in the legend to draw "' in (
            FOOD_PAGE
        )

    def test_every_recorded_sample_is_plotted(self) -> None:
        # A point is drawn for every sample; the series is never downsampled,
        # so even the 30d window keeps all of its points.
        assert "points.forEach(function (point) {" in FOOD_PAGE
        assert '"class": "series-dot"' in FOOD_PAGE

    def test_hover_draws_a_crosshair_and_shows_point_values(self) -> None:
        # Hovering snaps a thin translucent vertical line to the nearest sample
        # and shows that column's values in an HTML tooltip.
        assert '"class": "crosshair"' in FOOD_PAGE
        assert "rgba(128, 128, 128, 0.45)" in FOOD_PAGE
        assert 'addEventListener("pointermove"' in FOOD_PAGE
        assert 'addEventListener("pointerleave"' in FOOD_PAGE
        assert '"chart-tooltip"' in FOOD_PAGE

    def test_a_tap_selects_a_point_instead_of_hovering_it(self) -> None:
        # A finger gets one pointermove at the tap point and then a
        # pointerleave as it lifts, which made the crosshair flash and vanish.
        # Touch and pen are routed to a pointerdown selection instead and are
        # filtered out of the move and leave handlers.
        assert "function isHoverPointer(event) {" in FOOD_PAGE
        assert (
            'return !event.pointerType || event.pointerType === "mouse";'
            in FOOD_PAGE
        )
        assert 'overlay.addEventListener("pointerdown", function (event) {' in (
            FOOD_PAGE
        )
        assert "if (isHoverPointer(event)) { return; }" in FOOD_PAGE
        # The leave that ends a mouse hover must not end a touch selection.
        assert 'if (isHoverPointer(event)) { release("pointer-leave"); }' in (
            FOOD_PAGE
        )

    def test_a_tapped_selection_stays_until_the_next_interaction(self) -> None:
        # While a selection is pinned the page is watched for anything else the
        # reader does, and each of those listeners is dropped again on release
        # so a hover from a mouse never leaves one behind.
        for event_name in ("pointerdown", "wheel", "keydown"):
            assert (
                'document.addEventListener("%s", dismiss, true);' % event_name
                in FOOD_PAGE
            )
            assert (
                'document.removeEventListener("%s", dismiss, true);'
                % event_name in FOOD_PAGE
            )
        assert 'window.addEventListener("blur", dismiss);' in FOOD_PAGE
        assert 'window.removeEventListener("blur", dismiss);' in FOOD_PAGE
        # A tap on another point reaches the overlay after the page-level
        # listener, so the overlay's own handler is left to move the selection
        # rather than the dismissal wiping it first.
        assert "if (isRetargetingTap(event)) {" in FOOD_PAGE

    def test_only_a_tap_is_exempt_from_dismissing_the_selection(self) -> None:
        # The exemption is what keeps a second tap from clearing the selection
        # it is meant to move. It must cover nothing else: a wheel over the
        # plot and a mouse press on it are aimed at the overlay too, but the
        # overlay's pointerdown handler ignores both, so exempting them would
        # strand the selection on screen with nothing left to clear it.
        assert "function isRetargetingTap(event) {" in FOOD_PAGE
        body = FOOD_PAGE.split("function isRetargetingTap(event) {", 1)[1]
        body = body.split("\n    }", 1)[0]
        # A wheel carries no pointerType, so the hover test alone rejects it;
        # the type test is what rejects a press from a mouse.
        assert 'event.type === "pointerdown"' in body
        assert "event.target === overlay" in body
        assert "!isHoverPointer(event)" in body

    def test_a_scrolling_finger_drops_the_selection(self) -> None:
        # A touch that travels past the tap slop, or that the browser claims
        # for a scroll outright, is not a point selection.
        assert "var TAP_SLOP = 12;" in FOOD_PAGE
        assert (
            'if (Math.sqrt(dx * dx + dy * dy) > TAP_SLOP) { release("drag"); }'
            in FOOD_PAGE
        )
        assert 'overlay.addEventListener("pointercancel"' in FOOD_PAGE
        assert 'if (!isHoverPointer(event)) { release("pointer-cancel"); }' in (
            FOOD_PAGE
        )

    def test_a_redraw_releases_the_previous_charts_listeners(self) -> None:
        # attachHover hands back its teardown so the page-level listeners of a
        # pinned selection cannot outlive the canvas that opened them.
        assert "detachHover = attachHover(canvas, plotted);" in FOOD_PAGE
        assert "if (detachHover) { detachHover(); detachHover = null; }" in (
            FOOD_PAGE
        )
        assert 'return function () { release("redraw"); };' in FOOD_PAGE

    def test_every_touch_selection_outcome_is_traced(self) -> None:
        # A tap can pin, open, move, skip or release a selection, and each
        # outcome names itself in the console so a trace explains why a chart
        # selection appeared or went away.
        assert "function traceSelection(action, reason, count) {" in FOOD_PAGE
        traced = _call_arguments(FOOD_PAGE, "traceSelection")
        actions = {call[0] for call in traced}
        assert actions == {
            '"release"',
            '"keep"',
            '"pin"',
            '"skip"',
            'moved ? "move" : "open"',
        }
        # Every way a pinned selection can end reaches release() with a fixed
        # reason name, so none of them can clear the chart untraced.
        released = {call[0] for call in _call_arguments(FOOD_PAGE, "release")}
        assert released == {
            '"drag"',
            '"pointer-leave"',
            '"pointer-cancel"',
            '"skipped-tap"',
            '"redraw"',
            '"page-" + eventKind(event)',
        }
        # Each skip a tap can take has its own reason rather than a shared
        # "nothing happened".
        assert '{ column: null, reason: "no-samples" }' in FOOD_PAGE
        assert '{ column: null, reason: "unsized-canvas" }' in FOOD_PAGE
        assert '{ column: null, reason: "no-nearest" }' in FOOD_PAGE

    def test_selection_traces_carry_no_payload_or_coordinates(self) -> None:
        # The trace must stay sanitized: fixed action and reason names plus a
        # count of drawn elements. A pointer coordinate, a sample timestamp, a
        # stock value or a feast name must never be passed to it.
        allowed = re.compile(
            r"""^(
                "[a-z-]+"                        # fixed action or reason name
                | moved\ \?\ "move"\ :\ "open"   # which of the two a tap was
                | kind | reason | action | count # narrowed or forwarded names
                | tapped\.reason                 # fixed name from resolveColumn
                | columns\.length                # count of drawn columns
                | tapped\.column\.points\.length # count of dots in the column
            )$""",
            re.X,
        )
        for call in _call_arguments(FOOD_PAGE, "traceSelection"):
            assert len(call) == 3, call
            for arg in call:
                assert allowed.match(arg), arg
        for call in _call_arguments(FOOD_PAGE, "release"):
            assert len(call) == 1, call
            assert allowed.match(call[0]) or call[0] == (
                '"page-" + eventKind(event)'
            ), call

    def test_legend_traces_carry_no_feast_name_or_count(self) -> None:
        # The legend trace is held to the same rule: a fixed action name and a
        # count of the feasts left on, never a feast name or a stock value.
        assert "function traceLegend(action, count) {" in FOOD_PAGE
        for call in _call_arguments(FOOD_PAGE, "traceLegend"):
            assert call == [
                'hidden ? "show" : "hide"',
                "visibleFeasts().length",
            ], call

    def test_traced_pointer_and_event_names_are_narrowed(self) -> None:
        # pointerType and type are reflected into the console, so both are
        # mapped onto a closed set of spec names first.
        assert "function pointerKind(event) {" in FOOD_PAGE
        assert "function eventKind(event) {" in FOOD_PAGE
        for helper in ("pointerKind", "eventKind"):
            body = FOOD_PAGE.split("function %s(event) {" % helper, 1)[1]
            body = body.split("\n  }", 1)[0]
            assert 'return "other";' in body
        assert (
            'if (kind === "mouse" || kind === "pen" || kind === "touch") {'
            in FOOD_PAGE
        )
        assert 'if (name === "pointerdown" || name === "wheel" ||' in FOOD_PAGE
        assert 'name === "keydown" || name === "blur") {' in FOOD_PAGE

    def test_the_page_only_logs_through_its_sanitized_call_sites(self) -> None:
        # Every console call is a sanitized workflow trace or the load failure
        # that logs an error's type and message.
        assert re.findall(r"console\.\w+", FOOD_PAGE) == [
            "console.debug",
            "console.debug",
            "console.debug",
            "console.debug",
            "console.error",
        ]
        assert _call_arguments(FOOD_PAGE, "console.debug") == [
            ['"feast chart mode:"', "mode", "count"],
            ['"feast chart selection:"', "action", "reason", "count"],
            ['"feast chart legend:"', "action", "count"],
            ['"feast chart range:"', "action", "reason", "days"],
        ]

    def test_hover_geometry_uses_the_active_layout_metrics(self) -> None:
        # The hover was written against fixed chart constants that the mobile
        # layout replaced with metrics(). Any survivor is an undefined global
        # that throws inside renderChart and blanks the page, so none may
        # remain.
        for dead in (
            "VB_W",
            "VB_H",
            "PAD_TOP",
            "PAD_LEFT",
            "PAD_RIGHT",
            "PAD_BOTTOM",
            "PLOT_W",
            "PLOT_H",
        ):
            assert dead not in FOOD_PAGE
        # The hover reads the metrics its canvas was drawn with, so a
        # breakpoint flip cannot leave it measuring against the other layout.
        assert "var m = M;" in FOOD_PAGE

    def test_a_render_fault_is_reported_to_the_console(self) -> None:
        # render() runs inside the fetch chain, so a drawing fault surfaces as
        # the same generic message as a failed request. Logging the error type
        # keeps it traceable; no request, response or payload is logged.
        assert 'console.error(\n          "feast usage load failed:",' in (
            FOOD_PAGE
        )
        assert "error && error.name, error && error.message);" in FOOD_PAGE

    def test_table_pages_five_removals_at_a_time(self) -> None:
        assert "var TABLE_PAGE_SIZE = 5;" in FOOD_PAGE

    def test_dynamic_values_never_become_markup(self) -> None:
        # Like the calendar, feast names and rows are only ever set through
        # textContent or attributes, never innerHTML.
        assert "innerHTML" not in FOOD_PAGE


class TestCustomRangePicker:
    """All three dashboards offer the same picker, so all three are
    checked together."""

    def test_both_dashboards_offer_a_custom_range(self) -> None:
        for page in (FOOD_PAGE, ROSTER_PAGE, GOLD_PAGE):
            assert 'data-range="custom"' in page
            assert 'id="custom-start"' in page
            assert 'id="custom-end"' in page
            assert 'id="custom-apply"' in page

    def test_the_picker_stays_hidden_until_the_button_reveals_it(self) -> None:
        for page in (FOOD_PAGE, ROSTER_PAGE, GOLD_PAGE):
            assert ".custom {\n  display: none;" in page
            assert ".custom.open { display: flex; }" in page
            assert 'if (picked === "custom") {' in page
            assert (
                "toggleCustomPanel(!customPanel.classList.contains(\"open\"));"
            ) in page

    def test_a_picked_pair_covers_whole_local_days(self) -> None:
        # The window opens at midnight on the first day and closes on the last
        # second of the second, so one day picked twice is that whole day.
        for page in (FOOD_PAGE, ROSTER_PAGE, GOLD_PAGE):
            assert "var since = Math.floor(from.getTime() / 1000);" in page
            assert (
                "to.getFullYear(), to.getMonth(), to.getDate() + 1"
            ) in page

    def test_a_range_that_cannot_be_drawn_is_named_not_fetched(self) -> None:
        for page in (FOOD_PAGE, ROSTER_PAGE, GOLD_PAGE):
            assert 'error: "Pick a start and an end date."' in page
            assert 'error: "The end date is before the start date."' in page
            assert 'error: "The start date is in the future."' in page
            # The refusal is traced, reaches the reader, and returns before
            # any request is made.
            refusal = page.split("if (picked.error) {", 1)[1]
            refusal = refusal.split("    }", 1)[0]
            assert 'traceRange("refuse", picked.reason, 0);' in refusal
            assert "customError.textContent = picked.error;" in refusal
            assert "return;" in refusal
            assert "fetch(" not in refusal

    def test_untouched_defaults_cover_the_window_in_whole_days(self) -> None:
        # The fields hold days and nothing finer, so the defaults are the
        # whole local days the drawn window falls inside rather than a copy of
        # it. Applying an untouched 24h default reads a few hours wider than
        # the button it came from, which is the right way to miss: the
        # narrower pair would drop hours the reader can already see.
        for page in (FOOD_PAGE, ROSTER_PAGE, GOLD_PAGE):
            assert "var span = windowSpan() || 24 * 60 * 60;" in page
            assert (
                "customStart.value = "
                "dayValue(new Date(today.getTime() - span * 1000));"
            ) in page
            assert "customEnd.value = dayValue(today);" in page

    def test_a_refused_range_is_traced_with_a_fixed_reason(self) -> None:
        # A refusal ends the workflow in the browser, without a request, so
        # this trace is the only place a console can say the reader asked for
        # a window and did not get one.
        for page in (FOOD_PAGE, ROSTER_PAGE, GOLD_PAGE):
            assert "function traceRange(action, reason, days) {" in page
            traced = _call_arguments(page, "traceRange")
            assert traced == [
                ['"refuse"', "picked.reason", "0"],
                [
                    '"apply"',
                    '"ok"',
                    "Math.round((picked.until - picked.since) / 86400)",
                ],
            ], traced
            # Every way pickedWindow can refuse names itself, so none of them
            # reaches the trace as an undefined reason.
            for reason in (
                '"no-dates"',
                '"backwards"',
                '"too-wide"',
                '"future-start"',
            ):
                assert "reason: %s" % reason in page

    def test_range_traces_carry_no_dates_the_reader_entered(self) -> None:
        # The trace is held to the same rule as the selection and legend ones:
        # a fixed action name, a fixed reason name and a count of days. The
        # picked dates and the sentence shown to the reader stay out of it.
        allowed = re.compile(
            r"""^(
                "(refuse|apply|ok)"
                | picked\.reason
                | 0
                | Math\.round\(\(picked\.until\ -\ picked\.since\)\ /\ 86400\)
            )$""",
            re.X,
        )
        for page in (FOOD_PAGE, ROSTER_PAGE, GOLD_PAGE):
            for call in _call_arguments(page, "traceRange"):
                assert len(call) == 3, call
                for arg in call:
                    assert allowed.match(arg), arg

    def test_the_picker_mirrors_the_servers_own_ceiling(self) -> None:
        for page in (FOOD_PAGE, ROSTER_PAGE, GOLD_PAGE):
            assert "var MAX_CUSTOM_DAYS = 366;" in page
        assert MAX_CUSTOM_WINDOW_SECONDS == 366 * 24 * 60 * 60

    def test_an_applied_pair_is_sent_as_epoch_seconds(self) -> None:
        for page in (FOOD_PAGE, ROSTER_PAGE, GOLD_PAGE):
            assert '"?range=custom&start=" +' in page
            assert (
                'encodeURIComponent(String(customWindow.since))'
            ) in page
        assert 'fetch("/api/food" + rangeQuery())' in FOOD_PAGE
        assert 'fetch("/api/roster" + rangeQuery())' in ROSTER_PAGE
        assert 'fetch("/api/gold" + rangeQuery())' in GOLD_PAGE

    def test_axis_labels_follow_the_windows_width(self) -> None:
        # A custom window has no preset name to key the label format off, so
        # the span decides: about a day or less reads off the clock.
        for page in (FOOD_PAGE, ROSTER_PAGE, GOLD_PAGE):
            assert "if (windowSpan() <= 48 * 60 * 60) {" in page
            assert 'if (state.range === "24h") {' not in page


class TestDashboardChartModes:
    def test_regular_lines_are_the_default_and_the_header_toggle_is_shared(
        self,
    ) -> None:
        for page in (FOOD_PAGE, ROSTER_PAGE, GOLD_PAGE):
            assert 'id="chart-mode" class="chart-mode"' in page
            assert 'title="Regular line graph">╱</button>' in page
            assert "staircase: false" in page
            assert "state.staircase = !state.staircase;" in page
            assert 'state.staircase ? "⎿" : "╱"' in page
            assert "justify-content: space-between;" in page

    def test_staircase_vertices_are_only_added_in_staircase_mode(self) -> None:
        for page in (FOOD_PAGE, ROSTER_PAGE):
            assert "state.staircase &&" in page
        assert "if (state.staircase) {" in GOLD_PAGE

    def test_mode_changes_are_traced_without_chart_values(self) -> None:
        expectations = (
            (FOOD_PAGE, '"feast chart mode:"', "feasts().length"),
            (ROSTER_PAGE, '"roster chart mode:"', "points().length"),
            (GOLD_PAGE, '"gold chart mode:"', "points().length"),
        )
        for page, message, count in expectations:
            assert "function traceMode(mode, count) {" in page
            assert f"console.debug({message}, mode, count);" in page
            assert (
                'traceMode(state.staircase ? "staircase" : "regular", '
                + count + ");"
            ) in page

    def test_feast_mode_can_change_before_data_loads(self) -> None:
        handler = FOOD_PAGE.split(
            'chartMode.addEventListener("click", function () {', 1
        )[1].split("\n  });", 1)[0]
        assert "if (state.data) { renderChart(); }" in handler
        assert "renderChart();\n" not in handler.replace(
            "if (state.data) { renderChart(); }", ""
        )


class TestRosterChart:
    def test_hover_uses_both_pointer_coordinates(self) -> None:
        # Joins and leaves recorded moments apart sit almost on top of each
        # other horizontally, so picking by x alone made the dot a finger
        # actually landed on unreachable.
        assert "function nearestColumn(vbX, vbY)" in ROSTER_PAGE
        assert "var dx = point.x - vbX;" in ROSTER_PAGE
        assert "var dy = point.y - vbY;" in ROSTER_PAGE
        assert "nearestColumn(at.x, at.y)" in ROSTER_PAGE
        assert "nearestColumn(at.x)" not in ROSTER_PAGE


class TestRosterTable:
    def test_a_long_account_name_wraps_instead_of_widening_the_table(
        self,
    ) -> None:
        # A guild account name has no spaces to break at, so without this the
        # table grows past its card and carries the rest of the row off the
        # page. `anywhere` is what also shrinks the column's minimum width,
        # which is the width the table lays itself out from.
        assert (
            "table.changes td.name, table.changes td.by "
            "{ overflow-wrap: anywhere; }"
        ) in ROSTER_PAGE
        assert 'el("td", "name", change.name)' in ROSTER_PAGE
        assert ".chart-tooltip .tip-row .name { overflow-wrap: anywhere; }" in (
            ROSTER_PAGE
        )

    def test_a_phone_shows_the_change_as_its_dot_alone(self) -> None:
        # The word beside the dot says nothing the colour does not, and it
        # costs the account column width a phone has none of to spare.
        assert 'el("span", "change-label", kindOf(change.kind).label)' in (
            ROSTER_PAGE
        )
        mobile = ROSTER_PAGE[ROSTER_PAGE.index("@media (max-width: 640px)"):]
        assert "table.changes .change-label {" in mobile
        # Out of sight rather than out of the document, so a screen reader
        # still reads each row's change out.
        assert "clip-path: inset(50%);" in mobile
        assert "display: none" not in mobile.split(
            "table.changes .change-label {"
        )[1].split("}")[0]

    def test_the_lone_dot_sits_under_its_heading(self) -> None:
        # With only the dot left in the cell, the column is centred so it
        # reads as a column of dots; the heading carries the same class so
        # the two stay over each other.
        assert 'el("th", "change", "Change")' in ROSTER_PAGE
        mobile = ROSTER_PAGE[ROSTER_PAGE.index("@media (max-width: 640px)"):]
        assert "table.changes .change { text-align: center; }" in mobile
        assert "table.changes .change .dot { margin-right: 0; }" in mobile
        desktop = ROSTER_PAGE[: ROSTER_PAGE.index("@media (max-width: 640px)")]
        assert "text-align: center" not in desktop

    def test_the_desktop_layout_keeps_the_word(self) -> None:
        desktop = ROSTER_PAGE[: ROSTER_PAGE.index("@media (max-width: 640px)")]
        assert "change-label" not in desktop


class TestRosterPendingInvites:
    def test_the_section_is_on_the_page_with_its_own_heading(self) -> None:
        assert "<h2>Pending invites" in ROSTER_PAGE
        assert (
            "These accounts have been invited in-game but have not" in
            ROSTER_PAGE
        )
        assert 'id="pending"' in ROSTER_PAGE
        assert 'id="pending-status"' in ROSTER_PAGE

    def test_the_list_is_loaded_once_rather_than_per_range(self) -> None:
        # The invites are the guild's state now, not a window of history, so
        # the range buttons do not reload them.
        assert 'fetch("/api/pending")' in ROSTER_PAGE
        assert "function loadPending()" in ROSTER_PAGE
        assert "  loadPending();\n})();" in ROSTER_PAGE
        assert "loadPending" not in ROSTER_PAGE[
            ROSTER_PAGE.index("function refresh()"):
            ROSTER_PAGE.index("document.querySelectorAll(\"[data-range]\")")
        ]

    def test_an_unmatched_invite_says_so_instead_of_showing_a_blank(
        self,
    ) -> None:
        assert '"No application matched"' in ROSTER_PAGE
        assert 'el("td", "discord unmatched", matched' in ROSTER_PAGE
        assert "table.changes td.unmatched { color: var(--muted); }" in (
            ROSTER_PAGE
        )

    def test_an_empty_list_and_a_disabled_section_read_differently(
        self,
    ) -> None:
        # Nobody waiting is a fact about the guild; an unconfigured GW2 API is
        # a fact about the bot, and the reader is told which they are seeing.
        assert "No invites are waiting to be accepted." in ROSTER_PAGE
        assert "The pending invites are off until " in ROSTER_PAGE
        assert "Could not load the pending invites." in ROSTER_PAGE

    def test_the_disabled_section_names_the_settings_it_needs(self) -> None:
        # A feature that needs a setting says which /settings subcommand turns
        # it on, rather than sending the reader to the README for it.
        assert 'var missing = payload.missing || [];' in ROSTER_PAGE
        assert 'return "/settings " + name;' in ROSTER_PAGE

    def test_an_unread_forum_is_not_called_a_non_match(self) -> None:
        # Every row comes back unmatched when the application forum could not
        # be read, and saying "no application matched" there would assert
        # something the server never established.
        assert 'var matched = payload.matched !== false;' in ROSTER_PAGE
        assert '"Could not be checked"' in ROSTER_PAGE
        assert (
            "The Trial application forum could not be read in full, so an "
            in ROSTER_PAGE
        )

    def test_the_discord_column_survives_the_phone_layout(self) -> None:
        # The membership table hides its "By" column on a phone, and this
        # table's second column is the section's whole point, so it is not
        # that column.
        assert 'el("table", "changes pending")' in ROSTER_PAGE
        assert 'el("td", "discord", invite.discord_name)' in ROSTER_PAGE
        # The table rules live in the last of the page's mobile blocks.
        mobile = ROSTER_PAGE[
            ROSTER_PAGE.rindex("@media (max-width: 640px)"):
            ROSTER_PAGE.index("</style>")
        ]
        assert "table.changes .by { display: none; }" in mobile
        assert "discord" not in mobile
        # A Discord display name is bounded like an account name, so a long
        # one wraps instead of widening the table.
        assert "table.changes td.discord { overflow-wrap: anywhere; }" in (
            ROSTER_PAGE
        )

    def test_tracing_carries_no_account_or_discord_name(self) -> None:
        # Only a fixed action name and a row count reach the console.
        assert 'console.debug("roster pending invites:", action, count)' in (
            ROSTER_PAGE
        )
        assert (
            'tracePending(matched ? "render" : "unmatched", invites.length)'
            in ROSTER_PAGE
        )
        assert 'tracePending("unavailable", missing.length)' in ROSTER_PAGE


class TestGoldPage:
    def test_coin_values_match_the_profit_pages_spaced_denominations(self) -> None:
        assert "function formatCoins(copper)" in GOLD_PAGE
        assert 'parts.push(goldCoins.toLocaleString() + "g")' in GOLD_PAGE
        assert 'parts.push(silverCoins + "s")' in GOLD_PAGE
        assert 'parts.push(copperCoins + "c")' in GOLD_PAGE
        assert 'return parts.join(" ");' in GOLD_PAGE
        assert "formatGold" not in GOLD_PAGE

    def test_axis_keeps_compact_coin_labels_inside_its_margin(self) -> None:
        assert "function formatAxisCoins(copper)" in GOLD_PAGE
        assert "yLabel.textContent = formatAxisCoins(" in GOLD_PAGE
        assert "yLabel.textContent = formatCoins(" not in GOLD_PAGE

    def test_table_amount_is_only_an_ascii_sign_and_value(self) -> None:
        assert 'operation === "withdraw" ? "-" : "+"' in GOLD_PAGE
        assert 'el("span", "amount-label"' not in GOLD_PAGE
        assert 'el("span", "dot")' not in GOLD_PAGE

    def test_hover_uses_both_pointer_coordinates(self) -> None:
        assert "function nearestColumn(vbX, vbY)" in GOLD_PAGE
        assert "var dx = point.x - vbX;" in GOLD_PAGE
        assert "var dy = point.y - vbY;" in GOLD_PAGE
        assert "nearestColumn(at.x, at.y)" in GOLD_PAGE

    def test_movements_in_a_minute_share_one_cumulative_dot_and_tooltip(
        self,
    ) -> None:
        assert "function movementMinutes()" in GOLD_PAGE
        assert "Math.floor(movement.t / 60) * 60" in GOLD_PAGE
        assert "minute.after = movement.after;" in GOLD_PAGE
        assert "minute.movements.push(movement);" in GOLD_PAGE
        assert "movementMinutes().forEach(function (minute)" in GOLD_PAGE
        assert "emphasized.movements.forEach(function (movement)" in GOLD_PAGE

    def test_grouped_dot_and_hover_use_the_combined_balance_change(self) -> None:
        assert "netChangeColor(previousCoins, minute.after)" in GOLD_PAGE
        assert "fill: point.color" in GOLD_PAGE
        assert "stroke: point.color" in GOLD_PAGE
        assert "finalMovement" not in GOLD_PAGE

    def test_balance_line_uses_only_combined_minute_points(self) -> None:
        assert "var linePoints = [{ t: points()[0].t" in GOLD_PAGE
        assert "plotted.forEach(function (point)" in GOLD_PAGE
        assert "linePoints.slice(1).forEach(function (point, index)" in GOLD_PAGE
        assert "netChangeColor(previous.coins, point.coins)" in GOLD_PAGE

    def test_balance_segments_are_painted_before_combined_dots(self) -> None:
        segment_loop = GOLD_PAGE.index(
            "linePoints.slice(1).forEach(function (point, index)"
        )
        dot_loop = GOLD_PAGE.index('plotted.forEach(function (point) {', segment_loop)
        assert segment_loop < dot_loop
        assert '"class": "balance-line"' in GOLD_PAGE[segment_loop:dot_loop]
        assert '"class": "event-dot"' in GOLD_PAGE[dot_loop:]

    def test_legend_describes_net_balance_changes(self) -> None:
        assert 'label: "Increase"' in GOLD_PAGE
        assert 'label: "Decrease"' in GOLD_PAGE
        assert '{ color: "#D9D9D9", label: "Net Zero" }' in GOLD_PAGE
        legend = GOLD_PAGE.split("function renderLegend()", 1)[1].split(
            "function renderTotals()", 1
        )[0]
        assert "NET_CHANGES.forEach" in legend
        assert "OPERATION_ORDER.forEach" not in legend

    def test_net_zero_lines_and_dots_use_the_neutral_legend_colour(self) -> None:
        colour_picker = GOLD_PAGE.split(
            "function netChangeColor(before, after)", 1
        )[1].split("function movements()", 1)[0]
        assert "if (after === before) { return NET_CHANGES[2].color; }" in (
            colour_picker
        )

    def test_minute_grouping_restores_tied_api_order(self) -> None:
        assert "movements().slice().reverse().forEach(function (movement)" in (
            GOLD_PAGE
        )
        assert "left.t - right.t" not in GOLD_PAGE
