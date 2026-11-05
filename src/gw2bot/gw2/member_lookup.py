"""Turn an account name a command was given into the guild member it names.

The raffle and Trial commands all take a GW2 account name, check it against
the in-game roster, and stop with the same two private replies when they
cannot: the roster could not be read, or the account is not in the guild.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import aiohttp
import discord

LOGGER = logging.getLogger(__name__)

MEMBERSHIP_UNVERIFIED = "Could not verify guild membership. Try again later."


class GuildMemberResolver(Protocol):
    async def resolve_guild_member(
        self,
        username: str,
        *,
        force_refresh: bool = False,
    ) -> str | None: ...


async def report_unverified_membership(
    interaction: discord.Interaction,
    error: BaseException,
    *,
    action: str,
) -> None:
    """Tell a deferred caller the roster could not be read.

    ``action`` names the command in the log line, which carries the error's
    type and nothing it says: a transport error's message can hold the
    request it failed on.
    """
    LOGGER.error(
        "Could not refresh the guild member cache; action=%s error_type=%s",
        action,
        type(error).__name__,
    )
    await interaction.followup.send(MEMBERSHIP_UNVERIFIED, ephemeral=True)


async def resolve_member_or_reply(
    resolver: GuildMemberResolver,
    interaction: discord.Interaction,
    username: str,
    *,
    action: str,
    force_refresh: bool = False,
) -> str | None:
    """The account's canonical name, or None once the caller has been told why.

    The interaction must already be deferred: the roster read can take longer
    than Discord waits for a first response, so every reply goes through the
    followup. ``action`` names the command in the log line and never carries
    the account name, which is the one thing here a caller typed.
    """
    try:
        canonical = await resolver.resolve_guild_member(
            username,
            force_refresh=force_refresh,
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        await report_unverified_membership(interaction, error, action=action)
        return None
    if canonical is None:
        LOGGER.debug("%s rejected; guild member was not found", action)
        await interaction.followup.send(
            f"`{username}` is not a member of the configured guild.",
            ephemeral=True,
        )
        return None
    return canonical
