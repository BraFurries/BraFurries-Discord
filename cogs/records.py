from discord.ext import commands, tasks
import asyncio
import discord
import aiohttp
from discord import app_commands
from datetime import datetime, timedelta
import json
import logging
from core.time_functions import now
from core.database import (
    updateVoiceRecord,
    getAllVoiceRecords,
    updateGameRecord,
    getAllGameRecords,
    getBlacklistedGames,
    addGameToBlacklist,
    getLogConfig,
    upsertActiveCallLog,
    getActiveCallLogs,
    deleteActiveCallLog,
)
from core.monthly_bumps import get_monthly_bump_records
from schemas.types.record_types import RecordTypes


class RecordsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.call_logs: dict[int, dict[int, list[tuple[datetime, datetime | None]]]] = {}
        self.call_log_messages: dict[int, int] = {}
        self.voice_session_starts: dict[tuple[int, int], datetime] = {}
        self.game_sessions: dict[tuple[int, int], tuple[datetime, str]] = {}
        self.blacklisted_games_by_guild: dict[int, set[str]] = {}
        self._blacklist_loads: dict[int, asyncio.Task[set[str]]] = {}
        super().__init__()
        self.save_records.start()

    async def _get_blacklisted_games(self, guild_id: int) -> set[str]:
        cached = self.blacklisted_games_by_guild.get(guild_id)
        if cached is not None:
            return cached

        existing_load = self._blacklist_loads.get(guild_id)
        if existing_load is None:
            async def _load_games() -> set[str]:
                games = await asyncio.to_thread(getBlacklistedGames, guild_id)
                return set(games)

            existing_load = asyncio.create_task(_load_games())
            self._blacklist_loads[guild_id] = existing_load

        try:
            loaded_games = await asyncio.shield(existing_load)
        finally:
            self._blacklist_loads.pop(guild_id, None)

        self.blacklisted_games_by_guild[guild_id] = loaded_games
        return loaded_games

    def _open_voice_interval(self, channel_id: int, user_id: int, started_at: datetime):
        channel_logs = self.call_logs.setdefault(channel_id, {})
        user_intervals = channel_logs.setdefault(user_id, [])
        user_intervals.append((started_at, None))

    def _close_voice_interval(self, channel_id: int, user_id: int, ended_at: datetime) -> datetime | None:
        channel_logs = self.call_logs.get(channel_id)
        if channel_logs is None:
            return None

        user_intervals = channel_logs.get(user_id)
        if not user_intervals:
            return None

        start, end = user_intervals[-1]
        if end is not None:
            return None

        user_intervals[-1] = (start, ended_at)
        return start

    def _iter_open_voice_intervals(self):
        for channel_id, channel_logs in self.call_logs.items():
            for user_id, intervals in channel_logs.items():
                if not intervals:
                    continue
                start, end = intervals[-1]
                if end is None:
                    yield channel_id, user_id, start


    def _serialize_call_log(self, channel_id: int) -> dict:
        payload: dict[str, dict[str, list[list[str | None]]]] = {"users": {}}
        channel_logs = self.call_logs.get(channel_id, {})
        for user_id, intervals in channel_logs.items():
            payload["users"][str(user_id)] = [
                [start.isoformat(), end.isoformat() if end else None]
                for start, end in intervals
            ]
        return payload

    def _deserialize_call_log(self, payload_raw: str | dict | None) -> dict[int, list[tuple[datetime, datetime | None]]]:
        if payload_raw is None:
            return {}

        try:
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
        except (TypeError, ValueError):
            return {}

        if not isinstance(payload, dict):
            return {}

        users_payload = payload.get("users", {})
        if not isinstance(users_payload, dict):
            return {}

        parsed: dict[int, list[tuple[datetime, datetime | None]]] = {}
        for user_id_raw, intervals in users_payload.items():
            if not isinstance(intervals, list):
                continue
            try:
                user_id = int(user_id_raw)
            except (TypeError, ValueError):
                continue

            parsed_intervals: list[tuple[datetime, datetime | None]] = []
            for interval in intervals:
                if not isinstance(interval, list) or len(interval) != 2:
                    continue
                start_raw, end_raw = interval
                if not isinstance(start_raw, str):
                    continue
                try:
                    start_dt = datetime.fromisoformat(start_raw)
                    end_dt = datetime.fromisoformat(end_raw) if isinstance(end_raw, str) else None
                except ValueError:
                    continue
                parsed_intervals.append((start_dt, end_dt))

            if parsed_intervals:
                parsed[user_id] = parsed_intervals

        return parsed

    def _persist_call_log_state(self, guild_id: int, channel_id: int):
        upsertActiveCallLog(
            guild_id,
            channel_id,
            self._serialize_call_log(channel_id),
            self.call_log_messages.get(channel_id),
        )

    async def _hydrate_active_call_logs(self, guild: discord.Guild):
        rows = getActiveCallLogs(guild.id)
        for row in rows:
            channel_id: int | None = None
            try:
                try:
                    channel_id = int(row["voice_channel_id"])
                except (TypeError, ValueError, KeyError):
                    continue

                intervals_by_user = self._deserialize_call_log(row.get("payload_json"))
                if not intervals_by_user:
                    deleteActiveCallLog(guild.id, channel_id)
                    continue

                self.call_logs[channel_id] = intervals_by_user
                message_id = row.get("log_message_id")
                if message_id is not None:
                    try:
                        self.call_log_messages[channel_id] = int(message_id)
                    except (TypeError, ValueError):
                        self.call_log_messages.pop(channel_id, None)

                voice_channel = guild.get_channel(channel_id)
                if self._is_voice_like_channel(voice_channel) and self._has_human_members(voice_channel):
                    await self._refresh_call_log(guild, channel_id)
                else:
                    await self._finalize_call_log(guild, channel_id)
            except Exception:
                logging.exception(
                    "Erro ao hidratar log de call ativo para guild=%s canal=%s.",
                    guild.id,
                    channel_id if channel_id is not None else "desconhecido",
                )

    def _format_call_log_embed(self, guild: discord.Guild, channel_id: int) -> discord.Embed:
        voice_channel = guild.get_channel(channel_id)
        channel_name = voice_channel.name if self._is_voice_like_channel(voice_channel) else f"Canal {channel_id}"
        embed = discord.Embed(
            title=f"Call ({channel_name})",
            color=discord.Color.blurple(),
        )

        channel_logs = self.call_logs.get(channel_id, {})
        if not channel_logs:
            embed.description = "(sem sessões registradas)"
            return embed

        lines: list[str] = []

        for user_id, intervals in sorted(channel_logs.items(), key=lambda item: item[0]):
            member = guild.get_member(user_id)
            display_name = member.display_name if member else f"Usuário {user_id}"
            mention = member.mention if member else f"<@{user_id}>"
            formatted_intervals: list[str] = []
            for start, end in intervals:
                start_text = start.strftime("%H:%M")
                end_text = end.strftime("%H:%M") if end else "agora"
                formatted_intervals.append(f"{start_text} - {end_text}")
            periods = " / ".join(formatted_intervals)
            lines.append(f"**{display_name}** ({mention})\nPeríodos: {periods}")

        embed.description = "\n\n".join(lines)
        return embed

    def _is_voice_like_channel(self, channel: discord.abc.GuildChannel | None) -> bool:
        return isinstance(channel, (discord.VoiceChannel, discord.StageChannel))

    def _has_human_members(self, channel: discord.VoiceChannel | discord.StageChannel | None) -> bool:
        if channel is None:
            return False
        return any(not member.bot for member in channel.members)

    def _get_call_log_channel_id(self, guild_id: int) -> int | None:
        from core.log_types import effective_call_config
        config = effective_call_config(getLogConfig, guild_id)
        if not config or not config.get("enabled") or not config.get("log_channel"):
            return None
        try:
            return int(config["log_channel"])
        except (TypeError, ValueError):
            return None

    async def _ensure_call_log_message(self, guild: discord.Guild, channel_id: int) -> discord.Message | None:
        log_channel_id = self._get_call_log_channel_id(guild.id)
        if log_channel_id is None:
            return None

        text_channel = guild.get_channel(log_channel_id)
        if text_channel is None:
            try:
                text_channel = await guild.fetch_channel(log_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, aiohttp.ClientError, OSError, RuntimeError):
                # RuntimeError can happen if the HTTP session is closing/restarting
                return None

        if not isinstance(text_channel, (discord.TextChannel, discord.Thread)):
            return None

        current_message_id = self.call_log_messages.get(channel_id)
        if current_message_id:
            try:
                return await text_channel.fetch_message(current_message_id)
            except discord.NotFound:
                self.call_log_messages.pop(channel_id, None)
            except (discord.Forbidden, discord.HTTPException, aiohttp.ClientError, OSError, RuntimeError):
                return None

        try:
            message = await text_channel.send(embed=self._format_call_log_embed(guild, channel_id))
        except (discord.Forbidden, discord.HTTPException, aiohttp.ClientError, OSError, RuntimeError):
            return None

        self.call_log_messages[channel_id] = message.id
        return message

    async def _refresh_call_log(self, guild: discord.Guild, channel_id: int):
        message = await self._ensure_call_log_message(guild, channel_id)
        if message is None:
            self._persist_call_log_state(guild.id, channel_id)
            return
        try:
            await message.edit(content=None, embed=self._format_call_log_embed(guild, channel_id))
            self.call_log_messages[channel_id] = message.id
        except (discord.Forbidden, discord.NotFound, discord.HTTPException, aiohttp.ClientError, OSError, RuntimeError):
            self.call_log_messages.pop(channel_id, None)
        self._persist_call_log_state(guild.id, channel_id)

    async def _finalize_call_log(self, guild: discord.Guild, channel_id: int):
        try:
            await self._refresh_call_log(guild, channel_id)
        finally:
            deleteActiveCallLog(guild.id, channel_id)
            self.call_log_messages.pop(channel_id, None)
            self.call_logs.pop(channel_id, None)

    def cog_unload(self):
        self.save_records.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            try:
                await self._hydrate_active_call_logs(guild)
            except Exception:
                logging.exception(
                    "Erro ao restaurar logs de call ativos do banco de dados (guild=%s).",
                    guild.id,
                )

    @app_commands.command(name='recordes', description='Mostra os recordes do servidor')
    async def showRecords(self, ctx: discord.Interaction, tipo: RecordTypes = None):
        await ctx.response.defer()
        if tipo == "Tempo em call":
            records = getAllVoiceRecords(ctx.guild.id, limit=10)
            if not records:
                await ctx.followup.send(content='Nenhum recorde registrado.')
                return

            embed = discord.Embed(
                title='Recordes de tempo em call',
                color=discord.Color.blue(),
            )

            for index, record in enumerate(records, start=1):
                member = ctx.guild.get_member(record['user_id'])
                if member is None:
                    continue
                duration = str(timedelta(seconds=record['seconds']))
                embed.add_field(
                    name=f'{index}. {member.display_name}',
                    value=duration,
                    inline=False
                )

            await ctx.followup.send(embed=embed)
        elif tipo == "Tempo em jogo":
            guild_blacklist = list(await self._get_blacklisted_games(ctx.guild.id))
            records = getAllGameRecords(ctx.guild.id, limit=10, blacklist=guild_blacklist)
            if not records:
                await ctx.followup.send(content='Nenhum recorde registrado.')
                return

            embed = discord.Embed(
                title='Recordes de tempo em jogo',
                color=discord.Color.blue(),
            )

            for index, record in enumerate(records, start=1):
                member = ctx.guild.get_member(record['user_id'])
                if member is None:
                    continue
                duration = str(timedelta(seconds=record['seconds']))
                game_name = record.get('game', '')
                display = f'{member.display_name} - {game_name}' if game_name else member.display_name
                embed.add_field(
                    name=f'{index}. {display}',
                    value=duration,
                    inline=False
                )

            await ctx.followup.send(embed=embed)
        elif tipo == "Bumps":
            records = get_monthly_bump_records(ctx.guild.id, limit=10)
            if not records:
                await ctx.followup.send(content='Nenhum recorde registrado.')
                return

            embed = discord.Embed(
                title='Recordes de bumps do mês',
                color=discord.Color.blue(),
            )

            for index, record in enumerate(records, start=1):
                member = ctx.guild.get_member(record['user_id'])
                if member is None:
                    continue
                embed.add_field(
                    name=f'{index}. {member.display_name}',
                    value=str(record['bumps']),
                    inline=False
                )

            await ctx.followup.send(embed=embed)
        else:
            voice_records = getAllVoiceRecords(ctx.guild.id, limit=3)
            guild_blacklist = list(await self._get_blacklisted_games(ctx.guild.id))
            game_records = getAllGameRecords(ctx.guild.id, limit=3, blacklist=guild_blacklist)
            bump_records = get_monthly_bump_records(ctx.guild.id, limit=3)
            if not voice_records and not game_records and not bump_records:
                await ctx.followup.send(content='Nenhum recorde registrado.')
                return

            embeds: list[discord.Embed] = []

            if voice_records:
                embed = discord.Embed(
                    title='Tempo em call',
                    color=discord.Color.blue(),
                )
                for index, record in enumerate(voice_records, start=1):
                    member = ctx.guild.get_member(record['user_id'])
                    if member is None:
                        continue
                    duration = str(timedelta(seconds=record['seconds']))
                    embed.add_field(
                        name=f'{index}. {member.display_name}',
                        value=duration,
                        inline=False,
                    )
                embeds.append(embed)

            if game_records:
                embed = discord.Embed(
                    title='Tempo em jogo',
                    color=discord.Color.blue(),
                )
                for index, record in enumerate(game_records, start=1):
                    member = ctx.guild.get_member(record['user_id'])
                    if member is None:
                        continue
                    duration = str(timedelta(seconds=record['seconds']))
                    game_name = record.get('game', '')
                    display = f'{member.display_name} - {game_name}' if game_name else member.display_name
                    embed.add_field(
                        name=f'{index}. {display}',
                        value=duration,
                        inline=False,
                    )
                embeds.append(embed)

            if bump_records:
                embed = discord.Embed(
                    title='Bumps',
                    color=discord.Color.blue(),
                )
                for index, record in enumerate(bump_records, start=1):
                    member = ctx.guild.get_member(record['user_id'])
                    if member is None:
                        continue
                    embed.add_field(
                        name=f'{index}. {member.display_name}',
                        value=str(record['bumps']),
                        inline=False,
                    )
                embeds.append(embed)

            await ctx.followup.send(content='# Recordes do servidor', embeds=embeds)

    @app_commands.command(name='recorde_adicionar_blacklist', description='Adiciona um jogo à blacklist de recordes')
    async def addRecordBlacklist(self, ctx: discord.Interaction, *, jogo: str):
        await ctx.response.defer()
        if addGameToBlacklist(ctx.guild.id, jogo):
            (await self._get_blacklisted_games(ctx.guild.id)).add(jogo)
            # remove any ongoing sessions for this game
            for session_key, (start, game_name) in list(self.game_sessions.items()):
                guild_id, _ = session_key
                if guild_id == ctx.guild.id and game_name == jogo:
                    self.game_sessions.pop(session_key, None)
            await ctx.followup.send(content=f'Jogo "{jogo}" adicionado à blacklist!')
        else:
            await ctx.followup.send(content='Não foi possível adicionar o jogo à blacklist.', ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return

        if before.channel == after.channel:
            return

        current_time = now()
        member_key = (member.guild.id, member.id)
        guild_blacklist = await self._get_blacklisted_games(member.guild.id)

        # Member joined a voice channel
        if before.channel is None and after.channel is not None:
            self._open_voice_interval(after.channel.id, member.id, current_time)
            self.voice_session_starts.setdefault(member_key, current_time)
            game_name = next((a.name for a in member.activities if a.type == discord.ActivityType.playing), None)
            if game_name and game_name not in guild_blacklist:
                self.game_sessions[member_key] = (current_time, game_name)
            await self._refresh_call_log(member.guild, after.channel.id)
        # Member left voice channel
        elif before.channel is not None and after.channel is None:
            self._close_voice_interval(before.channel.id, member.id, current_time)
            session_start = self.voice_session_starts.pop(member_key, None)
            if session_start:
                seconds = int((current_time - session_start).total_seconds())
                await asyncio.to_thread(updateVoiceRecord, member.guild.id, member, seconds)
            session = self.game_sessions.pop(member_key, None)
            if session:
                start, game_name = session
                seconds = int((current_time - start).total_seconds())
                await asyncio.to_thread(updateGameRecord, member.guild.id, member, seconds, game_name)

            if self._has_human_members(before.channel):
                await self._refresh_call_log(member.guild, before.channel.id)
            else:
                await self._finalize_call_log(member.guild, before.channel.id)
        # Member switched channels
        elif before.channel != after.channel:
            self._close_voice_interval(before.channel.id, member.id, current_time)
            self._open_voice_interval(after.channel.id, member.id, current_time)

            if self._has_human_members(before.channel):
                await self._refresh_call_log(member.guild, before.channel.id)
            else:
                await self._finalize_call_log(member.guild, before.channel.id)

            await self._refresh_call_log(member.guild, after.channel.id)

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if after.bot:
            return
        before_game = next((a.name for a in before.activities if a.type == discord.ActivityType.playing), None)
        after_game = next((a.name for a in after.activities if a.type == discord.ActivityType.playing), None)

        if before_game == after_game:
            return

        guild_blacklist = await self._get_blacklisted_games(after.guild.id)
        if before_game in guild_blacklist:
            before_game = None
        if after_game in guild_blacklist:
            after_game = None
        in_voice = after.voice and after.voice.channel is not None
        member_key = (after.guild.id, after.id)
        session = self.game_sessions.get(member_key)

        if session:
            start, current_game = session
            if after_game is None or not in_voice or after_game != current_game:
                seconds = int((now() - start).total_seconds())
                await asyncio.to_thread(updateGameRecord, after.guild.id, after, seconds, current_game)
                self.game_sessions.pop(member_key, None)
                session = None

        if after_game is not None and in_voice and session is None:
            self.game_sessions[member_key] = (now(), after_game)

    @tasks.loop(minutes=5)
    async def save_records(self):
        if not self.call_logs and not self.voice_session_starts and not self.game_sessions:
            return
        for (guild_id, user_id), start in list(self.voice_session_starts.items()):
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            member = guild.get_member(user_id)
            if member is None or member.bot:
                continue
            seconds = int((now() - start).total_seconds())
            if seconds > 0:
                await asyncio.to_thread(updateVoiceRecord, guild.id, member, seconds)
        for (guild_id, user_id), (start, game_name) in list(self.game_sessions.items()):
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            member = guild.get_member(user_id)
            if member is None or member.bot:
                continue
            if game_name in await self._get_blacklisted_games(guild_id):
                continue
            seconds = int((now() - start).total_seconds())
            if seconds > 0:
                await asyncio.to_thread(updateGameRecord, guild.id, member, seconds, game_name)


async def setup(bot: commands.Bot):
    await bot.add_cog(RecordsCog(bot))
