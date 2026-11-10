"""Discord-only effects for API-authorized BraFurries partner publications.

The API is the control plane. This module never reads partner/business tables,
selects representatives, or grants administrative rights.
"""
from __future__ import annotations

import asyncio
import re
import weakref
from typing import Any
from urllib.parse import urlsplit

import discord

_locks: weakref.WeakValueDictionary[tuple[int, int], asyncio.Lock] = weakref.WeakValueDictionary()
_NAME_CLEANUP = re.compile(r"[^a-z0-9-]+")
_MAX_EMBED_TOTAL = 5900


class PartnerDiscordError(ValueError):
    """Invalid or unsafe publication request, without a Discord side effect."""


def _id(value: Any, label: str, *, optional: bool = False) -> int | None:
    if optional and value in (None, ""):
        return None
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
        raise PartnerDiscordError(f"{label}: Discord ID inválido")
    number = int(value)
    if number < 1 or number >= 2 ** 63:
        raise PartnerDiscordError(f"{label}: Discord ID fora do intervalo")
    return number


def _slug(name: str) -> str:
    import unicodedata
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    slug = _NAME_CLEANUP.sub("-", ascii_name.replace(" ", "-")).strip("-")
    return (slug[:90].strip("-") or "parceiro")


def _marker(partner_id: int) -> str:
    return f"BraFurries partner:{partner_id}"


def _validated_url(url: Any) -> str:
    if not isinstance(url, str) or not 0 < len(url) <= 1024:
        raise PartnerDiscordError("URL de parceiro inválida")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise PartnerDiscordError("Apenas URLs HTTP(S) são permitidas")
    return url


def build_partner_embed(payload: dict[str, Any]) -> discord.Embed:
    """Validate all partner-sourced text before attempting a Discord write."""
    partner_id = _id(payload.get("partnerId"), "partnerId")
    name = payload.get("name")
    description = payload.get("description") or ""
    links = payload.get("links") or []
    if not isinstance(name, str) or not 0 < len(name.strip()) <= 64:
        raise PartnerDiscordError("Nome do parceiro inválido")
    if not isinstance(description, str) or len(description) > 2048:
        raise PartnerDiscordError("Descrição do parceiro inválida")
    if not isinstance(links, list) or len(links) > 50:
        raise PartnerDiscordError("Quantidade de links inválida")

    embed = discord.Embed(title=name.strip(), description=description.strip() or "Parceria BraFurries")
    text_total = len(name) + len(embed.description or "") + len(_marker(partner_id))
    chunks: list[str] = []
    current = ""
    for item in links:
        if not isinstance(item, dict):
            raise PartnerDiscordError("Link do parceiro inválido")
        url = _validated_url(item.get("url"))
        label = item.get("label") or item.get("type") or "Link"
        if not isinstance(label, str) or len(label) > 128:
            raise PartnerDiscordError("Rótulo de link inválido")
        line = f"• **{discord.utils.escape_markdown(label.strip() or 'Link')}**: <{url}>"
        if len(line) > 1024:
            raise PartnerDiscordError("Link longo demais para o Discord")
        if current and len(current) + 1 + len(line) > 1024:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}".strip()
    if current:
        chunks.append(current)
    if len(chunks) > 25 or text_total + sum(len(c) for c in chunks) > _MAX_EMBED_TOTAL:
        raise PartnerDiscordError("Descrição e links excedem o limite de publicação do Discord")
    for index, chunk in enumerate(chunks):
        embed.add_field(name="Links" if index == 0 else "Links (continuação)", value=chunk, inline=False)
    embed.set_footer(text=_marker(partner_id))
    return embed


def _matches_embed(message: discord.Message, partner_id: int) -> bool:
    return bool(getattr(message.author, "bot", False) and any(
        getattr(getattr(embed, "footer", None), "text", None) == _marker(partner_id)
        for embed in message.embeds
    ))


