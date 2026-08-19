from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
from cryptography.fernet import Fernet

from gw2bot.config import Config, bootstrap_from_env
from gw2bot.raffle import RaffleTotal
from gw2bot.settings.composition import compose_config
from gw2bot.settings.crypto import SettingsCipher
from gw2bot.settings.definitions import LEGACY_SETTINGS, setting_key
from gw2bot.settings.store import SettingsStore

"""Shared builders for fake GW2 guild-log events used across test modules."""

# Guild Wars 2 guild ids are GUIDs, and /settings checks the shape before
# storing one, so anything that reaches the settings parser needs a real
# one rather than a readable label.
GW2_GUILD_ID = "116e0c0e-0035-44a9-bb22-4ae3e23127e5"
OTHER_GW2_GUILD_ID = "22c7b3a1-9f4e-4d18-8a05-6b1c0f2d7e93"


def gold_deposit(
    event_id: int,
    username: str = "Username.1234",
    coins: int = 10_000,
    event_time: str = "2026-06-07T06:26:17.000Z",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": event_time,
        "type": "stash",
        "user": username,
        "operation": "deposit",
        "coins": coins,
        "item_id": 0,
        "count": 0,
    }


def guild_leave(
    event_id: int,
    username: str = "Username.1234",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": "2026-06-07T06:26:17.000Z",
        "type": "kick",
        "user": username,
        "kicked_by": username,
    }


def guild_kick(
    event_id: int,
    username: str = "Kicked.1234",
    kicked_by: str = "Officer.5678",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": "2026-06-07T06:26:17.000Z",
        "type": "kick",
        "user": username,
        "kicked_by": kicked_by,
    }


def guild_join(
    event_id: int,
    username: str = "Username.1234",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": "2026-06-07T06:26:17.000Z",
        "type": "joined",
        "user": username,
    }


def guild_invite(
    event_id: int,
    username: str = "Invited.1234",
    invited_by: str = "Officer.5678",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": "2026-06-07T06:26:17.000Z",
        "type": "invited",
        "user": username,
        "invited_by": invited_by,
    }


def guild_rank_change(
    event_id: int,
    username: str = "Member.1234",
    old_rank: str = "Trial",
    new_rank: str = "Sunborne",
    changed_by: str = "Officer.5678",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": "2026-06-07T06:26:17.000Z",
        "type": "rank_change",
        "user": username,
        "old_rank": old_rank,
        "new_rank": new_rank,
        "changed_by": changed_by,
    }

def config_from_env(values: Mapping[str, str]) -> Config:
    """Compose the Config a given environment would produce after migration.

    Walks the same path startup does - bootstrap the environment-only values,
    then compose the settings on top - with the legacy variables standing in
    for the settings rows the one-time import would have written. It is how a
    test says "this is the environment an operator had" without building a
    database for it.
    """
    bootstrap = bootstrap_from_env(values)
    raw_values: dict[str, str] = {}
    for definition in LEGACY_SETTINGS:
        variable = definition.legacy_variable
        assert variable is not None
        value = values.get(variable, "").strip()
        if value:
            raw_values[setting_key(definition)] = value
    return compose_config(bootstrap, raw_values)


def default_config(**overrides: Any) -> Config:
    """Config carrying the shipped defaults for every Discord role and channel.

    The role, channel and forum-tag ids commands gate on are settings now, read
    off the bot's config rather than imported as constants, so every fake bot
    driving one of those commands needs a config to read them from.
    """
    overrides.setdefault("discord_token", "discord-token")
    overrides.setdefault("discord_command_guild_id", 5678)
    return Config(**overrides)


def configured_bot(**attributes: Any) -> SimpleNamespace:
    """Fake bot whose GW2 configuration guards let a command run.

    Commands that look guild members up ask the bot whether GW2_API_KEY and
    GW2_GUILD_ID are set before doing any work, so every fake bot driving one
    of those commands has to answer.
    """
    attributes.setdefault("_config", default_config())
    return SimpleNamespace(
        gw2_api_enabled=True,
        reject_without_gw2_api=AsyncMock(return_value=False),
        **attributes,
    )


def unconfigured_bot(**attributes: Any) -> SimpleNamespace:
    """Fake bot that reports GW2_API_KEY and GW2_GUILD_ID as unset."""
    attributes.setdefault("_config", default_config())
    return SimpleNamespace(
        gw2_api_enabled=False,
        reject_without_gw2_api=AsyncMock(return_value=True),
        **attributes,
    )


def forbidden_error(code: int) -> discord.Forbidden:
    response = SimpleNamespace(status=403, reason="Forbidden")
    return discord.Forbidden(
        response,  # type: ignore[arg-type]
        {"code": code, "message": "Missing Access"},
    )


def not_found_error() -> discord.NotFound:
    response = SimpleNamespace(status=404, reason="Not Found")
    return discord.NotFound(
        response,  # type: ignore[arg-type]
        {"code": 10007, "message": "Unknown Member"},
    )


def raffle_total(
    username: str,
    *,
    purchased: int = 0,
    free: int = 0,
) -> RaffleTotal:
    return RaffleTotal(
        username=username,
        coins_deposited=purchased * 10_000,
        raffle_tickets=purchased + free,
        gold_raffle_tickets=purchased,
        manual_raffle_tickets=free,
    )


def settings_store(tmp_path: Path) -> SettingsStore:
    """Settings store on a throwaway database, with a throwaway key."""
    return SettingsStore(
        str(tmp_path / "gw2bot.db"),
        SettingsCipher(Fernet.generate_key()),
    )


def settings_interaction(
    *,
    role_ids: tuple[int, ...] = (),
    owner: bool = False,
    administrator: bool = False,
    guild: Any = None,
    user_id: int = 1234,
) -> Any:
    """Interaction for a /settings command, with the gate's three arms.

    The officer role, the server owner and Administrator each let a caller
    through on their own, so a test has to be able to build a caller holding
    exactly one of them.
    """
    if guild is None:
        guild = SimpleNamespace(
            id=5678,
            owner_id=user_id if owner else 999,
            roles=(),
            channels=(),
            get_role=lambda role_id: None,
            get_channel=lambda channel_id: None,
        )
    elif owner:
        guild.owner_id = user_id
    user = SimpleNamespace(
        id=user_id,
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
        guild_permissions=SimpleNamespace(administrator=administrator),
    )
    response = _FakeInteractionResponse()
    return SimpleNamespace(
        user=user,
        guild=guild,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


class _FakeInteractionResponse:
    """Discord's response half, with the one behaviour the flow depends on.

    Deferring makes ``is_done()`` true, which is what sends every later notice
    through the followup instead. A double that always answered false would
    hide the difference the commands actually rely on.
    """

    def __init__(self) -> None:
        self._done = False
        self.send_message = AsyncMock(side_effect=self._finish)
        self.defer = AsyncMock(side_effect=self._finish)

    async def _finish(self, *args: Any, **kwargs: Any) -> None:
        self._done = True

    def is_done(self) -> bool:
        return self._done


def settings_reply(interaction: Any) -> str:
    """The single ephemeral message a /settings command replied with.

    A command that defers first answers through the followup, so this looks
    wherever the reply actually went rather than assuming.
    """
    if interaction.followup.send.await_count:
        interaction.followup.send.assert_awaited_once()
        call = interaction.followup.send.await_args
    else:
        interaction.response.send_message.assert_awaited_once()
        call = interaction.response.send_message.await_args
    assert call.kwargs["ephemeral"] is True
    return call.args[0]
