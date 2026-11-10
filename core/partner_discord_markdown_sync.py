"""Idempotent Discord message reconciliation for partner Markdown announcements.

The first message is bound to the existing thread_intro_message_id; subsequent
message IDs are persisted by the API as ordered continuation references. No
internal partner ID is included in publicly visible messages.
"""
from __future__ import annotations

import io

import discord

from core.partner_discord_runtime import (
    PartnerDiscordError, _get_channel, _matches_embed, _validated_url,
)


def _own(message: discord.Message, partner_id: int, bot_id: int) -> bool:
    # Legacy embeds must have their original ownership marker. Converted plain
    # messages rely on the trusted API's stored thread/message identifiers.
    if getattr(message.author, "id", None) != bot_id:
        return False
    # Automatic previews of ordinary Markdown links are embeds too. A
    # nonempty bot-authored text message with a web preview is still ours.
    # Only explicit legacy BraFurries ownership markers are identity-bearing.
    markers = [
        getattr(getattr(embed, "footer", None), "text", "") or ""
        for embed in getattr(message, "embeds", ())
    ]
    ownership = [marker for marker in markers if marker.startswith("BraFurries partner:")]
    return (
        _matches_embed(message, partner_id, bot_id)
        if ownership else bool(getattr(message, "content", ""))
    )


async def _owned_message(thread: discord.Thread, message_id: int, partner_id: int):
    message = await thread.fetch_message(message_id)
    if not _own(message, partner_id, thread.guild.me.id):
        raise PartnerDiscordError("Mensagem existente não pertence à publicação do parceiro")
    return message


async def _edit_plain(message: discord.Message, text: str, *, image_preview: bool = False) -> None:
    legacy = any(
        (getattr(getattr(embed, "footer", None), "text", "") or "").startswith("BraFurries partner:")
        for embed in message.embeds
    )
    was_suppressed = bool(getattr(getattr(message, "flags", None), "suppress_embeds", False))
    if message.content != text or legacy or (image_preview and was_suppressed):
        kwargs = {
            "content": text,
            "suppress": not image_preview,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        # Explicit embed=None removes a legacy announcement embed, but would
        # also discard the native image preview from an image-only message.
        if not image_preview:
            kwargs["embed"] = None
        await message.edit(**kwargs)


_IMAGE_PREFIX = "brafurries-logo-"


def _managed_logo(attachment: discord.Attachment) -> bool:
    filename = getattr(attachment, "filename", "")
    if not isinstance(filename, str) or not filename.startswith(_IMAGE_PREFIX):
        return False
    digest = filename[len(_IMAGE_PREFIX):-5] if filename.endswith(".webp") else ""
    return len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest)


def _image_file(content: bytes, digest: str) -> discord.File:
    return discord.File(io.BytesIO(content), filename=f"{_IMAGE_PREFIX}{digest}.webp")


def _external_image_embed(image_url: str) -> discord.Embed:
    # Only the image uses a technical embed: the announcement's text stays
    # native Discord Markdown, not an embed description or title.
    return discord.Embed().set_image(url=_validated_url(image_url))


def _external_image_url(embed: discord.Embed) -> str | None:
    # Identify only our image-only rich embeds, not legacy partner embeds or
    # automatic link previews for the announcement's other URLs.
    data = embed.to_dict() if hasattr(embed, "to_dict") else {}
    if data.get("type", "rich") != "rich" or set(data) - {"type", "image"}:
        return None
    image = data.get("image")
    return image.get("url") if isinstance(image, dict) and isinstance(image.get("url"), str) else None


