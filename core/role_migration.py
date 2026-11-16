"""Read-only, fail-closed preview for transferring Discord members between roles.

A transfer moves members who hold a source role. It never removes source
before adding destination; existing destination memberships remain intact.
"""
from __future__ import annotations

from hashlib import sha256

import discord

from core.bulk_role_assignment import BulkRolePreviewError, _require_bulk_role_access


def require_role_migration_access(
    guild: discord.Guild, discord_user_id: int, source_role_id: int, target_role_id: int
) -> tuple[discord.Role, discord.Role]:
    if source_role_id == target_role_id:
        raise BulkRolePreviewError("identical_source_and_destination")
    source = _require_bulk_role_access(guild, discord_user_id, source_role_id)
    destination = _require_bulk_role_access(guild, discord_user_id, target_role_id)
    return source, destination


def build_role_migration_preview(
    guild: discord.Guild, discord_user_id: int, source_role_id: int, target_role_id: int
) -> dict:
    source, destination = require_role_migration_access(
        guild, discord_user_id, source_role_id, target_role_id
    )
    if not guild.chunked:
        raise BulkRolePreviewError("member_cache_incomplete")

    fingerprint = sha256()
    fingerprint.update(f"ROLE_MIGRATE:{guild.id}:{source.id}:{destination.id}:".encode("ascii"))
    source_members = needs_addition = already_target = excluded_bots = 0
    for member in sorted(guild.members, key=lambda member: member.id):
        if source not in member.roles:
            continue
        if member.bot:
            excluded_bots += 1
            continue
        source_members += 1
        has_destination = destination in member.roles
        if has_destination:
            already_target += 1
        else:
            needs_addition += 1
        fingerprint.update(f"{member.id}:{int(has_destination)};".encode("ascii"))

    return {
        "guildId": str(guild.id),
        "sourceRoleId": str(source.id),
        "sourceRoleName": source.name,
        "targetRoleId": str(destination.id),
        "targetRoleName": destination.name,
        "sourceMembers": source_members,
        "needsAddition": needs_addition,
        "alreadyHasDestination": already_target,
        "excludedBots": excluded_bots,
        "fingerprint": fingerprint.hexdigest(),
    }
