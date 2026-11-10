"""Guardrails for the BraFurries-only partner Discord runtime."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request

from core.partner_discord_runtime import (
    PartnerDiscordError,
    _id,
    _matches_embed,
    _find_thread,
    _require_channel_marker,
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
