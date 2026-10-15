from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import discord

CHANNEL_CREATION_DELAY_SECONDS = 0.8
ROLE_MUTATION_DELAY_SECONDS = 0.5
ROLE_MUTATION_TIMEOUT_SECONDS = 300.0
RESTORABLE_CHANNEL_TYPES = {0, 2, 4, 5, 13, 15}
RESTORE_SCOPES = {"full", "roles", "channels", "permissions"}
_CREATE_NEW = "CREATE_NEW"

logger = logging.getLogger(__name__)


class BackupRuntimeError(RuntimeError):
    pass


class BackupBusyError(BackupRuntimeError):
    pass


class BackupDecisionRequired(BackupRuntimeError):
    pass


class BackupRestoreEngine:
    CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:([a-zA-Z0-9_]+):\d+>")

    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def _capture_roles(guild: discord.Guild) -> list[dict]:
        roles_top_to_bottom = sorted(
            guild.roles,
            key=lambda role: role.position,
            reverse=True,
        )
        return [
            {
                "discord_id": int(role.id),
                "name": role.name,
                "color": int(role.color.value),
                "permissions": int(role.permissions.value),
                "position": int(role.position),
                "hoist": bool(role.hoist),
                "mentionable": bool(role.mentionable),
            }
            for role in roles_top_to_bottom
            if role != guild.default_role and not role.managed
        ]

    @staticmethod
    def _capture_channels(guild: discord.Guild) -> list[dict]:
        return [
            {
                "discord_id": int(channel.id),
                "parent_id": int(channel.category_id)
                if getattr(channel, "category_id", None) else None,
                "name": channel.name,
                "type": int(channel.type.value),
                "position": int(channel.position)
                if channel.position is not None else None,
                "topic": getattr(channel, "topic", None),
                "nsfw": bool(getattr(channel, "nsfw", False))
                if getattr(channel, "nsfw", None) is not None else None,
            }
            for channel in guild.channels
        ]

    @staticmethod
    def _capture_overwrites(guild: discord.Guild) -> list[dict]:
        overwrites: list[dict] = []
        for channel in guild.channels:
            for target, overwrite in channel.overwrites.items():
                if not isinstance(target, discord.Role):
                    continue
                allow, deny = overwrite.pair()
                target_type = (
                    "EVERYONE"
                    if target == guild.default_role
                    else "MANAGED_ROLE"
                    if target.managed
                    else "ROLE"
                )
                overwrites.append(
                    {
                        "channel_discord_id": int(channel.id),
                        "role_discord_id": int(target.id),
                        "target_type": target_type,
                        "target_discord_id": int(target.id),
                        "role_name": target.name,
                        "allow_bits": int(allow.value),
                        "deny_bits": int(deny.value),
                    }
                )
        return overwrites

    @classmethod
    def build_guild_snapshot(
        cls,
        guild: discord.Guild,
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """Capture the structural subset supported by Coddy.

        Channel permission overwrites are intentionally limited to Role targets;
        member-specific overwrites are outside the current Backup domain.
        """
        return (
            cls._capture_roles(guild),
            cls._capture_channels(guild),
            cls._capture_overwrites(guild),
        )

    async def create_snapshot(
        self,
        guild: discord.Guild,
        *,
        name: str,
        backup_type: str,
        created_by_discord_user_id: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Compatibility entrypoint that creates a tracked snapshot operation."""
        from core.database import create_backup_snapshot_operation

        normalized_type = "periodic" if backup_type == "periodic" else "normal"
        operation_key = str(idempotency_key or "").strip() or (
            f"runtime:{normalized_type}:{uuid.uuid4().hex}"
        )
        operation_id = create_backup_snapshot_operation(
            int(guild.id),
            operation_key,
            normalized_type,
            name,
            actor_discord_user_id=created_by_discord_user_id,
        )
        return await self.run_snapshot_operation(
            guild,
            operation_id=operation_id,
        )

    async def run_snapshot_operation(
        self,
        guild: discord.Guild,
        *,
        operation_id: int,
    ) -> dict[str, Any]:
        from core.database import (
            acquire_backup_guild_lock,
            complete_backup_snapshot_operation,
            create_discord_backup_snapshot,
            get_backup_snapshot_operation,
            mark_backup_snapshot_operation_running,
            record_backup_operation_step,
            release_backup_guild_lock,
        )

        operation = get_backup_snapshot_operation(
            int(operation_id),
            int(guild.id),
        )
        if operation is None:
            raise LookupError("backup_snapshot_operation_not_found")
        if str(operation.get("status") or "") != "PENDING":
            result = operation.get("result_json")
            if isinstance(result, str) and result.strip():
                try:
                    parsed = json.loads(result)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    pass
            return {
                "operationId": int(operation_id),
                "status": str(operation.get("status") or "UNKNOWN"),
                "backupId": operation.get("backup_id"),
            }

        normalized_type = (
            "periodic"
            if str(operation.get("backup_type")) == "periodic"
            else "normal"
        )
        total = 6
        current = 0

        def step(
            code: str,
            status: str,
            message: str,
            *,
            detail: Optional[dict[str, Any]] = None,
        ) -> None:
            nonlocal current
            current += 1
            record_backup_operation_step(
                int(guild.id),
                "SNAPSHOT",
                int(operation_id),
                code,
                status,
                message,
                min(current, total),
                total,
                detail=detail,
            )

        if not mark_backup_snapshot_operation_running(
            int(operation_id),
            int(guild.id),
        ):
            latest = get_backup_snapshot_operation(
                int(operation_id),
                int(guild.id),
            ) or {}
            return {
                "operationId": int(operation_id),
                "status": str(latest.get("status") or "NOT_PENDING"),
                "backupId": latest.get("backup_id"),
            }

        lock_token = f"snapshot:{operation_id}"
        if not acquire_backup_guild_lock(
            int(guild.id),
            lock_token,
            f"snapshot:{normalized_type}",
            operation_id=int(operation_id),
            ttl_seconds=900,
        ):
            step(
                "LOCK_REJECTED",
                "FAILED",
                "Outra operação de Backup já está em andamento nesta guild.",
                detail={"reason": "backup_operation_in_progress"},
            )
            complete_backup_snapshot_operation(
                int(operation_id),
                int(guild.id),
                status="FAILED",
                result={"warnings": ["backup_operation_in_progress"]},
                error_code="backup_operation_in_progress",
            )
            raise BackupBusyError("backup_operation_in_progress")

        try:
            step(
                "LOCK_ACQUIRED",
                "SUCCEEDED",
                "Lock exclusivo da guild adquirido.",
            )
            roles = self._capture_roles(guild)
            step(
                "ROLES_CAPTURED",
                "SUCCEEDED",
                f"{len(roles)} cargos capturados.",
                detail={"count": len(roles)},
            )
            channels = self._capture_channels(guild)
            step(
                "CHANNELS_CAPTURED",
                "SUCCEEDED",
                f"{len(channels)} canais/categorias capturados.",
                detail={"count": len(channels)},
            )
            overwrites = self._capture_overwrites(guild)
            step(
                "PERMISSIONS_CAPTURED",
                "SUCCEEDED",
                f"{len(overwrites)} overwrites de cargos capturados.",
                detail={"count": len(overwrites)},
            )
            backup_id = create_discord_backup_snapshot(
                guild_id=int(guild.id),
                original_name=str(operation["requested_name"]),
                roles=roles,
                channels=channels,
                overwrites=overwrites,
                backup_type=normalized_type,
                created_by_discord_user_id=operation.get(
                    "actor_discord_user_id"
                ),
                idempotency_key=str(operation["operation_key"]),
            )
            step(
                "SNAPSHOT_REPLACED",
                "SUCCEEDED",
                (
                    "Backup manual substituído com sucesso."
                    if normalized_type == "normal"
                    else "Backup periódico substituído com sucesso."
                ),
                detail={"backupId": backup_id, "slot": normalized_type},
            )
            result = {
                "operationId": int(operation_id),
                "status": "SUCCEEDED",
                "id": backup_id,
                "backupId": backup_id,
                "guildId": str(guild.id),
                "name": str(operation["requested_name"]).strip(),
                "type": (
                    "PERIODIC" if normalized_type == "periodic" else "MANUAL"
                ),
                "summary": {
                    "roles": len(roles),
                    "channels": len(channels),
                    "permissionOverwrites": len(overwrites),
                    "roleOverwritesOnly": True,
                },
            }
            step(
                "COMPLETED",
                "SUCCEEDED",
                "Snapshot concluído e disponível para restauração.",
                detail={"backupId": backup_id},
            )
            complete_backup_snapshot_operation(
                int(operation_id),
                int(guild.id),
                status="SUCCEEDED",
                backup_id=backup_id,
                result=result,
            )
            return result
        except asyncio.CancelledError:
            try:
                record_backup_operation_step(
                    int(guild.id),
                    "SNAPSHOT",
                    int(operation_id),
                    "INTERRUPTED",
                    "FAILED",
                    "Snapshot interrompido pelo encerramento do runtime.",
                    current,
                    total,
                )
            except Exception:
                logger.exception(
                    "Could not append cancelled Backup snapshot step: guild=%s operation=%s",
                    guild.id,
                    operation_id,
                )
            try:
                complete_backup_snapshot_operation(
                    int(operation_id),
                    int(guild.id),
                    status="FAILED",
                    result={"warnings": ["snapshot_interrupted_by_runtime_shutdown"]},
                    error_code="snapshot_interrupted_by_runtime_shutdown",
                )
            except Exception:
                logger.exception(
                    "Could not finalize cancelled Backup snapshot: guild=%s operation=%s",
                    guild.id,
                    operation_id,
                )
            raise
        except Exception:
            logger.exception(
                "Backup snapshot operation failed: guild=%s operation=%s",
                guild.id,
                operation_id,
            )
            try:
                record_backup_operation_step(
                    int(guild.id),
                    "SNAPSHOT",
                    int(operation_id),
                    "FAILED",
                    "FAILED",
                    "Falha ao gerar o snapshot.",
                    current,
                    total,
                )
            except Exception:
                logger.exception(
                    "Could not append failed Backup snapshot step: guild=%s operation=%s",
                    guild.id,
                    operation_id,
                )
            complete_backup_snapshot_operation(
                int(operation_id),
                int(guild.id),
                status="FAILED",
                result={"warnings": ["unexpected_snapshot_failure"]},
                error_code="unexpected_snapshot_failure",
            )
            raise
        finally:
            release_backup_guild_lock(int(guild.id), lock_token)

    def build_restore_preflight(
        self,
        guild: discord.Guild,
        backup_id: int,
        scope: str,
    ) -> dict[str, Any]:
        from core.database import get_discord_backup_payload

        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope not in RESTORE_SCOPES:
            raise ValueError("invalid_backup_restore_scope")

        payload = get_discord_backup_payload(int(backup_id), int(guild.id))
        if not payload:
            raise LookupError("backup_not_found_for_guild")

        bot_member = guild.me or guild.get_member(self.bot.user.id if self.bot.user else 0)
        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        role_mapping_required = normalized_scope in {"full", "roles", "permissions"}
        if bot_member is None:
            blockers.append({"code": "BOT_MEMBER_UNAVAILABLE"})
            bot_top_role = None
            blocking_roles: list[discord.Role] = []
        else:
            bot_top_role = bot_member.top_role
            blocking_roles = (
                [
                    role
                    for role in guild.roles
                    if role != guild.default_role
                    and role != bot_top_role
                    and role > bot_top_role
                ]
                if role_mapping_required
                else []
            )
            if blocking_roles and normalized_scope in {"full", "roles"}:
                warnings.append({
                    "code": "ROLE_POSITIONS_CLAMPED_BELOW_BOT",
                    "count": len(blocking_roles),
                    "roles": [
                        {
                            "id": str(role.id),
                            "name": role.name,
                            "position": role.position,
                        }
                        for role in sorted(
                            blocking_roles,
                            key=lambda item: item.position,
                            reverse=True,
                        )
                    ],
                })

            permissions = bot_member.guild_permissions
            missing: list[str] = []
            if normalized_scope in {"full", "roles", "permissions"} and not bool(
                getattr(permissions, "manage_roles", False)
            ):
                missing.append("MANAGE_ROLES")
            if normalized_scope in {"full", "channels", "permissions"} and not bool(
                getattr(permissions, "manage_channels", False)
            ):
                missing.append("MANAGE_CHANNELS")
            if missing:
                blockers.append({"code": "BOT_MISSING_PERMISSIONS", "permissions": missing})

        duplicates = (
            self._ambiguous_backup_roles(
                guild,
                payload["roles"],
                restore_roles=normalized_scope in {"full", "roles"},
            )
            if role_mapping_required
            else []
        )
        if duplicates:
            blockers.append({
                "code": "ROLE_RESOLUTION_REQUIRED",
                "backupRoleIds": [str(item["backupRoleId"]) for item in duplicates],
            })

        unsupported_types = sorted({
            int(channel["type"])
            for channel in payload["channels"]
            if int(channel["type"]) not in RESTORABLE_CHANNEL_TYPES
        })
        if unsupported_types and normalized_scope in {"full", "channels"}:
            warnings.append({
                "code": "UNSUPPORTED_CHANNEL_TYPES",
                "channelTypes": unsupported_types,
            })

        announcement_channels = [
            channel
            for channel in payload["channels"]
            if int(channel["type"]) == 5
        ]
        if (
            announcement_channels
            and normalized_scope in {"full", "channels"}
            and not self._supports_announcement_channels(guild)
        ):
            warnings.append({
                "code": "ANNOUNCEMENT_CHANNELS_DOWNGRADED",
                "count": len(announcement_channels),
                "channels": [
                    {
                        "backupChannelId": int(channel["id"]),
                        "name": str(channel["name"]),
                    }
                    for channel in announcement_channels
                ],
            })

        role_plan = self._preview_roles(guild, payload["roles"], normalized_scope)
        channel_plan = self._preview_channels(guild, payload["channels"], normalized_scope)

        return {
            "guildId": str(guild.id),
            "backupId": int(backup_id),
            "backup": {
                "name": payload["backup"]["original_name"],
                "type": (
                    "PERIODIC"
                    if str(payload["backup"].get("backup_type")) == "periodic"
                    else "MANUAL"
                ),
                "createdAt": self._json_time(payload["backup"].get("created_at")),
            },
            "scope": normalized_scope,
            "summary": {
                "roles": len(payload["roles"]),
                "channels": len(payload["channels"]),
                "permissionOverwrites": len(payload["overwrites"]),
                "roleOverwritesOnly": True,
            },
            "bot": {
                "topRole": (
                    {
                        "id": str(bot_top_role.id),
                        "name": bot_top_role.name,
                        "position": bot_top_role.position,
                    }
                    if bot_top_role is not None else None
                ),
                "blockingRoleCount": len(blocking_roles),
                "rolesAboveBot": [
                    {
                        "id": str(role.id),
                        "name": role.name,
                        "position": role.position,
                    }
                    for role in sorted(
                        blocking_roles,
                        key=lambda item: item.position,
                        reverse=True,
                    )
                ],
            },
            "rolePlan": role_plan,
            "channelPlan": channel_plan,
            "duplicateRoles": duplicates,
            "warnings": warnings,
            "blockers": blockers,
            "ready": len(blockers) == 0,
        }

    async def run_restore_operation(
        self,
        guild: discord.Guild,
        *,
        operation_id: int,
        backup_id: int,
        scope: str,
        decision: Optional[dict[str, Any]] = None,
    ) -> None:
        from core.database import (
            acquire_backup_guild_lock,
            complete_backup_restore_operation,
            fail_pending_backup_restore_operation,
            get_backup_restore_operation,
            get_discord_backup_payload,
            mark_backup_restore_operation_running,
            record_backup_operation_step,
            release_backup_guild_lock,
        )

        row = get_backup_restore_operation(int(operation_id), int(guild.id))
        if not row or int(row["backup_id"]) != int(backup_id):
            return
        if str(row.get("status")) != "PENDING":
            return

        persisted_scope = str(row.get("scope") or "").lower()
        if persisted_scope != str(scope or "").lower():
            fail_pending_backup_restore_operation(
                int(operation_id),
                int(guild.id),
                "backup_restore_scope_mismatch",
            )
            return

        try:
            raw_decision = row.get("decision_json")
            persisted_decision = (
                json.loads(raw_decision)
                if isinstance(raw_decision, str) and raw_decision.strip()
                else {}
            )
            if not isinstance(persisted_decision, dict):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            fail_pending_backup_restore_operation(
                int(operation_id),
                int(guild.id),
                "invalid_persisted_restore_decision",
            )
            return

        if not mark_backup_restore_operation_running(
            int(operation_id),
            int(guild.id),
        ):
            return

        lock_token = f"restore:{operation_id}"
        if not acquire_backup_guild_lock(
            int(guild.id),
            lock_token,
            "restore",
            operation_id=int(operation_id),
            ttl_seconds=7200,
        ):
            record_backup_operation_step(
                int(guild.id),
                "RESTORE",
                int(operation_id),
                "LOCK_REJECTED",
                "FAILED",
                "Outra operação de Backup já está em andamento nesta guild.",
                0,
                1,
                detail={"reason": "backup_operation_in_progress"},
            )
            complete_backup_restore_operation(
                int(operation_id),
                int(guild.id),
                status="FAILED",
                result={"warnings": ["backup_operation_in_progress"]},
                error_code="backup_operation_in_progress",
            )
            return

        heartbeat: Optional[asyncio.Task] = None
        progress_total = 0

        def terminal_step(
            code: str,
            status: str,
            message: str,
            *,
            detail: Optional[dict[str, Any]] = None,
        ) -> None:
            latest = get_backup_restore_operation(
                int(operation_id),
                int(guild.id),
            ) or {}
            current = max(0, int(latest.get("progress_current") or 0))
            total = max(
                current,
                int(latest.get("progress_total") or progress_total or 0),
            )
            record_backup_operation_step(
                int(guild.id),
                "RESTORE",
                int(operation_id),
                code,
                status,
                message,
                current,
                total,
                detail=detail,
            )

        try:
            heartbeat = asyncio.create_task(
                self._heartbeat_backup_lock(int(guild.id), lock_token),
                name=f"backup-lock-heartbeat-{guild.id}-{operation_id}",
            )

            payload = get_discord_backup_payload(int(backup_id), int(guild.id))
            if not payload:
                record_backup_operation_step(
                    int(guild.id),
                    "RESTORE",
                    int(operation_id),
                    "BACKUP_NOT_FOUND",
                    "FAILED",
                    "O snapshot não existe mais nesta guild.",
                    1,
                    1,
                )
                complete_backup_restore_operation(
                    int(operation_id),
                    int(guild.id),
                    status="FAILED",
                    result={"warnings": ["backup_not_found_for_guild"]},
                    error_code="backup_not_found_for_guild",
                )
                return

            restore_roles = persisted_scope in {"full", "roles"}
            role_mapping_required = restore_roles or persisted_scope == "permissions"
            channel_mapping_required = persisted_scope in {
                "full", "channels", "permissions"
            }
            progress_total = (
                4
                + (len(payload["roles"]) if role_mapping_required else 0)
                + (len(payload["channels"]) if channel_mapping_required else 0)
                + (
                    len(payload["overwrites"])
                    if persisted_scope in {"full", "permissions"} else 0
                )
                + (1 if restore_roles else 0)
            )
            record_backup_operation_step(
                int(guild.id),
                "RESTORE",
                int(operation_id),
                "LOCK_ACQUIRED",
                "SUCCEEDED",
                "Lock exclusivo da guild adquirido.",
                1,
                progress_total,
            )
            record_backup_operation_step(
                int(guild.id),
                "RESTORE",
                int(operation_id),
                "BACKUP_LOADED",
                "SUCCEEDED",
                "Snapshot carregado e validado para esta guild.",
                2,
                progress_total,
                detail={
                    "roles": len(payload["roles"]),
                    "channels": len(payload["channels"]),
                    "permissionOverwrites": len(payload["overwrites"]),
                },
            )

            preflight = self.build_restore_preflight(
                guild,
                int(backup_id),
                persisted_scope,
            )
            non_resolution_blockers = [
                blocker
                for blocker in preflight["blockers"]
                if blocker.get("code") != "ROLE_RESOLUTION_REQUIRED"
            ]
            record_backup_operation_step(
                int(guild.id),
                "RESTORE",
                int(operation_id),
                "PREFLIGHT_VALIDATED",
                "FAILED" if non_resolution_blockers else "SUCCEEDED",
                (
                    "Preflight bloqueou a restauração."
                    if non_resolution_blockers
                    else "Preflight revalidado contra o estado vivo do Discord."
                ),
                3,
                progress_total,
                detail={
                    "blockers": preflight.get("blockers", []),
                    "warnings": preflight.get("warnings", []),
                },
            )
            if non_resolution_blockers:
                complete_backup_restore_operation(
                    int(operation_id),
                    int(guild.id),
                    status="FAILED",
                    result={"preflight": preflight},
                    error_code="restore_preflight_blocked",
                )
                return

            try:
                result = await self.execute_restore(
                    guild,
                    int(backup_id),
                    payload,
                    scope=persisted_scope,
                    decision=persisted_decision,
                    operation_id=int(operation_id),
                    progress_start=3,
                    progress_total=progress_total,
                )
            except BackupDecisionRequired as exc:
                terminal_step(
                    "DECISION_REQUIRED",
                    "FAILED",
                    "O estado vivo exige uma nova decisão antes de continuar o restore.",
                    detail={"reason": str(exc)},
                )
                complete_backup_restore_operation(
                    int(operation_id),
                    int(guild.id),
                    status="FAILED",
                    result={"preflight": preflight, "warnings": [str(exc)]},
                    error_code="restore_decision_required",
                )
                return

            if result["status"] == "SUCCEEDED" or (
                result["status"] == "PARTIAL" and not result.get("aborted", False)
            ):
                # An exhaustive partial restore has executed the entire plan;
                # the final step legitimately advances to progress_total.
                record_backup_operation_step(
                    int(guild.id),
                    "RESTORE",
                    int(operation_id),
                    "COMPLETED",
                    "SUCCEEDED" if result["status"] == "SUCCEEDED" else "FAILED",
                    (
                        "Restauração concluída."
                        if result["status"] == "SUCCEEDED"
                        else "Restauração concluída com ocorrências parciais."
                    ),
                    progress_total,
                    progress_total,
                    detail={"status": result["status"]},
                )
            else:
                # An aborted restore must never advance unexecuted planned steps.
                terminal_step(
                    "COMPLETED",
                    "FAILED",
                    "Restauração interrompida com alterações parciais."
                    if result["status"] == "PARTIAL"
                    else "Restauração encerrada com falhas.",
                    detail={"status": result["status"], "aborted": True},
                )
            complete_backup_restore_operation(
                int(operation_id),
                int(guild.id),
                status=result["status"],
                result=result,
                error_code=None if result["status"] != "FAILED" else "restore_failed",
            )
        except asyncio.CancelledError:
            try:
                terminal_step(
                    "INTERRUPTED",
                    "FAILED",
                    "Restore interrompido pelo encerramento do runtime.",
                    detail={"reason": "runtime_shutdown"},
                )
            except Exception:
                logger.exception(
                    "Could not append cancelled Backup restore step: guild=%s operation=%s backup=%s",
                    guild.id,
                    operation_id,
                    backup_id,
                )
            try:
                complete_backup_restore_operation(
                    int(operation_id),
                    int(guild.id),
                    status="PARTIAL",
                    result={"warnings": ["restore_interrupted_by_runtime_shutdown"]},
                    error_code="restore_interrupted_by_runtime_shutdown",
                )
            except Exception:
                logger.exception(
                    "Could not finalize cancelled Backup restore: guild=%s operation=%s backup=%s",
                    guild.id,
                    operation_id,
                    backup_id,
                )
            raise
        except Exception as error:
            logger.exception(
                "Backup restore operation failed: guild=%s operation=%s backup=%s",
                guild.id,
                operation_id,
                backup_id,
            )
            error_detail = self._restore_error_detail(error)
            try:
                terminal_step(
                    "FAILED",
                    "FAILED",
                    "Restore interrompido por uma falha inesperada.",
                    detail={
                        "reason": "unexpected_restore_failure",
                        **error_detail,
                    },
                )
            except Exception:
                logger.exception(
                    "Could not append failed Backup restore step: guild=%s operation=%s backup=%s",
                    guild.id,
                    operation_id,
                    backup_id,
                )
            complete_backup_restore_operation(
                int(operation_id),
                int(guild.id),
                status="FAILED",
                result={
                    "warnings": ["unexpected_restore_failure"],
                    "error": error_detail,
                },
                error_code="unexpected_restore_failure",
            )
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
            release_backup_guild_lock(int(guild.id), lock_token)

    async def _heartbeat_backup_lock(self, guild_id: int, lock_token: str) -> None:
        from core.database import refresh_backup_guild_lock

        while True:
            await asyncio.sleep(300)
            try:
                refreshed = refresh_backup_guild_lock(
                    guild_id,
                    lock_token,
                    ttl_seconds=7200,
                )
            except Exception:
                logger.exception(
                    "Backup restore lock heartbeat failed: guild=%s token=%s",
                    guild_id,
                    lock_token,
                )
                continue
            if not refreshed:
                logger.critical(
                    "Backup restore lost durable lock ownership: guild=%s token=%s",
                    guild_id,
                    lock_token,
                )
                return


    async def execute_restore(
        self,
        guild: discord.Guild,
        backup_id: int,
        payload: dict[str, Any],
        *,
        scope: str,
        decision: dict[str, Any],
        operation_id: Optional[int] = None,
        progress_start: int = 0,
        progress_total: Optional[int] = None,
    ) -> dict[str, Any]:
        normalized_scope = str(scope or "").lower()
        if normalized_scope not in RESTORE_SCOPES:
            raise ValueError("invalid_backup_restore_scope")

        restore_roles = normalized_scope in {"full", "roles"}
        restore_channels = normalized_scope in {"full", "channels"}
        restore_permissions = normalized_scope in {"full", "permissions"}
        role_mapping_required = restore_roles or restore_permissions

        result = {
            "status": "SUCCEEDED",
            "roles": self._bucket(),
            "channels": self._bucket(),
            "permissions": self._bucket(),
            "warnings": [],
        }

        progress_current = max(0, int(progress_start))
        computed_total = (
            progress_current
            + (len(payload["roles"]) if role_mapping_required else 0)
            + (
                len(payload["channels"])
                if restore_channels or restore_permissions else 0
            )
            + (len(payload["overwrites"]) if restore_permissions else 0)
            + (1 if restore_roles else 0)
            + 1
        )
        effective_total = max(
            computed_total,
            int(progress_total or 0),
        )

        def progress(
            code: str,
            status: str,
            message: str,
            *,
            detail: Optional[dict[str, Any]] = None,
            advance: bool = True,
        ) -> None:
            nonlocal progress_current
            if advance:
                progress_current += 1
            if operation_id is None:
                return
            from core.database import record_backup_operation_step
            record_backup_operation_step(
                int(guild.id),
                "RESTORE",
                int(operation_id),
                code,
                status,
                message,
                min(progress_current, effective_total),
                effective_total,
                detail=detail,
            )

        duplicate_strategy = str(decision.get("duplicateStrategy") or "explicit").lower()
        resolutions = decision.get("roleResolutions") or {}
        if not isinstance(resolutions, dict):
            raise BackupDecisionRequired("invalid_role_resolutions")

        ambiguous = (
            self._ambiguous_backup_roles(
                guild,
                payload["roles"],
                restore_roles=restore_roles,
            )
            if role_mapping_required
            else []
        )
        if ambiguous and duplicate_strategy not in {"merge", "explicit"}:
            raise BackupDecisionRequired("duplicate_role_strategy_required")
        if ambiguous and duplicate_strategy == "merge" and not restore_roles:
            raise BackupDecisionRequired("duplicate_merge_requires_roles_scope")
        if ambiguous and duplicate_strategy == "explicit":
            missing = [
                str(item["backupRoleId"])
                for item in ambiguous
                if str(item["backupRoleId"]) not in resolutions
            ]
            if missing:
                raise BackupDecisionRequired(
                    "role_resolution_required:" + ",".join(missing)
                )

        if ambiguous and duplicate_strategy == "merge":
            issues = await self._merge_duplicate_roles(
                guild,
                {
                    item["name"]: [
                        guild.get_role(int(candidate["id"]))
                        for candidate in item["candidates"]
                        if guild.get_role(int(candidate["id"])) is not None
                    ]
                    for item in ambiguous
                },
                backup_id,
            )
            if issues:
                result["warnings"].extend(issues)
                result["roles"]["failed"] += len(issues)
            progress(
                "DUPLICATE_ROLES_MERGED",
                "FAILED" if issues else "SUCCEEDED",
                (
                    "Cargos duplicados foram unificados."
                    if not issues
                    else "A unificação de cargos duplicados teve falhas parciais."
                ),
                detail={"issues": issues},
                advance=False,
            )

        role_map: dict[int, int] = {}
        resolved_roles: dict[int, discord.Role] = {}
        role_items = (
            sorted(
                payload["roles"],
                key=lambda item: int(item["position"] or 0),
                reverse=True,
            )
            if restore_roles
            else payload["roles"]
        ) if role_mapping_required else []
        for role_data in role_items:
            backup_role_pk = int(role_data["id"])
            progress(
                "ROLE_RESTORE_STARTED",
                "RUNNING",
                f"Restaurando cargo {role_data['name']}...",
                detail={
                    "backupRoleId": backup_role_pk,
                    "savedPosition": int(role_data["position"] or 0),
                },
                advance=False,
            )
            previous_role_ids = {int(role.id) for role in guild.roles}
            try:
                role, action = await asyncio.wait_for(
                    self._resolve_role(
                        guild,
                        backup_id,
                        role_data,
                        restore_roles=restore_roles,
                        duplicate_strategy=duplicate_strategy,
                        explicit_resolution=resolutions.get(str(backup_role_pk)),
                    ),
                    timeout=ROLE_MUTATION_TIMEOUT_SECONDS,
                )
            except discord.RateLimited as exc:
                reconciliation = await self._find_possible_created_role(
                    guild, role_data, previous_role_ids
                )
                result["roles"]["failed"] += 1
                result["warnings"].extend([
                    "discord_role_creation_cooldown",
                    "restore_aborted_after_discord_rate_limit",
                ])
                progress(
                    "ROLE_RATE_LIMITED",
                    "FAILED",
                    "O Discord impôs um cooldown para criação/alteração de cargos.",
                    detail={
                        "backupRoleId": backup_role_pk,
                        "retryAfterSeconds": round(float(exc.retry_after), 2),
                        "possibleCreatedRole": reconciliation,
                        "bucket": self._role_create_bucket_state(guild),
                    },
                )
                logger.error(
                    "Backup role restore stopped by Discord rate limit: "
                    "guild=%s backup=%s role=%s retry_after=%ss reconcile=%s",
                    guild.id, backup_id, role_data["name"],
                    round(float(exc.retry_after), 2), reconciliation,
                )
                result["status"] = "PARTIAL"
                result["aborted"] = True
                return result
            except asyncio.TimeoutError:
                reconciliation = await self._find_possible_created_role(
                    guild, role_data, previous_role_ids
                )
                result["roles"]["failed"] += 1
                result["warnings"].append(f"role_timeout:{role_data['name']}")
                progress(
                    "ROLE_TIMEOUT",
                    "FAILED",
                    f"Tempo limite excedido ao restaurar cargo {role_data['name']}.",
                    detail={
                        "backupRoleId": backup_role_pk,
                        "timeoutSeconds": int(ROLE_MUTATION_TIMEOUT_SECONDS),
                        "possibleCreatedRole": reconciliation,
                        "bucket": self._role_create_bucket_state(guild),
                    },
                )
                logger.error(
                    "Backup role restore stopped after a Discord mutation timeout: "
                    "guild=%s backup=%s role=%s timeout=%ss; "
                    "remaining mutations will not be attempted",
                    guild.id,
                    backup_id,
                    role_data["name"],
                    int(ROLE_MUTATION_TIMEOUT_SECONDS),
                )
                progress(
                    "ROLE_RESTORE_ABORTED",
                    "FAILED",
                    "Restore interrompido após timeout no Discord; "
                    "não serão feitas novas mutações nesta operação.",
                    detail={"backupRoleId": backup_role_pk, "reason": "discord_role_timeout"},
                    advance=False,
                )
                result["warnings"].append("restore_aborted_after_discord_timeout")
                result["status"] = "PARTIAL"
                result["aborted"] = True
                return result
            except (discord.Forbidden, discord.HTTPException) as exc:
                if isinstance(exc, discord.Forbidden) or self._is_missing_permissions_error(exc):
                    result["roles"]["failed"] += 1
                    result["warnings"].append(f"role_permission:{role_data['name']}")
                    progress(
                        "ROLE_FAILED",
                        "FAILED",
                        f"Falha de permissão ao restaurar cargo {role_data['name']}.",
                        detail={"backupRoleId": backup_role_pk},
                    )
                    continue
                raise

            if role is None:
                result["roles"]["skipped"] += 1
                result["warnings"].append(f"role_unresolved:{role_data['name']}")
                progress(
                    "ROLE_SKIPPED",
                    "SKIPPED",
                    f"Cargo {role_data['name']} não pôde ser resolvido.",
                    detail={"backupRoleId": backup_role_pk},
                )
                continue

            role_map[backup_role_pk] = int(role.id)
            resolved_roles[backup_role_pk] = role
            result["roles"][action] += 1
            role_mutated = action == "created"
            if (
                restore_roles
                and action != "created"
                and not role.managed
                and role != guild.default_role
            ):
                try:
                    await asyncio.wait_for(
                        role.edit(
                            name=role_data["name"],
                            colour=discord.Colour(int(role_data["color"] or 0)),
                            permissions=discord.Permissions(int(role_data["permissions"] or 0)),
                            hoist=bool(role_data["hoist"]),
                            mentionable=bool(role_data["mentionable"]),
                            reason=f"Sincronização do backup {backup_id}",
                        ),
                        timeout=ROLE_MUTATION_TIMEOUT_SECONDS,
                    )
                    role_mutated = True
                except discord.RateLimited as exc:
                    result["roles"]["failed"] += 1
                    result["warnings"].extend([
                        "discord_role_edit_cooldown",
                        "restore_aborted_after_discord_rate_limit",
                    ])
                    progress(
                        "ROLE_RATE_LIMITED",
                        "FAILED",
                        f"O Discord limitou a edição do cargo {role_data['name']}.",
                        detail={
                            "backupRoleId": backup_role_pk,
                            "roleId": str(role.id),
                            "retryAfterSeconds": round(float(exc.retry_after), 2),
                        },
                    )
                    logger.error(
                        "Backup role edit stopped by Discord rate limit: "
                        "guild=%s backup=%s role=%s retry_after=%ss",
                        guild.id, backup_id, role_data["name"],
                        round(float(exc.retry_after), 2),
                    )
                    result["status"] = "PARTIAL"
                    result["aborted"] = True
                    return result
                except asyncio.TimeoutError:
                    result["roles"]["failed"] += 1
                    result["warnings"].append(f"role_edit_timeout:{role_data['name']}")
                    progress(
                        "ROLE_TIMEOUT",
                        "FAILED",
                        f"Tempo limite excedido ao atualizar cargo {role_data['name']}.",
                        detail={
                            "backupRoleId": backup_role_pk,
                            "roleId": str(role.id),
                            "timeoutSeconds": int(ROLE_MUTATION_TIMEOUT_SECONDS),
                        },
                    )
                    logger.error(
                        "Backup role restore stopped after a Discord edit timeout: "
                        "guild=%s backup=%s role=%s timeout=%ss; "
                        "remaining mutations will not be attempted",
                        guild.id,
                        backup_id,
                        role_data["name"],
                        int(ROLE_MUTATION_TIMEOUT_SECONDS),
                    )
                    progress(
                        "ROLE_RESTORE_ABORTED",
                        "FAILED",
                        "Restore interrompido após timeout no Discord; "
                        "não serão feitas novas mutações nesta operação.",
                        detail={
                            "backupRoleId": backup_role_pk,
                            "roleId": str(role.id),
                            "reason": "discord_role_edit_timeout",
                        },
                        advance=False,
                    )
                    result["warnings"].append("restore_aborted_after_discord_timeout")
                    result["status"] = "PARTIAL"
                    result["aborted"] = True
                    return result
                except (discord.Forbidden, discord.HTTPException) as exc:
                    if isinstance(exc, discord.Forbidden) or self._is_missing_permissions_error(exc):
                        result["roles"]["failed"] += 1
                        result["warnings"].append(f"role_edit_failed:{role_data['name']}")
                        progress(
                            "ROLE_FAILED",
                            "FAILED",
                            f"Falha ao aplicar especificações do cargo {role_data['name']}.",
                            detail={"backupRoleId": backup_role_pk, "roleId": str(role.id)},
                        )
                        continue
                    raise

            if role_mutated:
                await asyncio.sleep(ROLE_MUTATION_DELAY_SECONDS)

            progress(
                "ROLE_RESTORED",
                "SUCCEEDED",
                f"Cargo {role_data['name']} restaurado ({action}).",
                detail={
                    "backupRoleId": backup_role_pk,
                    "roleId": str(role.id),
                    "action": action,
                },
            )

        if restore_roles:
            positions: dict[discord.Role, int] = {}
            bot_member = guild.me or guild.get_member(
                self.bot.user.id if self.bot.user else 0
            )
            bot_top_position = (
                int(bot_member.top_role.position)
                if bot_member is not None else 1
            )
            next_available = max(1, bot_top_position - 1)
            restored_roles: list[tuple[int, discord.Role]] = []
            for role_data in payload["roles"]:
                backup_role_pk = int(role_data["id"])
                role = resolved_roles.get(backup_role_pk)
                if role is None:
                    role_id = role_map.get(backup_role_pk)
                    role = guild.get_role(role_id) if role_id else None
                if role and self._role_editable_by_bot(guild, role):
                    restored_roles.append(
                        (int(role_data["position"] or 0), role)
                    )
            for saved_position, role in sorted(
                restored_roles,
                key=lambda item: item[0],
                reverse=True,
            ):
                target = max(1, min(saved_position, next_available))
                positions[role] = target
                next_available = max(1, target - 1)
            if positions:
                position_failed = False
                position_timed_out = False
                try:
                    await asyncio.wait_for(
                        guild.edit_role_positions(positions),
                        timeout=ROLE_MUTATION_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    result["roles"]["failed"] += len(positions)
                    result["warnings"].append("role_position_update_timeout")
                    position_failed = True
                    position_timed_out = True
                except (discord.Forbidden, discord.HTTPException) as exc:
                    if isinstance(exc, discord.Forbidden) or self._is_missing_permissions_error(exc):
                        result["roles"]["failed"] += len(positions)
                        result["warnings"].append("role_position_update_failed")
                        position_failed = True
                    else:
                        raise
                progress(
                    (
                        "ROLE_POSITIONS_TIMEOUT"
                        if position_timed_out
                        else "ROLE_POSITIONS_APPLIED"
                    ),
                    "FAILED" if position_failed else "SUCCEEDED",
                    (
                        "Tempo limite excedido ao reposicionar os cargos."
                        if position_timed_out
                        else "Não foi possível posicionar todos os cargos abaixo do Coddy."
                        if position_failed
                        else "Cargos reposicionados abaixo do Coddy."
                    ),
                    detail={
                        "count": len(positions),
                        "botTopPosition": bot_top_position,
                        "timeoutSeconds": (
                            int(ROLE_MUTATION_TIMEOUT_SECONDS)
                            if position_timed_out
                            else None
                        ),
                    },
                )
            else:
                progress(
                    "ROLE_POSITIONS_APPLIED",
                    "SUCCEEDED",
                    "Nenhum reposicionamento de cargo foi necessário.",
                    detail={
                        "count": 0,
                        "botTopPosition": bot_top_position,
                    },
                )

        channel_mapping_required = restore_channels or restore_permissions
        categories = [
            item for item in payload["channels"] if int(item["type"]) == 4
        ] if channel_mapping_required else []
        channels = [
            item for item in payload["channels"]
            if int(item["type"]) in RESTORABLE_CHANNEL_TYPES
            and int(item["type"]) != 4
        ] if channel_mapping_required else []
        unsupported = [
            item for item in payload["channels"]
            if int(item["type"]) not in RESTORABLE_CHANNEL_TYPES
        ] if channel_mapping_required else []
        if unsupported:
            if restore_channels:
                result["channels"]["skipped"] += len(unsupported)
                result["warnings"].append(
                    "unsupported_channel_types:"
                    + ",".join(sorted({str(item["type"]) for item in unsupported}))
                )
            for item in unsupported:
                progress(
                    "CHANNEL_SKIPPED",
                    "SKIPPED",
                    f"Canal {item['name']} usa um tipo não suportado nesta restauração.",
                    detail={
                        "backupChannelId": int(item["id"]),
                        "channelType": int(item["type"]),
                    },
                )

        channel_map: dict[int, int] = {}
        category_map: dict[int, int] = {}

        for category_data in categories:
            old_id = int(category_data["discord_id"])
            category = guild.get_channel(old_id)
            if not isinstance(category, discord.CategoryChannel):
                matches = [item for item in guild.categories if item.name == category_data["name"]]
                category = matches[0] if len(matches) == 1 else None
            action = "reused"
            if restore_channels:
                try:
                    if category is None:
                        category = await guild.create_category(
                            name=category_data["name"],
                            position=int(category_data["position"] or 0),
                            reason=f"Restauração do backup {backup_id}",
                        )
                        action = "created"
                        await asyncio.sleep(CHANNEL_CREATION_DELAY_SECONDS)
                    else:
                        await category.edit(
                            name=category_data["name"],
                            position=int(category_data["position"] or 0),
                            reason=f"Sincronização do backup {backup_id}",
                        )
                        action = "updated"
                except discord.HTTPException as exc:
                    result["channels"]["failed"] += 1
                    result["warnings"].append(
                        f"category_failed:{category_data['name']}:{getattr(exc, 'code', 0)}"
                    )
                    progress(
                        "CATEGORY_FAILED",
                        "FAILED",
                        f"Falha ao restaurar categoria {category_data['name']}.",
                        detail={
                            "backupChannelId": int(category_data["id"]),
                            **self._restore_error_detail(exc),
                        },
                    )
                    logger.warning(
                        "Backup category restore failed but operation will continue: "
                        "guild=%s backup=%s category=%s error=%s",
                        guild.id,
                        backup_id,
                        category_data["name"],
                        exc,
                    )
                    continue
            if isinstance(category, discord.CategoryChannel):
                channel_map[int(category_data["id"])] = int(category.id)
                category_map[old_id] = int(category.id)
                result["channels"][action] += 1
                progress(
                    "CATEGORY_RESTORED",
                    "SUCCEEDED",
                    f"Categoria {category_data['name']} processada ({action}).",
                    detail={
                        "backupChannelId": int(category_data["id"]),
                        "channelId": str(category.id),
                        "action": action,
                    },
                )
            else:
                result["channels"]["skipped"] += 1
                progress(
                    "CATEGORY_SKIPPED",
                    "SKIPPED",
                    f"Categoria {category_data['name']} não pôde ser resolvida.",
                    detail={"backupChannelId": int(category_data["id"])},
                )

        for channel_data in channels:
            old_id = int(channel_data["discord_id"])
            parent: Optional[discord.CategoryChannel] = None
            if channel_data.get("parent_id"):
                parent_id = category_map.get(
                    int(channel_data["parent_id"]),
                    int(channel_data["parent_id"]),
                )
                maybe_parent = guild.get_channel(parent_id)
                if isinstance(maybe_parent, discord.CategoryChannel):
                    parent = maybe_parent

            current = guild.get_channel(old_id)
            if current is None:
                current = self._find_channel_by_fallback(
                    guild,
                    str(channel_data["name"]),
                    int(channel_data["type"]),
                    parent,
                )

            action = "reused"
            announcement_downgraded = (
                int(channel_data["type"]) == 5
                and not self._supports_announcement_channels(guild)
            )
            try:
                if current is None and restore_channels:
                    current = await self._create_channel_by_type(
                        guild,
                        channel_data,
                        parent,
                        backup_id,
                    )
                    if current is None:
                        result["channels"]["skipped"] += 1
                        progress(
                            "CHANNEL_SKIPPED",
                            "SKIPPED",
                            f"Canal {channel_data['name']} não pôde ser criado.",
                            detail={"backupChannelId": int(channel_data["id"])},
                        )
                        continue
                    action = "created"
                    if announcement_downgraded:
                        result["warnings"].append(
                            f"channel_type_downgraded:{channel_data['name']}:5->0"
                        )
                    await asyncio.sleep(CHANNEL_CREATION_DELAY_SECONDS)
                elif current is not None and restore_channels:
                    await self._edit_channel_by_type(
                        current,
                        channel_data,
                        parent,
                        backup_id,
                    )
                    action = "updated"
            except discord.HTTPException as exc:
                result["channels"]["failed"] += 1
                result["warnings"].append(
                    f"channel_failed:{channel_data['name']}:{getattr(exc, 'code', 0)}"
                )
                progress(
                    "CHANNEL_FAILED",
                    "FAILED",
                    f"Falha ao restaurar canal {channel_data['name']}.",
                    detail={
                        "backupChannelId": int(channel_data["id"]),
                        "channelType": int(channel_data["type"]),
                        **self._restore_error_detail(exc),
                    },
                )
                logger.warning(
                    "Backup channel restore failed but operation will continue: "
                    "guild=%s backup=%s channel=%s type=%s error=%s",
                    guild.id,
                    backup_id,
                    channel_data["name"],
                    channel_data["type"],
                    exc,
                )
                continue

            if current is not None:
                channel_map[int(channel_data["id"])] = int(current.id)
                result["channels"][action] += 1
                progress(
                    "CHANNEL_RESTORED",
                    "SUCCEEDED",
                    f"Canal {channel_data['name']} processado ({action}).",
                    detail={
                        "backupChannelId": int(channel_data["id"]),
                        "channelId": str(current.id),
                        "action": action,
                        "downgradedFromType": (
                            5 if announcement_downgraded else None
                        ),
                        "restoredAsType": (
                            0 if announcement_downgraded else int(channel_data["type"])
                        ),
                    },
                )
            else:
                result["channels"]["skipped"] += 1
                progress(
                    "CHANNEL_SKIPPED",
                    "SKIPPED",
                    f"Canal {channel_data['name']} não pôde ser resolvido.",
                    detail={"backupChannelId": int(channel_data["id"])},
                )

        if restore_permissions:
            for overwrite in payload["overwrites"]:
                mapped_channel_id = channel_map.get(int(overwrite["channel_id"]))
                channel = guild.get_channel(mapped_channel_id) if mapped_channel_id else None
                target_type = str(overwrite.get("target_type") or "ROLE").upper()
                target_discord_id = overwrite.get("target_discord_id")
                role: Optional[discord.Role] = None
                if target_type == "EVERYONE":
                    role = guild.default_role
                elif target_type == "MANAGED_ROLE":
                    if target_discord_id is not None:
                        candidate = guild.get_role(int(target_discord_id))
                        if candidate is not None and candidate.managed:
                            role = candidate
                else:
                    raw_role_id = overwrite.get("role_id")
                    if raw_role_id is not None:
                        backup_role_pk = int(raw_role_id)
                        mapped_role_id = role_map.get(backup_role_pk)
                        role = resolved_roles.get(backup_role_pk)
                        if role is None and mapped_role_id:
                            role = guild.get_role(mapped_role_id)
                if channel is None or role is None:
                    result["permissions"]["skipped"] += 1
                    result["warnings"].append(
                        f"overwrite_unresolved:{overwrite['role_name']}"
                    )
                    progress(
                        "PERMISSION_SKIPPED",
                        "SKIPPED",
                        f"Overwrite para {overwrite['role_name']} não pôde ser resolvido.",
                        detail={
                            "backupChannelId": int(overwrite["channel_id"]),
                            "backupRoleId": (
                                int(overwrite["role_id"])
                                if overwrite.get("role_id") is not None
                                else None
                            ),
                            "targetType": target_type,
                            "targetDiscordRoleId": (
                                str(target_discord_id)
                                if target_discord_id is not None
                                else None
                            ),
                        },
                    )
                    continue

                permission_overwrite = discord.PermissionOverwrite.from_pair(
                    discord.Permissions(int(overwrite["allow_bits"] or 0)),
                    discord.Permissions(int(overwrite["deny_bits"] or 0)),
                )
                try:
                    await channel.set_permissions(
                        role,
                        overwrite=permission_overwrite,
                        reason=f"Aplicação de permissões do backup {backup_id}",
                    )
                    result["permissions"]["updated"] += 1
                    progress(
                        "PERMISSION_APPLIED",
                        "SUCCEEDED",
                        f"Overwrite aplicado para {role.name}.",
                        detail={
                            "channelId": str(channel.id),
                            "roleId": str(role.id),
                        },
                    )
                except discord.HTTPException as exc:
                    result["permissions"]["failed"] += 1
                    result["warnings"].append(
                        f"overwrite_failed:{getattr(channel, 'name', channel.id)}:{role.name}"
                    )
                    progress(
                        "PERMISSION_FAILED",
                        "FAILED",
                        f"Falha ao aplicar overwrite para {role.name}.",
                        detail={
                            "channelId": str(channel.id),
                            "roleId": str(role.id),
                            **self._restore_error_detail(exc),
                        },
                    )
                    logger.warning(
                        "Backup overwrite restore failed but operation will continue: "
                        "guild=%s backup=%s channel=%s role=%s error=%s",
                        guild.id,
                        backup_id,
                        getattr(channel, "name", channel.id),
                        role.name,
                        exc,
                    )

        requested_buckets = []
        if restore_roles:
            requested_buckets.append(result["roles"])
        if restore_channels:
            requested_buckets.append(result["channels"])
        if restore_permissions:
            requested_buckets.append(result["permissions"])
        has_channel_downgrade = any(
            str(warning).startswith("channel_type_downgraded:")
            for warning in result["warnings"]
        )
        if (
            any(
                bucket["failed"] > 0 or bucket["skipped"] > 0
                for bucket in requested_buckets
            )
            or has_channel_downgrade
        ):
            result["status"] = "PARTIAL"

        return result

    def _role_editable_by_bot(
        self,
        guild: discord.Guild,
        role: discord.Role,
    ) -> bool:
        if role == guild.default_role or role.managed:
            return False
        bot_member = guild.me or guild.get_member(
            self.bot.user.id if self.bot.user else 0
        )
        return bot_member is not None and role < bot_member.top_role

    def _ambiguous_backup_roles(
        self,
        guild: discord.Guild,
        backup_roles: list[dict],
        *,
        restore_roles: bool = False,
    ) -> list[dict[str, Any]]:
        ambiguous: list[dict[str, Any]] = []
        for role_data in backup_roles:
            exact = guild.get_role(int(role_data["discord_id"]))
            if exact is not None and (
                not restore_roles or self._role_editable_by_bot(guild, exact)
            ):
                continue
            matches = [
                role for role in guild.roles
                if role != guild.default_role
                and not role.managed
                and role.name == role_data["name"]
                and (
                    not restore_roles
                    or self._role_editable_by_bot(guild, role)
                )
            ]
            if len(matches) <= 1:
                continue
            ambiguous.append({
                "backupRoleId": int(role_data["id"]),
                "backupDiscordRoleId": str(role_data["discord_id"]),
                "name": str(role_data["name"]),
                "candidates": [
                    {
                        "id": str(role.id),
                        "name": role.name,
                        "position": role.position,
                    }
                    for role in sorted(
                        matches,
                        key=lambda item: item.position,
                        reverse=True,
                    )
                ],
            })
        return ambiguous

    def _preview_roles(self, guild, roles: list[dict], scope: str) -> dict[str, int]:
        counts = {"create": 0, "update": 0, "reuse": 0, "ambiguous": 0, "skip": 0}
        restore_roles = scope in {"full", "roles"}
        for item in roles:
            existing = guild.get_role(int(item["discord_id"]))
            if existing is not None:
                if existing == guild.default_role or existing.managed:
                    counts["reuse"] += 1
                    continue
                if not restore_roles or self._role_editable_by_bot(guild, existing):
                    counts["update" if restore_roles else "reuse"] += 1
                    continue
            if (
                str(item.get("name") or "") == "@everyone"
                and int(item.get("position") or 0) == 0
            ):
                counts["reuse"] += 1
                continue
            matches = [
                role for role in guild.roles
                if role.name == item["name"]
                and not role.managed
                and (
                    not restore_roles
                    or self._role_editable_by_bot(guild, role)
                )
            ]
            if len(matches) == 1:
                counts["update" if restore_roles else "reuse"] += 1
            elif len(matches) > 1:
                counts["ambiguous"] += 1
            elif restore_roles:
                counts["create"] += 1
            else:
                counts["skip"] += 1
        return counts

    def _preview_channels(self, guild, channels: list[dict], scope: str) -> dict[str, int]:
        counts = {"create": 0, "update": 0, "reuse": 0, "skip": 0}
        restore_channels = scope in {"full", "channels"}
        for item in channels:
            if int(item["type"]) not in RESTORABLE_CHANNEL_TYPES:
                counts["skip"] += 1
                continue
            existing = guild.get_channel(int(item["discord_id"]))
            if existing is not None:
                counts["update" if restore_channels else "reuse"] += 1
            elif restore_channels:
                counts["create"] += 1
            else:
                counts["skip"] += 1
        return counts

    async def _resolve_role(
        self,
        guild: discord.Guild,
        backup_id: int,
        role_data: dict,
        *,
        restore_roles: bool,
        duplicate_strategy: str,
        explicit_resolution: Any,
    ) -> tuple[Optional[discord.Role], str]:
        role = guild.get_role(int(role_data["discord_id"]))
        if role is not None:
            if role == guild.default_role or role.managed:
                return role, "reused"
            if not restore_roles or self._role_editable_by_bot(guild, role):
                return role, "updated" if restore_roles else "reused"

        if (
            str(role_data.get("name") or "") == "@everyone"
            and int(role_data.get("position") or 0) == 0
        ):
            return guild.default_role, "reused"

        same_name = [
            item for item in guild.roles
            if item != guild.default_role
            and not item.managed
            and item.name == role_data["name"]
            and (
                not restore_roles
                or self._role_editable_by_bot(guild, item)
            )
        ]
        if len(same_name) == 1:
            return same_name[0], "updated" if restore_roles else "reused"

        if len(same_name) > 1:
            if duplicate_strategy == "merge":
                role = sorted(
                    same_name,
                    key=lambda item: item.position,
                    reverse=True,
                )[0]
                return role, "updated" if restore_roles else "reused"

            if explicit_resolution == _CREATE_NEW:
                if not restore_roles:
                    return None, "skipped"
                return await self._create_role(guild, backup_id, role_data), "created"

            try:
                role_id = int(str(explicit_resolution))
            except (TypeError, ValueError):
                raise BackupDecisionRequired(
                    f"role_resolution_required:{role_data['id']}"
                )
            selected = guild.get_role(role_id)
            if (
                selected is None
                or selected.managed
                or selected == guild.default_role
                or (
                    restore_roles
                    and not self._role_editable_by_bot(guild, selected)
                )
                or all(int(candidate.id) != int(selected.id) for candidate in same_name)
            ):
                raise BackupDecisionRequired(
                    f"invalid_role_resolution:{role_data['id']}"
                )
            return selected, "updated" if restore_roles else "reused"

        if not restore_roles:
            return None, "skipped"
        return await self._create_role(guild, backup_id, role_data), "created"

    def _role_create_bucket_state(self, guild: discord.Guild) -> dict[str, Any]:
        """Read-only discord.py 2.3.2 telemetry; never changes rate-limit state."""
        http = getattr(self.bot, "http", None)
        if http is None:
            return {}
        try:
            route = discord.http.Route(
                "POST", "/guilds/{guild_id}/roles", guild_id=int(guild.id)
            )
            hashes = getattr(http, "_bucket_hashes", {})
            buckets = getattr(http, "_buckets", {})
            bucket_hash = hashes.get(route.key)
            key = f"{bucket_hash or route.key}:{route.major_parameters}"
            bucket = buckets.get(key)
            if bucket is None:
                return {"known": False}
            expires = getattr(bucket, "expires", None)
            seconds_until_reset = (
                max(0.0, expires - asyncio.get_running_loop().time())
                if expires is not None else None
            )
            return {
                "known": True,
                "limit": int(bucket.limit),
                "remaining": int(bucket.remaining),
                "outgoing": int(bucket.outgoing),
                "pending": len(bucket._pending_requests),
                "resetAfterSeconds": round(float(bucket.reset_after), 2),
                "secondsUntilReset": (
                    round(seconds_until_reset, 2)
                    if seconds_until_reset is not None else None
                ),
            }
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
            return {"known": False}

    @staticmethod
    async def _find_possible_created_role(
        guild: discord.Guild,
        role_data: dict,
        previous_role_ids: set[int],
    ) -> dict[str, Any]:
        """Read-only reconciliation: never claim that an ambiguous role was created."""
        try:
            # Longer than the client's 120s rate-limit bound: do not cancel its bucket refresh.
            roles = await asyncio.wait_for(guild.fetch_roles(), timeout=150.0)
        except (asyncio.TimeoutError, discord.DiscordException, AttributeError):
            return {"state": "unavailable"}
        matches = [
            role for role in roles
            if int(role.id) not in previous_role_ids
            and role.name == role_data["name"]
            and not role.managed
            and int(role.permissions.value) == int(role_data["permissions"] or 0)
            and int(role.color.value) == int(role_data["color"] or 0)
            and bool(role.hoist) == bool(role_data["hoist"])
            and bool(role.mentionable) == bool(role_data["mentionable"])
        ]
        if len(matches) == 1:
            return {"state": "possible", "roleId": str(matches[0].id)}
        return {"state": "ambiguous" if len(matches) > 1 else "not_found"}

    async def _create_role(self, guild, backup_id: int, role_data: dict) -> discord.Role:
        logger.info(
            "Backup role creation beginning: guild=%s backup=%s role=%s bucket=%s",
            guild.id, backup_id, role_data["name"],
            self._role_create_bucket_state(guild),
        )
        try:
            role = await guild.create_role(
            name=role_data["name"],
            colour=discord.Colour(int(role_data["color"] or 0)),
            permissions=discord.Permissions(int(role_data["permissions"] or 0)),
            hoist=bool(role_data["hoist"]),
            mentionable=bool(role_data["mentionable"]),
            reason=f"Restauração do backup {backup_id}",
            )
        except discord.RateLimited as exc:
            logger.warning(
                "Backup role creation blocked by Discord cooldown: "
                "guild=%s backup=%s role=%s retry_after=%ss bucket=%s",
                guild.id, backup_id, role_data["name"],
                round(float(exc.retry_after), 2),
                self._role_create_bucket_state(guild),
            )
            raise
        logger.info(
            "Backup role creation returned: guild=%s backup=%s role=%s role_id=%s bucket=%s",
            guild.id, backup_id, role_data["name"], role.id,
            self._role_create_bucket_state(guild),
        )
        return role

    async def _merge_duplicate_roles(
        self,
        guild: discord.Guild,
        groups: dict[str, list[discord.Role]],
        backup_id: int,
    ) -> list[str]:
        issues: list[str] = []
        bot_member = guild.me or guild.get_member(self.bot.user.id if self.bot.user else 0)
        for role_name, raw_roles in groups.items():
            roles = [role for role in raw_roles if role is not None]
            if len(roles) < 2:
                continue
            ordered = sorted(roles, key=lambda role: role.position, reverse=True)
            highest = ordered[0]
            bot_role = next(
                (role for role in ordered if bot_member is not None and role in bot_member.roles),
                None,
            )
            primary = bot_role or highest
            if primary.position != highest.position:
                try:
                    await guild.edit_role_positions({primary: highest.position})
                except discord.Forbidden:
                    issues.append(f"merge_position_failed:{role_name}")

            for duplicate in ordered:
                if duplicate.id == primary.id:
                    continue
                for member in list(duplicate.members):
                    if primary in member.roles:
                        continue
                    try:
                        await member.add_roles(
                            primary,
                            reason=f"Unificação de cargo duplicado durante backup {backup_id}",
                        )
                    except discord.Forbidden:
                        issues.append(f"merge_member_failed:{role_name}:{member.id}")
                if bot_member is not None and duplicate in bot_member.roles:
                    continue
                try:
                    await duplicate.delete(
                        reason=f"Unificação de cargos duplicados durante backup {backup_id}",
                    )
                except discord.Forbidden:
                    issues.append(f"merge_delete_failed:{role_name}:{duplicate.id}")
        return issues

    def _find_channel_by_fallback(
        self,
        guild: discord.Guild,
        name: str,
        channel_type: int,
        parent: Optional[discord.CategoryChannel],
    ):
        parent_id = int(parent.id) if parent else None
        matches = []
        for candidate in guild.channels:
            if candidate.name != name or not self._matches_channel_type(candidate, channel_type):
                continue
            candidate_parent_id = (
                int(candidate.category_id)
                if getattr(candidate, "category_id", None) else None
            )
            if candidate_parent_id == parent_id:
                matches.append(candidate)
        # Never choose an arbitrary channel when fallback identity is ambiguous.
        return matches[0] if len(matches) == 1 else None

    def _matches_channel_type(self, channel, channel_type: int) -> bool:
        if channel_type == 0:
            return isinstance(channel, discord.TextChannel) and not channel.is_news()
        if channel_type == 5:
            return isinstance(channel, discord.TextChannel) and channel.is_news()
        if channel_type == 2:
            return isinstance(channel, discord.VoiceChannel)
        if channel_type == 13:
            return isinstance(channel, discord.StageChannel)
        if channel_type == 15:
            return isinstance(channel, discord.ForumChannel)
        if channel_type == 4:
            return isinstance(channel, discord.CategoryChannel)
        return int(channel.type.value) == int(channel_type)

    async def _create_channel_by_type(self, guild, data, parent, backup_id: int):
        channel_type = int(data["type"])
        kwargs = {
            "name": data["name"],
            "category": parent,
            "position": int(data["position"] or 0),
            "reason": f"Restauração do backup {backup_id}",
        }
        if channel_type == 0:
            return await guild.create_text_channel(
                **kwargs,
                topic=self._sanitize_channel_topic(data.get("topic")),
                nsfw=bool(data.get("nsfw")),
            )
        if channel_type == 5:
            if not self._supports_announcement_channels(guild):
                return await guild.create_text_channel(
                    **kwargs,
                    topic=self._sanitize_channel_topic(data.get("topic")),
                    nsfw=bool(data.get("nsfw")),
                )
            try:
                return await guild.create_text_channel(
                    **kwargs,
                    topic=self._sanitize_channel_topic(data.get("topic")),
                    nsfw=bool(data.get("nsfw")),
                    news=True,
                )
            except TypeError:
                return await guild.create_text_channel(
                    **kwargs,
                    topic=self._sanitize_channel_topic(data.get("topic")),
                    nsfw=bool(data.get("nsfw")),
                )
        if channel_type == 2:
            return await guild.create_voice_channel(**kwargs)
        if channel_type == 13:
            return await guild.create_stage_channel(**kwargs)
        if channel_type == 15 and hasattr(guild, "create_forum"):
            return await guild.create_forum(**kwargs)
        return None

    async def _edit_channel_by_type(self, channel, data, parent, backup_id: int):
        kwargs = {
            "name": data["name"],
            "position": int(data["position"] or 0),
            "category": parent,
            "reason": f"Sincronização do backup {backup_id}",
        }
        if isinstance(channel, discord.TextChannel):
            kwargs["topic"] = self._sanitize_channel_topic(data.get("topic"))
            kwargs["nsfw"] = bool(data.get("nsfw"))
        await channel.edit(**kwargs)

    def _sanitize_channel_topic(self, topic: Optional[str]) -> Optional[str]:
        if topic is None:
            return None
        return self.CUSTOM_EMOJI_PATTERN.sub(r":\1:", str(topic))

    @staticmethod
    def _supports_announcement_channels(guild: discord.Guild) -> bool:
        features = getattr(guild, "features", None) or []
        return "COMMUNITY" in {
            str(feature).strip().upper()
            for feature in features
        }

    @staticmethod
    def _restore_error_detail(error: Exception) -> dict[str, Any]:
        message = str(error).strip()
        if len(message) > 500:
            message = message[:497] + "..."
        detail: dict[str, Any] = {
            "exceptionClass": error.__class__.__name__,
            "message": message,
        }
        status = getattr(error, "status", None)
        if status is not None:
            detail["status"] = int(status)
        code = getattr(error, "code", None)
        if code is not None:
            detail["discordCode"] = int(code)
        return detail

    @staticmethod
    def _is_missing_permissions_error(error: Exception) -> bool:
        return isinstance(error, discord.HTTPException) and int(
            getattr(error, "code", 0)
        ) == 50013

    @staticmethod
    def _bucket() -> dict[str, int]:
        return {"created": 0, "updated": 0, "reused": 0, "skipped": 0, "failed": 0}

    @staticmethod
    def _json_time(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat()
        return str(value)


def periodic_idempotency_key(guild_id: int, frequency: str, now: Optional[datetime] = None) -> str:
    current = now or datetime.now(timezone.utc)
    normalized = str(frequency or "weekly").lower()
    if normalized == "daily":
        window = current.strftime("%Y-%m-%d")
    elif normalized == "monthly":
        window = current.strftime("%Y-%m")
    else:
        iso_year, iso_week, _ = current.isocalendar()
        window = f"{iso_year}-W{iso_week:02d}"
    return f"periodic:{guild_id}:{normalized}:{window}"
