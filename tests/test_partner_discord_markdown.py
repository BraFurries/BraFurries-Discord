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
    _edit_plain, _find_existing, _own, _recover_continuation, sync_markdown_thread,
)
from core.partner_discord_runtime import PartnerDiscordError, publish_partner_message


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


def test_emoji_count_uses_discord_utf16_units_not_python_character_count():
    with pytest.raises(PartnerDiscordError, match="Linha Markdown longa demais"):
        split_discord_markdown("🐑" * 1000)
    result = split_discord_markdown("🐑" * 800 + "\n\n" + "🐑" * 800)
    assert len(result) == 2
    assert all(len(item.encode("utf-16-le")) // 2 <= 1900 for item in result)


def test_plain_markdown_is_not_rewritten_into_embed_or_tracking_id():
    parts = render_announcement(sample())
    assert parts == ["# 🐑 **República Indie**\n\nConheça nosso espaço!\n\n## 🌾 Comunidade\n\n- 🎨 Artes"]
    assert "partner:42" not in "".join(parts)
    assert "https://discord.gg/indie" not in "".join(parts)


def test_partner_image_is_appended_as_its_own_preview_without_embed_body():
    url = "https://cdn.example.org/partners/42/image/logo.webp"
    parts = render_announcement(sample(imageUrl=url))
    assert parts[-1] == url
    assert parts[0].startswith("# 🐑")
    assert "partner:42" not in "".join(parts)


def test_invalid_partner_image_is_rejected():
    with pytest.raises(PartnerDiscordError):
        render_announcement(sample(imageUrl="file:///etc/passwd"))


def test_legacy_partner_without_custom_markdown_keeps_registered_links():
    text = "\n\n".join(render_announcement(sample(announcementMarkdown=None)))
    assert text.startswith("# República Indie")
    assert "https://discord.gg/indie" in text
    assert "https://indie.example" in text


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

def test_automatic_link_preview_is_not_mistaken_for_legacy_partner_embed():
    preview = discord.Embed(title="Website", url="https://nextfur.net")
    preview.set_footer(text="Website preview")
    message = SimpleNamespace(
        id=900, author=SimpleNamespace(id=99), content="# NextFUR https://nextfur.net",
        embeds=[preview], edit=AsyncMock(),
    )
    assert _own(message, 42, 99)
    asyncio.run(_edit_plain(message, message.content))
    message.edit.assert_not_awaited()


def test_foreign_legacy_owner_marker_is_never_adopted_as_plain_markdown():
    legacy = discord.Embed(title="Outro parceiro")
    legacy.set_footer(text="BraFurries partner:900")
    message = SimpleNamespace(
        author=SimpleNamespace(id=99), content="Texto", embeds=[legacy]
    )
    assert not _own(message, 42, 99)

def test_new_forum_starter_uses_plain_text_and_returns_continuation_ids():
    bot = SimpleNamespace(id=99)
    guild = SimpleNamespace(id=123, me=bot)
    intro = SimpleNamespace(
        id=2000, content="# Indie", author=bot, embeds=[], edit=AsyncMock(),
    )
    continuation = SimpleNamespace(
        id=2001, content="## Artes", author=bot, embeds=[], edit=AsyncMock(),
    )
    thread = Mock(spec=discord.Thread)
    thread.id = 2000
    thread.parent_id = 1000
    thread.guild = guild
    thread.name = "Indie"
    thread.archived = False
    thread.locked = True
    thread.send = AsyncMock(return_value=continuation)
    async def empty_history(limit):
        if False:
            yield
    thread.history = empty_history
    parent = Mock(spec=discord.ForumChannel)
    parent.id = 1000
    parent.guild = guild
    parent.threads = []
    async def empty_archives(limit):
        if False:
            yield
    parent.archived_threads = empty_archives
    parent.create_thread = AsyncMock(
        return_value=SimpleNamespace(thread=thread, message=intro)
    )

    ids = asyncio.run(sync_markdown_thread(
        parent, 42, "Indie", ["# Indie", "## Artes"], None, None, []
    ))
    assert ids == (2000, 2000, [2001])
    assert parent.create_thread.await_args.kwargs["content"] == "# Indie"
    assert parent.create_thread.await_args.kwargs["suppress_embeds"] is True
    assert "embed" not in parent.create_thread.await_args.kwargs
    thread.send.assert_awaited_once()
    assert thread.send.await_args.kwargs["suppress_embeds"] is True
    intro.edit.assert_not_awaited()


def test_deleted_thread_is_recreated_only_after_safe_reconciliation(monkeypatch):
    bot = SimpleNamespace(id=99)
    guild = SimpleNamespace(id=123, me=bot)
    intro = SimpleNamespace(id=3000, content="# Indie", author=bot, embeds=[], edit=AsyncMock())
    thread = Mock(spec=discord.Thread)
    thread.id = 3000
    thread.parent_id = 1000
    thread.guild = guild
    thread.name = "Indie"
    thread.archived = False
    thread.locked = True
    async def no_archived(limit):
        if False:
            yield
    parent = Mock(spec=discord.ForumChannel)
    parent.guild = guild
    parent.id = 1000
    parent.threads = []
    parent.archived_threads = no_archived
    parent.create_thread = AsyncMock(return_value=SimpleNamespace(thread=thread, message=intro))
    from core import partner_discord_markdown_sync as module
    missing = discord.NotFound(Mock(status=404, reason="Not Found"), "Unknown Channel")
    monkeypatch.setattr(module, "_get_channel", AsyncMock(side_effect=missing))
    result = asyncio.run(sync_markdown_thread(parent, 42, "Indie", ["# Indie"], 2000, 2000, [2001]))
    assert result == (3000, 3000, [])
    parent.create_thread.assert_awaited_once()


def test_deleted_thread_does_not_adopt_an_ambiguous_same_name(monkeypatch):
    bot = SimpleNamespace(id=99)
    guild = SimpleNamespace(id=123, me=bot)
    other = SimpleNamespace(id=9000, name="Indie")
    parent = Mock(spec=discord.ForumChannel)
    parent.id = 1000
    parent.guild = guild
    parent.threads = [other]
    async def no_archived(limit):
        if False:
            yield
    parent.archived_threads = no_archived
    other.fetch_message = AsyncMock(return_value=SimpleNamespace(id=9000, author=bot, content="# Indie", embeds=[]))
    from core import partner_discord_markdown_sync as module
    missing = discord.NotFound(Mock(status=404, reason="Not Found"), "Unknown Channel")
    monkeypatch.setattr(module, "_get_channel", AsyncMock(side_effect=missing))
    with pytest.raises(PartnerDiscordError, match="ambígua"):
        asyncio.run(sync_markdown_thread(parent, 42, "Indie", ["# Indie"], 2000, 2000, [2001]))
    parent.create_thread.assert_not_called()


def test_image_only_continuation_is_sent_with_preview_and_stored_id():
    bot = SimpleNamespace(id=99)
    guild = SimpleNamespace(id=123, me=bot)
    intro = SimpleNamespace(id=3000, content="# Indie", author=bot, embeds=[], edit=AsyncMock())
    photo = SimpleNamespace(id=3001, content="", author=bot, embeds=[], edit=AsyncMock())
    thread = Mock(spec=discord.Thread)
    thread.id = 3000
    thread.parent_id = 1000
    thread.guild = guild
    thread.name = "Indie"
    thread.archived = False
    thread.locked = True
    thread.send = AsyncMock(return_value=photo)
    async def no_archived(limit):
        if False:
            yield
    thread.history = no_archived
    parent = Mock(spec=discord.ForumChannel)
    parent.guild = guild
    parent.id = 1000
    parent.threads = []
    parent.archived_threads = no_archived
    parent.create_thread = AsyncMock(return_value=SimpleNamespace(thread=thread, message=intro))
    url = "https://cdn.example.org/partners/42/image/logo.webp"
    result = asyncio.run(sync_markdown_thread(
        parent, 42, "Indie", ["# Indie", url], None, None, [], image_url=url
    ))
    assert result == (3000, 3000, [3001])
    assert thread.send.await_args.kwargs["suppress_embeds"] is False


def test_representative_post_remains_allowed_after_plain_markdown_migration(monkeypatch):
    bot = SimpleNamespace(id=99)
    intro = SimpleNamespace(id=2000, author=bot, content="# Indie", embeds=[])
    posted = SimpleNamespace(id=2002)
    thread = Mock(spec=discord.Thread)
    thread.id = 2000
    thread.locked = True
    thread.archived = False
    thread.guild = SimpleNamespace(me=bot)
    thread.fetch_message = AsyncMock(return_value=intro)
    thread.send = AsyncMock(return_value=posted)
    monkeypatch.setattr(
        "core.partner_discord_runtime._get_channel", AsyncMock(return_value=thread)
    )
    result = asyncio.run(publish_partner_message(
        SimpleNamespace(id=123), {
            "partnerId": 42, "threadId": 2000,
            "threadIntroMessageId": 2000, "content": "Novidades da semana!",
        }
    ))
    assert result["messageId"] == "2002"
    thread.send.assert_awaited_once()

def test_untracked_plain_thread_cannot_be_claimed_by_another_partner():
    bot = SimpleNamespace(id=99)
    guild = SimpleNamespace(me=bot)
    thread = SimpleNamespace(id=2000, name="Indie")
    root = SimpleNamespace(author=bot, content="# Indie", embeds=[])
    async def archives(limit):
        if False:
            yield
    parent = Mock(spec=discord.ForumChannel)
    parent.guild = guild
    parent.threads = [thread]
    parent.archived_threads = archives
    thread.fetch_message = AsyncMock(return_value=root)
    # Matching text is not proof of ownership after removing public partner IDs.
    with pytest.raises(PartnerDiscordError, match="ambígua"):
        asyncio.run(_find_existing(parent, 42, "Indie", "# Indie"))
