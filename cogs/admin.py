import asyncio
import time
import json
import re
import logging
from discord import Interaction, Member, User, app_commands
from typing import Literal
from discord.ext import commands
import discord

from core.database import (
    admConnectTelegramAccount,
    setServerMessage,
    getConfig,
    updateServerConfig,
    get_portaria_base_config,
    getStaffRoles as getStaffRoleIds,
    getAutoJoinRolesConfig,
    setAutoJoinRolesConfig,
    get_allowed_feature_channels,
    getVipRoleDivisionConfig,
    getVipRolesConfig,
    getVipCustomRolePrefixConfig,
    getGuildStaffColors,
    setGuildStaffColors,
    getBirthdayMessageChannelId,
    setBirthdayMessageChannelId,
    getCollaborativeModerationConfig,
    getHashtagRoleMentionsConfig,
    setHashtagRoleMentionsConfig,
    getThreadOwnerOnlyPostingConfig,
    setThreadOwnerOnlyPostingConfig,
    getAllLogConfigs,
    getLogConfig,
    upsertLogConfig,
    getBumpConfig,
    setBumpWarningConfig,
    setBumpMonthlyRewardConfig,
    set_bump_reward_economy_config,
    getLevelConfig,
)
from core.notifications import notify_owner_and_user
from core.discord_events import getStaffRoles
from core.log_targets import validate_log_target
from core.log_types import DEFAULT_LOG_TYPES, LOG_TYPE_LABELS
from core.auto_join_roles import is_sensitive
from schemas.types.server_messages import ServerMessages




PORTARIA_ROLE_FIELDS = {
    "acesso_provisorio": "acesso_provisorio_role_id",
    "visitante": "visitante_role_id",
    "maior_18": "maior_18_role_id",
    "menor_18": "menor_18_role_id",
}



AI_ALLOWED_CHANNELS_FEATURE_KEY = "ai_responses"
AUTO_JOIN_SENSITIVE_PERMISSIONS = ("ban_members", "manage_roles", "manage_channels")
ROLE_REFERENCE_PATTERN = re.compile(r"<@&(\d+)>|\b(\d{15,22})\b")
HASHTAG_REFERENCE_PATTERN = re.compile(r"(#[\w\-]{2,64})", re.UNICODE)


def _chunk_message_lines(lines: list[str], limit: int = 1900) -> list[str]:
    pages: list[str] = []
    current: list[str] = []
    current_size = 0

    for raw_line in lines:
        line = raw_line if len(raw_line) <= limit else raw_line[: max(limit - 3, 1)] + "..."
        line_size = len(line) + (1 if current else 0)
        if current and current_size + line_size > limit:
            pages.append("\n".join(current))
            current = [line]
            current_size = len(line)
            continue
        current.append(line)
        current_size += line_size

    if current:
        pages.append("\n".join(current))

    return pages or [""]


def _log_status_label(enabled: bool) -> str:
    return "Ativado" if enabled else "Desativado"


def _has_sensitive_permissions(role: discord.Role) -> bool:
    return is_sensitive(role)


def _normalize_hashtag_token(raw_value: str) -> str:
    token = str(raw_value or "").strip()
    if not token:
        return ""
    if not token.startswith("#"):
        token = f"#{token}"
    token = token.replace(" ", "")
    return token.casefold()


def _parse_role_references(guild: discord.Guild, raw_references: str) -> tuple[list[discord.Role], list[str]]:
    seen: set[int] = set()
    parsed_roles: list[discord.Role] = []
    invalid_references: list[str] = []

    for match in ROLE_REFERENCE_PATTERN.finditer(raw_references or ""):
        role_id_text = match.group(1) or match.group(2)
        if not role_id_text:
            continue
        role_id = int(role_id_text)
        if role_id in seen:
            continue
        seen.add(role_id)

        role = guild.get_role(role_id)
        if role is None:
            invalid_references.append(role_id_text)
            continue
        parsed_roles.append(role)

    return parsed_roles, invalid_references


def _log_type_display(log_type: str) -> str:
    return LOG_TYPE_LABELS.get(log_type, log_type.replace("_", " ").title())


def _build_logs_overview_embed(guild: discord.Guild, log_configs: dict[str, dict]) -> discord.Embed:
    embed = discord.Embed(
        title="Configuração de logs",
        description="Lista de logs disponíveis, status e canal configurado.",
        color=discord.Color.blurple(),
    )
    for log_type in sorted(log_configs.keys()):
        config = log_configs[log_type]
        channel_id = config.get("log_channel")
        channel_display = f"<#{channel_id}>" if channel_id else "não configurado"
        embed.add_field(
            name=_log_type_display(log_type),
            value=(
                f"**Status:** {_log_status_label(bool(config.get('enabled')))}\n"
                f"**Canal:** {channel_display}"
            ),
            inline=False,
        )
    embed.set_footer(text=f"Servidor: {guild.name}")
    return embed


def _build_xp_settings_lines(config: dict) -> list[str]:
        warning_channel = config.get("levelupWarningChannel")
        warning_channel_display = f"<#{warning_channel}> (`{warning_channel}`)" if warning_channel else "não configurado"

        return [
            "📋 **Configuração de XP**",
            "",
            "**Curva de progressão** (`XP(nível) = k·nível^p + b·nível`)",
            f"- `k`: `{config.get('phase1K')}`",
            f"- `p`: `{config.get('phase1P')}`",
            f"- `b`: `{config.get('phase1B')}`",
            "",
            "**Multiplicadores**",
            f"- Multiplicador global: `{config.get('multiplier')}`",
            f"- Combo diário base: `{config.get('dailyCombo')}`",
            f"- Multiplicador de combo: `{config.get('comboMultiplier')}`",
            "",
            "**Configuração de level up**",
            f"- Aviso de level up: `{'ativado' if config.get('levelupWarning') else 'desativado'}`",
            f"- Canal de aviso: {warning_channel_display}",
            f"- Mensagem de level up: `{config.get('levelUpMessage')}`",
            f"- Limpar dados na saída: `{'sim' if config.get('clearOnExit') else 'não'}`",
            "",
            "**XP de voz**",
            f"- XP base por minuto: `{config.get('xpBasePerMin')}`",
            f"- Bônus social (%): `{config.get('voiceSocialBonusPct')}`",
            f"- Humanos mínimos p/ bônus: `{config.get('voiceSocialBonusMinHumans')}`",
            f"- Janela 1 (min): `{config.get('voiceDiminishingWindow1Minutes')}`",
            f"- Janela 2 (min): `{config.get('voiceDiminishingWindow2Minutes')}`",
            f"- Fator após janela 2: `{config.get('voiceDiminishingFactor2')}`",
            f"- Fator após janela 3: `{config.get('voiceDiminishingFactor3')}`",
            "",
            "**Caps diários de XP**",
            f"- Voz: `{config.get('voiceDailyCapXp')}`",
            f"- Texto: `{config.get('textDailyCapXp')}`",
            f"- Global: `{config.get('globalDailyCapXp')}`",
        ]

class LogOverviewView(discord.ui.View):
    def __init__(self, cog: "AdminCog", allowed_user_id: int, log_configs: dict[str, dict]):
        super().__init__(timeout=300)
        self.cog = cog
        self.allowed_user_id = int(allowed_user_id)
        self.log_configs = log_configs

    async def interaction_check(self, interaction: Interaction) -> bool:
        if int(interaction.user.id) != self.allowed_user_id:
            await interaction.response.send_message(
                "Apenas quem executou o comando pode interagir com esta visualização.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Editar", style=discord.ButtonStyle.secondary)
    async def edit(self, interaction: Interaction, _: discord.ui.Button):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )

        edit_view = LogsEditView(
            cog=self.cog,
            guild=interaction.guild,
            allowed_user_id=self.allowed_user_id,
            log_configs=self.log_configs,
        )
        await interaction.response.edit_message(
            embed=edit_view.build_embed(),
            view=edit_view,
        )


