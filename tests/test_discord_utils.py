from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gw2bot.discord_utils import (
    GuildMembership,
    resolve_display_name,
    resolve_display_names,
    resolve_guild_membership,
    resolve_guild_memberships,
    send_direct_message,
    send_interaction_notice,
)

from factories import forbidden_error, not_found_error


class FakeUser:
    def __init__(self, user_id: int, display_name: str):
        self.id = user_id
        self.display_name = display_name
        self.send = AsyncMock()


class FakeBot:
    def __init__(self, names: dict[int, str] | None = None):
        self.names = names if names is not None else {}
        self.users: dict[int, FakeUser] = {}
        self.fetch_errors: dict[int, Exception] = {}
        self.send_errors: dict[int, Exception] = {}
        self.fetched: list[int] = []

    async def fetch_user(self, user_id: int) -> Any:
        self.fetched.append(user_id)
        error = self.fetch_errors.get(user_id)
        if error is not None:
            raise error
        user = self.users.get(user_id)
        if user is None:
            user = FakeUser(user_id, self.names.get(user_id, f"user{user_id}"))
            send_error = self.send_errors.get(user_id)
            if send_error is not None:
                user.send = AsyncMock(side_effect=send_error)
            self.users[user_id] = user
        return user


class FakeGuild:
    def __init__(
        self,
        cached: dict[int, str] | None = None,
        fetchable: dict[int, str] | None = None,
        fetch_error: Exception | None = None,
    ):
        self._cached = cached if cached is not None else {}
        self._fetchable = fetchable if fetchable is not None else {}
        # What an unknown member raises. NotFound (the default) is Discord
        # saying the user has left; anything else leaves membership unknown.
        self._fetch_error = fetch_error
        self.fetched: list[int] = []

    def get_member(self, user_id: int) -> Any:
        name = self._cached.get(user_id)
        if name is None:
            return None
        return SimpleNamespace(id=user_id, display_name=name)

    async def fetch_member(self, user_id: int) -> Any:
        self.fetched.append(user_id)
        name = self._fetchable.get(user_id)
        if name is None:
            raise (
                self._fetch_error
                if self._fetch_error is not None
                else not_found_error()
            )
        return SimpleNamespace(id=user_id, display_name=name)


