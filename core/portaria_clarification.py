"""Private, Discord-only clarification conversations for pending Portaria forms.

Website/API can enable the feature and choose a Discord category, never open
tickets or decide submitted forms. The Coddy is the sole operator of the
actual Discord-channel effects. State is durable and tenant-scoped in MariaDB.
"""
from __future__ import annotations

import logging
import discord

from core.discord_events import getStaffRoles
from core.database import get_form_submission
from core.portaria_clarification_store import (
    reserve_ticket, ticket_settings, get_ticket, list_open_tickets,
    list_recoverable_tickets, list_stale_closing_tickets,
    claim_open, remember_channel, mark_open, mark_open_failed,
    claim_close, mark_closed, restore_open, reset_missing_open_channel,
)

log = logging.getLogger(__name__)


def _is_staff(interaction: discord.Interaction) -> bool:
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    return bool(member and interaction.guild and
                any(role in member.roles for role in getStaffRoles(interaction.guild)))


def _ticket_topic(ticket_id: int, submission_id: int) -> str:
    return f"brafurries-portaria-clarification:{ticket_id};submission:{submission_id}"


async def _existing_channel(
    guild: discord.Guild, ticket_id: int, submission_id: int, saved_channel_id: int | None
) -> discord.TextChannel | None:
    topic = _ticket_topic(ticket_id, submission_id)
    if saved_channel_id:
        found = guild.get_channel(int(saved_channel_id))
        if found is not None:
            if (not isinstance(found, discord.TextChannel)
                    or found.guild.id != guild.id or found.topic != topic):
                raise ValueError("O canal salvo não corresponde ao atendimento.")
            return found
        try:
            fetched = await guild.fetch_channel(int(saved_channel_id))
            if (not isinstance(fetched, discord.TextChannel)
                    or fetched.guild.id != guild.id or fetched.topic != topic):
                raise ValueError("O canal salvo não corresponde ao atendimento.")
            return fetched
        except discord.NotFound:
            pass
        # A failed Discord request must NOT be confused with a deleted
        # channel: callers may reset state only after a complete 200 response
        # from fetch_channels with the expected topic absent.
    # Recovery after Discord created a channel but its HTTP response was lost.
    # The gateway cache can be stale; consult Discord before ever creating
    # another channel for the same durable ticket ID.
    cached = next((channel for channel in guild.text_channels
                   if channel.topic == topic), None)
    if cached is not None:
        return cached
    channels = await guild.fetch_channels()
    return next((channel for channel in channels
                 if isinstance(channel, discord.TextChannel) and channel.topic == topic),
                None)


def _assert_channel_is_private(guild: discord.Guild, channel: discord.TextChannel) -> None:
    """Never ping an applicant in a channel that lost its @everyone deny."""
    overwrite = channel.overwrites_for(guild.default_role)
    if overwrite.view_channel is not False:
        raise ValueError("O canal de esclarecimento não está privado para @everyone.")


async def _ensure_ticket_introduction(
    channel: discord.TextChannel, *, bot_id: int, ticket_id: int,
    submission_id: int, applicant: discord.Member,
    view: discord.ui.View,
) -> None:
    """Avoid mentioning the applicant twice after a Discord/SQL timeout."""
    marker = f"atendimento **#{ticket_id}**"
    async for message in channel.history(limit=100):
        if (message.author.id == bot_id and marker in (message.content or "")):
            return

    await channel.send(
        f"{applicant.mention}, a equipe da Portaria deseja conversar com você. "
        f"Este é o atendimento **#{ticket_id}**, vinculado à ficha "
        f"**#{submission_id}**. Responda neste canal privado; "
        "a ficha original continua pendente.",
        view=view,
        allowed_mentions=discord.AllowedMentions(
            everyone=False, roles=False, users=[applicant]),
    )


