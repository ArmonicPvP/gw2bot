from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import discord
import pytest

from gw2bot.events.formatting import (
    DRAFT_PENDING_TEXT,
    EMBED_TOTAL_LIMIT,
    ROSTER_UPDATE_HEADER,
    WAITLIST_EMOJI,
    compute_status,
    confirm_embed,
    describe_repeat,
    details_confirm_embed,
    details_preview_embed,
    event_embed,
    event_thread_name,
    format_duration,
    format_role_groups,
    next_occurrence_start,
    parse_event_datetime,
    parse_event_duration,
    parse_repeat_days,
    roster_update_messages,
    signup_edit_limit_message,
)
from gw2bot.guild_members import DISCORD_MESSAGE_LIMIT
from gw2bot.events.models import (
    CATEGORY_CAPACITIES,
    CATEGORY_EMOJI,
    EMOJI_ALACRITY,
    EMOJI_DPS,
    EMOJI_QUICKNESS,
    CategoryCapacity,
    Event,
    EventCategory,
    EventRole,
    EventSignup,
    EventStatus,
    RepeatFrequency,
    RoleChange,
    RosterUpdate,
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


def _roster_fields(embed: discord.Embed) -> list[tuple[str, str, bool]]:
    """The (name, value, inline) of every field below the event's header.

    Date & Time, Duration and Leader always lead the embed, so everything after
    them is the roster.
    """
    return [
        (field.name or "", field.value or "", bool(field.inline))
        for field in embed.fields[3:]
    ]


def _participant_columns(embed: discord.Embed) -> tuple[str, str]:
    (_, left, _), (_, right, _) = _roster_fields(embed)[:2]
    return left, right


class TestEventCategories:
    def test_every_category_has_an_emoji_and_a_capacity(self) -> None:
        # The pickers and the embed title index both maps by category, so a
        # category added without an entry raises a KeyError at render time.
        for category in EventCategory:
            assert category in CATEGORY_EMOJI
            assert category in CATEGORY_CAPACITIES

    def test_open_world_shares_the_role_less_wvw_capacity(self) -> None:
        capacity = CATEGORY_CAPACITIES[EventCategory.OPEN_WORLD]

        assert capacity == CATEGORY_CAPACITIES[EventCategory.WVW]
        assert not capacity.has_roles
        assert capacity.total == 50

    def test_general_is_role_less_and_uncapped(self) -> None:
        capacity = CATEGORY_CAPACITIES[EventCategory.GENERAL]

        # Same shape as WvW - one participants list, no roles or boons - but
        # without the headcount, so nobody is ever turned away.
        assert not capacity.has_roles
        assert capacity.total is None

    def test_dungeon_has_five_dps_with_boon_dps_slots(self) -> None:
        capacity = CATEGORY_CAPACITIES[EventCategory.DUNGEON]

        assert capacity.total == 5
        assert capacity.healers == 0
        assert capacity.dps == 5
        assert capacity.quickness == 1
        assert capacity.alacrity == 1
        assert capacity.required_boon_healers == 0
        assert capacity.required_boon_dps == 2


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

        assert name == "🟢 | 01.30.2027 | 20:05"

    def test_uses_the_status_emoji(self) -> None:
        start = datetime(2027, 1, 30, 20, 0, tzinfo=UTC)

        assert event_thread_name(
            EventStatus.OVER,
            start,
            UTC_ZONE,
        ).startswith("⚫️ |")


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


class TestRosterUpdateMessage:
    def test_reassignments_and_promotions_share_one_message(self) -> None:
        update = RosterUpdate(
            reassigned=(
                RoleChange(
                    discord_user_id=11,
                    old_role=EventRole.QUICKNESS_DPS,
                    new_role=EventRole.DPS,
                ),
            ),
            promoted=(
                make_signup(
                    12,
                    role=EventRole.QUICKNESS_HEAL,
                    assigned_role=EventRole.QUICKNESS_HEAL,
                ),
            ),
        )

        messages = roster_update_messages(update)

        assert len(messages) == 1
        lines = messages[0].splitlines()
        assert lines[0] == "🔀 **Roster update**"
        assert "<@11>" in lines[1]
        assert EventRole.QUICKNESS_DPS.value in lines[1]
        assert EventRole.DPS.value in lines[1]
        assert EMOJI_QUICKNESS in lines[1]
        assert EMOJI_DPS in lines[1]
        assert "<@12>" in lines[2]
        assert "moved up from the waitlist" in lines[2]
        assert EventRole.QUICKNESS_HEAL.value in lines[2]

    def test_role_less_promotion_omits_the_seat(self) -> None:
        update = RosterUpdate(promoted=(make_signup(12),))

        messages = roster_update_messages(update)

        assert len(messages) == 1
        assert "<@12> moved up from the waitlist" in messages[0]
        assert " as " not in messages[0]

    def test_empty_update_produces_no_message(self) -> None:
        assert roster_update_messages(RosterUpdate()) == []

    def test_large_promotion_batch_splits_across_messages(self) -> None:
        # Switching a capped event to the uncapped General category promotes
        # the whole waitlist at once, which outgrows a single Discord message.
        update = RosterUpdate(
            promoted=tuple(
                make_signup(10**17 + user_id) for user_id in range(120)
            ),
        )

        messages = roster_update_messages(update)

        assert len(messages) > 1
        assert all(
            len(message) <= DISCORD_MESSAGE_LIMIT for message in messages
        )
        # Every promotion is announced exactly once, and each part reads as a
        # roster update on its own.
        assert all(
            message.startswith(ROSTER_UPDATE_HEADER) for message in messages
        )
        lines = [
            line
            for message in messages
            for line in message.splitlines()
            if line != ROSTER_UPDATE_HEADER
        ]
        assert len(lines) == 120
        assert len(set(lines)) == 120


class TestSignupEditLimitMessage:
    def test_reports_time_until_the_next_token(self) -> None:
        # One token refills every three hours, so an empty bucket waits the
        # full period and a half-refilled one waits the remainder.
        assert "3h" in signup_edit_limit_message(0.0)
        assert "1h 30m" in signup_edit_limit_message(0.5)


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

        assert embed.title == (
            f"{CATEGORY_EMOJI[EventCategory.FRACTAL]} Kitty Cleanup"
        )
        assert embed.description == "Bring food."
        names = [field.name for field in embed.fields]
        assert names == [
            "📅 Date & Time",
            "⏳ Duration",
            "👑 Leader",
            "👥 Participants (2/5)",
            "💚 Healer (1/1)",
            "⚔️ DPS (1/4)",
            "Boons",
            "🔁 Flexroles",
            "⌛️ Waitlist",
        ]
        values = {field.name: field.value for field in embed.fields}
        start_epoch = int(event.start_time.timestamp())
        assert values["📅 Date & Time"] == f"<t:{start_epoch}:f>"
        assert values["⏳ Duration"] == "1h 30m"
        assert values["👑 Leader"] == "<@42>"
        assert values["💚 Healer (1/1)"] == f"└ {EMOJI_QUICKNESS} <@11>"
        # The waitlisted DPS (user 13) is listed under the DPS section below the
        # seated member, marked with the hourglass, but not counted in "1/4".
        assert values["⚔️ DPS (1/4)"] == (
            f"└ {EMOJI_ALACRITY} <@12>\n"
            f"└ {WAITLIST_EMOJI} {EMOJI_DPS} <@13>"
        )
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

    def test_waitlisted_members_listed_under_their_role_section(self) -> None:
        event = make_event(EventCategory.RAID)
        signups = [
            make_signup(1, EventRole.QUICKNESS_HEAL, EventRole.QUICKNESS_HEAL),
            make_signup(2, EventRole.ALACRITY_HEAL, EventRole.ALACRITY_HEAL),
            make_signup(3, EventRole.ALACRITY_DPS, EventRole.ALACRITY_DPS),
            # Waitlisted members have no assigned_role; they are grouped by the
            # role they requested.
            make_signup(4, EventRole.QUICKNESS_HEAL, None, waitlisted=True),
            make_signup(5, EventRole.QUICKNESS_DPS, None, waitlisted=True),
        ]

        embed = event_embed(event, signups, EventStatus.OPEN)

        values = {field.name: field.value for field in embed.fields}
        # The counts only reflect seated members, not the waitlisted ones.
        assert "💚 Healer (2/2)" in values
        assert "⚔️ DPS (1/8)" in values
        # Waitlisted members are appended below the seated ones, each marked
        # with the hourglass before their boon emoji.
        assert values["💚 Healer (2/2)"] == (
            f"└ {EMOJI_QUICKNESS} <@1>\n"
            f"└ {EMOJI_ALACRITY} <@2>\n"
            f"└ {WAITLIST_EMOJI} {EMOJI_QUICKNESS} <@4>"
        )
        assert values["⚔️ DPS (1/8)"] == (
            f"└ {EMOJI_ALACRITY} <@3>\n"
            f"└ {WAITLIST_EMOJI} {EMOJI_QUICKNESS} <@5>"
        )
        # The standalone Waitlist section is kept and still lists everyone on it.
        waitlist_value = values[f"{WAITLIST_EMOJI} Waitlist"]
        assert waitlist_value is not None
        assert "<@4>" in waitlist_value
        assert "<@5>" in waitlist_value

    def test_title_with_emoji_prefix_stays_within_title_limit(self) -> None:
        # A user may enter a title at the full 256-character limit; the emoji
        # prefix must not push the embed title past Discord's cap.
        event = replace(make_event(), title="x" * 256)

        embed = event_embed(event, [], EventStatus.OPEN)

        assert embed.title is not None
        assert len(embed.title) <= 256
        assert embed.title.startswith(
            f"{CATEGORY_EMOJI[EventCategory.FRACTAL]} "
        )
        assert embed.title.endswith("…")

    def test_raid_and_strike_use_ten_player_capacities(self) -> None:
        for category in (EventCategory.RAID, EventCategory.STRIKE):
            embed = event_embed(make_event(category), [], EventStatus.OPEN)

            names = [field.name for field in embed.fields]
            assert "👥 Participants (0/10)" in names
            assert "💚 Healer (0/2)" in names
            assert "⚔️ DPS (0/8)" in names
            values = {field.name: field.value for field in embed.fields}
            assert values["Boons"] == (
                f"{EMOJI_ALACRITY} 0/2 | {EMOJI_QUICKNESS} 0/2"
            )

    def test_dungeon_embed_omits_zero_capacity_healer_section(self) -> None:
        embed = event_embed(
            make_event(EventCategory.DUNGEON), [], EventStatus.OPEN
        )

        names = [field.name or "" for field in embed.fields]
        assert "👥 Participants (0/5)" in names
        assert not any("Healer" in name for name in names)
        assert "⚔️ DPS (0/5)" in names
        values = {field.name: field.value for field in embed.fields}
        assert values["Boons"] == (
            f"{EMOJI_ALACRITY} 0/1 | {EMOJI_QUICKNESS} 0/1"
        )

    def test_wvw_embed_lists_participants_without_roles(self) -> None:
        event = make_event(EventCategory.WVW)
        signups = [make_signup(user_id) for user_id in range(1, 4)]

        embed = event_embed(event, signups, EventStatus.OPEN)

        names = [field.name or "" for field in embed.fields]
        assert "👥 Participants (3/50)" in names
        assert not any("Healer" in name for name in names)
        assert not any(name.startswith("Boons") for name in names)
        # A fifty-seat squad is listed in two columns, the left one taking the
        # extra name on an odd count.
        left, right = _participant_columns(embed)
        assert left == "└ <@1>\n└ <@2>"
        assert right == "└ <@3>"

    def test_open_world_embed_matches_the_role_less_wvw_layout(self) -> None:
        event = make_event(EventCategory.OPEN_WORLD)
        signups = [make_signup(user_id) for user_id in range(1, 4)]

        embed = event_embed(event, signups, EventStatus.OPEN)

        assert embed.title == (
            f"{CATEGORY_EMOJI[EventCategory.OPEN_WORLD]} Kitty Cleanup"
        )
        names = [field.name or "" for field in embed.fields]
        assert "👥 Participants (3/50)" in names
        assert not any("Healer" in name for name in names)
        assert not any(name.startswith("Boons") for name in names)
        left, right = _participant_columns(embed)
        assert left == "└ <@1>\n└ <@2>"
        assert right == "└ <@3>"

    def test_general_embed_lists_participants_without_a_cap(self) -> None:
        event = make_event(EventCategory.GENERAL)
        signups = [make_signup(user_id) for user_id in range(1, 4)]

        embed = event_embed(event, signups, EventStatus.OPEN)

        assert embed.title == (
            f"{CATEGORY_EMOJI[EventCategory.GENERAL]} Kitty Cleanup"
        )
        names = [field.name or "" for field in embed.fields]
        # No denominator: an uncapped roster has no total to count towards.
        assert "👥 Participants (3)" in names
        assert not any("Healer" in name for name in names)
        assert not any(name.startswith("Boons") for name in names)
        assert not any(WAITLIST_EMOJI in name for name in names)
        # An uncapped roster grows without bound, so it uses the two-column
        # layout too.
        left, right = _participant_columns(embed)
        assert left == "└ <@1>\n└ <@2>"
        assert right == "└ <@3>"

    def test_small_role_less_squad_keeps_one_participant_column(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(
            CATEGORY_CAPACITIES,
            EventCategory.WVW,
            CategoryCapacity(10, None, None, None, None),
        )
        event = make_event(EventCategory.WVW)
        signups = [make_signup(user_id) for user_id in range(1, 4)]

        embed = event_embed(event, signups, EventStatus.OPEN)

        assert _roster_fields(embed) == [
            ("👥 Participants (3/10)", "└ <@1>\n└ <@2>\n└ <@3>", False),
        ]

    def test_participant_columns_sit_side_by_side(self) -> None:
        event = make_event(EventCategory.GENERAL)
        signups = [make_signup(user_id) for user_id in range(1, 7)]

        embed = event_embed(event, signups, EventStatus.OPEN)

        # Two inline fields and nothing else on the row: Discord renders them
        # side by side as the two columns. The second column carries no header
        # of its own, so both columns start on the same line.
        assert _roster_fields(embed) == [
            ("👥 Participants (6)", "└ <@1>\n└ <@2>\n└ <@3>", True),
            ("​", "└ <@4>\n└ <@5>\n└ <@6>", True),
        ]

    def test_empty_participant_list_keeps_the_placeholder(self) -> None:
        event = make_event(EventCategory.GENERAL)

        embed = event_embed(event, [], EventStatus.OPEN)

        assert _roster_fields(embed) == [
            ("👥 Participants (0)", "—", True),
            ("​", "​", True),
        ]

    def test_full_wvw_roster_fits_two_columns(self) -> None:
        event = make_event(EventCategory.WVW)
        signups = [
            make_signup(10**17 + user_id) for user_id in range(50)
        ]

        embed = event_embed(event, signups, EventStatus.OPEN)

        roster = _roster_fields(embed)
        # Split in half, a full fifty-seat squad still fits one column each,
        # so the roster is two fields rather than a run of continuations.
        assert len(roster) == 2
        assert all(len(value) <= 1024 for _, value, _ in roster)
        assert "".join(value for _, value, _ in roster).count("<@") == 50

    def test_long_participant_columns_chunk_row_by_row(self) -> None:
        event = make_event(EventCategory.GENERAL)
        signups = [make_signup(10**17 + user_id) for user_id in range(100)]

        embed = event_embed(event, signups, EventStatus.OPEN)

        roster = _roster_fields(embed)
        assert all(len(value) <= 1024 for _, value, _ in roster)
        # Each column overflows one field, so the roster continues on a second
        # row of two columns. The full-width spacer between the rows is what
        # stops Discord from pulling the continuation up beside the first row.
        names = [name for name, _, _ in roster]
        assert names[0] == "👥 Participants (100)"
        assert names[1:] == ["​", "​", "​", "​"]
        assert [inline for _, _, inline in roster] == [
            True,
            True,
            False,
            True,
            True,
        ]
        assert roster[2][1] == "​"
        left = f"{roster[0][1]}\n{roster[3][1]}"
        right = f"{roster[1][1]}\n{roster[4][1]}"
        assert left.count("<@") == 50
        assert right.count("<@") == 50
        # The columns are read down the left and then down the right, so the
        # first half of the roster is the left column.
        assert left.startswith(f"└ <@{10**17}>")
        assert right.startswith(f"└ <@{10**17 + 50}>")

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
            if field.name != "👑 Leader"
        )
        assert mentions == len(active) + len(waitlist)

    def test_embed_within_limit_is_left_untouched(self) -> None:
        event = make_event(EventCategory.WVW)
        signups = [make_signup(user_id) for user_id in range(1, 4)]

        embed = event_embed(event, signups, EventStatus.OPEN)

        assert len(embed) <= EMBED_TOTAL_LIMIT
        assert embed.description == "Bring food."


class TestDetailsPreviewEmbed:
    def test_shows_the_details_entered_so_far(self) -> None:
        embed = details_preview_embed(
            EventCategory.FRACTAL,
            "Kitty Cleanup",
            "Bring food.",
            1234,
            42,
        )

        assert embed.title == (
            f"{CATEGORY_EMOJI[EventCategory.FRACTAL]} Kitty Cleanup"
        )
        assert embed.description == "Bring food."
        values = {field.name: field.value for field in embed.fields}
        assert values["👑 Leader"] == "<@42>"
        assert values["📢 Posted in"] == "<#1234>"

    def test_marks_the_unanswered_schedule_as_pending(self) -> None:
        embed = details_preview_embed(
            EventCategory.FRACTAL,
            "Kitty Cleanup",
            "Bring food.",
            1234,
            42,
        )

        values = {field.name: field.value for field in embed.fields}
        assert values["📅 Date & Time"] == DRAFT_PENDING_TEXT
        assert values["⏳ Duration"] == DRAFT_PENDING_TEXT

    def test_renders_without_a_category_or_channel(self) -> None:
        embed = details_preview_embed(None, "Kitty Cleanup", "", None, 42)

        assert embed.title == "Kitty Cleanup"
        values = {field.name: field.value for field in embed.fields}
        assert values["📢 Posted in"] == DRAFT_PENDING_TEXT

    def test_title_with_emoji_prefix_stays_within_title_limit(self) -> None:
        embed = details_preview_embed(
            EventCategory.FRACTAL,
            "x" * 256,
            "Bring food.",
            1234,
            42,
        )

        assert embed.title is not None
        assert len(embed.title) <= 256
        assert embed.title.endswith("…")

    def test_long_description_stays_within_the_total_limit(self) -> None:
        embed = details_preview_embed(
            EventCategory.FRACTAL,
            "Kitty Cleanup",
            "x" * (EMBED_TOTAL_LIMIT + 100),
            1234,
            42,
        )

        assert len(embed) <= EMBED_TOTAL_LIMIT

    def test_footer_carries_the_placeholder_event_id(self) -> None:
        embed = details_preview_embed(
            EventCategory.FRACTAL,
            "Kitty Cleanup",
            "Bring food.",
            1234,
            42,
            "—",
        )

        assert embed.footer.text == "eventID: —"


class TestDetailsConfirmEmbed:
    def test_offers_next_or_change(self) -> None:
        embed = details_confirm_embed()

        assert embed.title == "Create new event"
        assert embed.description is not None
        assert "**Next**" in embed.description
        assert "change something" in embed.description
        assert "step 2 of 3" in embed.description


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
