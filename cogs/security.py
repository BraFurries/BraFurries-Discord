from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from core.database import (
    getStaffRoles as getStaffRoleIds,
    markBanDiscordEffectReverted,
    is_sensitive_permission_whitelisted,
    list_sensitive_permission_whitelist,
    register_sensitive_permission_whitelist,
    remove_sensitive_permission_whitelist,
)
from core.membership_presence import begin_identity_unban, record_unban
from core.discord_events import getStaffRoles


logger = logging.getLogger(__name__)


@dataclass
class PendingSecurityAction:
    guild_id: int
    description: str
    actor_id: int | None
    created_at: datetime
    confirm_callback: Callable[[], Awaitable[None]]


class OwnerConfirmationView(discord.ui.View):
    def __init__(self, cog: "SecurityCog", action_id: int, description: str):
        super().__init__(timeout=3600)
        self.cog = cog
        self.action_id = action_id
        self.description = description
        self.message: discord.Message | None = None

    def _resolved_embed(self, title: str, message: str, color: discord.Color) -> discord.Embed:
        return discord.Embed(
            title=title,
            color=color,
            description=f"Ação: **{self.description}**\n\n{message}\n\nEsta confirmação não aceita mais interações.",
            timestamp=discord.utils.utcnow(),
        )

    async def _finish_interaction(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        message: str,
        color: discord.Color,
    ) -> None:
        await interaction.response.edit_message(
            embed=self._resolved_embed(title, message, color),
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        ok, message = await self.cog.confirm_pending_action(self.action_id, interaction.user)
        if self.action_id in self.cog.pending_actions:
            return await interaction.response.send_message(message, ephemeral=True)

        await self._finish_interaction(
            interaction,
            title="✅ Ação de segurança confirmada" if ok else "⚠️ Confirmação encerrada",
            message=message,
            color=discord.Color.green() if ok else discord.Color.orange(),
        )

    @discord.ui.button(label="Recusar", style=discord.ButtonStyle.secondary)
    async def refuse(self, interaction: discord.Interaction, _: discord.ui.Button):
        ok, message = self.cog.reject_pending_action(self.action_id, interaction.user)
        if not ok and self.action_id in self.cog.pending_actions:
            return await interaction.response.send_message(message, ephemeral=True)

        await self._finish_interaction(
            interaction,
            title="🛡️ Ação de segurança recusada" if ok else "⚠️ Confirmação encerrada",
            message=message,
            color=discord.Color.red() if ok else discord.Color.orange(),
        )

    async def on_timeout(self) -> None:
        if not self.cog.expire_pending_action(self.action_id):
            return

        if self.message is not None:
            try:
                await self.message.edit(
                    embed=self._resolved_embed(
                        "⌛ Confirmação de segurança expirada",
                        "O prazo de confirmação terminou. A ação preventiva do bot foi mantida.",
                        discord.Color.dark_grey(),
                    ),
                    view=None,
                )
            except discord.HTTPException:
                pass


class WhitelistFromAlertView(discord.ui.View):
    def __init__(
        self,
        cog: "SecurityCog",
        guild_id: int,
        actor_id: int,
        actor_type: str,
        permission_name: str,
    ):
        super().__init__(timeout=3600)
        self.cog = cog
        self.guild_id = guild_id
        self.actor_id = actor_id
        self.actor_type = actor_type
        self.permission_name = permission_name

    @discord.ui.button(label="Autorizar este membro", style=discord.ButtonStyle.primary)
    async def authorize(self, interaction: discord.Interaction, _: discord.ui.Button):
        guild = self.cog.bot.get_guild(self.guild_id)
        if guild is None:
            return await interaction.response.send_message("Servidor não encontrado.", ephemeral=True)

        if interaction.user.id != guild.owner_id:
            return await interaction.response.send_message(
                "Somente o dono do servidor pode autorizar membros na whitelist.",
                ephemeral=True,
            )

        if self.permission_name == "administrator":
            return await interaction.response.send_message(
                "A permissão `administrator` não pode ser adicionada à whitelist.",
                ephemeral=True,
            )

        created = register_sensitive_permission_whitelist(
            self.guild_id,
            self.actor_id,
            self.actor_type,
            self.permission_name,
        )
        if not created:
            return await interaction.response.send_message(
                "Esse membro já estava autorizado para essa permissão.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"Autorizado com sucesso: <@{self.actor_id}> para `{self.permission_name}`.",
            ephemeral=True,
        )
        self.stop()


class SecurityCog(commands.Cog):
    security = app_commands.Group(name="security", description="Comandos de segurança")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pending_actions: dict[int, PendingSecurityAction] = {}
        self._next_action_id = 1
        self._ignored_role_deletes: set[int] = set()

    def _is_staff(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True
        staff_roles = getStaffRoles(member.guild)
        return any(role in member.roles for role in staff_roles)

    async def _get_relevant_audit_entry(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: int | None = None,
        window_seconds: int = 20,
    ) -> discord.AuditLogEntry | None:
        now = discord.utils.utcnow()
        try:
            async for entry in guild.audit_logs(limit=10, action=action):
                if now - entry.created_at > timedelta(seconds=window_seconds):
                    continue
                if target_id is not None and getattr(getattr(entry, "target", None), "id", None) != target_id:
                    continue
                return entry
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    async def _alert_owner(
        self,
        guild: discord.Guild,
        *,
        action: str,
        actor: discord.Member,
        permission_name: str,
        removed_roles: list[discord.Role],
        failed_roles: list[discord.Role],
        removed_sensitive_permissions: list[str],
        failed_permission_removals: list[str],
    ):
        owner = guild.owner or await guild.fetch_member(guild.owner_id)
        if owner is None:
            return

        roles_text = ", ".join(role.mention for role in removed_roles) if removed_roles else "Nenhum"
        failed_roles_text = ", ".join(role.mention for role in failed_roles) if failed_roles else "Nenhum"
        removed_permissions_text = ", ".join(removed_sensitive_permissions) if removed_sensitive_permissions else "Nenhuma"
        failed_permissions_text = ", ".join(failed_permission_removals) if failed_permission_removals else "Nenhuma"
        actor_text = f"{actor.mention} (`{actor.id}`)"
        actor_type = "bot" if actor.bot else "user"

        embed = discord.Embed(
            title="⚠️ Alerta de Segurança",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
            description=(
                "Uma ação sensível foi detectada e não autorizada. "
                "Use o botão abaixo para autorizar este membro especificamente para essa permissão."
            ),
        )
        embed.add_field(name="Servidor", value=f"{guild.name} (`{guild.id}`)", inline=False)
        embed.add_field(name="Autor", value=actor_text, inline=False)
        embed.add_field(name="Tipo", value=actor_type, inline=True)
        embed.add_field(name="Permissão sensível", value=permission_name, inline=True)
        embed.add_field(name="Ação", value=action, inline=False)
        embed.add_field(name="Cargos removidos", value=roles_text, inline=False)
        embed.add_field(name="Cargos NÃO removidos", value=failed_roles_text, inline=False)
        embed.add_field(name="Permissões sensíveis removidas", value=removed_permissions_text, inline=False)
        embed.add_field(name="Permissões sensíveis NÃO removidas", value=failed_permissions_text, inline=False)

        view = WhitelistFromAlertView(
            self,
            guild.id,
            actor.id,
            actor_type,
            permission_name,
        )
        try:
            await owner.send(embed=embed, view=view)
        except discord.HTTPException:
            pass

    async def _remove_roles_with_permission(
        self,
        member: discord.Member,
        permission_name: str,
        *,
        reason: str,
        strip_permissions_from_roles: bool,
    ) -> tuple[list[discord.Role], list[discord.Role], list[str], list[str]]:
        removed_roles: list[discord.Role] = []
        failed_roles: list[discord.Role] = []
        removed_sensitive_permissions: set[str] = set()
        failed_permission_removals: set[str] = set()

        roles_to_check = [
            role
            for role in member.roles
            if role != member.guild.default_role
            and (
                role.permissions.administrator
                or getattr(role.permissions, permission_name, False)
            )
        ]

        if not strip_permissions_from_roles:
            for role in roles_to_check:
                try:
                    await member.remove_roles(role, reason=reason)
                    removed_roles.append(role)
                except discord.HTTPException:
                    failed_roles.append(role)
            return removed_roles, failed_roles, [], []

        for role in roles_to_check:
            cleaned_permissions = discord.Permissions(role.permissions.value)
            changed = False

            if cleaned_permissions.administrator:
                cleaned_permissions.administrator = False
                changed = True

            if getattr(cleaned_permissions, permission_name, False):
                setattr(cleaned_permissions, permission_name, False)
                changed = True

            if changed:
                try:
                    await role.edit(permissions=cleaned_permissions, reason=reason)
                    if role.permissions.administrator:
                        removed_sensitive_permissions.add("administrator")
                    if getattr(role.permissions, permission_name, False):
                        removed_sensitive_permissions.add(permission_name)
                    continue
                except discord.HTTPException:
                    if role.permissions.administrator:
                        failed_permission_removals.add("administrator")
                    if getattr(role.permissions, permission_name, False):
                        failed_permission_removals.add(permission_name)

            failed_roles.append(role)

        return (
            removed_roles,
            failed_roles,
            sorted(removed_sensitive_permissions),
            sorted(failed_permission_removals),
        )

    async def _is_sensitive_action_authorized(
        self,
        guild: discord.Guild,
        audit_entry: discord.AuditLogEntry | None,
        permission_name: str,
    ) -> bool:
        if audit_entry is None:
            return False

        actor = audit_entry.user
        if not isinstance(actor, (discord.Member, discord.User)):
            return False
        if actor.id == guild.owner_id:
            return True

        member = guild.get_member(actor.id)
        if member is None:
            try:
                member = await guild.fetch_member(actor.id)
            except discord.HTTPException:
                return False

        if self.bot.user and member.id == self.bot.user.id:
            return True
        if self._is_staff(member):
            return True

        actor_type = "bot" if member.bot else "user"
        return is_sensitive_permission_whitelisted(
            guild.id,
            member.id,
            actor_type,
            permission_name,
        )

    async def _handle_unauthorized_sensitive_action(
        self,
        guild: discord.Guild,
        audit_entry: discord.AuditLogEntry | None,
        *,
        action_label: str,
        permission_name: str,
    ):
        if audit_entry is None:
            return

        actor = audit_entry.user
        if not isinstance(actor, (discord.Member, discord.User)):
            return
        if actor.id == guild.owner_id:
            return

        member = guild.get_member(actor.id)
        if member is None:
            try:
                member = await guild.fetch_member(actor.id)
            except discord.HTTPException:
                return

        if self.bot.user and member.id == self.bot.user.id:
            return

        if member.bot or not self._is_staff(member):
            actor_type = "bot" if member.bot else "user"
            if is_sensitive_permission_whitelisted(guild.id, member.id, actor_type, permission_name):
                return

            removed_roles, failed_roles, removed_permissions, failed_permission_removals = await self._remove_roles_with_permission(
                member,
                permission_name,
                reason=f"Ação sensível não autorizada detectada: {action_label}",
                strip_permissions_from_roles=member.bot,
            )
            await self._alert_owner(
                guild,
                action=action_label,
                actor=member,
                permission_name=permission_name,
                removed_roles=removed_roles,
                failed_roles=failed_roles,
                removed_sensitive_permissions=removed_permissions,
                failed_permission_removals=failed_permission_removals,
            )

    async def _register_owner_confirmation(
        self,
        guild: discord.Guild,
        actor: discord.abc.User | None,
        description: str,
        confirm_callback: Callable[[], Awaitable[None]],
    ):
        owner = guild.owner or await guild.fetch_member(guild.owner_id)
        if owner is None:
            return

        action_id = self._next_action_id
        self._next_action_id += 1
        self.pending_actions[action_id] = PendingSecurityAction(
            guild_id=guild.id,
            description=description,
            actor_id=getattr(actor, "id", None),
            created_at=datetime.utcnow(),
            confirm_callback=confirm_callback,
        )

        embed = discord.Embed(
            title="✅ Confirmação de segurança necessária",
            color=discord.Color.orange(),
            description=(
                f"Ação: **{description}**\n"
                f"Autor: {actor.mention if actor else 'Desconhecido'}\n"
                f"ID da ação: `{action_id}`\n\n"
                "A alteração foi revertida temporariamente e só será aplicada após sua confirmação."
            ),
            timestamp=discord.utils.utcnow(),
        )
        view = OwnerConfirmationView(self, action_id, description)
        try:
            view.message = await owner.send(embed=embed, view=view)
        except discord.HTTPException:
            self.pending_actions.pop(action_id, None)

    async def confirm_pending_action(self, action_id: int, user: discord.abc.User) -> tuple[bool, str]:
        action = self.pending_actions.get(action_id)
        if not action:
            return False, "Ação não encontrada ou já processada."

        guild = self.bot.get_guild(action.guild_id)
        if guild is None:
            self.pending_actions.pop(action_id, None)
            return False, "Servidor não encontrado para essa ação."

        if user.id != guild.owner_id:
            return False, "Somente o dono do servidor pode confirmar esta ação."

        # Claim the action before awaiting Discord so a timeout or second click
        # cannot process the same sensitive operation concurrently.
        self.pending_actions.pop(action_id, None)
        try:
            await action.confirm_callback()
        except discord.HTTPException as error:
            return False, f"Falha ao confirmar ação: {error}"

        return True, "Ação confirmada e aplicada com sucesso."

    def reject_pending_action(self, action_id: int, user: discord.abc.User) -> tuple[bool, str]:
        action = self.pending_actions.get(action_id)
        if not action:
            return False, "Ação não encontrada ou já processada."

        guild = self.bot.get_guild(action.guild_id)
        if guild is None:
            self.pending_actions.pop(action_id, None)
            return False, "Servidor não encontrado para essa ação."

        if user.id != guild.owner_id:
            return False, "Somente o dono do servidor pode recusar esta ação."

        self.pending_actions.pop(action_id, None)
        return True, "A ação foi recusada. A medida preventiva aplicada pelo bot foi mantida."

    def expire_pending_action(self, action_id: int) -> bool:
        """Expire an unresolved action, preserving the preventive bot change."""
        return self.pending_actions.pop(action_id, None) is not None

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        entry = await self._get_relevant_audit_entry(guild, discord.AuditLogAction.ban, user.id)
        await self._handle_unauthorized_sensitive_action(
            guild,
            entry,
            action_label=f"Aplicação de ban em {user.mention}",
            permission_name="ban_members",
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        entry = None
        for attempt in range(3):
            entry = await self._get_relevant_audit_entry(
                guild,
                discord.AuditLogAction.unban,
                user.id,
            )
            if entry is not None:
                break
            if attempt < 2:
                await asyncio.sleep(0.5)
        authorized = await self._is_sensitive_action_authorized(
            guild,
            entry,
            "ban_members",
        )
        if not authorized:
            try:
                await guild.ban(
                    discord.Object(id=user.id),
                    reason="Reversão automática de unban não autorizado",
                    delete_message_seconds=0,
                )
            except discord.HTTPException:
                logger.exception(
                    "Falha ao restaurar ban após unban não autorizado de %s na guild %s",
                    user.id,
                    guild.id,
                )
            await self._handle_unauthorized_sensitive_action(
                guild,
                entry,
                action_label=f"Retirada de ban de {user.mention}",
                permission_name="ban_members",
            )
            return

        try:
            actor_id = entry.user.id if entry and entry.user else None
            reason = entry.reason if entry and entry.reason else None
            sibling_effects = begin_identity_unban(
                guild.id,
                user.id,
                actor_id,
                reason,
            )
            if sibling_effects is None:
                record_unban(guild.id, user.id, actor_id, reason)
            else:
                for sibling in sibling_effects:
                    sibling_id = int(sibling["discord_user_id"])
                    effect_ids = [
                        int(effect_id)
                        for effect_id in sibling["effect_ids"]
                    ]
                    try:
                        await guild.unban(
                            discord.Object(id=sibling_id),
                            reason=(
                                "Unban propagado por identidade confirmada; "
                                f"origem Discord {user.id}"
                            ),
                        )
                    except discord.NotFound:
                        outcome = "ALREADY_UNBANNED"
                        error_code = "NotFound"
                    except discord.Forbidden as error:
                        outcome = "FORBIDDEN"
                        error_code = type(error).__name__
                    except Exception as error:
                        outcome = "FAILED"
                        error_code = type(error).__name__
                    else:
                        outcome = "REVERTED"
                        error_code = None

                    for effect_id in effect_ids:
                        markBanDiscordEffectReverted(
                            effect_id,
                            outcome,
                            error_code,
                        )

                    if outcome in {"FORBIDDEN", "FAILED"}:
                        logger.error(
                            "Falha ao propagar unban de identidade para Discord %s na guild %s: %s",
                            sibling_id,
                            guild.id,
                            outcome,
                        )
        except Exception:
            logger.exception(
                "Falha ao registrar/propagar revogação de ban para %s",
                user.id,
            )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        if role.id in self._ignored_role_deletes:
            self._ignored_role_deletes.discard(role.id)
            return

        entry = await self._get_relevant_audit_entry(role.guild, discord.AuditLogAction.role_delete, role.id)
        if role.id in set(getStaffRoleIds(role.guild.id)):
            await self._handle_unauthorized_sensitive_action(
                role.guild,
                entry,
                action_label=f"Deleção de cargo de staff: {role.name}",
                permission_name="manage_roles",
            )
            return

        await self._handle_unauthorized_sensitive_action(
            role.guild,
            entry,
            action_label=f"Deleção de cargo: {role.name}",
            permission_name="manage_roles",
        )

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        entry = await self._get_relevant_audit_entry(role.guild, discord.AuditLogAction.role_create, role.id)
        await self._handle_unauthorized_sensitive_action(
            role.guild,
            entry,
            action_label=f"Criação de cargo: {role.name}",
            permission_name="manage_roles",
        )

        if (
            role.permissions.administrator
            and entry
            and entry.user
            and (not self.bot.user or entry.user.id != self.bot.user.id)
        ):
            self._ignored_role_deletes.add(role.id)
            snapshot = {
                "name": role.name,
                "permissions": role.permissions,
                "color": role.color,
                "hoist": role.hoist,
                "mentionable": role.mentionable,
            }
            await role.delete(reason="Aguardando confirmação do dono para cargo administrador")

            async def confirm():
                await role.guild.create_role(reason="Confirmação do dono", **snapshot)

            await self._register_owner_confirmation(
                role.guild,
                entry.user,
                f"Criar cargo administrador `{snapshot['name']}`",
                confirm,
            )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if before.permissions.administrator or not after.permissions.administrator:
            return

        entry = await self._get_relevant_audit_entry(after.guild, discord.AuditLogAction.role_update, after.id)
        if not entry or not entry.user:
            return
        if self.bot.user and entry.user.id == self.bot.user.id:
            return

        cleaned_permissions = discord.Permissions(after.permissions.value)
        cleaned_permissions.administrator = False
        await after.edit(
            permissions=cleaned_permissions,
            reason="Aguardando confirmação do dono para permissão de administrador",
        )

        async def confirm():
            confirmed_permissions = discord.Permissions(cleaned_permissions.value)
            confirmed_permissions.administrator = True
            await after.edit(permissions=confirmed_permissions, reason="Confirmação do dono")

        await self._register_owner_confirmation(
            after.guild,
            entry.user,
            f"Adicionar permissão de administrador ao cargo `{after.name}`",
            confirm,
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        added_roles = [role for role in after.roles if role not in before.roles]
        admin_roles = [role for role in added_roles if role.permissions.administrator]
        if not admin_roles:
            return

        entry = await self._get_relevant_audit_entry(after.guild, discord.AuditLogAction.member_role_update, after.id)
        if not entry or not entry.user:
            return
        if self.bot.user and entry.user.id == self.bot.user.id:
            return

        try:
            await after.remove_roles(
                *admin_roles,
                reason="Aguardando confirmação do dono para atribuição de cargo administrador",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(
                "Não foi possível remover preventivamente cargos administradores: guild=%s (%s) member=%s (%s) roles=%s",
                getattr(after.guild, "name", None),
                getattr(after.guild, "id", None),
                getattr(after, "display_name", None) or getattr(after, "name", None),
                getattr(after, "id", None),
                ", ".join(f"{role.name} ({role.id})" for role in admin_roles),
                exc_info=True,
            )
            return

        async def confirm():
            await after.add_roles(*admin_roles, reason="Confirmação do dono")

        await self._register_owner_confirmation(
            after.guild,
            entry.user,
            f"Atribuir cargos administrativos para {after.mention}: {', '.join(role.name for role in admin_roles)}",
            confirm,
        )

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        entry = await self._get_relevant_audit_entry(channel.guild, discord.AuditLogAction.channel_create, channel.id)
        await self._handle_unauthorized_sensitive_action(
            channel.guild,
            entry,
            action_label=f"Criação de canal: {channel.name}",
            permission_name="manage_channels",
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        entry = await self._get_relevant_audit_entry(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
        await self._handle_unauthorized_sensitive_action(
            channel.guild,
            entry,
            action_label=f"Deleção de canal: {channel.name}",
            permission_name="manage_channels",
        )

    @security.command(name="whitelist_add", description="Autoriza uma permissão sensível para um membro")
    @app_commands.describe(membro="Membro a ser autorizado", permissao="Permissão sensível")
    async def whitelist_add(self, ctx: discord.Interaction, membro: discord.Member, permissao: str):
        if ctx.guild is None or ctx.user.id != ctx.guild.owner_id:
            return await ctx.response.send_message(
                "Somente o dono do servidor pode gerenciar a whitelist.",
                ephemeral=True,
            )

        permission_name = permissao.strip().lower()
        if permission_name == "administrator":
            return await ctx.response.send_message(
                "A permissão `administrator` não pode ser adicionada à whitelist e é sempre validada.",
                ephemeral=True,
            )

        allowed_permissions = {"ban_members", "manage_roles", "manage_channels"}
        if permission_name not in allowed_permissions:
            return await ctx.response.send_message(
                f"Permissão inválida. Use uma destas: {', '.join(sorted(allowed_permissions))}",
                ephemeral=True,
            )

        actor_type = "bot" if membro.bot else "user"
        created = register_sensitive_permission_whitelist(ctx.guild.id, membro.id, actor_type, permission_name)
        if not created:
            return await ctx.response.send_message(
                "Essa permissão já estava na whitelist desse membro.",
                ephemeral=True,
            )

        await ctx.response.send_message(
            f"Whitelist registrada: {membro.mention} ({actor_type}) agora pode usar `{permission_name}` sem punição automática.",
            ephemeral=True,
        )

    @security.command(name="whitelist_remove", description="Remove uma permissão sensível da whitelist")
    @app_commands.describe(membro="Membro da whitelist", permissao="Permissão sensível")
    async def whitelist_remove(self, ctx: discord.Interaction, membro: discord.Member, permissao: str):
        if ctx.guild is None or ctx.user.id != ctx.guild.owner_id:
            return await ctx.response.send_message(
                "Somente o dono do servidor pode gerenciar a whitelist.",
                ephemeral=True,
            )

        permission_name = permissao.strip().lower()
        actor_type = "bot" if membro.bot else "user"
        removed = remove_sensitive_permission_whitelist(ctx.guild.id, membro.id, actor_type, permission_name)
        if not removed:
            return await ctx.response.send_message("Entrada não encontrada na whitelist.", ephemeral=True)

        await ctx.response.send_message(
            f"Whitelist removida: {membro.mention} ({actor_type}) não está mais autorizado para `{permission_name}`.",
            ephemeral=True,
        )

    @security.command(name="whitelist_list", description="Lista permissões sensíveis autorizadas na whitelist")
    @app_commands.describe(membro="Membro específico (opcional)")
    async def whitelist_list(self, ctx: discord.Interaction, membro: discord.Member | None = None):
        if ctx.guild is None or ctx.user.id != ctx.guild.owner_id:
            return await ctx.response.send_message(
                "Somente o dono do servidor pode gerenciar a whitelist.",
                ephemeral=True,
            )

        entries = list_sensitive_permission_whitelist(ctx.guild.id, membro.id if membro else None, None)
        if not entries:
            return await ctx.response.send_message("Não há entradas na whitelist.", ephemeral=True)

        grouped: dict[tuple[int, str], list[str]] = {}
        for entry in entries:
            key = (entry["actor_id"], entry["actor_type"])
            grouped.setdefault(key, []).append(entry["permission_name"])

        lines = []
        for (actor_id, actor_type), permissions in grouped.items():
            member = ctx.guild.get_member(actor_id)
            label = member.mention if member else f"<@{actor_id}>"
            lines.append(f"{label} ({actor_type}): {', '.join(sorted(permissions))}")

        await ctx.response.send_message("\n".join(lines), ephemeral=True)

    @security.command(name="pendencias", description="Lista pendências de confirmação de segurança")
    async def pending(self, ctx: discord.Interaction):
        if ctx.guild is None or ctx.user.id != ctx.guild.owner_id:
            return await ctx.response.send_message(
                "Somente o dono do servidor pode listar pendências de segurança.",
                ephemeral=True,
            )

        guild_actions = [
            (action_id, action)
            for action_id, action in self.pending_actions.items()
            if action.guild_id == ctx.guild.id
        ]
        if not guild_actions:
            return await ctx.response.send_message("Não há ações pendentes.", ephemeral=True)

        lines = [f"`{action_id}` - {action.description}" for action_id, action in guild_actions]
        await ctx.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SecurityCog(bot))
