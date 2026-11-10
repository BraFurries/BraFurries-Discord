"""Discord-only effects for API-authorized BraFurries partner publications.

The API is the control plane. This module never reads partner/business tables,
selects representatives, or grants administrative rights.
"""
from __future__ import annotations

import asyncio
import logging
import re
import weakref
from typing import Any
from urllib.parse import urlsplit

import discord

_locks: weakref.WeakValueDictionary[tuple[int, int], asyncio.Lock] = weakref.WeakValueDictionary()
_NAME_CLEANUP = re.compile(r"[^a-z0-9-]+")
_MAX_EMBED_TOTAL = 5900
_WRITE_BITS = ("send_messages", "send_messages_in_threads", "create_public_threads", "create_private_threads")
_logger = logging.getLogger(__name__)


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
    if any(ch.isspace() or ch in "<>\`" for ch in url):
        raise PartnerDiscordError("URL contém caracteres inseguros para o Discord")
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

    embed = discord.Embed(
        title=discord.utils.escape_mentions(name.strip()),
        description=discord.utils.escape_mentions(description.strip()) or "Parceria BraFurries",
    )
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


def _matches_embed(message: discord.Message, partner_id: int, bot_id: int) -> bool:
    # Another bot can mimic a footer; only Coddy's own message proves ownership.
    return bool(getattr(message.author, "id", None) == bot_id and any(
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



def _is_administrator(subject: Any) -> bool:
    # Discord's Administrator bypasses channel denies. Explicitly inspect the
    # concrete target type: unknown/departed members are discord.Object and
    # must not be treated as administrators.
    if isinstance(subject, discord.Role):
        return subject.permissions.administrator
    if isinstance(subject, discord.Member):
        return subject.guild_permissions.administrator
    return False


def _require_manage_overwrites(channel: discord.abc.GuildChannel) -> None:
    # set_permissions needs Manage Roles, not Manage Channels. Check before
    # making a partial series of permission changes.
    if not channel.permissions_for(channel.guild.me).manage_roles:
        raise PartnerDiscordError(
            "Coddy precisa de Gerenciar Cargos no canal para corrigir as permissões da vitrine"
        )


async def _repair_overwrite(
    channel: discord.abc.GuildChannel,
    target: discord.Member | discord.Role,
    updates: dict[str, bool],
    *,
    reason: str,
    existing: discord.PermissionOverwrite | None = None,
) -> bool:
    # Uncached Object targets need the exact typed overwrite returned by
    # channel.overwrites; overwrites_for(Object) guesses the wrong type for
    # uncached roles and could discard unrelated permission bits.
    overwrite = existing if existing is not None else channel.overwrites_for(target)
    drift = {key: value for key, value in updates.items() if getattr(overwrite, key) is not value}
    if not drift:
        return False
    for key, value in drift.items():
        setattr(overwrite, key, value)
    # Preserve all existing read, moderation and unrelated permissions.
    # discord.py may return a type-aware Object for an uncached role or a
    # former member. GuildChannel.set_permissions rejects that object, so
    # apply its typed overwrite directly instead of aborting all partners.
    if isinstance(target, discord.Object):
        kind = getattr(target, "type", None)
        if kind is discord.Role:
            overwrite_type = 0
        elif kind in (discord.User, discord.Member):
            overwrite_type = 1
        else:
            raise PartnerDiscordError("Sobrescrita Discord sem tipo verificável")
        allow, deny = overwrite.pair()
        await channel._state.http.edit_channel_permissions(
            channel.id, target.id, str(allow.value), str(deny.value),
            overwrite_type, reason=reason,
        )
    else:
        await channel.set_permissions(target, overwrite=overwrite, reason=reason)
    _logger.info(
        "Partner Discord permissions reconciled guild=%s channel=%s target=%s fields=%s",
        channel.guild.id, channel.id, target.id, ",".join(sorted(drift)),
    )
    return True


async def _deny_other_writers(
    channel: discord.abc.GuildChannel, protected_ids: set[int], *, reason: str
) -> None:
    for subject, overwrite in tuple(channel.overwrites.items()):
        if subject.id in protected_ids or _is_administrator(subject):
            continue
        drift = {key: False for key in _WRITE_BITS if getattr(overwrite, key) is True}
        if drift:
            await _repair_overwrite(channel, subject, drift, reason=reason, existing=overwrite)


async def _reconcile_thread_parent(channel: discord.ForumChannel | discord.TextChannel) -> None:
    # The API-selected destination is authoritative. Coddy enforces a
    # read-only directory at the channel level; no per-thread ACL exists.
    guild = channel.guild
    _require_manage_overwrites(channel)
    reason = "BraFurries: reconciliacao de escrita da vitrine de parceiros"
    await _repair_overwrite(
        channel, guild.default_role, {key: False for key in _WRITE_BITS}, reason=reason
    )
    # Including roles held by Coddy is intentional: its own member overwrite
    # below restores its ability to post without granting it to other members.
    await _deny_other_writers(channel, {guild.default_role.id, guild.me.id}, reason=reason)
    await _repair_overwrite(
        channel, guild.me,
        {
            "view_channel": True,
            "read_message_history": True,
            "send_messages": True,
            "send_messages_in_threads": True,
            "create_public_threads": True,
            "manage_threads": True,
        },
        reason=reason,
    )


async def _thread_parent(
    guild: discord.Guild, category: discord.CategoryChannel, requested_id: int | None
) -> tuple[discord.ForumChannel | discord.TextChannel, bool]:
    if requested_id is not None:
        channel = await _get_channel(guild, requested_id)
        if not isinstance(channel, (discord.ForumChannel, discord.TextChannel)):
            raise PartnerDiscordError("O canal de threads precisa ser Fórum ou Texto")
        if getattr(channel, "category_id", None) != category.id:
            raise PartnerDiscordError("O canal de threads não pertence à categoria configurada")
        await _reconcile_thread_parent(channel)
        return channel, False
    if any(x.name == "parceiros" and x.category_id == category.id for x in guild.channels):
        raise PartnerDiscordError("Já existe um canal parceiros nessa categoria; selecione seu ID")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=False, send_messages_in_threads=False,
            create_public_threads=False, create_private_threads=False,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, send_messages_in_threads=True,
            create_public_threads=True, manage_threads=True, read_message_history=True,
        ),
    }
    channel = await guild.create_forum(
        "parceiros", category=category, overwrites=overwrites,
        topic="Vitrine de parceiros BraFurries — postagens mediadas pelo Coddy",
        reason="Vitrine oficial de parceiros",
    )
    return channel, True


