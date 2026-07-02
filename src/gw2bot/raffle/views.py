from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp
import discord

from gw2bot.raffle.formatting import (
    RAFFLE_TICKETS_PAGE_SIZE,
    RaffleTicketTableRow,
    order_raffle_totals,
    parse_squad_attendance_usernames,
    raffle_contribution_table_rows,
    raffle_ticket_embed,
    raffle_ticket_list_embed,
    raffle_ticket_table_embed,
    raffle_tier_summary_embed,
)
from gw2bot.raffle.models import RaffleContribution, RaffleTotal
from gw2bot.raffle.roles import RAFFLE_ADDTICKET_ROLE_ID

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot
    from gw2bot.raffle.commands import RaffleCommands

LOGGER = logging.getLogger(__name__)

RAFFLE_BULK_MODAL_MAX_LENGTH = 4_000


class RaffleTicketTablePageButton(discord.ui.Button["RaffleTicketTableView"]):
    def __init__(self, direction: int):
        super().__init__(
            label="<" if direction < 0 else ">",
            style=discord.ButtonStyle.secondary,
        )
        self._direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is not None:
            await self.view.change_page(interaction, self._direction)


class RaffleTicketTableView(discord.ui.View):
    def __init__(self, rows: list[RaffleTicketTableRow], title: str):
        super().__init__(timeout=180)
        self._rows = rows
        self._title = title
        self._page = 0
        self._previous = RaffleTicketTablePageButton(-1)
        self._next = RaffleTicketTablePageButton(1)
        self.add_item(self._previous)
        self.add_item(self._next)
        self._sync_buttons()

    @property
    def embed(self) -> discord.Embed:
        return raffle_ticket_table_embed(self._rows, self._title, self._page)

    async def change_page(
        self,
        interaction: discord.Interaction,
        direction: int,
    ) -> None:
        page_count = (
            len(self._rows) + RAFFLE_TICKETS_PAGE_SIZE - 1
        ) // RAFFLE_TICKETS_PAGE_SIZE
        self._page = max(0, min(self._page + direction, page_count - 1))
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed, view=self)

    def _sync_buttons(self) -> None:
        page_count = (
            len(self._rows) + RAFFLE_TICKETS_PAGE_SIZE - 1
        ) // RAFFLE_TICKETS_PAGE_SIZE
        self._previous.disabled = self._page == 0
        self._next.disabled = self._page >= page_count - 1


class RaffleTicketsPageButton(discord.ui.Button["RaffleTicketsListView"]):
    def __init__(self, direction: int):
        super().__init__(
            label="<" if direction < 0 else ">",
            style=discord.ButtonStyle.secondary,
        )
        self._direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is not None:
            await self.view.change_page(interaction, self._direction)


class RaffleTicketsListView(discord.ui.View):
    def __init__(self, totals: list[RaffleTotal]):
        super().__init__(timeout=180)
        self._totals = order_raffle_totals(totals)
        self._summary_embed = raffle_tier_summary_embed(totals)
        self._page = 0
        self._previous = RaffleTicketsPageButton(-1)
        self._next = RaffleTicketsPageButton(1)
        self.add_item(self._previous)
        self.add_item(self._next)
        self._sync_buttons()

    async def change_page(
        self,
        interaction: discord.Interaction,
        direction: int,
    ) -> None:
        page_count = (
            len(self._totals) + RAFFLE_TICKETS_PAGE_SIZE - 1
        ) // RAFFLE_TICKETS_PAGE_SIZE
        self._page = max(0, min(self._page + direction, page_count - 1))
        LOGGER.debug(
            "Changing raffle ticket list page; direction=%s page=%s page_count=%s",
            direction,
            self._page + 1,
            page_count,
        )
        self._sync_buttons()
        await interaction.response.edit_message(
            embeds=[
                self._summary_embed,
                raffle_ticket_list_embed(self._totals, self._page),
            ],
            view=self,
        )

    def _sync_buttons(self) -> None:
        page_count = (
            len(self._totals) + RAFFLE_TICKETS_PAGE_SIZE - 1
        ) // RAFFLE_TICKETS_PAGE_SIZE
        self._previous.disabled = self._page == 0
        self._next.disabled = self._page >= page_count - 1


class RaffleContributionReportView(RaffleTicketTableView):
    def __init__(self, contributions: list[RaffleContribution]):
        super().__init__(
            raffle_contribution_table_rows(contributions),
            "Raffle contributions from the last 6 hours",
        )


class RaffleAccountLinkModal(discord.ui.Modal):
    def __init__(self, bot: Gw2Bot):
        super().__init__(title="Link GW2 Account")
        self._bot = bot
        self.username = discord.ui.TextInput(
            label="GW2 account name",
            placeholder="Username.1234",
            min_length=6,
            max_length=42,
        )
        self.add_item(self.username)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            canonical_username = await self._bot.resolve_guild_member(
                self.username.value,
                force_refresh=True,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError):
            LOGGER.error("Could not refresh the guild member cache")
            await interaction.followup.send(
                "Could not verify guild membership. Try again later.",
                ephemeral=True,
            )
            return

        if canonical_username is None:
            await interaction.followup.send(
                f"`{self.username.value}` is not a member of the configured guild.",
                ephemeral=True,
            )
            return

        self._bot.link_raffle_account(interaction.user.id, canonical_username)
        await interaction.followup.send(
            f"Linked your Discord account to **{canonical_username}**.",
            embed=raffle_ticket_embed(
                self._bot.get_raffle_total(canonical_username)
            ),
            ephemeral=True,
        )


class RaffleBulkAddTicketsModal(discord.ui.Modal):
    def __init__(self, commands: RaffleCommands):
        super().__init__(title="Bulk Add Raffle Tickets")
        self._commands = commands
        self.attendance = discord.ui.TextInput(
            label="Squad attendance",
            style=discord.TextStyle.paragraph,
            placeholder=":Username.1234, Character Name",
            min_length=1,
            max_length=RAFFLE_BULK_MODAL_MAX_LENGTH,
        )
        self.add_item(self.attendance)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        LOGGER.debug(
            "Bulk attendance raffle ticket modal submitted; characters=%s",
            len(self.attendance.value),
        )
        if not await self._commands._bot.authorize_raffle_command(
            interaction,
            RAFFLE_ADDTICKET_ROLE_ID,
        ):
            return

        await interaction.response.defer(ephemeral=True)
        requested_usernames = parse_squad_attendance_usernames(
            self.attendance.value
        )
        if not requested_usernames:
            LOGGER.debug(
                "Bulk attendance raffle ticket modal rejected; usernames=0"
            )
            await interaction.followup.send(
                "No GW2 account names were found in the pasted attendance text.",
                ephemeral=True,
            )
            return
        await self._commands._add_tickets_for_usernames(
            interaction,
            requested_usernames,
        )
