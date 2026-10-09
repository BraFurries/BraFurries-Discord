import asyncio
from core.routine_functions import generateUserDescription
from core.AI_Functions.terceiras.openAI import OPENAI_TOKEN_INVALID_MARKER, analisaTicketPortaria, resumirConversaHistorico
from discord import Interaction, Member, Role, app_commands
from discord.http import Route
from discord.ui import View, button
from discord.ext import commands, tasks
from datetime import timedelta, datetime, date, timezone
from dateutil.relativedelta import relativedelta
from core.time_functions import now
from core.database import (
    assignTempRole,
    getProfileData,
    getCommandVisibleChannels,
    getMemberNotes,
    getMemberNotesCount,
    addMemberNote,
    getExpiredBans,
    removeBanRecord,
    finalizeExpiredBan,
    markBanDiscordEffectReverted,
    recordBanDiscordEffects,
)
from core.verifications import verifyDate
from core.database import *
from core.time_functions import MONTHS
from core.discord_events import logWarn, logBan, getStaffRoles
from core.notifications import notify_owner_and_user
from core.identity_api import IdentityApiError, get_identity_summary
from core.identity_bans import (
    compensate_unrecorded_propagated_bans,
    propagate_confirmed_ban,
    summarize_ban_effects,
    validate_confirmed_ban_origin,
)

from typing import Literal
import discord
import re
import os
import io
import logging

BIRTHDAY_REGEX = re.compile(
    r'(\d(?:\s?\d)?)'
    r'(?:\s?(?:d[eo]|\/|\\|\.|-)\s?|\s?)'
    r'(\d(?:\s?\d)?|(?:janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro))'
    r'(?:\s?(?:d[eo]|\/|\\|\.|-)\s?|\s?)'
    r'(\d(?:\s?\d){1,3})'
)


def should_include_identity_aggregate(actor_is_staff: bool, ephemeral: bool) -> bool:
    """Confirmed identity aggregates are staff-only moderation context."""
    return actor_is_staff and ephemeral


async def confirmed_warning_count_for_limit(
    discord_user_id: int,
    community_id: int,
) -> int:
    """Return the Community-scoped total across the current CONFIRMED cluster."""
    summary = await get_identity_summary(discord_user_id, community_id)
    return summary.warning_count


def profile_stats_account_ids(member_id: int, alt_accounts: list[int] | None) -> set[int]:
    """Statistics include only Discord aliases attached to the same internal User."""
    return {int(member_id), *(int(account) for account in (alt_accounts or []))}


def profile_warning_lines(warnings: list, external_warning_count: int) -> list[str]:
    lines = [
        f"**{warn.date.strftime('%d/%m/%Y')}** - {warn.reason}"
        for warn in warnings
    ]
    if external_warning_count > 0:
        lines.append(
            f"+ {external_warning_count} advertências associadas a outras contas vinculadas"
        )
    return lines


async def _notify_invalid_ai_token_for_interaction(ctx: discord.Interaction) -> None:
    warning = (
        "⚠️ O token OpenAI configurado para este servidor não está funcional. "
        "Adicione um novo token ou desative as respostas por IA."
    )
    if isinstance(ctx.user, discord.Member) and ctx.user.guild_permissions.administrator:
        try:
            await ctx.user.send(warning)
            return
        except discord.HTTPException:
            pass
    owner = getattr(ctx.guild, "owner", None)
    if owner is None and ctx.guild is not None:
        try:
            owner = await ctx.guild.fetch_member(ctx.guild.owner_id)
        except Exception:
            owner = None
    if owner is not None:
        try:
            await owner.send(warning)
            return
        except discord.HTTPException:
            pass
    logging.warning("Não foi possível notificar admin/dono sobre token OpenAI inválido na guild %s.", getattr(ctx.guild, "id", None))


class BirthdayConflictView(View):
    def __init__(
        self,
        cog: "ModerationCog",
        requester_id: int,
        member: discord.Member,
        new_birthday: date,
        existing_birthday: date,
        age_in_days: int,
        channel: discord.TextChannel,
        carteirinha_provisoria: discord.Role,
        cargo_visitante: discord.Role,
        cargo_maior_18: discord.Role,
        cargo_menor_18: discord.Role | None,
        acesso_provisorio_ativo: bool,
        acesso_provisorio_duracao_dias: int,
        is_new_account: bool,
    ) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.requester_id = requester_id
        self.member = member
        self.new_birthday = new_birthday
        self.existing_birthday = existing_birthday
        self.age_in_days = age_in_days
        self.channel = channel
        self.carteirinha_provisoria = carteirinha_provisoria
        self.cargo_visitante = cargo_visitante
        self.cargo_maior_18 = cargo_maior_18
        self.cargo_menor_18 = cargo_menor_18
        self.acesso_provisorio_ativo = acesso_provisorio_ativo
        self.acesso_provisorio_duracao_dias = acesso_provisorio_duracao_dias
        self.is_new_account = is_new_account

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Apenas quem iniciou a aprovação pode escolher uma opção.",
                ephemeral=True,
            )
            return False
        return True

    async def _handle(self, interaction: Interaction, action: Literal["keep", "replace"]) -> None:
        await interaction.response.defer(ephemeral=True)
        final_birthday = self.new_birthday
        age_in_days = self.age_in_days

        if action == "keep" and self.existing_birthday:
            final_birthday = self.existing_birthday
            age_in_days = (now().date() - final_birthday).days

        await self.cog._finalize_portaria_approval(
            interaction,
            self.member,
            final_birthday,
            age_in_days,
            self.channel,
            self.carteirinha_provisoria,
            self.cargo_visitante,
            self.cargo_maior_18,
            self.cargo_menor_18,
            self.acesso_provisorio_ativo,
            self.acesso_provisorio_duracao_dias,
            self.is_new_account,
            action,
        )
        self.stop()

    @button(label="Manter", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: Interaction, _: discord.ui.Button) -> None:
        await self._handle(interaction, "keep")

    @button(label="Substituir", style=discord.ButtonStyle.primary)
    async def replace(self, interaction: Interaction, _: discord.ui.Button) -> None:
        await self._handle(interaction, "replace")


