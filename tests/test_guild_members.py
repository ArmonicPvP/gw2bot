import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gw2bot.guild_members import (
    DISCORD_MESSAGE_LIMIT,
    TRIAL_BEFORE_MARK_HEADER,
    TRIAL_WARNING_MARK_HEADER,
    TRIAL_WARNING_PENDING_HEADER,
    GuildMemberCache,
    TrialMemberReportEntry,
    filter_sunborne_discord_entries,
    format_overdue_trial_report,
    get_overdue_trial_members,
    get_recent_trial_members,
    partition_tracked_overdue_members,
    seconds_until_trial_report,
    select_pending_warning_members,
    select_warned_overdue_members,
)


class FakeGuildApi:
    def __init__(self):
        self.calls = 0
        self.members: list[dict[str, Any]] = [
            {"name": "Member One.1234", "rank": "Officer"},
            {"name": "Another Member.5678", "rank": "Member"},
        ]

    async def get_guild_members(self, guild_id: str) -> list[dict[str, Any]]:
        self.calls += 1
        return self.members


class BlockingGuildApi(FakeGuildApi):
    def __init__(self):
        super().__init__()
        self.block = False
        self.release = asyncio.Event()

    async def get_guild_members(self, guild_id: str) -> list[dict[str, Any]]:
        self.calls += 1
        if self.block:
            await self.release.wait()
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

    async def test_returns_usernames_with_rank_case_insensitively(self) -> None:
        api = FakeGuildApi()
        cache = GuildMemberCache(api, "guild-id", ttl_seconds=60)

        assert await cache.usernames_with_rank("officer") == {"Member One.1234"}
        assert await cache.usernames_with_rank("Member") == {"Another Member.5678"}
        assert api.calls == 1

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

    async def test_searches_case_insensitively_with_prefix_matches_first(
        self,
    ) -> None:
        api = FakeGuildApi()
        api.members = [
            {"name": "Another Member.5678"},
            {"name": "Member Zulu.1234"},
            {"name": "member Alpha.9012"},
            {"name": "Not Included.3456"},
        ]
        cache = GuildMemberCache(api, "guild-id", ttl_seconds=60)
        await cache.resolve("Not Included.3456")

        assert await cache.search("MEMBER", limit=2) == [
            "member Alpha.9012",
            "Member Zulu.1234",
        ]
        assert api.calls == 1

    async def test_search_logging_omits_typed_query(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "autocomplete-query-secret"
        cache = GuildMemberCache(FakeGuildApi(), "guild-id", ttl_seconds=60)
        await cache.resolve("Member One.1234")

        with caplog.at_level(logging.DEBUG, logger="gw2bot.guild_members"):
            assert await cache.search(secret) == []

        assert secret not in caplog.text
        assert (
            "Guild member cache search started; query_chars=25 limit=25 "
            "cached_members=2 cache_expired=False"
            in caplog.text
        )
        assert (
            "query_chars=25 matches=0 returned=0 "
            "background_refresh_started=False"
            in caplog.text
        )
        assert "elapsed_ms=" in caplog.text

    async def test_search_returns_stale_snapshot_while_refresh_runs_once(
        self,
    ) -> None:
        api = BlockingGuildApi()
        now = [100.0]
        cache = GuildMemberCache(
            api,
            "guild-id",
            ttl_seconds=60,
            clock=lambda: now[0],
        )
        assert await cache.resolve("Member One.1234") == "Member One.1234"
        api.members = [{"name": "Member New.9012", "rank": "Member"}]
        api.block = True
        now[0] = 161.0

        assert await asyncio.wait_for(cache.search("member"), timeout=0.1) == [
            "Member One.1234",
            "Another Member.5678",
        ]
        await asyncio.sleep(0)
        assert api.calls == 2
        assert await cache.search("member") == [
            "Member One.1234",
            "Another Member.5678",
        ]
        assert api.calls == 2

        api.release.set()
        assert await cache.resolve("Member New.9012") == "Member New.9012"
        assert api.calls == 2

    async def test_background_refresh_failure_logs_type_without_exception_body(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "background-refresh-secret"

        class FailingGuildApi(FakeGuildApi):
            async def get_guild_members(
                self,
                guild_id: str,
            ) -> list[dict[str, Any]]:
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError(secret)
                return self.members

        api = FailingGuildApi()
        now = [100.0]
        cache = GuildMemberCache(
            api,
            "guild-id",
            ttl_seconds=60,
            clock=lambda: now[0],
        )
        await cache.resolve("Member One.1234")
        now[0] = 161.0

        with caplog.at_level(logging.DEBUG, logger="gw2bot.guild_members"):
            assert await cache.search("member") == [
                "Member One.1234",
                "Another Member.5678",
            ]
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert secret not in caplog.text
        assert (
            "Guild member cache background refresh failed; "
            "error_type=RuntimeError"
            in caplog.text
        )
        assert await cache.search("member") == [
            "Member One.1234",
            "Another Member.5678",
        ]
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

    def test_finds_trial_members_before_fourteen_days(self) -> None:
        now = datetime(2026, 6, 7, 17, 0, tzinfo=UTC)
        members = [
            {
                "name": "Exactly.1234",
                "rank": "Trial",
                "joined": (now - timedelta(days=14)).isoformat(),
            },
            {
                "name": "Recent.1234",
                "rank": "trial",
                "joined": (now - timedelta(days=13, hours=23)).isoformat(),
            },
            {
                "name": "Newest.1234",
                "rank": "Trial",
                "joined": (now - timedelta(hours=1)).isoformat(),
            },
            {
                "name": "Sunborne.1234",
                "rank": "Sunborne",
                "joined": (now - timedelta(days=1)).isoformat(),
            },
            {"name": "MissingJoin.1234", "rank": "Trial"},
            {"name": "BadJoin.1234", "rank": "Trial", "joined": "not-a-date"},
        ]

        assert get_recent_trial_members(members, now) == ["Newest.1234", "Recent.1234"]

    def test_filters_entries_to_sunborne_discord_status(self) -> None:
        entries = [
            TrialMemberReportEntry("EarlySunborne.1234", 1, "Sunborne"),
            TrialMemberReportEntry("StillTrial.1234", 2, "Trial"),
            TrialMemberReportEntry("Unresolved.1234"),
        ]

        assert filter_sunborne_discord_entries(entries) == [
            TrialMemberReportEntry("EarlySunborne.1234", 1, "Sunborne"),
        ]

    def test_formats_before_mark_report_with_custom_header(self) -> None:
        messages = format_overdue_trial_report(
            [TrialMemberReportEntry("EarlySunborne.1234", 1, "Sunborne")],
            header=TRIAL_BEFORE_MARK_HEADER,
        )

        assert messages[0].startswith("**Trial members before the 14-day mark**")
        assert "EarlySunborne.1234 - <@1> - Sunborne" in messages[0]
        assert "ranked up to Sunborne" not in messages[0]

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

    def test_uppercase_names_sort_before_lowercase(self) -> None:
        message = format_overdue_trial_report(
            [
                TrialMemberReportEntry("apple.1234"),
                TrialMemberReportEntry("Zebra.1234"),
                TrialMemberReportEntry("Apple.1234"),
                TrialMemberReportEntry("zebra.1234"),
            ]
        )[0]

        lines = [line for line in message.splitlines() if line.startswith("* ")]
        assert lines == [
            "* Apple.1234",
            "* Zebra.1234",
            "* apple.1234",
            "* zebra.1234",
        ]

    def test_partitions_overdue_members_by_tracked_status(self) -> None:
        untracked, tracked, stale = partition_tracked_overdue_members(
            ["Overdue.1234", "Tracked.5678"],
            {"tracked.5678", "Gone.9012"},
        )

        assert untracked == ["Overdue.1234"]
        assert tracked == ["Tracked.5678"]
        assert stale == {"Gone.9012"}

    def test_partition_returns_canonical_overdue_and_stored_stale_names(
        self,
    ) -> None:
        untracked, tracked, stale = partition_tracked_overdue_members(
            ["Canonical.1234"],
            {"CANONICAL.1234"},
        )

        assert untracked == []
        assert tracked == ["Canonical.1234"]
        assert stale == set()

    def test_selects_only_members_past_the_seven_day_warning_mark(self) -> None:
        now = datetime(2026, 6, 10, 17, 0, tzinfo=UTC)
        tracked_times = {
            "Warned.1234": now - timedelta(days=7),
            "Grace.5678": now - timedelta(days=6, hours=23),
            "CASEFOLD.9012": now - timedelta(days=10),
            "Untimed.3456": now,
        }

        warned = select_warned_overdue_members(
            ["Warned.1234", "Grace.5678", "casefold.9012", "NoTime.7890"],
            tracked_times,
            now,
        )

        assert warned == ["Warned.1234", "casefold.9012"]

    def test_warning_report_uses_seven_day_header(self) -> None:
        message = format_overdue_trial_report(
            [TrialMemberReportEntry("Tracked.1234")],
            header=TRIAL_WARNING_MARK_HEADER,
        )[0]

        assert message.startswith(
            "**Trial members past the 7-day warning mark (to be kicked)**"
        )
        assert "* Tracked.1234" in message

    def test_selects_pending_members_inside_the_warning_window(self) -> None:
        now = datetime(2026, 6, 10, 17, 0, tzinfo=UTC)
        tracked_times = {
            "Warned.1234": now - timedelta(days=7),
            "Grace.5678": now - timedelta(days=6, hours=23),
            "CASEFOLD.9012": now - timedelta(days=2),
        }

        pending = select_pending_warning_members(
            ["Warned.1234", "Grace.5678", "casefold.9012", "NoTime.7890"],
            tracked_times,
            now,
        )

        assert pending == {
            "Grace.5678": now + timedelta(hours=1),
            "casefold.9012": now + timedelta(days=5),
        }

    def test_pending_warning_report_shows_kick_countdown_timestamp(self) -> None:
        deadline = datetime(2026, 6, 12, 17, 0, tzinfo=UTC)
        message = format_overdue_trial_report(
            [
                TrialMemberReportEntry(
                    "Tracked.1234",
                    discord_user_id=42,
                    discord_status="Trial",
                    warning_deadline=deadline,
                ),
                TrialMemberReportEntry(
                    "Unresolved.5678",
                    warning_deadline=deadline,
                ),
            ],
            header=TRIAL_WARNING_PENDING_HEADER,
        )[0]

        assert message.startswith(
            "**Trial members within the 7-day warning window**"
        )
        expected = int(deadline.timestamp())
        assert (
            f"* Tracked.1234 - <@42> - Trial - kick <t:{expected}:R>" in message
        )
        assert f"* Unresolved.5678 - kick <t:{expected}:R>" in message

    def test_schedules_next_report_for_1700_utc(self) -> None:
        assert (
            seconds_until_trial_report(datetime(2026, 6, 7, 16, 30, tzinfo=UTC))
            == 30 * 60
        )
        assert (
            seconds_until_trial_report(datetime(2026, 6, 7, 17, 0, tzinfo=UTC))
            == 24 * 60 * 60
        )
