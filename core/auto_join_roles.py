"""Shared auto-join role policy used by Discord commands and web management."""
from __future__ import annotations

import discord

SENSITIVE_PERMISSIONS = ("ban_members", "manage_roles", "manage_channels")


def _read_auto_join_config(guild_id: int) -> dict:
    """Import persistence only when an auto-join operation actually needs it."""
    from core.database import getAutoJoinRolesConfig
    return getAutoJoinRolesConfig(guild_id)


def _read_staff_roles(guild_id: int) -> list[int]:
    from core.database import getStaffRoles
    return getStaffRoles(guild_id)


def _write_auto_join_config(guild_id: int, **settings) -> bool:
    from core.database import setAutoJoinRolesConfig
    return setAutoJoinRolesConfig(guild_id, **settings)


def is_sensitive(role: discord.Role) -> bool:
    return role.permissions.administrator or any(
        getattr(role.permissions, permission, False) for permission in SENSITIVE_PERMISSIONS
    )


def validate_role_for_auto_join(guild: discord.Guild, role: discord.Role) -> str | None:
    """Return the stable rejection reason, or ``None`` when the role can be added."""
    bot_member = guild.me
    if role.is_default():
        return "default_role"
    if role.managed:
        return "managed_role"
    if bot_member is None or not bot_member.guild_permissions.manage_roles or role >= bot_member.top_role:
        return "not_assignable"
    if role.id in set(_read_staff_roles(guild.id)):
        return "staff_role"
    if is_sensitive(role):
        return "sensitive_permissions"
    return None


def read_auto_join(guild_id: int) -> dict:
    return _read_auto_join_config(guild_id)


def add_auto_join_role(guild: discord.Guild, role_id: int) -> dict:
    role = guild.get_role(role_id)
    if role is None:
        raise ValueError("role_not_found")
    reason = validate_role_for_auto_join(guild, role)
    if reason:
        raise ValueError(reason)
    current = read_auto_join(guild.id)
    role_ids = set(current.get("roleIds") or [])
    changed = role_id not in role_ids
    role_ids.add(role_id)
    if changed and not _write_auto_join_config(guild.id, role_ids=sorted(role_ids)):
        raise RuntimeError("auto_join_save_failed")
    return read_auto_join(guild.id)


def remove_auto_join_role(guild_id: int, role_id: int) -> dict:
    current = read_auto_join(guild_id)
    role_ids = set(current.get("roleIds") or [])
    if role_id in role_ids:
        role_ids.remove(role_id)
        if not _write_auto_join_config(guild_id, role_ids=sorted(role_ids)):
            raise RuntimeError("auto_join_save_failed")
    return read_auto_join(guild_id)


def set_auto_join_enabled(guild_id: int, enabled: bool) -> dict:
    current = read_auto_join(guild_id)
    if bool(current.get("enabled")) != enabled and not _write_auto_join_config(guild_id, enabled=enabled):
        raise RuntimeError("auto_join_save_failed")
    return read_auto_join(guild_id)


def ensure_auto_join_role_enabled(guild: discord.Guild, role_id: int) -> dict:
    """Idempotently reconcile the single auto-join configuration for Portaria."""
    add_auto_join_role(guild, role_id)
    return set_auto_join_enabled(guild.id, True)
