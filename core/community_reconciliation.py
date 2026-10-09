from __future__ import annotations

import asyncio
from collections.abc import Callable

import discord

from core.membership_operation_gate import membership_reconciliation_guard
from core.community_lifecycle_api import (
    ClaimedSyncRun,
    CommunityLifecycleApiClient,
    CommunityLifecycleApiError,
    IncompleteGuildSnapshot,
    fetch_complete_members,
)


MEMBER_BATCH_SIZE = 100


class SnapshotBatchApiError(CommunityLifecycleApiError):
    def __init__(
        self,
        original: CommunityLifecycleApiError,
        *,
        batch_start: int,
    ) -> None:
        super().__init__(
            f"Falha no batch iniciado em {batch_start}: {original}",
            status=original.status,
            retryable=original.retryable,
        )
        self.batch_start = batch_start


def reconciliation_trigger_for_observation(
    *,
    sync_state: str | None,
    tracked_present_members: int | None,
    provider_member_count: int | None,
    local_recovery_required: bool = False,
    required_state_trigger: str = "DRIFT",
    recover_stale_reconciling: bool = False,
    membership_reconciliation_running: bool = False,
) -> str | None:
    """Choose whether a full reconciliation is justified.

    Healthy networks with matching counts stay event-driven. Local delivery
    failures and degraded health require recovery even when counts match,
    because updates such as approval changes do not affect member counts.
    """
    if recover_stale_reconciling and membership_reconciliation_running:
        # The previous process can die after finalizing the snapshot (HEALTHY)
        # but before closing its run. A fresh runtime must supersede that orphan.
        return "STARTUP"
    if sync_state == "RECONCILING":
        return "STARTUP" if recover_stale_reconciling else None
    if local_recovery_required or sync_state == "DEGRADED":
        return "RECOVERY"
    if sync_state == "RECONCILIATION_REQUIRED":
        return required_state_trigger
    if (
        isinstance(tracked_present_members, int)
        and provider_member_count is not None
        and tracked_present_members != int(provider_member_count)
    ):
        return "DRIFT"
    return None


def _error_code(error: BaseException) -> str:
    if isinstance(error, IncompleteGuildSnapshot):
        return "INCOMPLETE_SNAPSHOT"
    if isinstance(error, SnapshotBatchApiError):
        status = error.status if error.status is not None else "ERROR"
        return f"API_{status}_BATCH_{error.batch_start}"
    if isinstance(error, CommunityLifecycleApiError):
        if error.status is not None:
            return f"API_{error.status}"
        return "LIFECYCLE_API_ERROR"
    if isinstance(error, discord.Forbidden):
        return "DISCORD_FORBIDDEN"
    if isinstance(error, discord.HTTPException):
        return "DISCORD_HTTP_ERROR"
    return "RUNTIME_ERROR"


async def reconcile_guild(
    guild: discord.Guild,
    client: CommunityLifecycleApiClient,
    *,
    trigger: str,
    approved_resolver: Callable[[discord.Member], bool],
) -> dict:
    """Reconcile one guild from a complete Discord snapshot.

    The API owns the membership mutation. This function refuses to submit a
    snapshot unless Discord's complete-member fetch passed its completeness
    checks.
    """
    async with membership_reconciliation_guard(guild.id):
        await client.observe_network(guild, active=True)
        run = await client.create_sync_run(guild.id, trigger)
        run_id = int(run["runId"])
        return await _reconcile_with_run(
            guild,
            client,
            run_id=run_id,
            approved_resolver=approved_resolver,
        )


async def reconcile_claimed_run(
    bot: discord.Client,
    client: CommunityLifecycleApiClient,
    run: ClaimedSyncRun,
    *,
    approved_resolver_factory: Callable[[discord.Guild], Callable[[discord.Member], bool]],
) -> dict | None:
    """Execute a user/admin-enqueued run already atomically claimed by the API."""
    guild = bot.get_guild(run.guild_id)
    if guild is None:
        try:
            await client.finish_sync_run(
                run.guild_id,
                run.run_id,
                success=False,
                error_code="GUILD_NOT_AVAILABLE",
            )
        except CommunityLifecycleApiError:
            pass
        return None

    async with membership_reconciliation_guard(guild.id):
        try:
            await client.observe_network(guild, active=True)
            approved_resolver = approved_resolver_factory(guild)
        except Exception as error:
            try:
                await client.finish_sync_run(
                    guild.id,
                    run.run_id,
                    success=False,
                    error_code=_error_code(error),
                )
            except CommunityLifecycleApiError:
                pass
            raise

        return await _reconcile_with_run(
            guild,
            client,
            run_id=run.run_id,
            approved_resolver=approved_resolver,
        )


async def _reconcile_with_run(
    guild: discord.Guild,
    client: CommunityLifecycleApiClient,
    *,
    run_id: int,
    approved_resolver: Callable[[discord.Member], bool],
) -> dict:
    try:
        members = await fetch_complete_members(guild)
        updated_total = 0
        for start in range(0, len(members), MEMBER_BATCH_SIZE):
            batch = members[start : start + MEMBER_BATCH_SIZE]
            try:
                batch_result = await client.apply_member_batch(
                    guild,
                    run_id=run_id,
                    members=batch,
                    approved_resolver=approved_resolver,
                )
            except CommunityLifecycleApiError as error:
                raise SnapshotBatchApiError(
                    error,
                    batch_start=start,
                ) from error
            batch_updated = batch_result.get("updatedMembers")
            if isinstance(batch_updated, int):
                updated_total += batch_updated

        finalized = await client.finalize_member_snapshot(
            guild.id,
            run_id=run_id,
            observed_members=len(members),
        )
        finalize_updated = finalized.get("updatedMembers")
        if isinstance(finalize_updated, int):
            updated_total += finalize_updated

        await client.finish_sync_run(
            guild.id,
            run_id,
            success=True,
            observed_members=len(members),
            updated_members=updated_total,
        )
        return {
            "communityId": finalized.get("communityId"),
            "guildId": finalized.get("guildId", str(guild.id)),
            "observedMembers": len(members),
            "updatedMembers": updated_total,
        }
    except Exception as error:
        try:
            await client.finish_sync_run(
                guild.id,
                run_id,
                success=False,
                error_code=_error_code(error),
            )
        except CommunityLifecycleApiError:
            pass
        raise
