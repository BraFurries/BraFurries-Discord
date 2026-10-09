from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class BotStatusSnapshot:
    bot_name: str
    ready: bool
    connected: bool
    guild_count: int
    user_count: int
    commands_loaded: int
    cogs_loaded: int
    latency_ms: float | None
    uptime_seconds: float
    started_at: datetime
    last_ready_at: datetime | None
    python_version: str
    discord_py_version: str
    process_id: int
    memory_used_mb: float | None = None
    memory_limit_mb: float | None = None
    cpu_usage_percent: float | None = None


def build_status_payload(snapshot: BotStatusSnapshot) -> dict[str, Any]:
    state = 'online' if snapshot.ready else 'starting' if snapshot.connected else 'offline'

    return {
        'ok': snapshot.ready,
        'state': state,
        'bot_name': snapshot.bot_name,
        'ready': snapshot.ready,
        'connected': snapshot.connected,
        'guilds': snapshot.guild_count,
        'users': snapshot.user_count,
        'commands_loaded': snapshot.commands_loaded,
        'cogs_loaded': snapshot.cogs_loaded,
        'latency_ms': round(snapshot.latency_ms, 2) if snapshot.latency_ms is not None else None,
        'uptime_seconds': round(snapshot.uptime_seconds, 2),
        'started_at': snapshot.started_at.isoformat(),
        'last_ready_at': snapshot.last_ready_at.isoformat() if snapshot.last_ready_at else None,
        'python_version': snapshot.python_version,
        'discord_py_version': snapshot.discord_py_version,
        'process_id': snapshot.process_id,
        'memory_used_mb': round(snapshot.memory_used_mb, 2) if snapshot.memory_used_mb is not None else None,
        'memory_limit_mb': round(snapshot.memory_limit_mb, 2) if snapshot.memory_limit_mb is not None else None,
        'cpu_usage_percent': round(snapshot.cpu_usage_percent, 2) if snapshot.cpu_usage_percent is not None else None,
    }