async def _edit_intro(
    message: discord.Message, text: str, image_bytes: bytes | None,
    image_digest: str | None, external_image_url: str | None = None,
) -> None:
    attachments = list(getattr(message, "attachments", ()) or ())
    ours = [attachment for attachment in attachments if _managed_logo(attachment)]
    wanted = f"{_IMAGE_PREFIX}{image_digest}.webp" if image_bytes is not None else None
    same_logo = (len(ours) == 1 and ours[0].filename == wanted) if wanted else not ours
    embeds = list(getattr(message, "embeds", ()) or ())
    legacy = any(
        (getattr(getattr(embed, "footer", None), "text", "") or "").startswith("BraFurries partner:")
        for embed in embeds
    )
    previews = [url for embed in embeds if (url := _external_image_url(embed))]
    # Do not consider arbitrary link-preview embeds as managed image previews.
    preview_ok = (previews == [external_image_url]) if external_image_url else not previews
    desired_suppression = external_image_url is None
    # Old/mocked messages may omit flags; don't mistake that for an active
    # unsuppressed image preview when the attachment is already correct.
    suppressed = bool(getattr(getattr(message, "flags", None), "suppress_embeds", desired_suppression))
    if (
        message.content == text and same_logo and preview_ok and not legacy
        and suppressed == desired_suppression
    ):
        return
    kwargs = {
        "content": text,
        "allowed_mentions": discord.AllowedMentions.none(),
        "suppress": desired_suppression,
    }
    if not same_logo:
        # Preserve all attachments we don't own, replacing only our logo.
        keep = [attachment for attachment in attachments if not _managed_logo(attachment)]
        kwargs["attachments"] = (
            [*keep, _image_file(image_bytes, image_digest)] if image_bytes is not None else keep
        )
    if external_image_url:
        kwargs["embed"] = _external_image_embed(external_image_url)
    elif legacy or previews:
        # Remove only our explicit image/legacy embed. Link previews in plain
        # Markdown are not used as identity or authorization evidence.
        kwargs["embed"] = None
    await message.edit(**kwargs)


async def _recover_continuation(
    thread: discord.Thread, text: str, excluded_ids: set[int], partner_id: int
):
    matches = []
    async for message in thread.history(limit=250):
        if message.id in excluded_ids or message.content != text:
            continue
        if _own(message, partner_id, thread.guild.me.id):
            matches.append(message)
            if len(matches) > 1:
                break
    if len(matches) > 1:
        raise PartnerDiscordError(
            "Mensagens de continuação ambíguas; reconciliação manual necessária"
        )
    return matches[0] if matches else None


