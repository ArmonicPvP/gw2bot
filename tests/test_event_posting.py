import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.discord_utils import GuildMembership
from gw2bot.events import posting
from gw2bot.events.models import (
    CATEGORY_CAPACITIES,
    AutoSignupChoice,
    EventCategory,
    EventRole,
    EventSignup,
    EventStatus,
    RepeatFrequency,
    RoleChange,
    RosterCandidate,
    RosterUpdate,
    fitting_roles,
    is_roster_full,
    preferred_role_order,
    roster_feasible,
    seated_candidates,
    solve_roster,
)
from gw2bot.events.posting import (
    apply_auto_signups,
    apply_signup_edit,
    cancel_occurrence,
    post_pending_occurrence,
    complete_signup,
    delete_event_posts,
    departed_roster_members,
    disable_auto_signup,
    merge_roster_updates,
    occurrence_status,
    post_occurrence,
    prune_departed_signups,
    prune_superseded_occurrences,
    rebalance_occurrence_roster,
    refresh_occurrence_message,
    remove_signup,
    repost_occurrence,
)
from gw2bot.events.store import EventStore

from factories import forbidden_error, not_found_error

START = datetime(2027, 1, 30, 20, 0, tzinfo=UTC)
BEFORE_START = START - timedelta(hours=2)


class FakeThread:
    def __init__(self, thread_id: int = 777):
        self.id = thread_id
        self.add_user = AsyncMock()
        self.remove_user = AsyncMock()
        self.edit = AsyncMock()
        self.delete = AsyncMock()
        self.send = AsyncMock()


class FakeChannel:
    def __init__(self, channel_id: int = 1234, thread: FakeThread | None = None):
        self.id = channel_id
        self.type = discord.ChannelType.text
        self.thread = thread if thread is not None else FakeThread()
        self.sent: list[dict[str, Any]] = []
        self.partial_message = SimpleNamespace(
            edit=AsyncMock(),
            delete=AsyncMock(),
        )
        self.create_thread_error: Exception | None = None
        self.send_error: Exception | None = None

    async def send(self, *, embed: Any = None, view: Any = None) -> Any:
        if self.send_error is not None:
            error = self.send_error
            self.send_error = None
            raise error
        message = SimpleNamespace(
            id=555,
            create_thread=AsyncMock(return_value=self.thread),
            delete=AsyncMock(),
        )
        if self.create_thread_error is not None:
            message.create_thread = AsyncMock(
                side_effect=self.create_thread_error
            )
        self.sent.append({"embed": embed, "view": view, "message": message})
        return message

    def get_partial_message(self, message_id: int) -> Any:
        return self.partial_message


class FakeForumPost(FakeThread):
    """A forum post that already exists: the bot may only post messages in it."""

    def __init__(self, thread_id: int = 901, archived: bool = False):
        super().__init__(thread_id)
        self.type = discord.ChannelType.public_thread
        self.archived = archived
        self.sent: list[dict[str, Any]] = []
        self.send_error: Exception | None = None
        self.partial_message = SimpleNamespace(
            edit=AsyncMock(),
            delete=AsyncMock(),
        )
        self.edit = AsyncMock(side_effect=self._edit)
        self.send = AsyncMock(side_effect=self._send)

    async def _edit(self, **fields: Any) -> None:
        # Discord clears the archive flag as part of the edit.
        if "archived" in fields:
            self.archived = bool(fields["archived"])

    async def _send(self, content: Any = None, **fields: Any) -> Any:
        if self.send_error is not None:
            error = self.send_error
            self.send_error = None
            raise error
        # A message inside a thread cannot carry a thread of its own, so this
        # deliberately has no create_thread: calling it would raise.
        message = SimpleNamespace(id=606, delete=AsyncMock())
        self.sent.append({**fields, "content": content, "message": message})
        return message

    def get_partial_message(self, message_id: int) -> Any:
        return self.partial_message


def forum_post_bot(
    store: EventStore,
    post: FakeForumPost,
    channel: FakeChannel | None = None,
) -> Any:
    # The text channel comes along so a move between a channel and a post can be
    # exercised; the post is resolvable on its own, as its own channel.
    bot = cast(
        Any,
        FakeBot(store, channel if channel is not None else FakeChannel()),
    )
    bot._channels[post.id] = post
    return bot


class FakeUser:
    def __init__(self, user_id: int, display_name: str):
        self.id = user_id
        self.display_name = display_name
        self.send = AsyncMock()


class FakeBot:
    def __init__(self, store: EventStore, channel: FakeChannel):
        self.event_store = store
        self.event_timezone = ZoneInfo("UTC")
        self._channels: dict[int, Any] = {
            channel.id: channel,
            channel.thread.id: channel.thread,
        }
        # Users are created on demand and kept, so a test can read back the
        # direct messages the bot sent to any of them.
        self.users: dict[int, FakeUser] = {}
        self.fetch_user_errors: dict[int, Exception] = {}
        self.dm_errors: dict[int, Exception] = {}

    def get_guild(self, guild_id: int) -> Any:
        # The bot runs without the members intent, so a guild is never cached.
        return None

    async def fetch_user(self, user_id: int) -> Any:
        error = self.fetch_user_errors.get(user_id)
        if error is not None:
            raise error
        user = self.users.get(user_id)
        if user is None:
            user = FakeUser(user_id, f"User {user_id}")
            dm_error = self.dm_errors.get(user_id)
            if dm_error is not None:
                user.send = AsyncMock(side_effect=dm_error)
            self.users[user_id] = user
        return user

    def get_channel(self, channel_id: int) -> Any:
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> Any:
        # Discord raises NotFound for a channel that is gone, so an unknown id
        # must surface that rather than a KeyError.
        if channel_id not in self._channels:
            raise not_found_error()
        return self._channels[channel_id]


@pytest.fixture
def store(tmp_path: Path):
    store = EventStore(str(tmp_path / "gw2bot.db"))
    yield store
    store.close()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def bot(store: EventStore, channel: FakeChannel) -> Any:
    return cast(Any, FakeBot(store, channel))


def create_event(
    store: EventStore,
    category: EventCategory = EventCategory.FRACTAL,
    repeat_frequency: RepeatFrequency = RepeatFrequency.NONE,
    repeat_days: tuple[int, ...] = (),
    delete_previous_on_repeat: bool = False,
    channel_id: int = 1234,
):
    return store.create_event(
        category=category,
        title="Kitty Cleanup",
        description="Bring food.",
        channel_id=channel_id,
        leader_discord_id=42,
        start_time=START,
        duration_minutes=90,
        repeat_frequency=repeat_frequency,
        repeat_days=repeat_days,
        delete_previous_on_repeat=delete_previous_on_repeat,
    )


async def post_new_event(
    bot: Any,
    store: EventStore,
    category: EventCategory = EventCategory.FRACTAL,
    repeat_frequency: RepeatFrequency = RepeatFrequency.NONE,
    repeat_days: tuple[int, ...] = (),
):
    event = create_event(store, category, repeat_frequency, repeat_days)
    occurrence = store.create_occurrence(event.event_id, event.start_time)
    posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
    return event, posted


FRACTAL_CAPACITY = CATEGORY_CAPACITIES[EventCategory.FRACTAL]
RAID_CAPACITY = CATEGORY_CAPACITIES[EventCategory.RAID]


def make_candidate(
    user_id: int,
    role: EventRole,
    flex_roles: tuple[EventRole, ...] = (),
) -> RosterCandidate:
    return RosterCandidate(
        discord_user_id=user_id,
        preferences=preferred_role_order(role, flex_roles),
    )


def make_signup(
    user_id: int,
    role: EventRole | None,
    assigned_role: EventRole | None = None,
    flex_roles: tuple[EventRole, ...] = (),
    waitlisted: bool = False,
    signed_up_at: datetime = START,
) -> EventSignup:
    return EventSignup(
        occurrence_id=1,
        discord_user_id=user_id,
        role=role,
        assigned_role=assigned_role,
        flex_roles=flex_roles,
        signed_up_at=signed_up_at,
        waitlisted=waitlisted,
    )


class TestSolveRoster:
    def test_five_step_worked_example(self) -> None:
        # The reference scenario for the matchmaking behaviour: each new
        # signup may flex the earlier ones aside, and everyone lands a seat.
        flexer = make_candidate(
            11,
            EventRole.QUICKNESS_DPS,
            (
                EventRole.ALACRITY_HEAL,
                EventRole.QUICKNESS_HEAL,
                EventRole.DPS,
            ),
        )
        rigid_quickness = make_candidate(12, EventRole.QUICKNESS_DPS)
        alacrity_flex = make_candidate(
            13,
            EventRole.ALACRITY_DPS,
            (EventRole.DPS,),
        )
        rigid_heal = make_candidate(14, EventRole.ALACRITY_HEAL)
        rigid_dps = make_candidate(15, EventRole.DPS)

        assert solve_roster(FRACTAL_CAPACITY, [flexer]) == {
            11: EventRole.QUICKNESS_DPS,
        }
        assert solve_roster(FRACTAL_CAPACITY, [flexer, rigid_quickness]) == {
            11: EventRole.ALACRITY_HEAL,
            12: EventRole.QUICKNESS_DPS,
        }
        assert solve_roster(
            FRACTAL_CAPACITY,
            [flexer, rigid_quickness, alacrity_flex],
        ) == {
            # Seniority: the earlier flexer keeps the alacrity heal seat, so
            # the later alacrity DPS falls to their flex instead.
            11: EventRole.ALACRITY_HEAL,
            12: EventRole.QUICKNESS_DPS,
            13: EventRole.DPS,
        }
        assert solve_roster(
            FRACTAL_CAPACITY,
            [flexer, rigid_quickness, alacrity_flex, rigid_heal],
        ) == {
            11: EventRole.DPS,
            12: EventRole.QUICKNESS_DPS,
            13: EventRole.DPS,
            14: EventRole.ALACRITY_HEAL,
        }
        assert solve_roster(
            FRACTAL_CAPACITY,
            [
                flexer,
                rigid_quickness,
                alacrity_flex,
                rigid_heal,
                rigid_dps,
            ],
        ) == {
            11: EventRole.DPS,
            12: EventRole.QUICKNESS_DPS,
            13: EventRole.DPS,
            14: EventRole.ALACRITY_HEAL,
            15: EventRole.DPS,
        }

    def test_flex_input_order_is_ignored(self) -> None:
        # Discord's multi-select does not reliably preserve click order, so
        # every ordering of the same flex set must produce one preference
        # tuple.
        orderings = [
            (
                EventRole.DPS,
                EventRole.QUICKNESS_HEAL,
                EventRole.ALACRITY_HEAL,
            ),
            (
                EventRole.ALACRITY_HEAL,
                EventRole.DPS,
                EventRole.QUICKNESS_HEAL,
            ),
            (
                EventRole.QUICKNESS_HEAL,
                EventRole.ALACRITY_HEAL,
                EventRole.DPS,
            ),
        ]
        expected = (
            EventRole.QUICKNESS_DPS,
            EventRole.QUICKNESS_HEAL,
            EventRole.ALACRITY_HEAL,
            EventRole.DPS,
        )
        for flexes in orderings:
            assert (
                preferred_role_order(EventRole.QUICKNESS_DPS, flexes)
                == expected
            )

    def test_scarcity_places_flexers_in_specialised_seats_first(self) -> None:
        # With quickness saturated, the flexer's fallback is chosen by
        # scarcity tier: the boon-heal seat wins over the boon-DPS seat even
        # though Alacrity DPS is declared earlier in the enum.
        solution = solve_roster(
            RAID_CAPACITY,
            [
                make_candidate(1, EventRole.QUICKNESS_DPS),
                make_candidate(2, EventRole.QUICKNESS_DPS),
                make_candidate(
                    3,
                    EventRole.QUICKNESS_DPS,
                    (
                        EventRole.ALACRITY_DPS,
                        EventRole.ALACRITY_HEAL,
                        EventRole.DPS,
                    ),
                ),
            ],
        )
        assert solution is not None
        assert solution[3] is EventRole.ALACRITY_HEAL

    def test_primary_outranks_all_flexes(self) -> None:
        # A scarcer flex never beats the role the user actually asked for.
        solution = solve_roster(
            FRACTAL_CAPACITY,
            [
                make_candidate(
                    11,
                    EventRole.DPS,
                    (EventRole.ALACRITY_HEAL, EventRole.QUICKNESS_HEAL),
                ),
            ],
        )
        assert solution == {11: EventRole.DPS}

    def test_infeasible_set_returns_none(self) -> None:
        rigid_heals = [
            make_candidate(11, EventRole.QUICKNESS_HEAL),
            make_candidate(12, EventRole.QUICKNESS_HEAL),
        ]
        assert not roster_feasible(
            FRACTAL_CAPACITY,
            [candidate.preferences for candidate in rigid_heals],
        )
        assert solve_roster(FRACTAL_CAPACITY, rigid_heals) is None

    def test_equal_timestamps_break_ties_by_user_id(self) -> None:
        larger_id = make_signup(20, EventRole.DPS, EventRole.DPS)
        smaller_id = make_signup(10, EventRole.DPS, EventRole.DPS)

        candidates = seated_candidates([larger_id, smaller_id])

        assert [
            candidate.discord_user_id for candidate in candidates
        ] == [10, 20]

    def test_seated_signup_without_a_role_is_rigid_dps(self) -> None:
        # Corrupt or legacy data (a category change that was never
        # rebalanced) must still occupy capacity rather than vanish from the
        # counts.
        legacy = make_signup(
            5,
            None,
            None,
            flex_roles=(EventRole.QUICKNESS_HEAL,),
        )
        assert seated_candidates([legacy])[0].preferences == (EventRole.DPS,)


