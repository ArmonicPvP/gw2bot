from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote

import aiohttp

LOGGER = logging.getLogger(__name__)


class Gw2ApiClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, api_key: str):
        self._session = session
        self._base_url = base_url
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def get_account(self) -> dict[str, Any]:
        return await self._get("/v2/account")

    async def get_token_info(self) -> dict[str, Any]:
        return await self._get("/v2/tokeninfo")

    async def create_subtoken(
        self,
        expire: datetime | str,
        permissions: Sequence[str],
        urls: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        params = {
            "expire": expire.isoformat() if isinstance(expire, datetime) else expire,
            "permissions": ",".join(permissions),
        }
        if urls:
            params["urls"] = ",".join(urls)
        return await self._get("/v2/createsubtoken", params=params)

    async def get_guild(self, guild_id: str) -> dict[str, Any]:
        return await self._get(self._guild_path(guild_id))

    async def get_guild_log(
        self,
        guild_id: str,
        since: int | None = None,
    ) -> list[dict[str, Any]]:
        params = {"since": str(since)} if since is not None else None
        return await self._get(f"{self._guild_path(guild_id)}/log", params=params)

    async def get_guild_members(self, guild_id: str) -> list[dict[str, Any]]:
        return await self._get(f"{self._guild_path(guild_id)}/members")

    async def get_guild_ranks(self, guild_id: str) -> list[dict[str, Any]]:
        return await self._get(f"{self._guild_path(guild_id)}/ranks")

    async def get_guild_stash(self, guild_id: str) -> list[dict[str, Any]]:
        return await self._get(f"{self._guild_path(guild_id)}/stash")

    async def get_guild_storage(self, guild_id: str) -> list[dict[str, Any]]:
        return await self._get(f"{self._guild_path(guild_id)}/storage")

    async def get_guild_treasury(self, guild_id: str) -> list[dict[str, Any]]:
        return await self._get(f"{self._guild_path(guild_id)}/treasury")

    async def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> Any:
        LOGGER.debug("Sending GW2 API GET request to %s", path)
        async with self._session.get(
            f"{self._base_url}{path}",
            headers=self._headers,
            params=params,
        ) as response:
            LOGGER.debug(
                "GW2 API GET %s returned HTTP %s",
                path,
                getattr(response, "status", "unknown"),
            )
            response.raise_for_status()
            payload = await response.json()
            LOGGER.debug(
                "Decoded GW2 API response for %s; result_type=%s result_count=%s",
                path,
                type(payload).__name__,
                len(payload) if isinstance(payload, (dict, list)) else "n/a",
            )
            return payload

    @staticmethod
    def _guild_path(guild_id: str) -> str:
        return f"/v2/guild/{quote(guild_id, safe='')}"
