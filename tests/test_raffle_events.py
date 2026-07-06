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

from factories import (
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
