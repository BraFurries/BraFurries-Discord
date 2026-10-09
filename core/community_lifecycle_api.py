from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import os
from typing import Any, Iterable

import aiohttp
import discord


DEFAULT_API_BASE_URL = "http://127.0.0.1:18080"


class CommunityLifecycleApiError(RuntimeError):
    """Raised when the Coddy -> API lifecycle contract cannot be completed safely."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class IncompleteGuildSnapshot(CommunityLifecycleApiError):
    """Raised when Discord did not provide a complete guild-member view."""


@dataclass(frozen=True)
class ClaimedSyncRun:
    run_id: int
    guild_id: int
    trigger: str


def _local_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat()


def member_payload(member: discord.Member, *, approved: bool) -> dict[str, Any]:
    return {
        "username": member.name,
        "globalDisplayName": member.global_name,
        "displayName": member.display_name,
        "approved": bool(approved),
        "joinedAt": _local_datetime(member.joined_at),
    }


def snapshot_member_payload(member: discord.Member, *, approved: bool) -> dict[str, Any]:
    return {
        "discordUserId": str(member.id),
        **member_payload(member, approved=approved),
    }


class CommunityLifecycleApiClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        session_factory=aiohttp.ClientSession,
        timeout_seconds: float = 30,
    ) -> None:
        self.base_url = (
            base_url
            if base_url is not None
            else os.getenv("BOT_API_BASE_URL", DEFAULT_API_BASE_URL)
        ).strip().rstrip("/")
        self.token = (
            token
            if token is not None
            else os.getenv("BOT_STATUS_API_TOKEN", "")
        ).strip()
        self.session_factory = session_factory
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    def _ensure_configured(self) -> None:
        if not self.base_url or not self.token:
            raise CommunityLifecycleApiError(
                "Integração de lifecycle com a API não configurada"
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        expected: Iterable[int] = (200,),
    ) -> dict[str, Any] | None:
        self._ensure_configured()
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with self.session_factory(timeout=self.timeout) as session:
                async with session.request(
                    method,
                    url,
                    json=json,
                    headers=headers,
                ) as response:
                    if response.status not in set(expected):
                        body = await response.text()
                        raise CommunityLifecycleApiError(
                            f"API lifecycle respondeu {response.status}: {body[:200]}",
                            status=response.status,
                            retryable=response.status == 429 or response.status >= 500,
                        )
                    if response.status == 204:
                        return None
                    try:
                        payload = await response.json()
                    except (aiohttp.ContentTypeError, ValueError, TypeError) as error:
                        raise CommunityLifecycleApiError(
                            "Resposta de lifecycle inválida"
                        ) from error
                    if not isinstance(payload, dict):
                        raise CommunityLifecycleApiError(
                            "Resposta de lifecycle inválida"
                        )
                    return payload
        except CommunityLifecycleApiError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise CommunityLifecycleApiError(
                "Falha ao chamar API de lifecycle",
                retryable=True,
            ) from error

    async def observe_network(self, guild: discord.Guild, *, active: bool) -> dict[str, Any]:
        owner_id = guild.owner_id or getattr(getattr(guild, "owner", None), "id", None)
        if not owner_id:
            raise CommunityLifecycleApiError(
                f"Owner não disponível para guild {guild.id}",
                retryable=True,
            )
        return await self._request(
            "PUT",
            f"/internal/community-networks/discord/{guild.id}",
            json={
                "name": guild.name,
                "ownerDiscordUserId": str(owner_id),
                "active": bool(active),
                "observedMembers": max(0, int(guild.member_count or 0)),
            },
        ) or {}

    async def reconcile_presence(self, guilds: Iterable[discord.Guild]) -> dict[str, Any]:
        return await self._request(
            "PUT",
            "/internal/community-networks/discord/presence-snapshot",
            json={
                "complete": True,
                "guildIds": [str(guild.id) for guild in guilds],
            },
        ) or {}

    async def observe_member(
        self,
        member: discord.Member,
        *,
        approved: bool,
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"/internal/community-networks/discord/{member.guild.id}/members/{member.id}",
            json=member_payload(member, approved=approved),
        ) or {}

    async def observe_member_approval(
        self,
        guild_id: int,
        discord_user_id: int,
        *,
        approved: bool,
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"/internal/community-networks/discord/{guild_id}/members/{discord_user_id}/approval",
            json={"approved": bool(approved)},
        ) or {}

    async def remove_member(self, guild_id: int, discord_user_id: int) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/internal/community-networks/discord/{guild_id}/members/{discord_user_id}",
        ) or {}

    async def create_sync_run(self, guild_id: int, trigger: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/internal/community-networks/discord/{guild_id}/sync-runs",
            json={
                "trigger": trigger,
                "status": "RUNNING",
            },
            expected=(201,),
        ) or {}

    async def claim_next_sync(self) -> ClaimedSyncRun | None:
        payload = await self._request(
            "POST",
            "/internal/community-networks/discord/sync-runs/claim",
            expected=(200, 204),
        )
        if payload is None:
            return None
        run_id = payload.get("runId")
        external_network_id = payload.get("externalNetworkId")
        trigger = payload.get("trigger")
        if (
            not isinstance(run_id, int)
            or not isinstance(external_network_id, str)
            or not external_network_id.isdecimal()
            or not isinstance(trigger, str)
        ):
            raise CommunityLifecycleApiError("Sync run reclamado possui payload inválido")
        return ClaimedSyncRun(
            run_id=run_id,
            guild_id=int(external_network_id),
            trigger=trigger,
        )

    async def apply_member_batch(
        self,
        guild: discord.Guild,
        *,
        run_id: int,
        members: list[discord.Member],
        approved_resolver,
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"/internal/community-networks/discord/{guild.id}/members/snapshot/batch",
            json={
                "runId": run_id,
                "members": [
                    snapshot_member_payload(
                        member,
                        approved=bool(approved_resolver(member)),
                    )
                    for member in members
                ],
            },
        ) or {}

    async def finalize_member_snapshot(
        self,
        guild_id: int,
        *,
        run_id: int,
        observed_members: int,
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"/internal/community-networks/discord/{guild_id}/members/snapshot/finalize",
            json={
                "runId": run_id,
                "complete": True,
                "observedMembers": max(0, int(observed_members)),
            },
        ) or {}

    async def finish_sync_run(
        self,
        guild_id: int,
        run_id: int,
        *,
        success: bool,
        observed_members: int | None = None,
        updated_members: int | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "status": "SUCCESS" if success else "FAILED",
            "observedMembers": observed_members,
            "updatedMembers": updated_members,
            "errorCode": error_code,
        }
        return await self._request(
            "PATCH",
            f"/internal/community-networks/discord/{guild_id}/sync-runs/{run_id}",
            json=body,
        ) or {}


async def observe_network_with_retry(
    client: CommunityLifecycleApiClient,
    guild: discord.Guild,
    *,
    active: bool,
    attempts: int = 3,
    base_delay_seconds: float = 0.5,
) -> dict[str, Any]:
    attempts = max(1, attempts)
    last_error: CommunityLifecycleApiError | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await client.observe_network(guild, active=active)
        except CommunityLifecycleApiError as error:
            last_error = error
            if not error.retryable:
                raise
            if attempt >= attempts:
                break
            await asyncio.sleep(base_delay_seconds * (2 ** (attempt - 1)))

    assert last_error is not None
    raise last_error


async def fetch_complete_members(
    guild: discord.Guild,
    *,
    attempts: int = 2,
) -> list[discord.Member]:
    """Exhaust Discord's member listing and fail closed on a stable mismatch.

    On large active guilds, member_count can legitimately change while the
    paginated REST listing is in progress. Those concurrent gateway events are
    held behind the reconciliation gate and applied immediately afterwards, so
    a changing count is not evidence of a partial REST listing. A stable count
    that still disagrees after retry remains a hard failure.
    """
    attempts = max(1, attempts)
    last_count = -1
    expected_after = None
    expected_before = None

    for attempt in range(attempts):
        expected_before = guild.member_count
        members: list[discord.Member] = []
        try:
            async for member in guild.fetch_members(limit=None):
                members.append(member)
        except (discord.Forbidden, discord.HTTPException) as error:
            raise IncompleteGuildSnapshot(
                f"Não foi possível obter todos os membros da guild {guild.id}"
            ) from error

        unique_ids = {member.id for member in members}
        if len(unique_ids) != len(members):
            raise IncompleteGuildSnapshot(
                f"Snapshot duplicado na guild {guild.id}"
            )

        expected_after = guild.member_count
        last_count = len(members)
        if expected_before is None or expected_after is None:
            return members

        if int(expected_before) != int(expected_after):
            return members

        if last_count == int(expected_after):
            return members

        if attempt + 1 < attempts:
            await asyncio.sleep(1)

    raise IncompleteGuildSnapshot(
        f"Snapshot incompleto na guild {guild.id}: "
        f"observados={last_count}, esperado={expected_after}"
    )
