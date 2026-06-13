from datetime import UTC, datetime, timedelta
from typing import Any

from gw2bot.guild_members import (
    DISCORD_MESSAGE_LIMIT,
    GuildMemberCache,
    TrialMemberReportEntry,
    format_overdue_trial_report,
    get_overdue_trial_members,
    seconds_until_trial_report,
)


class FakeGuildApi:
    def __init__(self):
        self.calls = 0
        self.members: list[dict[str, Any]] = [
            {"name": "Member One.1234"},
            {"name": "Another Member.5678"},
        ]

    async def get_guild_members(self, guild_id: str) -> list[dict[str, Any]]:
        self.calls += 1
        return self.members


class TestGuildMemberCache:
    async def test_resolves_case_insensitively_and_reuses_cache(self) -> None:
        api = FakeGuildApi()
        now = [100.0]
        cache = GuildMemberCache(
            api,
            "guild-id",
            ttl_seconds=60,
            clock=lambda: now[0],
        )

        assert await cache.resolve("member one.1234") == "Member One.1234"
        assert await cache.resolve("Not A Member.9999") is None
        assert api.calls == 1

        now[0] = 161.0
        assert await cache.resolve("another member.5678") == "Another Member.5678"
        assert api.calls == 2

    async def test_force_refresh_verifies_current_guild_membership(self) -> None:
        api = FakeGuildApi()
        cache = GuildMemberCache(api, "guild-id", ttl_seconds=60)

        assert await cache.resolve("Member One.1234") == "Member One.1234"
        api.members = []

        assert (
            await cache.resolve("Member One.1234", force_refresh=True)
            is None
        )
        assert api.calls == 2


class TestTrialMemberReport:
    def test_finds_trial_members_at_or_past_fourteen_days(self) -> None:
        now = datetime(2026, 6, 7, 17, 0, tzinfo=UTC)
        members = [
            {
                "name": "Exactly.1234",
                "rank": "Trial",
                "joined": (now - timedelta(days=14)).isoformat(),
            },
            {
                "name": "Older.1234",
                "rank": "trial",
                "joined": (now - timedelta(days=30)).isoformat(),
            },
            {
                "name": "Recent.1234",
                "rank": "Trial",
                "joined": (now - timedelta(days=13, hours=23)).isoformat(),
            },
            {
                "name": "Sunborne.1234",
                "rank": "Sunborne",
                "joined": (now - timedelta(days=30)).isoformat(),
            },
            {"name": "MissingJoin.1234", "rank": "Trial"},
            {"name": "BadJoin.1234", "rank": "Trial", "joined": "not-a-date"},
        ]

        assert get_overdue_trial_members(members, now) == ["Exactly.1234", "Older.1234"]

    def test_formats_contextual_reports_within_discord_limit(self) -> None:
        usernames = [f"Long Trial Username {index:03d}.1234" for index in range(150)]

        messages = format_overdue_trial_report(usernames)

        assert len(messages) > 1
        assert all(len(message) <= DISCORD_MESSAGE_LIMIT for message in messages)
        for username in usernames:
            assert sum(username in message for message in messages) == 1
        assert all("ranked up to Sunborne" in message for message in messages)

    def test_empty_trial_report_does_not_create_message(self) -> None:
        assert format_overdue_trial_report([]) == []

    def test_formats_linked_discord_status_and_plain_fallback(self) -> None:
        messages = format_overdue_trial_report(
            [
                TrialMemberReportEntry("Linked.1234", 123456789, "Sunborne"),
                TrialMemberReportEntry("MentionOnly.9012", 987654321),
                TrialMemberReportEntry("Unresolved.5678"),
            ]
        )

        assert "* Linked.1234 - <@123456789> - Sunborne" in messages[0]
        assert "* MentionOnly.9012 - <@987654321>" in messages[0]
        assert "* Unresolved.5678" in messages[0]
        assert "Unresolved.5678 -" not in messages[0]

    def test_sorts_sunborne_then_trial_then_unresolved(self) -> None:
        message = format_overdue_trial_report(
            [
                TrialMemberReportEntry("Zulu.1234"),
                TrialMemberReportEntry("Bravo.1234", 2, "Trial"),
                TrialMemberReportEntry("Charlie.1234", 3, "Sunborne"),
                TrialMemberReportEntry("Alpha.1234", 1, "Trial"),
                TrialMemberReportEntry("Delta.1234", 4, "Sunborne"),
                TrialMemberReportEntry("Echo.1234", 5),
            ]
        )[0]

        lines = [line for line in message.splitlines() if line.startswith("* ")]
        assert lines == [
            "* Charlie.1234 - <@3> - Sunborne",
            "* Delta.1234 - <@4> - Sunborne",
            "* Alpha.1234 - <@1> - Trial",
            "* Bravo.1234 - <@2> - Trial",
            "* Echo.1234 - <@5>",
            "* Zulu.1234",
        ]

    def test_schedules_next_report_for_1700_utc(self) -> None:
        assert (
            seconds_until_trial_report(datetime(2026, 6, 7, 16, 30, tzinfo=UTC))
            == 30 * 60
        )
        assert (
            seconds_until_trial_report(datetime(2026, 6, 7, 17, 0, tzinfo=UTC))
            == 24 * 60 * 60
        )
