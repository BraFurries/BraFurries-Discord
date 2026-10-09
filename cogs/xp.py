import asyncio
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
from math import ceil

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.database import (
    async_pooled_connection,
    async_delete_voice_session,
    async_get_level_config,
    async_get_pending_level_reconciliation_guild_ids,
    async_get_voice_sessions,
    async_update_level_config,
    async_upsert_voice_session,
    _ensure_voice_sessions_table,
    async_getUserId,
    async_includeUser,
    pooled_connection,
)
from core.levels import apply_xp_multipliers, level_from_total_xp, xp_to_reach_level
from core.xp_policy import XpPolicy
from core.xp_reconciliation import reconcile_guild_levels_serialized


class XpCog(commands.Cog):
    admin_xp = app_commands.Group(name="admin-xp", description="Comandos administrativos de xp")
    rp = app_commands.Group(name="rp", description="Comandos de roleplay")

    _LEVEL_FIELD_MAP: dict[str, str] = {
        "levelup_warning": "levelup_warning",
        "phase1_k": "phase1_k",
        "phase1_p": "phase1_p",
        "phase1_b": "phase1_b",
        "multiplier": "multiplier",
        "daily_combo": "daily_combo",
        "combo_multiplier": "combo_multiplier",
        "xp_base_per_min": "xp_base_per_min",
        "voice_social_bonus_pct": "voice_social_bonus_pct",
        "voice_social_bonus_min_humans": "voice_social_bonus_min_humans",
        "voice_diminishing_window1_minutes": "voice_diminishing_window1_minutes",
        "voice_diminishing_window2_minutes": "voice_diminishing_window2_minutes",
        "voice_diminishing_factor2": "voice_diminishing_factor2",
        "voice_diminishing_factor3": "voice_diminishing_factor3",
        "voice_daily_cap_xp": "voice_daily_cap_xp",
        "text_daily_cap_xp": "text_daily_cap_xp",
        "global_daily_cap_xp": "global_daily_cap_xp",
    }
    _LEVELUP_MESSAGE_CLEAR_TOKENS: set[str] = {"--", "++", "[[clear]]"}

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_sessions: dict[tuple[int, int], dict] = {}
        self._voice_sessions_hydrated = False
        super().__init__()
        _ensure_voice_sessions_table()
        self._ensure_voice_xp_audit_tables()

    def cog_unload(self):
        self.voice_xp_tick.cancel()
        self.level_reconcile_tick.cancel()

    @staticmethod
    def _utcnow_naive() -> datetime:
        return datetime.utcnow().replace(microsecond=0)

    @staticmethod
    def _ensure_voice_xp_audit_tables() -> None:
        with pooled_connection() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS voice_xp_audit_log (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    server_guild_id BIGINT NOT NULL,
                    discord_user_id BIGINT NOT NULL,
                    voice_channel_id BIGINT NULL,
                    event_type VARCHAR(64) NOT NULL,
                    xp_amount INT NOT NULL DEFAULT 0,
                    eligible_seconds INT NOT NULL DEFAULT 0,
                    ineligible_seconds INT NOT NULL DEFAULT 0,
                    meta_json JSON NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    KEY idx_voice_xp_audit_guild_day (server_guild_id, created_at),
                    KEY idx_voice_xp_audit_user_day (server_guild_id, discord_user_id, created_at)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS voice_xp_daily_metrics (
                    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    metric_day DATE NOT NULL,
                    server_guild_id BIGINT NOT NULL,
                    modality VARCHAR(32) NOT NULL,
                    voice_channel_id BIGINT NULL,
                    total_xp BIGINT NOT NULL DEFAULT 0,
                    grants_count BIGINT NOT NULL DEFAULT 0,
                    blocks_count BIGINT NOT NULL DEFAULT 0,
                    eligible_seconds BIGINT NOT NULL DEFAULT 0,
                    ineligible_seconds BIGINT NOT NULL DEFAULT 0,
                    UNIQUE KEY uniq_voice_xp_daily_metrics (metric_day, server_guild_id, modality, voice_channel_id)
                )
                """
            )

    async def _register_voice_xp_audit(self, guild_id: int, user_id: int, channel_id: int | None, event_type: str, xp_amount: int = 0, eligible_seconds: int = 0, ineligible_seconds: int = 0, meta_json: str | None = None) -> None:
        async with async_pooled_connection() as cursor:
            await cursor.execute(
                """
                INSERT INTO voice_xp_audit_log
                    (server_guild_id, discord_user_id, voice_channel_id, event_type, xp_amount, eligible_seconds, ineligible_seconds, meta_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (guild_id, user_id, channel_id, event_type, int(xp_amount), int(eligible_seconds), int(ineligible_seconds), meta_json),
            )
            await cursor.execute(
                """
                INSERT INTO voice_xp_daily_metrics
                    (metric_day, server_guild_id, modality, voice_channel_id, total_xp, grants_count, blocks_count, eligible_seconds, ineligible_seconds)
                VALUES
                    (UTC_DATE(), %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    total_xp = total_xp + VALUES(total_xp),
                    grants_count = grants_count + VALUES(grants_count),
                    blocks_count = blocks_count + VALUES(blocks_count),
                    eligible_seconds = eligible_seconds + VALUES(eligible_seconds),
                    ineligible_seconds = ineligible_seconds + VALUES(ineligible_seconds)
                """,
                (
                    guild_id,
                    event_type,
                    channel_id,
                    int(xp_amount),
                    1 if event_type == "voice_xp_awarded" else 0,
                    1 if event_type != "voice_xp_awarded" else 0,
                    int(eligible_seconds),
                    int(ineligible_seconds),
                ),
            )

    async def _hydrate_voice_sessions(self, guild: discord.Guild) -> None:
        rows = await async_get_voice_sessions(guild.id)
        for row in rows:
            key = (guild.id, int(row["discord_user_id"]))
            self.voice_sessions[key] = {
                "guild_id": guild.id,
                "user_id": int(row["discord_user_id"]),
                "channel_id": int(row["voice_channel_id"]),
                "started_at": row["started_at"],
                "last_tick_at": row["last_tick_at"],
                "is_eligible": bool(row.get("is_eligible")),
                "is_self_muted": bool(row.get("is_self_muted")),
                "is_self_deafened": bool(row.get("is_self_deafened")),
                "is_server_muted": bool(row.get("is_server_muted")),
                "is_server_deafened": bool(row.get("is_server_deafened")),
            }


    async def cog_load(self):
        if self.bot.is_ready() and not self._voice_sessions_hydrated:
            self.voice_sessions.clear()
            for guild in self.bot.guilds:
                await self._hydrate_voice_sessions(guild)
            self._voice_sessions_hydrated = True
        if not self.voice_xp_tick.is_running():
            self.voice_xp_tick.start()
        if not self.level_reconcile_tick.is_running():
            self.level_reconcile_tick.start()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._voice_sessions_hydrated:
            self.voice_sessions.clear()
        for guild in self.bot.guilds:
            await self._hydrate_voice_sessions(guild)
        self._voice_sessions_hydrated = True
        if not self.voice_xp_tick.is_running():
            self.voice_xp_tick.start()
        if not self.level_reconcile_tick.is_running():
            self.level_reconcile_tick.start()

    @tasks.loop(seconds=60)
    async def level_reconcile_tick(self):
        active_guild_ids = {guild.id for guild in self.bot.guilds}
        pending_guild_ids = await async_get_pending_level_reconciliation_guild_ids(
            active_guild_ids,
            limit=2,
        )
        for guild_id in pending_guild_ids:
            try:
                await reconcile_guild_levels_serialized(guild_id)
            except Exception:
                logging.exception(
                    "Falha ao reconciliar current_level da guild %s; marcador continuará pendente.",
                    guild_id,
                )

    async def _open_voice_session(self, member: discord.Member, channel_id: int) -> None:
        now = self._utcnow_naive()
        key = (member.guild.id, member.id)
        session = {
            "guild_id": member.guild.id,
            "user_id": member.id,
            "channel_id": channel_id,
            "started_at": now,
            "last_tick_at": now,
            "is_eligible": False,
            "is_self_muted": bool(member.voice.self_mute) if member.voice else False,
            "is_self_deafened": bool(member.voice.self_deaf) if member.voice else False,
            "is_server_muted": bool(member.voice.mute) if member.voice else False,
            "is_server_deafened": bool(member.voice.deaf) if member.voice else False,
        }
        self.voice_sessions[key] = session
        await async_upsert_voice_session(session)

    async def _close_voice_session(self, guild_id: int, user_id: int) -> None:
        self.voice_sessions.pop((guild_id, user_id), None)
        await async_delete_voice_session(guild_id, user_id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        if before.channel is None and after.channel is not None:
            await self._open_voice_session(member, after.channel.id)
            return
        if before.channel is not None and after.channel is None:
            await self._close_voice_session(member.guild.id, member.id)
            return
        if before.channel and after.channel and before.channel.id != after.channel.id:
            await self._close_voice_session(member.guild.id, member.id)
            await self._open_voice_session(member, after.channel.id)
            return
        key = (member.guild.id, member.id)
        session = self.voice_sessions.get(key)
        if session:
            session["is_self_muted"] = bool(after.self_mute)
            session["is_self_deafened"] = bool(after.self_deaf)
            session["is_server_muted"] = bool(after.mute)
            session["is_server_deafened"] = bool(after.deaf)
            await async_upsert_voice_session(session)


    @staticmethod
    def is_voice_minute_eligible(session_state: dict) -> float:
        """Return XP factor for current voice window: 0.0, partial (0-1), or 1.0."""
        voice_channel = session_state.get("voice_channel")
        member: discord.Member | None = session_state.get("member")
        if member is None or member.bot:
            return 0.0

        if voice_channel is None:
            return 0.0

        afk_channel = getattr(member.guild, "afk_channel", None)
        if afk_channel is not None and voice_channel.id == afk_channel.id:
            return 0.0

        humans_in_channel = int(session_state.get("humans_in_channel") or 0)
        if humans_in_channel < max(1, int(session_state.get("min_humans", 1))):
            return 0.0

        account_age_seconds_min = int(session_state.get("min_account_age_seconds") or 0)
        guild_join_age_seconds_min = int(session_state.get("min_guild_join_age_seconds") or 0)
        now = None
        if account_age_seconds_min > 0 or guild_join_age_seconds_min > 0:
            now = session_state.get("now")
            if now is None:
                raise ValueError(
                    "session_state must include a 'now' datetime when account or guild join age checks are enabled"
                )

        if account_age_seconds_min > 0:
            created_at = member.created_at
            if created_at and (now - created_at.replace(tzinfo=None)).total_seconds() < account_age_seconds_min:
                return 0.0

        if guild_join_age_seconds_min > 0:
            joined_at = member.joined_at
            if joined_at is None:
                return 0.0
            if (now - joined_at.replace(tzinfo=None)).total_seconds() < guild_join_age_seconds_min:
                return 0.0

        is_self_deafened = bool(session_state.get("is_self_deafened"))
        if is_self_deafened:
            return 0.0

        is_self_muted = bool(session_state.get("is_self_muted"))
        if is_self_muted:
            return max(0.0, min(1.0, float(session_state.get("self_mute_xp_factor", 0.5))))

        return 1.0

    @staticmethod
    def _voice_diminishing_factor(elapsed_seconds: int, config: dict) -> float:
        minutes = max(1, elapsed_seconds // 60)
        window1 = max(1, int(config.get("voiceDiminishingWindow1Minutes") or 60))
        window2 = max(window1, int(config.get("voiceDiminishingWindow2Minutes") or 120))
        if minutes <= window1:
            return 1.0
        if minutes <= window2:
            return max(0.0, float(config.get("voiceDiminishingFactor2") or Decimal("0.6")))
        return max(0.0, float(config.get("voiceDiminishingFactor3") or Decimal("0.3")))

    @tasks.loop(seconds=60)
    async def voice_xp_tick(self):
        now = self._utcnow_naive()
        for guild in self.bot.guilds:
            for voice_channel in guild.voice_channels:
                humans = [m for m in voice_channel.members if not m.bot]
                if not humans:
                    continue
                for member in humans:
                    key = (guild.id, member.id)
                    session = self.voice_sessions.get(key)
                    if session is None:
                        await self._open_voice_session(member, voice_channel.id)
                        session = self.voice_sessions.get(key)
                        if session is None:
                            continue
                    voice_state = member.voice
                    level_config = await async_get_level_config(guild.id)
                    if not bool(level_config.get("voiceXpEnabled", True)):
                        session["is_eligible"] = False
                        session["last_tick_at"] = now
                        session["started_at"] = now
                        session["is_self_muted"] = bool(member.voice.self_mute) if member.voice else False
                        session["is_self_deafened"] = bool(member.voice.self_deaf) if member.voice else False
                        session["is_server_muted"] = bool(member.voice.mute) if member.voice else False
                        session["is_server_deafened"] = bool(member.voice.deaf) if member.voice else False
                        await async_upsert_voice_session(session)
                        continue

                    min_social_humans = max(2, int(level_config.get("voiceSocialBonusMinHumans") or 2))
                    xp_factor = self.is_voice_minute_eligible(
                        {
                            "member": member,
                            "voice_channel": voice_channel,
                            "humans_in_channel": len(humans),
                            "now": now,
                            "is_self_muted": bool(voice_state.self_mute) if voice_state else False,
                            "is_self_deafened": bool(voice_state.self_deaf) if voice_state else False,
                            # Configuráveis por servidor no futuro (defaults seguros):
                            "self_mute_xp_factor": 0.5,
                            "min_humans": 1,
                            "min_account_age_seconds": 0,
                            "min_guild_join_age_seconds": 0,
                        }
                    )
                    session["is_eligible"] = xp_factor > 0
                    session["is_self_muted"] = bool(member.voice.self_mute) if member.voice else False
                    session["is_self_deafened"] = bool(member.voice.self_deaf) if member.voice else False
                    session["is_server_muted"] = bool(member.voice.mute) if member.voice else False
                    session["is_server_deafened"] = bool(member.voice.deaf) if member.voice else False

                    elapsed = max(0, int((now - session["last_tick_at"]).total_seconds()))
                    session_elapsed = max(0, int((now - session.get("started_at", session["last_tick_at"])).total_seconds()))
                    consumed_seconds = 0
                    if xp_factor > 0 and elapsed > 0:
                        whole_minutes = elapsed // 60
                        consumed_seconds = whole_minutes * 60
                        base_per_min = max(0, int(level_config.get("xpBasePerMin") or 0))
                        base_xp = max(0, whole_minutes * base_per_min)
                        social_bonus_multiplier = 1.0
                        if len(humans) >= min_social_humans:
                            social_bonus_multiplier += max(0.0, float(level_config.get("voiceSocialBonusPct") or 0))
                        diminishing_factor = self._voice_diminishing_factor(session_elapsed, level_config)
                        activity_factor = float(Decimal(str(xp_factor)) * Decimal(str(social_bonus_multiplier)) * Decimal(str(diminishing_factor)))
                        if base_xp > 0 and activity_factor > 0:
                            db_user_id = await async_getUserId(member.id)
                            if db_user_id is None:
                                try:
                                    db_user_id = await async_includeUser(member, guild.id)
                                except Exception:
                                    db_user_id = None
                            if db_user_id is not None:
                                async with async_pooled_connection() as cursor:
                                    await cursor.execute(
                                        """
                                        INSERT INTO user_level (
                                            user_id, server_guild_id, total_xp, current_level, xp_awarded_voice_today, xp_awarded_voice_day
                                        )
                                        VALUES (%s, %s, 0, 0, 0, UTC_DATE())
                                        ON DUPLICATE KEY UPDATE user_id = VALUES(user_id)
                                        """,
                                        (db_user_id, guild.id),
                                    )
                                    await cursor.execute(
                                        """
                                        UPDATE user_level
                                        SET
                                            xp_awarded_today = CASE
                                                WHEN xp_awarded_day = UTC_DATE() THEN xp_awarded_today
                                                ELSE 0
                                            END,
                                            xp_awarded_day = UTC_DATE(),
                                            xp_awarded_voice_today = CASE
                                                WHEN xp_awarded_voice_day = UTC_DATE() THEN xp_awarded_voice_today
                                                ELSE 0
                                            END,
                                            xp_awarded_voice_day = UTC_DATE()
                                        WHERE user_id = %s AND server_guild_id = %s
                                        """,
                                        (db_user_id, guild.id),
                                    )
                                    await cursor.execute(
                                        """
                                    SELECT xp_awarded_today, xp_awarded_voice_today
                                    FROM user_level
                                    WHERE user_id = %s AND server_guild_id = %s
                                    LIMIT 1
                                        """,
                                        (db_user_id, guild.id),
                                    )
                                    row = await cursor.fetchone() or {}
                                    today_awarded = int(row.get("xp_awarded_today") or 0)
                                    today_voice_awarded = int(row.get("xp_awarded_voice_today") or 0)
                                    award = XpPolicy.award_voice_xp(
                                        base_xp,
                                        level_config,
                                        today_awarded,
                                        activity_factor,
                                        today_voice_awarded,
                                    )
                                    capped_gain = int(award.granted_xp)
                                    raw_gain = max(0, int(apply_xp_multipliers(base_xp, level_config) * activity_factor))
                                    if capped_gain > 0:
                                        await cursor.execute(
                                            """
                                            UPDATE user_level
                                            SET
                                                total_xp = total_xp + %s,
                                                xp_awarded_today = xp_awarded_today + %s,
                                                xp_awarded_voice_today = xp_awarded_voice_today + %s
                                            WHERE user_id = %s AND server_guild_id = %s
                                            """,
                                            (int(capped_gain), int(capped_gain), int(capped_gain), db_user_id, guild.id),
                                        )
                                        await cursor.execute(
                                            """
                                            SELECT total_xp, current_level
                                            FROM user_level
                                            WHERE user_id = %s AND server_guild_id = %s
                                            LIMIT 1
                                            """,
                                            (db_user_id, guild.id),
                                        )
                                        level_row = await cursor.fetchone() or {}
                                        total_xp = int(level_row.get("total_xp") or 0)
                                        current_level = int(level_row.get("current_level") or 0)
                                        recalculated_level = int(level_from_total_xp(total_xp, level_config))
                                        if recalculated_level != current_level:
                                            await cursor.execute(
                                                """
                                                UPDATE user_level
                                                SET current_level = %s
                                                WHERE user_id = %s AND server_guild_id = %s
                                                """,
                                                (recalculated_level, db_user_id, guild.id),
                                            )
                                        await self._register_voice_xp_audit(
                                            guild.id, member.id, voice_channel.id, "voice_xp_awarded",
                                            xp_amount=capped_gain, eligible_seconds=elapsed
                                        )
                                        if raw_gain > capped_gain:
                                            await self._register_voice_xp_audit(
                                                guild.id, member.id, voice_channel.id, "voice_xp_capped_daily",
                                                xp_amount=raw_gain - capped_gain, eligible_seconds=elapsed
                                            )
                                    elif award.reason == "daily_cap_reached":
                                        await self._register_voice_xp_audit(
                                            guild.id, member.id, voice_channel.id, "voice_xp_capped_daily",
                                            xp_amount=0, eligible_seconds=elapsed
                                        )
                    elif elapsed > 0:
                        event_type = "voice_xp_blocked_afk"
                        if getattr(member.guild, "afk_channel", None) and voice_channel.id != member.guild.afk_channel.id:
                            if len(humans) < 2:
                                event_type = "voice_xp_blocked_alone"
                            elif bool(member.voice.self_mute) if member.voice else False:
                                event_type = "voice_xp_reduced_muted"
                        await self._register_voice_xp_audit(
                            guild.id, member.id, voice_channel.id, event_type,
                            xp_amount=0, ineligible_seconds=elapsed
                        )
                    if consumed_seconds > 0:
                        session["last_tick_at"] = session["last_tick_at"] + timedelta(seconds=consumed_seconds)
                    else:
                        session["last_tick_at"] = now
                    await async_upsert_voice_session(session)

    @voice_xp_tick.before_loop
    async def before_voice_xp_tick(self):
        await self.bot.wait_until_ready()

    @admin_xp.command(name="voice-inspect", description="Inspeciona auditoria e métricas de XP de voz por usuário")
    @app_commands.describe(member="Membro para inspeção", dias="Janela em dias para métricas (1-30)")
    async def admin_voice_inspect(self, ctx: discord.Interaction, member: discord.Member, dias: app_commands.Range[int, 1, 30] = 7):
        if not await self._ensure_admin_permissions(ctx):
            return
        async with async_pooled_connection() as cursor:
            await cursor.execute(
                """
                SELECT event_type, xp_amount, voice_channel_id, eligible_seconds, ineligible_seconds, created_at
                FROM voice_xp_audit_log
                WHERE server_guild_id = %s AND discord_user_id = %s
                ORDER BY id DESC
                LIMIT 10
                """,
                (ctx.guild.id, member.id),
            )
            latest = await cursor.fetchall() or []
            await cursor.execute(
                """
                SELECT modality, SUM(total_xp) AS total_xp, SUM(grants_count) AS grants_count, SUM(blocks_count) AS blocks_count,
                       SUM(eligible_seconds) AS eligible_seconds, SUM(ineligible_seconds) AS ineligible_seconds
                FROM voice_xp_daily_metrics
                WHERE server_guild_id = %s AND metric_day >= (UTC_DATE() - INTERVAL %s DAY)
                GROUP BY modality
                ORDER BY total_xp DESC
                """,
                (ctx.guild.id, int(dias)),
            )
            modality_rows = await cursor.fetchall() or []
            await cursor.execute(
                """
                SELECT voice_channel_id, SUM(total_xp) AS total_xp
                FROM voice_xp_daily_metrics
                WHERE server_guild_id = %s AND metric_day >= (UTC_DATE() - INTERVAL %s DAY) AND modality = 'voice_xp_awarded' AND voice_channel_id IS NOT NULL
                GROUP BY voice_channel_id
                ORDER BY total_xp DESC
                LIMIT 5
                """,
                (ctx.guild.id, int(dias)),
            )
            top_channels = await cursor.fetchall() or []
        desc = []
        if latest:
            desc.append("**Últimos eventos:**")
            for row in latest:
                desc.append(f"- `{row['created_at']}` • `{row['event_type']}` • XP `{int(row.get('xp_amount') or 0)}` • canal `{row.get('voice_channel_id')}`")
        if modality_rows:
            desc.append("\n**Métricas agregadas (guild/dia):**")
            for row in modality_rows:
                elig = int(row.get("eligible_seconds") or 0)
                inelig = int(row.get("ineligible_seconds") or 0)
                block_rate = (int(row.get("blocks_count") or 0) / max(1, int(row.get("grants_count") or 0) + int(row.get("blocks_count") or 0))) * 100
                desc.append(f"- `{row['modality']}`: XP `{int(row.get('total_xp') or 0)}` • bloqueio `{block_rate:.1f}%` • min elegíveis `{elig//60}` vs não elegíveis `{inelig//60}`")
        if top_channels:
            desc.append("\n**Top canais por XP:**")
            for row in top_channels:
                desc.append(f"- Canal `{row.get('voice_channel_id')}`: XP `{int(row.get('total_xp') or 0)}`")
        embed = discord.Embed(title=f"Auditoria de XP de voz — {member.display_name}", description="\n".join(desc)[:4000], color=discord.Color.teal())
        await ctx.response.send_message(embed=embed, ephemeral=True)

    @staticmethod
    def _is_guild_admin(member: discord.abc.User | discord.Member) -> bool:
        return isinstance(member, discord.Member) and (
            member.guild_permissions.administrator or member.guild_permissions.manage_guild
        )

    @staticmethod
    def _level_config_to_text(config: dict) -> str:
        warning_channel = config.get("levelupWarningChannel")
        warning_channel_display = f"<#{warning_channel}> (`{warning_channel}`)" if warning_channel else "não configurado"

        lines = [
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
        return "\n".join(lines)

    async def _ensure_admin_permissions(self, ctx: discord.Interaction) -> bool:
        if not ctx.guild:
            await ctx.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
            return False

        if not self._is_guild_admin(ctx.user):
            await ctx.response.send_message(
                "Você precisa de **Administrador** ou **Gerenciar Servidor** para usar este comando.",
                ephemeral=True,
            )
            return False

        return True

    @staticmethod
    def _normalize_value(field: str, value: str):
        cleaned_value = value.strip()

        if field in {
            "daily_combo",
            "levelup_warning",
            "xp_base_per_min",
            "voice_social_bonus_min_humans",
            "voice_diminishing_window1_minutes",
            "voice_diminishing_window2_minutes",
            "voice_daily_cap_xp",
        }:
            return int(cleaned_value)

        if field in {
            "phase1_k",
            "phase1_p",
            "phase1_b",
            "multiplier",
            "combo_multiplier",
            "voice_social_bonus_pct",
            "voice_diminishing_factor2",
            "voice_diminishing_factor3",
        }:
            return Decimal(cleaned_value)

        return cleaned_value

    @app_commands.command(name='xp', description='Mostra a quantidade de xp de um membro')
    @app_commands.describe(member="Membro para consultar (opcional)")
    async def showXp(self, ctx: discord.Interaction, member: discord.Member | None = None):
        if not ctx.guild:
            await ctx.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
            return

        target_member = member or ctx.user
        if not isinstance(target_member, discord.Member):
            await ctx.response.send_message(
                "Não foi possível identificar o membro para consulta.",
                ephemeral=True,
            )
            return

        guild_id = ctx.guild.id
        db_user_id = await async_getUserId(target_member.id)
        total_xp = 0
        current_level = 0
        rank: int | None = None

        if db_user_id is not None:
            async with async_pooled_connection() as cursor:
                await cursor.execute(
                    """
                    SELECT total_xp, current_level
                    FROM user_level
                    WHERE user_id = %s AND server_guild_id = %s
                    """,
                    (db_user_id, guild_id),
                )
                row = await cursor.fetchone() or {}
                total_xp = int(row.get("total_xp") or 0)
                current_level = int(row.get("current_level") or 0)

                await cursor.execute(
                    """
                    SELECT ranked.rank_position
                    FROM (
                        SELECT
                            user_id,
                            ROW_NUMBER() OVER (ORDER BY total_xp DESC, current_level DESC, user_id ASC) AS rank_position
                        FROM user_level
                        WHERE server_guild_id = %s
                    ) ranked
                    WHERE ranked.user_id = %s
                    """,
                    (guild_id, db_user_id),
                )
                rank_row = await cursor.fetchone() or {}
                rank_value = rank_row.get("rank_position")
                if rank_value is not None:
                    rank = int(rank_value)

        embed = discord.Embed(
            title="📈 Perfil de XP",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Membro", value=target_member.mention, inline=False)
        embed.add_field(name="Nível", value=f"`{current_level}`", inline=True)
        embed.add_field(name="XP total", value=f"`{total_xp}`", inline=True)
        embed.add_field(
            name="Posição no ranking",
            value=f"`#{rank}`" if rank is not None else "`Sem colocação`",
            inline=True,
        )

        await ctx.response.send_message(embed=embed)

    @app_commands.command(name='xp_ranking', description='Mostra o ranking de xp dos membros')
    async def showXpRanking(self, ctx: discord.Interaction):
        if not ctx.guild:
            await ctx.response.send_message(
                "Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
            return

        async with async_pooled_connection() as cursor:
            await cursor.execute(
                """
                SELECT
                    user_discord.discord_user_id,
                    user_discord.display_name,
                    user_level.total_xp,
                    user_level.current_level
                FROM user_level
                JOIN (
                    SELECT MAX(id) AS id, user_id
                    FROM user_discord
                    GROUP BY user_id
                ) latest_discord ON latest_discord.user_id = user_level.user_id
                JOIN user_discord ON user_discord.id = latest_discord.id
                WHERE user_level.server_guild_id = %s
                ORDER BY user_level.current_level DESC, user_level.total_xp DESC, user_discord.discord_user_id ASC
                LIMIT 10
                """,
                (ctx.guild.id,),
            )
            ranking_rows = await cursor.fetchall() or []

        if not ranking_rows:
            await ctx.response.send_message("Ainda não há dados de XP suficientes para montar o ranking.")
            return

        lines: list[str] = []
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}

        for index, row in enumerate(ranking_rows, start=1):
            discord_user_id = row.get("discord_user_id")
            display_name = row.get("display_name") or f"Usuário {discord_user_id}"
            total_xp = int(row.get("total_xp") or 0)
            current_level = int(row.get("current_level") or 0)
            placement = medals.get(index, f"`#{index}`")

            member = ctx.guild.get_member(int(discord_user_id)) if discord_user_id else None
            user_reference = member.mention if member else f"**{display_name}**"

            lines.append(
                f"{placement} {user_reference} — Nível `{current_level}` • XP `{total_xp}`"
            )

        embed = discord.Embed(
            title="🏆 Ranking de XP",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Mostrando o Top 10 do servidor.")

        await ctx.response.send_message(embed=embed)

    @admin_xp.command(name="ajustar", description="Ajusta o xp de um membro")
    @app_commands.rename(member="membro", mode="modo", value="valor")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="adicionar", value="adicionar"),
            app_commands.Choice(name="subtrair", value="subtrair"),
            app_commands.Choice(name="definir", value="definir"),
        ]
    )
    async def adjustXp(
        self,
        ctx: discord.Interaction,
        member: discord.Member,
        mode: app_commands.Choice[str],
        value: int,
    ):
        pass

    @admin_xp.command(name="config", description="Exibe ou altera a configuração atual de níveis do servidor")
    @app_commands.rename(
        warning_channel="canal_aviso",
        levelup_warning="aviso_habilitado",
        levelup_message="mensagem_level_up",
        phase1_k="k",
        phase1_p="p",
        phase1_b="b",
        multiplier="multiplicador",
    )
    @app_commands.describe(
        warning_channel="Canal de aviso de level up (opcional)",
        levelup_warning="Ativa/desativa aviso de level up (opcional)",
        levelup_message=(
            "Mensagem de level up (opcional). Placeholders: {user}, {last_level}, {actual_level}. "
            "Use --, ++ ou [[clear]] para limpar."
        ),
        phase1_k="Constante k da curva kL^p + bL (opcional)",
        phase1_p="Expoente p da curva kL^p + bL (opcional)",
        phase1_b="Termo linear b da curva kL^p + bL (opcional)",
        multiplier="Multiplicador global de XP (opcional)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def show_level_config(
        self,
        ctx: discord.Interaction,
        warning_channel: discord.TextChannel | discord.Thread | None = None,
        levelup_warning: bool | None = None,
        levelup_message: str | None = None,
        phase1_k: str | None = None,
        phase1_p: str | None = None,
        phase1_b: str | None = None,
        multiplier: str | None = None,
    ):
        if not await self._ensure_admin_permissions(ctx):
            return

        assert ctx.guild is not None

        updates: dict[str, object] = {}

        if warning_channel is not None:
            updates["levelup_warning_channel"] = warning_channel.id
        if levelup_warning is not None:
            updates["levelup_warning"] = int(levelup_warning)
        if levelup_message is not None:
            normalized_levelup_message = levelup_message.strip()
            if normalized_levelup_message in self._LEVELUP_MESSAGE_CLEAR_TOKENS:
                updates["level_up_message"] = None
            else:
                updates["level_up_message"] = normalized_levelup_message

        numeric_fields = {
            "phase1_k": phase1_k,
            "phase1_p": phase1_p,
            "phase1_b": phase1_b,
            "multiplier": multiplier,
        }
        for field_name, raw_value in numeric_fields.items():
            if raw_value is None:
                continue
            try:
                updates[field_name] = self._normalize_value(field_name, raw_value)
            except (ValueError, InvalidOperation):
                await ctx.response.send_message(
                    f"Valor inválido para `{field_name}`. Verifique o formato e tente novamente.",
                    ephemeral=True,
                )
                return

        try:
            if updates:
                config = await async_update_level_config(ctx.guild.id, **updates)
                title = "✅ Configuração de níveis atualizada"
            else:
                config = await async_get_level_config(ctx.guild.id)
                title = "⚙️ Configuração de níveis"

            embed = discord.Embed(
                title=title,
                description=self._level_config_to_text(config),
                color=discord.Color.blurple(),
            )
            await ctx.response.send_message(embed=embed, ephemeral=True)
        except ValueError as error:
            await ctx.response.send_message(
                f"Não foi possível atualizar: {error}",
                ephemeral=True,
            )
        except Exception:
            await ctx.response.send_message(
                "Não consegui processar a configuração de níveis agora. Tente novamente em instantes.",
                ephemeral=True,
            )

    @admin_xp.command(name="simular", description="Simula XP necessário para alcançar um nível (kL^p + bL)")
    @app_commands.rename(level="nivel", base_xp="xp_base", combo_count="combo")
    @app_commands.describe(
        level="Nível alvo da simulação",
        base_xp="XP base para cálculo com multiplicadores (opcional)",
        combo_count="Quantidade de combo diário para simulação (opcional)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def simulate_level_progression(
        self,
        ctx: discord.Interaction,
        level: app_commands.Range[int, 1, 100000],
        base_xp: app_commands.Range[int, 0, 100000] = 0,
        combo_count: app_commands.Range[int, 0, 365] = 0,
    ):
        if not await self._ensure_admin_permissions(ctx):
            return

        assert ctx.guild is not None

        try:
            config = await async_get_level_config(ctx.guild.id)
            required_xp = xp_to_reach_level(level, config)
            adjusted_xp = apply_xp_multipliers(base_xp, config, combo_count=combo_count)

            embed = discord.Embed(
                title="🧪 Simulação de progressão",
                color=discord.Color.green(),
            )
            embed.add_field(name="Nível alvo", value=f"`{level}`", inline=True)
            embed.add_field(name="XP necessário", value=f"`{required_xp}`", inline=True)
            embed.add_field(name="XP base informado", value=f"`{base_xp}`", inline=True)
            embed.add_field(name="XP com multiplicadores", value=f"`{adjusted_xp}`", inline=True)
            embed.add_field(name="Combo informado", value=f"`{combo_count}`", inline=True)

            await ctx.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            await ctx.response.send_message(
                "Não foi possível executar a simulação agora. Tente novamente em instantes.",
                ephemeral=True,
            )

###############################################################################################################

    """ @app_commands.cooldown(1, 86400, key=lambda self, ctx: (self, ctx.guild_id, self, ctx.author.id)) """
    @app_commands.command(name='daily', description='Pega sua recompensa diária')
    async def daily(self, ctx: discord.Interaction):
        await ctx.response.send_message(content='Recompensa diária pega com sucesso!')
        pass

    @daily.error
    async def daily_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f'Você já pegou sua recompensa diária! Tente novamente em {error.retry_after:.0f} segundos', ephemeral=True)
        else:
            raise error

##############################################################################################################

    @rp.command(name='banho', description='Tomar banho dá xp sabia?')
    async def bath(self, ctx: discord.Interaction):
        pass

    @rp.command(name='trabalhar', description='Trabalha em troca de xp e dinheiro')
    async def work(self, ctx: discord.Interaction):
        pass

    @rp.command(name='duelo', description='Desafie alguém para um duelo')
    async def duel(self, ctx: discord.Interaction, member: discord.Member):
        pass

    @rp.command(name='desenhar', description='Desenhe algo!')
    async def draw(self, ctx: discord.Interaction):
        pass

    @rp.command(name='escrever', description='Escreva uma história')
    async def write(self, ctx: discord.Interaction):
        pass

    """ @bot.tree.command(name=f'rp_missao', description=f'Complete missões para ganhar xp e dinheiro')
    async def mission(ctx: discord.Interaction):
        pass """


async def setup(bot: commands.Bot):
    await bot.add_cog(XpCog(bot))