class ProfileInfoView(View):
    def __init__(
        self,
        member: discord.User,
        member_profile,
        requester: discord.abc.User,
        guild: discord.Guild,
        bot: commands.Bot,
        *,
        enable_warns_button: bool,
        enable_notes_button: bool,
        enable_bans_button: bool,
        ban_records: list[dict],
        profile_embed: discord.Embed,
        external_warning_count: int = 0,
    ) -> None:
        super().__init__(timeout=120)
        self.member = member
        self.member_profile = member_profile
        self.bot = bot
        self.requester_id = requester.id
        self.guild = guild
        self.profile_embed = profile_embed
        self.profile_content: str | None = None
        self.current_view: Literal["profile", "warns", "notes", "stats", "bans"] = "profile"
        self.message: discord.Message | None = None
        self.stats_payload: dict[str, object] | None = None
        self.stats_task: asyncio.Task | None = None
        self.account_ids = profile_stats_account_ids(member.id, member_profile.altAccounts)
        self.ban_records = ban_records
        self.enable_bans_button = enable_bans_button
        self.external_warning_count = external_warning_count

        if not enable_warns_button:
            self.remove_item(self.show_warns)

        if not enable_notes_button:
            self.remove_item(self.show_notes)

        if not enable_bans_button:
            self.remove_item(self.show_bans)

        self._set_active_view("profile")

    def attach_message(self, message: discord.Message) -> None:
        self.message = message

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Apenas quem solicitou o perfil pode usar estes botões.",
                ephemeral=True,
            )
            return False
        return True

    def _set_active_view(self, active: Literal["profile", "warns", "notes", "stats", "bans"]) -> None:
        self.current_view = active
        self.profile_button.disabled = active == "profile"
        if hasattr(self, "show_warns"):
            self.show_warns.disabled = active == "warns"
        if hasattr(self, "show_notes"):
            self.show_notes.disabled = active == "notes"
        if hasattr(self, "show_stats"):
            self.show_stats.disabled = active == "stats"
        if hasattr(self, "show_bans"):
            self.show_bans.disabled = active == "bans"

    async def _edit_main_message(
        self,
        interaction: Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        file: discord.File | None = None,
    ) -> None:
        edit_kwargs: dict[str, object] = {
            "content": content,
            "embed": embed,
            "view": self,
            "attachments": [],
        }

        if file:
            edit_kwargs["files"] = [file]

        if interaction.response.is_done():
            target_message = getattr(interaction, "message", None)
            if target_message is not None:
                await target_message.edit(**edit_kwargs)
            else:
                await interaction.edit_original_response(**edit_kwargs)
            return

        await interaction.response.edit_message(**edit_kwargs)

    @staticmethod
    def _build_embed_from_lines(title: str, lines: list[str]) -> tuple[discord.Embed | None, discord.File | None]:
        def _build_attachment_filename(base_title: str) -> str:
            safe_title = re.sub(r"[^A-Za-z0-9_-]+", "_", base_title).strip("_") or "detalhes"
            return f"{safe_title.lower()}_detalhes.txt"

        combined = "\n".join(lines)
        embed = discord.Embed(title=title, color=discord.Color.blurple())

        if len(combined) <= 4000:
            embed.description = combined
            return embed, None

        chunks: list[str] = []
        current_chunk: list[str] = []
        current_length = 0

        for line in lines:
            additional_length = len(line) + 1  # +1 for the newline
            if current_length + additional_length > 1000 and current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = [line]
                current_length = additional_length
            else:
                current_chunk.append(line)
                current_length += additional_length

        if current_chunk:
            chunks.append("\n".join(current_chunk))

        total_chunk_length = sum(len(chunk) for chunk in chunks)
        field_count = len(chunks)

        if field_count > 25 or (total_chunk_length + len(title)) > 5900:
            file = discord.File(
                io.BytesIO(combined.encode("utf-8")),
                filename=_build_attachment_filename(title),
            )
            return None, file

        for index, chunk in enumerate(chunks, start=1):
            embed.add_field(name=f"Página {index}", value=chunk, inline=False)

        return embed, None

    @staticmethod
    def _format_number(value: int | float | None) -> str:
        if value is None:
            return "0"
        return f"{value:,.0f}".replace(",", ".")

    @staticmethod
    def _format_duration(seconds: int) -> str:
        return str(timedelta(seconds=seconds))

    def _build_record_lines(self) -> list[str]:
        records: list[str] = []
        voice_record = self.member_profile.voiceRecord
        game_record = self.member_profile.gameRecord

        if voice_record and voice_record.get("rank") and voice_record["rank"] <= 10:
            duration = self._format_duration(voice_record.get("seconds", 0))
            records.append(f"**Em call:** {duration} (#{voice_record['rank']})")
        if game_record and game_record.get("rank") and game_record["rank"] <= 10:
            duration = self._format_duration(game_record.get("seconds", 0))
            game_name = game_record.get("game")
            suffix = f" - {game_name}" if game_name else ""
            records.append(f"**Em jogo:** {duration}{suffix} (#{game_record['rank']})")

        return records

    async def _fetch_message_count_from_discord(self, guild: discord.Guild, user_id: int) -> int | None:
        route = Route("GET", "/guilds/{guild_id}/messages/search", guild_id=guild.id)
        params = {"author_id": user_id, "include_nsfw": "true"}
        try:
            response = await self.bot.http.request(route, params=params)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None

        total_results = response.get("total_results")
        try:
            return int(total_results)
        except (TypeError, ValueError):
            return None

    async def _collect_message_counts(self) -> tuple[dict[int, int], list[int]]:
        message_counts: dict[int, int] = {}
        failed_accounts: list[int] = []

        async def _fetch(user_id: int) -> tuple[int, int | None]:
            try:
                count = await asyncio.wait_for(
                    self._fetch_message_count_from_discord(self.guild, user_id),
                    timeout=12,
                )
                return user_id, count
            except asyncio.TimeoutError:
                return user_id, None
            except Exception:
                return user_id, None

        tasks = [_fetch(account_id) for account_id in self.account_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                continue
            user_id, count = result
            if count is None:
                failed_accounts.append(user_id)
                continue
            message_counts[user_id] = count

        return message_counts, failed_accounts

    def _build_stats_embed(
        self,
        message_counts: dict[int, int],
        failed_accounts: list[int],
        pending_accounts: set[int],
    ) -> discord.Embed:
        total_messages = sum(message_counts.values())
        member_since = getattr(self.member_profile, "memberSince", None)

        average_per_day: float | None = None
        if member_since:
            days_in_server = max((now().date() - member_since.date()).days, 1)
            average_per_day = total_messages / days_in_server if days_in_server > 0 else None

        embed = discord.Embed(
            title=(
                f"Estatísticas de "
                f"{self.member.display_name if isinstance(self.member, discord.Member) else self.member.name}"
            ),
            description="⏳ Calculando as mensagens enviadas..." if pending_accounts else None,
            color=discord.Color.from_str('#febf10'),
        )

        discord_joined_at = getattr(self.member, "created_at", None)
        if discord_joined_at:
            days_since_discord = max((now().date() - discord_joined_at.date()).days, 0)
            discord_joined_value = f"{discord_joined_at:%d/%m/%Y} ({days_since_discord} dias)"
        else:
            discord_joined_value = "Indisponível"

        embed.add_field(
            name="Ingressou no Discord",
            value=discord_joined_value,
            inline=True,
        )
        embed.add_field(
            name="Mensagens enviadas",
            value=self._format_number(total_messages),
            inline=True,
        )
        embed.add_field(
            name="Média por dia",
            value=f"{average_per_day:.2f}" if average_per_day is not None else "Indisponível",
            inline=True,
        )

        account_lines = []
        for account_id in sorted(self.account_ids):
            if account_id in pending_accounts:
                formatted_count = "Calculando..."
            elif account_id in message_counts:
                formatted_count = self._format_number(message_counts.get(account_id))
            else:
                formatted_count = "Indisponível"
            account_lines.append(f"<@{account_id}>: {formatted_count}")
        embed.add_field(
            name="Contas consideradas",
            value="\n".join(account_lines),
            inline=False,
        )

        record_lines = self._build_record_lines()
        embed.add_field(
            name="Recordes no ranking",
            value="\n".join(record_lines) if record_lines else "Nenhum recorde registrado no ranking.",
            inline=False,
        )

        if failed_accounts and not pending_accounts:
            embed.add_field(
                name="Aviso",
                value=(
                    "Não foi possível recuperar a contagem de mensagens das contas: "
                    + ", ".join(f"<@{account_id}>" for account_id in failed_accounts)
                ),
                inline=False,
            )

        return embed

    def _build_stats_placeholder(self) -> dict[str, object]:
        embed = self._build_stats_embed({}, [], set(self.account_ids))
        return {"content": "Atualizando estatísticas. Aguarde só um pouco!", "embed": embed, "file": None}

    async def _edit_stats_message(
        self,
        interaction: Interaction,
        message: discord.Message | None,
        *,
        content: str | None,
        embed: discord.Embed,
    ) -> None:
        edit_kwargs: dict[str, object] = {
            "content": content,
            "embed": embed,
            "attachments": [],
            "view": self,
        }
        message_flags = getattr(message, "flags", None)
        is_ephemeral = bool(message_flags and message_flags.ephemeral)
        if message is not None and not is_ephemeral:
            await message.edit(**edit_kwargs)
            return
        await interaction.edit_original_response(**edit_kwargs)

    async def _finalize_stats_update(
        self,
        interaction: Interaction,
        message: discord.Message | None,
    ) -> None:
        try:
            message_counts: dict[int, int] = {}
            failed_accounts: list[int] = []
            pending_accounts = set(self.account_ids)

            for account_id in sorted(self.account_ids):
                if self.current_view != "stats":
                    return
                try:
                    count = await asyncio.wait_for(
                        self._fetch_message_count_from_discord(self.guild, account_id),
                        timeout=12,
                    )
                except asyncio.TimeoutError:
                    count = None
                except Exception:
                    count = None

                if self.current_view != "stats":
                    return

                pending_accounts.discard(account_id)
                if count is None:
                    failed_accounts.append(account_id)
                else:
                    message_counts[account_id] = count

                embed = self._build_stats_embed(message_counts, failed_accounts, pending_accounts)
                await self._edit_stats_message(
                    interaction,
                    message,
                    content="Atualizando estatísticas. Aguarde só um pouco!" if pending_accounts else None,
                    embed=embed,
                )

            if self.current_view != "stats":
                return

            final_embed = self._build_stats_embed(message_counts, failed_accounts, set())
            self.stats_payload = {"content": None, "embed": final_embed, "file": None}
            await self._edit_stats_message(interaction, message, content=None, embed=final_embed)
        except Exception:
            logging.exception("Erro ao atualizar estatísticas do perfil.")
        finally:
            self.stats_task = None

    @button(label="Perfil", style=discord.ButtonStyle.primary)
    async def profile_button(self, interaction: Interaction, _: discord.ui.Button) -> None:
        self._set_active_view("profile")
        await self._edit_main_message(
            interaction,
            content=self.profile_content,
            embed=self.profile_embed,
        )

    @button(label="Estatísticas", style=discord.ButtonStyle.secondary)
    async def show_stats(self, interaction: Interaction, _: discord.ui.Button) -> None:
        self._set_active_view("stats")
        self.stats_payload = None
        placeholder_payload = self._build_stats_placeholder()
        await self._edit_main_message(
            interaction,
            content=placeholder_payload.get("content"),
            embed=placeholder_payload.get("embed"),
            file=placeholder_payload.get("file"),
        )
        if self.stats_task is None or self.stats_task.done():
            target_message = interaction.message or self.message
            self.stats_task = asyncio.create_task(
                self._finalize_stats_update(interaction, target_message)
            )

    @button(label="Warns", style=discord.ButtonStyle.secondary)
    async def show_warns(self, interaction: Interaction, _: discord.ui.Button) -> None:
        warnings = sorted(
            self.member_profile.warnings,
            key=lambda warn: warn.date or datetime.min,
            reverse=True,
        )
        self._set_active_view("warns")

        if warnings or self.external_warning_count > 0:
            warning_lines = profile_warning_lines(
                warnings,
                self.external_warning_count,
            )
            warn_embed, warn_file = self._build_embed_from_lines(
                f"Warnings de {self.member.display_name if isinstance(self.member, discord.Member) else self.member.name}",
                warning_lines,
            )
            warn_message = "Os warnings são longos; consulte o arquivo em anexo." if warn_file else None
        else:
            warn_embed = None
            warn_file = None
            warn_message = f"O membro <@{self.member.id}> não possui warnings."

        await self._edit_main_message(
            interaction,
            content=warn_message,
            embed=warn_embed,
            file=warn_file,
        )

    @button(label="Notas", style=discord.ButtonStyle.secondary)
    async def show_notes(self, interaction: Interaction, _: discord.ui.Button) -> None:
        staff_roles = getStaffRoles(self.guild)
        if not any(role in interaction.user.roles for role in staff_roles):
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(
                "Você não tem permissão para ver as notas deste membro.", ephemeral=True
            )
            return

        notes = getMemberNotes(self.guild.id, self.member)
        self._set_active_view("notes")
        if notes:
            note_lines = []
            for index, note in enumerate(notes, start=1):
                author = (
                    f"<@{note.author_discord_id}>" if note.author_discord_id else "Autor desconhecido"
                )
                note_lines.append(f"**{index}.** {note.note} — {author}")
        else:
            note_lines = []

        if note_lines:
            notes_embed, notes_file = self._build_embed_from_lines(
                f"Notas de {self.member.display_name if isinstance(self.member, discord.Member) else self.member.name}",
                note_lines,
            )
            note_message = "As notas são longas; consulte o arquivo em anexo." if notes_file else None
            await self._edit_main_message(
                interaction,
                content=note_message,
                embed=notes_embed,
                file=notes_file,
            )
        else:
            message = f"O membro <@{self.member.id}> não possui notas registradas."
            await self._edit_main_message(interaction, content=message)

    @button(label="Bans", style=discord.ButtonStyle.secondary)
    async def show_bans(self, interaction: Interaction, _: discord.ui.Button) -> None:
        if not self.enable_bans_button:
            await interaction.response.send_message(
                "O membro não possui ban ativo nem histórico de bans registrado.",
                ephemeral=True,
            )
            return

        self._set_active_view("bans")
        if self.ban_records:
            ban_lines = []
            for record in self.ban_records:
                ban_date = record.get("date")
                ban_reason = record.get("reason") or "Sem motivo informado."
                date_label = (
                    ban_date.strftime("%d/%m/%Y")
                    if isinstance(ban_date, datetime)
                    else "Data indisponível"
                )
                ban_lines.append(f"**{date_label}** - {ban_reason}")

            bans_embed, bans_file = self._build_embed_from_lines(
                f"Bans de {self.member.display_name if isinstance(self.member, discord.Member) else self.member.name}",
                ban_lines,
            )
            bans_message = "Os bans são longos; consulte o arquivo em anexo." if bans_file else None
            await self._edit_main_message(
                interaction,
                content=bans_message,
                embed=bans_embed,
                file=bans_file,
            )
        else:
            await self._edit_main_message(
                interaction,
                content=f"O membro <@{self.member.id}> não possui bans registrados.",
            )

    async def on_timeout(self) -> None:
        if not self.message:
            return

        for item in self.children:
            item.disabled = True

        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass
        finally:
            self.stop()


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()
        self.profile_context_menu = app_commands.ContextMenu(
            name='Ver perfil',
            callback=self.profile_context_menu_callback,
        )
        self.check_expired_bans.start()

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.profile_context_menu)

    def cog_unload(self):
        self.bot.tree.remove_command(
            self.profile_context_menu.name,
            type=self.profile_context_menu.type,
        )
        self.check_expired_bans.cancel()

    @tasks.loop(hours=1)
    async def check_expired_bans(self):
        """Scheduled task to check for expired bans and unban users automatically."""
        for guild in self.bot.guilds:
            try:
                expired_bans = getExpiredBans(guild.id)
                
                for ban_record in expired_bans:
                    discord_user_id = ban_record["discord_user_id"]
                    ban_id = ban_record["ban_id"]
                    effect_id = ban_record.get("effect_id")

                    if discord_user_id is None:
                        if finalizeExpiredBan(ban_id):
                            logging.info(
                                "Banimento temporário %s finalizado sem efeitos Discord pendentes no servidor %s",
                                ban_id,
                                guild.id,
                            )
                        else:
                            logging.error(
                                "Falha ao finalizar banimento temporário %s sem efeitos Discord pendentes",
                                ban_id,
                            )
                        continue

                    if hasOtherActiveBanRequirement(
                        guild.id,
                        ban_id,
                        discord_user_id,
                    ):
                        logging.info(
                            "Unban adiado para usuário %s no servidor %s: outro ban ativo da mesma Community ainda exige o bloqueio",
                            discord_user_id,
                            guild.id,
                        )
                        continue

                    try:
                        # Try to unban the user from the guild
                        user = await self.bot.fetch_user(discord_user_id)
                        await guild.unban(user, reason="Ban temporário expirado")
                        
                        recorded = (
                            markBanDiscordEffectReverted(effect_id, "REVERTED")
                            if effect_id is not None
                            else removeBanRecord(ban_id)
                        )
                        if recorded:
                            if effect_id is not None:
                                finalizeExpiredBan(ban_id)
                            logging.info(
                                "Unban automático executado para usuário %s no servidor %s",
                                discord_user_id,
                                guild.id,
                            )
                        else:
                            logging.error(
                                "Falha ao remover registro de ban %s após desbanir usuário %s",
                                ban_id,
                                discord_user_id,
                            )
                    except discord.NotFound:
                        recorded = (
                            markBanDiscordEffectReverted(
                                effect_id, "ALREADY_UNBANNED", "NotFound"
                            )
                            if effect_id is not None
                            else removeBanRecord(ban_id)
                        )
                        if recorded:
                            if effect_id is not None:
                                finalizeExpiredBan(ban_id)
                            logging.info(
                                "Registro de ban expirado removido para usuário %s (já não estava banido)",
                                discord_user_id,
                            )
                        else:
                            logging.error(
                                "Falha ao remover registro de ban expirado %s para usuário %s",
                                ban_id,
                                discord_user_id,
                            )
                    except discord.Forbidden as error:
                        if effect_id is not None:
                            markBanDiscordEffectReverted(
                                effect_id, "FORBIDDEN", type(error).__name__
                            )
                        logging.error(
                            "Sem permissão para desbanir usuário %s no servidor %s",
                            discord_user_id,
                            guild.id,
                        )
                    except Exception as error:
                        if effect_id is not None:
                            markBanDiscordEffectReverted(
                                effect_id, "FAILED", type(error).__name__
                            )
                        logging.error(
                            "Erro ao desbanir usuário %s no servidor %s: %s",
                            discord_user_id,
                            guild.id,
                            error,
                        )
            except Exception as error:
                logging.error(
                    "Erro ao verificar banimentos expirados no servidor %s: %s",
                    guild.id,
                    error,
                )

    @check_expired_bans.before_loop
    async def before_check_expired_bans(self):
        """Wait until the bot is ready before starting the task."""
        await self.bot.wait_until_ready()

    async def _notify_portaria_error(
        self,
        ctx: discord.Interaction,
        error: Exception,
        *,
        use_followup: bool,
        command_params: dict | None = None,
        ) -> None:
        await notify_owner_and_user(
            ctx,
            error,
            "Não foi possível concluir a aprovação. A staff foi notificada e investigará o problema.",
            use_followup=use_followup,
            command_params=command_params,
            ephemeral=True,
        )

    async def _send_warn_notification(
        self, member: discord.User, message: str, *, enabled: bool
    ) -> str:
        if not enabled:
            return ""

        try:
            await member.send(message)
            return ""
        except Exception:
            return " Não foi possível enviar mensagem privada para o membro."

    @staticmethod
    def _parse_duration(duration: str) -> timedelta | relativedelta | None:
        match = re.fullmatch(r"(\d+)([dsma])", duration.strip().lower().replace(" ", ""))
        if not match:
            return None

        amount = int(match.group(1))
        unit = match.group(2)
        if unit == "d":
            return timedelta(days=amount)
        if unit == "s":
            return timedelta(weeks=amount)
        if unit == "m":
            return relativedelta(months=amount)
        if unit == "a":
            return relativedelta(years=amount)
        return None


    @staticmethod
    async def _remove_visitante_role(
        member: discord.Member,
        cargo_visitante: discord.Role,
    ) -> None:
        if cargo_visitante in member.roles:
            await member.remove_roles(cargo_visitante)


    async def _apply_provisional_temp_roles(
        self,
        interaction: Interaction,
        member: discord.Member,
        channel: discord.TextChannel,
        carteirinha_provisoria: discord.Role,
        cargo_visitante: discord.Role,
        duracao_dias: int,
        update_channel: bool = True,
    ) -> None:
        expiration_date = now() + timedelta(days=duracao_dias)
        await assignTempRole(
            interaction.guild_id,
            member,
            cargo_visitante.id,
            expiration_date,
            'Cargo visitante temporário',
        )
        await assignTempRole(
            interaction.guild_id,
            member,
            carteirinha_provisoria.id,
            expiration_date,
            'Carteirinha provisória',
        )
        if update_channel:
            await channel.edit(
                name=(
                    f'{channel.name}-provisória'
                    if 'provisória' not in channel.name
                    else channel.name
                ),
            )
        try:
            await member.send(
                (
                    f'Sua conta recebeu o **acesso provisório** por {duracao_dias} dia(s) por ter menos de 30 dias de criação. '
                    'Após esse período, sua liberação completa ocorrerá automaticamente.'
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            pass


    async def _finalize_portaria_approval(
        self,
        interaction: Interaction,
        member: discord.Member,
        birthday: date,
        age_in_days: int,
        channel: discord.TextChannel,
        carteirinha_provisoria: discord.Role,
        cargo_visitante: discord.Role,
        cargo_maior_18: discord.Role,
        cargo_menor_18: discord.Role | None,
        acesso_provisorio_ativo: bool,
        acesso_provisorio_duracao_dias: int,
        is_new_account: bool,
        birthday_action: Literal["keep", "replace"] | None = None,
        update_channel: bool = True,
    ) -> bool:
        eighteen_years_in_days = 6570
        thirteen_years_in_days = 4745

        if member is None:
            raise ValueError("Membro não encontrado para registro")

        if birthday is None:
            raise ValueError("Data de aniversário não informada para registro")

        if age_in_days < thirteen_years_in_days:
            idade = relativedelta(now().date(), birthday).years
            await interaction.edit_original_response(
                content=(
                    f'O membro <@{member.id}> informou ter {idade} anos. '
                    'De acordo com os Termos de Serviço do Discord, menores de 13 anos não podem utilizar a plataforma, '
                    'portanto a aprovação foi bloqueada.\n'
                    'Finalize o atendimento adotando as medidas cabíveis (por exemplo, expulsar o usuário).'
                ),
                view=None,
            )
            return False

        config = get_portaria_base_config(interaction.guild.id)
        idade_minima_entrada_ativa = bool(config.get("idade_minima_entrada_servidor_ativa"))
        idade_minima_entrada_anos = int(config.get("idade_minima_entrada_servidor_anos") or 0)
        idade_anos = relativedelta(now().date(), birthday).years
        if idade_minima_entrada_ativa and idade_anos < idade_minima_entrada_anos:
            await interaction.edit_original_response(
                content=(
                    f'O membro <@{member.id}> possui {idade_anos} ano(s), abaixo da idade mínima '
                    f'configurada para entrada no servidor ({idade_minima_entrada_anos} anos). '
                    'A aprovação foi bloqueada automaticamente.'
                ),
                view=None,
            )
            return False

        if age_in_days >= eighteen_years_in_days:
            await member.add_roles(cargo_maior_18)
            if cargo_menor_18 is not None:
                await member.remove_roles(cargo_menor_18)
        elif age_in_days >= thirteen_years_in_days:
            if cargo_menor_18 is None:
                await interaction.edit_original_response(
                    content=(
                        "Configuração da portaria incompleta para concluir a aprovação. "
                        "Faltando: cargo menor_18. Configure com /admin cargos setar."
                    ),
                    view=None,
                )
                return False
            await member.add_roles(cargo_menor_18)
            await member.remove_roles(cargo_maior_18)

        try:
            registerUser(
                interaction.guild.id,
                member,
                birthday,
                now().date(),
                birthday_action,
            )
        except Exception as e:
            await self._notify_portaria_error(
                interaction,
                e,
                use_followup=interaction.response.is_done(),
                command_params={
                    "member_id": getattr(member, "id", "unknown"),
                    "channel_id": getattr(interaction.channel, "id", "unknown"),
                    "birthday": birthday,
                    "birthday_action": birthday_action,
                },
            )
            if interaction.response.is_done():
                await interaction.edit_original_response(
                    content=(
                        "Não foi possível registrar o membro no momento. "
                        "O incidente foi reportado para a staff."
                    ),
                    view=None,
                )
            return False

        if is_new_account and acesso_provisorio_ativo:
            await self._apply_provisional_temp_roles(
                interaction,
                member,
                channel,
                carteirinha_provisoria,
                cargo_visitante,
                acesso_provisorio_duracao_dias,
                update_channel=update_channel,
            )
            await interaction.edit_original_response(
                content=(
                    f'O membro <@{member.id}> entrará no servidor com **acesso provisório** '
                    'por sua conta ser considerada **recente** nas regras configuradas. '
                    f'O acesso provisório ficará ativo por **{acesso_provisorio_duracao_dias} dia(s)**.'
                ),
                view=None,
            )
            clear_portaria_account_release_override(interaction.guild.id, member.id)
            return True

        await self._remove_visitante_role(member, cargo_visitante)
        if update_channel:
            await channel.edit(
                name=f'{channel.name}-✅' if '-✅' not in channel.name else channel.name,
            )
        await interaction.edit_original_response(
            content=(f'O membro <@{member.id}> foi aprovado com sucesso!'),
            view=None,
        )
        clear_portaria_account_release_override(interaction.guild.id, member.id)
        return True


    @app_commands.command(name='chat_analisar_membro', description='Analisa mensagens de um membro em um canal (apenas staff)')
    @app_commands.describe(
        membro='Membro a ser analisado',
        canal='Canal a ser analisado (opcional)',
        periodo='Período das mensagens a considerar',
        contexto='Contexto adicional obrigatório para a análise',
        nao_efemera='Deixe ativado para enviar a análise de forma pública no canal',
    )
    async def analyze_member_chat(
        self,
        ctx: discord.Interaction,
        membro: discord.Member,
        periodo: Literal[
            'ultimas_10',
            'ultimas_50',
            'ultimas_100',
            'ultimo_mes',
            'ultimos_6_meses',
        ],
        contexto: str,
        canal: discord.TextChannel | None = None,
        nao_efemera: bool = False,
    ):
        staff_roles = getStaffRoles(ctx.guild)
        if not any(role in ctx.user.roles for role in staff_roles):
            return await ctx.response.send_message(
                content='Apenas membros da staff podem usar este comando.',
                ephemeral=True,
            )

        gpt_properties = hasGPTEnabled(ctx.guild)
        ai_settings = get_ai_response_settings(ctx.guild.id)
        openai_token = ai_settings.get("openaiToken")
        gpt_enabled = False
        gpt_model = 'gpt-3.5-turbo'

        if isinstance(gpt_properties, dict):
            gpt_enabled = bool(gpt_properties.get('enabled'))
            gpt_model = gpt_properties.get('model') or gpt_model
        elif gpt_properties:
            try:
                gpt_enabled = bool(gpt_properties[0])
                if len(gpt_properties) > 1 and gpt_properties[1]:
                    gpt_model = gpt_properties[1]
            except (IndexError, TypeError):
                gpt_enabled = False

        if not gpt_enabled:
            return await ctx.response.send_message(
                content='A análise por IA não está habilitada neste servidor.',
                ephemeral=True,
            )
        if not openai_token:
            return await ctx.response.send_message(
                content='Token OpenAI não configurado para este servidor.',
                ephemeral=True,
            )

        response_ephemeral = not nao_efemera

        await ctx.response.defer(ephemeral=response_ephemeral)
        try:
            channel = canal or ctx.channel
            webhook_token_expired = False

            async def _update_status(content: str) -> bool:
                nonlocal webhook_token_expired
                if webhook_token_expired:
                    return False

                try:
                    await ctx.edit_original_response(content=content, attachments=[])
                    return True
                except discord.HTTPException as error:
                    if getattr(error, 'code', None) == 50027:
                        webhook_token_expired = True
                        logging.warning(
                            'Token de webhook expirado ao atualizar status da análise do membro %s.',
                            membro.id,
                        )
                        return False
                    raise

            async def _send_fallback_message(
                content: str,
                *,
                file_bytes: bytes | None = None,
                filename: str | None = None,
            ) -> None:
                def _build_file() -> discord.File | None:
                    if file_bytes is None:
                        return None
                    return discord.File(
                        io.BytesIO(file_bytes),
                        filename=filename or 'analise_membro.txt',
                    )

                if response_ephemeral:
                    try:
                        await ctx.user.send(content=content, file=_build_file())
                        return
                    except discord.HTTPException:
                        logging.exception(
                            'Falha ao enviar DM de fallback com análise do membro %s.',
                            membro.id,
                        )
                    await channel.send(
                        f'{ctx.user.mention} não foi possível entregar a análise em privado por DM. '
                        'Habilite mensagens diretas e tente novamente.'
                    )
                    return
                await channel.send(content=content, file=_build_file())

            await _update_status(content='Iniciando análise...')
            now_utc = datetime.now(timezone.utc)

            period_config = {
                'ultimas_10': {'max_messages': 10, 'after': None},
                'ultimas_50': {'max_messages': 50, 'after': None},
                'ultimas_100': {'max_messages': 100, 'after': None},
                'ultimo_mes': {'max_messages': 1000, 'after': now_utc - relativedelta(months=1)},
                'ultimos_6_meses': {'max_messages': 1000, 'after': now_utc - relativedelta(months=6)},
            }
            selected_period = period_config[periodo]

            total_found = 0
            collected_messages: list[discord.Message] = []
            truncated_collection = False

            history_kwargs: dict[str, object] = {
                'limit': None,
                'oldest_first': False,
            }
            if selected_period['after']:
                history_kwargs['after'] = selected_period['after']

            await _update_status(content='Reunindo mensagens do membro no período selecionado...')
            async for message in channel.history(**history_kwargs):
                if message.author.id != membro.id:
                    continue

                total_found += 1
                if len(collected_messages) < selected_period['max_messages']:
                    collected_messages.append(message)
                else:
                    truncated_collection = True

                if (
                    selected_period['after'] is None
                    and len(collected_messages) >= selected_period['max_messages']
                ):
                    break

            if not collected_messages:
                await _update_status(
                    content='Não há mensagens deste membro no período informado.',
                )
                return

            collected_messages.reverse()
            total_analyzed = len(collected_messages)

            def _shrink(text: str, limit: int = 180) -> tuple[str, bool]:
                compact = ' '.join(text.split())
                if len(compact) <= limit:
                    return compact, False
                return compact[: limit - 1] + '…', True

            conversation_blocks: list[str] = []
            compacted = False

            for message in collected_messages:
                content = message.clean_content.strip()

                if not content and message.attachments:
                    attachments = ', '.join(attachment.filename for attachment in message.attachments)
                    content = f'Anexos: {attachments}'

                if not content:
                    continue

                reference_line = None
                if message.reference and message.reference.message_id:
                    referenced_message = message.reference.resolved
                    ref_channel = channel
                    if referenced_message is None and message.reference.channel_id:
                        ref_channel = ctx.guild.get_channel(message.reference.channel_id) or channel
                    if referenced_message is None and ref_channel:
                        try:
                            referenced_message = await ref_channel.fetch_message(message.reference.message_id)
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            referenced_message = None

                    if referenced_message and hasattr(referenced_message, 'content') and referenced_message.content:
                        ref_author = (
                            referenced_message.author.display_name
                            if isinstance(referenced_message.author, discord.Member)
                            else referenced_message.author.name
                        )
                        ref_content, was_truncated = _shrink(referenced_message.clean_content)
                        reference_line = f'(em resposta a: {ref_author}: {ref_content})'
                        compacted = compacted or was_truncated

                author_name = (
                    message.author.display_name
                    if isinstance(message.author, discord.Member)
                    else message.author.name
                )
                compact_content, was_truncated = _shrink(content, 500)
                if was_truncated:
                    compacted = True

                block_lines = []
                if reference_line:
                    block_lines.append(reference_line)
                block_lines.append(f'{author_name}: {compact_content}')
                conversation_blocks.append('\n'.join(block_lines))

            if not conversation_blocks:
                await _update_status(
                    content='Não há mensagens com conteúdo para analisar.',
                )
                return

            await _update_status(
                content=f'{total_analyzed} mensagens analisadas de {total_found} encontradas...'
            )

            def build_block_ranges(block_size: int = 40, max_chars: int = 3500) -> list[tuple[int, int, str]]:
                blocks: list[tuple[int, int, str]] = []
                current_messages: list[str] = []
                current_length = 0
                start_index = 1

                for idx, entry in enumerate(conversation_blocks, start=1):
                    projected_length = current_length + len(entry) + 2
                    if current_messages and (len(current_messages) >= block_size or projected_length > max_chars):
                        blocks.append((start_index, idx - 1, '\n\n'.join(current_messages)))
                        current_messages = []
                        current_length = 0
                        start_index = idx

                    current_messages.append(entry)
                    current_length += len(entry) + 2

                if current_messages:
                    blocks.append((start_index, start_index + len(current_messages) - 1, '\n\n'.join(current_messages)))

                return blocks

            blocks = build_block_ranges()
            await _update_status(content='Formatando mensagens para análise... (0/{})'.format(len(blocks)))

            block_summaries: list[str] = []
            total_blocks = len(blocks)
            for block_index, (start, end, block_text) in enumerate(blocks, start=1):
                if block_index == 1 or block_index == total_blocks or block_index % 2 == 0:
                    await _update_status(
                        content=(
                            f'Resumindo blocos de mensagens... ({block_index}/{total_blocks})'
                        )
                    )
                try:
                    summary = await resumirConversaHistorico(block_text, gpt_model, openai_token)
                    if summary == OPENAI_TOKEN_INVALID_MARKER:
                        await _notify_invalid_ai_token_for_interaction(ctx)
                        return await _update_status(
                            content=(
                                "Token OpenAI inválido neste servidor. "
                                "Peça para um admin atualizar o token ou desativar respostas por IA."
                            ),
                        )
                except Exception:
                    logging.exception('Erro ao resumir bloco %s-%s do chat', start, end)
                    summary = 'Não foi possível gerar o resumo deste bloco.'
                block_summaries.append(f'Bloco {start}-{end}:\n{summary}')

            summary_prompt_parts = [
                f'Contexto fornecido: {contexto}',
                f'Mensagens encontradas: {total_found}',
                f'Mensagens analisadas: {total_analyzed}',
                'Resumos por bloco:',
                '\n\n'.join(block_summaries),
                (
                    'Com base nos resumos, forneça uma análise consolidada sobre padrões de comportamento, '
                    'possíveis riscos, postura geral no canal e recomendações objetivas para a staff.'
                ),
            ]
            final_prompt = '\n\n'.join(summary_prompt_parts)

            await _update_status(content='Gerando análise final com IA...')
            try:
                final_analysis = await resumirConversaHistorico(final_prompt, gpt_model, openai_token)
                if final_analysis == OPENAI_TOKEN_INVALID_MARKER:
                    await _notify_invalid_ai_token_for_interaction(ctx)
                    return await _update_status(
                        content=(
                            "Token OpenAI inválido neste servidor. "
                            "Peça para um admin atualizar o token ou desativar respostas por IA."
                        ),
                    )
            except Exception:
                logging.exception('Erro ao gerar análise consolidada do membro %s', membro.id)
                final_analysis = 'Não foi possível gerar a análise consolidada no momento.'

            final_analysis = (final_analysis or '').strip()
            if not final_analysis:
                logging.warning(
                    'A análise consolidada do membro %s retornou vazia; usando fallback.',
                    membro.id,
                )
                final_analysis = (
                    'Não foi possível obter conteúdo de análise da IA nesta tentativa. '
                    'Tente novamente em alguns instantes.'
                )

            notes: list[str] = []
            if truncated_collection:
                notes.append('A análise foi limitada a 1000 mensagens para o período solicitado.')
            if compacted:
                notes.append('Algumas mensagens foram compactadas para caber no limite de caracteres.')

            notes_text = f"\n\nObservações: {' '.join(notes)}" if notes else ''
            response_header = 'Análise consolidada do membro'
            final_content = f'**{response_header}:**\n{final_analysis}{notes_text}'

            if len(final_analysis) <= 2000:
                updated = await _update_status(content=final_content)
                if not updated:
                    await _send_fallback_message(final_content)
            else:
                truncation_note = ' (resposta excedeu 2000 caracteres; conteúdo completo no arquivo)'
                compact_content = f'**{response_header}{truncation_note}:**{notes_text or ""}'
                analysis_bytes = final_analysis.encode('utf-8')
                if webhook_token_expired:
                    await _send_fallback_message(
                        compact_content,
                        file_bytes=analysis_bytes,
                        filename='analise_membro.txt',
                    )
                else:
                    try:
                        analysis_file = discord.File(
                            io.BytesIO(analysis_bytes),
                            filename='analise_membro.txt',
                        )
                        await ctx.edit_original_response(
                            content=compact_content,
                            attachments=[analysis_file],
                        )
                    except discord.HTTPException as error:
                        if getattr(error, 'code', None) != 50027:
                            raise
                        webhook_token_expired = True
                        logging.warning(
                            'Token de webhook expirado ao enviar arquivo da análise do membro %s.',
                            membro.id,
                        )
                        await _send_fallback_message(
                            compact_content,
                            file_bytes=analysis_bytes,
                            filename='analise_membro.txt',
                        )
        except discord.Forbidden:
            await _update_status(
                content='Não tenho permissão para ler o histórico deste canal ou enviar mensagens aqui.',
            )
        except discord.HTTPException:
            logging.exception('Erro HTTP ao analisar mensagens do membro %s', membro.id)
            await _update_status(
                content='Não foi possível coletar o histórico de mensagens para análise.',
            )
        except Exception:
            logging.exception('Erro inesperado ao analisar mensagens do membro %s', membro.id)
            await _update_status(
                content='Ocorreu um erro ao processar a análise. Tente novamente em instantes.',
            )

    @app_commands.command(name='chat_resumir', description='Gera um resumo das mensagens recentes de um canal (apenas staff)')
    @app_commands.describe(
        linhas='Quantidade de mensagens recentes a considerar',
        canal='Canal a ser analisado (opcional)',
        mensagem_id='ID de uma mensagem para definir o ponto de referência (opcional)',
        intervalo='Direção das mensagens em relação à mensagem informada',
        nao_efemera='Deixe desativado para receber o resumo de forma privada',
    )
    async def summarizeChat(
        self,
        ctx: discord.Interaction,
        linhas: app_commands.Range[int, 1, 100],
        canal: discord.TextChannel | None = None,
        mensagem_id: str | None = None,
        intervalo: Literal['anteriores', 'posteriores'] = 'anteriores',
        nao_efemera: bool = False,
    ):
        staff_roles = getStaffRoles(ctx.guild)
        if not any(role in ctx.user.roles for role in staff_roles):
            return await ctx.response.send_message(
                content='Apenas membros da staff podem usar este comando.',
                ephemeral=True,
            )

        gpt_properties = hasGPTEnabled(ctx.guild)
        ai_settings = get_ai_response_settings(ctx.guild.id)
        openai_token = ai_settings.get("openaiToken")
        if not gpt_properties or not gpt_properties['enabled']:
            return await ctx.response.send_message(
                content='A análise por IA não está habilitada neste servidor.',
                ephemeral=True,
            )
        if not openai_token:
            return await ctx.response.send_message(
                content='Token OpenAI não configurado para este servidor.',
                ephemeral=True,
            )

        response_ephemeral = not nao_efemera

        await ctx.response.defer(ephemeral=response_ephemeral)

        channel = canal or ctx.channel
        collected_messages: list[discord.Message] = []

        reference_message: discord.Message | None = None
        message_id_value: int | None = None
        if mensagem_id is not None:
            try:
                message_id_value = int(mensagem_id)
                if message_id_value <= 0:
                    raise ValueError
            except ValueError:
                return await ctx.followup.send(
                    content='O ID da mensagem deve ser um número inteiro válido.',
                    ephemeral=response_ephemeral,
                )
            try:
                reference_message = await channel.fetch_message(message_id_value)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return await ctx.followup.send(
                    content='Não foi possível localizar a mensagem informada.',
                    ephemeral=response_ephemeral,
                )

        history_kwargs: dict[str, object] = {"limit": linhas}
        if reference_message:
            if intervalo == 'anteriores':
                history_kwargs["before"] = reference_message
                history_kwargs["oldest_first"] = False
            else:
                history_kwargs["after"] = reference_message
                history_kwargs["oldest_first"] = True

        async for message in channel.history(**history_kwargs):
            collected_messages.append(message)

        if not collected_messages:
            return await ctx.followup.send(
                content='Não foi possível coletar mensagens para analisar.',
                ephemeral=response_ephemeral,
            )

        if not history_kwargs.get("oldest_first"):
            collected_messages.reverse()

        def _shrink(text: str, limit: int = 180) -> str:
            compact = " ".join(text.split())
            if len(compact) <= limit:
                return compact
            return compact[: limit - 1] + '…'

        conversation_blocks: list[str] = []
        for message in collected_messages:
            content = message.clean_content.strip()

            if not content and message.attachments:
                attachments = ', '.join(attachment.filename for attachment in message.attachments)
                content = f'Anexos: {attachments}'

            if not content:
                continue

            reference_line = None
            if message.reference and message.reference.message_id:
                referenced_message = message.reference.resolved
                if referenced_message is None:
                    ref_channel = channel
                    if message.reference.channel_id and message.reference.channel_id != channel.id:
                        ref_channel = ctx.guild.get_channel(message.reference.channel_id) or channel
                    try:
                        referenced_message = await ref_channel.fetch_message(message.reference.message_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        referenced_message = None

                if (
                    referenced_message
                    and hasattr(referenced_message, "content")
                    and referenced_message.content
                ):
                    ref_author = (
                        referenced_message.author.display_name
                        if isinstance(referenced_message.author, discord.Member)
                        else referenced_message.author.name
                    )
                    ref_content = _shrink(referenced_message.clean_content)
                    reference_line = f'(em resposta a: {ref_author}: {ref_content})'

            author_name = (
                message.author.display_name
                if isinstance(message.author, discord.Member)
                else message.author.name
            )
            block_lines = []
            if reference_line:
                block_lines.append(reference_line)
            block_lines.append(f'{author_name}: {_shrink(content, 500)}')
            conversation_blocks.append('\n'.join(block_lines))

        if not conversation_blocks:
            return await ctx.followup.send(
                content='Não há mensagens com conteúdo para analisar.',
                ephemeral=response_ephemeral,
            )

        transcript = '\n\n'.join(conversation_blocks)
        gpt_model = gpt_properties['model'] if 'model' in gpt_properties and gpt_properties['model'] else 'gpt-3.5-turbo'
        summary = await resumirConversaHistorico(transcript, gpt_model, openai_token)
        if summary == OPENAI_TOKEN_INVALID_MARKER:
            await _notify_invalid_ai_token_for_interaction(ctx)
            return await ctx.followup.send(
                content=(
                    "Token OpenAI inválido neste servidor. "
                    "Peça para um admin atualizar o token ou desativar respostas por IA."
                ),
                ephemeral=response_ephemeral,
            )

        if len(summary) <= 2000:
            await ctx.followup.send(
                content=f'**Resumo do chat:**\n{summary}',
                ephemeral=response_ephemeral,
            )
        else:
            summary_file = discord.File(
                io.BytesIO(summary.encode('utf-8')),
                filename='resumo_chat.txt',
            )
            await ctx.followup.send(
                content=(
                    '**Resumo do chat:**\n'
                    'O resumo completo excede 2000 caracteres. Confira o arquivo anexado.'
                ),
                file=summary_file,
                ephemeral=response_ephemeral,
            )

    async def _send_member_profile(
        self,
        ctx: discord.Interaction,
        member: discord.User,
        *,
        ephemeral: bool,
    ) -> None:
        await ctx.response.defer(ephemeral=ephemeral)
        guild_member = ctx.guild.get_member(member.id)
        memberProfile = getProfileData(ctx.guild.id, guild_member if guild_member else member)
        if not memberProfile:
            await ctx.followup.send("Usuário não encontrado.", ephemeral=ephemeral)
            return

        notes_count = getMemberNotesCount(ctx.guild.id, guild_member if guild_member else member)
        staff_roles = getStaffRoles(ctx.guild)
        actor_is_staff = isinstance(ctx.user, discord.Member) and any(
            role in ctx.user.roles for role in staff_roles
        )
        include_identity_aggregate = should_include_identity_aggregate(
            actor_is_staff,
            ephemeral,
        )
        identity_summary = None
        if include_identity_aggregate:
            try:
                identity_summary = await get_identity_summary(
                    member.id,
                    getCommunityId(ctx.guild.id),
                )
            except (IdentityApiError, ValueError):
                logging.exception(
                    "Não foi possível obter o resumo de identidade do usuário %s na guild %s",
                    member.id,
                    ctx.guild.id,
                )
        portaria_config = get_portaria_base_config(ctx.guild.id)
        visitante_role_id = portaria_config.get("visitante_role_id")
        formulario_portaria_ativo = bool(portaria_config.get("formulario_portaria_ativo", 1))

        visitante_role = (
            ctx.guild.get_role(visitante_role_id) if visitante_role_id else None
        )
        portaria_funcional = all(
            (
                formulario_portaria_ativo,
                visitante_role is not None,
            )
        )
        ban_date = None
        banned_in_discord = False
        ban_records: list[dict] = []
        discord_ban_reason: str | None = None
        ban_applied_by: discord.abc.User | None = None
        discord_ban_date: datetime | None = None
        if guild_member is None:
            try:
                ban_entry = await ctx.guild.fetch_ban(member)
                banned_in_discord = True
                discord_ban_reason = ban_entry.reason
                ban_date = getLatestBanDate(ctx.guild.id, member.id)
                has_active_ban_record = hasActiveBanRecord(ctx.guild.id, member.id)
                if ban_date is None and not has_active_ban_record:
                    try:
                        async for entry in ctx.guild.audit_logs(
                            limit=10,
                            action=discord.AuditLogAction.ban,
                        ):
                            target = getattr(entry, "target", None)
                            if not target or target.id != member.id:
                                continue
                            ban_applied_by = entry.user
                            discord_ban_date = entry.created_at
                            if not discord_ban_reason and entry.reason:
                                discord_ban_reason = entry.reason
                            break
                    except (discord.Forbidden, discord.HTTPException):
                        logging.exception(
                            "Não foi possível consultar os audit logs de ban para usuário %s no servidor %s",
                            member.id,
                            ctx.guild.id,
                        )

                    register_reason = (
                        discord_ban_reason
                        or "Banimento registrado no Discord sem motivo informado."
                    )
                    registered = registerUserBan(
                        ctx.guild.id,
                        member,
                        register_reason,
                        ban_applied_by,
                        valid_until=None,
                        can_appeal=False,
                        ban_date=discord_ban_date,
                        allow_incomplete_record=True,
                    )
                    if not registered:
                        logging.warning(
                            "Falha ao sincronizar banimento do Discord para o banco (usuário %s, servidor %s)",
                            member.id,
                            ctx.guild.id,
                        )
                    else:
                        ban_date = getLatestBanDate(ctx.guild.id, member.id)
            except discord.NotFound:
                banned_in_discord = False
            except (discord.Forbidden, discord.HTTPException):
                logging.exception(
                    "Não foi possível verificar status de banimento no Discord para usuário %s no servidor %s",
                    member.id,
                    ctx.guild.id,
                )
        ban_records = getBanRecords(ctx.guild.id, member.id)
        profileDescription = generateUserDescription(
            memberProfile,
            guild_member is not None,
            show_approval_status=portaria_funcional,
            ban_date=ban_date,
            banned_in_discord=banned_in_discord,
        )
        embedUserProfile = discord.Embed(
            color=discord.Color.from_str('#febf10'),
            description=profileDescription)
        avatar_url = member.avatar.url if member.avatar else member.default_avatar.url
        embedUserProfile.set_thumbnail(url=avatar_url)
        embedUserProfile.set_author(
            name=(guild_member.display_name if guild_member else member.name)+f' (level {memberProfile.level})',
            icon_url=guild_member.guild_avatar.url if guild_member and guild_member.guild_avatar != None else avatar_url)
        warning_count = len(memberProfile.warnings)
        external_warning_count = 0
        if identity_summary is not None:
            embedUserProfile.add_field(
                name='Contas vinculadas',
                value=str(identity_summary.other_account_count),
                inline=False,
            )
            warning_count = identity_summary.warning_count
            external_warning_count = max(
                0,
                warning_count - len(memberProfile.warnings),
            )
        footer_parts = [f'{warning_count} Warns']
        if len(memberProfile.warnings) > 0:
            last_warning_date = max(
                (warn.date for warn in memberProfile.warnings if warn.date),
                default=None,
            )
            if last_warning_date:
                footer_parts.append(f'Ultimo warn em {last_warning_date.strftime("%d/%m/%Y")}')
        if notes_count > 0:
            footer_parts.append(f'notas: {notes_count}')
        if guild_member and guild_member.is_timed_out():
            footer_parts.append('>> DE CASTIGO <<')
        embedUserProfile.set_footer(text='  -  '.join(footer_parts))
        show_warns_button = warning_count > 0
        show_notes_button = notes_count > 0
        show_bans_button = banned_in_discord or len(ban_records) > 0

        view = ProfileInfoView(
            member,
            memberProfile,
            ctx.user,
            ctx.guild,
            self.bot,
            enable_warns_button=show_warns_button,
            enable_notes_button=show_notes_button,
            enable_bans_button=show_bans_button,
            ban_records=ban_records,
            profile_embed=embedUserProfile,
            external_warning_count=external_warning_count,
        )

        message = await ctx.followup.send(
            embed=embedUserProfile,
            view=view,
            ephemeral=ephemeral,
        )
        view.attach_message(message)

    async def profile_context_menu_callback(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        await self._send_member_profile(interaction, member, ephemeral=True)

    @app_commands.command(
        name='perfil',
        description=(
            'Mostra o perfil de um membro (por padrão é efêmero; '
            'use "privado: falso" para exibir para todos).'
        ),
    )
    async def profile(
        self,
        ctx: discord.Interaction,
        member: discord.User | None = None,
        privado: bool = True,
    ):
        target_member = member or ctx.user
        await self._send_member_profile(ctx, target_member, ephemeral=privado)


    @app_commands.command(name='registrar_usuario', description='Registra um usuário')
    async def registerUser(
        self,
        ctx: discord.Interaction,
        member: discord.User,
        data_aprovacao: str | None = None,
        aniversario: str | None = None,
    ):
        approved_date = verifyDate(data_aprovacao) if data_aprovacao else None
        birthday = verifyDate(aniversario) if aniversario else None
        staff_roles = getStaffRoles(ctx.guild)
        actor_is_staff = any(role in ctx.user.roles for role in staff_roles)
        await ctx.response.send_message(content='Registrando usuário...', ephemeral=True)
        if data_aprovacao and not approved_date:
            return await ctx.edit_original_response(content='Data de aprovação no formato errado! use os formatos dd/MM/YYYY, dd-MM-YYYY, dd.MM.YYYY, dd/MM/YY, dd-MM-YY, dd.MM.YY ou dd/mmm/YYYY (ex: 17/out/1996)')
        if aniversario and not birthday:
            return await ctx.edit_original_response(content='Data de aniversário no formato errado! use os formatos dd/MM/YYYY, dd-MM-YYYY, dd.MM.YYYY, dd/MM/YY, dd-MM-YY, dd.MM.YY ou dd/mmm/YYYY (ex: 17/out/1996)')
        if birthday and not actor_is_staff:
            return await ctx.edit_original_response(
                content='Somente membros da staff podem registrar ou alterar aniversário.'
            )
        try:
            registerUser(ctx.guild.id, member, None, approved_date)

            if birthday is None:
                return await ctx.edit_original_response(content='Membro registrado com sucesso!')

            birthday_result = register_user_informed_birthday(
                ctx.guild.id,
                member,
                birthday,
                approved_date,
                None,
                actor_is_staff,
            )

            if birthday_result["status"] == "created":
                return await ctx.edit_original_response(
                    content='Membro registrado com sucesso! Aniversário incluído com verified = 0.'
                )

            if birthday_result["status"] == "duplicate":
                return await ctx.edit_original_response(content=birthday_result["message"])

            if birthday_result["status"] == "divergent":
                existing_birthday = birthday_result["existing_birthday"]
                informed_birthday = birthday_result["informed_birthday"]
                return await ctx.edit_original_response(
                    content=(
                        "Divergência de aniversário detectada.\n"
                        f"• Registrado: {existing_birthday.day:02}/{existing_birthday.month:02}/{existing_birthday.year}\n"
                        f"• Informado: {informed_birthday.day:02}/{informed_birthday.month:02}/{informed_birthday.year}\n"
                        f"• Tipo: {birthday_result['divergence_type']}\n"
                        f"• Classificação: {birthday_result['risk_level']}"
                    )
                )

            if birthday_result["status"] == "updated_minimal_risk":
                return await ctx.edit_original_response(
                    content='Data de aniversário atualizada (divergência de risco mínimo) pela staff.'
                )

            if birthday_result["status"] == "blocked_no_staff":
                return await ctx.edit_original_response(
                    content='Somente a staff pode alterar data de aniversário já cadastrada.'
                )

            return await ctx.edit_original_response(
                content='Membro registrado, mas não foi possível concluir o processamento do aniversário.'
            )
        except Exception as e:
            return await ctx.edit_original_response(content=f'Erro ao registrar o usuário: {e}')


    """@bot.tree.command(name=f'adm-banir', description=f'Bane um membro do servidor')"""



    @app_commands.command(name=f'warn', description=f'Aplica um warn em um membro')
    async def warn(
        self, ctx: discord.Interaction, membro: discord.User, motivo: str, notificar: bool = True
    ):
        await ctx.response.send_message("Registrando warn...")
        staff_roles = getStaffRoles(ctx.guild)
        if any(role in ctx.user.roles for role in staff_roles):
            warnings = warnMember(ctx.guild.id, membro, motivo, ctx.user)
            if warnings:
                direct_warning_count = warnings["warningsCount"]
                try:
                    warnings["warningsCount"] = await confirmed_warning_count_for_limit(
                        membro.id,
                        getCommunityId(ctx.guild.id),
                    )
                except (IdentityApiError, LookupError, ValueError):
                    logging.exception(
                        "Não foi possível validar o limite agregado de warns para %s na guild %s",
                        membro.id,
                        ctx.guild.id,
                    )
                    warning_limit = int(warnings["warningsLimit"])
                    warn_embed = discord.Embed(
                        title="Warn registrado",
                        description=f"{membro.mention} recebeu um warn.",
                        color=discord.Color.orange(),
                        timestamp=now(),
                    )
                    warn_embed.set_thumbnail(url=membro.display_avatar.url)
                    warn_embed.add_field(
                        name="Membro",
                        value=f"{membro} ({membro.id})",
                        inline=False,
                    )
                    warn_embed.add_field(
                        name="Aplicado por",
                        value=f"{ctx.user} ({ctx.user.mention})",
                        inline=False,
                    )
                    warn_embed.add_field(name="Motivo", value=motivo, inline=False)
                    warn_embed.add_field(
                        name="Total de warns",
                        value=(
                            f"{direct_warning_count} nesta conta; "
                            f"total do cluster confirmado indisponível/{warning_limit}"
                        ),
                        inline=False,
                    )
                    await logWarn(
                        ctx.guild,
                        membro,
                        ctx.user,
                        motivo,
                        direct_warning_count,
                    )
                    notification_note = await self._send_warn_notification(
                        membro,
                        f'Você recebeu um warn por "{motivo}".',
                        enabled=notificar,
                    )
                    return await ctx.edit_original_response(
                        content=(
                            "Warn registrado, mas não foi possível validar o total de warns "
                            "do cluster confirmado. O limite de banimento não foi avaliado; "
                            "a staff deve revisar o caso."
                            f"{notification_note}"
                        ),
                        embed=warn_embed,
                    )

                warn_embed = discord.Embed(
                    title="Warn registrado",
                    description=f"{membro.mention} recebeu um warn.",
                    color=discord.Color.orange(),
                    timestamp=now(),
                )
                warn_embed.set_thumbnail(url=membro.display_avatar.url)
                warn_embed.add_field(
                    name="Membro",
                    value=f"{membro} ({membro.id})",
                    inline=False,
                )
                warn_embed.add_field(
                    name="Aplicado por",
                    value=f"{ctx.user} ({ctx.user.mention})",
                    inline=False,
                )
                warn_embed.add_field(name="Motivo", value=motivo, inline=False)
                warn_embed.add_field(
                    name="Total de warns",
                    value=f"{warnings['warningsCount']}/{warnings['warningsLimit']}",
                    inline=False,
                )
                await logWarn(
                    ctx.guild,
                    membro,
                    ctx.user,
                    motivo,
                    warnings["warningsCount"],
                )
                if warnings["warningsCount"] < (int(warnings["warningsLimit"]) - 1):
                    notification_note = await self._send_warn_notification(
                        membro,
                        f'Você recebeu um warn por "{motivo}", totalizando {warnings["warningsCount"]}! Cuidado com suas ações no servidor!',
                        enabled=notificar,
                    )
                    return await ctx.edit_original_response(
                        content=(
                            f'Warn registrado com sucesso! total de {warnings["warningsCount"]} warns no membro {membro.mention}'
                            f"{notification_note}"
                        ),
                        embed=warn_embed,
                    )
                elif warnings["warningsCount"] < (int(warnings["warningsLimit"])):
                    notification_note = await self._send_warn_notification(
                        membro,
                        f'Você recebeu um warn por "{motivo}", totalizando {warnings["warningsCount"]}! Cuidado, caso receba mais um warn, você será banido do servidor',
                        enabled=notificar,
                    )
                    return await ctx.edit_original_response(
                        content=(
                            f'Warn registrado com sucesso! total de {warnings["warningsCount"]} warns no membro {membro.mention} \nAvise ao membro sobre o risco de banimento!'
                            f"{notification_note}"
                        ),
                        embed=warn_embed,
                    )
                else:
                    notification_note = await self._send_warn_notification(
                        membro,
                        f'Você recebeu um warn por "{motivo}" e atingiu o limite de {warnings["warningsCount"]} warnings do servidor!',
                        enabled=notificar,
                    )
                    return await ctx.edit_original_response(
                        content=(
                            f'Warn registrado com sucesso! \nCom esse warn, o membro {membro.mention} atingiu o limite de warns do servidor e deverá ser **Banido**'
                            f"{notification_note}"
                        ),
                        embed=warn_embed,
                    )
            return await ctx.edit_original_response(
                content=f'Não foi possível aplicar o warn no membro {membro.mention}'
            )
        return await ctx.edit_original_response(
            content='Você não tem permissão para fazer isso'
        )

    @app_commands.command(name='ban', description='Bane um membro do servidor e registra o banimento')
    @app_commands.choices(
        delete_messages=[
            app_commands.Choice(name="nenhuma", value=0),
            app_commands.Choice(name="última hora", value=3600),
            app_commands.Choice(name="últimas 6 horas", value=21600),
            app_commands.Choice(name="últimas 12 horas", value=43200),
            app_commands.Choice(name="últimas 24 horas", value=86400),
            app_commands.Choice(name="3 dias", value=259200),
            app_commands.Choice(name="7 dias", value=604800),
        ]
    )
    @app_commands.describe(
        member='Membro que será banido',
        reason='Motivo do banimento (obrigatório)',
        duration='Duração opcional (ex: 1d, 2s, 3m, 1a)',
        no_appeal='Define se o membro pode recorrer. Padrão: não pode.',
        delete_messages='Mensagens a deletar ao banir. Padrão: nenhuma.',
    )
    async def ban(
        self,
        ctx: discord.Interaction,
        member: discord.User,
        reason: str,
        duration: str | None = None,
        no_appeal: bool = True,
        delete_messages: int = 0,
    ):
        if ctx.user.id == member.id:
            return await ctx.response.send_message(
                content='Você não pode banir a si mesmo.', ephemeral=True
            )

        staff_roles = getStaffRoles(ctx.guild)
        if not any(role in ctx.user.roles for role in staff_roles):
            return await ctx.response.send_message(
                content='Você não tem permissão para fazer isso.',
                ephemeral=True,
            )

        await ctx.response.send_message(content='Processando banimento...', ephemeral=True)

        valid_until: datetime | None = None
        if duration:
            delta = self._parse_duration(duration)
            if delta is None:
                return await ctx.edit_original_response(
                    content='Duração inválida. Use formatos como 1d, 2s, 3m ou 1a.',
                )
            valid_until = now() + delta

        can_appeal = not no_appeal

        try:
            community_id = getCommunityId(ctx.guild.id)
            identity_summary = await get_identity_summary(member.id, community_id)
        except (IdentityApiError, LookupError, ValueError) as error:
            return await ctx.edit_original_response(
                content=(
                    'Não foi possível validar as identidades confirmadas com a API. '
                    'Nenhuma conta foi processada.'
                )
            )

        try:
            validate_confirmed_ban_origin(identity_summary, member.id)
        except ValueError:
            return await ctx.edit_original_response(
                content=(
                    'A API retornou um cluster confirmado inconsistente. '
                    'Nenhuma conta foi processada.'
                )
            )

        ban_id = registerUserBan(
            ctx.guild.id,
            member,
            reason,
            ctx.user,
            valid_until=valid_until,
            can_appeal=can_appeal,
        )
        if not ban_id:
            return await ctx.edit_original_response(
                content=(
                    'Não foi possível registrar o banimento antes de aplicar efeitos no Discord. '
                    'Nenhuma conta foi processada.'
                )
            )

        try:
            effects = await propagate_confirmed_ban(
                ctx.guild,
                member.id,
                identity_summary,
                reason=reason,
                delete_message_seconds=delete_messages,
            )
        except asyncio.CancelledError:
            removeBanRecord(ban_id)
            raise
        except Exception:
            logging.exception(
                'Falha inesperada durante propagação do ban: guild=%s ban_id=%s',
                ctx.guild.id,
                ban_id,
            )
            removeBanRecord(ban_id)
            return await ctx.edit_original_response(
                content=(
                    'O banimento foi cancelado por uma falha inesperada antes de concluir '
                    'a propagação. O registro administrativo foi revertido.'
                )
            )

        origin_effect = next((effect for effect in effects if effect.is_origin), None)
        origin_succeeded = (
            origin_effect is not None
            and origin_effect.outcome in {'APPLIED', 'ALREADY_BANNED'}
        )

        effects_registered = recordBanDiscordEffects(ban_id, effects)
        if not effects_registered:
            compensation_failed = await compensate_unrecorded_propagated_bans(
                ctx.guild,
                effects,
            )
            if compensation_failed:
                logging.critical(
                    'Falha ao compensar bans propagados sem ledger: guild=%s ban_id=%s discord_ids=%s',
                    ctx.guild.id,
                    ban_id,
                    compensation_failed,
                )
            if (
                origin_effect is None
                or origin_effect.outcome != 'APPLIED'
            ):
                # Without a durable ledger we cannot safely own/reverse a
                # pre-existing ban. Keep only a newly APPLIED origin action.
                removeBanRecord(ban_id)
            return await ctx.edit_original_response(
                content=(
                    'O banimento da conta de origem foi preservado quando aplicável, '
                    'mas a propagação não pôde ser registrada com segurança. '
                    'As contas vinculadas aplicadas foram revertidas quando possível; '
                    'entre em contato com a equipe técnica.'
                )
            )

        if not origin_succeeded:
            removeBanRecord(ban_id)
            return await ctx.edit_original_response(
                content=(
                    'Não foi possível aplicar o banimento à conta de origem. '
                    'Nenhuma conta vinculada foi processada.'
                )
            )

        await logBan(
            ctx.guild,
            member,
            ctx.user,
            reason=reason,
            valid_until=valid_until,
            can_appeal=can_appeal,
            propagated_effects=summarize_ban_effects(effects),
        )

        outcomes = summarize_ban_effects(effects)
        message_lines = [
            f'Banimento processado para {member.mention}.',
            f'Motivo: {reason}',
            (
                'Efeitos confirmados: '
                f'{outcomes.get("APPLIED", 0)} aplicado(s), '
                f'{outcomes.get("ALREADY_BANNED", 0)} já banido(s), '
                f'{sum(outcomes.get(key, 0) for key in ("NOT_FOUND", "FORBIDDEN", "FAILED"))} falha(s).'
            ),
        ]
        if valid_until:
            message_lines.append(
                f'Válido até: {valid_until.strftime("%d/%m/%Y %H:%M:%S")}'
            )
        message_lines.append('Recurso permitido.' if can_appeal else 'Recurso não permitido.')

        return await ctx.edit_original_response(content='\n'.join(message_lines))

    @app_commands.command(name=f'notas_add', description=f'Adiciona uma nota a um membro (apenas staff)')
    async def addNote(self, ctx: discord.Interaction, member: discord.User, nota: str):
        staff_roles = getStaffRoles(ctx.guild)
        if not any(role in ctx.user.roles for role in staff_roles):
            return await ctx.response.send_message(
                content='Você não tem permissão para adicionar notas a este membro.',
                ephemeral=True,
            )

        nota = nota.strip()
        if not nota:
            return await ctx.response.send_message(
                content='A nota não pode ser vazia.',
                ephemeral=True,
            )

        note_id = addMemberNote(ctx.guild.id, member, nota, ctx.user)
        if note_id is None:
            return await ctx.response.send_message(
                content='Não foi possível adicionar a nota. Tente novamente mais tarde.',
                ephemeral=True,
            )

        return await ctx.response.send_message(
            content=(
                f'Nota adicionada para {member.mention} com sucesso. ' f'(ID: {note_id})'
            ),
            ephemeral=True,
        )

    @app_commands.command(name=f'portaria_aprovar', description=f'Aprova um membro que está esperando aprovação na portaria')
    async def approvePortaria(self, ctx: discord.Interaction, member: discord.Member, data_nascimento: str=None):
        staff_roles = getStaffRoles(ctx.guild)
        if not any(role in ctx.user.roles for role in staff_roles):
            return await ctx.response.send_message(
                content='Você não tem permissão para fazer isso.',
                ephemeral=True,
            )

        try:
            portaria_config = get_portaria_base_config(ctx.guild.id)
            carteirinhaProvisoria = ctx.guild.get_role(
                portaria_config.get("acesso_provisorio_role_id")
            )
            cargoVisitante = ctx.guild.get_role(portaria_config.get("visitante_role_id"))
            cargoMaior18 = ctx.guild.get_role(portaria_config.get("maior_18_role_id"))
            cargoMenor18 = ctx.guild.get_role(portaria_config.get("menor_18_role_id"))
            acesso_provisorio_ativo = bool(portaria_config.get("aprovacao_acesso_provisorio_ativo"))
            acesso_provisorio_duracao_dias = int(portaria_config.get("aprovacao_acesso_provisorio_duracao_dias") or 15)

            missing_items = []
            if carteirinhaProvisoria is None:
                missing_items.append("cargo acesso_provisorio")
            if cargoVisitante is None:
                missing_items.append("cargo visitante")
            if cargoMaior18 is None:
                missing_items.append("cargo maior_18")
            if acesso_provisorio_ativo and acesso_provisorio_duracao_dias <= 0:
                missing_items.append("duração de acesso provisório (dias > 0)")

            if missing_items:
                return await ctx.response.send_message(
                    content=(
                        'Configuração da portaria incompleta para concluir a aprovação. '
                        f'Faltando: {", ".join(missing_items)}. '
                        'Configure com /admin cargos setar.'
                    ),
                    ephemeral=True,
                )

            channel = ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None
            if channel is None:
                return await ctx.response.send_message(
                    content='Não foi possível identificar um canal de texto para concluir a aprovação.',
                    ephemeral=True,
                )

            if (cargoVisitante in member.roles and carteirinhaProvisoria in member.roles) or (cargoVisitante not in member.roles):
                return await ctx.response.send_message(content=f'O membro <@{member.id}> ja foi aprovado!', ephemeral=True)

            conta_dias = (now().date() - member.created_at.date()).days
            idade_minima_prov_ativa = bool(portaria_config.get("idade_minima_conta_acesso_provisorio_ativa"))
            idade_minima_prov_dias = int(portaria_config.get("idade_minima_conta_acesso_provisorio_dias") or 30)
            if idade_minima_prov_ativa and idade_minima_prov_dias > 0:
                is_new_account = conta_dias < idade_minima_prov_dias
            else:
                is_new_account = conta_dias < 30

            account_release_override = get_portaria_account_release_override(ctx.guild.id, member.id)

            channel_name = channel.name.casefold()
            had_provisoria_ticket = "provisória" in channel_name
            if had_provisoria_ticket and carteirinhaProvisoria not in member.roles:
                is_new_account = False

            invite_link_used = get_user_community_status_invite_link(ctx.guild.id, member)
            bypass_invites = {
                code.casefold() for code in get_portaria_invite_bypass_codes(ctx.guild.id)
            }
            if invite_link_used:
                invite_code = normalize_discord_invite_code(invite_link_used)
                if invite_code in bypass_invites:
                    is_new_account = False

            if account_release_override:
                is_new_account = (
                    str(account_release_override.get("access_mode") or "").casefold() == "provisorio"
                )

            async for message in channel.history(limit=1, oldest_first=True):
                if data_nascimento:
                    matchEmbedded = BIRTHDAY_REGEX.search(data_nascimento)
                    if not matchEmbedded:
                        return await ctx.response.send_message(content=f'Você digitou uma data inválida: {data_nascimento}', ephemeral=True)
                else:
                    matchEmbedded = None
                    if len(message.embeds) > 1 and isinstance(message.embeds[1].description, str):
                        matchEmbedded = BIRTHDAY_REGEX.search(message.embeds[1].description)

                if matchEmbedded:
                    await ctx.response.send_message(content='registrando usuario...', ephemeral=True)
                    try:
                        day = int(matchEmbedded.group(1).replace(" ", ""))
                        month_value = matchEmbedded.group(2).replace(" ", "")
                        month = int(month_value if month_value.isdigit() else MONTHS.index(month_value))
                        year = int(matchEmbedded.group(3).replace(" ", ""))
                        if len(str(year)) <= 2:
                            year += 2000 if year < (now().date().year - 2000) else 1900
                        birthday = datetime(year, month, day)
                        if birthday.year > 1975 and birthday.year < now().year:
                            age = (datetime.now().date() - birthday.date()).days
                            existing_birthday = getUserBirthday(ctx.guild.id, member)
                            if isinstance(existing_birthday, datetime):
                                existing_birthday = existing_birthday.date()
                            age_in_days = age
                            if (
                                existing_birthday
                                and existing_birthday != birthday.date()
                            ):
                                view = BirthdayConflictView(
                                    self,
                                    ctx.user.id,
                                    member,
                                    birthday.date(),
                                    existing_birthday,
                                    age_in_days,
                                    channel,
                                    carteirinhaProvisoria,
                                    cargoVisitante,
                                    cargoMaior18,
                                    cargoMenor18,
                                    acesso_provisorio_ativo,
                                    acesso_provisorio_duracao_dias,
                                    is_new_account,
                                )
                                await ctx.edit_original_response(
                                    content=(
                                        f"existe outro registro de aniversário no banco: {existing_birthday.strftime('%d/%m/%Y')}. "
                                        "Deseja manter ou substituir pelo atual?"
                                    ),
                                    view=view,
                                )
                                return
                            await self._finalize_portaria_approval(
                                ctx,
                                member,
                                birthday.date(),
                                age_in_days,
                                channel,
                                carteirinhaProvisoria,
                                cargoVisitante,
                                cargoMaior18,
                                cargoMenor18,
                                acesso_provisorio_ativo,
                                acesso_provisorio_duracao_dias,
                                is_new_account,
                            )
                            return
                        else:
                            return await ctx.edit_original_response(content=f'Data inválida encontrada: {matchEmbedded.group(0)}\nO membro tem {relativedelta(now().date(), birthday.date()).years} anos?')
                    except ValueError:
                        return await ctx.edit_original_response(content=f'Data inválida encontrada: {matchEmbedded.group(0)}')
            return await ctx.response.send_message(content=f'Não foi possível encontrar a data de nascimento do membro <@{member.id}> na portaria\nEm ultimo caso, digite a data de nascimento nos argumentos do comando.', ephemeral=True)
        except Exception as error:
            await self._notify_portaria_error(
                ctx,
                error,
                use_followup=ctx.response.is_done(),
                command_params={
                    "member_id": getattr(member, "id", "unknown"),
                    "data_nascimento": data_nascimento,
                    "channel_id": getattr(ctx.channel, "id", "unknown"),
                },
            )

    @app_commands.command(
        name='portaria_liberar_conta',
        description='Libera manualmente uma conta para passar pelas travas da portaria.',
    )
    @app_commands.describe(
        member='Membro que será liberado',
        tipo_acesso='Define como ficará o acesso do membro após aprovação',
        precisa_ficha='Define se o membro ainda precisará abrir ficha na portaria',
    )
    async def releasePortariaAccount(
        self,
        ctx: discord.Interaction,
        member: discord.Member,
        tipo_acesso: Literal["provisorio", "completo"],
        precisa_ficha: Literal["sim", "nao"] = "sim",
    ):
        staff_roles = getStaffRoles(ctx.guild)
        if not any(role in ctx.user.roles for role in staff_roles):
            return await ctx.response.send_message(
                content='Você não tem permissão para fazer isso.',
                ephemeral=True,
            )

        return await ctx.response.send_message(
            content=(
                'Bypasses por conta são administrados pelo painel em '
                '**Moderação & Acesso → Bypasses da Portaria**.'
            ),
            ephemeral=True,
        )

    
    
async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
