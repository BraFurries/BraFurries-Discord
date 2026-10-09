"""Shared, conservative resolution for destinations used by Coddy logs."""
from __future__ import annotations

from typing import Any

import discord


def _is_forum_thread(channel: Any) -> bool:
    return isinstance(channel, discord.Thread) and isinstance(getattr(channel, "parent", None), discord.ForumChannel)


def _target_type(channel: Any) -> str | None:
    if _is_forum_thread(channel):
        return "forum_thread"
    # NewsChannel is a TextChannel subclass in discord.py and is intentionally included.
    if isinstance(channel, discord.TextChannel):
        return "text"
    return None


def log_target_metadata(channel: Any, *, writable: bool | None = None, missing: bool = False) -> dict[str, Any]:
    parent = getattr(channel, "parent", None)
    return {
        "id": str(getattr(channel, "id", "")),
        "type": _target_type(channel),
        "name": getattr(channel, "name", None),
        "parentId": str(parent.id) if _is_forum_thread(channel) else None,
        "parentName": parent.name if _is_forum_thread(channel) else None,
        "writable": writable,
        "missing": missing,
    }


def log_target_warnings(guild: discord.Guild, channel: Any) -> list[str]:
    target_type = _target_type(channel)
    if target_type is None:
        return ["O destino não é um canal de texto nem um post de fórum."]
    if isinstance(channel, discord.Thread) and (getattr(channel, "archived", False) or getattr(channel, "locked", False)):
        return ["O post de fórum está arquivado ou bloqueado."]
    member = guild.me
    if member is None:
        return ["O membro do Coddy não está disponível neste servidor."]
    permissions = channel.permissions_for(member)
    required = ("view_channel", "send_messages", "embed_links")
    missing = [name for name in required if not bool(getattr(permissions, name, False))]
    if isinstance(channel, discord.Thread) and not bool(getattr(permissions, "send_messages_in_threads", True)):
        missing.append("send_messages_in_threads")
    if missing:
        return ["O Coddy não consegue enviar logs nesse destino. Verifique as permissões do canal ou tópico."]
    return []


async def resolve_log_target(guild: discord.Guild, target_id: int | str) -> Any | None:
    try:
        normalized_id = int(target_id)
    except (TypeError, ValueError):
        return None
    channel = guild.get_channel_or_thread(normalized_id)
    if channel is not None:
        return channel
    try:
        channel = await guild.fetch_channel(normalized_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return channel if getattr(channel, "guild", guild) == guild else None


async def validate_log_target(guild: discord.Guild, target_id: int | str) -> tuple[Any | None, list[str]]:
    channel = await resolve_log_target(guild, target_id)
    if channel is None:
        return None, ["O canal ou tópico configurado não existe mais ou não está acessível ao Coddy."]
    return channel, log_target_warnings(guild, channel)


def selectable_log_targets(guild: discord.Guild) -> list[dict[str, Any]]:
    # discord.py documents by_category() as the UI order for categories and
    # their children. Forum posts retain the cache order supplied by guild.threads.
    threads_by_parent: dict[int, list[Any]] = {}
    for thread in getattr(guild, "threads", []):
        parent = getattr(thread, "parent", None)
        if isinstance(parent, discord.ForumChannel):
            threads_by_parent.setdefault(parent.id, []).append(thread)
    candidates: list[Any] = []
    for _category, channels in guild.by_category():
        for channel in channels:
            if isinstance(channel, discord.TextChannel):
                candidates.append(channel)
            elif isinstance(channel, discord.ForumChannel):
                candidates.extend(threads_by_parent.get(channel.id, []))
    targets = []
    seen: set[int] = set()
    for channel in candidates:
        if channel.id in seen or _target_type(channel) is None:
            continue
        seen.add(channel.id)
        warnings = log_target_warnings(guild, channel)
        if not warnings:
            targets.append(log_target_metadata(channel, writable=True))
    return targets