async def _find_existing(
    parent: discord.ForumChannel | discord.TextChannel,
    partner_id: int, name: str, first_text: str,
) -> discord.Thread | None:
    candidates = list(getattr(parent, "threads", ()) or ())
    try:
        async for archived in parent.archived_threads(limit=500):
            candidates.append(archived)
    except discord.Forbidden as exc:
        raise PartnerDiscordError("Sem permissão para verificar threads arquivadas") from exc
    seen = set()
    matching = []
    ambiguous_title = False
    for thread in candidates:
        if thread.id in seen:
            continue
        seen.add(thread.id)
        try:
            starter = await (
                thread.fetch_message(thread.id)
                if isinstance(parent, discord.ForumChannel)
                else parent.fetch_message(thread.id)
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            raise PartnerDiscordError("Não foi possível validar threads existentes") from exc
        if _matches_embed(starter, partner_id, parent.guild.me.id):
            matching.append(thread)
        elif thread.name == name:
            # Plain bot-authored text has no partner identifier. Two partners
            # may have the same content, so a matching title is NOT proof of
            # ownership. Require a persisted thread ID, or manual recovery.
            ambiguous_title = True
    if len(matching) == 1:
        return matching[0]
    if matching or ambiguous_title or len(seen) >= 500:
        # A partial Discord creation might have succeeded before the API lost
        # its reply. Never silently create another same-name announcement.
        raise PartnerDiscordError(
            "Thread de divulgação ambígua; selecione/reconcilie a publicação existente"
        )
    return None


async def sync_markdown_thread(
    parent: discord.ForumChannel | discord.TextChannel,
    partner_id: int,
    name: str,
    parts: list[str],
    thread_id: int | None,
    intro_id: int | None,
    known_continuations: list[int],
    image_url: str | None = None,
    image_bytes: bytes | None = None,
    image_digest: str | None = None,
) -> tuple[int, int, list[int]]:
    if not parts:
        raise PartnerDiscordError("O anúncio não pode ficar vazio")
    # Only an external image URL needs an image-only preview. Managed R2
    # images already arrive as validated bytes for a native attachment.
    external_image = _validated_url(image_url.strip()) if image_bytes is None and image_url and image_url.strip() else None
    thread = None
    if thread_id is not None:
        try:
            channel = await _get_channel(parent.guild, thread_id)
        except discord.NotFound:
            # The persisted Discord thread was genuinely deleted. Recover
            # only after the bounded ownership/ambiguity scan below; never
            # adopt another bot's or partner's thread based on its title.
            channel = None
            intro_id = None
            known_continuations = []
        if channel is not None and (
            not isinstance(channel, discord.Thread) or channel.parent_id != parent.id
        ):
            raise PartnerDiscordError("A thread registrada pertence a outro canal")
        if channel is not None:
            root = await (
                channel.fetch_message(channel.id)
                if isinstance(parent, discord.ForumChannel)
                else parent.fetch_message(channel.id)
            )
            if not _own(root, partner_id, parent.guild.me.id):
                raise PartnerDiscordError("A thread registrada pertence a outra publicação")
            thread = channel
    if thread is None:
        # This scan is mandatory even if the stored thread was deleted:
        # a mutable same-name thread cannot prove ownership.
        thread = await _find_existing(parent, partner_id, name, parts[0])

    new_intro = None
    if thread is None:
        if isinstance(parent, discord.ForumChannel):
            initial = {
                "name": name, "content": parts[0], "auto_archive_duration": 1440,
                "allowed_mentions": discord.AllowedMentions.none(),
                "suppress_embeds": external_image is None,
                "reason": "Divulgação Markdown BraFurries",
            }
            if image_bytes is not None:
                initial["file"] = _image_file(image_bytes, image_digest)
            elif external_image:
                initial["embed"] = _external_image_embed(external_image)
            created = await parent.create_thread(**initial)
            thread, new_intro = created.thread, created.message
        else:
            starter = await parent.send(
                content=f"💬 Apresentação de {name} — abra a thread",
                allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True,
            )
            thread = await starter.create_thread(name=name, auto_archive_duration=1440)
    if thread.archived:
        await thread.edit(archived=False, reason="Atualização do parceiro")
    if not thread.locked or thread.name != name:
        await thread.edit(name=name, locked=True, reason="Vitrine somente leitura")

    bot_id = parent.guild.me.id
    intro_was_created_with_image = bool(new_intro is not None and (image_bytes is not None or external_image))
    if isinstance(parent, discord.ForumChannel):
        intro = new_intro or await _owned_message(thread, thread.id, partner_id)
        if intro_id is not None and intro_id != intro.id:
            raise PartnerDiscordError("Mensagem inicial da publicação não confere")
    else:
        # The starter in a text parent is outside the thread and acts as its
        # entry point. Replace any legacy embed with a plain teaser.
        root = await parent.fetch_message(thread.id)
        if not _own(root, partner_id, bot_id):
            raise PartnerDiscordError("Entrada da thread pertence a outra publicação")
        await _edit_plain(root, f"💬 Apresentação de {name} — abra a thread")
        intro = None
        if intro_id is not None:
            try:
                intro = await _owned_message(thread, intro_id, partner_id)
            except discord.NotFound:
                intro = None
        if intro is None:
            # Recover the legacy introduction, or a prior successfully sent
            # plain first part after an API timeout.
            matches = []
            async for candidate in thread.history(limit=250):
                if _matches_embed(candidate, partner_id, bot_id) or (
                    candidate.content == parts[0] and not candidate.embeds
                    and getattr(candidate.author, "id", None) == bot_id
                ):
                    matches.append(candidate)
            if len(matches) > 1:
                raise PartnerDiscordError("Mais de uma apresentação antiga; revise a thread")
            if matches:
                intro = matches[0]
            else:
                first = {
                    "content": parts[0],
                    "allowed_mentions": discord.AllowedMentions.none(),
                    "suppress_embeds": external_image is None,
                }
                if image_bytes is not None:
                    first["file"] = _image_file(image_bytes, image_digest)
                    intro_was_created_with_image = True
                elif external_image:
                    first["embed"] = _external_image_embed(external_image)
                    intro_was_created_with_image = True
                intro = await thread.send(**first)

    if not intro_was_created_with_image:
        await _edit_intro(intro, parts[0], image_bytes, image_digest, external_image)
    excluded = {intro.id, *known_continuations}
    applied = []
    for pos, content in enumerate(parts[1:]):
        message = None
        if pos < len(known_continuations):
            try:
                message = await _owned_message(thread, known_continuations[pos], partner_id)
            except discord.NotFound:
                message = None
        if message is None:
            message = await _recover_continuation(thread, content, excluded, partner_id)
        if message is None:
            message = await thread.send(
                content, allowed_mentions=discord.AllowedMentions.none(),
                suppress_embeds=not (image_url is not None and content == image_url)
            )
        else:
            await _edit_plain(message, content, image_preview=image_url is not None and content == image_url)
        excluded.add(message.id)
        applied.append(message.id)

    # Only delete surplus continuation messages whose IDs are persisted by the
    # API and whose authorship we have verified. Never touch representative posts.
    for stale_id in known_continuations[len(applied):]:
        try:
            stale = await _owned_message(thread, stale_id, partner_id)
        except discord.NotFound:
            continue
        await stale.delete()

    return thread.id, intro.id, applied