async def _get_channel(guild: discord.Guild, channel_id: int) -> Any:
    channel = guild.get_channel_or_thread(channel_id)
    if channel is None:
        channel = await guild.fetch_channel(channel_id)
    if getattr(getattr(channel, "guild", None), "id", None) != guild.id:
        raise PartnerDiscordError("Canal não pertence ao servidor informado")
    return channel


async def _category(guild: discord.Guild, requested_id: int | None) -> tuple[discord.CategoryChannel, bool]:
    if requested_id is not None:
        channel = await _get_channel(guild, requested_id)
        if not isinstance(channel, discord.CategoryChannel):
            raise PartnerDiscordError("A categoria selecionada não é uma categoria")
        return channel, False
    # Refuse ambiguous adoption: without an ID, a pre-existing matching name
    # must be selected explicitly by an administrator.
    if any(x.name.lower() == "parcerias brafurries" for x in guild.categories):
        raise PartnerDiscordError("A categoria já existe; selecione o ID existente")
    channel = await guild.create_category("Parcerias BraFurries", reason="Vitrine oficial de parceiros")
    return channel, True


async def _thread_parent(
    guild: discord.Guild, category: discord.CategoryChannel, requested_id: int | None
) -> tuple[discord.ForumChannel | discord.TextChannel, bool]:
    if requested_id is not None:
        channel = await _get_channel(guild, requested_id)
        if not isinstance(channel, (discord.ForumChannel, discord.TextChannel)):
            raise PartnerDiscordError("O canal de threads precisa ser Fórum ou Texto")
        if getattr(channel, "category_id", None) != category.id:
            raise PartnerDiscordError("O canal de threads não pertence à categoria configurada")
        # Public threads inherit parent permissions and cannot restrict
        # individual representatives.
        everyone = channel.permissions_for(guild.default_role)
        if everyone.send_messages or everyone.send_messages_in_threads:
            raise PartnerDiscordError("Configure o canal de threads como somente leitura")
        for subject, overwrite in channel.overwrites.items():
            if getattr(subject, "id", None) == getattr(guild.me, "id", None):
                continue
            if overwrite.send_messages is True or overwrite.send_messages_in_threads is True:
                raise PartnerDiscordError("Remova outras permissões de escrita antes de adotar o canal")
        return channel, False
    if any(x.name == "parceiros" and x.category_id == category.id for x in guild.channels):
        raise PartnerDiscordError("Já existe um canal parceiros nessa categoria; selecione seu ID")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=False, send_messages_in_threads=False,
            create_public_threads=False, create_private_threads=False,
        )
    }
    channel = await guild.create_forum(
        "parceiros", category=category, overwrites=overwrites,
        topic="Vitrine de parceiros BraFurries — postagens mediadas pelo Coddy",
        reason="Vitrine oficial de parceiros",
    )
    return channel, True


