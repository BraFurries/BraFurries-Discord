"""Guardrails for the BraFurries-only partner Discord runtime."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from aiohttp.test_utils import make_mocked_request

from core.partner_discord_runtime import (
    PartnerDiscordError,
    _id,
    _matches_embed,
    _find_thread,
    _grant_channel,
    _reconcile_thread_parent,
    _revoke_member,
    _require_channel_marker,
    _thread_parent,
    _slug,
    build_partner_embed,
)
from message_services.bot_status_api import BotStatusApi


def sample(**changes):
    data = {
        "partnerId": "8001",
        "name": "Parceiro Ônix",
        "description": "Contato e novidades",
        "links": [{"type": "website", "label": "Página", "url": "https://example.org"}],
    }
    data.update(changes)
    return data


def test_embed_contains_partner_description_links_and_owner_marker():
    embed = build_partner_embed(sample())
    assert embed.title == "Parceiro Ônix"
    assert embed.description == "Contato e novidades"
    assert "https://example.org" in embed.fields[0].value
    assert embed.footer.text == "BraFurries partner:8001"


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "ftp://example.org", "//example.org",
    "https://user:pass@example.org", "https:///missing-host",
])
def test_external_links_must_be_safe_http_urls(url):
    with pytest.raises(PartnerDiscordError):
        build_partner_embed(sample(links=[{"url": url}]))


def test_embed_enforces_discord_limits():
    with pytest.raises(PartnerDiscordError):
        build_partner_embed(sample(name="x" * 65))
    with pytest.raises(PartnerDiscordError):
        build_partner_embed(sample(description="x" * 2049))
    with pytest.raises(PartnerDiscordError):
        build_partner_embed(sample(links=[{"url": "https://x.example"}] * 51))


@pytest.mark.parametrize("value", [0, False, "0", "-123", "1.5", "٠١٢", str(2 ** 63)])
def test_discord_ids_are_strict_snowflakes(value):
    with pytest.raises(PartnerDiscordError):
        _id(value, "test")


def test_channel_ownership_marker_prevents_cross_partner_reuse():
    owned = SimpleNamespace(topic="Canal do cliente\nBraFurries partner:8001")
    assert _require_channel_marker(owned, 8001)
    with pytest.raises(PartnerDiscordError):
        _require_channel_marker(owned, 8002)
    assert not _require_channel_marker(SimpleNamespace(topic="Sem marcador"), 8001)


def test_partner_names_are_safe_channel_names():
    assert _slug("Criador Fúria!") == "criador-furia"


@pytest.mark.parametrize("token,ready,status", [
    (None, True, 503),
    ("internal-token", False, 503),
    ("internal-token", True, 200),
])
def test_internal_partner_endpoint_fails_closed(token, ready, status):
    guild = SimpleNamespace(id=123456789012345678)
    bot = SimpleNamespace(is_ready=lambda: ready, get_guild=lambda guild_id: guild)
    api = BotStatusApi(
        bot, host="127.0.0.1", port=8080, token=token, initialized_getter=lambda: True,
    )
    request = make_mocked_request(
        "POST", "/guilds/123456789012345678/partners/sync",
        match_info={"guild_id": "123456789012345678"},
    )
    found, error = api._internal_partner_guild(request)
    assert (200 if error is None else error.status) == status
    if status == 200:
        assert found is guild
    else:
        assert found is None


def test_invalid_json_payload_never_invokes_discord_effects():
    guild = SimpleNamespace(id=123456789012345678)
    bot = SimpleNamespace(is_ready=lambda: True, get_guild=lambda guild_id: guild)
    api = BotStatusApi(
        bot, host="127.0.0.1", port=8080,
        token="internal-token", initialized_getter=lambda: True,
    )
    request = make_mocked_request(
        "POST", "/guilds/123456789012345678/partners/sync",
        match_info={"guild_id": "123456789012345678"},
    )
    request.json = AsyncMock(return_value=["not-an-object"])
    operation = AsyncMock()
    result = asyncio.run(api._partner_operation(request, operation))
    assert result.status == 422
    assert json.loads(result.text)["error"] == "invalid_partner_request"
    operation.assert_not_awaited()

def test_spoofed_embed_from_other_bot_does_not_prove_ownership():
    message = SimpleNamespace(
        author=SimpleNamespace(id=77, bot=True),
        embeds=[build_partner_embed(sample())],
    )
    assert not _matches_embed(message, 8001, 88)
    assert _matches_embed(message, 8001, 77)


def test_thread_recovery_ignores_mutable_title_and_requires_original_bot():
    intro = SimpleNamespace(author=SimpleNamespace(id=55, bot=True),
                            embeds=[build_partner_embed(sample())])
    renamed_thread = SimpleNamespace(id=700, name="Nome antigo")
    parent = SimpleNamespace(
        guild=SimpleNamespace(me=SimpleNamespace(id=55)),
        threads=[renamed_thread],
        fetch_message=AsyncMock(return_value=intro),
    )

    async def archived_threads(limit):
        if False:
            yield

    parent.archived_threads = archived_threads
    found = asyncio.run(_find_thread(parent, 8001, "Nome novo"))
    assert found is renamed_thread
    parent.fetch_message.assert_awaited_once_with(700)

def test_revoke_member_overwrite_uses_member_not_untyped_discord_object():
    import discord
    member = SimpleNamespace(id=501)
    guild = SimpleNamespace(get_member=lambda member_id: member)
    channel = SimpleNamespace(
        guild=guild,
        overwrites_for=lambda user: discord.PermissionOverwrite(send_messages=True),
        set_permissions=AsyncMock(),
    )
    asyncio.run(_revoke_member(channel, 501))
    target = channel.set_permissions.await_args.args[0]
    overwrite = channel.set_permissions.await_args.kwargs["overwrite"]
    assert target is member
    assert overwrite.send_messages is False


def test_revocation_of_departed_member_removes_stale_member_overwrite():
    import discord
    not_found = discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"), "Unknown Member",
    )
    http = SimpleNamespace(delete_channel_permissions=AsyncMock())
    guild = SimpleNamespace(
        get_member=lambda _: None,
        fetch_member=AsyncMock(side_effect=not_found),
    )
    channel = SimpleNamespace(
        id=321,
        guild=guild,
        _state=SimpleNamespace(http=http),
        set_permissions=AsyncMock(),
    )
    asyncio.run(_revoke_member(channel, 501))
    http.delete_channel_permissions.assert_awaited_once()
    assert http.delete_channel_permissions.await_args.args[:2] == (321, 501)
    channel.set_permissions.assert_not_awaited()


def _permission_fixture(*, manageable=True):
    # Keep the overwrites mutable to simulate Discord's cache update events.
    everyone = Mock(id=1, permissions=discord.Permissions.none())
    bot = Mock(id=2, guild_permissions=discord.Permissions.none())
    bot_role = Mock(id=3, permissions=discord.Permissions.none())
    writer_role = Mock(id=4, permissions=discord.Permissions.none())
    writer_member = Mock(id=5, guild_permissions=discord.Permissions.none())
    admin_role = Mock(id=6, permissions=discord.Permissions(administrator=True))
    representative = Mock(id=7, guild_permissions=discord.Permissions.none())
    guild = SimpleNamespace(
        id=900,
        default_role=everyone,
        me=bot,
    )
    overrides = {
        everyone: discord.PermissionOverwrite(view_channel=False, send_messages=True,
                                                send_messages_in_threads=True),
        bot: discord.PermissionOverwrite(view_channel=True, send_messages=False),
        bot_role: discord.PermissionOverwrite(send_messages=True),
        writer_role: discord.PermissionOverwrite(view_channel=True,
                                                 send_messages=True,
                                                 create_public_threads=True,
                                                 manage_messages=True),
        writer_member: discord.PermissionOverwrite(view_channel=False,
                                                   send_messages_in_threads=True),
        admin_role: discord.PermissionOverwrite(send_messages=True),
        representative: discord.PermissionOverwrite(),
    }
    channel = SimpleNamespace(
        id=500,
        guild=guild,
        category_id=10,
        overwrites=overrides,
        overwrites_for=lambda subject: overrides.setdefault(subject, discord.PermissionOverwrite()),
        permissions_for=lambda subject: SimpleNamespace(manage_roles=manageable),
        set_permissions=AsyncMock(),
    )
    return channel, guild, SimpleNamespace(
        everyone=everyone, bot=bot, bot_role=bot_role, writer_role=writer_role,
        writer_member=writer_member, admin_role=admin_role, representative=representative,
    )


def test_existing_forum_permissions_are_reconciled_not_rejected_and_idempotent():
    channel, guild, subjects = _permission_fixture()
    asyncio.run(_reconcile_thread_parent(channel))

    default = channel.overwrites[subjects.everyone]
    assert default.view_channel is False   # never grant access to a private forum
    assert default.send_messages is False
    assert default.send_messages_in_threads is False
    assert default.create_public_threads is False

    role = channel.overwrites[subjects.writer_role]
    assert role.send_messages is False
    assert role.create_public_threads is False
    assert role.view_channel is True
    assert role.manage_messages is True  # unrelated moderation is preserved
    assert channel.overwrites[subjects.bot_role].send_messages is False
    assert channel.overwrites[subjects.writer_member].send_messages_in_threads is False
    assert channel.overwrites[subjects.writer_member].view_channel is False

    # Administrator bypasses channel permissions, and its original overwrite
    # must not be silently rewritten.
    assert channel.overwrites[subjects.admin_role].send_messages is True
    assert channel.overwrites[subjects.bot].send_messages is True
    assert channel.overwrites[subjects.bot].send_messages_in_threads is True
    assert channel.overwrites[subjects.bot].manage_threads is True
    assert channel.overwrites[subjects.bot].read_message_history is True

    first_run_calls = channel.set_permissions.await_count
    assert first_run_calls > 0
    asyncio.run(_reconcile_thread_parent(channel))
    assert channel.set_permissions.await_count == first_run_calls


def test_parent_selection_reconciles_an_existing_discord_forum(monkeypatch):
    channel, guild, subjects = _permission_fixture()
    forum = Mock(spec=discord.ForumChannel)
    forum.id = channel.id
    forum.guild = guild
    forum.category_id = 10
    forum.overwrites = channel.overwrites
    forum.overwrites_for = channel.overwrites_for
    forum.permissions_for = channel.permissions_for
    forum.set_permissions = channel.set_permissions
    monkeypatch.setattr(
        "core.partner_discord_runtime._get_channel",
        AsyncMock(return_value=forum),
    )
    async def selected():
        return await _thread_parent(guild, SimpleNamespace(id=10), 500)
    result, created = asyncio.run(selected())
    assert result is forum
    assert created is False
    assert forum.set_permissions.await_count > 0


def test_permission_reconciliation_requires_manage_roles_before_changes():
    channel, _, _ = _permission_fixture(manageable=False)
    with pytest.raises(PartnerDiscordError, match="Gerenciar Cargos"):
        asyncio.run(_reconcile_thread_parent(channel))
    channel.set_permissions.assert_not_awaited()


def test_exclusive_channel_reconciles_other_writers_but_keeps_representative():
    channel, _, subjects = _permission_fixture()
    asyncio.run(_grant_channel(channel, subjects.representative))

    assert channel.overwrites[subjects.everyone].view_channel is True
    assert channel.overwrites[subjects.everyone].send_messages is False
    assert channel.overwrites[subjects.writer_role].send_messages is False
    assert channel.overwrites[subjects.writer_role].manage_messages is True
    assert channel.overwrites[subjects.writer_member].send_messages_in_threads is False
    assert channel.overwrites[subjects.bot_role].send_messages is False
    assert channel.overwrites[subjects.bot].send_messages is True
    assert channel.overwrites[subjects.representative].view_channel is True
    assert channel.overwrites[subjects.representative].send_messages is True
    assert channel.overwrites[subjects.admin_role].send_messages is True

    initial_count = channel.set_permissions.await_count
    asyncio.run(_grant_channel(channel, subjects.representative))
    assert channel.set_permissions.await_count == initial_count
