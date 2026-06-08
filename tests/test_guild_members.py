import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

from gw2bot.guild_members import (
    DISCORD_MESSAGE_LIMIT,
    GuildMemberCache,
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


class GuildMemberCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_case_insensitively_and_reuses_cache(self) -> None:
        api = FakeGuildApi()
        now = [100.0]
        cache = GuildMemberCache(
            api,
            "guild-id",
            ttl_seconds=60,
            clock=lambda: now[0],
        )

        self.assertEqual(await cache.resolve("member one.1234"), "Member One.1234")
        self.assertIsNone(await cache.resolve("Not A Member.9999"))
        self.assertEqual(api.calls, 1)

        now[0] = 161.0
        self.assertEqual(await cache.resolve("another member.5678"), "Another Member.5678")
        self.assertEqual(api.calls, 2)


class TrialMemberReportTests(unittest.TestCase):
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

        self.assertEqual(
            get_overdue_trial_members(members, now),
            ["Exactly.1234", "Older.1234"],
        )

    def test_formats_contextual_reports_within_discord_limit(self) -> None:
        usernames = [f"Long Trial Username {index:03d}.1234" for index in range(150)]

        messages = format_overdue_trial_report(usernames)

        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= DISCORD_MESSAGE_LIMIT for message in messages))
        for username in usernames:
            self.assertEqual(sum(username in message for message in messages), 1)
        self.assertTrue(all("ranked up to Sunborne" in message for message in messages))

    def test_empty_trial_report_does_not_create_message(self) -> None:
        self.assertEqual(format_overdue_trial_report([]), [])

    def test_schedules_next_report_for_1700_utc(self) -> None:
        self.assertEqual(
            seconds_until_trial_report(datetime(2026, 6, 7, 16, 30, tzinfo=UTC)),
            30 * 60,
        )
        self.assertEqual(
            seconds_until_trial_report(datetime(2026, 6, 7, 17, 0, tzinfo=UTC)),
            24 * 60 * 60,
        )


if __name__ == "__main__":
    unittest.main()