async def _find_thread(parent: discord.ForumChannel | discord.TextChannel, partner_id: int,
                       title: str) -> discord.Thread | None:
    candidates = list(getattr(parent, "threads", ()) or ())
    try:
        async for archived in parent.archived_threads(limit=100):
            candidates.append(archived)
    except discord.Forbidden:
        # Never pick a thread based on name alone when its ownership is uncertain.
        pass
    checked: set[int] = set()
    for thread in candidates:
        if thread.id in checked or thread.name != title:
            continue
        checked.add(thread.id)
        try:
            starter = await (thread.fetch_message(thread.id) if isinstance(parent, discord.ForumChannel)
                             else parent.fetch_message(thread.id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            continue
        if _matches_embed(starter, partner_id):
            return thread
    return None


async def _intro(thread: discord.Thread, known_id: int | None, partner_id: int,
                 embed: discord.Embed) -> int:
    message = None
    if known_id is not None:
        try:
            message = await thread.fetch_message(known_id)
        except discord.NotFound:
            pass
        if message is not None and not _matches_embed(message, partner_id):
            raise PartnerDiscordError("Mensagem inicial não pertence a este parceiro")
    if message is None:
        async for previous in thread.history(limit=100):
            if _matches_embed(previous, partner_id):
                message = previous
                break
    if message is None:
        message = await thread.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    else:
        await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    return message.id


async def _ensure_thread(parent: discord.ForumChannel | discord.TextChannel,
                         partner_id: int, name: str, embed: discord.Embed,
                         thread_id: int | None, intro_id: int | None) -> tuple[int, int]:
    thread = None
    if thread_id is not None:
        channel = await _get_channel(parent.guild, thread_id)
        if not isinstance(channel, discord.Thread) or channel.parent_id != parent.id:
            raise PartnerDiscordError("A thread registrada pertence a outro canal")
        try:
            starter = await (channel.fetch_message(channel.id)
                             if isinstance(parent, discord.ForumChannel)
                             else parent.fetch_message(channel.id))
        except (discord.NotFound, discord.Forbidden):
            raise PartnerDiscordError("Não foi possível validar a thread já publicada")
        if not _matches_embed(starter, partner_id):
            raise PartnerDiscordError("A thread informada pertence a outra publicação")
        thread = channel
    if thread is None:
        thread = await _find_thread(parent, partner_id, name)
    new_intro_id = None
    if thread is None:
        if isinstance(parent, discord.ForumChannel):
            created = await parent.create_thread(
                name=name, embed=embed, auto_archive_duration=10080,
                allowed_mentions=discord.AllowedMentions.none(),
                reason="Publicação de parceiro BraFurries",
            )
            thread = created.thread
            new_intro_id = created.message.id
        else:
            starter = await parent.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            thread = await starter.create_thread(name=name, auto_archive_duration=10080)
    if thread.archived:
        await thread.edit(archived=False, reason="Atualização do cadastro do parceiro")
    if not thread.locked or thread.name != name:
        await thread.edit(locked=True, name=name, reason="Vitrine somente leitura")
    # Forum initial posts live inside the thread; text-channel starters do not.
    resolved_intro_id = await _intro(thread, new_intro_id or intro_id, partner_id, embed)
    return thread.id, resolved_intro_id


def _require_channel_marker(channel: discord.TextChannel, partner_id: int) -> bool:
    topic = getattr(channel, "topic", None) or ""
    match = re.search(r"BraFurries partner:(\d+)", topic)
    if match and int(match.group(1)) != partner_id:
        raise PartnerDiscordError("Canal já vinculado a outro parceiro")
    return bool(match)


async def _resolve_member(guild: discord.Guild, user_id: int) -> discord.Member:
    member = guild.get_member(user_id)
    if member is None:
        member = await guild.fetch_member(user_id)
    if member.bot:
        raise PartnerDiscordError("O representante não pode ser um bot")
    return member


async def _grant_channel(channel: discord.TextChannel, member: discord.Member) -> None:
    everyone = channel.overwrites_for(channel.guild.default_role)
    everyone.view_channel = True
    everyone.send_messages = False
    everyone.create_public_threads = False
    everyone.create_private_threads = False
    everyone.send_messages_in_threads = False
    await channel.set_permissions(channel.guild.default_role, overwrite=everyone,
                                  reason="Canal do parceiro: leitura pública")
    overwrite = channel.overwrites_for(member)
    overwrite.view_channel = True
    overwrite.send_messages = True
    await channel.set_permissions(member, overwrite=overwrite, reason="Representante do parceiro")


async def _revoke_member(channel: discord.TextChannel, discord_user_id: int | None) -> None:
    if discord_user_id is None:
        return
    subject = discord.Object(id=discord_user_id)
    overwrite = channel.overwrites_for(subject)
    if overwrite.send_messages is not None:
        overwrite.send_messages = False
        await channel.set_permissions(subject, overwrite=overwrite, reason="Revogação de representante")


async def _ensure_exclusive(
    guild: discord.Guild, category: discord.CategoryChannel, partner_id: int,
    name: str, embed: discord.Embed, representative_id: int | None,
    previous_representative_id: int | None, enabled: bool,
    channel_id: int | None, existing_id: int | None,
    adopt_existing: bool, previously_managed: bool, intro_id: int | None,
) -> tuple[int | None, int | None, bool]:
    previous = None
    if channel_id is not None:
        candidate = await _get_channel(guild, channel_id)
        if not isinstance(candidate, discord.TextChannel):
            raise PartnerDiscordError("Canal do parceiro deixou de ser de texto")
        if not _require_channel_marker(candidate, partner_id):
            raise PartnerDiscordError("Canal registrado não possui marcador de propriedade")
        previous = candidate
    if not enabled:
        if previous is not None:
            await _revoke_member(previous, previous_representative_id)
            if previously_managed:
                overwrite = previous.overwrites_for(guild.default_role)
                overwrite.view_channel = False
                await previous.set_permissions(guild.default_role, overwrite=overwrite,
                                               reason="Canal exclusivo desativado")
        return (channel_id, intro_id, previously_managed)

    if representative_id is None:
        raise PartnerDiscordError("Vincule a conta Discord do representante antes de habilitar o canal")
    member = await _resolve_member(guild, representative_id)
    chosen = existing_id or channel_id
    if previous is not None and chosen != previous.id:
        await _revoke_member(previous, previous_representative_id)
        if previously_managed:
            overwrite = previous.overwrites_for(guild.default_role)
            overwrite.view_channel = False
            await previous.set_permissions(guild.default_role, overwrite=overwrite,
                                           reason="Canal exclusivo transferido")
    created = False
    if chosen is None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        channel = await guild.create_text_channel(
            _slug(name), category=category, overwrites=overwrites,
            topic=_marker(partner_id), reason="Canal exclusivo de parceiro",
        )
        created = True
    else:
        candidate = await _get_channel(guild, chosen)
        if not isinstance(candidate, discord.TextChannel):
            raise PartnerDiscordError("O canal exclusivo selecionado não é de texto")
        already_managed = _require_channel_marker(candidate, partner_id)
        if not already_managed and not adopt_existing:
            raise PartnerDiscordError("Confirme expressamente a adoção do canal existente")
        # Do not silently rewrite other custom role/user permissions. An
        # existing channel must already have no third-party write overrides.
        if not already_managed:
            for subject, overwrite in candidate.overwrites.items():
                if overwrite.send_messages is True and subject.id not in {
                    guild.default_role.id, member.id, guild.me.id
                } and not getattr(getattr(subject, "guild_permissions", None), "administrator", False):
                    raise PartnerDiscordError("Canal contém outros escritores; ajuste permissões antes de adotar")
            topic = (candidate.topic or "").strip()
            new_topic = f"{topic}\n{_marker(partner_id)}".strip()
            if len(new_topic) > 1024:
                raise PartnerDiscordError("Tópico do canal muito longo para anexar a identificação")
            await candidate.edit(topic=new_topic, reason="Adoção explícita de canal de parceiro")
        channel = candidate
        created = previously_managed and channel_id == channel.id
    if previous_representative_id != representative_id:
        await _revoke_member(channel, previous_representative_id)
    await _grant_channel(channel, member)
    message = None
    if intro_id is not None and channel.id == channel_id:
        try:
            message = await channel.fetch_message(intro_id)
        except discord.NotFound:
            pass
        if message is not None and not _matches_embed(message, partner_id):
            raise PartnerDiscordError("Mensagem inicial de canal não pertence ao parceiro")
    if message is None:
        async for previous_message in channel.history(limit=100):
            if _matches_embed(previous_message, partner_id):
                message = previous_message
                break
    if message is None:
        message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    else:
        await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    return channel.id, message.id, created


async def sync_partner_discord(guild: discord.Guild, request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise PartnerDiscordError("Requisição inválida")
    partner_id = _id(request.get("partnerId"), "partnerId")
    embed = build_partner_embed(request)
    category_id = _id(request.get("categoryId"), "categoryId", optional=True)
    parent_id = _id(request.get("threadParentChannelId"), "threadParentChannelId", optional=True)
    thread_id = _id(request.get("threadId"), "threadId", optional=True)
    intro_id = _id(request.get("threadIntroMessageId"), "threadIntroMessageId", optional=True)
    channel_id = _id(request.get("textChannelId"), "textChannelId", optional=True)
    channel_intro = _id(request.get("textIntroMessageId"), "textIntroMessageId", optional=True)
    existing_id = _id(request.get("existingTextChannelId"), "existingTextChannelId", optional=True)
    representative_id = _id(request.get("representativeDiscordUserId"), "representativeDiscordUserId", optional=True)
    previous_representative_id = _id(request.get("lastRepresentativeDiscordUserId"), "lastRepresentativeDiscordUserId", optional=True)
    exclusive_enabled = request.get("exclusiveChannelEnabled") is True
    active = request.get("active") is True
    if exclusive_enabled and active and representative_id is None:
        raise PartnerDiscordError("Representante sem conta Discord vinculada")
    key = (guild.id, partner_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    async with lock:
        if active:
            category, created_category = await _category(guild, category_id)
            parent, created_parent = await _thread_parent(guild, category, parent_id)
            thread_id, intro_id = await _ensure_thread(
                parent, partner_id, request["name"].strip(), embed, thread_id, intro_id
            )
        else:
            created_category = created_parent = False
            if thread_id is not None:
                channel = await _get_channel(guild, thread_id)
                if not isinstance(channel, discord.Thread):
                    raise PartnerDiscordError("Thread inválida")
                await channel.edit(locked=True, archived=True, reason="Parceria inativa")
        if active or channel_id is not None:
            text_id, text_intro, text_managed = await _ensure_exclusive(
                guild, category if active else None, partner_id, request["name"].strip(),
                embed, representative_id, previous_representative_id,
                exclusive_enabled and active, channel_id, existing_id,
                request.get("adoptExistingTextChannel") is True,
                request.get("textChannelCreatedByBot") is True, channel_intro,
            )
        else:
            text_id, text_intro, text_managed = channel_id, channel_intro, False
        return {
            "categoryId": str(category_id or category.id) if active else (str(category_id) if category_id else None),
            "categoryCreatedByBot": bool(created_category or request.get("categoryCreatedByBot")),
            "threadParentChannelId": str(parent_id or parent.id) if active else (str(parent_id) if parent_id else None),
            "threadParentCreatedByBot": bool(created_parent or request.get("threadParentCreatedByBot")),
            "threadId": str(thread_id) if thread_id else None,
            "threadIntroMessageId": str(intro_id) if intro_id else None,
            "textChannelId": str(text_id) if text_id else None,
            "textIntroMessageId": str(text_intro) if text_intro else None,
            "textChannelCreatedByBot": text_managed,
            "representativeDiscordUserId": str(representative_id) if exclusive_enabled and active else None,
        }


async def publish_partner_message(guild: discord.Guild, request: dict[str, Any]) -> dict[str, str]:
    partner_id = _id(request.get("partnerId"), "partnerId")
    thread_id = _id(request.get("threadId"), "threadId")
    content = request.get("content")
    if not isinstance(content, str) or not 0 < len(content.strip()) <= 1900:
        raise PartnerDiscordError("Mensagem deve ter de 1 a 1900 caracteres")
    thread = await _get_channel(guild, thread_id)
    if not isinstance(thread, discord.Thread) or not thread.locked:
        raise PartnerDiscordError("A vitrine do parceiro não está protegida")
    valid = False
    async for message in thread.history(limit=100):
        if _matches_embed(message, partner_id):
            valid = True
            break
    if not valid:
        raise PartnerDiscordError("Thread não está registrada para este parceiro")
    if thread.archived:
        await thread.edit(archived=False, reason="Nova publicação do parceiro")
    message = await thread.send(content.strip(), allowed_mentions=discord.AllowedMentions.none())
    return {"messageId": str(message.id), "threadId": str(thread.id)}
