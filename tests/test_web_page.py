import re

from gw2bot.web.page import CALENDAR_PAGE, FOOD_PAGE, ROSTER_PAGE
from gw2bot.web.server import MAX_CUSTOM_WINDOW_SECONDS


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
        # The stepper and username are hidden on mobile; the period label is
        # not, so the month stays on screen.
        assert ".controls, #whoami { display: none; }" in CALENDAR_PAGE

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
        # Four console calls exist on this page and no others: the sanitized
        # selection, legend and range traces, and the load failure that logs
        # an error's type and message. Anything else would be an unreviewed
        # path to the console.
        assert re.findall(r"console\.\w+", FOOD_PAGE) == [
            "console.debug",
            "console.debug",
            "console.debug",
            "console.error",
        ]
        assert _call_arguments(FOOD_PAGE, "console.debug") == [
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
    """Both dashboards offer the same picker, so both are checked together."""

    def test_both_dashboards_offer_a_custom_range(self) -> None:
        for page in (FOOD_PAGE, ROSTER_PAGE):
            assert 'data-range="custom"' in page
            assert 'id="custom-start"' in page
            assert 'id="custom-end"' in page
            assert 'id="custom-apply"' in page

    def test_the_picker_stays_hidden_until_the_button_reveals_it(self) -> None:
        for page in (FOOD_PAGE, ROSTER_PAGE):
            assert ".custom {\n  display: none;" in page
            assert ".custom.open { display: flex; }" in page
            assert 'if (picked === "custom") {' in page
            assert (
                "toggleCustomPanel(!customPanel.classList.contains(\"open\"));"
            ) in page

    def test_a_picked_pair_covers_whole_local_days(self) -> None:
        # The window opens at midnight on the first day and closes on the last
        # second of the second, so one day picked twice is that whole day.
        for page in (FOOD_PAGE, ROSTER_PAGE):
            assert "var since = Math.floor(from.getTime() / 1000);" in page
            assert (
                "to.getFullYear(), to.getMonth(), to.getDate() + 1"
            ) in page

    def test_a_range_that_cannot_be_drawn_is_named_not_fetched(self) -> None:
        for page in (FOOD_PAGE, ROSTER_PAGE):
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

    def test_a_refused_range_is_traced_with_a_fixed_reason(self) -> None:
        # A refusal ends the workflow in the browser, without a request, so
        # this trace is the only place a console can say the reader asked for
        # a window and did not get one.
        for page in (FOOD_PAGE, ROSTER_PAGE):
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
        for page in (FOOD_PAGE, ROSTER_PAGE):
            for call in _call_arguments(page, "traceRange"):
                assert len(call) == 3, call
                for arg in call:
                    assert allowed.match(arg), arg

    def test_the_picker_mirrors_the_servers_own_ceiling(self) -> None:
        for page in (FOOD_PAGE, ROSTER_PAGE):
            assert "var MAX_CUSTOM_DAYS = 366;" in page
        assert MAX_CUSTOM_WINDOW_SECONDS == 366 * 24 * 60 * 60

    def test_an_applied_pair_is_sent_as_epoch_seconds(self) -> None:
        for page in (FOOD_PAGE, ROSTER_PAGE):
            assert '"?range=custom&start=" +' in page
            assert (
                'encodeURIComponent(String(customWindow.since))'
            ) in page
        assert 'fetch("/api/food" + rangeQuery())' in FOOD_PAGE
        assert 'fetch("/api/roster" + rangeQuery())' in ROSTER_PAGE

    def test_axis_labels_follow_the_windows_width(self) -> None:
        # A custom window has no preset name to key the label format off, so
        # the span decides: about a day or less reads off the clock.
        for page in (FOOD_PAGE, ROSTER_PAGE):
            assert "if (windowSpan() <= 48 * 60 * 60) {" in page
            assert 'if (state.range === "24h") {' not in page


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