class TestPostOccurrence:
    async def test_posts_message_with_thread_and_stores_ids(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, posted = await post_new_event(bot, store)

        assert posted.message_id == 555
        assert posted.thread_id == 777
        assert posted.status is EventStatus.OPEN
        # The channel is stored with the message, so a later edit or delete can
        # address it even after the event's channel has moved on.
        assert posted.channel_id == 1234
        assert len(channel.sent) == 1
        embed = channel.sent[0]["embed"]
        assert embed.footer.text == f"eventID: {event.event_id}"
        view = channel.sent[0]["view"]
        custom_ids = {
            item.item.custom_id
            for item in view.children
            if isinstance(item, discord.ui.DynamicItem)
        }
        occurrence_id = posted.occurrence_id
        assert custom_ids == {
            f"gw2bot:event-signup:{occurrence_id}",
            f"gw2bot:event-signout:{occurrence_id}",
            f"gw2bot:event-settings:{occurrence_id}",
        }

    async def test_thread_creation_failure_still_posts_the_event(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        channel.create_thread_error = forbidden_error(50001)

        event, posted = await post_new_event(bot, store)

        assert posted.message_id == 555
        assert posted.thread_id is None

    async def test_a_row_deleted_mid_post_deletes_the_orphaned_message(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = create_event(store, repeat_frequency=RepeatFrequency.DAILY)
        occurrence = store.create_occurrence(event.event_id, event.start_time)

        async def cancel_it_mid_flight(**kwargs: Any) -> Any:
            # Someone deletes or cancels the run while the message is being
            # sent, so the row this post belongs to is gone by the time it
            # comes to be recorded.
            message = await FakeChannel.send(channel, **kwargs)
            store.delete_occurrence(occurrence.occurrence_id)
            return message

        channel.send = cancel_it_mid_flight  # type: ignore[method-assign]

        with pytest.raises(ValueError):
            await post_occurrence(bot, event, occurrence, BEFORE_START)

        # Nothing owns the message that was just sent, so leaving it in the
        # channel would strand a post whose buttons point at a run that no
        # longer exists.
        channel.sent[-1]["message"].delete.assert_awaited_once()
        channel.thread.delete.assert_awaited_once()

    async def test_persistence_failure_deletes_the_orphaned_message(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        store.set_occurrence_message = MagicMock(  # type: ignore[method-assign]
            side_effect=SQLAlchemyError("database is locked")
        )

        with pytest.raises(SQLAlchemyError):
            await post_occurrence(bot, event, occurrence, BEFORE_START)

        # The sent message must be removed so it is not left orphaned, and the
        # occurrence must still look unposted so a retry can re-send cleanly
        # instead of the scheduler adding a duplicate public message. Its
        # thread does not disappear on its own, so it must be deleted too.
        channel.sent[-1]["message"].delete.assert_awaited_once()
        channel.thread.delete.assert_awaited_once()
        stored = store.get_occurrence(occurrence.occurrence_id)
        assert stored is not None
        assert stored.message_id is None
        assert stored.occurrence_id in {
            entry.occurrence_id
            for entry in store.get_unposted_occurrences()
        }


class TestPostingIntoAnExistingForumPost:
    async def post_event_in_post(
        self,
        bot: Any,
        store: EventStore,
        post: FakeForumPost,
    ) -> Any:
        event = create_event(store, channel_id=post.id)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        return event, posted

    async def test_sends_the_event_into_the_post_without_creating_one(
        self,
        store: EventStore,
    ) -> None:
        post = FakeForumPost()
        bot = forum_post_bot(store, post)

        event, posted = await self.post_event_in_post(bot, store, post)

        assert len(post.sent) == 1
        sent = post.sent[0]
        assert sent["embed"].footer.text == f"eventID: {event.event_id}"
        assert sent["view"] is not None
        # The message lives in the post, so the post is both the stored channel
        # (what later edits and deletes address) and the stored thread (where the
        # roster is announced). No thread is opened for the event.
        assert posted.message_id == 606
        assert posted.channel_id == post.id
        assert posted.thread_id == post.id
        assert posted.status is EventStatus.OPEN

    async def test_reopens_an_archived_post_before_sending(
        self,
        store: EventStore,
    ) -> None:
        post = FakeForumPost(archived=True)
        bot = forum_post_bot(store, post)

        await self.post_event_in_post(bot, store, post)

        # Discord refuses messages in an archived post, so a dormant one is
        # reopened rather than the event failing to post.
        assert not post.archived
        assert len(post.sent) == 1

    async def test_a_refused_send_leaves_the_occurrence_unposted(
        self,
        store: EventStore,
    ) -> None:
        post = FakeForumPost()
        post.send_error = forbidden_error(50013)
        bot = forum_post_bot(store, post)
        event = create_event(store, channel_id=post.id)
        occurrence = store.create_occurrence(event.event_id, event.start_time)

        with pytest.raises(discord.HTTPException):
            await post_occurrence(bot, event, occurrence, BEFORE_START)

        stored = store.get_occurrence(occurrence.occurrence_id)
        assert stored is not None
        assert stored.message_id is None
        assert stored.occurrence_id in {
            entry.occurrence_id
            for entry in store.get_unposted_occurrences()
        }

    async def test_persistence_failure_deletes_only_the_message(
        self,
        store: EventStore,
    ) -> None:
        post = FakeForumPost()
        bot = forum_post_bot(store, post)
        event = create_event(store, channel_id=post.id)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        store.set_occurrence_message = MagicMock(  # type: ignore[method-assign]
            side_effect=SQLAlchemyError("database is locked")
        )

        with pytest.raises(SQLAlchemyError):
            await post_occurrence(bot, event, occurrence, BEFORE_START)

        # The orphaned message goes, but the post it was sent into is not the
        # bot's to delete.
        post.sent[-1]["message"].delete.assert_awaited_once()
        post.delete.assert_not_awaited()

    async def test_refresh_edits_the_message_and_never_renames_the_post(
        self,
        store: EventStore,
    ) -> None:
        post = FakeForumPost()
        bot = forum_post_bot(store, post)
        event, posted = await self.post_event_in_post(bot, store, post)

        status = await refresh_occurrence_message(
            bot,
            event,
            posted,
            START + timedelta(hours=3),
        )

        # The post's name describes whatever the post is for, not this event's
        # status, so it is left alone - and the status still commits.
        assert status is EventStatus.OVER
        post.partial_message.edit.assert_awaited_once()
        post.edit.assert_not_awaited()
        cleared = store.get_occurrence(posted.occurrence_id)
        assert cleared is not None
        assert not cleared.needs_refresh

    async def test_refresh_reopens_an_archived_post(
        self,
        store: EventStore,
    ) -> None:
        post = FakeForumPost()
        bot = forum_post_bot(store, post)
        event, posted = await self.post_event_in_post(bot, store, post)
        post.archived = True

        status = await refresh_occurrence_message(
            bot,
            event,
            posted,
            START + timedelta(hours=3),
        )

        # Discord refuses edits inside an archived post, so it is reopened
        # before the embed edit rather than after it.
        edits = [call.kwargs for call in post.edit.await_args_list]
        assert edits[0]["archived"] is False
        assert not post.archived
        post.partial_message.edit.assert_awaited_once()
        assert status is EventStatus.OVER

    async def test_signup_joins_the_post_and_announces_the_roster_in_it(
        self,
        store: EventStore,
    ) -> None:
        post = FakeForumPost()
        bot = forum_post_bot(store, post)
        event, posted = await self.post_event_in_post(bot, store, post)

        await complete_signup(
            bot,
            event,
            posted,
            11,
            EventRole.QUICKNESS_DPS,
            (EventRole.ALACRITY_HEAL,),
        )
        # Seating the second member flexes the first, which is announced where
        # the event lives: the post stands in for the signup thread.
        await complete_signup(
            bot,
            event,
            posted,
            12,
            EventRole.QUICKNESS_DPS,
            (),
        )
        await remove_signup(bot, event, posted, 12)

        assert post.add_user.await_count == 2
        announcements = [
            entry["content"] for entry in post.sent if entry["content"]
        ]
        assert len(announcements) == 2
        assert "<@11>" in announcements[0]
        assert post.partial_message.edit.await_count == 3

    async def test_signing_out_leaves_the_shared_post_membership_alone(
        self,
        store: EventStore,
    ) -> None:
        post = FakeForumPost()
        bot = forum_post_bot(store, post)
        first_event, first = await self.post_event_in_post(bot, store, post)
        second_event, second = await self.post_event_in_post(bot, store, post)
        await complete_signup(bot, first_event, first, 11, EventRole.DPS, ())
        await complete_signup(bot, second_event, second, 11, EventRole.DPS, ())

        await remove_signup(bot, first_event, first, 11)

        # Both events live in the same post, so its members are not either
        # event's roster. Removing the member here would drop them from the
        # event they are still signed up to - and in a private thread, take away
        # their access to it.
        post.remove_user.assert_not_awaited()
        assert post.add_user.await_count == 2
        assert store.get_signups(second.occurrence_id)[0].discord_user_id == 11

    async def test_signing_out_still_leaves_a_signup_thread(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # The thread under a text-channel message belongs to the event, so the
        # roster and the thread membership stay in step there.
        event, posted = await post_new_event(bot, store)
        await complete_signup(bot, event, posted, 11, EventRole.DPS, ())

        await remove_signup(bot, event, posted, 11)

        channel.thread.remove_user.assert_awaited_once()

    async def test_signup_reopens_an_archived_post_before_using_it(
        self,
        store: EventStore,
    ) -> None:
        post = FakeForumPost()
        bot = forum_post_bot(store, post)
        event, posted = await self.post_event_in_post(bot, store, post)
        post.archived = True

        await complete_signup(bot, event, posted, 11, EventRole.DPS, ())

        assert not post.archived
        post.add_user.assert_awaited_once()

    async def test_delete_removes_the_message_but_keeps_the_post(
        self,
        store: EventStore,
    ) -> None:
        post = FakeForumPost()
        bot = forum_post_bot(store, post)
        event, posted = await self.post_event_in_post(bot, store, post)

        deleted = await delete_event_posts(bot, event, [posted])

        # Deleting the post would take everything else in it - and the forum
        # entry itself - along with the event.
        assert deleted == 1
        post.partial_message.delete.assert_awaited_once()
        post.delete.assert_not_awaited()

    async def test_moving_into_a_post_deletes_the_channel_message_and_thread(
        self,
        store: EventStore,
    ) -> None:
        channel = FakeChannel()
        post = FakeForumPost()
        bot = forum_post_bot(store, post, channel)
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        store.add_signup(
            occurrence_id=posted.occurrence_id,
            discord_user_id=11,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )
        moved = store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=post.id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )

        reposted = await repost_occurrence(bot, moved, posted)

        assert len(post.sent) == 1
        assert reposted.channel_id == post.id
        assert reposted.thread_id == post.id
        # The signup thread the bot opened in the channel is its own to remove.
        channel.partial_message.delete.assert_awaited_once()
        channel.thread.delete.assert_awaited_once()
        post.add_user.assert_awaited_once()

    async def test_moving_out_of_a_post_leaves_the_post_standing(
        self,
        store: EventStore,
    ) -> None:
        channel = FakeChannel()
        post = FakeForumPost()
        bot = forum_post_bot(store, post, channel)
        event = create_event(store, channel_id=post.id)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        moved = store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=channel.id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )

        reposted = await repost_occurrence(bot, moved, posted)

        assert len(channel.sent) == 1
        assert reposted.channel_id == channel.id
        assert reposted.thread_id == channel.thread.id
        post.partial_message.delete.assert_awaited_once()
        post.delete.assert_not_awaited()


class TestCompleteSignup:
    async def test_assigns_role_updates_thread_and_message(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)

        signup = await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_HEAL,
            (EventRole.DPS,),
        )

        assert not signup.waitlisted
        assert signup.assigned_role is EventRole.QUICKNESS_HEAL
        channel.thread.add_user.assert_awaited_once()
        channel.partial_message.edit.assert_awaited()

    async def test_boon_capacity_forces_flex_or_waitlist(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_DPS,
            (),
        )

        # Quickness is full (1/1 for fractals); the flex role is used.
        flexed = await complete_signup(
            bot,
            event,
            occurrence,
            12,
            EventRole.QUICKNESS_HEAL,
            (EventRole.ALACRITY_HEAL,),
        )
        assert flexed.assigned_role is EventRole.ALACRITY_HEAL

        # Quickness and healers are both full; no flex fits, so waitlist.
        waitlisted = await complete_signup(
            bot,
            event,
            occurrence,
            13,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        assert waitlisted.waitlisted
        assert waitlisted.assigned_role is None

    async def test_wvw_signs_up_without_roles_and_waitlists_beyond_capacity(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(
            bot,
            store,
            category=EventCategory.WVW,
        )
        for user_id in range(1, 51):
            signup = await complete_signup(
                bot,
                event,
                occurrence,
                user_id,
                None,
                (),
            )
            assert not signup.waitlisted

        overflow = await complete_signup(bot, event, occurrence, 51, None, ())

        assert overflow.waitlisted

    async def test_open_world_signs_up_without_roles_like_wvw(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(
            bot,
            store,
            category=EventCategory.OPEN_WORLD,
        )
        for user_id in range(1, 51):
            signup = await complete_signup(
                bot,
                event,
                occurrence,
                user_id,
                None,
                (),
            )
            assert not signup.waitlisted
            assert signup.assigned_role is None

        overflow = await complete_signup(bot, event, occurrence, 51, None, ())

        assert overflow.waitlisted

    async def test_instanced_event_requires_a_role(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)

        with pytest.raises(ValueError, match="requires picking a role"):
            await complete_signup(bot, event, occurrence, 11, None, ())

    async def test_signup_after_event_ends_is_rejected(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        after_end = START + timedelta(hours=3)

        # A view left open until after the event ends must not be able to
        # mutate the historical roster on a late click.
        with pytest.raises(ValueError, match="already ended"):
            await complete_signup(
                bot,
                event,
                occurrence,
                11,
                EventRole.DPS,
                (),
                now=after_end,
            )

        assert store.get_signups(occurrence.occurrence_id) == []
        channel.thread.add_user.assert_not_awaited()

    async def test_full_event_status_becomes_full(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        roster = [
            (11, EventRole.QUICKNESS_HEAL),
            (12, EventRole.ALACRITY_DPS),
            (13, EventRole.DPS),
            (14, EventRole.DPS),
            (15, EventRole.DPS),
        ]
        for user_id, role in roster:
            await complete_signup(bot, event, occurrence, user_id, role, ())

        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        signups = store.get_signups(occurrence.occurrence_id)
        assert occurrence_status(
            event,
            updated,
            signups,
            BEFORE_START,
        ) is EventStatus.FULL


class TestRemoveSignup:
    async def test_removes_signup_and_promotes_fitting_waitlisted_user(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        # Healer slot is full, so this signup lands on the waitlist.
        waitlisted = await complete_signup(
            bot,
            event,
            occurrence,
            12,
            EventRole.ALACRITY_HEAL,
            (),
        )
        assert waitlisted.waitlisted

        removed, update = await remove_signup(bot, event, occurrence, 11)

        assert removed is not None
        assert len(update.promoted) == 1
        promoted = update.promoted[0]
        assert promoted.discord_user_id == 12
        assert not promoted.waitlisted
        assert promoted.assigned_role is EventRole.ALACRITY_HEAL
        stored = store.get_signup(occurrence.occurrence_id, 12)
        assert stored is not None
        assert not stored.waitlisted
        assert stored.assigned_role is EventRole.ALACRITY_HEAL
        channel.thread.remove_user.assert_awaited_once()

    async def test_promotion_respects_boon_capacity(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        for user_id, role in (
            (12, EventRole.ALACRITY_DPS),
            (13, EventRole.DPS),
            (14, EventRole.DPS),
            (15, EventRole.DPS),
        ):
            await complete_signup(bot, event, occurrence, user_id, role, ())
        # Quickness provider exists, so a quickness-only candidate cannot
        # be promoted into the freed pure-DPS slot.
        quickness_candidate = await complete_signup(
            bot,
            event,
            occurrence,
            16,
            EventRole.QUICKNESS_DPS,
            (),
        )
        dps_candidate = await complete_signup(
            bot,
            event,
            occurrence,
            17,
            EventRole.DPS,
            (),
        )
        assert quickness_candidate.waitlisted
        assert dps_candidate.waitlisted

        removed, update = await remove_signup(bot, event, occurrence, 13)

        assert removed is not None
        # The earlier waitlisted quickness candidate is skipped, not blocking:
        # promotion goes to the first waitlisted member who actually fits.
        assert [
            signup.discord_user_id for signup in update.promoted
        ] == [17]
        still_waitlisted = store.get_signup(occurrence.occurrence_id, 16)
        assert still_waitlisted is not None
        assert still_waitlisted.waitlisted

    async def test_removing_unknown_signup_returns_none(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)

        removed, update = await remove_signup(bot, event, occurrence, 99)

        assert removed is None
        assert not update.has_changes


class TestCompleteSignupReshuffle:
    async def test_five_step_scenario_reshuffles_seated_flexers(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)

        def seats() -> dict[int, EventRole | None]:
            return {
                signup.discord_user_id: signup.assigned_role
                for signup in store.get_signups(occurrence.occurrence_id)
                if not signup.waitlisted
            }

        first = await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_DPS,
            (
                EventRole.ALACRITY_HEAL,
                EventRole.QUICKNESS_HEAL,
                EventRole.DPS,
            ),
        )
        assert first.assigned_role is EventRole.QUICKNESS_DPS

        # A rigid Quickness DPS arrives: instead of waitlisting them, the
        # seated flexer is moved aside into the alacrity heal seat.
        second = await complete_signup(
            bot,
            event,
            occurrence,
            12,
            EventRole.QUICKNESS_DPS,
            (),
        )
        assert second.assigned_role is EventRole.QUICKNESS_DPS
        assert seats()[11] is EventRole.ALACRITY_HEAL

        # Seniority: the earlier flexer keeps the alacrity seat, so the later
        # Alacrity DPS lands on their flex instead.
        third = await complete_signup(
            bot,
            event,
            occurrence,
            13,
            EventRole.ALACRITY_DPS,
            (EventRole.DPS,),
        )
        assert third.assigned_role is EventRole.DPS
        assert seats()[11] is EventRole.ALACRITY_HEAL

        # A rigid Alacrity Heal claims the heal seat; the flexer moves again.
        fourth = await complete_signup(
            bot,
            event,
            occurrence,
            14,
            EventRole.ALACRITY_HEAL,
            (),
        )
        assert fourth.assigned_role is EventRole.ALACRITY_HEAL
        assert seats()[11] is EventRole.DPS

        fifth = await complete_signup(
            bot,
            event,
            occurrence,
            15,
            EventRole.DPS,
            (),
        )
        assert fifth.assigned_role is EventRole.DPS

        signups = store.get_signups(occurrence.occurrence_id)
        assert not any(signup.waitlisted for signup in signups)
        assert is_roster_full(event.capacity, signups)
        assert seats() == {
            11: EventRole.DPS,
            12: EventRole.QUICKNESS_DPS,
            13: EventRole.DPS,
            14: EventRole.ALACRITY_HEAL,
            15: EventRole.DPS,
        }
        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        assert (
            occurrence_status(event, updated, signups, BEFORE_START)
            is EventStatus.FULL
        )

    async def test_reshuffle_sends_one_batched_thread_ping(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_DPS,
            (EventRole.ALACRITY_HEAL,),
        )
        channel.thread.send.assert_not_awaited()

        await complete_signup(
            bot,
            event,
            occurrence,
            12,
            EventRole.QUICKNESS_DPS,
            (),
        )

        channel.thread.send.assert_awaited_once()
        send = channel.thread.send.await_args
        assert send is not None
        content = send.args[0]
        assert "<@11>" in content
        assert EventRole.QUICKNESS_DPS.value in content
        assert EventRole.ALACRITY_HEAL.value in content
        # The newcomer learns their seat from the signup summary; only the
        # members who were moved are pinged.
        assert "<@12>" not in content

    async def test_signup_that_would_unseat_someone_is_waitlisted(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        seated = await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        assert not seated.waitlisted

        overflow = await complete_signup(
            bot,
            event,
            occurrence,
            12,
            EventRole.ALACRITY_HEAL,
            (),
        )

        # The seated rigid healer is never unseated for a better fit; the
        # newcomer waits instead and nothing about the roster is announced.
        assert overflow.waitlisted
        assert overflow.assigned_role is None
        stored = store.get_signup(occurrence.occurrence_id, 11)
        assert stored is not None
        assert not stored.waitlisted
        assert stored.assigned_role is EventRole.QUICKNESS_HEAL
        channel.thread.send.assert_not_awaited()

    async def test_ping_failure_does_not_fail_the_signup(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        channel.thread.send = AsyncMock(side_effect=forbidden_error(50001))
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_DPS,
            (EventRole.DPS,),
        )

        signup = await complete_signup(
            bot,
            event,
            occurrence,
            12,
            EventRole.QUICKNESS_DPS,
            (),
        )

        assert not signup.waitlisted
        moved = store.get_signup(occurrence.occurrence_id, 11)
        assert moved is not None
        assert moved.assigned_role is EventRole.DPS
        # The public embed refresh still runs after the failed ping.
        channel.partial_message.edit.assert_awaited()


class TestPruneDepartedSignups:
    async def seat_pair(self, bot: Any, store: EventStore) -> Any:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot, event, occurrence, 11, EventRole.QUICKNESS_HEAL, ()
        )
        await complete_signup(bot, event, occurrence, 12, EventRole.DPS, ())
        return event, occurrence

    def test_only_a_confirmed_departure_counts(self) -> None:
        signups = [
            make_signup(11, EventRole.DPS),
            make_signup(12, EventRole.DPS),
            make_signup(13, EventRole.DPS),
        ]
        memberships = {
            11: GuildMembership("Present", True),
            12: GuildMembership("Gone", False),
            # Discord could not be reached for this one, which proves nothing.
            13: GuildMembership(None, None),
        }

        assert departed_roster_members(signups, memberships) == [12]

    def test_a_member_with_no_lookup_at_all_keeps_their_seat(self) -> None:
        signups = [make_signup(11, EventRole.DPS)]

        assert departed_roster_members(signups, {}) == []

    async def test_removes_departed_members_and_promotes_the_waitlist(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot, event, occurrence, 11, EventRole.QUICKNESS_HEAL, ()
        )
        waitlisted = await complete_signup(
            bot, event, occurrence, 12, EventRole.ALACRITY_HEAL, ()
        )
        assert waitlisted.waitlisted

        removed, update = await prune_departed_signups(
            bot,
            event,
            occurrence,
            {
                11: GuildMembership("Gone", False),
                12: GuildMembership("Present", True),
            },
        )

        assert removed == [11]
        # The freed seat goes to the waitlist exactly as a leader's own
        # removal would hand it over.
        assert [signup.discord_user_id for signup in update.promoted] == [12]
        assert store.get_signup(occurrence.occurrence_id, 11) is None
        promoted = store.get_signup(occurrence.occurrence_id, 12)
        assert promoted is not None
        assert not promoted.waitlisted

    async def test_keeps_members_whose_lookup_failed(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await self.seat_pair(bot, store)

        removed, update = await prune_departed_signups(
            bot,
            event,
            occurrence,
            {
                11: GuildMembership(None, None),
                12: GuildMembership("Present", True),
            },
        )

        assert removed == []
        assert not update.has_changes
        assert len(store.get_signups(occurrence.occurrence_id)) == 2

    async def test_disables_auto_signup_for_a_departed_member(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        # Left alone, automatic sign-up would seat them again on the series'
        # next occurrence, undoing the prune every week.
        event, occurrence = await post_new_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        await complete_signup(bot, event, occurrence, 11, EventRole.DPS, ())
        store.set_auto_signup(
            event.event_id,
            11,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (),
        )

        removed, _ = await prune_departed_signups(
            bot,
            event,
            occurrence,
            {11: GuildMembership("Gone", False)},
        )

        assert removed == [11]
        auto = store.get_auto_signup(event.event_id, 11)
        assert auto is not None
        assert auto.choice is AutoSignupChoice.NO

    async def test_stops_when_the_event_ends_partway_through(
        self,
        bot: Any,
        store: EventStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Each removal awaits Discord I/O, so a long roster of departures can
        # cross the event's end mid-loop. Everything after that point would be
        # a removal - and a waitlist promotion - landing on a finished roster.
        event, occurrence = await post_new_event(bot, store)
        for user_id in (11, 12, 13):
            await complete_signup(
                bot, event, occurrence, user_id, EventRole.DPS, ()
            )
        real_remove = posting.remove_signup
        ended = False

        async def remove_then_end(*args: Any, **kwargs: Any) -> Any:
            nonlocal ended
            result = await real_remove(*args, **kwargs)
            if not ended:
                # The first removal is the one the clock catches up with.
                ended = True
                store.set_occurrence_start_time(
                    occurrence.occurrence_id,
                    datetime.now(UTC)
                    - timedelta(minutes=event.duration_minutes + 1),
                )
            return result

        monkeypatch.setattr(posting, "remove_signup", remove_then_end)

        removed, _ = await prune_departed_signups(
            bot,
            event,
            occurrence,
            {
                user_id: GuildMembership(f"Gone {user_id}", False)
                for user_id in (11, 12, 13)
            },
        )

        # Only the removal that ran before the event ended went through; the
        # rest keep their rows for the next edit to deal with.
        assert removed == [11]
        remaining = {
            signup.discord_user_id
            for signup in store.get_signups(occurrence.occurrence_id)
        }
        assert remaining == {12, 13}

    async def test_stops_when_the_occurrence_is_retired_partway_through(
        self,
        bot: Any,
        store: EventStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The same guard covers the row disappearing outright - a deleted event
        # or a pruned superseded occurrence - rather than only the clock.
        event, occurrence = await post_new_event(bot, store)
        for user_id in (11, 12):
            await complete_signup(
                bot, event, occurrence, user_id, EventRole.DPS, ()
            )
        real_remove = posting.remove_signup
        dropped = False

        async def remove_then_delete(*args: Any, **kwargs: Any) -> Any:
            nonlocal dropped
            result = await real_remove(*args, **kwargs)
            if not dropped:
                dropped = True
                store.delete_event(event.event_id)
            return result

        monkeypatch.setattr(posting, "remove_signup", remove_then_delete)

        removed, _ = await prune_departed_signups(
            bot,
            event,
            occurrence,
            {
                11: GuildMembership("Gone", False),
                12: GuildMembership("Gone too", False),
            },
        )

        assert removed == [11]

    async def test_leaves_a_finished_occurrence_alone(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        # A finished roster is history: removing from it would promote someone
        # into a run that is already over, and the refresh behind that can
        # persist OVER without seeding the series' next occurrence.
        event, occurrence = await self.seat_pair(bot, store)
        store.set_occurrence_start_time(
            occurrence.occurrence_id,
            datetime.now(UTC) - timedelta(minutes=event.duration_minutes + 1),
        )
        finished = store.get_occurrence(occurrence.occurrence_id)
        assert finished is not None

        removed, _ = await prune_departed_signups(
            bot,
            event,
            finished,
            {11: GuildMembership("Gone", False)},
        )

        assert removed == []
        assert store.get_signup(finished.occurrence_id, 11) is not None


class TestRemoveSignupResettle:
    async def test_flexed_member_snaps_back_to_primary_on_departure(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_DPS,
            (),
        )
        flexed = await complete_signup(
            bot,
            event,
            occurrence,
            12,
            EventRole.QUICKNESS_DPS,
            (EventRole.ALACRITY_DPS,),
        )
        assert flexed.assigned_role is EventRole.ALACRITY_DPS

        removed, update = await remove_signup(bot, event, occurrence, 11)

        assert removed is not None
        assert update.reassigned == (
            RoleChange(
                discord_user_id=12,
                old_role=EventRole.ALACRITY_DPS,
                new_role=EventRole.QUICKNESS_DPS,
            ),
        )
        stored = store.get_signup(occurrence.occurrence_id, 12)
        assert stored is not None
        assert stored.assigned_role is EventRole.QUICKNESS_DPS
        channel.thread.send.assert_awaited_once()
        send = channel.thread.send.await_args
        assert send is not None
        assert "<@12>" in send.args[0]

    async def test_promotion_can_reshuffle_seated_flexers(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        for user_id, role, flex_roles in (
            (11, EventRole.ALACRITY_HEAL, ()),
            (12, EventRole.QUICKNESS_DPS, (EventRole.DPS,)),
            (13, EventRole.DPS, ()),
            (14, EventRole.DPS, ()),
        ):
            await complete_signup(
                bot,
                event,
                occurrence,
                user_id,
                role,
                flex_roles,
            )
        waiting = await complete_signup(
            bot,
            event,
            occurrence,
            15,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        assert waiting.waitlisted

        removed, update = await remove_signup(bot, event, occurrence, 11)

        # The rigid Quickness Heal only fits once the seated quickness flexer
        # moves to plain DPS; one departure produces both a promotion and a
        # reassignment, announced together.
        assert removed is not None
        assert [
            signup.discord_user_id for signup in update.promoted
        ] == [15]
        assert update.promoted[0].assigned_role is EventRole.QUICKNESS_HEAL
        assert update.reassigned == (
            RoleChange(
                discord_user_id=12,
                old_role=EventRole.QUICKNESS_DPS,
                new_role=EventRole.DPS,
            ),
        )
        channel.thread.send.assert_awaited_once()
        send = channel.thread.send.await_args
        assert send is not None
        assert "<@15>" in send.args[0]
        assert "<@12>" in send.args[0]

    async def test_single_departure_promotes_multiple_waitlisted(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        for user_id, role, flex_roles in (
            (11, EventRole.QUICKNESS_HEAL, ()),
            (12, EventRole.ALACRITY_DPS, (EventRole.DPS,)),
            (13, EventRole.DPS, ()),
            (14, EventRole.DPS, ()),
        ):
            await complete_signup(
                bot,
                event,
                occurrence,
                user_id,
                role,
                flex_roles,
            )
        first_waiting = await complete_signup(
            bot,
            event,
            occurrence,
            15,
            EventRole.QUICKNESS_DPS,
            (),
        )
        second_waiting = await complete_signup(
            bot,
            event,
            occurrence,
            16,
            EventRole.ALACRITY_HEAL,
            (),
        )
        assert first_waiting.waitlisted
        assert second_waiting.waitlisted

        removed, update = await remove_signup(bot, event, occurrence, 11)

        assert removed is not None
        assert [
            signup.discord_user_id for signup in update.promoted
        ] == [15, 16]
        signups = store.get_signups(occurrence.occurrence_id)
        assert not any(signup.waitlisted for signup in signups)
        assert is_roster_full(event.capacity, signups)
        seats = {
            signup.discord_user_id: signup.assigned_role
            for signup in signups
        }
        assert seats == {
            12: EventRole.DPS,
            13: EventRole.DPS,
            14: EventRole.DPS,
            15: EventRole.QUICKNESS_DPS,
            16: EventRole.ALACRITY_HEAL,
        }

    async def test_removing_waitlisted_member_leaves_roster_untouched(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        waiting = await complete_signup(
            bot,
            event,
            occurrence,
            12,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        assert waiting.waitlisted

        removed, update = await remove_signup(bot, event, occurrence, 12)

        assert removed is not None
        assert not update.has_changes
        stored = store.get_signup(occurrence.occurrence_id, 11)
        assert stored is not None
        assert stored.assigned_role is EventRole.QUICKNESS_HEAL
        channel.thread.send.assert_not_awaited()

    async def test_ping_failure_does_not_fail_the_removal(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        channel.thread.send = AsyncMock(side_effect=forbidden_error(50001))
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_DPS,
            (),
        )
        await complete_signup(
            bot,
            event,
            occurrence,
            12,
            EventRole.QUICKNESS_DPS,
            (EventRole.ALACRITY_DPS,),
        )

        removed, update = await remove_signup(bot, event, occurrence, 11)

        assert removed is not None
        assert len(update.reassigned) == 1
        stored = store.get_signup(occurrence.occurrence_id, 12)
        assert stored is not None
        assert stored.assigned_role is EventRole.QUICKNESS_DPS


class TestApplySignupEdit:
    async def test_seated_edit_keeps_seat_and_signup_time(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        original = await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_DPS,
            (),
        )

        result = await apply_signup_edit(
            bot,
            event,
            occurrence,
            11,
            EventRole.ALACRITY_DPS,
            (EventRole.DPS,),
        )

        assert not result.needs_waitlist_confirmation
        assert result.signup is not None
        assert result.signup.role is EventRole.ALACRITY_DPS
        assert result.signup.flex_roles == (EventRole.DPS,)
        assert result.signup.assigned_role is EventRole.ALACRITY_DPS
        assert not result.signup.waitlisted
        # The whole point of editing over sign-out-and-rejoin: the original
        # signup time, and with it the seating priority, survives.
        assert result.signup.signed_up_at == original.signed_up_at

    async def test_edit_that_frees_a_seat_promotes_the_waitlist(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        waiting = await complete_signup(
            bot,
            event,
            occurrence,
            12,
            EventRole.ALACRITY_HEAL,
            (),
        )
        assert waiting.waitlisted

        result = await apply_signup_edit(
            bot,
            event,
            occurrence,
            11,
            EventRole.DPS,
            (),
        )

        assert result.signup is not None
        assert result.signup.assigned_role is EventRole.DPS
        promoted = store.get_signup(occurrence.occurrence_id, 12)
        assert promoted is not None
        assert not promoted.waitlisted
        assert promoted.assigned_role is EventRole.ALACRITY_HEAL
        # The thread hears about the member the edit moved up, but not about
        # the editor, who reads their outcome in the ephemeral summary.
        channel.thread.send.assert_awaited_once()
        send = channel.thread.send.await_args
        assert send is not None
        assert "<@12>" in send.args[0]
        assert "<@11>" not in send.args[0]

    async def test_edit_that_would_cost_the_seat_requires_confirmation(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        await complete_signup(bot, event, occurrence, 12, EventRole.DPS, ())

        result = await apply_signup_edit(
            bot,
            event,
            occurrence,
            12,
            EventRole.QUICKNESS_HEAL,
            (),
        )

        # Nothing may change until the member consents to losing the seat.
        assert result.needs_waitlist_confirmation
        assert result.signup is None
        stored = store.get_signup(occurrence.occurrence_id, 12)
        assert stored is not None
        assert stored.role is EventRole.DPS
        assert stored.assigned_role is EventRole.DPS
        assert not stored.waitlisted
        channel.thread.send.assert_not_awaited()

    async def test_confirmed_edit_waitlists_but_keeps_queue_priority(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        roster = [
            (11, EventRole.QUICKNESS_HEAL),
            (12, EventRole.DPS),
            (13, EventRole.DPS),
            (14, EventRole.DPS),
            (15, EventRole.DPS),
        ]
        for user_id, role in roster:
            await complete_signup(bot, event, occurrence, user_id, role, ())
        waiting = await complete_signup(
            bot,
            event,
            occurrence,
            16,
            EventRole.DPS,
            (),
        )
        assert waiting.waitlisted

        result = await apply_signup_edit(
            bot,
            event,
            occurrence,
            12,
            EventRole.QUICKNESS_HEAL,
            (),
            allow_waitlist=True,
        )

        # The editor's freed DPS seat goes to the waitlisted member behind
        # them.
        assert result.signup is not None
        assert result.signup.waitlisted
        assert result.signup.assigned_role is None
        assert result.signup.role is EventRole.QUICKNESS_HEAL
        assert [
            signup.discord_user_id for signup in result.update.promoted
        ] == [16]

        # Their original signup time still outranks anyone who joined later:
        # the next freed heal seat is theirs.
        removed, update = await remove_signup(bot, event, occurrence, 11)
        assert removed is not None
        assert [
            signup.discord_user_id for signup in update.promoted
        ] == [12]
        reseated = store.get_signup(occurrence.occurrence_id, 12)
        assert reseated is not None
        assert reseated.assigned_role is EventRole.QUICKNESS_HEAL

    async def test_waitlisted_member_edit_can_seat_them(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        waiting = await complete_signup(
            bot,
            event,
            occurrence,
            12,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        assert waiting.waitlisted

        result = await apply_signup_edit(
            bot,
            event,
            occurrence,
            12,
            EventRole.DPS,
            (),
        )

        assert result.signup is not None
        assert not result.signup.waitlisted
        assert result.signup.assigned_role is EventRole.DPS
        # Seating themselves is the editor's own news; nobody else moved, so
        # the thread stays quiet.
        channel.thread.send.assert_not_awaited()

    async def test_edit_updates_an_enabled_auto_signup(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_DPS,
            (),
        )
        store.set_auto_signup(
            event.event_id,
            11,
            AutoSignupChoice.YES,
            EventRole.QUICKNESS_DPS,
            (),
        )

        await apply_signup_edit(
            bot,
            event,
            occurrence,
            11,
            EventRole.ALACRITY_DPS,
            (EventRole.DPS,),
        )

        auto = store.get_auto_signup(event.event_id, 11)
        assert auto is not None
        assert auto.choice is AutoSignupChoice.YES
        assert auto.role is EventRole.ALACRITY_DPS
        assert auto.flex_roles == (EventRole.DPS,)

    async def test_edit_leaves_a_declined_auto_signup_alone(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_DPS,
            (),
        )
        store.set_auto_signup(
            event.event_id,
            11,
            AutoSignupChoice.NO,
            None,
            (),
        )

        await apply_signup_edit(
            bot,
            event,
            occurrence,
            11,
            EventRole.ALACRITY_DPS,
            (),
        )

        auto = store.get_auto_signup(event.event_id, 11)
        assert auto is not None
        assert auto.choice is AutoSignupChoice.NO
        assert auto.role is None

    async def test_edit_rate_limit_allows_three_then_refills(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_DPS,
            (),
        )
        edit_time = BEFORE_START
        for new_role in (
            EventRole.ALACRITY_DPS,
            EventRole.QUICKNESS_DPS,
            EventRole.ALACRITY_DPS,
        ):
            result = await apply_signup_edit(
                bot,
                event,
                occurrence,
                11,
                new_role,
                (),
                now=edit_time,
            )
            assert result.signup is not None

        with pytest.raises(ValueError, match="used all your signup edits"):
            await apply_signup_edit(
                bot,
                event,
                occurrence,
                11,
                EventRole.QUICKNESS_DPS,
                (),
                now=edit_time,
            )

        # One token returns after three hours (the event, start + 90m from
        # two hours after edit_time, is still running then) - and only one.
        refill_time = edit_time + timedelta(hours=3)
        refilled = await apply_signup_edit(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_DPS,
            (),
            now=refill_time,
        )
        assert refilled.signup is not None
        with pytest.raises(ValueError, match="used all your signup edits"):
            await apply_signup_edit(
                bot,
                event,
                occurrence,
                11,
                EventRole.ALACRITY_DPS,
                (),
                now=refill_time,
            )

    async def test_declined_confirmation_does_not_spend_an_edit(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        await complete_signup(bot, event, occurrence, 12, EventRole.DPS, ())

        # An edit that only reaches the waitlist confirmation mutates
        # nothing, so it must not drain the bucket either - four attempts in
        # a row all get the prompt instead of the rate limit.
        for _ in range(4):
            result = await apply_signup_edit(
                bot,
                event,
                occurrence,
                12,
                EventRole.QUICKNESS_HEAL,
                (),
                now=BEFORE_START,
            )
            assert result.needs_waitlist_confirmation

        stored = store.get_signup(occurrence.occurrence_id, 12)
        assert stored is not None
        assert stored.edit_tokens == 3.0

    async def test_edit_guards_reject_invalid_states(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)

        with pytest.raises(ValueError, match="not signed up"):
            await apply_signup_edit(
                bot,
                event,
                occurrence,
                99,
                EventRole.DPS,
                (),
            )

        await complete_signup(bot, event, occurrence, 11, EventRole.DPS, ())
        with pytest.raises(ValueError, match="already ended"):
            await apply_signup_edit(
                bot,
                event,
                occurrence,
                11,
                EventRole.QUICKNESS_DPS,
                (),
                now=START + timedelta(hours=3),
            )

        wvw_event, wvw_occurrence = await post_new_event(
            bot,
            store,
            category=EventCategory.WVW,
        )
        await complete_signup(bot, wvw_event, wvw_occurrence, 11, None, ())
        with pytest.raises(ValueError, match="no roles to edit"):
            await apply_signup_edit(
                bot,
                wvw_event,
                wvw_occurrence,
                11,
                EventRole.DPS,
                (),
            )


class TestRosterFullComposition:
    def test_fractal_full_requires_a_boon_heal_and_a_boon_dps(self) -> None:
        with_composition = [
            make_signup(1, EventRole.QUICKNESS_HEAL, EventRole.QUICKNESS_HEAL),
            make_signup(2, EventRole.ALACRITY_DPS, EventRole.ALACRITY_DPS),
            make_signup(3, EventRole.DPS, EventRole.DPS),
            make_signup(4, EventRole.DPS, EventRole.DPS),
            make_signup(5, EventRole.DPS, EventRole.DPS),
        ]
        assert is_roster_full(FRACTAL_CAPACITY, with_composition)

    def test_fractal_with_all_seats_but_no_boon_dps_is_not_full(self) -> None:
        # Every seat is taken, yet one boon has no source; the event must
        # keep reading as open rather than done.
        missing_boon_dps = [
            make_signup(1, EventRole.QUICKNESS_HEAL, EventRole.QUICKNESS_HEAL),
            make_signup(2, EventRole.DPS, EventRole.DPS),
            make_signup(3, EventRole.DPS, EventRole.DPS),
            make_signup(4, EventRole.DPS, EventRole.DPS),
            make_signup(5, EventRole.DPS, EventRole.DPS),
        ]
        assert not is_roster_full(FRACTAL_CAPACITY, missing_boon_dps)

    def test_raid_full_requires_two_boon_heals_and_two_boon_dps(self) -> None:
        one_boon_dps = [
            make_signup(1, EventRole.QUICKNESS_HEAL, EventRole.QUICKNESS_HEAL),
            make_signup(2, EventRole.ALACRITY_HEAL, EventRole.ALACRITY_HEAL),
            make_signup(3, EventRole.QUICKNESS_DPS, EventRole.QUICKNESS_DPS),
        ] + [
            make_signup(user_id, EventRole.DPS, EventRole.DPS)
            for user_id in range(4, 11)
        ]
        assert not is_roster_full(RAID_CAPACITY, one_boon_dps)

        two_boon_dps = [
            make_signup(1, EventRole.QUICKNESS_HEAL, EventRole.QUICKNESS_HEAL),
            make_signup(2, EventRole.ALACRITY_HEAL, EventRole.ALACRITY_HEAL),
            make_signup(3, EventRole.QUICKNESS_DPS, EventRole.QUICKNESS_DPS),
            make_signup(4, EventRole.ALACRITY_DPS, EventRole.ALACRITY_DPS),
        ] + [
            make_signup(user_id, EventRole.DPS, EventRole.DPS)
            for user_id in range(5, 11)
        ]
        assert is_roster_full(RAID_CAPACITY, two_boon_dps)

    async def test_status_stays_open_without_boon_coverage(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        roster = [
            (11, EventRole.QUICKNESS_HEAL),
            (12, EventRole.DPS),
            (13, EventRole.DPS),
            (14, EventRole.DPS),
            (15, EventRole.DPS),
        ]
        for user_id, role in roster:
            await complete_signup(bot, event, occurrence, user_id, role, ())

        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        signups = store.get_signups(occurrence.occurrence_id)
        assert (
            occurrence_status(event, updated, signups, BEFORE_START)
            is EventStatus.OPEN
        )


class TestFittingRolesReshuffle:
    def test_boon_seat_held_by_flexer_still_fits(self) -> None:
        flexer = make_signup(
            11,
            EventRole.QUICKNESS_DPS,
            EventRole.QUICKNESS_DPS,
            flex_roles=(EventRole.DPS,),
        )
        # The quickness seat is occupied, but its holder can move aside, so a
        # rigid quickness signup still fits.
        assert EventRole.QUICKNESS_DPS in fitting_roles(
            FRACTAL_CAPACITY,
            [flexer],
        )

    def test_boon_seat_held_rigidly_does_not_fit(self) -> None:
        rigid = make_signup(
            11,
            EventRole.QUICKNESS_DPS,
            EventRole.QUICKNESS_DPS,
        )
        fits = fitting_roles(FRACTAL_CAPACITY, [rigid])
        assert EventRole.QUICKNESS_DPS not in fits
        assert EventRole.QUICKNESS_HEAL not in fits

    def test_rigid_boon_saturation_leaves_no_fitting_role_but_not_full(
        self,
    ) -> None:
        # Both boon caps and the DPS cap are saturated by rigid members while
        # the heal seats sit empty: nobody can join, yet the count-based FULL
        # status stays OPEN. This asymmetry predates the solver and is pinned
        # here deliberately.
        signups = [
            make_signup(1, EventRole.QUICKNESS_DPS, EventRole.QUICKNESS_DPS),
            make_signup(2, EventRole.QUICKNESS_DPS, EventRole.QUICKNESS_DPS),
            make_signup(3, EventRole.ALACRITY_DPS, EventRole.ALACRITY_DPS),
            make_signup(4, EventRole.ALACRITY_DPS, EventRole.ALACRITY_DPS),
            make_signup(5, EventRole.DPS, EventRole.DPS),
            make_signup(6, EventRole.DPS, EventRole.DPS),
            make_signup(7, EventRole.DPS, EventRole.DPS),
            make_signup(8, EventRole.DPS, EventRole.DPS),
        ]
        assert fitting_roles(RAID_CAPACITY, signups) == []
        assert not is_roster_full(RAID_CAPACITY, signups)


class TestMergeRosterUpdates:
    def test_chains_role_changes_and_drops_identity_chains(self) -> None:
        first = RosterUpdate(
            reassigned=(
                RoleChange(
                    discord_user_id=11,
                    old_role=EventRole.QUICKNESS_DPS,
                    new_role=EventRole.DPS,
                ),
            ),
        )
        second = RosterUpdate(
            reassigned=(
                RoleChange(
                    discord_user_id=11,
                    old_role=EventRole.DPS,
                    new_role=EventRole.ALACRITY_DPS,
                ),
                RoleChange(
                    discord_user_id=12,
                    old_role=EventRole.ALACRITY_HEAL,
                    new_role=EventRole.QUICKNESS_HEAL,
                ),
            ),
        )
        third = RosterUpdate(
            reassigned=(
                RoleChange(
                    discord_user_id=12,
                    old_role=EventRole.QUICKNESS_HEAL,
                    new_role=EventRole.ALACRITY_HEAL,
                ),
            ),
        )

        merged = merge_roster_updates([first, second, third])

        # 11's moves collapse to first-old -> last-new; 12 ended where they
        # started, so announcing them would be noise.
        assert merged.reassigned == (
            RoleChange(
                discord_user_id=11,
                old_role=EventRole.QUICKNESS_DPS,
                new_role=EventRole.ALACRITY_DPS,
            ),
        )
        assert merged.promoted == ()

    def test_promotion_followed_by_reassignment_stays_one_promotion(
        self,
    ) -> None:
        promoted = make_signup(
            11,
            EventRole.QUICKNESS_DPS,
            EventRole.QUICKNESS_DPS,
        )
        first = RosterUpdate(promoted=(promoted,))
        second = RosterUpdate(
            reassigned=(
                RoleChange(
                    discord_user_id=11,
                    old_role=EventRole.QUICKNESS_DPS,
                    new_role=EventRole.DPS,
                ),
            ),
        )

        merged = merge_roster_updates([first, second])

        # The member never saw their intermediate seat, so the announcement
        # is a single promotion at the final one.
        assert merged.reassigned == ()
        assert len(merged.promoted) == 1
        assert merged.promoted[0].assigned_role is EventRole.DPS

    def test_users_removed_later_in_the_batch_are_dropped(self) -> None:
        update = RosterUpdate(
            reassigned=(
                RoleChange(
                    discord_user_id=12,
                    old_role=EventRole.QUICKNESS_DPS,
                    new_role=EventRole.DPS,
                ),
            ),
            promoted=(make_signup(11, EventRole.DPS, EventRole.DPS),),
        )

        merged = merge_roster_updates([update], removed_user_ids=[11, 12])

        assert not merged.has_changes


class TestApplyAutoSignups:
    async def test_applies_stored_yes_choices_with_roles(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        store.set_auto_signup(
            event.event_id,
            11,
            AutoSignupChoice.YES,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        store.set_auto_signup(
            event.event_id,
            12,
            AutoSignupChoice.NO,
            EventRole.DPS,
            (),
        )
        store.set_auto_signup(
            event.event_id,
            13,
            AutoSignupChoice.YES,
            None,
            (),
        )

        applied = apply_auto_signups(bot, event, occurrence)

        assert applied == 1
        signups = store.get_signups(occurrence.occurrence_id)
        assert [signup.discord_user_id for signup in signups] == [11]
        assert signups[0].assigned_role is EventRole.QUICKNESS_HEAL

    async def test_skips_users_who_are_already_signed_up(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.DPS,
            (),
        )
        store.set_auto_signup(
            event.event_id,
            11,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (),
        )

        assert apply_auto_signups(bot, event, occurrence) == 0

    async def test_auto_signup_can_reshuffle_earlier_entries(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        store.set_auto_signup(
            event.event_id,
            11,
            AutoSignupChoice.YES,
            EventRole.QUICKNESS_DPS,
            (EventRole.DPS,),
        )
        store.set_auto_signup(
            event.event_id,
            12,
            AutoSignupChoice.YES,
            EventRole.QUICKNESS_DPS,
            (),
        )

        applied = apply_auto_signups(bot, event, occurrence)

        # The flexible entry yields the quickness seat to the rigid one, the
        # same admission a pair of live signups would get.
        assert applied == 2
        seats = {
            signup.discord_user_id: signup.assigned_role
            for signup in store.get_signups(occurrence.occurrence_id)
            if not signup.waitlisted
        }
        assert seats == {
            11: EventRole.DPS,
            12: EventRole.QUICKNESS_DPS,
        }

    async def test_auto_signup_that_cannot_fit_is_waitlisted(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        store.set_auto_signup(
            event.event_id,
            11,
            AutoSignupChoice.YES,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        store.set_auto_signup(
            event.event_id,
            12,
            AutoSignupChoice.YES,
            EventRole.QUICKNESS_HEAL,
            (),
        )

        applied = apply_auto_signups(bot, event, occurrence)

        assert applied == 2
        by_user = {
            signup.discord_user_id: signup
            for signup in store.get_signups(occurrence.occurrence_id)
        }
        assert not by_user[11].waitlisted
        assert by_user[12].waitlisted
        assert by_user[12].assigned_role is None


class TestDisableAutoSignup:
    async def _seed_next(
        self,
        bot: Any,
        store: EventStore,
        user_ids: tuple[int, ...] = (11,),
    ) -> Any:
        # Mirrors what _create_next_occurrence leaves behind: a later, unposted
        # occurrence whose roster came entirely from apply_auto_signups.
        event, occurrence = await post_new_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        for user_id in user_ids:
            store.set_auto_signup(
                event.event_id,
                user_id,
                AutoSignupChoice.YES,
                EventRole.QUICKNESS_HEAL,
                (),
            )
        following = store.create_occurrence(
            event.event_id,
            START + timedelta(days=1),
        )
        apply_auto_signups(bot, event, following)
        return event, occurrence, following

    async def test_stores_the_choice(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence, _ = await self._seed_next(bot, store)

        disable_auto_signup(bot, event, occurrence, 11)

        stored = store.get_auto_signup(event.event_id, 11)
        assert stored is not None
        assert stored.choice is AutoSignupChoice.NO

    async def test_withdraws_the_seat_on_an_unposted_next_occurrence(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        # The regression: the next occurrence can be seeded from the still
        # enabled preference before the choice lands, leaving the member on a
        # roster they were just told they would not be on.
        event, occurrence, following = await self._seed_next(bot, store)

        result = disable_auto_signup(bot, event, occurrence, 11)

        assert store.get_signup(following.occurrence_id, 11) is None
        assert [
            withdrawn.occurrence_id for withdrawn in result.withdrawn
        ] == [following.occurrence_id]
        assert result.still_seated == ()

    async def test_withdrawing_promotes_the_seeded_waitlist(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        # Both entries want the only quickness heal seat, so the second is
        # waitlisted; freeing the seat has to hand it over.
        event, occurrence, following = await self._seed_next(
            bot,
            store,
            (11, 12),
        )
        assert store.get_signups(following.occurrence_id)[1].waitlisted

        disable_auto_signup(bot, event, occurrence, 11)

        remaining = store.get_signups(following.occurrence_id)
        assert [signup.discord_user_id for signup in remaining] == [12]
        assert not remaining[0].waitlisted
        assert remaining[0].assigned_role is EventRole.QUICKNESS_HEAL

    async def test_keeps_and_reports_a_seat_on_a_posted_occurrence(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        # A posted occurrence's roster can hold deliberate signups, so the seat
        # stands and the caller is told about it rather than the member being
        # unseated on a guess.
        event, occurrence, following = await self._seed_next(bot, store)
        posted = await post_occurrence(bot, event, following, BEFORE_START)

        result = disable_auto_signup(bot, event, occurrence, 11)

        assert store.get_signup(posted.occurrence_id, 11) is not None
        assert result.withdrawn == ()
        assert [
            seated.occurrence_id for seated in result.still_seated
        ] == [posted.occurrence_id]

    async def test_leaves_the_current_occurrence_alone(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        await complete_signup(bot, event, occurrence, 11, EventRole.DPS, ())
        store.set_auto_signup(
            event.event_id,
            11,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (),
        )

        result = disable_auto_signup(bot, event, occurrence, 11)

        assert store.get_signup(occurrence.occurrence_id, 11) is not None
        assert result.withdrawn == ()
        assert result.still_seated == ()


class TestRefreshOccurrenceMessage:
    async def test_status_transition_renames_thread_and_persists(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        during_event = START + timedelta(minutes=10)

        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            during_event,
        )

        assert status is EventStatus.ONGOING
        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        assert updated.status is EventStatus.ONGOING
        channel.thread.edit.assert_awaited_once()
        rename = channel.thread.edit.await_args
        assert rename is not None
        assert rename.kwargs["name"].startswith("🟡 |")

    async def test_over_transition_seeds_next_recurring_occurrence(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        # A refresh driven by a roster change (not the scheduler) can be the one
        # that crosses into OVER when it lands just before start + duration. The
        # scheduler seeds the next occurrence before an OVER transition; this
        # path must do the same, or the recurring series ends silently once the
        # occurrence drops out of the unfinished set.
        event, occurrence = await post_new_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        after_end = START + timedelta(minutes=90)
        assert len(store.get_event_occurrences(event.event_id)) == 1

        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            after_end,
        )

        assert status is EventStatus.OVER
        occurrences = store.get_event_occurrences(event.event_id)
        assert len(occurrences) == 2
        seeded = next(
            item
            for item in occurrences
            if item.occurrence_id != occurrence.occurrence_id
        )
        assert seeded.start_time == START + timedelta(days=1)

    async def test_over_transition_does_not_seed_a_non_repeating_event(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        after_end = START + timedelta(minutes=90)

        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            after_end,
        )

        assert status is EventStatus.OVER
        # A one-off event has no successor to seed.
        assert len(store.get_event_occurrences(event.event_id)) == 1

    async def test_unchanged_status_does_not_rename_the_thread(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)

        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            BEFORE_START,
        )

        assert status is EventStatus.OPEN
        channel.thread.edit.assert_not_awaited()

    async def test_failed_thread_rename_defers_status_for_retry(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        during_event = START + timedelta(minutes=10)
        channel.thread.edit = AsyncMock(side_effect=forbidden_error(50001))

        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            during_event,
        )

        # The transition must not be committed while the thread name is still
        # stale, or the scheduler would stop retrying the rename.
        assert status is EventStatus.OPEN
        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        assert updated.status is EventStatus.OPEN
        assert updated.needs_refresh
        assert updated.occurrence_id in {
            live.occurrence_id
            for live in store.get_posted_unfinished_occurrences()
        }

        # Once the thread rename succeeds, the transition is committed and the
        # dirty flag cleared.
        channel.thread.edit = AsyncMock()
        retry = await refresh_occurrence_message(
            bot,
            event,
            updated,
            during_event,
        )

        assert retry is EventStatus.ONGOING
        committed = store.get_occurrence(occurrence.occurrence_id)
        assert committed is not None
        assert committed.status is EventStatus.ONGOING
        assert not committed.needs_refresh
        channel.thread.edit.assert_awaited_once()

    async def test_failed_message_edit_keeps_status_for_retry(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        after_end = START + timedelta(hours=3)
        channel.partial_message.edit = AsyncMock(
            side_effect=forbidden_error(50001)
        )

        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            after_end,
        )

        # The transition to OVER must not be persisted when the public
        # message could not be refreshed, so the scheduler keeps retrying.
        assert status is EventStatus.OPEN
        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        assert updated.status is EventStatus.OPEN
        assert updated.needs_refresh
        assert updated.occurrence_id in {
            live.occurrence_id
            for live in store.get_posted_unfinished_occurrences()
        }
        channel.thread.edit.assert_not_awaited()

    async def test_failed_refresh_marks_dirty_when_status_unchanged(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        channel.partial_message.edit = AsyncMock(
            side_effect=forbidden_error(50001)
        )

        # A roster change that leaves the status OPEN but fails to edit the
        # message must still record dirty state so the scheduler retries.
        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            BEFORE_START,
        )

        assert status is EventStatus.OPEN
        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        assert updated.needs_refresh

    async def test_successful_refresh_clears_dirty_flag(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        store.set_occurrence_needs_refresh(occurrence.occurrence_id, True)
        dirty = store.get_occurrence(occurrence.occurrence_id)
        assert dirty is not None and dirty.needs_refresh

        await refresh_occurrence_message(bot, event, dirty, BEFORE_START)

        cleared = store.get_occurrence(occurrence.occurrence_id)
        assert cleared is not None
        assert not cleared.needs_refresh
        channel.partial_message.edit.assert_awaited()

    async def test_forced_rename_updates_thread_without_status_change(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)

        # An edit that reschedules the occurrence keeps the OPEN status but must
        # still rename the thread, whose name encodes the date and time.
        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            BEFORE_START,
            force_thread_rename=True,
        )

        assert status is EventStatus.OPEN
        channel.thread.edit.assert_awaited_once()
        rename = channel.thread.edit.await_args
        assert rename is not None
        assert rename.kwargs["name"].startswith("🟢 |")

    async def test_forced_rename_failure_recovers_on_scheduler_retry(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        channel.thread.edit = AsyncMock(side_effect=forbidden_error(50001))

        # An edit forces a rename (status unchanged) but the rename fails
        # transiently, so the occurrence is left dirty.
        await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            BEFORE_START,
            force_thread_rename=True,
        )
        dirty = store.get_occurrence(occurrence.occurrence_id)
        assert dirty is not None
        assert dirty.needs_refresh

        # The scheduler retry does NOT pass force_thread_rename, so the dirty
        # flag itself must trigger the rename; otherwise the thread name would
        # be cleared as clean while still stale.
        channel.thread.edit = AsyncMock()
        await refresh_occurrence_message(bot, event, dirty, BEFORE_START)

        channel.thread.edit.assert_awaited_once()
        cleared = store.get_occurrence(occurrence.occurrence_id)
        assert cleared is not None
        assert not cleared.needs_refresh


class TestRepostOccurrence:
    async def test_reposts_to_new_channel_and_readds_members(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread

        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        for user_id in (11, 12):
            store.add_signup(
                occurrence_id=posted.occurrence_id,
                discord_user_id=user_id,
                role=EventRole.DPS,
                assigned_role=EventRole.DPS,
                flex_roles=(),
                waitlisted=False,
            )
        moved = store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=new_channel.id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )

        reposted = await repost_occurrence(bot, moved, posted)

        # Old message and its thread deleted, fresh one sent in the new
        # channel, and every existing signup re-added to the new thread.
        old_channel.partial_message.delete.assert_awaited_once()
        old_channel.thread.delete.assert_awaited_once()
        assert len(new_channel.sent) == 1
        assert reposted.thread_id == 888
        assert new_channel.thread.add_user.await_count == 2
        stored = store.get_occurrence(posted.occurrence_id)
        assert stored is not None
        assert stored.thread_id == 888

    async def test_repost_survives_a_failed_old_message_delete(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        old_channel.partial_message.delete = AsyncMock(
            side_effect=forbidden_error(50001)
        )
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread

        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        moved = store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=new_channel.id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )

        reposted = await repost_occurrence(bot, moved, posted)

        # A failed delete of the old post must not stop the move from posting
        # into the new channel, nor stop the old thread from being cleaned up.
        assert len(new_channel.sent) == 1
        assert reposted.thread_id == 888
        old_channel.thread.delete.assert_awaited_once()


    async def test_repost_keeps_the_old_post_when_the_new_post_fails(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread

        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        moved = store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=new_channel.id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )
        new_channel.send_error = forbidden_error(50001)

        with pytest.raises(discord.HTTPException):
            await repost_occurrence(bot, moved, posted)

        # The new post never went out, so the old one must still be live and
        # still referenced. Deleting it first would strand the occurrence on a
        # dead message id and cost the event its only public post.
        old_channel.partial_message.delete.assert_not_awaited()
        old_channel.thread.delete.assert_not_awaited()
        stored = store.get_occurrence(posted.occurrence_id)
        assert stored is not None
        assert stored.message_id == posted.message_id
        assert stored.thread_id == posted.thread_id


class TestRebalanceOccurrenceRoster:
    def seat(
        self,
        store: EventStore,
        occurrence_id: int,
        user_id: int,
        role: EventRole | None,
        assigned_role: EventRole | None,
        waitlisted: bool = False,
    ) -> None:
        store.add_signup(
            occurrence_id=occurrence_id,
            discord_user_id=user_id,
            role=role,
            assigned_role=assigned_role,
            flex_roles=(),
            waitlisted=waitlisted,
        )

    def test_role_less_roster_falls_back_to_dps_and_waitlists(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event = create_event(store, category=EventCategory.WVW)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        for user_id in range(1, 8):
            self.seat(store, occurrence.occurrence_id, user_id, None, None)
        fractal = store.update_event(
            event_id=event.event_id,
            category=EventCategory.FRACTAL,
            title=event.title,
            description=event.description,
            channel_id=event.channel_id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )

        changed, _ = rebalance_occurrence_roster(bot, fractal, occurrence)

        assert changed == 7
        signups = store.get_signups(occurrence.occurrence_id)
        admitted = [signup for signup in signups if not signup.waitlisted]
        # Fractal seats 4 DPS; the role-less WvW roster would otherwise read as
        # zero DPS and keep admitting past capacity.
        assert [signup.discord_user_id for signup in admitted] == [1, 2, 3, 4]
        assert all(
            signup.assigned_role is EventRole.DPS for signup in admitted
        )
        assert not is_roster_full(fractal.capacity, signups)
        # A further DPS no longer fits, so the overfill is closed.
        assert EventRole.DPS not in fitting_roles(fractal.capacity, signups)

    def test_shrinking_capacity_waitlists_the_overflow_in_signup_order(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event = create_event(store, category=EventCategory.RAID)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        # A full raid roster: 2 healers and 8 DPS.
        self.seat(
            store,
            occurrence.occurrence_id,
            1,
            EventRole.QUICKNESS_HEAL,
            EventRole.QUICKNESS_HEAL,
        )
        self.seat(
            store,
            occurrence.occurrence_id,
            2,
            EventRole.ALACRITY_HEAL,
            EventRole.ALACRITY_HEAL,
        )
        for user_id in range(3, 11):
            self.seat(
                store,
                occurrence.occurrence_id,
                user_id,
                EventRole.DPS,
                EventRole.DPS,
            )
        fractal = store.update_event(
            event_id=event.event_id,
            category=EventCategory.FRACTAL,
            title=event.title,
            description=event.description,
            channel_id=event.channel_id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )

        rebalance_occurrence_roster(bot, fractal, occurrence)

        signups = store.get_signups(occurrence.occurrence_id)
        seats = {
            signup.discord_user_id: signup.assigned_role
            for signup in signups
            if not signup.waitlisted
        }
        # Fractal seats 1 healer and 4 DPS. User 1 keeps the only heal seat.
        # User 2 was the second healer and no longer fits as one, so rather than
        # being waitlisted they take the DPS fallback. That plus users 3-5 fills
        # the 4 DPS seats, and the remaining DPS drop to the waitlist in sign-up
        # order.
        assert seats == {
            1: EventRole.QUICKNESS_HEAL,
            2: EventRole.DPS,
            3: EventRole.DPS,
            4: EventRole.DPS,
            5: EventRole.DPS,
        }
        assert [
            signup.discord_user_id for signup in signups if signup.waitlisted
        ] == [6, 7, 8, 9, 10]
        assert len(signups) == 10
        # Every seat is taken, but the reseated roster has no boon DPS, so
        # the composition-gated FULL keeps the event reading as open.
        assert not is_roster_full(fractal.capacity, signups)

    def test_moving_to_a_role_less_category_clears_the_assignments(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event = create_event(store, category=EventCategory.FRACTAL)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        self.seat(
            store,
            occurrence.occurrence_id,
            1,
            EventRole.QUICKNESS_HEAL,
            EventRole.QUICKNESS_HEAL,
        )
        self.seat(
            store,
            occurrence.occurrence_id,
            2,
            EventRole.DPS,
            None,
            waitlisted=True,
        )
        wvw = store.update_event(
            event_id=event.event_id,
            category=EventCategory.WVW,
            title=event.title,
            description=event.description,
            channel_id=event.channel_id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )

        rebalance_occurrence_roster(bot, wvw, occurrence)

        signups = store.get_signups(occurrence.occurrence_id)
        # WvW seats plain headcount, so assignments are dropped and the
        # waitlisted DPS gets a seat (50 slots, 2 signups).
        assert all(signup.assigned_role is None for signup in signups)
        assert all(not signup.waitlisted for signup in signups)
        # The role preferences survive, so switching back can honour them.
        assert [signup.role for signup in signups] == [
            EventRole.QUICKNESS_HEAL,
            EventRole.DPS,
        ]


class TestDeleteEventPosts:
    async def test_deletes_posted_messages_and_skips_unposted(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = create_event(store)
        posted = store.create_occurrence(event.event_id, event.start_time)
        await post_occurrence(bot, event, posted, BEFORE_START)
        unposted = store.create_occurrence(event.event_id, event.start_time)
        occurrences = store.get_event_occurrences(event.event_id)

        deleted = await delete_event_posts(bot, event, occurrences)

        # Only the posted occurrence has a message to remove, and its thread
        # is deleted separately since it does not disappear on its own.
        assert deleted == 1
        assert unposted.message_id is None
        channel.partial_message.delete.assert_awaited_once()
        channel.thread.delete.assert_awaited_once()

    async def test_deletes_each_post_through_the_channel_it_was_posted_to(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread

        event = create_event(store, repeat_frequency=RepeatFrequency.DAILY)
        old = store.create_occurrence(event.event_id, START)
        await post_occurrence(bot, event, old, BEFORE_START)
        store.set_occurrence_status(old.occurrence_id, EventStatus.OVER)
        # The event is moved to another channel. A channel edit only re-posts
        # the live occurrences, so this finished post stays behind in the old
        # channel while event.channel_id moves on.
        moved = store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=new_channel.id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )
        new_start = START + timedelta(days=1)
        new = store.create_occurrence(moved.event_id, new_start)
        await post_occurrence(bot, moved, new, new_start - timedelta(hours=1))
        occurrences = store.get_event_occurrences(moved.event_id)

        deleted = await delete_event_posts(bot, moved, occurrences)

        # Both posts are removed, each through the channel it actually lives in.
        # Addressing the old one through the event's current channel returns
        # NotFound and would leave it visible forever once the rows are gone.
        # Both threads are removed too, since neither disappears on its own.
        assert deleted == 2
        old_channel.partial_message.delete.assert_awaited_once()
        new_channel.partial_message.delete.assert_awaited_once()
        old_channel.thread.delete.assert_awaited_once()
        new_channel.thread.delete.assert_awaited_once()

    async def test_an_unresolvable_channel_does_not_strand_the_others(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread

        event = create_event(store, repeat_frequency=RepeatFrequency.DAILY)
        old = store.create_occurrence(event.event_id, START)
        await post_occurrence(bot, event, old, BEFORE_START)
        moved = store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=new_channel.id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )
        new_start = START + timedelta(days=1)
        new = store.create_occurrence(moved.event_id, new_start)
        await post_occurrence(bot, moved, new, new_start - timedelta(hours=1))
        # The old channel is gone (deleted by a moderator, say).
        del bot._channels[old_channel.id]
        occurrences = store.get_event_occurrences(moved.event_id)

        deleted = await delete_event_posts(bot, moved, occurrences)

        # The post in the surviving channel is still removed, thread included.
        # The old occurrence's channel could not be resolved at all, so its
        # thread delete is never attempted (a real dead parent channel takes
        # its threads with it on Discord's side).
        assert deleted == 1
        new_channel.partial_message.delete.assert_awaited_once()
        new_channel.thread.delete.assert_awaited_once()
        old_channel.thread.delete.assert_not_awaited()

    async def test_a_failed_message_delete_does_not_stop_the_others(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = create_event(store)
        first = store.create_occurrence(event.event_id, event.start_time)
        await post_occurrence(bot, event, first, BEFORE_START)
        second = store.create_occurrence(event.event_id, event.start_time)
        await post_occurrence(bot, event, second, BEFORE_START)
        channel.partial_message.delete = AsyncMock(
            side_effect=forbidden_error(50001)
        )
        occurrences = store.get_event_occurrences(event.event_id)

        deleted = await delete_event_posts(bot, event, occurrences)

        # Both message deletes were attempted even though they fail, and a
        # failed message delete does not stop the thread delete either.
        assert deleted == 0
        assert channel.partial_message.delete.await_count == 2
        assert channel.thread.delete.await_count == 2

    async def test_a_failed_thread_delete_does_not_stop_the_others(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        old_channel.thread.delete = AsyncMock(side_effect=forbidden_error(50001))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread

        event = create_event(store, repeat_frequency=RepeatFrequency.DAILY)
        old = store.create_occurrence(event.event_id, START)
        await post_occurrence(bot, event, old, BEFORE_START)
        store.set_occurrence_status(old.occurrence_id, EventStatus.OVER)
        moved = store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=new_channel.id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )
        new_start = START + timedelta(days=1)
        new = store.create_occurrence(moved.event_id, new_start)
        await post_occurrence(bot, moved, new, new_start - timedelta(hours=1))
        occurrences = store.get_event_occurrences(moved.event_id)

        deleted = await delete_event_posts(bot, moved, occurrences)

        # The old thread's delete fails, but the message deletes (both posts)
        # still go through and the new thread is still removed.
        assert deleted == 2
        old_channel.thread.delete.assert_awaited_once()
        new_channel.thread.delete.assert_awaited_once()

    async def test_a_thread_already_gone_is_skipped_without_error(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        await post_occurrence(bot, event, occurrence, BEFORE_START)
        channel.thread.delete = AsyncMock(side_effect=not_found_error())
        occurrences = store.get_event_occurrences(event.event_id)

        deleted = await delete_event_posts(bot, event, occurrences)

        # A thread already gone (deleted by a moderator, or by Discord along
        # with its parent channel) is not a failure worth logging as an error.
        assert deleted == 1
        channel.thread.delete.assert_awaited_once()


class TestPruneSupersededOccurrences:
    async def make_series(
        self,
        bot: Any,
        store: EventStore,
        *,
        delete_previous_on_repeat: bool = True,
    ) -> Any:
        event = create_event(
            store,
            repeat_frequency=RepeatFrequency.DAILY,
            delete_previous_on_repeat=delete_previous_on_repeat,
        )
        old = store.create_occurrence(event.event_id, START)
        posted_old = await post_occurrence(bot, event, old, BEFORE_START)
        new_start = START + timedelta(days=1)
        new = store.create_occurrence(event.event_id, new_start)
        posted_new = await post_occurrence(
            bot, event, new, new_start - timedelta(hours=1)
        )
        return event, posted_old, posted_new

    async def test_removes_earlier_over_occurrences_and_their_posts(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, posted_old, posted_new = await self.make_series(bot, store)
        store.set_occurrence_status(
            posted_old.occurrence_id, EventStatus.OVER
        )

        deleted = await prune_superseded_occurrences(bot, event)

        assert deleted == 1
        assert store.get_occurrence(posted_old.occurrence_id) is None
        assert store.get_occurrence(posted_new.occurrence_id) is not None
        channel.partial_message.delete.assert_awaited()

    async def test_keeps_earlier_occurrence_that_is_not_over(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, posted_old, _ = await self.make_series(bot, store)

        deleted = await prune_superseded_occurrences(bot, event)

        # The still-live earlier occurrence must never be removed.
        assert deleted == 0
        assert store.get_occurrence(posted_old.occurrence_id) is not None
        channel.partial_message.delete.assert_not_awaited()

    async def test_no_op_for_an_event_that_did_not_opt_in(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, posted_old, _ = await self.make_series(
            bot, store, delete_previous_on_repeat=False
        )
        store.set_occurrence_status(
            posted_old.occurrence_id, EventStatus.OVER
        )

        deleted = await prune_superseded_occurrences(bot, event)

        # The opt-in is enforced inside the prune, so no caller can forget it.
        assert deleted == 0
        assert store.get_occurrence(posted_old.occurrence_id) is not None
        channel.partial_message.delete.assert_not_awaited()

    async def test_keeps_the_previous_post_until_the_next_one_is_live(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = create_event(
            store,
            repeat_frequency=RepeatFrequency.DAILY,
            delete_previous_on_repeat=True,
        )
        old = store.create_occurrence(event.event_id, START)
        posted_old = await post_occurrence(bot, event, old, BEFORE_START)
        store.set_occurrence_status(
            posted_old.occurrence_id, EventStatus.OVER
        )
        # The next occurrence is seeded but not posted yet.
        store.create_occurrence(event.event_id, START + timedelta(days=1))

        deleted = await prune_superseded_occurrences(bot, event)

        # Removing the old post before the new one is live would leave the
        # channel with no post at all.
        assert deleted == 0
        assert store.get_occurrence(posted_old.occurrence_id) is not None
        channel.partial_message.delete.assert_not_awaited()


class TestCancelOccurrence:
    async def make_series(
        self,
        bot: Any,
        store: EventStore,
        *,
        delete_previous_on_repeat: bool = True,
    ) -> Any:
        event = create_event(
            store,
            repeat_frequency=RepeatFrequency.DAILY,
            delete_previous_on_repeat=delete_previous_on_repeat,
        )
        occurrence = store.create_occurrence(event.event_id, START)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        return event, posted

    async def test_removes_the_occurrence_and_posts_the_next_one(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, posted = await self.make_series(bot, store)

        cancellation = await cancel_occurrence(
            bot, event, posted, BEFORE_START
        )

        assert store.get_occurrence(posted.occurrence_id) is None
        channel.partial_message.delete.assert_awaited_once()
        channel.thread.delete.assert_awaited_once()
        successor = cancellation.successor
        assert successor is not None
        assert successor.start_time == START + timedelta(days=1)
        # The cancelled post was the series' only posted occurrence, so the
        # scheduler would never pick the successor up: it has to be posted here
        # or the series would stay invisible forever.
        assert cancellation.successor_posted
        assert successor.message_id is not None
        assert len(channel.sent) == 2

    async def test_removes_the_cancelled_occurrences_signups(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, posted = await self.make_series(bot, store)
        store.add_signup(
            occurrence_id=posted.occurrence_id,
            discord_user_id=11,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )

        cancellation = await cancel_occurrence(
            bot, event, posted, BEFORE_START
        )

        assert store.get_signups(posted.occurrence_id) == []
        successor = cancellation.successor
        assert successor is not None
        # The roster belonged to the cancelled run; the next one starts empty
        # unless a member asked to be signed up automatically.
        assert store.get_signups(successor.occurrence_id) == []

    async def test_seeds_the_next_occurrence_with_auto_signups(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, posted = await self.make_series(bot, store)
        store.set_auto_signup(
            event.event_id,
            11,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (),
        )

        cancellation = await cancel_occurrence(
            bot, event, posted, BEFORE_START
        )

        successor = cancellation.successor
        assert successor is not None
        assert [
            signup.discord_user_id
            for signup in store.get_signups(successor.occurrence_id)
        ] == [11]

    async def test_reuses_a_successor_that_already_exists(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, posted = await self.make_series(bot, store)
        # The scheduler already seeded the next run (the cancelled occurrence
        # is one it has moved past), so cancelling must not seed a second one.
        existing = store.create_occurrence(
            event.event_id,
            START + timedelta(days=1),
        )

        cancellation = await cancel_occurrence(
            bot, event, posted, BEFORE_START
        )

        successor = cancellation.successor
        assert successor is not None
        assert successor.occurrence_id == existing.occurrence_id
        assert len(store.get_event_occurrences(event.event_id)) == 1

    async def test_keeps_a_successor_that_is_already_posted(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, posted = await self.make_series(bot, store)
        following = store.create_occurrence(
            event.event_id,
            START + timedelta(days=1),
        )
        await post_occurrence(bot, event, following, BEFORE_START)

        cancellation = await cancel_occurrence(
            bot, event, posted, BEFORE_START
        )

        # The successor's post is already live, so it is left alone rather than
        # sent a second time.
        assert cancellation.successor_posted
        assert len(channel.sent) == 2

    async def test_subscribes_auto_signed_members_to_the_new_post(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, posted = await self.make_series(bot, store)
        store.set_auto_signup(
            event.event_id,
            11,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (),
        )

        await cancel_occurrence(bot, event, posted, BEFORE_START)

        # A member carried onto the successor never touched its post, so
        # nothing else would subscribe them to it.
        channel.thread.add_user.assert_awaited_once()

    async def test_reports_a_successor_that_could_not_be_posted(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, posted = await self.make_series(bot, store)
        channel.send_error = forbidden_error(50013)

        cancellation = await cancel_occurrence(
            bot, event, posted, BEFORE_START
        )

        # The cancellation itself already stands, so the failure is reported
        # rather than raised.
        assert store.get_occurrence(posted.occurrence_id) is None
        successor = cancellation.successor
        assert successor is not None
        assert not cancellation.successor_posted
        stored = store.get_occurrence(successor.occurrence_id)
        assert stored is not None
        # The series has no posted occurrence left, which is what normally
        # makes the scheduler leave a pending one alone; the flag is what lets
        # it retry this posting instead of hiding the series for good.
        assert stored.needs_refresh

    async def test_a_store_failure_leaves_the_occurrence_in_place(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, posted = await self.make_series(bot, store)
        real_delete = store.delete_occurrence

        def delete_all_but_the_cancelled_row(occurrence_id: int) -> None:
            if occurrence_id == posted.occurrence_id:
                raise SQLAlchemyError("boom")
            real_delete(occurrence_id)

        store.delete_occurrence = MagicMock(  # type: ignore[method-assign]
            side_effect=delete_all_but_the_cancelled_row
        )

        with pytest.raises(SQLAlchemyError):
            await cancel_occurrence(bot, event, posted, BEFORE_START)

        # Nothing public is touched before the row is gone, so the run is still
        # posted and the commander can try again.
        channel.partial_message.delete.assert_not_awaited()
        channel.thread.delete.assert_not_awaited()
        # The successor was committed in its own transaction before the delete
        # failed. Left behind, a maintenance pass would post next week's run
        # while this week's - which was never cancelled - is still live.
        assert [
            occurrence.occurrence_id
            for occurrence in store.get_event_occurrences(event.event_id)
        ] == [posted.occurrence_id]

    async def test_a_racing_post_is_not_sent_twice(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = create_event(store, repeat_frequency=RepeatFrequency.DAILY)
        pending = store.create_occurrence(event.event_id, START)

        # The maintenance pass and a cancellation both post pending
        # occurrences, and each awaits Discord while the other can run.
        results = await asyncio.gather(
            post_pending_occurrence(bot, event, pending, BEFORE_START),
            post_pending_occurrence(bot, event, pending, BEFORE_START),
        )

        assert len(channel.sent) == 1
        assert sum(result is not None for result in results) == 1

    async def test_a_successor_posted_by_the_scheduler_is_left_alone(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, posted = await self.make_series(bot, store)
        successor = store.create_occurrence(
            event.event_id,
            START + timedelta(days=1),
        )
        async def post_from_the_scheduler(*args: Any, **kwargs: Any) -> None:
            # Stand in for a maintenance pass that posts the successor while
            # the cancellation is clearing the cancelled run's post.
            await posting.post_occurrence(bot, event, successor, BEFORE_START)

        channel.thread.delete = AsyncMock(side_effect=post_from_the_scheduler)

        cancellation = await cancel_occurrence(
            bot, event, posted, BEFORE_START
        )

        # One post for the successor, and the cancellation reports the series
        # as posted rather than as a failure.
        assert len(channel.sent) == 2
        assert cancellation.successor_posted
        assert cancellation.successor is not None
        assert (
            cancellation.successor.occurrence_id == successor.occurrence_id
        )

    async def test_cancellation_logs_never_contain_user_content(
        self,
        bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        title = "SECRET EVENT TITLE"
        description = "SECRET EVENT DESCRIPTION"
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title=title,
            description=description,
            channel_id=1234,
            leader_discord_id=42,
            start_time=START,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.DAILY,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, START)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)

        with caplog.at_level("DEBUG"):
            await cancel_occurrence(bot, event, posted, BEFORE_START)

        assert title not in caplog.text
        assert description not in caplog.text


class TestPostingLoggingSafety:
    async def test_posting_and_signup_logs_never_contain_user_content(
        self,
        bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        title = "SECRET EVENT TITLE"
        description = "SECRET EVENT DESCRIPTION"
        with caplog.at_level("DEBUG"):
            event = store.create_event(
                category=EventCategory.FRACTAL,
                title=title,
                description=description,
                channel_id=1234,
                leader_discord_id=42,
                start_time=START,
                duration_minutes=90,
                repeat_frequency=RepeatFrequency.NONE,
                repeat_days=(),
            )
            occurrence = store.create_occurrence(
                event.event_id,
                event.start_time,
            )
            occurrence = await post_occurrence(
                bot,
                event,
                occurrence,
                BEFORE_START,
            )
            await complete_signup(
                bot,
                event,
                occurrence,
                11,
                EventRole.DPS,
                (),
            )
            await remove_signup(bot, event, occurrence, 11)
            await delete_event_posts(
                bot,
                event,
                store.get_event_occurrences(event.event_id),
            )

        assert title not in caplog.text
        assert description not in caplog.text

    async def test_forum_post_logs_never_contain_user_content(
        self,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Posting into an existing forum post takes its own code path - reopen,
        # send, edit, keep the post - so it gets its own proof that no event
        # content reaches the logs, including when the send is refused.
        title = "SECRET EVENT TITLE"
        description = "SECRET EVENT DESCRIPTION"
        post = FakeForumPost(archived=True)
        bot = forum_post_bot(store, post)
        with caplog.at_level("DEBUG"):
            event = store.create_event(
                category=EventCategory.FRACTAL,
                title=title,
                description=description,
                channel_id=post.id,
                leader_discord_id=42,
                start_time=START,
                duration_minutes=90,
                repeat_frequency=RepeatFrequency.NONE,
                repeat_days=(),
            )
            occurrence = store.create_occurrence(
                event.event_id,
                event.start_time,
            )
            posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
            await complete_signup(bot, event, posted, 11, EventRole.DPS, ())
            await refresh_occurrence_message(
                bot,
                event,
                posted,
                START + timedelta(hours=3),
            )
            await delete_event_posts(bot, event, [posted])
            post.send_error = forbidden_error(50013)
            retry = store.create_occurrence(
                event.event_id,
                START + timedelta(days=1),
            )
            with pytest.raises(discord.HTTPException):
                await post_occurrence(bot, event, retry, BEFORE_START)

        assert title not in caplog.text
        assert description not in caplog.text

    async def test_reshuffle_and_notification_logs_never_contain_content(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        title = "SECRET EVENT TITLE"
        description = "SECRET EVENT DESCRIPTION"
        with caplog.at_level("DEBUG"):
            event = store.create_event(
                category=EventCategory.FRACTAL,
                title=title,
                description=description,
                channel_id=1234,
                leader_discord_id=42,
                start_time=START,
                duration_minutes=90,
                repeat_frequency=RepeatFrequency.NONE,
                repeat_days=(),
            )
            occurrence = store.create_occurrence(
                event.event_id,
                event.start_time,
            )
            occurrence = await post_occurrence(
                bot,
                event,
                occurrence,
                BEFORE_START,
            )
            await complete_signup(
                bot,
                event,
                occurrence,
                11,
                EventRole.QUICKNESS_DPS,
                (EventRole.DPS,),
            )
            # Reshuffles the flexer and sends the ping.
            await complete_signup(
                bot,
                event,
                occurrence,
                12,
                EventRole.QUICKNESS_DPS,
                (),
            )
            # Snap-back with a failing ping still logs only counts and types.
            channel.thread.send = AsyncMock(
                side_effect=forbidden_error(50001)
            )
            await remove_signup(bot, event, occurrence, 12)

        assert title not in caplog.text
        assert description not in caplog.text
        # The decisions stay traceable end to end without user content.
        assert "Resolved signup seating" in caplog.text
        assert "Resettled event roster" in caplog.text
        assert "Sent roster update notification" in caplog.text
        assert "Could not send roster update notification" in caplog.text

    async def test_thread_cleanup_is_traceable_end_to_end(
        self,
        bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        occurrences = store.get_event_occurrences(event.event_id)

        with caplog.at_level("DEBUG", logger="gw2bot.events.posting"):
            await delete_event_posts(bot, event, occurrences)

        # A successful thread delete is an external Discord action, so it must
        # leave a trace rather than only being visible when it fails.
        assert (
            f"Deleted event thread; occurrence_id={posted.occurrence_id}"
            in caplog.text
        )

    async def test_an_occurrence_without_a_thread_logs_the_skip(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        channel.create_thread_error = forbidden_error(50001)
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        assert posted.thread_id is None
        occurrences = store.get_event_occurrences(event.event_id)

        with caplog.at_level("DEBUG", logger="gw2bot.events.posting"):
            await delete_event_posts(bot, event, occurrences)

        # The skip is recorded too, so a post that never got a thread is
        # distinguishable from one whose cleanup silently did nothing.
        assert (
            f"No event thread to delete; skipping; "
            f"occurrence_id={posted.occurrence_id}" in caplog.text
        )

    async def test_missing_manage_threads_logs_actionable_permission_diagnostics(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        channel.thread.delete = AsyncMock(side_effect=forbidden_error(50013))
        occurrences = store.get_event_occurrences(event.event_id)

        with caplog.at_level("ERROR", logger="gw2bot.events.posting"):
            await delete_event_posts(bot, event, occurrences)

        # Deleting a thread needs Manage Threads (README documents this for
        # /event channels); a deployment missing it must get a log that names
        # the permission, not just an opaque error type.
        assert (
            "Could not delete event thread; reason=missing_permissions "
            f"occurrence_id={posted.occurrence_id} "
            "required_permissions=manage_threads "
            "(type=Forbidden status=403 code=50013)" in caplog.text
        )
