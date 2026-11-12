"""Canonical successful command telemetry: one immutable event per Discord invocation.

No user IDs or command arguments are collected. Database migrations must be
applied before CODDY_COMMAND_METRICS_ENABLED=true; the bot never creates DDL.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import os

from core.database import async_execute

logger = logging.getLogger(__name__)
_MAX_SNOWFLAKE = 2**64 - 1


def enabled() -> bool:
    return os.getenv("CODDY_COMMAND_METRICS_ENABLED", "").strip().lower() == "true"


async def record_completion(
    *, event_id: int, guild_id: int | None, command_name: str, kind: str
) -> bool:
    """Persist a successful slash/prefix completion once, even after retries."""
    if not enabled():
        return False
    if (not isinstance(event_id, int) or isinstance(event_id, bool)
        or not 0 < event_id <= _MAX_SNOWFLAKE
        or (guild_id is not None and
            (not isinstance(guild_id, int) or isinstance(guild_id, bool)
             or not 0 < guild_id < 2**63))
        or kind not in ("SLASH", "PREFIX")
        or not isinstance(command_name, str)
        or not 0 < len(command_name.strip()) <= 128):
        logger.warning("Command telemetry rejected invalid execution metadata")
        return False
    normalized = " ".join(command_name.strip().split())
    if not normalized or len(normalized) > 128:
        return False
    try:
        inserted = await async_execute(
            """
            INSERT INTO stats_coddy_command_executions
                (event_id,guild_id,command_name,command_kind,completed_at)
            VALUES (%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE event_id=event_id
            """,
            (event_id, guild_id, normalized, kind,
             datetime.now(timezone.utc).replace(tzinfo=None)),
        )
        return inserted == 1
    except Exception:
        # Telemetry must not alter the result of a successfully handled command.
        logger.exception("Unable to persist successful Coddy command telemetry")
        return False
