import pytest

from gw2bot.raffle import (
    RAFFLE_DRAW_TIERS,
    RAFFLE_REWARD_TIERS,
    format_gold,
    parse_gold_deposit,
    parse_guild_invite,
    parse_guild_join,
    parse_guild_leave,
    parse_guild_rank_change,
)
from gw2bot.raffle.events import parse_feast_deposit

from factories import (
    feast_deposit,
    gold_deposit,
    guild_invite,
    guild_join,
    guild_kick,
    guild_leave,
    guild_rank_change,
)


class TestRaffleModels:
    def test_default_reward_tiers_are_data_driven(self) -> None:
        assert [
            (tier.threshold, tier.name) for tier in RAFFLE_REWARD_TIERS
        ] == [
            (50, "Tier 1"),
            (100, "Tier 2"),
            (150, "Tier 3"),
            (200, "Tier 4"),
        ]
        assert [
            (tier.minimum_purchased_tickets, tier.winner_count)
            for tier in RAFFLE_DRAW_TIERS
        ] == [
            (0, 2),
            (50, 2),
            (100, 3),
            (150, 4),
            (200, 5),
        ]

    def test_formats_whole_and_fractional_gold(self) -> None:
        assert format_gold(10_000) == "1"
        assert format_gold(12_345) == "1.2345"


class TestRaffleEventParsing:
    def test_parses_gold_deposit_and_formats_message(self) -> None:
        deposit = parse_gold_deposit(gold_deposit(101, coins=35_000))

        assert deposit is not None
        assert deposit.raffle_tickets == 3
        assert (
            deposit.message
            == "Username.1234 deposited 3.5 gold and purchased 3 raffle tickets"
        )

    def test_tracks_partial_gold_but_ignores_non_deposit_events(self) -> None:
        partial = parse_gold_deposit(gold_deposit(101, coins=9_999))
        assert partial is not None
        assert partial.raffle_tickets == 0
        assert (
            parse_gold_deposit({**gold_deposit(102), "operation": "withdraw"}) is None
        )
        assert parse_gold_deposit({**gold_deposit(103), "type": "treasury"}) is None

    def test_parses_guild_leave_with_exact_message(self) -> None:
        leave = parse_guild_leave(guild_leave(104))

        assert leave is not None
        assert leave.message == "Username.1234 has left the guild."
        assert parse_guild_leave({**guild_leave(105), "type": "joined"}) is None

    def test_parses_guild_kick_with_exact_message(self) -> None:
        leave = parse_guild_leave(guild_kick(106))

        assert leave is not None
        assert leave.message == "Officer.5678 kicked Kicked.1234 from the guild."

    def test_parses_guild_join_with_exact_message(self) -> None:
        join = parse_guild_join(guild_join(104))

        assert join is not None
        assert join.message == "Username.1234 has joined the guild."
        assert parse_guild_join({**guild_join(105), "type": "invited"}) is None
        assert parse_guild_join({**guild_join(106), "user": ""}) is None

    def test_parses_guild_invite_with_exact_message(self) -> None:
        invite = parse_guild_invite(guild_invite(104))

        assert invite is not None
        assert invite.message == "Officer.5678 invited Invited.1234 to the guild."
        assert parse_guild_invite({**guild_invite(105), "type": "joined"}) is None
        assert parse_guild_invite({**guild_invite(106), "user": ""}) is None

    def test_parses_guild_invite_without_inviter(self) -> None:
        invite = parse_guild_invite({**guild_invite(104), "invited_by": ""})

        assert invite is not None
        assert invite.message == "Invited.1234 was invited to the guild."

    def test_parses_guild_rank_change_with_exact_message(self) -> None:
        rank_change = parse_guild_rank_change(guild_rank_change(104))

        assert rank_change is not None
        assert rank_change.message == (
            "Officer.5678 changed Member.1234's guild rank from Trial to Sunborne."
        )
        assert (
            parse_guild_rank_change({**guild_rank_change(105), "type": "joined"})
            is None
        )
        assert (
            parse_guild_rank_change({**guild_rank_change(106), "user": ""}) is None
        )

    def test_parses_self_or_unattributed_rank_change_without_actor(self) -> None:
        unattributed = parse_guild_rank_change(
            {**guild_rank_change(104), "changed_by": ""}
        )
        assert unattributed is not None
        assert unattributed.message == (
            "Member.1234's guild rank changed from Trial to Sunborne."
        )

        self_change = parse_guild_rank_change(
            {**guild_rank_change(105), "changed_by": "Member.1234"}
        )
        assert self_change is not None
        assert self_change.message == (
            "Member.1234's guild rank changed from Trial to Sunborne."
        )


