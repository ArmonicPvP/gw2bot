"""Delivery to the configured notification channel.

An unconfigured channel is a skip logged at debug, not a retried failure: the
startup warning already named the `/settings` subcommand to run.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import discord

from gw2bot.config import NOTIFICATION_CHANNEL_SETTING
from gw2bot.core.discord_utils import discord_failure_reason, log_discord_failure
from gw2bot.settings.definitions import definition_for_variable

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


def format_legacy_configuration_warning(variables: Sequence[str]) -> str:
    """Name the variables /settings replaced, and how to migrate each one.

    Only the variable names appear. Several of them held credentials, and this
    message goes to a channel people can read and search.
    """
    lines = [
        "**These environment variables no longer configure the bot.**",
        "They are ignored. Set each value with the command beside it, then "
        "remove the variable from the environment so this notice stops.",
        "",
    ]
    for variable in variables:
        definition = definition_for_variable(variable)
        command = (
            definition.command_path
            if definition is not None
            else "/settings list"
        )
        lines.append(f"- `{variable}` → `{command}`")
    lines.append("")
    lines.append(
        "Run `/settings list` to see every setting and where its value came "
        "from. Values that hold a secret can never be read back once set."
    )
    return "\n".join(lines)


async def send_legacy_configuration_warning(
    bot: Gw2Bot,
    variables: Sequence[str],
) -> bool:
    LOGGER.debug(
        "Delivering the legacy configuration warning; variables=%s",
        len(variables),
    )
    # Through the bot, like every other delivery here, so an unconfigured
    # channel is skipped and logged at debug rather than retried.
    return await bot._try_send_notification(
        format_legacy_configuration_warning(variables)
    )


async def try_send_notification(bot: Gw2Bot, message: str) -> bool:
    if not bot._config.notifications_enabled:
        # The startup warning already named the variable; each skipped
        # delivery only needs to be traceable, not repeated at warning level.
        LOGGER.debug(
            "Skipped Discord notification; /settings %s is not set; "
            "characters=%s",
            NOTIFICATION_CHANNEL_SETTING,
            len(message),
        )
        return False
    LOGGER.debug("Sending Discord notification; characters=%s", len(message))
    try:
        await bot._send_notification(message)
    except discord.DiscordException as exc:
        log_discord_failure(
            "Could not send Discord notification; reason=%s channel_id=%s "
            "required_permissions=view_channel,send_messages",
            exc,
            discord_failure_reason(exc),
            bot._config.discord_notification_channel_id,
        )
        return False
    LOGGER.debug("Discord notification sent")
    return True


async def send_notification(bot: Gw2Bot, message: str) -> None:
    channel = await bot._get_notification_channel()
    await channel.send(message)

async def get_notification_channel(bot: Gw2Bot) -> Any:
    if bot._notification_channel is None:
        channel_id = bot._config.discord_notification_channel_id
        if channel_id is None:
            # Raised as a Discord error so every caller's existing failure
            # handling reports it instead of crashing a poll task.
            raise discord.ClientException(
                f"/settings {NOTIFICATION_CHANNEL_SETTING} is not set"
            )
        LOGGER.debug("Fetching Discord notification channel %s", channel_id)
        channel = await bot.fetch_channel(channel_id)
        if (
            getattr(getattr(channel, "guild", None), "id", None)
            != bot._config.discord_command_guild_id
        ):
            raise discord.ClientException(
                f"/settings {NOTIFICATION_CHANNEL_SETTING} must name a "
                "channel in DISCORD_COMMAND_GUILD_ID"
            )
        bot._notification_channel = channel
        LOGGER.debug("Cached Discord notification channel")
    return bot._notification_channel
