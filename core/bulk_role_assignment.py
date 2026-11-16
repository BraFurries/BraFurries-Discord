"""Read-only, fail-closed preview for assigning one Discord role to all humans.

The API is responsible for administrative authorization and eventual durable jobs.
Coddy only observes actual Discord membership, hierarchy and role permissions.
"""

from __future__ import annotations

from hashlib import sha256

import discord


class BulkRolePreviewError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _require_bulk_role_access(guild: discord.Guild, discord_user_id: int, role_id: int):
    actor = guild.get_member(discord_user_id)
    if actor is None:
        raise BulkRolePreviewError("actor_not_in_guild")
    if discord_user_id != guild.owner_id and not actor.guild_permissions.administrator:
        raise PermissionError("bulk_assignment_requires_administrator")

    role = guild.get_role(role_id)
    if role is None:
        raise BulkRolePreviewError("role_not_found")
    if role.is_default() or role.managed:
        raise BulkRolePreviewError("role_not_assignable")

    bot = guild.me
    if bot is None or not bot.guild_permissions.manage_roles:
        raise PermissionError("bot_cannot_manage_roles")
    # Discord role comparisons are more precise than comparing positions alone.
    if not bot.top_role > role:
        raise PermissionError("role_above_bot")
    if discord_user_id != guild.owner_id and not actor.top_role > role:
        raise PermissionError("role_above_actor")
    return role


def build_bulk_role_preview(
    guild: discord.Guild,
    discord_user_id: int,
    role_id: int,
) -> dict:
    role = _require_bulk_role_access(guild, discord_user_id, role_id)

    # The operation must never silently use a partial guild.members cache.
    if not guild.chunked:
        raise BulkRolePreviewError("member_cache_incomplete")

    total = eligible = already_assigned = bot_count = 0
    fingerprint = sha256()
    fingerprint.update(f"{guild.id}:{role.id}:".encode("ascii"))
    for member in sorted(guild.members, key=lambda m: m.id):
        if member.bot:
            bot_count += 1
            continue
        total += 1
        has_role = role in member.roles
        if has_role:
            already_assigned += 1
        else:
            eligible += 1
        fingerprint.update(f"{member.id}:{int(has_role)};".encode("ascii"))

    return {
        "guildId": str(guild.id),
        "roleId": str(role.id),
        "roleName": role.name,
        "humanMembers": total,
        "eligible": eligible,
        "alreadyAssigned": already_assigned,
        "excludedBots": bot_count,
        "fingerprint": fingerprint.hexdigest(),
    }
