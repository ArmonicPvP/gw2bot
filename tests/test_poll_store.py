from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gw2bot.polls.store import PollStore

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path):
    store = PollStore(str(tmp_path / "gw2bot.db"))
    yield store
    store.close()


def _create(store: PollStore, *, allow_multiple: bool = False, duration: int = 60):
    return store.create_poll(
        guild_id=1,
        channel_id=2,
        creator_discord_id=3,
        title="Favourite mount?",
        options=("Griffon", "Skyscale", "Roller Beetle"),
        allow_multiple=allow_multiple,
        duration_minutes=duration,
        now=NOW,
    )


def test_create_poll_sets_end_time_from_duration(store: PollStore) -> None:
    poll = _create(store, duration=90)

    assert poll.poll_id > 0
    assert poll.options == ("Griffon", "Skyscale", "Roller Beetle")
    assert poll.message_id is None
    assert poll.created_at == NOW
    assert poll.end_time == NOW + timedelta(minutes=90)


def test_set_and_get_poll_by_message(store: PollStore) -> None:
    poll = _create(store)
    store.set_poll_message(poll.poll_id, channel_id=99, message_id=555)

    by_id = store.get_poll(poll.poll_id)
    by_message = store.get_poll_by_message(555)

    assert by_id is not None and by_id.message_id == 555
    assert by_id.channel_id == 99
    assert by_message is not None and by_message.poll_id == poll.poll_id
    assert store.get_poll_by_message(111) is None


def test_votes_count_and_user_options(store: PollStore) -> None:
    poll = _create(store)
    store.add_vote(poll.poll_id, 0, 10, now=NOW)
    store.add_vote(poll.poll_id, 0, 11, now=NOW)
    store.add_vote(poll.poll_id, 2, 10, now=NOW)

    assert store.get_vote_counts(poll.poll_id) == {0: 2, 2: 1}
    assert store.get_user_options(poll.poll_id, 10) == [0, 2]

    store.remove_vote(poll.poll_id, 0, 10)
    assert store.get_vote_counts(poll.poll_id) == {0: 1, 2: 1}


def test_add_vote_is_idempotent_and_refreshes_timestamp(store: PollStore) -> None:
    poll = _create(store)
    store.add_vote(poll.poll_id, 1, 10, now=NOW)
    later = NOW + timedelta(minutes=5)
    store.add_vote(poll.poll_id, 1, 10, now=later)

    assert store.get_vote_counts(poll.poll_id) == {1: 1}
    times = store.get_user_option_times(poll.poll_id)
    assert times[10] == [(1, later)]


def test_replace_votes_reconciles_to_exact_set(store: PollStore) -> None:
    poll = _create(store)
    store.add_vote(poll.poll_id, 0, 10, now=NOW)
    store.add_vote(poll.poll_id, 1, 11, now=NOW)

    store.replace_votes(poll.poll_id, {0: {11}, 2: {12}}, now=NOW)

    assert store.get_vote_counts(poll.poll_id) == {0: 1, 2: 1}
    assert store.get_user_options(poll.poll_id, 11) == [0]
    assert store.get_user_options(poll.poll_id, 10) == []


def test_get_expired_polls_filters_by_time_and_posted(store: PollStore) -> None:
    posted_over = _create(store, duration=30)
    store.set_poll_message(posted_over.poll_id, channel_id=2, message_id=1)
    posted_future = store.create_poll(
        guild_id=1,
        channel_id=2,
        creator_discord_id=3,
        title="Still running",
        options=("A", "B"),
        allow_multiple=False,
        duration_minutes=600,
        now=NOW,
    )
    store.set_poll_message(posted_future.poll_id, channel_id=2, message_id=2)
    # Expired but never posted: excluded because it has no message to finalize.
    _create(store, duration=30)

    after_expiry = NOW + timedelta(hours=1)
    expired = store.get_expired_polls(after_expiry)

    assert [poll.poll_id for poll in expired] == [posted_over.poll_id]


def test_update_poll_changes_fields(store: PollStore) -> None:
    poll = _create(store)
    new_end = NOW + timedelta(hours=5)
    updated = store.update_poll(
        poll_id=poll.poll_id,
        title="New question",
        options=("Yes", "No"),
        allow_multiple=True,
        end_time=new_end,
    )

    assert updated.title == "New question"
    assert updated.options == ("Yes", "No")
    assert updated.allow_multiple is True
    assert updated.end_time == new_end


def test_delete_poll_removes_votes(store: PollStore) -> None:
    poll = _create(store)
    store.add_vote(poll.poll_id, 0, 10, now=NOW)

    store.delete_poll(poll.poll_id)

    assert store.get_poll(poll.poll_id) is None
    assert store.get_vote_counts(poll.poll_id) == {}


def test_get_active_polls_orders_newest_first(store: PollStore) -> None:
    first = _create(store)
    second = _create(store)

    active = store.get_active_polls()

    assert [poll.poll_id for poll in active] == [second.poll_id, first.poll_id]
