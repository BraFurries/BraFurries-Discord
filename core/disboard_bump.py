def should_process_disboard_confirmation(channel_id: int | None, allowed_channel_ids: list[int]) -> bool:
    """Gate real-time confirmations; monthly ranking is intentionally separate."""
    return not allowed_channel_ids or channel_id in allowed_channel_ids


def resolve_realtime_bump_channel_id(
    processing_channel_ids: list[int],
    legacy_warning_source_channel_id: int | None,
) -> int | None:
    """Resolve the real-time Bump channel without mutating legacy configuration."""
    if processing_channel_ids:
        return processing_channel_ids[0]
    return legacy_warning_source_channel_id


def monthly_ranking_channel_id(monthly_config: dict) -> int | None:
    """Return the legacy monthly ranking channel, independent of processing."""
    return monthly_config.get("disboardChannelId")


def resolve_assignable_bump_role(guild, role_id: int | str | None):
    """Return the live role only when Coddy can assign it right now."""
    if not role_id:
        return None

    bot_member = getattr(guild, "me", None)
    permissions = getattr(bot_member, "guild_permissions", None)
    if bot_member is None or not bool(getattr(permissions, "manage_roles", False)):
        return None

    try:
        role = guild.get_role(int(role_id))
    except (TypeError, ValueError):
        return None
    if role is None or bool(getattr(role, "managed", False)) or role == getattr(guild, "default_role", None):
        return None

    bot_top_role = getattr(bot_member, "top_role", None)
    if bot_top_role is None:
        return None
    try:
        if not bot_top_role > role:
            return None
    except TypeError:
        if getattr(role, "position", 0) >= getattr(bot_top_role, "position", -1):
            return None
    return role
