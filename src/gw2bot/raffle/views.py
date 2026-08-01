from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import aiohttp
import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.raffle.formatting import (
    RAFFLE_AUDIT_RANGES_PAGE_SIZE,
    RAFFLE_TICKET_ROW_SORT_KEYS,
    RAFFLE_TICKETS_PAGE_SIZE,
    order_raffle_ticket_rows,
    order_raffle_totals,
    parse_squad_attendance_usernames,
    raffle_audit_embeds,
    raffle_contribution_report_embed,
    raffle_contribution_table_rows,
    raffle_leaderboard_title,
    raffle_ticket_embed,
    raffle_ticket_list_embed,
    raffle_ticket_table_embed,
    raffle_tier_summary_embed,
)
from gw2bot.raffle.roles import RAFFLE_ADDTICKET_ROLE_ID

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot
    from gw2bot.raffle.commands import RaffleCommands

LOGGER = logging.getLogger(__name__)

RAFFLE_BULK_MODAL_MAX_LENGTH = 4_000


# Every raffle pager below is persistent: state rides in each button's
# custom_id and the rows are reloaded from the store on click. A view holding
# its rows in memory would have to carry a timeout, and once that timeout
# elapsed discord.py would stop dispatching to it, so the arrows on a message
# that is still on screen would start failing. These pagers instead keep
# working for the life of the message, and across bot restarts.
RAFFLE_PAGER_LOAD_FAILURE = "Could not load this page. Try again later."


def _raffle_ticket_page_count(row_count: int) -> int:
    return max(
        1,
        (row_count + RAFFLE_TICKETS_PAGE_SIZE - 1) // RAFFLE_TICKETS_PAGE_SIZE,
    )


def _clamp_page(page: int, direction: int, page_count: int) -> int:
    return max(0, min(page + direction, page_count - 1))


class RaffleTicketsListButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=(
        r"gw2bot:raffle-tickets-list:(?P<page>[0-9]+):(?P<direction>-?1)"
    ),
):
    def __init__(self, page: int, direction: int, *, disabled: bool = False):
        self.page = page
        self.direction = direction
        super().__init__(
            discord.ui.Button(
                label="<" if direction < 0 else ">",
                style=discord.ButtonStyle.secondary,
                custom_id=f"gw2bot:raffle-tickets-list:{page}:{direction}",
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
    ) -> RaffleTicketsListButton:
        return cls(int(match["page"]), int(match["direction"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("Gw2Bot", interaction.client)
        try:
            totals = bot.get_raffle_totals()
        except SQLAlchemyError:
            LOGGER.error("Could not load raffle totals for ticket list paging")
            await interaction.response.send_message(
                RAFFLE_PAGER_LOAD_FAILURE,
                ephemeral=True,
            )
            return

        active_totals = order_raffle_totals(totals)
        page_count = _raffle_ticket_page_count(len(active_totals))
        page = _clamp_page(self.page, self.direction, page_count)
        LOGGER.debug(
            "Changing raffle ticket list page; direction=%s page=%s "
            "page_count=%s active_players=%s",
            self.direction,
            page + 1,
            page_count,
            len(active_totals),
        )
        await interaction.response.edit_message(
            embeds=[
                raffle_tier_summary_embed(active_totals),
                raffle_ticket_list_embed(active_totals, page),
            ],
            view=RaffleTicketsListView(len(active_totals), page),
        )


class RaffleTicketsListView(discord.ui.View):
    def __init__(self, active_count: int, page: int = 0):
        super().__init__(timeout=None)
        page_count = _raffle_ticket_page_count(active_count)
        page = max(0, min(page, page_count - 1))
        self.add_item(
            RaffleTicketsListButton(page, -1, disabled=page == 0)
        )
        self.add_item(
            RaffleTicketsListButton(
                page,
                1,
                disabled=page >= page_count - 1,
            )
        )


class RaffleLeaderboardButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=(
        r"gw2bot:raffle-leaderboard:(?P<sort_key>[a-z]+):"
        r"(?P<page>[0-9]+):(?P<direction>-?1)"
    ),
):
    def __init__(
        self,
        sort_key: str,
        page: int,
        direction: int,
        *,
        disabled: bool = False,
    ):
        self.sort_key = sort_key
        self.page = page
        self.direction = direction
        super().__init__(
            discord.ui.Button(
                label="<" if direction < 0 else ">",
                style=discord.ButtonStyle.secondary,
                custom_id=(
                    f"gw2bot:raffle-leaderboard:{sort_key}:{page}:{direction}"
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
    ) -> RaffleLeaderboardButton:
        return cls(
            match["sort_key"],
            int(match["page"]),
            int(match["direction"]),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("Gw2Bot", interaction.client)
        # A custom_id survives deploys, so an old message may still name a
        # sort key this build no longer knows about.
        if self.sort_key not in RAFFLE_TICKET_ROW_SORT_KEYS:
            LOGGER.debug(
                "Raffle leaderboard paging rejected an unknown sort key"
            )
            await interaction.response.send_message(
                "That leaderboard sort is no longer supported. "
                "Run `/raffle leaderboard` again.",
                ephemeral=True,
            )
            return

        try:
            contributions = bot.get_lifetime_raffle_contributions()
        except SQLAlchemyError:
            LOGGER.error(
                "Could not load lifetime raffle contributions for paging"
            )
            await interaction.response.send_message(
                RAFFLE_PAGER_LOAD_FAILURE,
                ephemeral=True,
            )
            return

        rows = order_raffle_ticket_rows(
            raffle_contribution_table_rows(contributions),
            self.sort_key,
        )
        page_count = _raffle_ticket_page_count(len(rows))
        page = _clamp_page(self.page, self.direction, page_count)
        LOGGER.debug(
            "Changing raffle leaderboard page; sort_key=%s direction=%s "
            "page=%s page_count=%s contributors=%s",
            self.sort_key,
            self.direction,
            page + 1,
            page_count,
            len(rows),
        )
        await interaction.response.edit_message(
            embed=raffle_ticket_table_embed(
                rows,
                raffle_leaderboard_title(self.sort_key),
                page,
            ),
            view=RaffleLeaderboardView(self.sort_key, len(rows), page),
        )


class RaffleLeaderboardView(discord.ui.View):
    def __init__(self, sort_key: str, row_count: int, page: int = 0):
        super().__init__(timeout=None)
        page_count = _raffle_ticket_page_count(row_count)
        page = max(0, min(page, page_count - 1))
        self.add_item(
            RaffleLeaderboardButton(sort_key, page, -1, disabled=page == 0)
        )
        self.add_item(
            RaffleLeaderboardButton(
                sort_key,
                page,
                1,
                disabled=page >= page_count - 1,
            )
        )


class RaffleContributionReportButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=(
        r"gw2bot:raffle-contributions:(?P<report_start>[0-9]+):"
        r"(?P<report_end>[0-9]+):(?P<page>[0-9]+):(?P<direction>-?1)"
    ),
):
    # The report covers one fixed window, so both of its bounds ride in the
    # custom_id as UTC epoch seconds and exactly that window is reloaded on
    # click. Carrying both bounds rather than deriving the start from a fixed
    # report width also lets the diagnostics preview page its partial window.
    def __init__(
        self,
        report_start: int,
        report_end: int,
        page: int,
        direction: int,
        *,
        disabled: bool = False,
    ):
        self.report_start = report_start
        self.report_end = report_end
        self.page = page
        self.direction = direction
        super().__init__(
            discord.ui.Button(
                label="<" if direction < 0 else ">",
                style=discord.ButtonStyle.secondary,
                custom_id=(
                    f"gw2bot:raffle-contributions:{report_start}:"
                    f"{report_end}:{page}:{direction}"
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
    ) -> RaffleContributionReportButton:
        return cls(
            int(match["report_start"]),
            int(match["report_end"]),
            int(match["page"]),
            int(match["direction"]),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("Gw2Bot", interaction.client)
        try:
            contributions = bot.get_raffle_contributions(
                datetime.fromtimestamp(self.report_start, UTC),
                datetime.fromtimestamp(self.report_end, UTC),
            )
        except SQLAlchemyError:
            LOGGER.error(
                "Could not load raffle contributions for report paging"
            )
            await interaction.response.send_message(
                RAFFLE_PAGER_LOAD_FAILURE,
                ephemeral=True,
            )
            return

        page_count = _raffle_ticket_page_count(len(contributions))
        page = _clamp_page(self.page, self.direction, page_count)
        LOGGER.debug(
            "Changing raffle contribution report page; direction=%s page=%s "
            "page_count=%s contributors=%s",
            self.direction,
            page + 1,
            page_count,
            len(contributions),
        )
        await interaction.response.edit_message(
            embed=raffle_contribution_report_embed(contributions, page),
            view=RaffleContributionReportView(
                datetime.fromtimestamp(self.report_start, UTC),
                datetime.fromtimestamp(self.report_end, UTC),
                len(contributions),
                page,
            ),
        )


class RaffleContributionReportView(discord.ui.View):
    def __init__(
        self,
        report_start: datetime,
        report_end: datetime,
        contributor_count: int,
        page: int = 0,
    ):
        super().__init__(timeout=None)
        start_epoch = int(report_start.astimezone(UTC).timestamp())
        end_epoch = int(report_end.astimezone(UTC).timestamp())
        page_count = _raffle_ticket_page_count(contributor_count)
        page = max(0, min(page, page_count - 1))
        self.add_item(
            RaffleContributionReportButton(
                start_epoch,
                end_epoch,
                page,
                -1,
                disabled=page == 0,
            )
        )
        self.add_item(
            RaffleContributionReportButton(
                start_epoch,
                end_epoch,
                page,
                1,
                disabled=page >= page_count - 1,
            )
        )


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
