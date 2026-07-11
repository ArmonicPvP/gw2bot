from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from gw2bot.events.formatting import (
    EMBED_TOTAL_LIMIT,
    compute_status,
    confirm_embed,
    describe_repeat,
    event_embed,
    event_thread_name,
    format_duration,
    format_role_groups,
    next_occurrence_start,
    parse_event_datetime,
    parse_event_duration,
    parse_repeat_days,
)
from gw2bot.events.models import (
    EMOJI_ALACRITY,
    EMOJI_DPS,
    EMOJI_QUICKNESS,
    Event,
    EventCategory,
    EventRole,
    EventSignup,
    EventStatus,
    RepeatFrequency,
)

NEW_YORK = ZoneInfo("America/New_York")
UTC_ZONE = ZoneInfo("UTC")


def make_event(
    category: EventCategory = EventCategory.FRACTAL,
    start_time: datetime | None = None,
    repeat_frequency: RepeatFrequency = RepeatFrequency.NONE,
    repeat_days: tuple[int, ...] = (),
) -> Event:
    return Event(
        event_id=7,
        category=category,
        title="Kitty Cleanup",
        description="Bring food.",
        channel_id=1234,
        leader_discord_id=42,
        start_time=(
            start_time
            if start_time is not None
            else datetime(2027, 1, 30, 20, 0, tzinfo=UTC)
        ),
        duration_minutes=90,
        repeat_frequency=repeat_frequency,
        repeat_days=repeat_days,
    )


def make_signup(
    user_id: int,
    role: EventRole | None = None,
    assigned_role: EventRole | None = None,
    flex_roles: tuple[EventRole, ...] = (),
    waitlisted: bool = False,
) -> EventSignup:
    return EventSignup(
        occurrence_id=1,
        discord_user_id=user_id,
        role=role,
        assigned_role=assigned_role,
        flex_roles=flex_roles,
        signed_up_at=datetime(2027, 1, 1, tzinfo=UTC),
        waitlisted=waitlisted,
    )


class TestParseEventDatetime:
    def test_interprets_input_in_the_configured_timezone(self) -> None:
        parsed = parse_event_datetime("01.30.2027 20:00", NEW_YORK)

        assert parsed == datetime(2027, 1, 31, 1, 0, tzinfo=UTC)
        assert parsed.tzinfo == UTC

    def test_rejects_malformed_input_with_format_hint(self) -> None:
        with pytest.raises(ValueError, match="MM.dd.yyyy HH:mm"):
            parse_event_datetime("2027-01-30 20:00", UTC_ZONE)

    def test_rejects_impossible_dates(self) -> None:
        with pytest.raises(ValueError):
            parse_event_datetime("02.30.2027 20:00", UTC_ZONE)


class TestParseEventDuration:
    def test_parses_hours_and_minutes(self) -> None:
        assert parse_event_duration("01:30") == 90
        assert parse_event_duration("00:45") == 45
        assert parse_event_duration("100:05") == 6005

    def test_rejects_malformed_and_zero_durations(self) -> None:
        for text in ("90", "1h30", "01:60", ""):
            with pytest.raises(ValueError):
                parse_event_duration(text)
        with pytest.raises(ValueError, match="longer than zero"):
            parse_event_duration("00:00")

    def test_formats_duration_as_hours_and_minutes(self) -> None:
        assert format_duration(parse_event_duration("02:05")) == "2h 5m"
        assert format_duration(parse_event_duration("01:00")) == "1h"
        assert format_duration(parse_event_duration("01:08")) == "1h 8m"
        assert format_duration(parse_event_duration("00:45")) == "45m"


