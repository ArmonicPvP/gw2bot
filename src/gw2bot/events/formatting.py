from __future__ import annotations

import calendar
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import discord

from gw2bot.events.models import (
    CATEGORY_EMOJI,
    EMOJI_ALACRITY,
    EMOJI_QUICKNESS,
    HEAL_ROLES,
    ROLE_EMOJI,
    STATUS_COLORS,
    STATUS_EMOJI,
    Event,
    EventCategory,
    EventRole,
    EventSignup,
    EventStatus,
    RepeatFrequency,
    count_roster,
    is_roster_full,
)

EVENT_DATETIME_FORMAT = "%m.%d.%Y %H:%M"
EVENT_DATETIME_PLACEHOLDER = "MM.dd.yyyy HH:mm"
EVENT_DURATION_PATTERN = re.compile(r"^(\d{1,3}):([0-5]\d)$")
EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_TOTAL_LIMIT = 6000
EMPTY_FIELD_TEXT = "—"
_TRUNCATION_MARKER = "…"

_WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def parse_event_datetime(text: str, timezone: ZoneInfo) -> datetime:
    try:
        parsed = datetime.strptime(text.strip(), EVENT_DATETIME_FORMAT)
    except ValueError as exc:
        raise ValueError(
            f"The date and time must match `{EVENT_DATETIME_PLACEHOLDER}`, "
            "for example `12.31.2026 20:00`."
        ) from exc
    return parsed.replace(tzinfo=timezone).astimezone(UTC)


def parse_event_duration(text: str) -> int:
    match = EVENT_DURATION_PATTERN.match(text.strip())
    if match is None:
        raise ValueError(
            "The duration must match `HH:mm`, for example `01:30`."
        )
    minutes = int(match.group(1)) * 60 + int(match.group(2))
    if minutes <= 0:
        raise ValueError("The duration must be longer than zero minutes.")
    return minutes


