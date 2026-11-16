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
    backup = app_commands.Group(name="backup", description="Comandos de backup da estrutura do servidor")
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