class TestParseRepeatDays:
    def test_parses_weekday_names_and_abbreviations(self) -> None:
        days = parse_repeat_days(RepeatFrequency.WEEKLY, "Sunday, wed, Sun")

        assert days == (2, 6)

    def test_rejects_unknown_weekday(self) -> None:
        with pytest.raises(ValueError, match="not a day of the week"):
            parse_repeat_days(RepeatFrequency.WEEKLY, "Sunday, Blursday")

    def test_parses_month_days(self) -> None:
        assert parse_repeat_days(RepeatFrequency.MONTHLY, "30, 1, 15") == (
            1,
            15,
            30,
        )

    def test_rejects_out_of_range_month_days(self) -> None:
        for text in ("0", "32", "first"):
            with pytest.raises(ValueError, match="not a day of the month"):
                parse_repeat_days(RepeatFrequency.MONTHLY, text)

    def test_requires_days_for_weekly_and_monthly(self) -> None:
        for frequency in (RepeatFrequency.WEEKLY, RepeatFrequency.MONTHLY):
            with pytest.raises(ValueError, match="Enter the day"):
                parse_repeat_days(frequency, "  ")

    def test_ignores_days_for_daily(self) -> None:
        assert parse_repeat_days(RepeatFrequency.DAILY, "") == ()
        # Days are meaningless for a daily event, so extra input is ignored
        # rather than rejected.
        assert parse_repeat_days(RepeatFrequency.DAILY, "Monday") == ()
        assert parse_repeat_days(RepeatFrequency.NONE, "1, 15") == ()


class TestNextOccurrenceStart:
    def test_daily_moves_one_day_at_the_same_local_time(self) -> None:
        start = datetime(2027, 1, 31, 1, 0, tzinfo=UTC)

        next_start = next_occurrence_start(
            RepeatFrequency.DAILY,
            (),
            start,
            NEW_YORK,
        )

        assert next_start.astimezone(NEW_YORK) == datetime(
            2027, 1, 31, 20, 0, tzinfo=NEW_YORK
        )

    def test_weekly_picks_the_next_selected_weekday(self) -> None:
        # 2027-01-30 is a Saturday in New York.
        start = datetime(2027, 1, 31, 1, 0, tzinfo=UTC)

        next_start = next_occurrence_start(
            RepeatFrequency.WEEKLY,
            (2, 6),
            start,
            NEW_YORK,
        )

        local = next_start.astimezone(NEW_YORK)
        assert local.weekday() == 6
        assert local == datetime(2027, 1, 31, 20, 0, tzinfo=NEW_YORK)

    def test_monthly_clamps_to_the_last_day_of_short_months(self) -> None:
        start = datetime(2027, 1, 30, 20, 0, tzinfo=UTC_ZONE)

        february = next_occurrence_start(
            RepeatFrequency.MONTHLY,
            (30,),
            start,
            UTC_ZONE,
        )
        march = next_occurrence_start(
            RepeatFrequency.MONTHLY,
            (30,),
            february,
            UTC_ZONE,
        )

        assert february == datetime(2027, 2, 28, 20, 0, tzinfo=UTC_ZONE)
        assert march == datetime(2027, 3, 30, 20, 0, tzinfo=UTC_ZONE)

    def test_monthly_clamps_to_leap_day_in_leap_years(self) -> None:
        start = datetime(2028, 1, 31, 20, 0, tzinfo=UTC_ZONE)

        february = next_occurrence_start(
            RepeatFrequency.MONTHLY,
            (31,),
            start,
            UTC_ZONE,
        )

        assert february == datetime(2028, 2, 29, 20, 0, tzinfo=UTC_ZONE)

    def test_monthly_supports_multiple_days_in_one_month(self) -> None:
        start = datetime(2027, 3, 1, 20, 0, tzinfo=UTC_ZONE)

        next_start = next_occurrence_start(
            RepeatFrequency.MONTHLY,
            (1, 15),
            start,
            UTC_ZONE,
        )

        assert next_start == datetime(2027, 3, 15, 20, 0, tzinfo=UTC_ZONE)

    def test_non_repeating_events_have_no_next_occurrence(self) -> None:
        with pytest.raises(ValueError):
            next_occurrence_start(
                RepeatFrequency.NONE,
                (),
                datetime(2027, 1, 1, tzinfo=UTC),
                UTC_ZONE,
            )


class TestComputeStatus:
    START = datetime(2027, 1, 30, 20, 0, tzinfo=UTC)

    def test_over_takes_precedence_over_everything(self) -> None:
        now = self.START.replace(hour=22)

        assert compute_status(self.START, 90, now, True) is EventStatus.OVER

    def test_ongoing_takes_precedence_over_full(self) -> None:
        now = self.START.replace(hour=20, minute=30)

        assert (
            compute_status(self.START, 90, now, True) is EventStatus.ONGOING
        )

    def test_full_before_start(self) -> None:
        now = self.START.replace(hour=10)

        assert compute_status(self.START, 90, now, True) is EventStatus.FULL

    def test_open_otherwise(self) -> None:
        now = self.START.replace(hour=10)

        assert compute_status(self.START, 90, now, False) is EventStatus.OPEN