class PortariaClarificationReasonModal(
    discord.ui.Modal, title="Solicitar esclarecimentos"
):
    reason = discord.ui.TextInput(
        label="Por que precisa conversar com o candidato?",
        style=discord.TextStyle.paragraph,
        min_length=10,
        max_length=1024,
        required=True,
    )

    def __init__(self, submission_id: int) -> None:
        super().__init__()
        self.submission_id = submission_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_staff(interaction) or interaction.guild is None:
            await interaction.response.send_message(
                "Apenas a Staff autorizada pode solicitar esclarecimentos.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        try:
            settings = ticket_settings(guild.id)
        except Exception:
            log.exception("Failed to read Portaria ticket feature config guild=%s", guild.id)
            await interaction.followup.send(
                "A configuração da Portaria está temporariamente indisponível. "
                "Nenhum ticket foi aberto; tente novamente mais tarde.",
                ephemeral=True)
            return
        if not settings.get("clarification_tickets_enabled"):
            await interaction.followup.send(
                "Os tickets da Portaria estão desabilitados na configuração do site.",
                ephemeral=True)
            return
        try:
            ticket = reserve_ticket(
                self.submission_id, guild.id, interaction.user.id, str(self.reason))
        except ValueError as ex:
            await interaction.followup.send(str(ex), ephemeral=True)
            return
        except Exception:
            log.exception("Portaria clarification reserve failed guild=%s submission=%s",
                          guild.id, self.submission_id)
            await interaction.followup.send(
                "Não foi possível registrar o atendimento. Tente novamente.", ephemeral=True)
            return

        ticket_id = int(ticket["id"])
        if ticket["status"] == "CLOSED":
            await interaction.followup.send(
                "Esta ficha já teve um atendimento encerrado. Utilize a decisão normal da Portaria.",
                ephemeral=True)
            return
        if ticket["status"] == "OPEN":
            try:
                channel = await _existing_channel(
                    guild, ticket_id, self.submission_id, ticket.get("discord_channel_id"))
            except (discord.Forbidden, discord.HTTPException, ValueError):
                await interaction.followup.send(
                    "Não foi possível validar o canal existente no Discord. "
                    "Nenhum novo canal será criado até conferir o atendimento.",
                    ephemeral=True)
                return
            if channel is None and ticket.get("discord_channel_id"):
                reset_missing_open_channel(
                    ticket_id, guild.id, int(ticket["discord_channel_id"]))
            await interaction.followup.send(
                f"Atendimento já aberto: {channel.mention}" if channel else
                "O canal anterior não foi encontrado. O mesmo ticket poderá "
                "ser recuperado solicitando esclarecimentos novamente.",
                ephemeral=True)
            return
        if not claim_open(ticket_id, guild.id):
            await interaction.followup.send(
                "Um atendimento já está sendo criado. Verifique novamente em instantes.",
                ephemeral=True)
            return

        try:
            category_id = settings.get("clarification_ticket_category_id")
            category = guild.get_channel(int(category_id)) if category_id else None
            if category is None and category_id:
                category = await guild.fetch_channel(int(category_id))
            if not isinstance(category, discord.CategoryChannel):
                raise ValueError("Categoria de atendimento não existe neste servidor.")
            staff_roles = getStaffRoles(guild)
            if not staff_roles:
                raise ValueError("É preciso configurar ao menos um cargo autorizado da Staff.")
            applicant = guild.get_member(int(ticket["applicant_discord_user_id"]))
            if applicant is None:
                applicant = await guild.fetch_member(int(ticket["applicant_discord_user_id"]))
            bot_member = guild.me or (guild.get_member(interaction.client.user.id)
                                      if interaction.client.user else None)
            if bot_member is None or not category.permissions_for(bot_member).manage_channels:
                raise ValueError("O Coddy não pode criar canais na categoria configurada.")

            channel = await _existing_channel(
                guild, ticket_id, self.submission_id, ticket.get("discord_channel_id"))
            if channel is None:
                allowed = discord.PermissionOverwrite(
                    view_channel=True, read_message_history=True, send_messages=True)
                overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                              applicant: allowed, bot_member: allowed}
                for role in staff_roles:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True, read_message_history=True, send_messages=True)
                channel = await guild.create_text_channel(
                    name=f"esclarecimento-{ticket_id}",
                    category=category,
                    overwrites=overwrites,
                    topic=_ticket_topic(ticket_id, self.submission_id),
                    reason=f"Portaria: atendimento excepcional #{ticket_id}",
                )
            _assert_channel_is_private(guild, channel)
            # If posting the message fails, keep this Discord channel ID
            # linked to the ticket. The original form must remain undecidable
            # until the same channel can be recovered and the ticket closed.
            if not remember_channel(ticket_id, guild.id, channel.id):
                raise RuntimeError("Falha ao registrar o canal privado do atendimento.")
            view = PortariaClarificationCloseView(ticket_id)
            # Tag the candidate inside the private channel, not only the
            # staff-facing original form. On retry, reuse the original bot
            # introduction instead of sending another mention.
            await _ensure_ticket_introduction(
                channel, bot_id=bot_member.id, ticket_id=ticket_id,
                submission_id=self.submission_id, applicant=applicant, view=view,
            )
            if not mark_open(ticket_id, guild.id, channel.id):
                raise RuntimeError("Falha de transição ao registrar o canal do atendimento.")
            interaction.client.add_view(view)
            # Also annotate the original staff-facing ficha with a mention,
            # as requested. Never include answers or the private conversation.
            # Notification failure must not roll back an opened ticket.
            try:
                form = get_form_submission(self.submission_id)
                if form and int(form.get("guild_id") or 0) == guild.id:
                    form_channel = guild.get_channel(int(form["channel_id"]))
                    if form_channel is None:
                        form_channel = await guild.fetch_channel(int(form["channel_id"]))
                    if isinstance(form_channel, discord.TextChannel) and form.get("message_id"):
                        original = await form_channel.fetch_message(int(form["message_id"]))
                        await original.reply(
                            f"Esclarecimento **#{ticket_id}** solicitado para "
                            f"{applicant.mention}. Atendimento em canal privado.",
                            allowed_mentions=discord.AllowedMentions(
                                everyone=False, roles=False, users=[applicant]),
                            mention_author=False,
                        )
            except Exception:
                log.exception("Could not annotate original Portaria form guild=%s ticket=%s",
                              guild.id, ticket_id)
            await interaction.followup.send(
                f"Atendimento privado criado: {channel.mention}", ephemeral=True)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException, ValueError, RuntimeError):
            log.exception("Portaria clarification creation failed guild=%s ticket=%s",
                          guild.id, ticket_id)
            mark_open_failed(ticket_id, guild.id, "DISCORD_CREATE_OR_STATE_ERROR")
            await interaction.followup.send(
                "Falha ao criar o canal privado. Confira categoria, permissões e presença do candidato.",
                ephemeral=True)