class LogsEditView(discord.ui.View):
    def __init__(
        self,
        cog: "AdminCog",
        guild: discord.Guild,
        allowed_user_id: int,
        log_configs: dict[str, dict],
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.allowed_user_id = int(allowed_user_id)
        self.log_configs = log_configs

        self.selected_type = sorted(self.log_configs.keys())[0]
        self.initial_status = bool(self.log_configs[self.selected_type]["enabled"])
        self.initial_channel_id = self.log_configs[self.selected_type]["log_channel"]
        self.selected_status = self.initial_status
        self.selected_channel_id = self.initial_channel_id

        self.type_select = discord.ui.Select(
            placeholder="Selecione o tipo de log",
            min_values=1,
            max_values=1,
            row=0,
            options=[
                discord.SelectOption(
                    label=_log_type_display(log_type)[:100],
                    value=log_type,
                    default=(log_type == self.selected_type),
                )
                for log_type in sorted(self.log_configs.keys())
            ],
        )
        self.type_select.callback = self._on_type_change
        self.add_item(self.type_select)

        self.status_select = discord.ui.Select(
            placeholder="Selecione o status",
            min_values=1,
            max_values=1,
            row=1,
            options=[],
        )
        self.status_select.callback = self._on_status_change
        self.add_item(self.status_select)

        self.channel_select = discord.ui.ChannelSelect(
            placeholder="Selecione um canal de texto ou tópico",
            min_values=0,
            max_values=1,
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
                discord.ChannelType.news_thread,
            ],
            row=2,
        )
        self.channel_select.callback = self._on_channel_change
        self.add_item(self.channel_select)

        self._refresh_dependent_selects()

    async def interaction_check(self, interaction: Interaction) -> bool:
        if int(interaction.user.id) != self.allowed_user_id:
            await interaction.response.send_message(
                "Apenas quem executou o comando pode interagir com esta visualização.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    def _refresh_type_defaults(self):
        for option in self.type_select.options:
            option.default = option.value == self.selected_type

    def _refresh_dependent_selects(self):
        self._refresh_type_defaults()

        current = self.log_configs[self.selected_type]
        self.initial_status = bool(current["enabled"])
        self.initial_channel_id = current["log_channel"]
        self.selected_status = self.initial_status
        self.selected_channel_id = self.initial_channel_id
        self._refresh_channel_placeholder()
        self.status_select.options = [
            discord.SelectOption(
                label="Ativado",
                value="1",
                default=self.selected_status,
            ),
            discord.SelectOption(
                label="Desativado",
                value="0",
                default=not self.selected_status,
            ),
        ]
        self._update_confirm_state()

    def _get_selected_status(self) -> bool:
        return self.selected_status

    def _get_selected_channel(self) -> int | None:
        return self.selected_channel_id

    def _refresh_channel_placeholder(self):
        if self.selected_channel_id:
            channel = self.guild.get_channel_or_thread(int(self.selected_channel_id))
            channel_name = f"#{channel.name}" if channel else f"ID {self.selected_channel_id}"
            self.channel_select.placeholder = f"Canal selecionado: {channel_name}"[:150]
            return

        self.channel_select.placeholder = "Selecione um canal de texto ou tópico"

    @staticmethod
    def _extract_selected_channel_id(interaction: Interaction) -> int | None:
        data = interaction.data if isinstance(interaction.data, dict) else {}
        selected_ids = data.get("values")
        if isinstance(selected_ids, list) and selected_ids:
            return int(selected_ids[0])

        resolved = data.get("resolved")
        if isinstance(resolved, dict):
            channels = resolved.get("channels")
            if isinstance(channels, dict) and channels:
                first_key = next(iter(channels.keys()))
                return int(first_key)

        return None

    def _update_confirm_state(self):
        changed = (
            self._get_selected_status() != self.initial_status
            or self._get_selected_channel() != self.initial_channel_id
        )
        self.confirm.disabled = not changed

    def build_embed(self) -> discord.Embed:
        selected_config = self.log_configs[self.selected_type]
        current_channel_id = selected_config.get("log_channel")
        current_channel = f"<#{current_channel_id}>" if current_channel_id else "não configurado"

        selected_channel_id = self._get_selected_channel()
        selected_channel = f"<#{selected_channel_id}>" if selected_channel_id else "não configurado"

        current_status = _log_status_label(bool(selected_config.get("enabled")))
        selected_status = _log_status_label(self._get_selected_status())
        embed = discord.Embed(
            title="Editar configurações de log",
            description=(
                "Escolha um tipo de log para editar. "
                "Ao trocar o tipo, os demais campos voltam para os valores atuais desse tipo. "
                "As seleções são aplicadas apenas após confirmar."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Tipo selecionado", value=_log_type_display(self.selected_type), inline=False)
        embed.add_field(name="Status atual", value=current_status, inline=True)
        embed.add_field(name="Canal atual", value=current_channel, inline=True)
        embed.add_field(name="Status selecionado", value=selected_status, inline=True)
        embed.add_field(name="Canal selecionado", value=selected_channel, inline=True)
        return embed

    async def _on_type_change(self, interaction: Interaction):
        self.selected_type = self.type_select.values[0]
        latest_config = getLogConfig(self.guild.id, self.selected_type)
        if latest_config is not None:
            self.log_configs[self.selected_type] = {
                "enabled": bool(latest_config.get("enabled")),
                "log_channel": latest_config.get("log_channel"),
            }
        else:
            self.log_configs[self.selected_type] = {
                "enabled": False,
                "log_channel": None,
            }
        self._refresh_dependent_selects()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_status_change(self, interaction: Interaction):
        self.selected_status = self.status_select.values[0] == "1"
        for option in self.status_select.options:
            option.default = option.value == ("1" if self.selected_status else "0")
        self._update_confirm_state()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_channel_change(self, interaction: Interaction):
        selected_channel_id = self._extract_selected_channel_id(interaction)
        if selected_channel_id is not None:
            self.selected_channel_id = selected_channel_id
        elif self.channel_select.values:
            selected_channel = self.channel_select.values[0]
            selected_channel_id = getattr(selected_channel, "id", selected_channel)
            self.selected_channel_id = int(selected_channel_id)
        else:
            self.selected_channel_id = None

        self._refresh_channel_placeholder()
        self._update_confirm_state()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Finalizar", style=discord.ButtonStyle.primary, row=3)
    async def finish(self, interaction: Interaction, _: discord.ui.Button):
        overview_view = LogOverviewView(
            cog=self.cog,
            allowed_user_id=self.allowed_user_id,
            log_configs=self.log_configs,
        )
        await interaction.response.edit_message(
            embed=_build_logs_overview_embed(self.guild, self.log_configs),
            view=overview_view,
        )

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.success, disabled=True, row=3)
    async def confirm(self, interaction: Interaction, _: discord.ui.Button):
        new_status = self._get_selected_status()
        new_channel = self._get_selected_channel()

        if new_status and new_channel is None:
            return await interaction.response.send_message(
                "Um canal ou post de fórum válido é obrigatório para ativar este log.", ephemeral=True
            )
        target_changed = new_channel != self.initial_channel_id
        if new_status or target_changed:
            if new_channel is None:
                return await interaction.response.send_message("Selecione um destino válido.", ephemeral=True)
            _, warnings = await validate_log_target(self.guild, new_channel)
            if warnings:
                return await interaction.response.send_message(warnings[0], ephemeral=True)

        updated = upsertLogConfig(
            guild_id=self.guild.id,
            log_type=self.selected_type,
            enabled=new_status,
            log_channel=new_channel,
        )
        if not updated:
            return await interaction.response.send_message(
                "Não foi possível salvar a configuração desse log.",
                ephemeral=True,
            )

        self.log_configs[self.selected_type] = {
            "enabled": new_status,
            "log_channel": new_channel,
        }
        self._refresh_dependent_selects()

        await interaction.response.edit_message(
            content=f"✅ Configuração de **{_log_type_display(self.selected_type)}** atualizada com sucesso.",
            embed=self.build_embed(),
            view=self,
        )

class RoleAssignmentConfirmationView(discord.ui.View):
    def __init__(self, allowed_user_id: int):
        super().__init__(timeout=60)
        self.allowed_user_id = int(allowed_user_id)
        self.decision: bool | None = None

    async def interaction_check(self, interaction: Interaction) -> bool:
        if int(interaction.user.id) != self.allowed_user_id:
            await interaction.response.send_message(
                "Apenas quem executou o comando pode confirmar esta ação.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: Interaction, _: discord.ui.Button):
        self.decision = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="✅ Confirmação recebida. Preparando atribuição de cargo...",
            view=self,
        )
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, _: discord.ui.Button):
        self.decision = False
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="❌ Atribuição de cargo cancelada.",
            view=self,
        )
        self.stop()


