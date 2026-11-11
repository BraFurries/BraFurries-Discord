"""Discord-only execution of API-authorized global Coddy admin operations.

The API decides who may act and which Community a guild belongs to. This
module never accepts browser traffic directly, never authorizes a platform user
and never mutates database schema.
"""
from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)

ACTIVITY_TYPES = frozenset({"PLAYING", "LISTENING", "WATCHING", "CUSTOM"})
AVAILABILITIES = frozenset({"online", "idle", "dnd", "invisible"})


def validate_presence(payload: dict) -> tuple[str, str, str]:
    if not isinstance(payload, dict):
        raise ValueError("invalid_presence")
    kind = payload.get("activityType")
    value = payload.get("activityText")
    availability = payload.get("availability")
    if kind not in ACTIVITY_TYPES or availability not in AVAILABILITIES:
        raise ValueError("invalid_presence")
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("invalid_presence")
    if len(value.encode("utf-16-le")) // 2 > 128:
        raise ValueError("invalid_presence")
    if any(ord(c) < 32 and c not in {"\n"} for c in value):
        raise ValueError("invalid_presence")
    return kind, value, availability


async def apply_presence(bot: discord.Client, payload: dict) -> dict:
    kind, value, availability = validate_presence(payload)
    if kind == "CUSTOM":
        activity = discord.CustomActivity(name=value)
    else:
        activity = discord.Activity(type={
            "PLAYING": discord.ActivityType.playing,
            "LISTENING": discord.ActivityType.listening,
            "WATCHING": discord.ActivityType.watching,
        }[kind], name=value)
    await bot.change_presence(activity=activity, status=getattr(discord.Status, availability))
    result = {"activityType": kind, "activityText": value, "availability": availability}
    bot._coddy_web_presence = result
    return result


def read_live_presence(bot: discord.Client) -> dict:
    applied = getattr(bot, "_coddy_web_presence", None)
    if applied is not None:
        return dict(applied)
    activity = getattr(bot, "activity", None)
    if isinstance(activity, discord.CustomActivity):
        kind = "CUSTOM"
    else:
        kind = {
            discord.ActivityType.playing: "PLAYING",
            discord.ActivityType.listening: "LISTENING",
            discord.ActivityType.watching: "WATCHING",
        }.get(getattr(activity, "type", None))
    status = getattr(bot, "status", None)
    availability = str(status) if str(status) in AVAILABILITIES else "online"
    return {
        "activityType": kind,
        "activityText": getattr(activity, "name", None) if kind else None,
        "availability": availability,
    }


async def reconcile_desired_presence(bot: discord.Client) -> None:
    """Apply the API's persisted desired presence on every gateway READY.

    Lazy import keeps status-only/test imports away from the real DB pool.
    The canonical migration must be installed first; missing table is logged,
    never created or repaired by Coddy.
    """
    try:
        from core.database import async_pooled_connection
        async with async_pooled_connection() as cursor:
            await cursor.execute("""
                SELECT activity_type, activity_text, availability
                FROM coddy_presence_settings WHERE singleton_id = 1
            """)
            row = await cursor.fetchone()
        if row:
            await apply_presence(bot, {
                "activityType": row["activity_type"],
                "activityText": row["activity_text"],
                "availability": row["availability"],
            })
    except Exception:
        logger.exception("Coddy desired presence reconciliation failed")


def validate_message(payload: dict) -> tuple[int, str]:
    if not isinstance(payload, dict):
        raise ValueError("invalid_message")
    raw_channel = payload.get("channelId")
    message = payload.get("content")
    if not isinstance(raw_channel, str) or not raw_channel.isascii() or not raw_channel.isdecimal():
        raise ValueError("invalid_channel")
    channel_id = int(raw_channel)
    if channel_id <= 0 or channel_id > 9_223_372_036_854_775_807:
        raise ValueError("invalid_channel")
    if not isinstance(message, str) or not message.strip() or len(message.encode("utf-16-le")) // 2 > 1800:
        raise ValueError("invalid_message")
    return channel_id, message


def is_writable_channel(guild: discord.Guild, channel) -> bool:
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return False
    me = getattr(guild, "me", None)
    if me is None:
        return False
    permissions = channel.permissions_for(me)
    return bool(
        permissions.view_channel
        and (permissions.send_messages_in_threads if isinstance(channel, discord.Thread)
             else permissions.send_messages)
        and not bool(getattr(channel, "locked", False))
        and not bool(getattr(channel, "archived", False))
    )


def writable_destinations(bot: discord.Client) -> list[dict]:
    guilds = []
    for guild in bot.guilds:
        channels = [*guild.text_channels, *guild.threads]
        allowed = [
            {"channelId": str(ch.id), "name": ch.name,
             "kind": "thread" if isinstance(ch, discord.Thread) else "text"}
            for ch in channels if is_writable_channel(guild, ch)
        ]
        guilds.append({
            "guildId": str(guild.id),
            "guildName": guild.name,
            "channels": sorted(allowed, key=lambda ch: (ch["kind"], ch["name"].casefold())),
        })
    return sorted(guilds, key=lambda item: item["guildName"].casefold())


async def send_message(bot: discord.Client, guild_id: int, payload: dict) -> dict:
    channel_id, message = validate_message(payload)
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise LookupError("guild_not_found")
    channel = guild.get_channel_or_thread(channel_id)
    if channel is None or not is_writable_channel(guild, channel):
        raise LookupError("channel_not_writable")
    sent = await channel.send(
        message,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    return {
        "guildId": str(guild.id),
        "channelId": str(channel_id),
        "messageId": str(sent.id),
    }