def format_duration(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    return " ".join(parts) if parts else "0m"


def parse_repeat_days(frequency: RepeatFrequency, text: str) -> tuple[int, ...]:
    entries = [entry.strip() for entry in text.split(",") if entry.strip()]
    if frequency in (RepeatFrequency.NONE, RepeatFrequency.DAILY):
        # Specific days are meaningless for non-repeating or daily events;
        # silently ignore anything entered rather than rejecting the whole
        # submission over a harmless extra field.
        return ()
    if not entries:
        raise ValueError(
            "Enter the day(s) for a weekly or monthly repeating event, "
            "separated by commas."
        )
    if frequency is RepeatFrequency.WEEKLY:
        days: list[int] = []
        for entry in entries:
            key = entry.casefold()
            matches = [
                index
                for index, name in enumerate(_WEEKDAY_NAMES)
                if name == key or (len(key) >= 3 and name.startswith(key))
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"`{entry}` is not a day of the week. Use names such "
                    "as `Sunday, Wednesday`."
                )
            days.extend(matches)
        return tuple(sorted(set(days)))
    month_days: list[int] = []
    for entry in entries:
        if not entry.isdigit() or not 1 <= int(entry) <= 31:
            raise ValueError(
                f"`{entry}` is not a day of the month. Use numbers "
                "between 1 and 31, such as `1, 15, 30`."
            )
        month_days.append(int(entry))
    return tuple(sorted(set(month_days)))


def format_repeat_days(
    frequency: RepeatFrequency,
    repeat_days: tuple[int, ...],
) -> str:
    if frequency is RepeatFrequency.WEEKLY:
        return ", ".join(
            _WEEKDAY_NAMES[day].capitalize() for day in repeat_days
        )
    return ", ".join(str(day) for day in repeat_days)


def _clamped_month_day(year: int, month: int, day: int) -> int:
    return min(day, calendar.monthrange(year, month)[1])


def next_occurrence_start(
    frequency: RepeatFrequency,
    repeat_days: tuple[int, ...],
    previous_start: datetime,
    timezone: ZoneInfo,
) -> datetime:
    previous_local = previous_start.astimezone(timezone)
    previous_date = previous_local.date()

    def _at_event_time(day: date) -> datetime:
        return datetime(
            day.year,
            day.month,
            day.day,
            previous_local.hour,
            previous_local.minute,
            tzinfo=timezone,
        ).astimezone(UTC)

    if frequency is RepeatFrequency.DAILY:
        return _at_event_time(previous_date + timedelta(days=1))
    if frequency is RepeatFrequency.WEEKLY:
        if not repeat_days:
            raise ValueError("A weekly event needs at least one weekday")
        for offset in range(1, 8):
            candidate = previous_date + timedelta(days=offset)
            if candidate.weekday() in repeat_days:
                return _at_event_time(candidate)
        raise AssertionError("unreachable: a weekday repeats within 7 days")
    if frequency is RepeatFrequency.MONTHLY:
        if not repeat_days:
            raise ValueError("A monthly event needs at least one month day")
        year, month = previous_date.year, previous_date.month
        for _ in range(24):
            candidates = sorted(
                {
                    _clamped_month_day(year, month, day)
                    for day in repeat_days
                }
            )
            for day in candidates:
                candidate = date(year, month, day)
                if candidate > previous_date:
                    return _at_event_time(candidate)
            year, month = (year, month + 1) if month < 12 else (year + 1, 1)
        raise AssertionError("unreachable: a month day repeats within a year")
    raise ValueError("A non-repeating event has no next occurrence")


def compute_status(
    start_time: datetime,
    duration_minutes: int,
    now: datetime,
    roster_full: bool,
) -> EventStatus:
    end_time = start_time + timedelta(minutes=duration_minutes)
    if now >= end_time:
        return EventStatus.OVER
    if now >= start_time:
        return EventStatus.ONGOING
    if roster_full:
        return EventStatus.FULL
    return EventStatus.OPEN


def event_thread_name(
    status: EventStatus,
    start_time: datetime,
    timezone: ZoneInfo,
) -> str:
    start_local = start_time.astimezone(timezone)
    return (
        f"{STATUS_EMOJI[status]}|{start_local:%m.%d.%Y}|{start_local:%H.%M}"
    )


def format_role_groups(roles: tuple[EventRole, ...]) -> str:
    heal_emoji = [ROLE_EMOJI[role] for role in roles if role in HEAL_ROLES]
    dps_emoji = [ROLE_EMOJI[role] for role in roles if role not in HEAL_ROLES]
    parts: list[str] = []
    if heal_emoji:
        parts.append(f"Heal ({','.join(heal_emoji)})")
    if dps_emoji:
        parts.append(f"DPS ({','.join(dps_emoji)})")
    return " | ".join(parts)


def _chunk_lines(lines: list[str]) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > EMBED_FIELD_VALUE_LIMIT and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [EMPTY_FIELD_TEXT]


def _add_chunked_field(
    embed: discord.Embed,
    name: str,
    lines: list[str],
) -> None:
    for index, value in enumerate(_chunk_lines(lines)):
        embed.add_field(
            name=name if index == 0 else "​",
            value=value,
            inline=False,
        )


def _member_line(signup: EventSignup) -> str:
    role = signup.assigned_role or signup.role
    emoji = f"{ROLE_EMOJI[role]} " if role is not None else ""
    return f"└ {emoji}<@{signup.discord_user_id}>"


def _role_group_lines(signups: list[EventSignup]) -> list[str]:
    lines: list[str] = []
    for signup in signups:
        roles = tuple(role for role in (signup.role,) if role is not None)
        roles += signup.flex_roles
        described = format_role_groups(roles)
        lines.append(f"<@{signup.discord_user_id}>")
        lines.append(f"└ {described}" if described else "└ Participant")
    return lines


def event_embed(
    event: Event,
    signups: list[EventSignup],
    status: EventStatus,
    event_id_text: str | None = None,
    start_time: datetime | None = None,
) -> discord.Embed:
    capacity = event.capacity
    active = [signup for signup in signups if not signup.waitlisted]
    waitlisted = [signup for signup in signups if signup.waitlisted]
    embed = discord.Embed(
        title=f"{CATEGORY_EMOJI[event.category]} {event.title}",
        description=event.description,
        color=STATUS_COLORS[status],
    )
    start = start_time if start_time is not None else event.start_time
    start_epoch = int(start.timestamp())
    # Date & Time, Duration, and Leader share one row via inline fields.
    embed.add_field(
        name="📅 Date & Time",
        value=f"<t:{start_epoch}:f>",
        inline=True,
    )
    embed.add_field(
        name="⏳ Duration",
        value=format_duration(event.duration_minutes),
        inline=True,
    )
    embed.add_field(
        name="👑 Leader",
        value=f"<@{event.leader_discord_id}>",
        inline=True,
    )

    if capacity.has_roles:
        counts = count_roster(signups)
        embed.add_field(
            name=f"👥 Participants ({counts.active}/{capacity.total})",
            value="​",
            inline=False,
        )
        healers = [
            signup
            for signup in active
            if signup.assigned_role in HEAL_ROLES
        ]
        dps = [
            signup
            for signup in active
            if signup.assigned_role is not None
            and signup.assigned_role not in HEAL_ROLES
        ]
        _add_chunked_field(
            embed,
            f"💚 Healer ({len(healers)}/{capacity.healers})",
            [_member_line(signup) for signup in healers],
        )
        _add_chunked_field(
            embed,
            f"⚔️ DPS ({len(dps)}/{capacity.dps})",
            [_member_line(signup) for signup in dps],
        )
        embed.add_field(
            name="Boons",
            value=(
                f"{EMOJI_ALACRITY} {counts.alacrity}/{capacity.alacrity} | "
                f"{EMOJI_QUICKNESS} {counts.quickness}/{capacity.quickness}"
            ),
            inline=False,
        )
        # Flexroles and Waitlist are only shown when populated, so an empty
        # section never clutters the embed.
        flexers = [signup for signup in active if signup.flex_roles]
        if flexers:
            _add_chunked_field(
                embed,
                "🔁 Flexroles",
                _role_group_lines(flexers),
            )
        if waitlisted:
            _add_chunked_field(
                embed,
                "⌛️ Waitlist",
                _role_group_lines(waitlisted),
            )
    else:
        _add_chunked_field(
            embed,
            f"👥 Participants ({len(active)}/{capacity.total})",
            [_member_line(signup) for signup in active],
        )
        if waitlisted:
            _add_chunked_field(
                embed,
                "⌛️ Waitlist",
                [_member_line(signup) for signup in waitlisted],
            )

    footer_id = event_id_text if event_id_text is not None else str(
        event.event_id
    )
    embed.set_footer(text=f"eventID: {footer_id}")
    _fit_within_total_limit(embed)
    return embed


def _fit_within_total_limit(embed: discord.Embed) -> None:
    # Discord rejects any embed whose title, description, field names/values
    # and footer exceed 6000 characters in aggregate. A long author
    # description combined with a growing roster (the waitlist is unbounded)
    # can cross that line, which would make every signup/removal edit fail and
    # leave the public message stale forever. Reclaim budget from the author
    # description first, then from trailing roster fields, so the embed always
    # stays sendable.
    if len(embed) <= EMBED_TOTAL_LIMIT:
        return
    if embed.description:
        overflow = len(embed) - EMBED_TOTAL_LIMIT
        keep = len(embed.description) - overflow - len(_TRUNCATION_MARKER)
        if keep > 0:
            embed.description = (
                embed.description[:keep].rstrip() + _TRUNCATION_MARKER
            )
        else:
            embed.description = None
    while len(embed) > EMBED_TOTAL_LIMIT and embed.fields:
        index = len(embed.fields) - 1
        field = embed.fields[index]
        value = field.value or ""
        overflow = len(embed) - EMBED_TOTAL_LIMIT
        if len(value) > overflow + len(_TRUNCATION_MARKER):
            trimmed = value[: len(value) - overflow - len(_TRUNCATION_MARKER)]
            embed.set_field_at(
                index,
                name=field.name,
                value=trimmed.rstrip() + _TRUNCATION_MARKER,
                inline=field.inline,
            )
        else:
            embed.remove_field(index)


def confirm_embed() -> discord.Embed:
    return discord.Embed(
        title="Create new event",
        description=(
            "Above is what the event will look like. Would you like to "
            "post the event or change something?"
        ),
    )


def describe_repeat(
    frequency: RepeatFrequency,
    repeat_days: tuple[int, ...],
) -> str:
    if frequency is RepeatFrequency.NONE:
        return "Does not repeat"
    if frequency is RepeatFrequency.DAILY:
        return "Repeats daily"
    days = format_repeat_days(frequency, repeat_days)
    if frequency is RepeatFrequency.WEEKLY:
        return f"Repeats weekly on {days}"
    return f"Repeats monthly on day(s) {days}"


def category_choices() -> list[EventCategory]:
    return list(EventCategory)
