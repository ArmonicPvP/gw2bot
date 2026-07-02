from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp
import discord
from discord import app_commands
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.discord_utils import user_has_role
from gw2bot.raffle.formatting import (
    RAFFLE_TICKETS_PAGE_SIZE,
    format_addticket_audit,
    format_bulk_addtickets_summary,
    format_removetickets_audit,
    format_raffle_result,
    raffle_contribution_table_rows,
    raffle_ticket_embed,
    raffle_ticket_list_embed,
    raffle_ticket_table_embed,
    raffle_tier_summary_embed,
)
from gw2bot.raffle.views import (
    RaffleAccountLinkModal,
    RaffleBulkAddTicketsModal,
    RaffleTicketTableView,
    RaffleTicketsListView,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot
from gw2bot.raffle.roles import (
    RAFFLE_ADDTICKET_ROLE_ID as RAFFLE_ADDTICKET_ROLE_ID,
    RAFFLE_DRAW_ROLE_ID as RAFFLE_DRAW_ROLE_ID,
    RAFFLE_OFFICER_ROLE_ID as RAFFLE_OFFICER_ROLE_ID,
)

LOGGER = logging.getLogger(__name__)


class RaffleCommands(app_commands.Group):
    def __init__(self, bot: Gw2Bot):
        super().__init__(
            name="raffle",
            description="Manage the guild raffle",
            guild_only=True,
        )
        self._bot = bot

    async def guild_member_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not (
            user_has_role(interaction.user, RAFFLE_ADDTICKET_ROLE_ID)
            or user_has_role(interaction.user, RAFFLE_OFFICER_ROLE_ID)
        ):
            LOGGER.debug(
                "Skipped raffle guild member autocomplete; authorized=false"
            )
            return []
        try:
            usernames = await self._bot.search_guild_members(current, limit=25)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            LOGGER.error("Could not refresh the guild member cache for autocomplete")
            return []
        LOGGER.debug(
            "Returning raffle guild member autocomplete choices; choices=%s",
            len(usernames),
        )
        return [
            app_commands.Choice(name=username, value=username)
            for username in usernames
        ]

    async def _add_tickets_for_usernames(
        self,
        interaction: discord.Interaction,
        requested_usernames: list[str],
    ) -> None:
        LOGGER.debug(
            "Bulk manual raffle ticket processing started; requested=%s",
            len(requested_usernames),
        )
        canonical_usernames: list[str] = []
        invalid_usernames: list[str] = []
        duplicate_usernames: list[str] = []
        seen_usernames: set[str] = set()
        try:
            for username in requested_usernames:
                canonical_username = await self._bot.resolve_guild_member(username)
                if canonical_username is None:
                    invalid_usernames.append(username)
                    continue
                username_key = canonical_username.casefold()
                if username_key in seen_usernames:
                    duplicate_usernames.append(canonical_username)
                    continue
                seen_usernames.add(username_key)
                canonical_usernames.append(canonical_username)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            LOGGER.error("Could not refresh the guild member cache")
            await interaction.followup.send(
                "Could not verify guild membership. No tickets were added. "
                "Try again later.",
                ephemeral=True,
            )
            return

        added_usernames: list[str] = []
        failed_usernames: list[str] = []
        audit_failures = 0
        for canonical_username in canonical_usernames:
            try:
                self._bot.add_manual_raffle_ticket(canonical_username)
            except ValueError:
                failed_usernames.append(canonical_username)
                LOGGER.debug("Bulk manual raffle ticket addition skipped; added=false")
                continue

            added_usernames.append(canonical_username)
            audit_message = format_addticket_audit(
                interaction.user.id,
                canonical_username,
            )
            LOGGER.info("%s", audit_message)
            audit_sent = await self._bot.send_notification(audit_message)
            if not audit_sent:
                audit_failures += 1
            LOGGER.debug("Bulk manual raffle ticket audit delivered=%s", audit_sent)

        LOGGER.debug(
            "Bulk manual raffle ticket processing completed; requested=%s valid=%s "
            "added=%s invalid=%s duplicates=%s add_failures=%s audit_failures=%s",
            len(requested_usernames),
            len(canonical_usernames),
            len(added_usernames),
            len(invalid_usernames),
            len(duplicate_usernames),
            len(failed_usernames),
            audit_failures,
        )
        await interaction.followup.send(
            format_bulk_addtickets_summary(
                added_usernames,
                len(invalid_usernames),
                duplicate_usernames,
                failed_usernames,
                audit_failures,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="draw", description="Draw a weighted raffle winner")
    async def draw(self, interaction: discord.Interaction) -> None:
        LOGGER.debug(
            "Raffle draw command invoked by Discord user %s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
        )
        if not await self._bot.authorize_raffle_command(
            interaction,
            RAFFLE_DRAW_ROLE_ID,
        ):
            return

        await interaction.response.defer()
        result = self._bot.get_pending_raffle_result()
        if result is None:
            LOGGER.debug("No pending raffle result; refreshing guild log")
            try:
                await self._bot.refresh_guild_log()
            except (aiohttp.ClientError, asyncio.TimeoutError, SQLAlchemyError):
                LOGGER.exception("Could not refresh the guild log before raffle draw")
                await interaction.followup.send(
                    "Could not refresh guild deposits. No raffle was drawn.",
                    ephemeral=True,
                )
                return
            result = self._bot.run_raffle()
        if result is None:
            LOGGER.debug("Raffle draw command found no tickets")
            await interaction.followup.send(
                "The raffle has no tickets.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(format_raffle_result(result))
        self._bot.mark_raffle_announcement_sent(result.run_id)
        LOGGER.debug("Raffle draw command announced run %s", result.run_id)

    @app_commands.command(
        name="addticket",
        description="Add a raffle ticket or record an Officer ticket purchase",
    )
    @app_commands.describe(
        username="Guild Wars 2 account name, including the four digits",
        amount="Purchased tickets to add; Officers only",
    )
    @app_commands.autocomplete(username=guild_member_autocomplete)
    async def addticket(
        self,
        interaction: discord.Interaction,
        username: str,
        amount: int | None = None,
    ) -> None:
        LOGGER.debug(
            "Raffle ticket addition invoked by Discord user %s; "
            "purchase_amount_supplied=%s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
            amount is not None,
        )
        required_role_id = (
            RAFFLE_OFFICER_ROLE_ID
            if amount is not None
            else RAFFLE_ADDTICKET_ROLE_ID
        )
        if not await self._bot.authorize_raffle_command(
            interaction,
            required_role_id,
        ):
            return

        await interaction.response.defer(ephemeral=True)
        try:
            canonical_username = await self._bot.resolve_guild_member(username)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            LOGGER.error("Could not refresh the guild member cache")
            await interaction.followup.send(
                "Could not verify guild membership. Try again later.",
                ephemeral=True,
            )
            return

        if canonical_username is None:
            LOGGER.debug("Manual raffle ticket rejected; guild member was not found")
            await interaction.followup.send(
                f"`{username}` is not a member of the configured guild.",
                ephemeral=True,
            )
            return

        if amount is not None:
            try:
                total = await self._bot.add_officer_raffle_purchase(
                    canonical_username,
                    amount,
                )
            except ValueError as exc:
                LOGGER.debug("Officer raffle purchase rejected; added=false")
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            LOGGER.debug(
                "Officer raffle purchase command completed; amount=%s "
                "current_purchased=%s current_total=%s",
                amount,
                total.gold_raffle_tickets,
                total.raffle_tickets,
            )
            await interaction.followup.send(
                f"Recorded **{amount} gold** deposited by "
                f"**{canonical_username}** and added {amount} purchased raffle "
                f"{'ticket' if amount == 1 else 'tickets'}. They now have "
                f"{total.gold_raffle_tickets} purchased and "
                f"{total.raffle_tickets} total current tickets.",
                ephemeral=True,
            )
            return

        try:
            total = self._bot.add_manual_raffle_ticket(canonical_username)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        audit_message = format_addticket_audit(
            interaction.user.id,
            canonical_username,
        )
        LOGGER.info("%s", audit_message)
        audit_sent = await self._bot.send_notification(audit_message)
        LOGGER.debug("Manual raffle ticket audit delivered=%s", audit_sent)
        await interaction.followup.send(
            f"Added one raffle ticket to **{canonical_username}**. "
            f"They now have {total.raffle_tickets} current tickets."
            + ("" if audit_sent else " The audit log could not be delivered."),
            ephemeral=True,
        )

    @app_commands.command(
        name="addtickets",
        description="Add one raffle ticket to up to ten guild members",
    )
    @app_commands.describe(
        username1="First Guild Wars 2 account name",
        username2="Second Guild Wars 2 account name",
        username3="Third Guild Wars 2 account name",
        username4="Fourth Guild Wars 2 account name",
        username5="Fifth Guild Wars 2 account name",
        username6="Sixth Guild Wars 2 account name",
        username7="Seventh Guild Wars 2 account name",
        username8="Eighth Guild Wars 2 account name",
        username9="Ninth Guild Wars 2 account name",
        username10="Tenth Guild Wars 2 account name",
    )
    @app_commands.autocomplete(
        username1=guild_member_autocomplete,
        username2=guild_member_autocomplete,
        username3=guild_member_autocomplete,
        username4=guild_member_autocomplete,
        username5=guild_member_autocomplete,
        username6=guild_member_autocomplete,
        username7=guild_member_autocomplete,
        username8=guild_member_autocomplete,
        username9=guild_member_autocomplete,
        username10=guild_member_autocomplete,
    )
    async def addtickets(
        self,
        interaction: discord.Interaction,
        username1: str | None = None,
        username2: str | None = None,
        username3: str | None = None,
        username4: str | None = None,
        username5: str | None = None,
        username6: str | None = None,
        username7: str | None = None,
        username8: str | None = None,
        username9: str | None = None,
        username10: str | None = None,
    ) -> None:
        requested_usernames = [
            username
            for username in (
                username1,
                username2,
                username3,
                username4,
                username5,
                username6,
                username7,
                username8,
                username9,
                username10,
            )
            if username is not None and username.strip()
        ]
        LOGGER.debug(
            "Bulk manual raffle ticket command invoked; requested=%s",
            len(requested_usernames),
        )
        if not await self._bot.authorize_raffle_command(
            interaction,
            RAFFLE_ADDTICKET_ROLE_ID,
        ):
            return
        if not requested_usernames:
            LOGGER.debug("Bulk manual raffle ticket command rejected; requested=0")
            await interaction.response.send_message(
                "Select at least one guild member.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self._add_tickets_for_usernames(interaction, requested_usernames)

    @app_commands.command(
        name="bulkaddtickets",
        description="Paste squad attendance to add raffle tickets",
    )
    async def bulkaddtickets(self, interaction: discord.Interaction) -> None:
        LOGGER.debug("Bulk attendance raffle ticket command invoked")
        if not await self._bot.authorize_raffle_command(
            interaction,
            RAFFLE_ADDTICKET_ROLE_ID,
        ):
            return
        await interaction.response.send_modal(RaffleBulkAddTicketsModal(self))

    @app_commands.command(
        name="removetickets",
        description="Remove purchased raffle tickets from a guild member",
    )
    @app_commands.describe(
        username="Guild Wars 2 account name, including the four digits",
        amount="Number of purchased tickets to remove",
    )
    async def removetickets(
        self,
        interaction: discord.Interaction,
        username: str,
        amount: int = 1,
    ) -> None:
        LOGGER.debug(
            "Purchased raffle ticket removal invoked by Discord user %s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
        )
        if not await self._bot.authorize_raffle_command(
            interaction,
            RAFFLE_DRAW_ROLE_ID,
        ):
            return

        await interaction.response.defer(ephemeral=True)
        try:
            canonical_username = await self._bot.resolve_guild_member(username)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            LOGGER.error("Could not refresh the guild member cache")
            await interaction.followup.send(
                "Could not verify guild membership. Try again later.",
                ephemeral=True,
            )
            return

        if canonical_username is None:
            await interaction.followup.send(
                f"`{username}` is not a member of the configured guild.",
                ephemeral=True,
            )
            return

        try:
            total = self._bot.remove_gold_raffle_tickets(
                canonical_username,
                amount,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        audit_message = format_removetickets_audit(
            interaction.user.id,
            canonical_username,
            amount,
        )
        LOGGER.info("%s", audit_message)
        audit_sent = await self._bot.send_notification(audit_message)
        await interaction.followup.send(
            f"Removed {amount} purchased raffle "
            f"{'ticket' if amount == 1 else 'tickets'} from "
            f"**{canonical_username}**. They now have "
            f"{total.gold_raffle_tickets} purchased and "
            f"{total.raffle_tickets} total current tickets."
            + ("" if audit_sent else " The audit log could not be delivered."),
            ephemeral=True,
        )

    @app_commands.command(
        name="tickets",
        description="View your or another player's tickets",
    )
    @app_commands.describe(
        username="Enter your GW2 account name, including the four digits",
    )
    async def tickets(
        self,
        interaction: discord.Interaction,
        username: str | None = None,
    ) -> None:
        if username is None:
            linked_username = self._bot.get_linked_raffle_username(
                interaction.user.id
            )
            if linked_username is None:
                await interaction.response.send_modal(
                    RaffleAccountLinkModal(self._bot)
                )
                return
            await interaction.response.send_message(
                embed=raffle_ticket_embed(
                    self._bot.get_raffle_total(linked_username)
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            canonical_username = await self._bot.resolve_guild_member(username)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            LOGGER.error("Could not refresh the guild member cache")
            await interaction.followup.send(
                "Could not verify guild membership. Try again later.",
                ephemeral=True,
            )
            return

        if canonical_username is None:
            await interaction.followup.send(
                f"`{username}` is not a member of the configured guild.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=raffle_ticket_embed(
                self._bot.get_raffle_total(canonical_username)
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="list",
        description="List all users' tickets",
    )
    async def list_tickets(self, interaction: discord.Interaction) -> None:
        totals = self._bot.get_raffle_totals()
        active_totals = [
            total for total in totals if total.raffle_tickets > 0
        ]
        LOGGER.debug(
            "Raffle list command invoked; records=%s active_players=%s",
            len(totals),
            len(active_totals),
        )
        view = (
            RaffleTicketsListView(active_totals)
            if len(active_totals) > RAFFLE_TICKETS_PAGE_SIZE
            else None
        )
        embeds = [
            raffle_tier_summary_embed(active_totals),
            raffle_ticket_list_embed(active_totals, 0),
        ]
        if view is None:
            await interaction.response.send_message(embeds=embeds)
        else:
            await interaction.response.send_message(embeds=embeds, view=view)

    @app_commands.command(
        name="leaderboard",
        description="List every user's lifetime earned and purchased tickets",
    )
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        contributions = self._bot.get_lifetime_raffle_contributions()
        LOGGER.debug(
            "Raffle leaderboard command invoked; contributors=%s",
            len(contributions),
        )
        if not contributions:
            await interaction.response.send_message(
                "No lifetime raffle tickets have been recorded yet."
            )
            return
        rows = raffle_contribution_table_rows(contributions)
        title = "Lifetime raffle tickets"
        view = (
            RaffleTicketTableView(rows, title)
            if len(rows) > RAFFLE_TICKETS_PAGE_SIZE
            else None
        )
        embed = raffle_ticket_table_embed(rows, title, 0)
        if view is None:
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(embed=embed, view=view)
