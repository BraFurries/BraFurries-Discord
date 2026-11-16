"""Discord runtime for API-authorized durable guild role operations.

Only the API creates jobs and chooses their guild/Community. The bot verifies
the actor, guild, permission hierarchy and stored request again before effects.
Persistence is shared with the API solely for technical execution/progress.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import discord

from core.bulk_role_assignment import (
    BulkRolePreviewError, _require_bulk_role_access, build_bulk_role_preview,
)
from core.database import async_pooled_connection

logger = logging.getLogger(__name__)
SOURCE_BOT_IDS = {
    "Dyno": 155149108183695360,
    "Loritta": 297153970613387264,
    "mee6": 159985870458322944,
}


class RoleToolRuntimeError(ValueError):
    pass


class RoleToolRuntime:
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.tasks: dict[int, asyncio.Task] = {}

    @staticmethod
    def _authorized_admin(guild: discord.Guild, discord_user_id: int) -> None:
        actor = guild.get_member(discord_user_id)
        if actor is None:
            raise PermissionError("actor_not_in_guild")
        if guild.owner_id != discord_user_id and not actor.guild_permissions.administrator:
            raise PermissionError("role_tool_requires_administrator")

    @staticmethod
    async def _read_job(guild_id: int, operation_id: int) -> dict | None:
        async with async_pooled_connection() as cur:
            await cur.execute(
                """SELECT id, guild_id, community_id, actor_user_id, actor_discord_user_id,
                          operation_type, role_id, channel_id, source_bot,
                          source_operation_id, role_key, preview_fingerprint,
                          expected_count, progress_current, status
                   FROM guild_role_tool_operations
                   WHERE id=%s AND guild_id=%s""",
                (operation_id, guild_id),
            )
            return await cur.fetchone()

    @staticmethod
    async def _read_source(job: dict) -> dict:
        async with async_pooled_connection() as cur:
            await cur.execute(
                """SELECT result_json FROM guild_role_tool_operations
                   WHERE id=%s AND guild_id=%s AND community_id=%s
                     AND operation_type='RECOVERY_SCAN' AND status='READY'""",
                (job["source_operation_id"], job["guild_id"], job["community_id"]),
            )
            row = await cur.fetchone()
        if not row or not row["result_json"]:
            raise RoleToolRuntimeError("source_scan_unavailable")
        return json.loads(row["result_json"])

    @staticmethod
    async def _claim(job: dict) -> bool:
        async with async_pooled_connection() as cur:
            await cur.execute(
                """UPDATE guild_role_tool_operations
                   SET status='RUNNING', started_at=UTC_TIMESTAMP(6)
                   WHERE id=%s AND guild_id=%s AND community_id=%s
                     AND status='PENDING'""",
                (job["id"], job["guild_id"], job["community_id"]),
            )
            return cur.rowcount == 1

    @staticmethod
    async def _progress(job: dict, *, current: int, total: int,
                        added: int, skipped: int, failed: int,
                        scanned: int = 0) -> None:
        async with async_pooled_connection() as cur:
            await cur.execute(
                """UPDATE guild_role_tool_operations
                   SET progress_current=%s, progress_total=%s, added_count=%s,
                       skipped_count=%s, failed_count=%s, scanned_count=%s
                   WHERE id=%s AND guild_id=%s AND status='RUNNING'""",
                (current, total, added, skipped, failed, scanned,
                 job["id"], job["guild_id"]),
            )

    @staticmethod
    async def _complete(job: dict, status: str, *, current: int = 0,
                        total: int = 0, added: int = 0, skipped: int = 0,
                        failed: int = 0, scanned: int = 0,
                        result: dict | None = None, error: str | None = None) -> None:
        if status not in {"READY", "SUCCEEDED", "PARTIAL", "FAILED"}:
            raise ValueError("invalid_final_status")
        body = json.dumps(result, ensure_ascii=False, separators=(",", ":")) if result is not None else None
        async with async_pooled_connection() as cur:
            await cur.execute(
                """UPDATE guild_role_tool_operations
                   SET status=%s, progress_current=%s, progress_total=%s,
                       added_count=%s, skipped_count=%s, failed_count=%s,
                       scanned_count=%s, result_json=%s, error_code=%s,
                       finished_at=UTC_TIMESTAMP(6)
                   WHERE id=%s AND guild_id=%s AND community_id=%s AND status='RUNNING'""",
                (status, current, total, added, skipped, failed, scanned, body, error,
                 job["id"], job["guild_id"], job["community_id"]),
            )
            if cur.rowcount == 1:
                await cur.execute(
                    """INSERT INTO guild_role_tool_audit
                           (operation_id,community_id,guild_id,actor_user_id,event_type,detail)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (job["id"], job["community_id"], job["guild_id"],
                     job["actor_user_id"], status, error),
                )

    async def dispatch(self, guild: discord.Guild, actor_id: int, operation_id: int) -> dict:
        job = await self._read_job(guild.id, operation_id)
        if not job or job["actor_discord_user_id"] != actor_id:
            raise RoleToolRuntimeError("role_operation_not_found")
        if job["status"] != "PENDING":
            raise RoleToolRuntimeError("role_operation_not_pending")
        if guild.id in self.tasks and not self.tasks[guild.id].done():
            raise RoleToolRuntimeError("another_role_operation_running")

        # The API verified admin access before persisting the request.
        # Claim and run runtime preconditions in the worker so a role removed
        # between API and Coddy cannot strand a PENDING guild-wide lock.
        if not await self._claim(job):
            raise RoleToolRuntimeError("role_operation_already_claimed")

        task = asyncio.create_task(self._run(guild, job),
                                   name=f"role_tool_{guild.id}_{operation_id}")
        self.tasks[guild.id] = task
        task.add_done_callback(lambda completed: self._done(guild.id, completed))
        return {"accepted": True, "operationId": operation_id, "status": "RUNNING"}

    def _done(self, guild_id: int, task: asyncio.Task) -> None:
        if self.tasks.get(guild_id) is task:
            self.tasks.pop(guild_id, None)
        if not task.cancelled():
            try:
                task.result()
            except Exception:
                logger.exception("Unhandled role-tool worker failure for guild %s", guild_id)

    async def _run(self, guild: discord.Guild, job: dict) -> None:
        try:
            self._authorized_admin(guild, job["actor_discord_user_id"])
            if job["operation_type"] in ("BULK_ADD", "RECOVERY_APPLY"):
                _require_bulk_role_access(guild, job["actor_discord_user_id"], int(job["role_id"]))
            elif job["operation_type"] == "RECOVERY_SCAN":
                channel = guild.get_channel(int(job["channel_id"]))
                if not isinstance(channel, discord.TextChannel):
                    raise RoleToolRuntimeError("RECOVERY_CHANNEL_NOT_FOUND")
                me = guild.me
                permissions = channel.permissions_for(me) if me else None
                if not permissions or not (permissions.view_channel and permissions.read_message_history):
                    raise PermissionError("BOT_CANNOT_READ_HISTORY")
            else:
                raise RoleToolRuntimeError("UNSUPPORTED_OPERATION")
            if job["operation_type"] == "BULK_ADD":
                await self._run_bulk(guild, job)
            elif job["operation_type"] == "RECOVERY_SCAN":
                await self._run_scan(guild, job)
            else:
                await self._run_recovery(guild, job)
        except asyncio.CancelledError:
            # Cancellation while shutting down must preserve partial progress.
            await self._mark_interrupted(job, "RUNTIME_CANCELLED")
            raise
        except Exception as exc:
            logger.exception("Role tool operation %s failed", job["id"])
            error = str(exc) if isinstance(exc, (RoleToolRuntimeError, BulkRolePreviewError, PermissionError)) else "RUNTIME_ERROR"
            await self._mark_interrupted(job, error[:96])

    async def _mark_interrupted(self, job: dict, error: str) -> None:
        async with async_pooled_connection() as cur:
            await cur.execute(
                """SELECT progress_current, progress_total, added_count,
                          skipped_count, failed_count, scanned_count
                   FROM guild_role_tool_operations
                   WHERE id=%s AND guild_id=%s""",
                (job["id"], job["guild_id"]),
            )
            r = await cur.fetchone() or {}
        added = int(r.get("added_count") or 0)
        await self._complete(job, "PARTIAL" if added else "FAILED",
                             current=int(r.get("progress_current") or 0),
                             total=int(r.get("progress_total") or 0),
                             added=added, skipped=int(r.get("skipped_count") or 0),
                             failed=int(r.get("failed_count") or 0),
                             scanned=int(r.get("scanned_count") or 0), error=error)

    async def _assign_members(self, guild: discord.Guild, job: dict,
                              role: discord.Role, member_ids: list[int],
                              *, allow_missing: bool) -> None:
        total = len(member_ids)
        added = skipped = failed = current = 0
        last_update = time.monotonic()
        # Discord API rate limits are handled by discord.py; modest parallelism
        # reduces contention with ordinary community operations.
        semaphore = asyncio.Semaphore(3)
        queue: asyncio.Queue[int] = asyncio.Queue()
        for member_id in member_ids:
            queue.put_nowait(member_id)

        async def worker() -> None:
            nonlocal added, skipped, failed, current, last_update
            async with semaphore:
                while True:
                    try:
                        member_id = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        member = guild.get_member(member_id)
                        if member is None and allow_missing:
                            try:
                                member = await guild.fetch_member(member_id)
                            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                                member = None
                        if member is None or member.bot:
                            skipped += 1
                        elif role in member.roles:
                            skipped += 1
                        else:
                            try:
                                await member.add_roles(
                                    role,
                                    reason=f"BraFurries painel: operação {job['id']}",
                                )
                                added += 1
                            except (discord.Forbidden, discord.HTTPException):
                                failed += 1
                    finally:
                        current += 1
                        now = time.monotonic()
                        if current == total or (current % 25 == 0 and now - last_update >= 2):
                            last_update = now
                            await self._progress(job, current=current, total=total,
                                added=added, skipped=skipped, failed=failed)
                        queue.task_done()

        await asyncio.gather(*(worker() for _ in range(min(3, total))))
        await self._complete(job, "PARTIAL" if failed else "SUCCEEDED",
                             current=current, total=total,
                             added=added, skipped=skipped, failed=failed)

    async def _run_bulk(self, guild: discord.Guild, job: dict) -> None:
        role = _require_bulk_role_access(guild, job["actor_discord_user_id"], job["role_id"])
        if not guild.chunked:
            await asyncio.wait_for(guild.chunk(cache=True), timeout=30)
        preview = build_bulk_role_preview(guild, job["actor_discord_user_id"], job["role_id"])
        if preview["fingerprint"] != job["preview_fingerprint"]:
            raise RoleToolRuntimeError("PREVIEW_CHANGED")
        if preview["eligible"] != job["expected_count"]:
            raise RoleToolRuntimeError("MEMBER_COUNT_CHANGED")
        member_ids = [
            member.id for member in sorted(guild.members, key=lambda m: m.id)
            if not member.bot and role not in member.roles
        ]
        await self._assign_members(guild, job, role, member_ids, allow_missing=False)

    async def _run_scan(self, guild: discord.Guild, job: dict) -> None:
        # Reuse the existing parsers; never invent a separate recovery grammar.
        parser = self.bot.get_cog("Import")
        if parser is None:
            raise RoleToolRuntimeError("RECOVERY_PARSER_UNAVAILABLE")
        bot_name = job["source_bot"]
        if bot_name not in SOURCE_BOT_IDS:
            raise RoleToolRuntimeError("UNSUPPORTED_LOG_BOT")
        channel = guild.get_channel(job["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            raise RoleToolRuntimeError("RECOVERY_CHANNEL_NOT_FOUND")

        roles_by_id = {role.id: role for role in guild.roles}
        roles_by_name = {}
        for role in guild.roles:
            roles_by_name.setdefault(parser._normalize_role_name(role.name), []).append(role)

        entries = {}
        scanned = matched = 0
        last_update = time.monotonic()
        async for message in channel.history(limit=None, oldest_first=True):
            scanned += 1
            if scanned > 150000:
                raise RoleToolRuntimeError("LOG_HISTORY_LIMIT_EXCEEDED")
            if parser._is_message_from_selected_bot(message, bot_name, SOURCE_BOT_IDS[bot_name]):
                matched += 1
                result = parser._process_recovery_message(
                    bot_name, message, roles_by_id, roles_by_name, entries
                )
                if result:
                    raise RoleToolRuntimeError("UNRECOGNIZED_ROLE_LOG")
            if scanned % 250 == 0:
                await asyncio.sleep(0)
                if time.monotonic() - last_update >= 2:
                    await self._progress(job, current=scanned, total=scanned,
                                         added=0, skipped=0, failed=0, scanned=scanned)
                    last_update = time.monotonic()

        role_entries = []
        for key, item in sorted(entries.items(), key=lambda i: i[1].display_name.casefold()):
            role_entries.append({
                "key": key,
                "name": item.display_name[:120],
                "foundRoleId": str(item.server_role.id) if item.server_role else None,
                "roleIdFromLog": str(item.role_id_from_log) if item.role_id_from_log else None,
                "ambiguous": item.is_name_ambiguous,
                "memberIds": sorted(item.member_ids),
            })
        snapshot = {
            "bot": bot_name, "channelId": str(channel.id),
            "messagesScanned": scanned, "botMessagesFound": matched,
            "roleEntries": role_entries,
        }
        if len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")) > 8_000_000:
            raise RoleToolRuntimeError("RECOVERY_SNAPSHOT_TOO_LARGE")
        await self._complete(job, "READY", current=scanned, total=scanned,
                             scanned=scanned, result=snapshot)

    async def _run_recovery(self, guild: discord.Guild, job: dict) -> None:
        snapshot = await self._read_source(job)
        selected = next(
            (role for role in snapshot.get("roleEntries", [])
             if role.get("key") == job["role_key"]), None
        )
        if selected is None:
            raise RoleToolRuntimeError("ROLE_NOT_FOUND_IN_SCAN")
        role = _require_bulk_role_access(guild, job["actor_discord_user_id"], job["role_id"])
        member_ids = selected["memberIds"]
        if not isinstance(member_ids, list) or len(member_ids) != job["expected_count"]:
            raise RoleToolRuntimeError("RECOVERY_SNAPSHOT_CHANGED")
        if len(set(member_ids)) != len(member_ids):
            raise RoleToolRuntimeError("DUPLICATE_RECOVERY_MEMBER")
        await self._assign_members(guild, job, role, member_ids, allow_missing=True)
