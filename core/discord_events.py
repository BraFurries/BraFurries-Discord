import logging
import math
import discord
from datetime import datetime, timedelta, timezone
from core.time_functions import now, timezone_offset

from core.database import getLogConfig, getStaffRoles as getStaffRoleIds

def checkRolesUpdate(before:discord.member.Member, after:discord.member.Member):
    if before.roles != after.roles:
        todosCargos = before.roles + after.roles
        #vamos remover os cargos repetidos e @everyone
        todosCargos = list(dict.fromkeys(todosCargos))
        todosCargos.remove(after.guild.default_role)
        cargosAlterados = {'adicionados': [], 'removidos': []}
        #agora vamos percorrer a lista de cargos e ver quais foram adicionados e quais foram removidos
        for cargo in todosCargos:
            if not before.roles.__contains__(cargo):
                cargosAlterados['adicionados'].append(cargo)
            if not after.roles.__contains__(cargo):
                cargosAlterados['removidos'].append(cargo)
        ##print(f'Os cargos alterados foram: {cargosAlterados}')
        return cargosAlterados
    else:
        return False


async def logProfileChange(bot: discord.Client, guild: discord.Guild, user: discord.abc.User, changes: dict):
    """Send a profile update log message if logging is enabled."""
    config = getLogConfig(guild.id, "profile")
    if not config or not config.get("enabled") or not config.get("log_channel"):
        return

    channel = await guild.fetch_channel(int(config["log_channel"]))
    if channel is None:
        return

    embed = discord.Embed(
        title="Alteração de perfil",
        color=discord.Color.blurple(),
        timestamp=now()
    )
    embed.set_author(name=str(user), icon_url=user.display_avatar.url)
    embed.set_footer(text=f"ID: {user.id}")

    for field, (before_value, after_value) in changes.items():
        embed.add_field(
            name=field,
            value=f"{before_value or 'N/A'} -> {after_value or 'N/A'}",
            inline=False,
        )

    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        await channel.send(embed=embed)


async def logWarn(
    guild: discord.Guild,
    member: discord.abc.User,
    moderator: discord.abc.User,
    reason: str,
    warnings_count: int,
):
    """Send a warn log message if logging is enabled for this guild."""
    config = getLogConfig(guild.id, "warn")
    if not config or not config.get("enabled") or not config.get("log_channel"):
        return

    channel = await guild.fetch_channel(int(config["log_channel"]))
    if channel is None:
        return

    embed = discord.Embed(
        title="Warn aplicado",
        color=discord.Color.orange(),
        timestamp=now(),
    )
    embed.add_field(name="Membro", value=f"{member.mention}", inline=True)
    embed.add_field(name="Total de warns", value=str(warnings_count), inline=True)
    embed.add_field(name="Moderador", value=moderator.mention, inline=False)
    embed.add_field(name="Motivo", value=reason or "N/A", inline=False)
    embed.set_footer(text=f"ID: {member.id}")

    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        await channel.send(embed=embed)


async def logBan(
    guild: discord.Guild,
    user: discord.abc.User,
    moderator: discord.abc.User,
    *,
    reason: str,
    valid_until: datetime | None,
    can_appeal: bool,
    propagated_effects: dict[str, int] | None = None,
):
    """Send a ban log message if logging is enabled for this guild."""
    config = getLogConfig(guild.id, "ban")
    if not config or not config.get("enabled") or not config.get("log_channel"):
        return

    channel = await guild.fetch_channel(int(config["log_channel"]))
    if channel is None:
        return

    embed = discord.Embed(
        title="Ban aplicado",
        color=discord.Color.red(),
        timestamp=now(),
    )
    embed.add_field(name="Membro", value=user.mention, inline=False)
    embed.add_field(name="Moderador", value=moderator.mention, inline=False)
    embed.add_field(name="Motivo", value=reason or "N/A", inline=False)
    embed.add_field(
        name="Válido até",
        value=valid_until.strftime("%d/%m/%Y %H:%M:%S") if valid_until else "Permanente",
        inline=True,
    )
    embed.add_field(
        name="Pode recorrer",
        value="Sim" if can_appeal else "Não",
        inline=True,
    )
    if propagated_effects:
        embed.add_field(
            name="Efeitos Discord",
            value=(
                f"Aplicados: {propagated_effects.get('APPLIED', 0)} | "
                f"Já banidos: {propagated_effects.get('ALREADY_BANNED', 0)} | "
                "Falhas: "
                f"{sum(propagated_effects.get(key, 0) for key in ('NOT_FOUND', 'FORBIDDEN', 'FAILED'))}"
            ),
            inline=False,
        )
    embed.set_footer(text=f"ID: {user.id}")

    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        await channel.send(embed=embed)