class TestEventThreadName:
    def test_formats_status_emoji_date_and_time(self) -> None:
        start = datetime(2027, 1, 31, 1, 5, tzinfo=UTC)

        name = event_thread_name(EventStatus.OPEN, start, NEW_YORK)

        assert name == "🟢|01.30.2027|20.05"

    def test_uses_the_status_emoji(self) -> None:
        start = datetime(2027, 1, 30, 20, 0, tzinfo=UTC)

        assert event_thread_name(
            EventStatus.OVER,
            start,
            UTC_ZONE,
        ).startswith("⚫️|")


class TestFormatRoleGroups:
    def test_groups_heal_and_dps_roles_with_boon_emoji(self) -> None:
        text = format_role_groups(
            (
                EventRole.ALACRITY_HEAL,
                EventRole.QUICKNESS_HEAL,
                EventRole.DPS,
                EventRole.QUICKNESS_DPS,
            )
        )

        assert text == (
            f"Heal ({EMOJI_ALACRITY},{EMOJI_QUICKNESS}) | "
            f"DPS ({EMOJI_DPS},{EMOJI_QUICKNESS})"
        )

    def test_empty_roles_produce_empty_text(self) -> None:
        assert format_role_groups(()) == ""


class TestEventEmbed:
    def test_fractal_embed_layout(self) -> None:
        event = make_event()
        signups = [
            make_signup(
                11,
                EventRole.QUICKNESS_HEAL,
                EventRole.QUICKNESS_HEAL,
            ),
            make_signup(
                12,
                EventRole.ALACRITY_DPS,
                EventRole.ALACRITY_DPS,
                flex_roles=(EventRole.ALACRITY_HEAL,),
            ),
            make_signup(
                13,
                EventRole.DPS,
                None,
                flex_roles=(EventRole.QUICKNESS_DPS,),
                waitlisted=True,
            ),
        ]

        embed = event_embed(event, signups, EventStatus.OPEN)

        assert embed.title == "Kitty Cleanup"
        assert embed.description == "Bring food."
        names = [field.name for field in embed.fields]
        assert names == [
            "Date & Time",
            "Duration",
            "Leader",
            "Participants (2/5)",
            "Healer (1/1)",
            "DPS (1/4)",
            "Boons",
            "🔁 Flexroles",
            "⌛️ Waitlist",
        ]
        values = {field.name: field.value for field in embed.fields}
        start_epoch = int(event.start_time.timestamp())
        assert values["Date & Time"] == f"<t:{start_epoch}:F>"
        assert values["Duration"] == "1h 30m"
        assert values["Leader"] == "<@42>"
        assert values["Healer (1/1)"] == f"└ {EMOJI_QUICKNESS} <@11>"
        assert values["DPS (1/4)"] == f"└ {EMOJI_ALACRITY} <@12>"
        assert values["Boons"] == (
            f"{EMOJI_ALACRITY} 1/1 | {EMOJI_QUICKNESS} 1/1"
        )
        assert values["🔁 Flexroles"] == (
            f"<@12>\n└ Heal ({EMOJI_ALACRITY}) | DPS ({EMOJI_ALACRITY})"
        )
        assert values["⌛️ Waitlist"] == (
            f"<@13>\n└ DPS ({EMOJI_DPS},{EMOJI_QUICKNESS})"
        )
        assert embed.footer.text == "eventID: 7"

    def test_raid_and_strike_use_ten_player_capacities(self) -> None:
        for category in (EventCategory.RAID, EventCategory.STRIKE):
            embed = event_embed(make_event(category), [], EventStatus.OPEN)

            names = [field.name for field in embed.fields]
            assert "Participants (0/10)" in names
            assert "Healer (0/2)" in names
            assert "DPS (0/8)" in names
            values = {field.name: field.value for field in embed.fields}
            assert values["Boons"] == (
                f"{EMOJI_ALACRITY} 0/2 | {EMOJI_QUICKNESS} 0/2"
            )

    def test_wvw_embed_lists_participants_without_roles(self) -> None:
        event = make_event(EventCategory.WVW)
        signups = [make_signup(user_id) for user_id in range(1, 4)]

        embed = event_embed(event, signups, EventStatus.OPEN)

        names = [field.name or "" for field in embed.fields]
        assert "Participants (3/50)" in names
        assert not any(name.startswith("Healer") for name in names)
        assert not any(name.startswith("Boons") for name in names)
        values = {field.name: field.value for field in embed.fields}
        assert values["Participants (3/50)"] == "└ <@1>\n└ <@2>\n└ <@3>"

    def test_wvw_participant_list_chunks_below_field_value_limit(self) -> None:
        event = make_event(EventCategory.WVW)
        signups = [
            make_signup(10**17 + user_id) for user_id in range(50)
        ]

        embed = event_embed(event, signups, EventStatus.OPEN)

        participant_fields = [
            field
            for field in embed.fields
            if field.name and field.name.startswith("Participants")
        ]
        assert participant_fields
        assert all(
            field.value is not None and len(field.value) <= 1024
            for field in embed.fields
        )
        # The 50 participants overflow one 1024-character field, so the
        # list continues in unnamed follow-up fields.
        assert len(participant_fields) == 1
        continuation = "".join(
            field.value or ""
            for field in embed.fields
            if field.name is not None
            and (field.name.startswith("Participants") or field.name == "​")
        )
        assert continuation.count("<@") == 50

    def test_embed_color_follows_status(self) -> None:
        event = make_event()

        def color_of(status: EventStatus) -> int:
            color = event_embed(event, [], status).color
            assert color is not None
            return color.value

        assert color_of(EventStatus.OPEN) == 0x2ECC71
        assert color_of(EventStatus.FULL) == 0xE74C3C
        assert color_of(EventStatus.ONGOING) == 0xF1C40F
        assert color_of(EventStatus.OVER) == 0x31373D

    def test_preview_footer_uses_placeholder_id(self) -> None:
        embed = event_embed(
            make_event(),
            [],
            EventStatus.OPEN,
            event_id_text="—",
        )

        assert embed.footer.text == "eventID: —"

    def test_long_description_and_roster_stay_within_aggregate_limit(
        self,
    ) -> None:
        event = replace(
            make_event(EventCategory.WVW),
            description="x" * 4000,
        )
        active = [make_signup(10**17 + user_id) for user_id in range(50)]
        waitlist = [
            make_signup(10**17 + 1000 + user_id, waitlisted=True)
            for user_id in range(30)
        ]

        embed = event_embed(event, active + waitlist, EventStatus.OPEN)

        # Discord rejects embeds over 6000 characters; without a budget the
        # roster would push a full description past that and every edit fails.
        assert len(embed) <= EMBED_TOTAL_LIMIT
        # The description is the oversized part, so it is what gets trimmed;
        # every roster member is still listed.
        assert embed.description is not None
        assert embed.description.endswith("…")
        mentions = sum(
            (field.value or "").count("<@")
            for field in embed.fields
            if field.name != "Leader"
        )
        assert mentions == len(active) + len(waitlist)

    def test_embed_within_limit_is_left_untouched(self) -> None:
        event = make_event(EventCategory.WVW)
        signups = [make_signup(user_id) for user_id in range(1, 4)]

        embed = event_embed(event, signups, EventStatus.OPEN)

        assert len(embed) <= EMBED_TOTAL_LIMIT
        assert embed.description == "Bring food."


class TestConfirmEmbed:
    def test_confirm_embed_offers_post_or_change(self) -> None:
        embed = confirm_embed()

        assert embed.title == "Create new event"
        assert embed.description is not None
        assert "post the event or change something" in embed.description


class TestDescribeRepeat:
    def test_describes_each_frequency(self) -> None:
        assert describe_repeat(RepeatFrequency.NONE, ()) == "Does not repeat"
        assert describe_repeat(RepeatFrequency.DAILY, ()) == "Repeats daily"
        assert (
            describe_repeat(RepeatFrequency.WEEKLY, (2, 6))
            == "Repeats weekly on Wednesday, Sunday"
        )
        assert (
            describe_repeat(RepeatFrequency.MONTHLY, (1, 30))
            == "Repeats monthly on day(s) 1, 30"
        )
