from discord.ext import commands, tasks
import discord
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Tuple
from dataclasses import dataclass

from core.time_functions import now
from core.monthly_activity import (
    add_time_batch,
    cleanup_old_weekly_entries,
)
from core.database import getTrendingPresenceEnabledBatch


@dataclass
class GameSession:
    start: datetime
    last_update: datetime
    game_name: str

class TrendingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Active game session per guild/member.
        self.sessions: Dict[Tuple[int, int], GameSession] = {}
        # Seconds accumulated since last flush by (guild_id, user_id, game_name, update_time).
        self.accumulated_seconds: Dict[Tuple[int, int, str, datetime], int] = {}
        self.trending_enabled_cache: dict[int, bool] = {}
        self._refreshing_guilds: set[int] = set()
        self._accumulator_lock = asyncio.Lock()
        super().__init__()
        self.flush_activity.start()
        self.cleanup_weekly.start()
        self.refresh_trending_cache.start()

    async def cog_load(self):
        await self._warmup_trending_cache()

    def cog_unload(self):
        self.flush_activity.cancel()
        self.cleanup_weekly.cancel()
        self.refresh_trending_cache.cancel()

    async def _warmup_trending_cache(self):
        guild_ids = [guild.id for guild in self.bot.guilds]
        if not guild_ids:
            return
        enabled_by_guild = await asyncio.to_thread(getTrendingPresenceEnabledBatch, guild_ids)
        self.trending_enabled_cache.update(enabled_by_guild)

    async def refresh_guild_trending_cache(self, guild_id: int):
        if guild_id in self._refreshing_guilds:
            return
        self._refreshing_guilds.add(guild_id)
        try:
            enabled_by_guild = await asyncio.to_thread(getTrendingPresenceEnabledBatch, [guild_id])
            self.trending_enabled_cache.update(enabled_by_guild)
        finally:
            self._refreshing_guilds.discard(guild_id)

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        before_game = next((a.name for a in before.activities if a.type == discord.ActivityType.playing), None)
        after_game = next((a.name for a in after.activities if a.type == discord.ActivityType.playing), None)

        if after.guild is None:
            return
        guild_id = after.guild.id
        enabled = self.trending_enabled_cache.get(guild_id)
        if enabled is None:
            # Safe default: keep tracking disabled until cache is hydrated.
            self.bot.loop.create_task(self.refresh_guild_trending_cache(guild_id))
            return
        if not enabled:
            return

        key = (guild_id, after.id)
        current = now()
        async with self._accumulator_lock:
            session = self.sessions.get(key)
            if session:
                self._accumulate_elapsed(guild_id, after.id, session.game_name, session.last_update, current)
                session.last_update = current

            if after_game is None:
                self.sessions.pop(key, None)
                return

            if session is None or session.game_name != after_game:
                self.sessions[key] = GameSession(current, current, after_game)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.refresh_guild_trending_cache(guild.id)

    @tasks.loop(minutes=5)
    async def flush_activity(self):
        current = now()
        flush_payload: Dict[Tuple[int, int, str, datetime], int] = {}
        async with self._accumulator_lock:
            stale_sessions: list[tuple[int, int]] = []
            for key, session in self.sessions.items():
                guild_id, user_id = key
                guild = self.bot.get_guild(guild_id)
                if guild is None or guild.get_member(user_id) is None:
                    stale_sessions.append(key)
                    continue

                self._accumulate_elapsed(guild_id, user_id, session.game_name, session.last_update, current)
                session.last_update = current

            for stale_key in stale_sessions:
                self.sessions.pop(stale_key, None)

            if not self.accumulated_seconds:
                return

            flush_payload = dict(self.accumulated_seconds)
            self.accumulated_seconds.clear()

        db_entries = [
            (game_name, seconds, guild_id, update_time)
            for (guild_id, _, game_name, update_time), seconds in flush_payload.items()
            if seconds > 0
        ]
        if not db_entries:
            return

        try:
            await asyncio.to_thread(add_time_batch, db_entries)
        except Exception:
            async with self._accumulator_lock:
                for accumulation_key, seconds in flush_payload.items():
                    self.accumulated_seconds[accumulation_key] = (
                        self.accumulated_seconds.get(accumulation_key, 0) + seconds
                    )
            raise

    def _accumulate_elapsed(
        self,
        guild_id: int,
        user_id: int,
        game_name: str,
        start: datetime,
        end: datetime,
    ) -> None:
        for segment_start, segment_end in self._split_interval_by_boundaries(start, end):
            elapsed_seconds = int((segment_end - segment_start).total_seconds())
            if elapsed_seconds <= 0:
                continue
            accumulation_key = (guild_id, user_id, game_name, segment_start)
            self.accumulated_seconds[accumulation_key] = (
                self.accumulated_seconds.get(accumulation_key, 0) + elapsed_seconds
            )

    def _split_interval_by_boundaries(
        self,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, datetime]]:
        if end <= start:
            return []

        intervals: list[tuple[datetime, datetime]] = []
        cursor = start
        while cursor < end:
            next_week = (cursor - timedelta(days=cursor.weekday())).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ) + timedelta(days=7)
            if cursor.month == 12:
                next_month = cursor.replace(
                    year=cursor.year + 1,
                    month=1,
                    day=1,
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
            else:
                next_month = cursor.replace(
                    month=cursor.month + 1,
                    day=1,
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )

            boundary = min(next_week, next_month, end)
            intervals.append((cursor, boundary))
            cursor = boundary

        return intervals

    @tasks.loop(hours=24)
    async def cleanup_weekly(self):
        if now().weekday() != 0:
            return
        cleanup_old_weekly_entries()

    @tasks.loop(minutes=15)
    async def refresh_trending_cache(self):
        await self._warmup_trending_cache()

    @refresh_trending_cache.before_loop
    async def before_refresh_trending_cache(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(TrendingCog(bot))
