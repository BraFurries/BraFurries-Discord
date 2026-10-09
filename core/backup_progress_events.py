"""Non-blocking, best-effort progress notification after durable Backup writes.

MariaDB remains authoritative. Dropped webhook hints are repaired by the
API's initial SSE state and by reconnect; they never affect the restore.
"""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

logger = logging.getLogger(__name__)
_latest: dict[tuple[int, str, int], int] = {}
_sending: set[tuple[int, str, int]] = set()
_background_tasks: set[asyncio.Task] = set()


def notify_backup_progress(guild_id: int, kind: str, operation_id: int, step_id: int) -> None:
    """Schedule coalesced delivery without holding a DB connection."""
    token = os.getenv("BOT_STATUS_API_TOKEN", "").strip()
    base = os.getenv("BOT_API_BASE_URL", "http://127.0.0.1:18080").strip().rstrip("/")
    if not token or not base:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Background/synchronous maintenance cannot produce a running-loop task.
        return

    key = (int(guild_id), str(kind).upper(), int(operation_id))
    _latest[key] = max(int(step_id), _latest.get(key, 0))
    if key not in _sending:
        _sending.add(key)
        task = loop.create_task(_deliver(key, base, token))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


async def _deliver(key: tuple[int, str, int], base: str, token: str) -> None:
    step_id = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5.0)) as session:
            while True:
                step_id = _latest.get(key)
                if step_id is None:
                    break
                payload = {
                    "guildId": key[0],
                    "operationKind": key[1],
                    "operationId": key[2],
                    "stepId": step_id,
                }
                try:
                    async with session.post(
                        base + "/internal/backup-progress",
                        json=payload,
                        headers={"Authorization": "Bearer " + token},
                    ) as response:
                        if response.status != 204:
                            logger.warning(
                                "Backup progress notification declined: guild=%s kind=%s "
                                "operation=%s http_status=%s",
                                *key, response.status,
                            )
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    logger.debug(
                        "Backup progress notification unavailable: guild=%s kind=%s operation=%s",
                        *key,
                    )
                if _latest.get(key) == step_id:
                    break
    finally:
        # A fresh write arriving during cleanup will start its own sender.
        latest = _latest.pop(key, None)
        _sending.discard(key)
        if latest is not None and latest != step_id:
            notify_backup_progress(*key, latest)