class TestResolveDisplayName:
    async def test_prefers_the_cached_guild_member(self) -> None:
        bot = FakeBot({7: "global"})
        guild = FakeGuild(cached={7: "nickname"})

        name = await resolve_display_name(cast(Any, bot), cast(Any, guild), 7)

        # A server nickname wins, and a cache hit costs no request at all.
        assert name == "nickname"
        assert guild.fetched == []
        assert bot.fetched == []

    async def test_fetches_the_member_when_the_cache_is_empty(self) -> None:
        # The bot runs without the members intent, so this is the normal path.
        bot = FakeBot({7: "global"})
        guild = FakeGuild(fetchable={7: "nickname"})

        name = await resolve_display_name(cast(Any, bot), cast(Any, guild), 7)

        assert name == "nickname"
        assert bot.fetched == []

    async def test_falls_back_to_the_global_user(self) -> None:
        bot = FakeBot({7: "global"})
        guild = FakeGuild()

        name = await resolve_display_name(cast(Any, bot), cast(Any, guild), 7)

        assert name == "global"

    async def test_works_without_a_guild(self) -> None:
        bot = FakeBot({7: "global"})

        name = await resolve_display_name(cast(Any, bot), None, 7)

        assert name == "global"

    async def test_reports_an_unreachable_user_as_none(self) -> None:
        bot = FakeBot()
        bot.fetch_errors[7] = not_found_error()

        name = await resolve_display_name(cast(Any, bot), None, 7)

        assert name is None

    async def test_a_failed_guild_lookup_is_logged_before_the_fallback(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The global lookup succeeds here, so the aggregate counts report a
        # clean resolution; without this entry the failed guild fetch - and
        # the lost nickname - would leave no trace at all.
        bot = FakeBot({7: "SECRETNAME"})
        guild = FakeGuild(fetch_error=forbidden_error(50001))

        with caplog.at_level("DEBUG"):
            name = await resolve_display_name(
                cast(Any, bot),
                cast(Any, guild),
                7,
            )

        assert name == "SECRETNAME"
        assert "Guild member lookup failed" in caplog.text
        assert "Forbidden" in caplog.text
        assert "SECRETNAME" not in caplog.text

    async def test_a_failed_global_lookup_logs_the_failure_type(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = FakeBot()
        bot.fetch_errors[7] = forbidden_error(50001)

        with caplog.at_level("DEBUG"):
            name = await resolve_display_name(cast(Any, bot), None, 7)

        assert name is None
        assert "Display name lookup failed" in caplog.text
        assert "Forbidden" in caplog.text


class TestResolveDisplayNames:
    async def test_resolves_every_id(self) -> None:
        bot = FakeBot({1: "one", 2: "two"})

        names = await resolve_display_names(cast(Any, bot), None, [1, 2])

        assert names == {1: "one", 2: "two"}

    async def test_deduplicates_repeated_ids(self) -> None:
        bot = FakeBot({1: "one"})

        names = await resolve_display_names(cast(Any, bot), None, [1, 1, 1])

        assert names == {1: "one"}
        assert bot.fetched == [1]

    async def test_a_failed_lookup_does_not_lose_the_others(self) -> None:
        bot = FakeBot({1: "one", 3: "three"})
        bot.fetch_errors[2] = not_found_error()

        names = await resolve_display_names(cast(Any, bot), None, [1, 2, 3])

        assert names == {1: "one", 2: None, 3: "three"}

    async def test_no_ids_makes_no_requests(self) -> None:
        bot = FakeBot()

        names = await resolve_display_names(cast(Any, bot), None, [])

        assert names == {}
        assert bot.fetched == []

    async def test_logs_counts_only(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = FakeBot({1: "SECRETNAME"})

        with caplog.at_level("DEBUG"):
            await resolve_display_names(cast(Any, bot), None, [1])

        assert "SECRETNAME" not in caplog.text
        assert "Resolved guild memberships" in caplog.text


class TestResolveGuildMembership:
    async def test_a_cached_member_is_in_the_guild(self) -> None:
        bot = FakeBot({7: "global"})
        guild = FakeGuild(cached={7: "nickname"})

        membership = await resolve_guild_membership(
            cast(Any, bot),
            cast(Any, guild),
            7,
        )

        assert membership == GuildMembership("nickname", True)

    async def test_a_fetched_member_is_in_the_guild(self) -> None:
        bot = FakeBot({7: "global"})
        guild = FakeGuild(fetchable={7: "nickname"})

        membership = await resolve_guild_membership(
            cast(Any, bot),
            cast(Any, guild),
            7,
        )

        assert membership == GuildMembership("nickname", True)

    async def test_an_unknown_member_has_left_the_guild(self) -> None:
        # Discord answering "unknown member" for a guild it does know is proof
        # the user is gone - the one answer that may cost someone their seat.
        bot = FakeBot({7: "global"})
        guild = FakeGuild()

        membership = await resolve_guild_membership(
            cast(Any, bot),
            cast(Any, guild),
            7,
        )

        # The global name still resolves, so a caller can say who left.
        assert membership == GuildMembership("global", False)

    async def test_a_failed_lookup_leaves_membership_unknown(self) -> None:
        # A permission error or an outage proves nothing about membership, and
        # reading it as a departure would empty rosters whenever Discord is
        # having a bad day.
        bot = FakeBot({7: "global"})
        guild = FakeGuild(fetch_error=forbidden_error(50001))

        membership = await resolve_guild_membership(
            cast(Any, bot),
            cast(Any, guild),
            7,
        )

        assert membership == GuildMembership("global", None)

    async def test_without_a_guild_membership_is_unknown(self) -> None:
        bot = FakeBot({7: "global"})

        membership = await resolve_guild_membership(cast(Any, bot), None, 7)

        assert membership == GuildMembership("global", None)

    async def test_a_departure_survives_an_unreachable_global_user(
        self,
    ) -> None:
        # The name is lost but the departure is not: the roster still has to
        # drop them.
        bot = FakeBot()
        bot.fetch_errors[7] = not_found_error()
        guild = FakeGuild()

        membership = await resolve_guild_membership(
            cast(Any, bot),
            cast(Any, guild),
            7,
        )

        assert membership == GuildMembership(None, False)

    async def test_resolves_a_whole_roster_at_once(self) -> None:
        bot = FakeBot({1: "one", 2: "two"})
        guild = FakeGuild(fetchable={1: "One"})

        memberships = await resolve_guild_memberships(
            cast(Any, bot),
            cast(Any, guild),
            [1, 2, 1],
        )

        assert memberships == {
            1: GuildMembership("One", True),
            2: GuildMembership("two", False),
        }
        # Repeated ids are deduplicated, so a roster costs one lookup each.
        assert guild.fetched == [1, 2]

    async def test_no_ids_makes_no_requests(self) -> None:
        bot = FakeBot()
        guild = FakeGuild()

        memberships = await resolve_guild_memberships(
            cast(Any, bot),
            cast(Any, guild),
            [],
        )

        assert memberships == {}
        assert guild.fetched == []


class TestSendInteractionNotice:
    async def test_answers_an_untouched_interaction_privately(self) -> None:
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                is_done=MagicMock(return_value=False),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await send_interaction_notice(cast(Any, interaction), "notice")

        interaction.response.send_message.assert_awaited_once_with(
            "notice",
            ephemeral=True,
        )
        interaction.followup.send.assert_not_awaited()

    async def test_follows_up_on_a_deferred_interaction(self) -> None:
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                is_done=MagicMock(return_value=True),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await send_interaction_notice(cast(Any, interaction), "notice")

        interaction.followup.send.assert_awaited_once_with(
            "notice",
            ephemeral=True,
        )
        interaction.response.send_message.assert_not_awaited()


class TestSendDirectMessage:
    async def test_delivers_the_message(self) -> None:
        bot = FakeBot()

        delivered = await send_direct_message(cast(Any, bot), 7, "hello")

        assert delivered
        bot.users[7].send.assert_awaited_once_with("hello")

    async def test_closed_dms_are_reported_rather_than_raised(self) -> None:
        # Blocking the bot or closing DMs raises Forbidden. That is an expected
        # outcome, so a bulk notification must be able to carry on past it.
        bot = FakeBot()
        bot.send_errors[7] = forbidden_error(50007)

        delivered = await send_direct_message(cast(Any, bot), 7, "hello")

        assert not delivered

    async def test_an_unfetchable_user_is_reported(self) -> None:
        bot = FakeBot()
        bot.fetch_errors[7] = not_found_error()

        delivered = await send_direct_message(cast(Any, bot), 7, "hello")

        assert not delivered

    async def test_logging_never_carries_the_message_body(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = FakeBot()
        bot.send_errors[2] = forbidden_error(50007)

        with caplog.at_level("DEBUG"):
            await send_direct_message(cast(Any, bot), 1, "SECRET BODY")
            await send_direct_message(cast(Any, bot), 2, "OTHER SECRET")

        assert "SECRET BODY" not in caplog.text
        assert "OTHER SECRET" not in caplog.text
        # The character count stands in for the body so a truncated or empty
        # message is still diagnosable.
        assert "characters=11" in caplog.text
        assert "Sending direct message" in caplog.text
        assert "Delivered direct message" in caplog.text
        assert "Could not deliver a direct message" in caplog.text
