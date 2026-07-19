import re

from gw2bot.web.page import CALENDAR_PAGE, FOOD_PAGE


class TestCalendarMarkdown:
    def test_renders_discord_subtext_lines(self) -> None:
        assert 'var subtext = /^-#\\s+(.*)$/.exec(line);' in CALENDAR_PAGE
        assert 'el("div", "md-subtext")' in CALENDAR_PAGE
        assert "#tooltip .desc .md-subtext" in CALENDAR_PAGE


class TestCalendarTimeGrid:
    def test_day_and_week_render_an_hour_gutter(self) -> None:
        assert 'renderTimeGrid(range, state.view === "day" ? 1 : 7)' in (
            CALENDAR_PAGE
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

    def test_the_viewer_is_told_which_time_zone_they_are_seeing(self) -> None:
        assert "Intl.DateTimeFormat().resolvedOptions().timeZone" in (
            CALENDAR_PAGE
        )
        assert '"Times in " + zone' in CALENDAR_PAGE
        assert '<span id="tz"></span>' in CALENDAR_PAGE


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
        assert '<span id="tz"></span>' in FOOD_PAGE

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
