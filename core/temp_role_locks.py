import asyncio
from contextlib import asynccontextmanager


_temp_role_locks: dict[tuple[int, int, int], tuple[asyncio.Lock, int]] = {}


def temp_role_lock_key(
    guild_id: int,
    discord_user_id: int,
    role_id: int | str,
) -> tuple[int, int, int]:
    return int(guild_id), int(discord_user_id), int(role_id)


@asynccontextmanager
async def temp_role_lock(
    guild_id: int,
    discord_user_id: int,
    role_id: int | str,
):
    """Serialize one temporary-role lifecycle inside this Coddy process."""
    key = temp_role_lock_key(guild_id, discord_user_id, role_id)
    entry = _temp_role_locks.get(key)
    if entry is None:
        lock = asyncio.Lock()
        users = 0
    else:
        lock, users = entry
    _temp_role_locks[key] = (lock, users + 1)

    acquired = False
    try:
        await lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            lock.release()
        current = _temp_role_locks.get(key)
        if current is not None and current[0] is lock:
            remaining_users = current[1] - 1
            if remaining_users == 0:
                _temp_role_locks.pop(key, None)
            else:
                _temp_role_locks[key] = (lock, remaining_users)
