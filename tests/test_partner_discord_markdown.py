"""Markdown-specific partner announcements: format, safety and reconciliation."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from core.partner_discord_markdown import (
    decode_part_ids, render_announcement, split_discord_markdown,
)
from core.partner_discord_markdown_sync import (
    _edit_plain, _recover_continuation, sync_markdown_thread,
)
from core.partner_discord_runtime import PartnerDiscordError


def sample(**changes):
    raw = {
        "partnerId": 42,
        "name": "República Indie",
        "description": "Uma comunidade",
        "announcementMarkdown": "# 🐑 **República Indie**\n\nConheça nosso espaço!\n\n## 🌾 Comunidade\n\n- 🎨 Artes",
        "announcementLinksMode": "INLINE_ONLY",
        "links": [
            {"label": "Discord", "url": "https://discord.gg/indie"},
            {"label": "Site", "url": "https://indie.example"},
        ],
    }
    raw.update(changes)
    return raw


def test_plain_markdown_is_not_rewritten_into_embed_or_tracking_id():
    parts = render_announcement(sample())
    assert parts == ["# 🐑 **República Indie**\n\nConheça nosso espaço!\n\n## 🌾 Comunidade\n\n- 🎨 Artes"]
    assert "partner:42" not in "".join(parts)
    assert "https://discord.gg/indie" not in "".join(parts)


def test_link_modes_preserve_inline_links_and_add_only_missing_links():
    announcement = "# Indie\n\nEntre pelo https://discord.gg/indie"
    parts = render_announcement(sample(
        announcementMarkdown=announcement, announcementLinksMode="APPEND_MISSING"
    ))
    content = "\n\n".join(parts)
    assert content.count("https://discord.gg/indie") == 1
    assert "https://indie.example" in content
    assert content.startswith(announcement)


def test_public_contact_is_optional_not_an_automatic_discord_representative():
    assert "Contato" not in "\n\n".join(render_announcement(sample()))
    combined = "\n\n".join(render_announcement(sample(
        announcementPublicContact="Parcerias: @everyone",
    )))
    assert "📩 **Contato:**" in combined
    assert "@everyone" not in combined   # escaped, AllowedMentions.none at send


def test_long_announcement_is_split_at_paragraphs_without_cutting_links():
    text = ("# NextFUR\n\n" + "Um evento da comunidade " * 43 + "\n\n") * 3
    parts = render_announcement(sample(announcementMarkdown=text))
    assert len(parts) > 1
    assert all(0 < len(part) <= 1900 for part in parts)
    assert "NextFUR" in "\n\n".join(parts)


@pytest.mark.parametrize("text", ["", "x" * 1901])
def test_rejects_invalid_or_unbreakable_markdown(text):
    with pytest.raises(PartnerDiscordError):
        split_discord_markdown(text)


@pytest.mark.parametrize("values", [["7", "7"], ["0"], ["1"] * 12, "7"])
def test_rejects_invalid_continuation_identifiers(values):
    with pytest.raises(PartnerDiscordError):
        decode_part_ids(values)


def test_markdown_edit_clears_legacy_embed_only_when_needed():
    original = SimpleNamespace(
        content="", embeds=[discord.Embed(title="Legacy")], edit=AsyncMock()
    )
    asyncio.run(_edit_plain(original, "# República Indie"))
    original.edit.assert_awaited_once()
    assert original.edit.await_args.kwargs["embed"] is None
    assert original.edit.await_args.kwargs["content"] == "# República Indie"


def test_missing_continuation_is_reused_after_api_timeout():
    bot = SimpleNamespace(id=2)
    recovered = SimpleNamespace(id=1002, content="## Projetos", embeds=[], author=bot)
    async def history(limit):
        yield recovered
    thread = SimpleNamespace(
        guild=SimpleNamespace(me=bot), history=history,
    )
    matched = asyncio.run(_recover_continuation(thread, "## Projetos", {1000}, 42))
    assert matched is recovered


def test_ambiguous_continuations_fail_closed_without_extra_send():
    bot = SimpleNamespace(id=2)
    async def history(limit):
        for ident in (1002, 1003):
            yield SimpleNamespace(id=ident, content="## Projetos", embeds=[], author=bot)
    thread = SimpleNamespace(guild=SimpleNamespace(me=bot), history=history)
    with pytest.raises(PartnerDiscordError, match="ambíguas"):
        asyncio.run(_recover_continuation(thread, "## Projetos", set(), 42))


def test_existing_forum_intro_and_continuation_are_edited_not_reposted(monkeypatch):
    bot = SimpleNamespace(id=99)
    guild = SimpleNamespace(id=123, me=bot)
    root = SimpleNamespace(
        id=2000, content="", author=bot, embeds=[discord.Embed(
            title="Antigo", description="Descrição"
        )], edit=AsyncMock(),
    )
    root.embeds[0].set_footer(text="BraFurries partner:42")
    continuation = SimpleNamespace(
        id=2001, content="Texto antigo", author=bot, embeds=[], edit=AsyncMock(),
    )
    async def history(limit):
        if False:
            yield
    thread = Mock(spec=discord.Thread)
    thread.id = 2000
    thread.parent_id = 1000
    thread.guild = guild
    thread.archived = False
    thread.locked = True
    thread.name = "República Indie"
    thread.fetch_message = AsyncMock(side_effect=lambda i: {2000: root, 2001: continuation}[i])
    thread.history = history
    thread.send = AsyncMock()
    parent = Mock(spec=discord.ForumChannel)
    parent.id = 1000
    parent.guild = guild
    from core import partner_discord_markdown_sync as module
    monkeypatch.setattr(module, "_get_channel", AsyncMock(return_value=thread))
    ids = asyncio.run(sync_markdown_thread(
        parent, 42, "República Indie",
        ["# Indie", "## Projetos"], 2000, 2000, [2001],
    ))
    assert ids == (2000, 2000, [2001])
    root.edit.assert_awaited_once()
    continuation.edit.assert_awaited_once()
    thread.send.assert_not_awaited()
