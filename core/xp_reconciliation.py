from __future__ import annotations

import asyncio


_reconcile_locks: dict[int, asyncio.Lock] = {}


def _lock_for(guild_id: int) -> asyncio.Lock:
    lock = _reconcile_locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        _reconcile_locks[guild_id] = lock
    return lock


async def reconcile_guild_levels_serialized(
    guild_id: int,
    batch_size: int = 500,
) -> dict[str, int | bool]:
    """Serialize current-level reconciliation for one guild.

    The database keeps the durable pending marker. This lock only prevents two
    runtime paths in the same Coddy process from doing the same batch work at
    once.
    """
    from core.database import async_reconcile_guild_levels

    async with _lock_for(int(guild_id)):
        return await async_reconcile_guild_levels(int(guild_id), batch_size)
