"""Preview-first, guild-scoped copy of Discord channel permission overwrites.

Avoid the legacy approach of deleting every overwrite one by one: a single
channel.edit(overwrites=...) gives the target one coherent permission update.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

import discord

_ALLOWED_TYPES = {"text", "news", "voice", "stage_voice", "forum", "category"}
_copy_locks: dict[tuple[int, int], asyncio.Lock] = {}


class ChannelPermissionConflict(Exception):
    """The live permission state differs from the preview the actor accepted."""


def _overwrite_entries(channel: Any) -> list[dict[str, Any]]:
    entries = []
    for principal, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        entries.append({
            "id": str(principal.id),
            "name": str(getattr(principal, "name", principal.id)),
            "type": "role" if isinstance(principal, discord.Role) else "member",
            "allow": str(allow.value),
            "deny": str(deny.value),
        })
    return sorted(entries, key=lambda item: (item["id"], item["type"]))


def _fingerprint(guild_id: int, source: Any, target: Any) -> str:
    # Source and target identities and *both* current ACLs form the preflight.
    current = {
        "guildId": str(guild_id),
        "sourceId": str(source.id),
        "targetId": str(target.id),
        "source": _overwrite_entries(source),
        "target": _overwrite_entries(target),
    }
    return hashlib.sha256(json.dumps(
        current, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()


def validate_channel_permission_copy(
    guild: discord.Guild, actor_id: int, request: dict[str, Any]
) -> tuple[Any, Any]:
    if not isinstance(request, dict):
        raise ValueError("Payload inválido")
    source_id, target_id = request.get("sourceChannelId"), request.get("targetChannelId")
    if not all(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value or "")
               for value in (source_id, target_id)):
        raise ValueError("IDs de canais inválidos")
    if source_id == target_id:
        raise ValueError("Escolha canais diferentes para origem e destino")

    member = guild.get_member(actor_id)
    member_perms = getattr(member, "guild_permissions", None)
    if member is None or not (
        int(getattr(guild, "owner_id", 0)) == actor_id
        or bool(getattr(member_perms, "administrator", False))
        or bool(getattr(member_perms, "manage_channels", False))
    ):
        raise PermissionError("Permissão Gerenciar canais é necessária")

    bot_perms = getattr(getattr(guild, "me", None), "guild_permissions", None)
    if not (getattr(bot_perms, "administrator", False)
            or getattr(bot_perms, "manage_channels", False)):
        raise PermissionError("O Coddy não pode gerenciar canais")

    source = guild.get_channel(int(source_id))
    target = guild.get_channel(int(target_id))
    if source is None or target is None:
        raise ValueError("Canal não encontrado neste servidor")
    if (getattr(getattr(source, "guild", None), "id", None) != guild.id
            or getattr(getattr(target, "guild", None), "id", None) != guild.id
            or str(getattr(source, "type", "")) not in _ALLOWED_TYPES
            or str(getattr(target, "type", "")) not in _ALLOWED_TYPES):
        raise ValueError("Apenas canais e categorias do servidor atual são aceitos")
    if not hasattr(source, "overwrites") or not hasattr(target, "overwrites"):
        raise ValueError("O canal não suporta sobrescritas de permissão")

    # Guild-level permission is not sufficient when the bot is denied access
    # through channel overwrites. Check the effective target-channel rights.
    check = getattr(target, "permissions_for", None)
    if callable(check):
        bot_target_permissions = check(guild.me)
        if not (
            getattr(bot_target_permissions, "manage_channels", False)
            or getattr(bot_target_permissions, "administrator", False)
        ):
            raise PermissionError("Coddy sem permissão efetiva no destino")
        # Administrators and guild owners may bypass channel restrictions;
        # a guild-level manage_channels role alone cannot bypass a local deny.
        if not (
            int(getattr(guild, "owner_id", 0)) == actor_id
            or bool(getattr(member_perms, "administrator", False))
        ):
            actor_target_permissions = check(member)
            if not (
                getattr(actor_target_permissions, "manage_channels", False)
                or getattr(actor_target_permissions, "administrator", False)
            ):
                raise PermissionError("Usuário sem permissão efetiva no destino")
    return source, target


def build_channel_permission_copy_preview(source: Any, target: Any) -> dict[str, Any]:
    source_acl = _overwrite_entries(source)
    target_acl = _overwrite_entries(target)
    return {
        "source": {"id": str(source.id), "name": source.name, "type": str(source.type)},
        "target": {"id": str(target.id), "name": target.name, "type": str(target.type)},
        "sourceOverwrites": source_acl,
        "targetOverwrites": target_acl,
        "appliedCount": len(source_acl),
        "replacedCount": len(target_acl),
        "changed": source_acl != target_acl,
        "fingerprint": _fingerprint(source.guild.id, source, target),
        "warning": "As permissões específicas atuais do destino serão substituídas integralmente.",
    }


async def apply_channel_permission_copy(
    guild: discord.Guild, actor_id: int, request: dict[str, Any]
) -> dict[str, Any]:
    expected = request.get("fingerprint")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Visualize a prévia antes de aplicar")
    source_id, target_id = int(request["sourceChannelId"]), int(request["targetChannelId"])
    lock = _copy_locks.setdefault((guild.id, target_id), asyncio.Lock())
    async with lock:
        source, target = validate_channel_permission_copy(guild, actor_id, request)
        preview = build_channel_permission_copy_preview(source, target)
        if preview["fingerprint"] != expected:
            raise ChannelPermissionConflict("Permissões alteradas desde a prévia")
        if preview["changed"]:
            await target.edit(
                overwrites=dict(source.overwrites),
                reason=f"Cópia de permissões do canal {source_id} para {target_id} por {actor_id} via painel",
            )
        return {
            "source": preview["source"],
            "target": preview["target"],
            "appliedCount": preview["appliedCount"],
            "replacedCount": preview["replacedCount"],
            "changed": preview["changed"],
        }
