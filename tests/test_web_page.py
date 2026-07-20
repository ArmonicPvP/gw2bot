import re

from gw2bot.web.page import CALENDAR_PAGE, FOOD_PAGE


class TestCalendarMarkdown:
    def test_renders_discord_subtext_lines(self) -> None:
        assert 'var subtext = /^-#\\s+(.*)$/.exec(line);' in CALENDAR_PAGE
        assert 'el("div", "md-subtext")' in CALENDAR_PAGE
        assert "#tooltip .desc .md-subtext" in CALENDAR_PAGE


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
        # chipFor drops the time span on mobile month cells, and tapping a cell
        # opens that day.
        assert "chipFor(entry, index, mobile)" in CALENDAR_PAGE
        assert "if (!hideTime) {" in CALENDAR_PAGE
        assert "function openDay(date)" in CALENDAR_PAGE
        assert 'cell.setAttribute("role", "button");' in CALENDAR_PAGE

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
        # The stepper, period label and username are hidden on mobile.
        assert ".controls, #period, #whoami { display: none; }" in (
            CALENDAR_PAGE
        )


class TestFoodPage:
    def test_offers_all_three_ranges(self) -> None:
        assert 'data-range="24h"' in FOOD_PAGE
        assert 'data-range="7d"' in FOOD_PAGE
        assert 'data-range="30d"' in FOOD_PAGE

    def test_chart_y_axis_is_fixed_zero_to_fifty(self) -> None:
        assert "var Y_MAX = 50;" in FOOD_PAGE
        # Gridlines and labels step through the whole 0..Y_MAX axis.
        assert "for (var value = 0; value <= Y_MAX; value += 10)" in FOOD_PAGE

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
        # that reveals its feast name when tapped.
        chart_index = FOOD_PAGE.index('<div id="chart">')
        legend_index = FOOD_PAGE.index('<div id="legend"')
        assert chart_index < legend_index
        assert 'var item = el("button", "item");' in FOOD_PAGE
        assert 'item.classList.toggle("show-name");' in FOOD_PAGE
        assert ".legend .item.show-name .legend-name { display: inline; }" in (
            FOOD_PAGE
        )

    def test_every_recorded_sample_is_plotted(self) -> None:
        # A point is drawn for every sample; the series is never downsampled,
        # so even the 30d window keeps all of its points.
        assert "points.forEach(function (point) {" in FOOD_PAGE
        assert '"class": "series-dot"' in FOOD_PAGE

    def test_table_pages_five_removals_at_a_time(self) -> None:
        assert "var TABLE_PAGE_SIZE = 5;" in FOOD_PAGE

    def test_dynamic_values_never_become_markup(self) -> None:
        # Like the calendar, feast names and rows are only ever set through
        # textContent or attributes, never innerHTML.
        assert "innerHTML" not in FOOD_PAGE
