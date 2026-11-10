"""Idempotent Discord message reconciliation for partner Markdown announcements.

The first message is bound to the existing thread_intro_message_id; subsequent
message IDs are persisted by the API as ordered continuation references. No
internal partner ID is included in publicly visible messages.
"""
from __future__ import annotations

import discord

from core.partner_discord_runtime import (
    PartnerDiscordError, _get_channel, _matches_embed,
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
    if message.content != text or legacy or image_preview:
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
) -> tuple[int, int, list[int]]:
    if not parts:
        raise PartnerDiscordError("O anúncio não pode ficar vazio")
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
            created = await parent.create_thread(
                name=name, content=parts[0], auto_archive_duration=1440,
                allowed_mentions=discord.AllowedMentions.none(),
                suppress_embeds=True,
                reason="Divulgação Markdown BraFurries",
            )
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
            intro = matches[0] if matches else await thread.send(
                parts[0], allowed_mentions=discord.AllowedMentions.none(),
                suppress_embeds=True
            )

    await _edit_plain(intro, parts[0])
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
