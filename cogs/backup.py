import logging

import discord
from discord import Interaction, app_commands
from discord.ext import commands
from discord.ext import tasks

from core.database import (
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
                "Nenhum backup encontrado para este servidor.\n"
                "Para gerar um backup manual ou ativar backups automáticos, "
                "acesse o site da BraFurries em **Painel do Coddy → Backups**.",
                ephemeral=True,
            )

        embed = discord.Embed(
            title=f"Backups estruturais de {ctx.guild.name}",
            description=(
                "O Coddy salva cargos, canais/categorias e overwrites de cargos suportados. "
                "Não inclui mensagens, membros, atribuições de cargos, emojis/stickers ou overwrites de usuário.\n\n"
                "Para gerar backups manuais ou ativar backups automáticos, "
                "acesse o site da BraFurries em **Painel do Coddy → Backups**."
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

async def setup(bot: commands.Bot):
    await bot.add_cog(BackupCog(bot))
