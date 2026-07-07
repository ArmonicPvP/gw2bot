from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, cast

import aiohttp
import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.raffle.formatting import (
    RAFFLE_AUDIT_RANGES_PAGE_SIZE,
    RAFFLE_TICKETS_PAGE_SIZE,
    RaffleTicketTableRow,
    order_raffle_totals,
    parse_squad_attendance_usernames,
    raffle_audit_embeds,
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


def _raffle_audit_ranges_page_count(entrant_count: int) -> int:
    return max(
        1,
        (entrant_count + RAFFLE_AUDIT_RANGES_PAGE_SIZE - 1)
        // RAFFLE_AUDIT_RANGES_PAGE_SIZE,
    )


class RaffleAuditRangesButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=(
        r"gw2bot:raffle-audit-ranges:(?P<run_id>[0-9]+):"
        r"(?P<page>[0-9]+):(?P<direction>-?1)"
    ),
):
    # Audit messages must stay browsable indefinitely, so the run id and
    # current page ride in the custom_id instead of view state; Discord
    # then rebuilds the button on dispatch and paging keeps working after
    # view timeouts and bot restarts.
    def __init__(
        self,
        run_id: int,
        page: int,
        direction: int,
        *,
        disabled: bool = False,
    ):
        self.run_id = run_id
        self.page = page
        self.direction = direction
        super().__init__(
            discord.ui.Button(
                label="<" if direction < 0 else ">",
                style=discord.ButtonStyle.secondary,
                custom_id=(
                    f"gw2bot:raffle-audit-ranges:{run_id}:{page}:{direction}"
                ),
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> RaffleAuditRangesButton:
        return cls(
            int(match["run_id"]),
            int(match["page"]),
            int(match["direction"]),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("Gw2Bot", interaction.client)
        try:
            audit = bot.get_raffle_audit(self.run_id)
        except SQLAlchemyError:
            LOGGER.error(
                "Could not load raffle audit for range paging; run_id=%s",
                self.run_id,
            )
            await interaction.response.send_message(
                "Could not load this raffle audit. Try again later.",
                ephemeral=True,
            )
            return
        if audit is None:
            LOGGER.debug(
                "Raffle audit range paging found no run; run_id=%s",
                self.run_id,
            )
            await interaction.response.send_message(
                f"Raffle run {self.run_id} is no longer recorded.",
                ephemeral=True,
            )
            return

        page_count = _raffle_audit_ranges_page_count(len(audit.entrants))
        page = max(0, min(self.page + self.direction, page_count - 1))
        LOGGER.debug(
            "Changing raffle audit ranges page; run_id=%s direction=%s "
            "page=%s page_count=%s",
            self.run_id,
            self.direction,
            page + 1,
            page_count,
        )
        await interaction.response.edit_message(
            embeds=raffle_audit_embeds(audit, page),
            view=RaffleAuditRangesView(
                self.run_id,
                len(audit.entrants),
                page,
            ),
        )


class RaffleAuditRangesView(discord.ui.View):
    def __init__(self, run_id: int, entrant_count: int, page: int = 0):
        # timeout=None marks the view persistent; every child carries a
        # custom_id, so dispatch survives bot restarts via the dynamic
        # button registration in Gw2Bot.
        super().__init__(timeout=None)
        page_count = _raffle_audit_ranges_page_count(entrant_count)
        page = max(0, min(page, page_count - 1))
        self.add_item(
            RaffleAuditRangesButton(run_id, page, -1, disabled=page == 0)
        )
        self.add_item(
            RaffleAuditRangesButton(
                run_id,
                page,
                1,
                disabled=page >= page_count - 1,
            )
        )


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