class TestFeastDepositParsing:
    def test_reads_a_tracked_feast_deposit(self) -> None:
        parsed = parse_feast_deposit(
            feast_deposit(11, username="Cook.1234", count=25)
        )

        assert parsed is not None
        assert parsed.event_id == 11
        assert parsed.guild_storage_id == 1078
        assert parsed.username == "Cook.1234"
        assert parsed.count == 25
        assert parsed.event_time == "2026-06-07T06:26:17.000Z"

    def test_ignores_an_untracked_guild_upgrade(self) -> None:
        # Guild upgrades that are not one of the four feasts the dashboard
        # follows say nothing about the shelves it draws.
        event = feast_deposit(11, guild_storage_id=42)

        assert parse_feast_deposit(event) is None

    def test_ignores_an_upgrade_that_was_not_completed(self) -> None:
        event = feast_deposit(11)
        event["action"] = "queued"

        assert parse_feast_deposit(event) is None

    def test_ignores_an_event_naming_nobody(self) -> None:
        event = feast_deposit(11)
        event["user"] = ""

        assert parse_feast_deposit(event) is None

    def test_ignores_an_event_carrying_no_count(self) -> None:
        assert parse_feast_deposit(feast_deposit(11, count=0)) is None

    def test_ignores_an_unreadable_count_or_upgrade(self) -> None:
        unreadable = feast_deposit(11)
        unreadable["count"] = "many"
        assert parse_feast_deposit(unreadable) is None

        missing = feast_deposit(11)
        del missing["upgrade_id"]
        assert parse_feast_deposit(missing) is None

    def test_ignores_a_stash_event(self) -> None:
        assert parse_feast_deposit(gold_deposit(11)) is None

    @pytest.mark.parametrize(
        "event",
        [
            # The two `completed` upgrade events the GW2 wiki documents by
            # example, with the upgrade id swapped to a tracked feast so the
            # tracking filter is not what decides the outcome. The first is
            # the scribe-station shape, which carries a recipe_id beside the
            # count; feasts are scribed, so that is the shape this reads in
            # practice.
            {
                "id": 1470,
                "time": "2016-12-19T20:36:03.000Z",
                "type": "upgrade",
                "recipe_id": 11856,
                "upgrade_id": 1078,
                "count": 1,
                "action": "completed",
                "user": "Lawton Campbell.9413",
            },
            {
                "id": 1522,
                "time": "2016-12-19T20:48:11.000Z",
                "type": "upgrade",
                "upgrade_id": 1078,
                "count": 1,
                "action": "completed",
                "user": "Lawton Campbell.9413",
            },
        ],
    )
    def test_reads_the_event_shapes_the_api_documents(
        self,
        event: dict[str, object],
    ) -> None:
        # A `completed` upgrade carries a count - the wiki's field list says
        # the action "will also generate a new count field indicating how
        # many upgrades were added", and both of its examples show one. This
        # pins that contract: a deposit of a single feast is the smallest
        # real event there is, and it must not be read as nothing.
        parsed = parse_feast_deposit(event)

        assert parsed is not None
        assert parsed.guild_storage_id == 1078
        assert parsed.username == "Lawton Campbell.9413"
        assert parsed.count == 1