class AdminCog(commands.Cog):
    admin = app_commands.Group(name="admin", description="Comandos administrativos")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    @admin.command(name="conectar_conta", description="Conecta sua conta do discord com a do telegram")
    async def connectAccount(self, ctx: Interaction, user: Member, telegram_username: str):
        await ctx.response.defer()
        result = admConnectTelegramAccount(ctx.guild.id, user, telegram_username)
        if result:
            return await ctx.followup.send(
                content=(
                    'Sua conta foi conectada com sucesso! agora você pode usar os comandos do bot no discord e no telegram'
                ),
                ephemeral=False,
            )
        else:
            return await ctx.followup.send(
                content='Não foi possível conectar sua conta! você já está conectado?',
                ephemeral=True,
            )

    @admin.command(name="mensagens_servidor", description="Configure as mensagens especificas do servidor")
    async def changeMessages(self, ctx: Interaction, tipo: ServerMessages, mensagem: str):
        if tipo == 'Aniversário':
            updated = setServerMessage(ctx.guild_id, 'birthday', mensagem)
        elif tipo == 'Bump':
            updated = setServerMessage(ctx.guild_id, 'bump', mensagem)
        else:
            updated = False
        if not updated:
            return await ctx.response.send_message(
                content=f'Não foi possível alterar a mensagem de {tipo}',
                ephemeral=True,
            )
        return await ctx.response.send_message(
            content=f'Mensagem de {tipo} alterada com sucesso!',
            ephemeral=True,
        )

    @admin.command(
        name='configurar-servidor',
        description='Exibe ou altera as configurações do servidor',
    )
    @app_commands.describe(
        economia='Ativa ou desativa a economia',
        ia='Ativa ou desativa as respostas por IA',
    )
    async def serverConfig(
        self,
        ctx: Interaction,
        economia: bool | None = None,
        ia: bool | None = None,
    ):
        if economia is None and ia is None:
            config = getConfig(ctx.guild)
            embed = discord.Embed(
                title='Configurações do Servidor',
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name='Economia',
                value='Ativada' if config.get('hasEconomyEnabled') else 'Desativada',
                inline=False,
            )
            embed.add_field(
                name='Respostas por IA',
                value='Ativadas' if config.get('hasGptEnabled') else 'Desativadas',
                inline=False,
            )
            return await ctx.response.send_message(embed=embed, ephemeral=True)

        staff_roles = getStaffRoles(ctx.guild)
        if not any(role in ctx.user.roles for role in staff_roles):
            return await ctx.response.send_message(
                content='Você não tem permissão para alterar as configurações.',
                ephemeral=True,
            )

        updates = {}
        if economia is not None:
            updates['has_economy_enabled'] = economia
        if ia is not None:
            updates['has_gpt_enabled'] = ia

        updated = updateServerConfig(ctx.guild_id, **updates)
        if not updated:
            return await ctx.response.send_message(
                content='Não foi possível atualizar as configurações.',
                ephemeral=True,
            )

        config = getConfig(ctx.guild)
        embed = discord.Embed(
            title='Configurações do Servidor',
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name='Economia',
            value='Ativada' if config.get('hasEconomyEnabled') else 'Desativada',
            inline=False,
        )
        embed.add_field(
            name='Respostas por IA',
            value='Ativadas' if config.get('hasGptEnabled') else 'Desativadas',
            inline=False,
        )
        await ctx.response.send_message(
            content='Configurações atualizadas com sucesso!',
            embed=embed,
            ephemeral=True,
        )

    @admin.command(name="logs", description="Lista e edita as configurações de logs do servidor")
    @app_commands.default_permissions(administrator=True)
    async def logs(self, ctx: Interaction):
        if not isinstance(ctx.user, discord.Member) or not ctx.user.guild_permissions.administrator:
            return await ctx.response.send_message(
                content='Apenas administradores podem alterar essa configuração.',
                ephemeral=True,
            )

        rows = getAllLogConfigs(ctx.guild_id)
        log_configs: dict[str, dict] = {}

        for row in rows:
            log_type = str(row.get("type") or "").strip().lower()
            if not log_type:
                continue
            channel_raw = row.get("log_channel")
            channel_id = int(channel_raw) if channel_raw is not None else None
            log_configs[log_type] = {
                "enabled": bool(row.get("enabled")),
                "log_channel": channel_id,
            }

        for log_type in DEFAULT_LOG_TYPES:
            log_configs.setdefault(
                log_type,
                {
                    "enabled": False,
                    "log_channel": None,
                },
            )

        if not log_configs:
            return await ctx.response.send_message(
                content='Nenhum tipo de log disponível para este servidor.',
                ephemeral=True,
            )

        overview_view = LogOverviewView(
            cog=self,
            allowed_user_id=ctx.user.id,
            log_configs=log_configs,
        )
        await ctx.response.send_message(
            embed=_build_logs_overview_embed(ctx.guild, log_configs),
            view=overview_view,
            ephemeral=True,
        )


    canais = app_commands.Group(name="canais", description="Configura canais permitidos por funcionalidade", parent=admin)

    @canais.command(name="setar_aniversario", description="Define o canal para envio das mensagens de aniversário")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(canal='Canal de texto onde o bot enviará as mensagens de aniversário')
    async def set_birthday_channel(
        self,
        ctx: Interaction,
        canal: discord.TextChannel,
    ):
        if not isinstance(ctx.user, discord.Member) or not ctx.user.guild_permissions.administrator:
            return await ctx.response.send_message(
                content='Apenas administradores podem alterar essa configuração.',
                ephemeral=True,
            )

        bot_user = self.bot.user
        bot_member = ctx.guild.get_member(bot_user.id) if ctx.guild and bot_user else None
        if bot_member is None:
            return await ctx.response.send_message(
                content='Não foi possível validar as permissões do bot neste servidor.',
                ephemeral=True,
            )

        channel_permissions = canal.permissions_for(bot_member)
        if not (channel_permissions.view_channel and channel_permissions.send_messages):
            return await ctx.response.send_message(
                content=(
                    'Não posso enviar mensagens nesse canal. '
                    'Garanta as permissões de visualizar e enviar mensagens antes de salvar.'
                ),
                ephemeral=True,
            )

        updated = setBirthdayMessageChannelId(ctx.guild_id, canal.id)
        if not updated:
            return await ctx.response.send_message(
                content='Não foi possível salvar o canal de aniversário.',
                ephemeral=True,
            )

        await ctx.response.send_message(
            content=(
                "Canal de aniversário atualizado com sucesso:\n"
                f"- {canal.mention} (`{canal.id}`)"
            ),
            ephemeral=True,
        )


    cargos = app_commands.Group(name="cargos", description="Configura os cargos base da portaria", parent=admin)
    portaria = app_commands.Group(name="portaria", description="Configura regras extras da portaria", parent=admin)
    bump = app_commands.Group(name="bump", description="Configura avisos e premiação de bump", parent=admin)
    auto_cargos = app_commands.Group(
        name="auto-cargos",
        description="Configura cargos automáticos para entrada de membros",
        parent=admin,
    )

    staff = app_commands.Group(name="staff", description="Configura cargos de staff", parent=admin)
    moderacao_colaborativa = app_commands.Group(
        name="moderacao_colaborativa",
        description="Configura a moderação por reação da comunidade",
        parent=admin,
    )
    hashtags = app_commands.Group(
        name="hashtags",
        description="Configura menções automáticas de cargos via hashtags",
        parent=admin,
    )
    threads = app_commands.Group(
        name="threads",
        description="Configura restrições de postagem em threads por autor",
        parent=admin,
    )
    configuracoes = app_commands.Group(
        name="configuracoes",
        description="Centraliza listagens de configurações administrativas",
        parent=admin,
    )

    @configuracoes.command(name="listar", description="Lista configurações administrativas por categoria")
    @app_commands.describe(lista="Qual categoria de configuração deseja listar")
    @app_commands.choices(
        lista=[
            app_commands.Choice(name="portaria", value="portaria"),
            app_commands.Choice(name="vip", value="vip"),
            app_commands.Choice(name="bump", value="bump"),
            app_commands.Choice(name="aniversário", value="aniversario"),
            app_commands.Choice(name="IA", value="ia"),
            app_commands.Choice(name="auto-cargos", value="auto_cargos"),
            app_commands.Choice(name="moderação colaborativa", value="moderacao_colaborativa"),
            app_commands.Choice(name="cargos de staff", value="staff"),
            app_commands.Choice(name="XP", value="xp"),
        ]
    )
    async def list_admin_configurations(self, ctx: Interaction, lista: app_commands.Choice[str]):
        if not self._staff_guard(ctx):
            return await ctx.response.send_message(
                content='Você não tem permissão para listar essa configuração.',
                ephemeral=True,
            )

        selected = lista.value
        lines: list[str]

        if selected == "portaria":
            config = get_portaria_base_config(ctx.guild_id)
            lines = self._build_portaria_settings_lines(config)
        elif selected == "vip":
            configured_role_ids = getVipRolesConfig(ctx.guild_id)
            config = getVipRoleDivisionConfig(ctx.guild_id)
            start_role_id = config.get("startRoleId")
            end_role_id = config.get("endRoleId")
            configured_prefix = getVipCustomRolePrefixConfig(ctx.guild_id)

            lines = ["📋 **Configuração VIP**", "", "**Cargos VIP configurados:**"]
            if configured_role_ids:
                for role_id in configured_role_ids:
                    role = ctx.guild.get_role(role_id)
                    role_display = role.mention if role else f"`{role_id}` (não encontrado no servidor)"
                    lines.append(f"- {role_display}")
            else:
                lines.append("- Nenhum cargo VIP dinâmico foi configurado")

            lines.extend(
                [
                    "",
                    "**Prefixo dos cargos customizados:**",
                    f"- `{configured_prefix or 'VIP'}`" + ("" if configured_prefix else " (fallback local do bot)"),
                    "",
                    "**Intervalo dos cargos customizados:**",
                    f"- **Início**: {self._mention_or_not_set(start_role_id, is_role=True)}",
                    f"- **Fim**: {self._mention_or_not_set(end_role_id, is_role=True)}",
                ]
            )
        elif selected == "bump":
            lines = self._build_bump_settings_lines(ctx.guild_id)
        elif selected == "aniversario":
            lines = self._build_birthday_settings_lines(ctx.guild_id)
        elif selected == "ia":
            channel_ids = get_allowed_feature_channels(ctx.guild_id, AI_ALLOWED_CHANNELS_FEATURE_KEY)
            lines = ["📋 **Canais permitidos para IA**"]
            if not channel_ids:
                lines.append("- Nenhum canal de IA configurado no banco de dados")
            else:
                for channel_id in channel_ids:
                    lines.append(f"- <#{channel_id}> (`{channel_id}`)")
        elif selected == "auto_cargos":
            config = getAutoJoinRolesConfig(ctx.guild_id)
            status = "ativado" if config.get("enabled") else "desativado"
            role_ids = config.get("roleIds") or []

            lines = [
                "📋 **Auto-cargos de entrada**",
                f"- **Status**: {status}",
                "",
                "**Cargos configurados:**",
            ]
            if role_ids:
                lines.extend(f"- <@&{role_id}> (`{role_id}`)" for role_id in role_ids)
            else:
                lines.append("- Nenhum cargo configurado")
        elif selected == "xp":
            config = getLevelConfig(ctx.guild_id)
            lines = _build_xp_settings_lines(config)
        elif selected == "moderacao_colaborativa":
            config = getCollaborativeModerationConfig(ctx.guild_id)
            status = "ativada" if config.get("enabled") else "desativada"
            emoji = config.get("emoji") or "não configurado"
            min_reactions = int(config.get("minReactions") or 3)
            lines = [
                "📋 **Configuração da moderação colaborativa**",
                f"- **Status**: {status}",
                f"- **Emoji monitorado**: {emoji}",
                f"- **Quantidade mínima de reações**: {min_reactions}",
            ]
        else:
            configured_ids = getStaffRoleIds(ctx.guild_id)
            lines = ["📋 **Cargos de staff registrados**"]
            if not configured_ids:
                lines.append("- Nenhum cargo de staff foi registrado ainda")
            else:
                for role_id in configured_ids:
                    role = ctx.guild.get_role(int(role_id))
                    lines.append(f"- {role.mention if role else f'Cargo removido ({role_id})'}")

        content_pages = _chunk_message_lines(lines)
        await ctx.response.send_message(content=content_pages[0], ephemeral=True)
        for content_page in content_pages[1:]:
            await ctx.followup.send(content=content_page, ephemeral=True)

    @hashtags.command(name="status", description="Mostra a configuração atual das hashtags de cargos")
    @app_commands.default_permissions(administrator=True)
    async def hashtag_roles_status(self, ctx: Interaction):
        if not isinstance(ctx.user, discord.Member) or not ctx.user.guild_permissions.administrator:
            return await ctx.response.send_message(
                content='Apenas administradores podem listar essa configuração.',
                ephemeral=True,
            )

        config = getHashtagRoleMentionsConfig(ctx.guild_id)
        enabled = bool(config.get("enabled"))
        channel_ids = config.get("channelIds") or []
        author_role_id = config.get("authorRoleId")
        hashtag_map = config.get("hashtagMap") or {}

        channels_display = "\n".join(f"- <#{channel_id}> (`{channel_id}`)" for channel_id in channel_ids) if channel_ids else "- Nenhum canal configurado"
        author_role_display = f"<@&{author_role_id}> (`{author_role_id}`)" if author_role_id else "Sem restrição de cargo do autor"

        lines = []
        for hashtag, role_id in sorted(hashtag_map.items()):
            lines.append(f"- `{hashtag}` → <@&{role_id}> (`{role_id}`)")

        mapped_display = "\n".join(lines) if lines else "- Nenhuma hashtag mapeada"
        await ctx.response.send_message(
            content=(
                f"**Status:** {'Ativado' if enabled else 'Desativado'}\n"
                f"**Canais monitorados:**\n{channels_display}\n"
                f"**Cargo obrigatório do autor da thread:** {author_role_display}\n"
                f"**Mapeamentos:**\n{mapped_display}"
            ),
            ephemeral=True,
        )

    @hashtags.command(name="ativar", description="Ativa ou desativa a automação de hashtags para cargos")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(ativo="Defina true para ativar ou false para desativar")
    async def hashtag_roles_enable(self, ctx: Interaction, ativo: bool):
        if not isinstance(ctx.user, discord.Member) or not ctx.user.guild_permissions.administrator:
            return await ctx.response.send_message(
                content='Apenas administradores podem alterar essa configuração.',
                ephemeral=True,
            )

        saved = setHashtagRoleMentionsConfig(ctx.guild_id, enabled=ativo)
        if not saved:
            return await ctx.response.send_message(
                content='Não foi possível salvar a configuração de ativação.',
                ephemeral=True,
            )

        await ctx.response.send_message(
            content=f"Automação de hashtags para cargos {'ativada' if ativo else 'desativada'} com sucesso.",
            ephemeral=True,
        )

    @hashtags.command(name="setar_canais", description="Define canais base cujas threads terão leitura de hashtags para menção")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(canais="IDs/menções de canais separados por vírgula")
    async def hashtag_roles_set_channels(self, ctx: Interaction, canais: str):
        if not isinstance(ctx.user, discord.Member) or not ctx.user.guild_permissions.administrator:
            return await ctx.response.send_message(
                content='Apenas administradores podem alterar essa configuração.',
                ephemeral=True,
            )

        raw_ids = [part.strip() for part in canais.split(',') if part.strip()]
        channel_ids: list[int] = []

        for raw_id in raw_ids:
            cleaned = raw_id.replace('<#', '').replace('>', '').strip()
            if not cleaned.isdigit():
                return await ctx.response.send_message(
                    content=f'Canal inválido informado: `{raw_id}`.',
                    ephemeral=True,
                )

            channel_id = int(cleaned)
            channel = ctx.guild.get_channel(channel_id) if ctx.guild else None
            if channel is None:
                return await ctx.response.send_message(
                    content=f'O canal `{channel_id}` não pertence a este servidor.',
                    ephemeral=True,
                )
            channel_ids.append(channel_id)

        saved = setHashtagRoleMentionsConfig(ctx.guild_id, channel_ids=channel_ids)
        if not saved:
            return await ctx.response.send_message(
                content='Não foi possível salvar os canais monitorados.',
                ephemeral=True,
            )

        mentions = "\n".join(f"- <#{channel_id}> (`{channel_id}`)" for channel_id in sorted(set(channel_ids)))
        await ctx.response.send_message(
            content=f'Canais monitorados atualizados com sucesso:\n{mentions}',
            ephemeral=True,
        )

    @hashtags.command(name="setar_cargo_autor", description="Define cargo obrigatório para o autor da thread acionar menções")
    @app_commands.default_permissions(administrator=True)
    async def hashtag_roles_set_author_role(self, ctx: Interaction, cargo: discord.Role):
        if not isinstance(ctx.user, discord.Member) or not ctx.user.guild_permissions.administrator:
            return await ctx.response.send_message(
                content='Apenas administradores podem alterar essa configuração.',
                ephemeral=True,
            )

        saved = setHashtagRoleMentionsConfig(ctx.guild_id, author_role_id=cargo.id)
        if not saved:
            return await ctx.response.send_message(
                content='Não foi possível salvar o cargo obrigatório.',
                ephemeral=True,
            )

        await ctx.response.send_message(
            content=f'Cargo obrigatório do autor atualizado para {cargo.mention}.',
            ephemeral=True,
        )

    @hashtags.command(name="limpar_cargo_autor", description="Remove a exigência de cargo do autor para acionar menções")
    @app_commands.default_permissions(administrator=True)
    async def hashtag_roles_clear_author_role(self, ctx: Interaction):
        if not isinstance(ctx.user, discord.Member) or not ctx.user.guild_permissions.administrator:
            return await ctx.response.send_message(
                content='Apenas administradores podem alterar essa configuração.',
                ephemeral=True,
            )

        saved = setHashtagRoleMentionsConfig(ctx.guild_id, author_role_id=0)
        if not saved:
            return await ctx.response.send_message(
                content='Não foi possível limpar o cargo obrigatório.',
                ephemeral=True,
            )

        await ctx.response.send_message(
            content='Exigência de cargo do autor removida com sucesso.',
            ephemeral=True,
        )

    @hashtags.command(name="mapear", description="Mapeia uma hashtag para um cargo")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(hashtag="Hashtag no formato #exemplo", cargo="Cargo que será mencionado")
    async def hashtag_roles_map(self, ctx: Interaction, hashtag: str, cargo: discord.Role):
        if not isinstance(ctx.user, discord.Member) or not ctx.user.guild_permissions.administrator:
            return await ctx.response.send_message(
                content='Apenas administradores podem alterar essa configuração.',
                ephemeral=True,
            )

        normalized = _normalize_hashtag_token(hashtag)
        if not normalized or not HASHTAG_REFERENCE_PATTERN.fullmatch(normalized):
            return await ctx.response.send_message(
                content='Hashtag inválida. Use apenas letras, números, `_` e `-` (ex.: `#comissoes`).',
                ephemeral=True,
            )

        config = getHashtagRoleMentionsConfig(ctx.guild_id)
        hashtag_map = dict(config.get("hashtagMap") or {})
        hashtag_map[normalized] = cargo.id

        saved = setHashtagRoleMentionsConfig(ctx.guild_id, hashtag_map=hashtag_map)
        if not saved:
            return await ctx.response.send_message(
                content='Não foi possível salvar o mapeamento.',
                ephemeral=True,
            )

        await ctx.response.send_message(
            content=f'Mapeamento salvo: `{normalized}` → {cargo.mention}.',
            ephemeral=True,
        )

    @hashtags.command(name="desmapear", description="Remove o mapeamento de uma hashtag")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(hashtag="Hashtag a remover")
    async def hashtag_roles_unmap(self, ctx: Interaction, hashtag: str):
        if not isinstance(ctx.user, discord.Member) or not ctx.user.guild_permissions.administrator:
            return await ctx.response.send_message(
                content='Apenas administradores podem alterar essa configuração.',
                ephemeral=True,
            )

        normalized = _normalize_hashtag_token(hashtag)
        config = getHashtagRoleMentionsConfig(ctx.guild_id)
        hashtag_map = dict(config.get("hashtagMap") or {})
        if normalized not in hashtag_map:
            return await ctx.response.send_message(
                content=f'A hashtag `{normalized}` não está mapeada.',
                ephemeral=True,
            )

        hashtag_map.pop(normalized, None)
        saved = setHashtagRoleMentionsConfig(ctx.guild_id, hashtag_map=hashtag_map)
        if not saved:
            return await ctx.response.send_message(
                content='Não foi possível remover o mapeamento.',
                ephemeral=True,
            )

        await ctx.response.send_message(
            content=f'Mapeamento `{normalized}` removido com sucesso.',
            ephemeral=True,
        )

    @threads.command(name="status_restricao_autor", description="Mostra o status da restrição de threads por autor")
    @app_commands.default_permissions(administrator=True)
    async def thread_owner_only_status(self, ctx: Interaction):
        if not isinstance(ctx.user, discord.Member) or not ctx.user.guild_permissions.administrator:
            return await ctx.response.send_message(
                content='Apenas administradores podem listar essa configuração.',
                ephemeral=True,
            )

        config = getThreadOwnerOnlyPostingConfig(ctx.guild_id)
        forum_channel_ids = config.get("forumChannelIds") or []
        channels_display = "\n".join(
            f"- <#{channel_id}> (`{channel_id}`)" for channel_id in forum_channel_ids
        ) if forum_channel_ids else "- Nenhum fórum configurado"

        await ctx.response.send_message(
            content=(
                f"**Status:** {'Ativado' if config.get('enabled') else 'Desativado'}\n"
                f"**Fóruns monitorados:**\n{channels_display}"
            ),
            ephemeral=True,
        )

    @threads.command(name="ativar_restricao_autor", description="Ativa/desativa a restrição para só o autor falar na thread")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(ativo="Defina true para ativar ou false para desativar")
    async def thread_owner_only_enable(self, ctx: Interaction, ativo: bool):
        if not isinstance(ctx.user, discord.Member) or not ctx.user.guild_permissions.administrator:
            return await ctx.response.send_message(
                content='Apenas administradores podem alterar essa configuração.',
                ephemeral=True,
            )

        saved = setThreadOwnerOnlyPostingConfig(ctx.guild_id, enabled=ativo)
        if not saved:
            return await ctx.response.send_message(
                content='Não foi possível salvar a configuração de ativação.',
                ephemeral=True,
            )

        await ctx.response.send_message(
            content=f"Restrição de threads por autor {'ativada' if ativo else 'desativada'} com sucesso.",
            ephemeral=True,
        )

    @threads.command(name="setar_foruns_restritos", description="Define fóruns onde só o autor da thread pode enviar mensagens")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(foruns="IDs/menções de canais de fórum separados por vírgula")
    async def thread_owner_only_set_forums(self, ctx: Interaction, foruns: str):
        if not isinstance(ctx.user, discord.Member) or not ctx.user.guild_permissions.administrator:
            return await ctx.response.send_message(
                content='Apenas administradores podem alterar essa configuração.',
                ephemeral=True,
            )

        raw_ids = [part.strip() for part in foruns.split(',') if part.strip()]
        forum_channel_ids: list[int] = []

        for raw_id in raw_ids:
            cleaned = raw_id.replace('<#', '').replace('>', '').strip()
            if not cleaned.isdigit():
                return await ctx.response.send_message(
                    content=f'Fórum inválido informado: `{raw_id}`.',
                    ephemeral=True,
                )

            forum_id = int(cleaned)
            channel = ctx.guild.get_channel(forum_id) if ctx.guild else None
            if not isinstance(channel, discord.ForumChannel):
                return await ctx.response.send_message(
                    content=f'O canal `{forum_id}` não é um fórum válido deste servidor.',
                    ephemeral=True,
                )
            forum_channel_ids.append(forum_id)

        saved = setThreadOwnerOnlyPostingConfig(ctx.guild_id, forum_channel_ids=forum_channel_ids)
        if not saved:
            return await ctx.response.send_message(
                content='Não foi possível salvar os fóruns restritos.',
                ephemeral=True,
            )

        mentions = "\n".join(f"- <#{channel_id}> (`{channel_id}`)" for channel_id in sorted(set(forum_channel_ids)))
        await ctx.response.send_message(
            content=f'Fóruns restritos atualizados com sucesso:\n{mentions}',
            ephemeral=True,
        )

    @threads.command(name="limpar_foruns_restritos", description="Remove todos os fóruns da lista de restrição")
    @app_commands.default_permissions(administrator=True)
    async def thread_owner_only_clear_forums(self, ctx: Interaction):
        if not isinstance(ctx.user, discord.Member) or not ctx.user.guild_permissions.administrator:
            return await ctx.response.send_message(
                content='Apenas administradores podem alterar essa configuração.',
                ephemeral=True,
            )

        saved = setThreadOwnerOnlyPostingConfig(ctx.guild_id, forum_channel_ids=[])
        if not saved:
            return await ctx.response.send_message(
                content='Não foi possível limpar os fóruns restritos.',
                ephemeral=True,
            )

        await ctx.response.send_message(
            content='Lista de fóruns restritos limpa com sucesso.',
            ephemeral=True,
        )

    @admin.command(name="cores_staff", description="Exibe ou define as cores reservadas da staff (hex)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(cores="Lista separada por vírgula (#RRGGBB). Use 'limpar' para remover todas.")
    async def staff_colors(self, ctx: Interaction, cores: str | None = None):
        if not isinstance(ctx.user, discord.Member) or not ctx.user.guild_permissions.administrator:
            return await ctx.response.send_message(
                content='Apenas administradores podem alterar essa configuração.',
                ephemeral=True,
            )

        if cores is not None:
            normalized = cores.strip()
            if normalized.lower() in {"limpar", "clear", "none", "0"}:
                parsed_colors: list[str] = []
            else:
                parsed_colors = [part.strip() for part in normalized.split(",") if part.strip()]

            updated = setGuildStaffColors(ctx.guild_id, parsed_colors)
            if not updated:
                return await ctx.response.send_message(
                    content='Não foi possível salvar as cores da staff.',
                    ephemeral=True,
                )

        configured_colors = getGuildStaffColors(ctx.guild_id)
        display = ", ".join(configured_colors) if configured_colors else "nenhuma"
        await ctx.response.send_message(
            content=f'🎨 Cores reservadas da staff: {display}',
            ephemeral=True,
        )

    def _staff_guard(self, ctx: Interaction) -> bool:
        if ctx.guild is None or not isinstance(ctx.user, discord.Member):
            return False

        if ctx.user.guild_permissions.administrator or ctx.user.guild_permissions.manage_guild:
            return True

        staff_roles = getStaffRoles(ctx.guild)
        return any(role in ctx.user.roles for role in staff_roles)

    @staticmethod
    def _mention_or_not_set(value: int | None, is_role: bool) -> str:
        if not value:
            return "não configurado"
        return f"<@&{value}>" if is_role else f"<#{value}>"

    def _build_portaria_settings_lines(self, config: dict) -> list[str]:
        ativo = bool(config.get("aprovacao_acesso_provisorio_ativo"))
        duracao_dias = int(config.get("aprovacao_acesso_provisorio_duracao_dias") or 0)
        formulario_portaria_ativo = bool(config.get("formulario_portaria_ativo", 1))
        idade_minima_ficha_ativa = bool(config.get("idade_minima_conta_ficha_ativa"))
        idade_minima_ficha_dias = int(config.get("idade_minima_conta_ficha_dias") or 0)
        idade_minima_prov_ativa = bool(config.get("idade_minima_conta_acesso_provisorio_ativa"))
        idade_minima_prov_dias = int(config.get("idade_minima_conta_acesso_provisorio_dias") or 0)
        idade_minima_entrada_ativa = bool(config.get("idade_minima_entrada_servidor_ativa"))
        idade_minima_entrada_anos = int(config.get("idade_minima_entrada_servidor_anos") or 0)
        lines = ["📋 **Configurações da portaria**", "", "👥 **Cargos base**"]
        for key, field in PORTARIA_ROLE_FIELDS.items():
            lines.append(f"- **{key}**: {self._mention_or_not_set(config.get(field), is_role=True)}")

        lines.extend(
            [
                "",
                "⚙️ **Regras e validações**",
                f"- **Aprovação de acesso provisório**: {'ativada' if ativo else 'desativada'}",
                f"- **Duração do acesso provisório**: {duracao_dias} dia(s)",
                f"- **Formulário da portaria**: {'ativado' if formulario_portaria_ativo else 'desativado'}",
                f"- **Idade mínima da conta para ficha**: {'ativada' if idade_minima_ficha_ativa else 'desativada'} ({idade_minima_ficha_dias} dia(s))",
                f"- **Idade mínima da conta para acesso provisório**: {'ativada' if idade_minima_prov_ativa else 'desativada'} ({idade_minima_prov_dias} dia(s))",
                f"- **Idade mínima para entrar no servidor**: {'ativada' if idade_minima_entrada_ativa else 'desativada'} ({idade_minima_entrada_anos} ano(s))",
                "",
                "🔑 **Bypasses da Portaria**",
                "- Gerenciados no painel **Moderação & Acesso**.",
            ]
        )
        return lines

    def _build_bump_settings_lines(self, guild_id: int) -> list[str]:
        config = getBumpConfig(guild_id)
        warn_messages = config.get("warnMessages") or []
        lines = [
            "📋 **Configuração de bump**",
            "",
            "⚠️ **Aviso de bump**",
            f"- **Status:** {'Ativado' if config.get('warnEnabled') else 'Desativado'}",
            f"- **Canal Disboard:** {self._mention_or_not_set(config.get('warnDisboardChannelId'), is_role=False)}",
            f"- **Canal de aviso:** {self._mention_or_not_set(config.get('warnTargetChannelId'), is_role=False)}",
            f"- **Mensagens:** {len(warn_messages)} configurada(s)",
            "",
            "💰 **Recompensa por bump individual**",
            f"- **Moedas (economia):** {'Ativado' if config.get('rewardCoinsEnabled') else 'Desativado'}",
            f"- **Quantidade por bump:** {int(config.get('rewardCoins') or 0)}",
        ]
        monthly_days = config.get('monthlyRewardDays') or [0, 0, 0]
        monthly_coins = config.get('monthlyRewardCoins') or [0, 0, 0]
        lines.extend(
            [
                "",
                "🏆 **Premiação mensal (Top 3)**",
                f"- **Status:** {'Ativado' if config.get('monthlyEnabled') else 'Desativado'}",
                f"- **Canal Disboard:** {self._mention_or_not_set(config.get('monthlyDisboardChannelId'), is_role=False)}",
                f"- **Cargo temporário:** {self._mention_or_not_set(config.get('monthlyRewardRoleId'), is_role=True)}",
                f"- **Duração (1º/2º/3º):** {int(monthly_days[0])}/{int(monthly_days[1])}/{int(monthly_days[2])} dia(s)",
                f"- **Moedas (1º/2º/3º):** {int(monthly_coins[0])}/{int(monthly_coins[1])}/{int(monthly_coins[2])}",
            ]
        )
        return lines

    def _build_birthday_settings_lines(self, guild_id: int) -> list[str]:
        channel_id = getBirthdayMessageChannelId(guild_id)
        lines = ["📋 **Canal de aniversário**"]
        if not channel_id:
            lines.append("- Nenhum canal de aniversário configurado no banco de dados")
        else:
            lines.append(f"- Canal atual: <#{channel_id}> (`{channel_id}`)")
        return lines

    @bump.command(name="setar_warn", description="Configura o aviso automático quando puder dar bump novamente")
    @app_commands.describe(
        ativado="Ativa/desativa o aviso automático",
        canal_disboard="Canal em que o bot do Disboard envia o bump",
        canal_aviso="Canal onde o aviso será enviado",
        mensagens="Lista de mensagens em JSON (ex: [\"msg1\", \"msg2\"])",
    )
    async def bump_set_warn(
        self,
        ctx: Interaction,
        ativado: bool,
        canal_disboard: discord.TextChannel | None = None,
        canal_aviso: discord.TextChannel | None = None,
        mensagens: str | None = None,
    ):
        if not self._staff_guard(ctx):
            return await ctx.response.send_message(
                content='Você não tem permissão para alterar essa configuração.',
                ephemeral=True,
            )

        parsed_messages = None
        if mensagens is not None:
            try:
                payload = json.loads(mensagens)
                if not isinstance(payload, list):
                    raise ValueError
                parsed_messages = [str(item).strip() for item in payload if str(item).strip()]
            except Exception:
                return await ctx.response.send_message(
                    content='Formato inválido em `mensagens`. Use JSON com lista de textos.',
                    ephemeral=True,
                )

        updated = setBumpWarningConfig(
            ctx.guild_id,
            enabled=ativado,
            disboard_channel_id=canal_disboard.id if canal_disboard else None,
            target_channel_id=canal_aviso.id if canal_aviso else None,
            messages=parsed_messages,
        )
        if not updated:
            return await ctx.response.send_message(
                content='Não foi possível salvar a configuração de aviso de bump.',
                ephemeral=True,
            )

        await ctx.response.send_message(content="✅ Configuração de aviso de bump atualizada.", ephemeral=True)

    @bump.command(name="setar_reward", description="Configura apenas as moedas por bump individual")
    @app_commands.describe(
        ativado="Ativa/desativa o ganho de moedas por bump",
        moedas="Quantidade de moedas recebidas por bump",
    )
    async def bump_set_reward(
        self,
        ctx: Interaction,
        ativado: bool,
        moedas: app_commands.Range[int, 0, 100000],
    ):
        if not self._staff_guard(ctx):
            return await ctx.response.send_message(
                content='Você não tem permissão para alterar essa configuração.',
                ephemeral=True,
            )

        updated = set_bump_reward_economy_config(
            ctx.guild_id,
            enabled=ativado,
            points=int(moedas),
        )

        if not updated:
            return await ctx.response.send_message(
                content='Não foi possível salvar a configuração de moedas por bump.',
                ephemeral=True,
            )

        await ctx.response.send_message(content="✅ Configuração de moedas por bump atualizada.", ephemeral=True)

    @bump.command(name="setar_mensal", description="Configura premiação mensal para top 3 bumpers")
    @app_commands.describe(
        ativado="Ativa/desativa a premiação mensal",
        canal_disboard="Canal onde o Disboard confirma os bumps",
        cargo_temporario="Cargo temporário para premiar top 3 (opcional)",
        dias_1="Dias do cargo para o 1º lugar",
        dias_2="Dias do cargo para o 2º lugar",
        dias_3="Dias do cargo para o 3º lugar",
        moedas_1="Moedas para o 1º lugar",
        moedas_2="Moedas para o 2º lugar",
        moedas_3="Moedas para o 3º lugar",
    )
    async def bump_set_monthly(
        self,
        ctx: Interaction,
        ativado: bool,
        canal_disboard: discord.TextChannel | None = None,
        cargo_temporario: discord.Role | None = None,
        dias_1: app_commands.Range[int, 0, 365] = 21,
        dias_2: app_commands.Range[int, 0, 365] = 14,
        dias_3: app_commands.Range[int, 0, 365] = 7,
        moedas_1: app_commands.Range[int, 0, 100000] = 0,
        moedas_2: app_commands.Range[int, 0, 100000] = 0,
        moedas_3: app_commands.Range[int, 0, 100000] = 0,
    ):
        if not self._staff_guard(ctx):
            return await ctx.response.send_message(
                content='Você não tem permissão para alterar essa configuração.',
                ephemeral=True,
            )

        updated = setBumpMonthlyRewardConfig(
            ctx.guild_id,
            enabled=ativado,
            disboard_channel_id=canal_disboard.id if canal_disboard else None,
            reward_role_id=cargo_temporario.id if cargo_temporario else None,
            reward_days=[int(dias_1), int(dias_2), int(dias_3)],
            reward_coins=[int(moedas_1), int(moedas_2), int(moedas_3)],
        )
        if not updated:
            return await ctx.response.send_message(
                content='Não foi possível salvar a configuração de premiação mensal de bump.',
                ephemeral=True,
            )

        await ctx.response.send_message(content="✅ Configuração de premiação mensal de bump atualizada.", ephemeral=True)

    @cargos.command(name="setar", description="Define um cargo base da portaria")
    @app_commands.describe(
        cargo="Qual cargo base deseja definir",
        valor="Cargo do servidor para usar nessa configuração",
    )
    async def set_portaria_role(
        self,
        ctx: Interaction,
        cargo: Literal["acesso_provisorio", "visitante", "maior_18", "menor_18"],
        valor: discord.Role,
    ):
        if not self._staff_guard(ctx):
            return await ctx.response.send_message(
                content='Você não tem permissão para alterar essa configuração.',
                ephemeral=True,
            )
        return await ctx.response.send_message(
            content='Os cargos funcionais são administrados pelo painel na seção **Portaria**.',
            ephemeral=True,
        )
    @portaria.command(name="setar", description="Define configurações avançadas da portaria")
    @app_commands.describe(
        configuracao="Qual configuração deseja alterar",
        valor_bool="Use para acesso_provisorio_ativo",
        valor_inteiro="Use para duração em dias ou idade mínima da conta",
        valor_texto="Use para invite_bypass_codes (ex: abc123,def456)",
    )
    async def set_portaria_setting(
        self,
        ctx: Interaction,
        configuracao: Literal[
            "acesso_provisorio_ativo",
            "duracao_dias_acesso_provisorio",
            "formulario_portaria_ativo",
            "idade_minima_conta_ficha_ativa",
            "idade_minima_conta_ficha_dias",
            "idade_minima_conta_acesso_provisorio_ativa",
            "idade_minima_conta_acesso_provisorio_dias",
            "idade_minima_entrada_servidor_ativa",
            "idade_minima_entrada_servidor_anos",
            "invite_bypass_codes",
        ],
        valor_bool: bool | None = None,
        valor_inteiro: int | None = None,
        valor_texto: str | None = None,
    ):
        if not self._staff_guard(ctx):
            return await ctx.response.send_message(
                content='Você não tem permissão para alterar essa configuração.',
                ephemeral=True,
            )
        return await ctx.response.send_message(
            content='As regras da Portaria são administradas pelo painel do Coddy.',
            ephemeral=True,
        )
    @staff.command(name="registrar", description="Registra um cargo de staff")
    @app_commands.describe(cargo="Cargo que terá acesso de staff")
    async def register_staff_role(self, ctx: Interaction, cargo: discord.Role):
        if not self._staff_guard(ctx):
            return await ctx.response.send_message(
                content='Você não tem permissão para registrar cargos de staff.',
                ephemeral=True,
            )
        return await ctx.response.send_message(
            content='Os cargos da staff são administrados pelo painel em **Moderação & Acesso**.',
            ephemeral=True,
        )
    @staff.command(name="remover", description="Remove um cargo da lista de staff")
    @app_commands.describe(cargo="Cargo que deixará de ser staff")
    async def remove_staff_role(self, ctx: Interaction, cargo: discord.Role):
        if not self._staff_guard(ctx):
            return await ctx.response.send_message(
                content='Você não tem permissão para remover cargos de staff.',
                ephemeral=True,
            )
        return await ctx.response.send_message(
            content='Os cargos da staff são administrados pelo painel em **Moderação & Acesso**.',
            ephemeral=True,
        )
    @auto_cargos.command(name="configurar", description="Configura auto-cargos de entrada em um único comando")
    @app_commands.describe(
        ativado="Ativa ou desativa os auto-cargos na entrada",
        modo="Como aplicar os cargos informados na lista de auto-cargos",
        cargos="Menções/IDs dos cargos (ex.: @Cargo1 @Cargo2)",
    )
    @app_commands.choices(
        modo=[
            app_commands.Choice(name="adicionar", value="adicionar"),
            app_commands.Choice(name="remover", value="remover"),
            app_commands.Choice(name="setar", value="setar"),
        ]
    )
    async def configure_auto_join_roles(
        self,
        ctx: Interaction,
        ativado: bool,
        modo: app_commands.Choice[str],
        cargos: str | None = None,
    ):
        if not self._staff_guard(ctx):
            return await ctx.response.send_message(
                content='Você não tem permissão para alterar essa configuração.',
                ephemeral=True,
            )

        if ctx.guild is None:
            return await ctx.response.send_message(
                content='Esse comando só pode ser usado dentro de um servidor.',
                ephemeral=True,
            )

        selected_mode = modo.value
        parsed_roles, invalid_references = _parse_role_references(ctx.guild, cargos or "")
        has_role_input = bool((cargos or "").strip())
        should_change_roles = bool(parsed_roles)
        if has_role_input and not parsed_roles:
            invalid_note = (
                f"\n- Referências inválidas: {', '.join(f'`{item}`' for item in invalid_references[:10])}"
                if invalid_references
                else ""
            )
            return await ctx.response.send_message(
                content=(
                    "Informe pelo menos um cargo válido em `cargos` "
                    "(menções como `@Cargo` ou IDs numéricos)."
                    + invalid_note
                ),
                ephemeral=True,
            )

        config = getAutoJoinRolesConfig(ctx.guild_id)
        current_role_ids = set(config.get("roleIds") or [])
        staff_role_ids = set(getStaffRoleIds(ctx.guild_id))
        bot_user = self.bot.user
        bot_member = ctx.guild.me or ctx.guild.get_member(bot_user.id if bot_user else 0)
        can_manage_roles = bool(bot_member and bot_member.guild_permissions.manage_roles)

        if should_change_roles and selected_mode in ("adicionar", "setar") and not can_manage_roles:
            return await ctx.response.send_message(
                content='Não foi possível configurar: o bot não tem permissão para gerenciar cargos.',
                ephemeral=True,
            )

        valid_role_ids: list[int] = []
        skipped_reasons: list[str] = []
        for role in parsed_roles:
            if selected_mode in ("adicionar", "setar"):
                if role.is_default():
                    skipped_reasons.append(f"{role.mention}: @everyone não pode ser auto-cargo")
                    continue
                if role.managed or role >= bot_member.top_role:
                    skipped_reasons.append(f"{role.mention}: não atribuível pelo bot")
                    continue
                if role.id in staff_role_ids:
                    skipped_reasons.append(f"{role.mention}: cargo de staff")
                    continue
                if _has_sensitive_permissions(role):
                    skipped_reasons.append(f"{role.mention}: permissões sensíveis/admin")
                    continue
            valid_role_ids.append(role.id)

        if should_change_roles and not valid_role_ids and selected_mode in ("adicionar", "setar"):
            return await ctx.response.send_message(
                content=(
                    "Nenhum dos cargos informados pôde ser usado na configuração.\n"
                    f"- Detalhes: {'; '.join(skipped_reasons[:8])}"
                ),
                ephemeral=True,
            )

        updated_role_ids = set(current_role_ids)
        added_role_ids: set[int] = set()
        removed_role_ids: set[int] = set()
        selected_ids = set(valid_role_ids)

        if selected_mode == "adicionar" and should_change_roles:
            added_role_ids = selected_ids - updated_role_ids
            updated_role_ids.update(selected_ids)
        elif selected_mode == "remover" and should_change_roles:
            removed_role_ids = updated_role_ids.intersection(selected_ids)
            updated_role_ids.difference_update(selected_ids)
        elif selected_mode == "setar" and should_change_roles:
            added_role_ids = selected_ids - updated_role_ids
            removed_role_ids = updated_role_ids - selected_ids
            updated_role_ids = selected_ids

        updated = setAutoJoinRolesConfig(ctx.guild_id, enabled=ativado, role_ids=sorted(updated_role_ids))
        if not updated:
            return await ctx.response.send_message(
                content='Não foi possível salvar a configuração de auto-cargos.',
                ephemeral=True,
            )

        ignored_parts: list[str] = []
        if invalid_references:
            ignored_parts.append(
                "referências inválidas: " + ", ".join(f"`{item}`" for item in invalid_references[:10])
            )
        if skipped_reasons:
            ignored_parts.append("cargos ignorados: " + "; ".join(skipped_reasons[:8]))
        ignored_text = f"\n- **Ignorados**: {' | '.join(ignored_parts)}" if ignored_parts else ""
        await ctx.response.send_message(
            content=(
                "✅ Configuração de auto-cargos atualizada.\n"
                f"- **Status**: {'ativado' if ativado else 'desativado'}\n"
                f"- **Modo**: {selected_mode}\n"
                f"- **Adicionados**: {len(added_role_ids)}\n"
                f"- **Removidos**: {len(removed_role_ids)}\n"
                f"- **Total configurado**: {len(updated_role_ids)}{ignored_text}"
            ),
            ephemeral=True,
        )

    @moderacao_colaborativa.command(
        name="setar",
        description="Define status, emoji e quantidade mínima de reações para remover mensagem",
    )
    @app_commands.describe(
        ativado="Ativa ou desativa a moderação colaborativa",
        emoji="Emoji que será usado para sinalizar remoção (ex.: 🧹 ou <:nome:id>)",
        quantidade="Quantidade mínima de reações para apagar a mensagem",
    )
    async def set_collaborative_moderation(
        self,
        ctx: Interaction,
        ativado: bool | None = None,
        emoji: str | None = None,
        quantidade: int | None = None,
    ):
        if not self._staff_guard(ctx):
            return await ctx.response.send_message(
                content='Você não tem permissão para alterar essa configuração.',
                ephemeral=True,
            )
        return await ctx.response.send_message(
            content='A moderação colaborativa é administrada pelo painel em **Moderação & Acesso**.',
            ephemeral=True,
        )
    @admin.command(name='adicionar-cargo-todos', description='Adiciona um cargo para todos os membros do servidor')
    @app_commands.describe(cargo='Cargo que será adicionado para todos os membros')
    async def add_role_to_all_members(self, ctx: Interaction, cargo: discord.Role):
        if ctx.guild is None:
            return await ctx.response.send_message(
                content='Esse comando só pode ser usado dentro de um servidor.',
                ephemeral=True,
            )

        if not getattr(ctx.user.guild_permissions, 'administrator', False):
            return await ctx.response.send_message(
                content='Você não tem permissão para fazer isso.',
                ephemeral=True,
            )

        confirmation_view = RoleAssignmentConfirmationView(ctx.user.id)
        await ctx.response.send_message(
            content=(
                f'⚠️ Você está prestes a adicionar o cargo {cargo.mention} para todos os membros elegíveis do servidor.\n'
                'Deseja continuar?'
            ),
            view=confirmation_view,
            ephemeral=True,
        )

        await confirmation_view.wait()
        if confirmation_view.decision is None:
            return await ctx.edit_original_response(
                content='⌛ Tempo de confirmação esgotado. Atribuição cancelada.',
                view=confirmation_view,
            )

        if not confirmation_view.decision:
            return

        await ctx.edit_original_response(
            content='🔎 Analisando membros elegíveis para receber o cargo...',
            view=None,
        )

        members_without_role: list[discord.Member] = []
        total_members = 0
        last_scan_update = 0.0

        for member in ctx.guild.members:
            if member.bot:
                continue

            total_members += 1
            if cargo not in member.roles:
                members_without_role.append(member)

            now = time.monotonic()
            if now - last_scan_update >= 2:
                await ctx.edit_original_response(
                    content=(
                        f'🔎 Analisando membros elegíveis para {cargo.mention}...\n'
                        f'- Membros analisados: **{total_members}**\n'
                        f'- Elegíveis até agora: **{len(members_without_role)}**'
                    ),
                )
                last_scan_update = now

            if total_members % 200 == 0:
                await asyncio.sleep(0)

        total_to_add = len(members_without_role)

        if total_to_add == 0:
            return await ctx.followup.send(
                content=(
                    f'ℹ️ Nenhum membro precisa receber o cargo {cargo.mention}.\n'
                    f'- Membros analisados: **{total_members}**'
                ),
                ephemeral=True,
            )

        added = 0
        skipped = total_members - total_to_add
        failed = 0
        processed = 0

        batch_size = 50
        progress_update_interval = 2
        last_progress_update = 0.0

        async def try_edit_progress_message(content: str) -> None:
            try:
                await ctx.edit_original_response(content=content)
            except (discord.NotFound, discord.HTTPException):
                pass

        await try_edit_progress_message(
            content=(
                f'⏳ Iniciando adição do cargo {cargo.mention}.\n'
                f'- Total para adicionar: **{total_to_add}**\n'
                f'- Já adicionados: **0/{total_to_add}**'
            )
        )

        async def add_role(member: discord.Member) -> bool:
            nonlocal failed
            try:
                await member.add_roles(
                    cargo,
                    reason=f'Ação administrativa executada por {ctx.user}',
                )
                return True
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
                return False

        for index in range(0, total_to_add, batch_size):
            batch = members_without_role[index:index + batch_size]
            results = await asyncio.gather(*(add_role(member) for member in batch))

            added += sum(results)
            processed += len(batch)

            now = time.monotonic()
            is_last_batch = processed >= total_to_add
            if is_last_batch or now - last_progress_update >= progress_update_interval:
                await try_edit_progress_message(
                    content=(
                        f'⏳ Progresso da adição do cargo {cargo.mention}:\n'
                        f'- Total para adicionar: **{total_to_add}**\n'
                        f'- Já adicionados: **{added}/{total_to_add}**\n'
                        f'- Em processamento: **{processed}/{total_to_add}**\n'
                        f'- Falhas: **{failed}**'
                    )
                )
                last_progress_update = now

        await ctx.followup.send(
            content=(
                f'✅ Processo finalizado para o cargo {cargo.mention}.\n'
                f'- Adicionados: **{added}**\n'
                f'- Ignorados: **{skipped}**\n'
                f'- Falhas: **{failed}**'
            ),
            ephemeral=True,
        )

    @admin.command(
        name='atualizar-comandos',
        description='Força a atualização dos comandos do bot',
    )
    async def updateCommands(self, ctx: Interaction):
        await ctx.response.defer(ephemeral=True)

        staff_roles = getStaffRoles(ctx.guild)
        if not any(role in ctx.user.roles for role in staff_roles):
            return await ctx.followup.send(
                content='Você não tem permissão para atualizar os comandos.',
                ephemeral=True,
            )

        try:
            synced = await self.bot.tree.sync()
        except Exception as error:
            return await ctx.followup.send(
                content=f'Não foi possível sincronizar os comandos: {error}',
                ephemeral=True,
            )

        await ctx.followup.send(
            content=f'{len(synced)} comandos sincronizados com sucesso!',
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