async def _find_thread(parent: discord.ForumChannel | discord.TextChannel, partner_id: int,
                       title: str) -> discord.Thread | None:
    # Identity must be in the starter embed, never in a mutable title. This
    # allows recovery after a crash between Discord creation and DB persistence.
    candidates = list(getattr(parent, "threads", ()) or ())
    try:
        async for archived in parent.archived_threads(limit=500):
            candidates.append(archived)
    except discord.Forbidden as exc:
        raise PartnerDiscordError("Sem permissão para verificar threads arquivadas") from exc
    checked: set[int] = set()
    for thread in candidates:
        if thread.id in checked:
            continue
        checked.add(thread.id)
        try:
            starter = await (thread.fetch_message(thread.id) if isinstance(parent, discord.ForumChannel)
                             else parent.fetch_message(thread.id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            raise PartnerDiscordError("Não foi possível verificar a propriedade das threads existentes") from exc
        if _matches_embed(starter, partner_id, parent.guild.me.id):
            return thread
    # Beyond the bounded archive search, duplicate detection is uncertain.
    # Reject auto-creation instead of silently creating a second partner thread.
    if len(candidates) >= 500:
        raise PartnerDiscordError("Vitrine com muitas threads arquivadas; reconciliação manual necessária")
    return None


async def _verify_starter_owner(thread: discord.Thread, partner_id: int) -> None:
    """Validate ownership against the original forum/text parent starter."""
    parent = await _get_channel(thread.guild, thread.parent_id)
    try:
        starter = await (
            thread.fetch_message(thread.id)
            if isinstance(parent, discord.ForumChannel)
            else parent.fetch_message(thread.id)
        )
    except (discord.NotFound, discord.Forbidden) as exc:
        raise PartnerDiscordError("Não foi possível validar a publicação original") from exc
    from core.partner_discord_markdown_sync import _own
    if not _own(starter, partner_id, thread.guild.me.id):
        raise PartnerDiscordError("Thread vinculada a outra publicação")


async def _intro(thread: discord.Thread, known_id: int | None, partner_id: int,
                 embed: discord.Embed) -> int:
    message = None
    if known_id is not None:
        try:
            message = await thread.fetch_message(known_id)
        except discord.NotFound:
            pass
        if message is not None and not _matches_embed(message, partner_id, thread.guild.me.id):
            raise PartnerDiscordError("Mensagem inicial não pertence a este parceiro")
    if message is None:
        async for previous in thread.history(limit=100):
            if _matches_embed(previous, partner_id, thread.guild.me.id):
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
        if not _matches_embed(starter, partner_id, parent.guild.me.id):
            raise PartnerDiscordError("A thread informada pertence a outra publicação")
        thread = channel
    if thread is None:
        thread = await _find_thread(parent, partner_id, name)
    new_intro_id = None
    if thread is None:
        if isinstance(parent, discord.ForumChannel):
            created = await parent.create_thread(
                name=name, embed=embed, auto_archive_duration=1440,
                allowed_mentions=discord.AllowedMentions.none(),
                reason="Publicação de parceiro BraFurries",
            )
            thread = created.thread
            new_intro_id = created.message.id
        else:
            starter = await parent.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            thread = await starter.create_thread(name=name, auto_archive_duration=1440)
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
    # A partner's explicitly selected/adopted channel has one regular writer:
    # their verified representative. Existing role/member write overrides are
    # drift, including those that could override @everyone's deny.
    guild = channel.guild
    _require_manage_overwrites(channel)
    reason = "BraFurries: reconciliacao de escrita exclusiva do parceiro"
    await _repair_overwrite(
        channel, guild.default_role,
        {"view_channel": True, **{key: False for key in _WRITE_BITS}},
        reason=reason,
    )
    await _deny_other_writers(
        channel, {guild.default_role.id, guild.me.id, member.id}, reason=reason
    )
    await _repair_overwrite(
        channel, guild.me,
        {"view_channel": True, "send_messages": True, "read_message_history": True},
        reason=reason,
    )
    await _repair_overwrite(
        channel, member, {"view_channel": True, "send_messages": True},
        reason=reason,
    )


async def _revoke_member(channel: discord.TextChannel, discord_user_id: int | None) -> None:
    if discord_user_id is None:
        return
    member = channel.guild.get_member(discord_user_id)
    if member is None:
        try:
            member = await channel.guild.fetch_member(discord_user_id)
        except discord.NotFound:
            # The representative has left the guild. discord.py 2.3 does not
            # accept discord.Object in set_permissions(), so remove their
            # stale per-member overwrite by its actual HTTP endpoint.
            try:
                await channel._state.http.delete_channel_permissions(
                    channel.id, discord_user_id, reason="Revogação de representante ausente"
                )
            except discord.NotFound:
                pass
            return
    overwrite = channel.overwrites_for(member)
    if overwrite.send_messages is True or overwrite.send_messages_in_threads is True:
        overwrite.send_messages = False
        overwrite.send_messages_in_threads = False
        await channel.set_permissions(member, overwrite=overwrite, reason="Revogação de representante")


async def _ensure_exclusive(
    guild: discord.Guild, category: discord.CategoryChannel, partner_id: int,
    name: str, presentation: str, representative_id: int | None,
    previous_representative_id: int | None, enabled: bool,
    channel_id: int | None, existing_id: int | None,
    adopt_existing: bool, previously_managed: bool, intro_id: int | None,
) -> tuple[int | None, int | None, bool]:
    from core.partner_discord_markdown_sync import _own
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
    recovered_created = False
    if chosen is None:
        # A previous Discord call may have created the channel just before the
        # API timed out. Recover by stable topic marker before ever creating.
        candidates = [c for c in guild.text_channels if _require_channel_marker(c, partner_id)]
        if len(candidates) > 1:
            raise PartnerDiscordError("Mais de um canal com marcador deste parceiro; intervenção manual necessária")
        if candidates:
            chosen = candidates[0].id
            recovered_created = "created-by-coddy" in (candidates[0].topic or "")
    created = False
    if chosen is None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        channel = await guild.create_text_channel(
            _slug(name), category=category, overwrites=overwrites,
            topic=f"{_marker(partner_id)} | created-by-coddy", reason="Canal exclusivo de parceiro",
        )
        created = True
    else:
        candidate = await _get_channel(guild, chosen)
        if not isinstance(candidate, discord.TextChannel):
            raise PartnerDiscordError("O canal exclusivo selecionado não é de texto")
        already_managed = _require_channel_marker(candidate, partner_id)
        if not already_managed and not adopt_existing:
            raise PartnerDiscordError("Confirme expressamente a adoção do canal existente")
        if not already_managed:
            topic = (candidate.topic or "").strip()
            new_topic = f"{topic}\n{_marker(partner_id)}".strip()
            if len(new_topic) > 1024:
                raise PartnerDiscordError("Tópico do canal muito longo para anexar a identificação")
            await candidate.edit(topic=new_topic, reason="Adoção explícita de canal de parceiro")
        channel = candidate
        created = recovered_created or (previously_managed and channel_id == channel.id)
    # Grant the new representative first; if Discord rejects it, keep the old
    # member's access until a later retry rather than locking everyone out.
    await _grant_channel(channel, member)
    message = None
    if intro_id is not None and channel.id == channel_id:
        try:
            message = await channel.fetch_message(intro_id)
        except discord.NotFound:
            pass
        from core.partner_discord_markdown_sync import _own
        if message is not None and not _own(message, partner_id, channel.guild.me.id):
            raise PartnerDiscordError("Mensagem inicial de canal não pertence ao parceiro")
    if message is None:
        async for previous_message in channel.history(limit=100):
            if _matches_embed(previous_message, partner_id, channel.guild.me.id) or (
                getattr(previous_message.author, "id", None) == channel.guild.me.id
                and previous_message.content == presentation
                and _own(previous_message, partner_id, channel.guild.me.id)
            ):
                message = previous_message
                break
    if message is None:
        message = await channel.send(
            content=presentation, allowed_mentions=discord.AllowedMentions.none(),
            suppress_embeds=True,
        )
    elif message.content != presentation or any(
        (getattr(getattr(embed, "footer", None), "text", "") or "").startswith("BraFurries partner:")
        for embed in message.embeds
    ):
        await message.edit(
            content=presentation, embed=None, allowed_mentions=discord.AllowedMentions.none()
        )
    if previous_representative_id != representative_id:
        await _revoke_member(channel, previous_representative_id)
    if previous is not None and previous.id != channel.id:
        await _revoke_member(previous, previous_representative_id)
        if previously_managed:
            overwrite = previous.overwrites_for(guild.default_role)
            overwrite.view_channel = False
            await previous.set_permissions(guild.default_role, overwrite=overwrite,
                                           reason="Canal exclusivo transferido")
    return channel.id, message.id, created


async def sync_partner_discord(guild: discord.Guild, request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise PartnerDiscordError("Requisição inválida")
    partner_id = _id(request.get("partnerId"), "partnerId")
    # Load only after the request has passed the internal status token checks.
    from core.partner_discord_markdown import decode_part_ids, render_announcement
    from core.partner_discord_markdown_sync import sync_markdown_thread
    parts = render_announcement(request)
    announcement_ids = decode_part_ids(request.get("announcementPartMessageIds", []))
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
    # Category/forum creation is shared across all partners in one guild.
    # Serializing only by partner_id would permit two simultaneous requests
    # to create duplicate shared directory resources.
    key = (guild.id, 0)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    async with lock:
        if active:
            category, created_category = await _category(guild, category_id)
            parent, created_parent = await _thread_parent(guild, category, parent_id)
            thread_id, intro_id, announcement_ids = await sync_markdown_thread(
                parent, partner_id, request["name"].strip(), parts,
                thread_id, intro_id, announcement_ids,
                image_url=request.get("imageUrl"),
            )
        else:
            created_category = created_parent = False
            if thread_id is not None:
                channel = await _get_channel(guild, thread_id)
                if not isinstance(channel, discord.Thread):
                    raise PartnerDiscordError("Thread inválida")
                await _verify_starter_owner(channel, partner_id)
                await channel.edit(locked=True, archived=True, reason="Parceria inativa")
        if active or channel_id is not None:
            text_id, text_intro, text_managed = await _ensure_exclusive(
                guild, category if active else None, partner_id, request["name"].strip(),
                parts[0], representative_id, previous_representative_id,
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
            "announcementPartMessageIds": [str(item) for item in announcement_ids],
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
    intro_id = _id(request.get("threadIntroMessageId"), "threadIntroMessageId", optional=True)
    if intro_id is None:
        raise PartnerDiscordError("Publicação sem mensagem inicial registrada na API")
    # API verifies active partner ownership and supplies the stored thread and
    # intro IDs. Require authorship by Coddy; legacy embeds still need their
    # original partner marker, while migrated Markdown messages have no ID.
    from core.partner_discord_markdown_sync import _owned_message
    try:
        await _owned_message(thread, intro_id, partner_id)
    except discord.NotFound as exc:
        raise PartnerDiscordError("Mensagem inicial da publicação não encontrada") from exc
    if thread.archived:
        await thread.edit(archived=False, reason="Nova publicação do parceiro")
    message = await thread.send(content.strip(), allowed_mentions=discord.AllowedMentions.none())
    return {"messageId": str(message.id), "threadId": str(thread.id)}
