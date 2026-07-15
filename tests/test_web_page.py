import re

from gw2bot.web.page import CALENDAR_PAGE


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
        assert "pixelsFor(item.layoutEnd - item.startMin)" in CALENDAR_PAGE

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
