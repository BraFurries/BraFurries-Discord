"""Render Discord partner announcements as plain Markdown messages.

Partner identity remains in the API/database. Never display platform IDs in
the announcement or add invisible tracking characters to user text.
"""
from __future__ import annotations

from typing import Any

import discord

from core.partner_discord_runtime import PartnerDiscordError, _validated_url

_MAX_SOURCE = 16000
_MAX_PART = 1900
_MAX_PARTS = 12
_MAX_CONTACT = 255


def _discord_units(text: str) -> int:
    # Discord message length is safest measured in UTF-16 code units.
    # Python's len() counts astral emojis once, whereas Discord clients may
    # count the surrogate pair as two characters.
    return len(text.encode("utf-16-le")) // 2


def _sections(text: str) -> list[str]:
    """Keep paragraphs/sections together so Markdown formatting survives splits."""
    pieces: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if _discord_units(line) > _MAX_PART:
            raise PartnerDiscordError(
                "Linha Markdown longa demais para o Discord; quebre-a em frases menores"
            )
        if line.strip() == "":
            if current:
                pieces.append("\n".join(current))
                current = []
        else:
            if current and _discord_units("\n".join(current)) + 1 + _discord_units(line) > _MAX_PART:
                # Lines are valid boundaries if a paragraph is unusually long.
                pieces.append("\n".join(current))
                current = []
            current.append(line)
    if current:
        pieces.append("\n".join(current))
    return pieces


def split_discord_markdown(text: str) -> list[str]:
    """Return stable, contiguous chunks without splitting URLs or Markdown lines."""
    if not text.strip():
        raise PartnerDiscordError("O anúncio não pode ficar vazio")
    parts: list[str] = []
    current = ""
    for section in _sections(text.strip()):
        if _discord_units(section) > _MAX_PART:
            raise PartnerDiscordError("Seção Markdown longa demais para o Discord")
        candidate = f"{current}\n\n{section}" if current else section
        if _discord_units(candidate) > _MAX_PART:
            parts.append(current)
            current = section
        else:
            current = candidate
    if current:
        parts.append(current)
    if len(parts) > _MAX_PARTS:
        raise PartnerDiscordError("Anúncio extenso demais; limite de 12 mensagens na thread")
    return parts


def render_announcement(payload: dict[str, Any]) -> list[str]:
    markdown = payload.get("announcementMarkdown")
    name = payload.get("name")
    description = payload.get("description") or ""
    if not isinstance(name, str) or not 0 < len(name.strip()) <= 64:
        raise PartnerDiscordError("Nome do parceiro inválido")
    if markdown is not None and not isinstance(markdown, str):
        raise PartnerDiscordError("Markdown de divulgação inválido")
    if markdown is not None and len(markdown) > _MAX_SOURCE:
        raise PartnerDiscordError("Anúncio Markdown acima de 16.000 caracteres")

    # Default for older publications: plain Markdown, never another embed.
    custom_announcement = bool(markdown and markdown.strip())
    content = markdown.strip() if custom_announcement else (
        f"# {discord.utils.escape_markdown(name.strip())}\n\n{description.strip() or 'Parceria BraFurries'}"
    )
    mode = payload.get("announcementLinksMode") or "INLINE_ONLY"
    if mode not in {"INLINE_ONLY", "APPEND_MISSING"}:
        raise PartnerDiscordError("Modo de links inválido")
    links = payload.get("links") or []
    if not isinstance(links, list) or len(links) > 50:
        raise PartnerDiscordError("Links da parceria inválidos")
    # Preserve website/invite links for pre-existing partners without a custom
    # announcement, while custom Markdown defaults to no automatic link block.
    if mode == "APPEND_MISSING" or not custom_announcement:
        missing: list[str] = []
        for item in links:
            if not isinstance(item, dict):
                raise PartnerDiscordError("Link da parceria inválido")
            url = _validated_url(item.get("url"))
            # Explicit raw or Markdown URLs already count as published.
            if url in content:
                continue
            label = item.get("label") or item.get("type") or "Link"
            if not isinstance(label, str) or len(label) > 128:
                raise PartnerDiscordError("Rótulo de link inválido")
            missing.append(f"- **{discord.utils.escape_markdown(label)}:** <{url}>")
        if missing:
            content += "\n\n🔗 **Outros links**\n" + "\n".join(missing)

    contact = payload.get("announcementPublicContact")
    if contact is not None and (not isinstance(contact, str) or len(contact) > _MAX_CONTACT):
        raise PartnerDiscordError("Contato público inválido")
    if contact and contact.strip() and contact.strip() not in content:
        content += "\n\n📩 **Contato:** " + discord.utils.escape_mentions(contact.strip())

    if len(content) > _MAX_SOURCE + 8000:
        raise PartnerDiscordError("Anúncio final excede os limites do Discord")
    parts = split_discord_markdown(content)
    # The image is a dedicated continuation with a native Discord image
    # preview. The announcement itself remains plain Markdown (no embed).
    image_url = payload.get("imageUrl")
    if image_url is not None:
        if not isinstance(image_url, str):
            raise PartnerDiscordError("Imagem do parceiro inválida")
        if image_url.strip():
            parts.append(_validated_url(image_url.strip()))
    if len(parts) > 12:
        raise PartnerDiscordError("Anúncio excede o número de mensagens permitido")
    return parts


def decode_part_ids(raw: Any) -> list[int]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > _MAX_PARTS - 1:
        raise PartnerDiscordError("Lista de mensagens do anúncio inválida")
    from core.partner_discord_runtime import _id
    result = [_id(value, "announcementPartMessageId") for value in raw]
    if len(set(result)) != len(result):
        raise PartnerDiscordError("IDs de mensagens repetidos")
    return result