class PortariaClarificationCloseView(discord.ui.View):
    def __init__(self, ticket_id: int) -> None:
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
        button = discord.ui.Button(
            label="Encerrar esclarecimento",
            style=discord.ButtonStyle.secondary,
            custom_id=f"portaria_clarification_close:{ticket_id}",
        )
        button.callback = self._close
        self.add_item(button)

    async def _close(self, interaction: discord.Interaction) -> None:
        if not _is_staff(interaction) or interaction.guild is None:
            await interaction.response.send_message(
                "Apenas a Staff autorizada pode encerrar este atendimento.",
                ephemeral=True)
            return
        ticket = get_ticket(self.ticket_id, interaction.guild.id)
        if not ticket or ticket.get("status") not in ("OPEN", "CLOSING"):
            await interaction.response.send_message(
                "Atendimento indisponível ou já encerrado.", ephemeral=True)
            return
        if not interaction.channel or interaction.channel.id != ticket.get("discord_channel_id"):
            await interaction.response.send_message(
                "Este botão não pertence ao canal do atendimento.", ephemeral=True)
            return
        await interaction.response.send_modal(PortariaClarificationCloseModal(self.ticket_id))


class PortariaClarificationCloseModal(discord.ui.Modal, title="Encerrar esclarecimento"):
    reason = discord.ui.TextInput(
        label="Conclusão do atendimento",
        style=discord.TextStyle.paragraph,
        min_length=10, max_length=1024, required=True,
    )

    def __init__(self, ticket_id: int):
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_staff(interaction) or interaction.guild is None:
            await interaction.response.send_message("Acesso negado.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        ticket = get_ticket(self.ticket_id, guild.id)
        if (not ticket or not interaction.channel or
                ticket.get("discord_channel_id") != interaction.channel.id):
            await interaction.followup.send("Ticket inválido para este servidor.", ephemeral=True)
            return
        if ticket["status"] == "CLOSED":
            await interaction.followup.send("Atendimento já encerrado.", ephemeral=True)
            return
        if not claim_close(self.ticket_id, guild.id, interaction.user.id,
                           str(self.reason)):
            await interaction.followup.send(
                "O atendimento já está sendo encerrado.", ephemeral=True)
            return
        edit_attempted = False
        try:
            channel = interaction.channel
            # Remove the applicant-specific overwrite in a single Discord
            # channel edit, even if the member already left the guild.
            # Keep everyone denied, Staff roles and Coddy permissions intact.
            active_overwrites = channel.overwrites
            sanitized = {
                target: overwrite for target, overwrite in active_overwrites.items()
                if getattr(target, "id", None) != int(ticket["applicant_discord_user_id"])
            }
            edit_attempted = True
            await channel.edit(
                name=f"esclarecimento-encerrado-{self.ticket_id}",
                overwrites=sanitized,
                reason=f"Encerramento de esclarecimento #{self.ticket_id}",
            )
            if not mark_closed(self.ticket_id, guild.id, interaction.user.id, str(self.reason)):
                raise RuntimeError("Não foi possível persistir o encerramento")
            # A failed informational message must not reverse a committed close.
            try:
                await channel.send(
                    f"Atendimento encerrado por {interaction.user.mention}. "
                    "A decisão sobre a ficha continua pendente.",
                    allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Closed clarification ticket could not post summary guild=%s ticket=%s",
                            guild.id, self.ticket_id)
            await interaction.followup.send(
                "Atendimento encerrado. A Staff pode decidir a ficha original.",
                ephemeral=True)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException, RuntimeError):
            log.exception("Portaria clarification closure failed guild=%s ticket=%s",
                          guild.id, self.ticket_id)
            # Discord can apply the edit even if its HTTP response is lost.
            # Keep the durable CLOSING intent after an attempted Discord edit,
            # so recovery can validate the channel before closing the ticket.
            if not edit_attempted:
                restore_open(self.ticket_id, guild.id)
            await interaction.followup.send(
                "Não foi possível concluir o encerramento. A Staff deve "
                "conferir o canal antes de tentar novamente.",
                ephemeral=True)



