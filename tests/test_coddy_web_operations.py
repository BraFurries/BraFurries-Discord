"""Web operations: isolated Discord doubles, no real Discord or DB connection."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from aiohttp.test_utils import make_mocked_request

from core.coddy_web_operations import (
    apply_presence, is_writable_channel, read_live_presence,
    send_message, validate_message, validate_presence, writable_destinations,
)
from message_services.bot_status_api import BotStatusApi


def fake_guild():
    channel = Mock(spec=discord.TextChannel)
    channel.id = 777
    channel.name = "anuncios"
    channel.locked = False
    channel.archived = False
    channel.permissions_for.return_value = SimpleNamespace(
        view_channel=True, send_messages=True, send_messages_in_threads=True,
    )
    channel.send = AsyncMock(return_value=SimpleNamespace(id=888))
    guild = SimpleNamespace(
        id=555, name="Comunidade", me=SimpleNamespace(id=13),
        text_channels=[channel], threads=[],
        get_channel_or_thread=lambda cid: channel if cid == 777 else None,
    )
    return guild, channel


@pytest.mark.parametrize("value", [
    {}, {"activityType": "STREAMING", "activityText": "A", "availability": "online"},
    {"activityType": "PLAYING", "activityText": "", "availability": "online"},
    {"activityType": "PLAYING", "activityText": "x" * 129, "availability": "online"},
    {"activityType": "PLAYING", "activityText": "ok", "availability": "offline"},
])
def test_presence_validation_fails_closed(value):
    with pytest.raises(ValueError):
        validate_presence(value)


def test_presence_applies_exact_discord_activity_and_status():
    bot = SimpleNamespace(change_presence=AsyncMock())
    desired = {"activityType": "LISTENING", "activityText": "música", "availability": "idle"}
    actual = asyncio.run(apply_presence(bot, desired))
    assert actual == desired
    kwargs = bot.change_presence.await_args.kwargs
    assert kwargs["activity"].type == discord.ActivityType.listening
    assert kwargs["activity"].name == "música"
    assert kwargs["status"] == discord.Status.idle
    assert read_live_presence(bot) == desired


def test_message_validates_snowflake_and_utf16_length():
    for payload in [
        {"channelId": 1, "content": "Hi"},
        {"channelId": "0", "content": "Hi"},
        {"channelId": "-1", "content": "Hi"},
        {"channelId": "777", "content": ""},
        {"channelId": "777", "content": "😀" * 901},
    ]:
        with pytest.raises(ValueError):
            validate_message(payload)
    assert validate_message({"channelId": "777", "content": "Hi"}) == (777, "Hi")


def test_send_only_to_writable_channel_without_any_mentions():
    guild, channel = fake_guild()
    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == 555 else None)
    sent = asyncio.run(send_message(bot, 555, {"channelId": "777", "content": "Olá @everyone <@123>"}))
    assert sent == {"guildId": "555", "channelId": "777", "messageId": "888"}
    channel.send.assert_awaited_once()
    args = channel.send.await_args
    assert args.args[0] == "Olá @everyone <@123>"
    assert args.kwargs["allowed_mentions"].everyone is False
    assert args.kwargs["allowed_mentions"].users is False
    assert args.kwargs["allowed_mentions"].roles is False


def test_rejects_cross_guild_or_nonwritable_channel():
    guild, channel = fake_guild()
    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == 555 else None)
    with pytest.raises(LookupError, match="guild_not_found"):
        asyncio.run(send_message(bot, 111, {"channelId": "777", "content": "Hi"}))
    with pytest.raises(LookupError, match="channel_not_writable"):
        asyncio.run(send_message(bot, 555, {"channelId": "999", "content": "Hi"}))
    channel.permissions_for.return_value.send_messages = False
    assert not is_writable_channel(guild, channel)
    assert writable_destinations(SimpleNamespace(guilds=[guild]))[0]["channels"] == []
    channel.send.assert_not_awaited()


def test_admin_routes_require_internal_token_even_in_legacy_public_status_mode():
    bot = SimpleNamespace(is_ready=lambda: True, guilds=[], get_guild=lambda _: None)
    api = BotStatusApi(bot, "127.0.0.1", 8080, token=None, initialized_getter=lambda: True)
    req = make_mocked_request("GET", "/admin/message-targets")
    assert asyncio.run(api._handle_web_message_targets(req)).status == 503
    assert asyncio.run(api._handle_web_presence_get(req)).status == 503
    put = make_mocked_request("PUT", "/admin/presence")
    put.json = AsyncMock()
    assert asyncio.run(api._handle_web_presence_put(put)).status == 503
    put.json.assert_not_awaited()


def test_admin_send_endpoint_checks_guild_and_channel_before_discord_effect():
    guild, channel = fake_guild()
    bot = SimpleNamespace(is_ready=lambda: True, get_guild=lambda gid: guild if gid == 555 else None)
    api = BotStatusApi(bot, "127.0.0.1", 8080, token="internal", initialized_getter=lambda: True)
    req = make_mocked_request("POST", "/guilds/555/admin-message", match_info={"guild_id": "555"})
    req.json = AsyncMock(return_value={"channelId": "999", "content": "Hi"})
    response = asyncio.run(api._handle_web_message_send(req))
    assert response.status == 404
    assert json.loads(response.text)["error"] == "channel_not_writable"
    channel.send.assert_not_awaited()

    req.json = AsyncMock(return_value={"channelId": "777", "content": "Hi"})
    success = asyncio.run(api._handle_web_message_send(req))
    assert success.status == 200
    assert json.loads(success.text)["messageId"] == "888"
