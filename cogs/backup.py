import json
import logging
from typing import Optional

import discord
from discord import Interaction, app_commands
from discord.ext import commands
from discord.ext import tasks

from core.database import (
    create_backup_restore_operation,
    get_backup_restore_operation,
    get_discord_backup_settings,
    initialize_discord_backup_tables,
    list_due_periodic_backup_guild_ids,
    purge_expired_discord_backup_guilds,
    recover_completed_backup_snapshot_operations,
    list_discord_backups,
)
from core.backup_runtime import (
    BackupBusyError,
    BackupRestoreEngine,
    periodic_idempotency_key,
)

SYNC_CONFIRMATION_TIMEOUT_SECONDS = 60
ROLE_SELECTION_TIMEOUT_SECONDS = 120
DUPLICATE_STRATEGY_TIMEOUT_SECONDS = 120


class SyncConfirmationView(discord.ui.View):
    def __init__(self, allowed_user_id: int):
        super().__init__(timeout=SYNC_CONFIRMATION_TIMEOUT_SECONDS)
        self.allowed_user_id = int(allowed_user_id)
        self.decision: Optional[bool] = None

    async def interaction_check(self, interaction: Interaction) -> bool:
        if int(interaction.user.id) != self.allowed_user_id:
            await interaction.response.send_message(
                "Apenas quem iniciou a sincronização pode confirmar esta ação.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Confirmar sincronização", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: Interaction, _: discord.ui.Button):
        self.decision = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="✅ Confirmação recebida. Iniciando sincronização...",
            view=self,
        )
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, _: discord.ui.Button):
        self.decision = False
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="❌ Sincronização cancelada.",
            view=self,
        )
        self.stop()


class RoleDisambiguationView(discord.ui.View):
    def __init__(self, allowed_user_id: int, options: list[discord.SelectOption]):
        super().__init__(timeout=ROLE_SELECTION_TIMEOUT_SECONDS)
        self.allowed_user_id = int(allowed_user_id)
        self.selected_role_id: Optional[int] = None
        self.create_new = False

        self.selector = discord.ui.Select(
            placeholder="Escolha o cargo alvo ou crie um novo",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.selector.callback = self._on_select
        self.add_item(self.selector)

    async def interaction_check(self, interaction: Interaction) -> bool:
        if int(interaction.user.id) != self.allowed_user_id:
            await interaction.response.send_message(
                "Apenas quem iniciou a sincronização pode responder esta seleção.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_select(self, interaction: Interaction):
        selected = self.selector.values[0]
        if selected == "create_new":
            self.create_new = True
            message = "✅ Opção selecionada: criar novo cargo."
        else:
            self.selected_role_id = int(selected)
            message = f"✅ Opção selecionada: usar cargo <@&{self.selected_role_id}>."

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=message, view=self)
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class DuplicateRoleStrategyView(discord.ui.View):
    def __init__(self, allowed_user_id: int):
        super().__init__(timeout=DUPLICATE_STRATEGY_TIMEOUT_SECONDS)
        self.allowed_user_id = int(allowed_user_id)
        self.strategy: Optional[str] = None

    async def interaction_check(self, interaction: Interaction) -> bool:
        if int(interaction.user.id) != self.allowed_user_id:
            await interaction.response.send_message(
                "Apenas quem iniciou a sincronização pode escolher esta opção.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Unificar cargos duplicados", style=discord.ButtonStyle.primary)
    async def merge_duplicates(self, interaction: Interaction, _: discord.ui.Button):
        self.strategy = "merge"
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="✅ Estratégia selecionada: unificar cargos duplicados.",
            view=self,
        )
        self.stop()

    @discord.ui.button(label="Decidir caso a caso", style=discord.ButtonStyle.secondary)
    async def decide_case_by_case(self, interaction: Interaction, _: discord.ui.Button):
        self.strategy = "manual"
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="✅ Estratégia selecionada: decidir caso a caso.",
            view=self,
        )
        self.stop()