async def logIdentityBanPropagation(
    guild: discord.Guild,
    ban_id: int,
    *,
    reason: str,
    effects,
) -> None:
    """Log automatic enforcement caused by a newly CONFIRMED identity link."""
    config = getLogConfig(guild.id, "ban")
    if not config or not config.get("enabled") or not config.get("log_channel"):
        return

    try:
        channel = await guild.fetch_channel(int(config["log_channel"]))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        logging.exception(
            "Não foi possível carregar o canal de log para propagação automática do ban %s",
            ban_id,
        )
        return
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return

    counts: dict[str, int] = {}
    for effect in effects:
        counts[effect.outcome] = counts.get(effect.outcome, 0) + 1

    processed_ids = ", ".join(
        f"<@{effect.discord_user_id}>"
        for effect in effects
    ) or "Nenhuma conta nova"

    embed = discord.Embed(
        title="Ban propagado automaticamente",
        color=discord.Color.red(),
        timestamp=now(),
    )
    embed.add_field(
        name="Gatilho",
        value="Novo vínculo de identidade CONFIRMED",
        inline=False,
    )
    embed.add_field(name="Ação administrativa", value=f"Ban #{ban_id}", inline=True)
    embed.add_field(name="Motivo original", value=reason or "N/A", inline=False)
    embed.add_field(
        name="Efeitos Discord",
        value=(
            f"Aplicados: {counts.get('APPLIED', 0)} | "
            f"Já banidos: {counts.get('ALREADY_BANNED', 0)} | "
            "Falhas: "
            f"{sum(counts.get(key, 0) for key in ('NOT_FOUND', 'FORBIDDEN', 'FAILED'))}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Contas alcançadas",
        value=processed_ids[:1024],
        inline=False,
    )
    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        logging.exception(
            "Não foi possível enviar o log de propagação automática do ban %s",
            ban_id,
        )


async def _get_mute_audit_entry(
    guild: discord.Guild | None,
    member_id: int,
    mute_until: datetime | None,
) -> discord.AuditLogEntry | None:
    """Return the most relevant audit log entry for a mute action."""

    if guild is None or mute_until is None:
        return None

    try:
        async for entry in guild.audit_logs(
            limit=5, action=discord.AuditLogAction.member_update
        ):
            target = getattr(entry, "target", None)
            if not target or target.id != member_id:
                continue

            changes = getattr(entry, "changes", None)
            if not changes:
                return entry

            after_state = getattr(entry, "after", None)
            if after_state is not None:
                new_value = getattr(
                    after_state,
                    "communication_disabled_until",
                    getattr(after_state, "timed_out_until", None),
                )
                if new_value == mute_until:
                    return entry

            changes_after = getattr(changes, "after", None)
            if changes_after is not None:
                new_value = getattr(
                    changes_after,
                    "communication_disabled_until",
                    getattr(changes_after, "timed_out_until", None),
                )
                if new_value == mute_until:
                    return entry
    except (discord.Forbidden, discord.HTTPException):
        return None

    return None


async def logMute(
    guild: discord.Guild,
    member: discord.abc.User,
    mute_until: datetime | None,
    moment: datetime | None = None,
):
    """Send a mute log message if logging is enabled for this guild."""
    config = getLogConfig(guild.id, "mute")
    if not config or not config.get("enabled") or not config.get("log_channel"):
        return

    channel = await guild.fetch_channel(int(config["log_channel"]))
    if channel is None:
        return

    audit_entry = await _get_mute_audit_entry(guild, member.id, mute_until)
    moderator = getattr(audit_entry, "user", None)
    reason = (getattr(audit_entry, "reason", None) or "").strip()
    if not reason:
        reason = "Não especificado"
    moderator_display = (
        moderator.mention if isinstance(moderator, discord.abc.User) else "Desconhecido"
    )
    mute_period = _format_mute_period(mute_until, moment=moment)

    embed = discord.Embed(
        title="Castigo aplicado (mute)",
        color=discord.Color.dark_gold(),
        timestamp=moment or now(),
    )
    embed.add_field(name="Membro", value=member.mention, inline=True)
    embed.add_field(name="Motivo", value=reason, inline=False)
    embed.add_field(name="Período", value=mute_period, inline=False)
    embed.add_field(name="Aplicado por", value=moderator_display, inline=True)
    embed.set_footer(text=f"ID: {member.id}")

    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        await channel.send(embed=embed)


def _format_mute_period(mute_until: datetime | None, moment: datetime | None = None) -> str:
    """Format the remaining mute period for logging."""

    if mute_until is None:
        return "Não especificado"

    if mute_until.tzinfo is not None and mute_until.tzinfo.utcoffset(mute_until) is not None:
        mute_until = mute_until.astimezone(
            timezone(timedelta(hours=timezone_offset))
        ).replace(tzinfo=None)

    remaining = mute_until - (moment or now())
    total_seconds_raw = remaining.total_seconds()
    if total_seconds_raw <= 0:
        return "Expirado"
    total_seconds = int(total_seconds_raw)
    if remaining.microseconds > 0:
        total_seconds += 1

    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds and not parts:
        parts.append(f"{seconds}s")

    return " ".join(parts)

def _format_message_content(content: str) -> str:
    """Prepare message content for logging embeds."""

    if not content or not content.strip():
        return "*Sem conteúdo*"
    if len(content) > 1024:
        return content[:1021] + "..."
    return content


async def _get_previous_message(
    channel: discord.abc.Messageable,
    before_message: discord.Message | None = None,
    before_id: int | None = None,
) -> discord.Message | None:
    """Return the message sent immediately before the provided reference."""

    history_kwargs: dict[str, object] = {"limit": 1}
    if before_message is not None:
        history_kwargs["before"] = before_message
    elif before_id is not None:
        history_kwargs["before"] = discord.Object(id=before_id)

    try:
        async for previous in channel.history(**history_kwargs):
            return previous
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return None

    return None


def _format_message_body(message: discord.Message | None) -> str:
    """Render a message's content and attachments for embeds."""

    if message is None:
        return "*Conteúdo indisponível (mensagem antiga ou não armazenada)*"

    content = message.content or ""
    attachment_links = [attachment.url for attachment in getattr(message, "attachments", [])]

    if attachment_links:
        attachments_text = "\n".join(attachment_links)
        content = f"{content}\n{attachments_text}" if content else attachments_text

    return _format_message_content(content)


def _format_attachments(attachments: list[discord.Attachment] | None) -> str:
    """Return a human-friendly list of attachment links."""

    if not attachments:
        return ""

    links = [str(attachment.url) for attachment in attachments]
    visible_links: list[str] = []

    for link in links:
        candidate_links = [*visible_links, link]
        omitted_count = len(links) - len(candidate_links)
        omitted_text = (
            f"… e mais {omitted_count} "
            f"{'anexo não exibido' if omitted_count == 1 else 'anexos não exibidos'}"
            if omitted_count
            else ""
        )
        candidate = "\n".join(candidate_links + ([omitted_text] if omitted_text else []))
        if len(candidate) > 1024:
            break
        visible_links.append(link)

    omitted_count = len(links) - len(visible_links)
    if omitted_count:
        omitted_text = (
            f"… e mais {omitted_count} "
            f"{'anexo não exibido' if omitted_count == 1 else 'anexos não exibidos'}"
        )
        return "\n".join(visible_links + [omitted_text])

    return "\n".join(visible_links)


async def _get_delete_audit_entry(
    guild: discord.Guild | None, author_id: int | None, channel_id: int | None
) -> discord.AuditLogEntry | None:
    """Return the most relevant audit log entry for a deleted message."""

    if guild is None:
        return None

    try:
        async for entry in guild.audit_logs(
            limit=5, action=discord.AuditLogAction.message_delete
        ):
            if author_id is not None:
                if not getattr(entry, "target", None) or entry.target.id != author_id:
                    continue
            if getattr(entry.extra, "channel", None) and entry.extra.channel.id != channel_id:
                continue

            if datetime.utcnow() - entry.created_at.replace(tzinfo=None) > timedelta(minutes=5):
                continue

            return entry
    except (discord.Forbidden, discord.HTTPException):
        return None

    return None


async def logMessageEdit(before: discord.Message, after: discord.Message):
    """Send a log entry when a message is edited and logging is enabled."""

    if before.guild is None or before.author.bot:
        return

    # Avoid logging edits where nothing changed.
    #
    # In some scenarios, Discord may send the cached message as ``before``
    # already updated, causing ``before.content`` to match ``after.content``
    # even when the user actually edited the message again. Using only the
    # content comparison would then skip legitimate edits. To reliably detect
    # changes we also compare the edit timestamp and other mutable fields.
    if (
        before.edited_at == after.edited_at
        and before.content == after.content
        and before.attachments == after.attachments
        and before.embeds == after.embeds
    ):
        return

    config = getLogConfig(before.guild.id, "message_edit")
    if not config or not config.get("enabled") or not config.get("log_channel"):
        return

    channel = await before.guild.fetch_channel(int(config["log_channel"]))
    if channel is None:
        return

    embed = discord.Embed(
        title="Mensagem editada",
        color=discord.Color.gold(),
        timestamp=now(),
    )
    embed.set_author(name=str(before.author), icon_url=before.author.display_avatar.url)
    embed.add_field(name="Canal", value=before.channel.mention, inline=True)
    embed.add_field(name="Membro", value=before.author.mention, inline=True)
    embed.add_field(name="Mensagem", value=f"[Abrir mensagem]({after.jump_url})", inline=True)
    embed.add_field(name="Antes", value=_format_message_content(before.content), inline=False)
    if before.attachments:
        embed.add_field(
            name="Anexos (antes)",
            value=_format_attachments(before.attachments),
            inline=False,
        )
    embed.add_field(name="Depois", value=_format_message_content(after.content), inline=False)
    if after.attachments:
        embed.add_field(
            name="Anexos (depois)",
            value=_format_attachments(after.attachments),
            inline=False,
        )
    embed.set_footer(text=f"ID: {before.author.id}")

    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        await channel.send(embed=embed)


async def logMessageDelete(
    message: discord.Message | None = None,
    bot: discord.Client | None = None,
    payload: discord.RawMessageDeleteEvent | None = None,
):
    """Send a log entry when a message is deleted and logging is enabled."""

    channel = getattr(message, "channel", None)
    guild = getattr(message, "guild", None)
    author = getattr(message, "author", None)

    if channel is None and payload is not None and bot is not None:
        channel = bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(payload.channel_id)
            except (discord.Forbidden, discord.HTTPException):
                channel = None
        if payload.cached_message is not None:
            message = payload.cached_message
            author = payload.cached_message.author
            guild = payload.cached_message.guild
        elif channel is not None:
            guild = getattr(channel, "guild", None)

    if guild is None and payload is not None:
        if bot is not None:
            guild = bot.get_guild(payload.guild_id)
            if guild is None:
                try:
                    guild = await bot.fetch_guild(payload.guild_id)
                except (discord.Forbidden, discord.HTTPException):
                    guild = None
        elif channel is not None:
            guild = getattr(channel, "guild", None)

    if guild is None:
        return

    config = getLogConfig(guild.id, "message_delete")
    if not config or not config.get("enabled") or not config.get("log_channel"):
        return

    log_channel = await guild.fetch_channel(int(config["log_channel"]))
    if log_channel is None:
        return

    audit_entry = await _get_delete_audit_entry(
        guild,
        getattr(author, "id", None),
        getattr(channel, "id", None) or getattr(payload, "channel_id", None),
    )

    if author is None and audit_entry and getattr(audit_entry, "target", None):
        author = guild.get_member(audit_entry.target.id) if guild else None
        if author is None:
            author = audit_entry.target

    deleter = audit_entry.user if audit_entry else None

    if (
        message is None
        and payload is not None
        and bot is not None
        and getattr(payload, "message_id", None) is not None
    ):
        cached = next(
            (m for m in getattr(bot, "cached_messages", []) if m.id == payload.message_id),
            None,
        )
        if cached is not None:
            message = cached
            author = getattr(cached, "author", author)
            channel = getattr(cached, "channel", channel)
            guild = getattr(cached, "guild", guild)

    if author is None or getattr(author, "bot", False):
        return

    previous_message = None
    if channel is not None:
        previous_message = await _get_previous_message(
            channel, before_message=message, before_id=getattr(payload, "message_id", None)
        )

    if author is not None:
        member_display = getattr(author, "mention", str(author))
    else:
        member_display = "Desconhecido"
    channel_display = channel.mention if hasattr(channel, "mention") else f"ID: {getattr(channel, 'id', 'N/A')}"

    embed = discord.Embed(
        title="Mensagem deletada",
        color=discord.Color.red(),
        timestamp=now(),
    )
    if author is not None:
        embed.set_author(name=str(author), icon_url=author.display_avatar.url)
    embed.add_field(name="Canal", value=channel_display, inline=True)
    embed.add_field(name="Membro", value=member_display, inline=True)

    if previous_message is not None:
        previous_link = f"[Abrir mensagem]({previous_message.jump_url})"
        embed.add_field(name="Mensagem anterior", value=previous_link, inline=True)
    else:
        embed.add_field(
            name="Mensagem anterior",
            value=_format_message_body(previous_message),
            inline=True,
        )

    embed.add_field(
        name="Mensagem deletada",
        value=_format_message_body(message),
        inline=False,
    )
    if getattr(message, "attachments", None):
        embed.add_field(
            name="Anexos da mensagem",
            value=_format_attachments(message.attachments),
            inline=False,
        )

    if deleter and getattr(author, "id", None) is not None and deleter.id != author.id:
        embed.add_field(name="Deletada por", value=deleter.mention, inline=False)

    if author is not None:
        embed.set_footer(text=f"ID: {author.id}")

    if isinstance(log_channel, (discord.TextChannel, discord.Thread)):
        await log_channel.send(embed=embed)


def getStaffRoles(guild: discord.Guild) -> list[discord.Role]:
    """Return roles considered staff for moderation commands."""

    role_ids = getStaffRoleIds(guild.id)
    roles: list[discord.Role] = []
    for role_id in role_ids:
        role = guild.get_role(int(role_id))
        if role is not None:
            roles.append(role)
    return roles
