"""Private, Discord-only clarification conversations for pending Portaria forms.

Website/API can enable the feature and choose a Discord category, never open
tickets or decide submitted forms. The Coddy is the sole operator of the
actual Discord-channel effects. State is durable and tenant-scoped in MariaDB.
"""
from __future__ import annotations

import logging
import discord

from core.discord_events import getStaffRoles
from core.portaria_clarification_store import (
    reserve_ticket, ticket_settings, get_ticket, list_open_tickets,
    claim_open, mark_open, mark_open_failed, claim_close, mark_closed, restore_open,
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
    if saved_channel_id:
        found = guild.get_channel(int(saved_channel_id))
        if isinstance(found, discord.TextChannel):
            return found
        try:
            fetched = await guild.fetch_channel(int(saved_channel_id))
            if isinstance(fetched, discord.TextChannel):
                return fetched
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    topic = _ticket_topic(ticket_id, submission_id)
    # Recovery after Discord has created the channel but SQL commit failed.
    return next((channel for channel in guild.text_channels
                 if channel.topic == topic), None)


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
        settings = ticket_settings(guild.id)
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
            channel = await _existing_channel(
                guild, ticket_id, self.submission_id, ticket.get("discord_channel_id"))
            await interaction.followup.send(
                f"Atendimento já aberto: {channel.mention}" if channel else
                "O ticket já consta como aberto, mas o canal não foi localizado. Avise a coordenação.",
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
            if not mark_open(ticket_id, guild.id, channel.id):
                raise RuntimeError("Falha de transição ao registrar o canal do atendimento.")
            view = PortariaClarificationCloseView(ticket_id)
            await channel.send(
                f"Atendimento excepcional da ficha **#{self.submission_id}**. "
                "A Staff poderá esclarecer dúvidas antes de analisar a ficha original. "
                "Este canal não altera a aprovação ou reprovação.",
                view=view, allowed_mentions=discord.AllowedMentions.none())
            interaction.client.add_view(view)
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
        if not claim_close(self.ticket_id, guild.id):
            await interaction.followup.send(
                "O atendimento já está sendo encerrado.", ephemeral=True)
            return
        try:
            channel = interaction.channel
            applicant = guild.get_member(int(ticket["applicant_discord_user_id"]))
            if applicant is None:
                applicant = await guild.fetch_member(int(ticket["applicant_discord_user_id"]))
            # Revoke access without deleting records or changing form decisions.
            await channel.set_permissions(
                applicant, overwrite=discord.PermissionOverwrite(view_channel=False),
                reason=f"Encerramento de esclarecimento #{self.ticket_id}")
            await channel.edit(name=f"esclarecimento-encerrado-{self.ticket_id}",
                               reason=f"Encerramento de esclarecimento #{self.ticket_id}")
            if not mark_closed(self.ticket_id, guild.id, interaction.user.id, str(self.reason)):
                raise RuntimeError("Não foi possível persistir o encerramento")
            await channel.send(
                f"Atendimento encerrado por {interaction.user.mention}. "
                "A decisão sobre a ficha continua pendente.",
                allowed_mentions=discord.AllowedMentions.none())
            await interaction.followup.send(
                "Atendimento encerrado. A Staff pode decidir a ficha original.",
                ephemeral=True)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException, RuntimeError):
            log.exception("Portaria clarification closure failed guild=%s ticket=%s",
                          guild.id, self.ticket_id)
            restore_open(self.ticket_id, guild.id)
            await interaction.followup.send(
                "Falha no encerramento. Confira as permissões e tente novamente.",
                ephemeral=True)


def register_open_clarification_ticket_views(bot: discord.Client) -> None:
    """Restore close button callbacks for surviving channels across restarts."""
    for ticket in list_open_tickets():
        try:
            bot.add_view(PortariaClarificationCloseView(int(ticket["id"])))
        except Exception:
            log.exception("Failed to restore clarification ticket view id=%s",
                          ticket["id"])