async def reconcile_incomplete_clarification_tickets(bot: discord.Client) -> None:
    """Recover already-created channels; never create new ones without Staff action.

    The worker runs in a small bounded batch. It only repairs existing Discord
    rooms after a partial failure; a missing/unverifiable channel is left for
    a controlled retry rather than risking a duplicate or privacy leak.
    """
    try:
        candidates = list_recoverable_tickets(limit=8)
    except Exception:
        log.exception("Could not scan stranded Portaria clarification tickets")
        return

    for ticket in candidates:
        ticket_id = int(ticket["id"])
        guild_id = int(ticket["guild_id"])
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue
        try:
            channel = await _existing_channel(
                guild, ticket_id, int(ticket["submission_id"]),
                ticket.get("discord_channel_id"),
            )
            if channel is None:
                continue
            _assert_channel_is_private(guild, channel)
            # Recheck the original ticket state under DB CAS before touching
            # Discord. A concurrent Staff retry may already own the lease.
            if not claim_open(ticket_id, guild_id):
                continue
            applicant = guild.get_member(int(ticket["applicant_discord_user_id"]))
            if applicant is None:
                applicant = await guild.fetch_member(int(ticket["applicant_discord_user_id"]))
            view = PortariaClarificationCloseView(ticket_id)
            await _ensure_ticket_introduction(
                channel,
                bot_id=int(bot.user.id),
                ticket_id=ticket_id,
                submission_id=int(ticket["submission_id"]),
                applicant=applicant,
                view=view,
            )
            if not remember_channel(ticket_id, guild_id, channel.id):
                raise RuntimeError("Failed to persist recovered Portaria channel")
            if not mark_open(ticket_id, guild_id, channel.id):
                raise RuntimeError("Failed to finalize recovered Portaria ticket")
            bot.add_view(view)
            log.info("Recovered Portaria clarification ticket id=%s guild=%s",
                     ticket_id, guild_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException,
                ValueError, RuntimeError):
            log.exception("Portaria clarification recovery deferred guild=%s ticket=%s",
                          guild_id, ticket_id)
            # Leaves channel identity preserved; new attempts never duplicate it.
            mark_open_failed(ticket_id, guild_id, "DISCORD_RECOVERY_ERROR")