class BackupCog(commands.Cog):
    DISCORD_MESSAGE_LIMIT = 2000

    @staticmethod
    def _is_missing_permissions_error(error: Exception) -> bool:
        return isinstance(error, discord.HTTPException) and int(getattr(error, "code", 0)) == 50013

    @classmethod
    def _split_warning_message(cls, prefix: str, items: list[str]) -> list[str]:
        unique_items = sorted(set(items))
        if not unique_items:
            return []

        messages: list[str] = []
        current_message = prefix
        separator = ", "

        for item in unique_items:
            addition = item if current_message == prefix else f"{separator}{item}"
            if len(current_message) + len(addition) <= cls.DISCORD_MESSAGE_LIMIT:
                current_message += addition
                continue

            if current_message != prefix:
                messages.append(current_message)
                current_message = f"{prefix}{item}"
                continue

            available_length = cls.DISCORD_MESSAGE_LIMIT - len(prefix)
            truncated_item = item[: max(0, available_length - 1)]
            messages.append(f"{prefix}{truncated_item}…")
            current_message = prefix

        if current_message != prefix:
            messages.append(current_message)

        return messages

    async def _send_warning_chunks(self, ctx: Interaction, prefix: str, items: list[str]) -> None:
        for message in self._split_warning_message(prefix, items):
            await ctx.followup.send(message, ephemeral=True)

    backup = app_commands.Group(name="backup", description="Comandos de backup da estrutura do servidor")
    migrate = app_commands.Group(name="migrate", description="Comandos de migração")
    migrate_permissions = app_commands.Group(
        name="permissoes",
        description="Comandos de migração de permissões",
        parent=migrate,
    )

    def __init__(self, bot: commands.Bot):
        initialize_discord_backup_tables()
        try:
            recovered = recover_completed_backup_snapshot_operations()
            if recovered:
                logging.warning(
                    "Operações de snapshot reconciliadas após finalização incompleta: %s",
                    recovered,
                )
        except Exception:
            logging.exception(
                "Falha ao reconciliar operações de snapshot incompletas no startup"
            )
        self.bot = bot
        self.backup_runtime = BackupRestoreEngine(bot)
        super().__init__()
        self._periodic_backup_loop.start()

    def cog_unload(self):
        self._periodic_backup_loop.cancel()

    def _build_guild_backup_snapshot(self, guild: discord.Guild) -> tuple[list[dict], list[dict], list[dict]]:
        return self.backup_runtime.build_guild_snapshot(guild)

    @tasks.loop(hours=6)
    async def _periodic_backup_loop(self):
        try:
            purged_guild_ids = purge_expired_discord_backup_guilds(
                active_guild_ids={int(guild.id) for guild in self.bot.guilds},
            )
            if purged_guild_ids:
                logging.info(
                    "Dados de Backup expirados removidos após janela de 7 dias: guilds=%s",
                    purged_guild_ids,
                )
        except Exception:
            logging.exception("Falha ao remover dados de Backup expirados")

        due_guild_ids = list_due_periodic_backup_guild_ids()
        for guild_id in due_guild_ids:
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                continue

            try:
                settings = get_discord_backup_settings(int(guild_id))
                frequency = str(settings.get("periodicity_frequency") or "weekly")
                await self.backup_runtime.create_snapshot(
                    guild,
                    name=f"Backup periódico automático - {guild.name}",
                    backup_type="periodic",
                    idempotency_key=periodic_idempotency_key(
                        int(guild.id),
                        frequency,
                    ),
                )
            except BackupBusyError:
                logging.info(
                    "Backup periódico ignorado por operação concorrente: guild=%s",
                    guild_id,
                )
            except ValueError as error:
                logging.error(
                    "Falha de validação ao gerar backup periódico do servidor %s: %s",
                    guild_id,
                    error,
                )
            except Exception:
                logging.exception(
                    "Falha ao gerar backup periódico automático do servidor %s",
                    guild_id,
                )

    @_periodic_backup_loop.before_loop
    async def _before_periodic_backup_loop(self):
        await self.bot.wait_until_ready()

    @migrate_permissions.command(
        name="canais",
        description="Migra permissões de um canal de origem para um canal alvo",
    )
    @app_commands.describe(
        canal_origem="Canal de onde as permissões serão copiadas",
        canal_alvo="Canal que receberá as permissões",
    )
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def migrate_permissions_between_channels(
        self,
        ctx: Interaction,
        canal_origem: (
            discord.TextChannel
            | discord.VoiceChannel
            | discord.StageChannel
            | discord.ForumChannel
            | discord.CategoryChannel
        ),
        canal_alvo: (
            discord.TextChannel
            | discord.VoiceChannel
            | discord.StageChannel
            | discord.ForumChannel
            | discord.CategoryChannel
        ),
    ):
        if not ctx.guild:
            return await ctx.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )

        if canal_origem.guild.id != ctx.guild.id or canal_alvo.guild.id != ctx.guild.id:
            return await ctx.response.send_message(
                "Os canais informados precisam pertencer ao servidor atual.",
                ephemeral=True,
            )

        if canal_origem.id == canal_alvo.id:
            return await ctx.response.send_message(
                "Escolha canais diferentes para origem e destino.",
                ephemeral=True,
            )

        await ctx.response.defer(ephemeral=True)

        reason = (
            f"Migração de permissões do canal {canal_origem.id} para {canal_alvo.id} "
            f"(solicitado por {ctx.user.id})"
        )

        failed_targets: list[str] = []
        removed_overwrites = 0
        migrated_overwrites = 0

        for target in list(canal_alvo.overwrites.keys()):
            try:
                await canal_alvo.set_permissions(target, overwrite=None, reason=reason)
                removed_overwrites += 1
            except (discord.Forbidden, discord.HTTPException) as error:
                if isinstance(error, discord.Forbidden) or self._is_missing_permissions_error(error):
                    failed_targets.append(getattr(target, "mention", str(target)))
                    continue
                raise

        for target, overwrite in canal_origem.overwrites.items():
            try:
                await canal_alvo.set_permissions(target, overwrite=overwrite, reason=reason)
                migrated_overwrites += 1
            except (discord.Forbidden, discord.HTTPException) as error:
                if isinstance(error, discord.Forbidden) or self._is_missing_permissions_error(error):
                    failed_targets.append(getattr(target, "mention", str(target)))
                    continue
                raise

        await ctx.followup.send(
            (
                "✅ Migração de permissões finalizada.\n"
                f"Canal origem: {canal_origem.mention}\n"
                f"Canal alvo: {canal_alvo.mention}\n"
                f"Sobrescritas removidas no alvo: **{removed_overwrites}**\n"
                f"Sobrescritas migradas da origem: **{migrated_overwrites}**"
            ),
            ephemeral=True,
        )

        if failed_targets:
            await self._send_warning_chunks(
                ctx,
                "⚠️ Não foi possível aplicar permissões para: ",
                failed_targets,
            )

    @backup.command(name="gerar", description="Gera um backup da estrutura suportada pelo Coddy")
    @app_commands.describe(nome_do_backup="Nome identificador do backup")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def generate_backup(self, ctx: Interaction, nome_do_backup: str):
        if not ctx.guild:
            return await ctx.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )

        await ctx.response.defer(ephemeral=True)
        try:
            snapshot = await self.backup_runtime.create_snapshot(
                ctx.guild,
                name=nome_do_backup,
                backup_type="normal",
                created_by_discord_user_id=int(ctx.user.id),
                idempotency_key=f"discord:{ctx.id}",
            )
        except BackupBusyError:
            return await ctx.followup.send(
                "Já existe uma operação de Backup em andamento neste servidor.",
                ephemeral=True,
            )
        except ValueError:
            return await ctx.followup.send(
                "Nome/configuração de backup inválido.",
                ephemeral=True,
            )

        summary = snapshot["summary"]
        await ctx.followup.send(
            (
                f"Backup estrutural `{snapshot['id']}` gerado com sucesso para **{ctx.guild.name}**.\n"
                f"{summary['roles']} cargos · {summary['channels']} canais/categorias · "
                f"{summary['permissionOverwrites']} overwrites de cargos.\n"
                "Mensagens, membros, atribuições de cargos, emojis/stickers e overwrites de usuário "
                "não fazem parte deste snapshot."
            ),
            ephemeral=True,
        )

    @backup.command(name="listar", description="Lista backups estruturais salvos do servidor atual")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_backups(self, ctx: Interaction):
        if not ctx.guild:
            return await ctx.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )

        backups = list_discord_backups(ctx.guild.id)
        if not backups:
            return await ctx.response.send_message(
                "Nenhum backup encontrado para este servidor.",
                ephemeral=True,
            )

        embed = discord.Embed(
            title=f"Backups estruturais de {ctx.guild.name}",
            description=(
                "O Coddy salva cargos, canais/categorias e overwrites de cargos suportados. "
                "Não inclui mensagens, membros, atribuições de cargos, emojis/stickers ou overwrites de usuário."
            ),
            color=discord.Color.blurple(),
        )
        for backup in backups[:25]:
            created_at = backup["created_at"].strftime("%d/%m/%Y %H:%M:%S")
            backup_type = (
                "Periódico"
                if str(backup.get("backup_type", "normal")) == "periodic"
                else "Manual"
            )
            embed.add_field(
                name=f"ID {backup['id']} - {backup['original_name']}",
                value=(
                    f"Tipo: {backup_type}\n"
                    f"Criado em: {created_at}\n"
                    f"{int(backup.get('role_count') or 0)} cargos · "
                    f"{int(backup.get('channel_count') or 0)} canais/categorias · "
                    f"{int(backup.get('overwrite_count') or 0)} overwrites"
                ),
                inline=False,
            )

        await ctx.response.send_message(embed=embed, ephemeral=True)

    @backup.command(name="sincronizar", description="Restaura um backup estrutural no servidor atual")
    @app_commands.describe(
        id_backup="ID do backup que será restaurado",
        escopo_restauracao="Define se restaura tudo ou apenas itens específicos",
    )
    @app_commands.choices(
        escopo_restauracao=[
            app_commands.Choice(name="Completo", value="full"),
            app_commands.Choice(name="Somente cargos", value="roles"),
            app_commands.Choice(name="Somente canais", value="channels"),
            app_commands.Choice(name="Somente permissões", value="permissions"),
        ]
    )
    async def synchronize_backup(
        self,
        ctx: Interaction,
        id_backup: int,
        escopo_restauracao: str = "full",
    ):
        if not ctx.guild:
            return await ctx.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
        if int(ctx.user.id) != int(ctx.guild.owner_id):
            return await ctx.response.send_message(
                "Apenas o dono do servidor pode usar este comando.",
                ephemeral=True,
            )

        try:
            preflight = self.backup_runtime.build_restore_preflight(
                ctx.guild,
                int(id_backup),
                escopo_restauracao,
            )
        except LookupError:
            return await ctx.response.send_message(
                "Backup não encontrado para este servidor.",
                ephemeral=True,
            )
        except ValueError:
            return await ctx.response.send_message(
                "Escopo de restauração inválido.",
                ephemeral=True,
            )

        hard_blockers = [
            blocker
            for blocker in preflight["blockers"]
            if blocker.get("code") != "ROLE_RESOLUTION_REQUIRED"
        ]
        if hard_blockers:
            lines = []
            for blocker in hard_blockers:
                code = blocker.get("code")
                if code == "BOT_ROLE_HIERARCHY":
                    roles = ", ".join(
                        f"<@&{item['id']}>"
                        for item in blocker.get("roles", [])[:10]
                    )
                    lines.append(
                        "O cargo mais alto do Coddy precisa ficar acima de todos os cargos "
                        f"do servidor. Bloqueadores: {roles or 'não identificado'}."
                    )
                elif code == "BOT_MISSING_PERMISSIONS":
                    lines.append(
                        "Permissões ausentes do Coddy: "
                        + ", ".join(blocker.get("permissions", []))
                    )
                else:
                    lines.append(str(code))
            return await ctx.response.send_message(
                "❌ O restore não pode começar:\n" + "\n".join(f"- {line}" for line in lines),
                ephemeral=True,
            )

        duplicate_roles = preflight.get("duplicateRoles", [])
        decision = {
            "duplicateStrategy": "explicit",
            "roleResolutions": {},
        }

        if duplicate_roles:
            allow_role_mutation = escopo_restauracao in {"full", "roles"}
            duplicate_embed = discord.Embed(
                title="⚠️ Cargos duplicados detectados",
                description=(
                    (
                        "O ID original de alguns cargos não existe mais e há múltiplos cargos "
                        "atuais com o mesmo nome. Escolha unificar as duplicatas ou decidir cada "
                        "mapeamento explicitamente. Nenhuma escolha arbitrária será feita."
                    )
                    if allow_role_mutation
                    else (
                        "O ID original de alguns cargos não existe mais e há múltiplos cargos "
                        "atuais com o mesmo nome. Como o escopo é somente permissões, escolha "
                        "explicitamente qual cargo existente deve receber cada overwrite. "
                        "Este escopo não cria, edita, unifica ou exclui cargos."
                    )
                ),
                color=discord.Color.orange(),
            )
            duplicate_embed.add_field(
                name="Duplicatas encontradas",
                value="\n".join(
                    (
                        f"**{item['name']}** → "
                        + ", ".join(
                            f"ID {candidate['id']} (posição {candidate['position']})"
                            for candidate in item["candidates"][:4]
                        )
                    )
                    for item in duplicate_roles[:10]
                ),
                inline=False,
            )

            manual_resolution = not allow_role_mutation
            if allow_role_mutation:
                strategy_view = DuplicateRoleStrategyView(allowed_user_id=ctx.user.id)
                await ctx.response.send_message(
                    embed=duplicate_embed,
                    view=strategy_view,
                    ephemeral=True,
                )
                await strategy_view.wait()
                if strategy_view.strategy not in {"merge", "manual"}:
                    return await ctx.followup.send(
                        "❌ Restore cancelado por falta de decisão sobre cargos duplicados.",
                        ephemeral=True,
                    )

                if strategy_view.strategy == "merge":
                    decision["duplicateStrategy"] = "merge"
                else:
                    manual_resolution = True
            else:
                await ctx.response.send_message(
                    embed=duplicate_embed,
                    ephemeral=True,
                )

            if manual_resolution:
                for duplicate in duplicate_roles:
                    options = [
                        discord.SelectOption(
                            label=f"Usar {candidate['name']}",
                            value=str(candidate["id"]),
                            description=(
                                f"ID {candidate['id']} | posição {candidate['position']}"
                            ),
                        )
                        for candidate in duplicate["candidates"][:24]
                    ]
                    if allow_role_mutation:
                        options.append(
                            discord.SelectOption(
                                label="Criar novo cargo com os dados do backup",
                                value="create_new",
                                description="Não usar nenhum dos cargos existentes",
                            )
                        )
                    selection = RoleDisambiguationView(
                        allowed_user_id=ctx.user.id,
                        options=options,
                    )
                    await ctx.followup.send(
                        (
                            f"Escolha o destino para **{duplicate['name']}** "
                            f"(cargo do backup #{duplicate['backupRoleId']})."
                        ),
                        view=selection,
                        ephemeral=True,
                    )
                    await selection.wait()
                    if selection.create_new and allow_role_mutation:
                        resolved = "CREATE_NEW"
                    elif selection.selected_role_id:
                        resolved = str(selection.selected_role_id)
                    else:
                        return await ctx.followup.send(
                            "❌ Restore cancelado por falta de resolução de cargo.",
                            ephemeral=True,
                        )
                    decision["roleResolutions"][
                        str(duplicate["backupRoleId"])
                    ] = resolved
        else:
            await ctx.response.send_message(
                "Preflight concluído sem ambiguidades de cargos. Prosseguindo para confirmação...",
                ephemeral=True,
            )

        warning_embed = discord.Embed(
            title="⚠️ Confirmação de restore estrutural",
            description=(
                "Esta ação pode alterar/criar cargos, categorias, canais e permission "
                "overwrites de cargos, conforme o escopo escolhido.\n\n"
                "**Não restaura** mensagens, membros, DMs, atribuições de cargos, "
                "emojis/stickers ou overwrites específicos de usuário."
            ),
            color=discord.Color.orange(),
        )
        warning_embed.add_field(
            name="Backup",
            value=f"{preflight['backup']['name']} (ID `{id_backup}`)",
            inline=False,
        )
        warning_embed.add_field(
            name="Escopo",
            value=escopo_restauracao,
            inline=True,
        )
        summary = preflight["summary"]
        warning_embed.add_field(
            name="Resumo",
            value=(
                f"{summary['roles']} cargos · {summary['channels']} canais/categorias · "
                f"{summary['permissionOverwrites']} overwrites"
            ),
            inline=False,
        )

        confirmation_view = SyncConfirmationView(allowed_user_id=ctx.user.id)
        await ctx.followup.send(
            embed=warning_embed,
            view=confirmation_view,
            ephemeral=True,
        )
        await confirmation_view.wait()
        if confirmation_view.decision is not True:
            return

        operation_id = create_backup_restore_operation(
            int(ctx.guild.id),
            int(id_backup),
            f"discord:{ctx.id}",
            escopo_restauracao,
            int(ctx.user.id),
            decision=decision,
        )
        if operation_id is None:
            return await ctx.followup.send(
                "Backup não encontrado para este servidor.",
                ephemeral=True,
            )

        await ctx.followup.send(
            f"Restore `{operation_id}` em andamento...",
            ephemeral=True,
        )
        await self.backup_runtime.run_restore_operation(
            ctx.guild,
            operation_id=int(operation_id),
            backup_id=int(id_backup),
            scope=escopo_restauracao,
            decision=decision,
        )

        operation = get_backup_restore_operation(int(operation_id), int(ctx.guild.id))
        if not operation:
            return await ctx.followup.send(
                "Não consegui reler o estado da operação de restore.",
                ephemeral=True,
            )

        status = str(operation.get("status") or "FAILED")
        try:
            result = json.loads(operation.get("result_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            result = {}

        if status == "FAILED":
            return await ctx.followup.send(
                "❌ Restore falhou: "
                + str(operation.get("error_code") or "falha não especificada"),
                ephemeral=True,
            )

        labels = {"SUCCEEDED": "✅ Concluído", "PARTIAL": "⚠️ Concluído parcialmente"}
        lines = [f"{labels.get(status, status)} — operação `{operation_id}`."]
        for domain, label in (
            ("roles", "Cargos"),
            ("channels", "Canais"),
            ("permissions", "Permissões"),
        ):
            bucket = result.get(domain) or {}
            lines.append(
                f"{label}: {int(bucket.get('created') or 0)} criados · "
                f"{int(bucket.get('updated') or 0)} atualizados · "
                f"{int(bucket.get('reused') or 0)} reutilizados · "
                f"{int(bucket.get('skipped') or 0)} ignorados · "
                f"{int(bucket.get('failed') or 0)} falharam"
            )
        await ctx.followup.send("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BackupCog(bot))