async def reconcile_stale_closing_clarification_tickets(bot: discord.Client) -> None:
    """Complete interrupted closes using the persisted actor/reason.

    Never mark CLOSED unless the exact guild, channel and ticket topic match
    and the applicant's direct Discord overwrite has been removed. If Discord
    cannot confirm that state, leave CLOSING to block Portaria decisions.
    """
    try:
        tickets = list_stale_closing_tickets(limit=8)
    except Exception:
        log.exception("Could not scan stale Portaria ticket closures")
        return

    for ticket in tickets:
        ticket_id = int(ticket["id"])
        guild_id = int(ticket["guild_id"])
        try:
            guild = bot.get_guild(guild_id)
            if guild is None:
                continue
            channel_id = int(ticket["discord_channel_id"])
            channel = guild.get_channel(channel_id)
            if channel is None:
                channel = await guild.fetch_channel(channel_id)
            if (not isinstance(channel, discord.TextChannel)
                    or channel.id != channel_id or channel.guild.id != guild_id
                    or channel.topic != _ticket_topic(ticket_id, int(ticket["submission_id"]))):
                log.warning("Cannot recover Portaria closing: channel mismatch guild=%s ticket=%s",
                            guild_id, ticket_id)
                continue
            _assert_channel_is_private(guild, channel)
            if not claim_close(ticket_id, guild_id,
                               int(ticket["closed_by_discord_user_id"]),
                               str(ticket["close_reason"])):
                continue
            applicant_id = int(ticket["applicant_discord_user_id"])
            overwrites = channel.overwrites
            sanitized = {target: ow for target, ow in overwrites.items()
                         if getattr(target, "id", None) != applicant_id}
            if len(sanitized) != len(overwrites):
                await channel.edit(
                    name=f"esclarecimento-encerrado-{ticket_id}",
                    overwrites=sanitized,
                    reason=f"Recuperação de encerramento #{ticket_id}",
                )
            if not mark_closed(ticket_id, guild_id,
                               int(ticket["closed_by_discord_user_id"]),
                               str(ticket["close_reason"])):
                raise RuntimeError("Could not commit recovered Portaria closure")
            log.info("Recovered Portaria clarification closure guild=%s ticket=%s",
                     guild_id, ticket_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException,
                ValueError, RuntimeError):
            # Do not release the form decision lock while Discord is uncertain.
            log.exception("Portaria closing recovery deferred guild=%s ticket=%s",
                          guild_id, ticket_id)


def register_open_clarification_ticket_views(bot: discord.Client) -> None:
    """Restore close button callbacks for surviving channels across restarts."""
    for ticket in list_open_tickets():
        try:
            bot.add_view(PortariaClarificationCloseView(int(ticket["id"])))
        except Exception:
            log.exception("Failed to restore clarification ticket view id=%s",
                          ticket["id"])
