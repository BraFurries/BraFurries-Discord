from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from typing import Any, Optional

import discord

from core.theme_api import ThemeAssetApiClient, ThemeAssetBytes

logger = logging.getLogger(__name__)


class ThemeRuntimeError(RuntimeError):
    pass


class ThemeBusyError(ThemeRuntimeError):
    pass


class ThemeRuntime:
    """Live Discord executor for temporary Themes.

    This runtime never reads or writes the Backup domain. Theme definitions,
    applications, rollback snapshots, operations and locks live exclusively in
    discord_theme_* tables.
    """

    def __init__(self, bot, *, asset_api: Optional[ThemeAssetApiClient] = None):
        self.bot = bot
        self.asset_api = asset_api or ThemeAssetApiClient()

    def build_preflight(self, guild: discord.Guild, definition: dict) -> dict:
        from core.database import get_theme_vip_custom_role_ids

        parsed = self._validate_definition(guild, definition)
        blockers: list[dict] = []
        warnings: list[dict] = []
        changes: list[dict] = []
        vip_role_ids = get_theme_vip_custom_role_ids(int(guild.id))
        me = guild.me
        permissions = getattr(me, "guild_permissions", None)
        can_manage_guild = bool(
            permissions and (
                getattr(permissions, "administrator", False)
                or getattr(permissions, "manage_guild", False)
            )
        )
        can_manage_channels = bool(
            permissions and (
                getattr(permissions, "administrator", False)
                or getattr(permissions, "manage_channels", False)
            )
        )
        can_manage_roles = bool(
            permissions and (
                getattr(permissions, "administrator", False)
                or getattr(permissions, "manage_roles", False)
            )
        )
        top_role = getattr(me, "top_role", None)

        for asset_name, resource_type in (
            ("icon", "GUILD_ICON"),
            ("banner", "GUILD_BANNER"),
        ):
            asset = parsed[asset_name]
            action = asset["action"]
            if action == "UNCHANGED":
                continue

            current_asset = getattr(guild, asset_name, None)
            changes.append({
                "resourceType": resource_type,
                "resourceId": str(guild.id),
                "beforeValue": "PRESENT" if current_asset is not None else "ABSENT",
                "targetValue": action,
                "action": action,
            })
            if not can_manage_guild:
                blockers.append(self._issue(
                    "BOT_MISSING_MANAGE_GUILD",
                    resource_type,
                    str(guild.id),
                    "O Coddy não possui Manage Server para alterar este asset.",
                ))
            if asset_name == "banner" and "BANNER" not in set(getattr(guild, "features", []) or []):
                blockers.append(self._issue(
                    "BANNER_UNAVAILABLE",
                    resource_type,
                    str(guild.id),
                    "Este servidor não suporta banner no estado atual.",
                ))
            if action == "SET" and (
                not asset.get("assetKey")
                or not asset.get("sha256")
                or not asset.get("contentType")
            ):
                blockers.append(self._issue(
                    "THEME_ASSET_MISSING",
                    resource_type,
                    str(guild.id),
                    "O asset desejado não está persistido na plataforma.",
                ))
            if action == "REMOVE" and current_asset is None:
                warnings.append(self._issue(
                    "ALREADY_ABSENT",
                    resource_type,
                    str(guild.id),
                    "O asset já está ausente; nenhuma alteração será necessária.",
                ))

        seen: set[tuple[str, int]] = set()
        for item in parsed["resources"]:
            resource_type = item["resourceType"]
            resource_id = int(item["resourceId"])
            target_name = item["targetName"]
            identity = (resource_type, resource_id)
            if identity in seen:
                blockers.append(self._issue(
                    "DUPLICATE_RESOURCE",
                    resource_type,
                    str(resource_id),
                    "O mesmo recurso aparece mais de uma vez no Theme.",
                ))
                continue
            seen.add(identity)

            if resource_type in {"CHANNEL", "CATEGORY"}:
                resource = guild.get_channel(resource_id)
                if resource is None:
                    blockers.append(self._issue(
                        "RESOURCE_MISSING",
                        resource_type,
                        str(resource_id),
                        "O canal/categoria não existe mais.",
                    ))
                    continue
                is_category = isinstance(resource, discord.CategoryChannel)
                if (resource_type == "CATEGORY") != is_category:
                    blockers.append(self._issue(
                        "RESOURCE_TYPE_MISMATCH",
                        resource_type,
                        str(resource_id),
                        "O recurso existe, mas o tipo atual não corresponde ao Theme.",
                    ))
                    continue
                changes.append({
                    "resourceType": resource_type,
                    "resourceId": str(resource_id),
                    "beforeValue": resource.name,
                    "targetValue": target_name,
                    "action": "SET_NAME",
                })
                if not can_manage_channels:
                    blockers.append(self._issue(
                        "BOT_MISSING_MANAGE_CHANNELS",
                        resource_type,
                        str(resource_id),
                        "O Coddy não possui Manage Channels.",
                    ))
                if resource.name == target_name:
                    warnings.append(self._issue(
                        "NO_CHANGE",
                        resource_type,
                        str(resource_id),
                        "O recurso já possui o nome desejado.",
                    ))
                continue

            role = guild.get_role(resource_id)
            if role is None:
                blockers.append(self._issue(
                    "RESOURCE_MISSING",
                    "ROLE",
                    str(resource_id),
                    "O cargo não existe mais.",
                ))
                continue
            changes.append({
                "resourceType": "ROLE",
                "resourceId": str(resource_id),
                "beforeValue": role.name,
                "targetValue": target_name,
                "action": "SET_NAME",
            })
            if role.is_default():
                blockers.append(self._issue(
                    "EVERYONE_ROLE",
                    "ROLE",
                    str(resource_id),
                    "@everyone não pode ser usado em Theme.",
                ))
            elif int(role.id) in vip_role_ids:
                blockers.append(self._issue(
                    "CODDY_VIP_CUSTOM_ROLE",
                    "ROLE",
                    str(resource_id),
                    "Gerenciado pelo Coddy — não disponível para temas.",
                ))
            elif bool(getattr(role, "managed", False)):
                blockers.append(self._issue(
                    "MANAGED_ROLE",
                    "ROLE",
                    str(resource_id),
                    "Cargo gerenciado por integração/bot não é editável.",
                ))
            elif not can_manage_roles:
                blockers.append(self._issue(
                    "BOT_MISSING_MANAGE_ROLES",
                    "ROLE",
                    str(resource_id),
                    "O Coddy não possui Manage Roles.",
                ))
            elif top_role is None or int(role.position) >= int(top_role.position):
                blockers.append(self._issue(
                    "BOT_ROLE_HIERARCHY",
                    "ROLE",
                    str(resource_id),
                    "O cargo está no mesmo nível ou acima do Coddy.",
                ))
            if role.name == target_name:
                warnings.append(self._issue(
                    "NO_CHANGE",
                    "ROLE",
                    str(resource_id),
                    "O cargo já possui o nome desejado.",
                ))

        return {
            "guildId": str(guild.id),
            "themeId": int(parsed["themeId"]),
            "changes": changes,
            "warnings": warnings,
            "blockers": blockers,
            "ready": not blockers,
        }

    async def run_apply_operation(
        self,
        guild: discord.Guild,
        *,
        application_id: int,
        operation_id: int,
    ) -> None:
        from core.database import (
            acquire_theme_guild_lock,
            complete_theme_operation,
            create_theme_application_snapshot,
            get_theme_application,
            get_theme_operation,
            initialize_discord_theme_tables,
            mark_theme_operation_running,
            record_theme_operation_step,
            refresh_theme_guild_lock,
            release_theme_guild_lock,
            update_theme_snapshot_apply_status,
        )

        initialize_discord_theme_tables()
        operation = get_theme_operation(operation_id, int(guild.id))
        application = get_theme_application(application_id, int(guild.id))
        if (
            operation is None
            or application is None
            or int(operation["application_id"]) != int(application_id)
            or str(operation["operation_type"]).upper() != "APPLY"
        ):
            raise LookupError("theme_apply_operation_not_found")

        if not mark_theme_operation_running(operation_id, int(guild.id)):
            return

        lock_token = f"theme-apply:{operation_id}:{uuid.uuid4()}"
        if not acquire_theme_guild_lock(
            int(guild.id),
            lock_token,
            "APPLY",
            operation_id=operation_id,
            application_id=application_id,
            ttl_seconds=7200,
        ):
            complete_theme_operation(
                operation_id,
                int(guild.id),
                status="FAILED",
                application_status="FAILED",
                result={"applied": 0, "failed": 0, "blockers": ["theme_operation_busy"]},
                error_code="theme_operation_busy",
                release_active=True,
            )
            raise ThemeBusyError("theme_operation_busy")

        heartbeat: Optional[asyncio.Task] = None
        lock_lost = asyncio.Event()
        effects_applied = 0
        try:
            heartbeat = asyncio.create_task(
                self._heartbeat(
                    int(guild.id),
                    lock_token,
                    refresh_theme_guild_lock,
                    lock_lost,
                )
            )
            definition = self._validate_definition(
                guild,
                json.loads(application["frozen_definition_json"]),
            )
            preflight = self.build_preflight(guild, definition)
            total = max(3, 3 + len(preflight["changes"]))
            progress = 0

            def step(
                code: str,
                status: str,
                message: str,
                *,
                detail: Optional[dict] = None,
            ) -> None:
                nonlocal progress
                progress += 1
                record_theme_operation_step(
                    int(guild.id),
                    operation_id,
                    code,
                    status,
                    message,
                    min(progress, total),
                    total,
                    detail=detail,
                )

            step(
                "PREFLIGHT",
                "SUCCEEDED" if preflight["ready"] else "FAILED",
                "Preflight vivo concluído.",
                detail={
                    "warnings": preflight["warnings"],
                    "blockers": preflight["blockers"],
                },
            )
            if not preflight["ready"]:
                step(
                    "BLOCKED",
                    "FAILED",
                    "Theme bloqueado pelo estado atual da guild.",
                )
                complete_theme_operation(
                    operation_id,
                    int(guild.id),
                    status="FAILED",
                    application_status="FAILED",
                    result={
                        "applied": 0,
                        "failed": 0,
                        "preflight": preflight,
                    },
                    error_code="theme_preflight_blocked",
                    release_active=True,
                )
                return

            plans = await self._capture_apply_plans(
                guild,
                application_id,
                definition,
            )
            if not plans:
                step(
                    "NO_EFFECTIVE_CHANGES",
                    "FAILED",
                    "Nenhuma alteração efetiva precisa ser aplicada no estado vivo atual.",
                )
                complete_theme_operation(
                    operation_id,
                    int(guild.id),
                    status="FAILED",
                    application_status="FAILED",
                    result={"applied": 0, "skipped": 0, "failed": 0},
                    error_code="theme_no_effective_changes",
                    release_active=True,
                )
                return

            snapshots: list[dict] = []
            for plan in plans:
                snapshot = create_theme_application_snapshot(
                    int(guild.id),
                    application_id,
                    **plan["snapshot"],
                )
                self._validate_persisted_snapshot(snapshot, plan["snapshot"])
                plan["snapshot_id"] = int(snapshot["id"])
                snapshots.append(snapshot)

            step(
                "ROLLBACK_PERSISTED",
                "SUCCEEDED",
                "Rollback snapshot persistido antes de qualquer alteração.",
                detail={"snapshotCount": len(plans)},
            )

            result = {
                "applied": 0,
                "skipped": 0,
                "failed": 0,
                "changes": [],
            }
            for plan in plans:
                try:
                    self._assert_lock_owned(
                        int(guild.id),
                        lock_token,
                        refresh_theme_guild_lock,
                        lock_lost,
                    )
                    outcome = await self._apply_plan(
                        guild,
                        application_id,
                        plan,
                    )
                    if outcome == "APPLIED":
                        effects_applied += 1
                        result["applied"] += 1
                        step(
                            f"APPLY_{plan['snapshot']['resource_type']}",
                            "SUCCEEDED",
                            self._apply_message(plan),
                            detail=self._plan_identity(plan),
                        )
                    else:
                        result["skipped"] += 1
                        step(
                            f"APPLY_{plan['snapshot']['resource_type']}",
                            "SKIPPED",
                            "Alteração já estava no estado desejado.",
                            detail=self._plan_identity(plan),
                        )
                    update_theme_snapshot_apply_status(
                        int(guild.id),
                        application_id,
                        int(plan["snapshot_id"]),
                        "APPLIED" if outcome == "APPLIED" else "SKIPPED",
                    )
                    result["changes"].append({
                        **self._plan_identity(plan),
                        "status": outcome,
                    })
                except Exception as error:
                    result["failed"] += 1
                    code = self._error_code(error, "theme_apply_failed")
                    update_theme_snapshot_apply_status(
                        int(guild.id),
                        application_id,
                        int(plan["snapshot_id"]),
                        "FAILED",
                        error_code=code,
                    )
                    step(
                        f"APPLY_{plan['snapshot']['resource_type']}",
                        "FAILED",
                        "Falha ao aplicar uma alteração do Theme.",
                        detail={**self._plan_identity(plan), "error": code},
                    )
                    result["changes"].append({
                        **self._plan_identity(plan),
                        "status": "FAILED",
                        "error": code,
                    })

            if result["failed"] == 0:
                terminal = "SUCCEEDED"
                app_status = "ACTIVE"
                error_code = None
                release_active = False
            elif effects_applied > 0:
                terminal = "PARTIAL"
                app_status = "APPLY_PARTIAL"
                error_code = "theme_apply_partial"
                release_active = False
            else:
                terminal = "FAILED"
                app_status = "FAILED"
                error_code = "theme_apply_failed"
                release_active = True

            step(
                "COMPLETED",
                "SUCCEEDED" if terminal == "SUCCEEDED" else "FAILED",
                (
                    "Theme aplicado com sucesso."
                    if terminal == "SUCCEEDED"
                    else "Aplicação do Theme terminou com ocorrências."
                ),
                detail={"status": terminal},
            )
            complete_theme_operation(
                operation_id,
                int(guild.id),
                status=terminal,
                application_status=app_status,
                result=result,
                error_code=error_code,
                release_active=release_active,
            )
        except asyncio.CancelledError:
            logger.warning(
                "Theme apply interrupted; leaving operation RUNNING for reconciliation: guild=%s application=%s operation=%s",
                guild.id,
                application_id,
                operation_id,
            )
            raise
        except Exception as error:
            logger.exception(
                "Theme apply failed: guild=%s application=%s operation=%s",
                guild.id,
                application_id,
                operation_id,
            )
            try:
                complete_theme_operation(
                    operation_id,
                    int(guild.id),
                    status="PARTIAL" if effects_applied else "FAILED",
                    application_status="APPLY_PARTIAL" if effects_applied else "FAILED",
                    result={
                        "applied": effects_applied,
                        "failed": 1,
                        "error": self._error_code(error, "theme_apply_unexpected"),
                    },
                    error_code="theme_apply_unexpected",
                    release_active=not bool(effects_applied),
                )
            except Exception:
                logger.exception("Could not finalize failed Theme apply operation")
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            release_theme_guild_lock(int(guild.id), lock_token)

    async def run_restore_operation(
        self,
        guild: discord.Guild,
        *,
        application_id: int,
        operation_id: int,
    ) -> None:
        from core.database import (
            acquire_theme_guild_lock,
            complete_theme_operation,
            get_theme_application,
            get_theme_operation,
            initialize_discord_theme_tables,
            list_theme_application_snapshots,
            mark_theme_operation_running,
            record_theme_operation_step,
            refresh_theme_guild_lock,
            release_theme_guild_lock,
            update_theme_snapshot_apply_status,
            update_theme_snapshot_restore_status,
        )

        initialize_discord_theme_tables()
        operation = get_theme_operation(operation_id, int(guild.id))
        application = get_theme_application(application_id, int(guild.id))
        if (
            operation is None
            or application is None
            or int(operation["application_id"]) != int(application_id)
            or str(operation["operation_type"]).upper() != "RESTORE"
        ):
            raise LookupError("theme_restore_operation_not_found")
        if not mark_theme_operation_running(operation_id, int(guild.id)):
            return

        lock_token = f"theme-restore:{operation_id}:{uuid.uuid4()}"
        if not acquire_theme_guild_lock(
            int(guild.id),
            lock_token,
            "RESTORE",
            operation_id=operation_id,
            application_id=application_id,
            ttl_seconds=7200,
        ):
            complete_theme_operation(
                operation_id,
                int(guild.id),
                status="FAILED",
                application_status=application["status"]
                if application["status"] in {"ACTIVE", "APPLY_PARTIAL", "RESTORE_PARTIAL"}
                else "RESTORE_PARTIAL",
                result={"restored": 0, "failed": 0, "error": "theme_operation_busy"},
                error_code="theme_operation_busy",
                release_active=False,
            )
            raise ThemeBusyError("theme_operation_busy")

        heartbeat: Optional[asyncio.Task] = None
        lock_lost = asyncio.Event()
        try:
            heartbeat = asyncio.create_task(
                self._heartbeat(
                    int(guild.id),
                    lock_token,
                    refresh_theme_guild_lock,
                    lock_lost,
                )
            )
            force = bool(operation.get("force_restore"))
            all_snapshots = list_theme_application_snapshots(
                int(guild.id),
                application_id,
            )
            for snapshot in all_snapshots:
                if str(snapshot.get("apply_status")) != "PENDING":
                    continue
                live_state = await self._snapshot_live_state(guild, snapshot)
                if live_state == "APPLIED":
                    update_theme_snapshot_apply_status(
                        int(guild.id),
                        application_id,
                        int(snapshot["id"]),
                        "APPLIED",
                    )
                    snapshot["apply_status"] = "APPLIED"
                elif live_state == "BEFORE":
                    update_theme_snapshot_apply_status(
                        int(guild.id),
                        application_id,
                        int(snapshot["id"]),
                        "SKIPPED",
                    )
                    snapshot["apply_status"] = "SKIPPED"
            snapshots = [
                row
                for row in all_snapshots
                if str(row.get("apply_status")) == "APPLIED"
                and str(row.get("restore_status") or "") != "RESTORED"
            ]
            total = max(2, 2 + len(snapshots))
            progress = 0

            def step(
                code: str,
                status: str,
                message: str,
                *,
                detail: Optional[dict] = None,
            ) -> None:
                nonlocal progress
                progress += 1
                record_theme_operation_step(
                    int(guild.id),
                    operation_id,
                    code,
                    status,
                    message,
                    min(progress, total),
                    total,
                    detail=detail,
                )

            step(
                "RESTORE_PREFLIGHT",
                "SUCCEEDED",
                "Rollback snapshot carregado e estado vivo será comparado.",
                detail={"force": force, "snapshotCount": len(snapshots)},
            )

            result = {
                "restored": 0,
                "skipped": 0,
                "drifted": 0,
                "missing": 0,
                "failed": 0,
                "changes": [],
            }
            for snapshot in snapshots:
                try:
                    self._assert_lock_owned(
                        int(guild.id),
                        lock_token,
                        refresh_theme_guild_lock,
                        lock_lost,
                    )
                    outcome = await self._restore_snapshot(
                        guild,
                        application_id,
                        snapshot,
                        force=force,
                    )
                except Exception as error:
                    outcome = "FAILED"
                    code = self._error_code(error, "theme_restore_failed")
                    update_theme_snapshot_restore_status(
                        int(guild.id),
                        application_id,
                        int(snapshot["id"]),
                        "FAILED",
                        error_code=code,
                    )

                result_key = {
                    "RESTORED": "restored",
                    "SKIPPED": "skipped",
                    "DRIFTED": "drifted",
                    "MISSING": "missing",
                    "FAILED": "failed",
                }[outcome]
                result[result_key] += 1
                step(
                    f"RESTORE_{str(snapshot['resource_type']).upper()}",
                    outcome if outcome in {"DRIFTED", "MISSING", "FAILED"} else (
                        "SKIPPED" if outcome == "SKIPPED" else "SUCCEEDED"
                    ),
                    self._restore_message(outcome),
                    detail={
                        "resourceType": str(snapshot["resource_type"]),
                        "resourceId": str(snapshot["resource_discord_id"]),
                        "outcome": outcome,
                    },
                )
                result["changes"].append({
                    "resourceType": str(snapshot["resource_type"]),
                    "resourceId": str(snapshot["resource_discord_id"]),
                    "status": outcome,
                })

            unresolved = (
                result["drifted"]
                + result["missing"]
                + result["failed"]
            )
            if unresolved == 0:
                terminal = "SUCCEEDED"
                app_status = "RESTORED"
                error_code = None
                release_active = True
            else:
                terminal = "PARTIAL"
                app_status = "RESTORE_PARTIAL"
                error_code = "theme_restore_partial"
                release_active = False

            step(
                "COMPLETED",
                "SUCCEEDED" if terminal == "SUCCEEDED" else "FAILED",
                (
                    "Estado anterior restaurado."
                    if terminal == "SUCCEEDED"
                    else "Restore concluído com conflitos ou recursos ausentes."
                ),
                detail={"status": terminal, **result},
            )
            complete_theme_operation(
                operation_id,
                int(guild.id),
                status=terminal,
                application_status=app_status,
                result=result,
                error_code=error_code,
                release_active=release_active,
            )
        except asyncio.CancelledError:
            logger.warning(
                "Theme restore interrupted; leaving operation RUNNING for reconciliation: guild=%s application=%s operation=%s",
                guild.id,
                application_id,
                operation_id,
            )
            raise
        except Exception as error:
            logger.exception(
                "Theme restore failed: guild=%s application=%s operation=%s",
                guild.id,
                application_id,
                operation_id,
            )
            try:
                complete_theme_operation(
                    operation_id,
                    int(guild.id),
                    status="PARTIAL",
                    application_status="RESTORE_PARTIAL",
                    result={"error": self._error_code(error, "theme_restore_unexpected")},
                    error_code="theme_restore_unexpected",
                    release_active=False,
                )
            except Exception:
                logger.exception("Could not finalize unexpected Theme restore failure")
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            release_theme_guild_lock(int(guild.id), lock_token)

    async def recover_stale_operations(self) -> int:
        from core.database import (
            acquire_theme_guild_lock,
            complete_theme_operation,
            initialize_discord_theme_tables,
            list_recoverable_theme_operations,
            list_theme_application_snapshots,
            record_theme_operation_step,
            release_theme_guild_lock,
            update_theme_snapshot_apply_status,
            update_theme_snapshot_restore_status,
        )

        try:
            initialize_discord_theme_tables()
        except Exception:
            logger.exception("Theme recovery skipped because schema validation failed")
            return 0

        recovered = 0
        for operation in list_recoverable_theme_operations():
            guild = self.bot.get_guild(int(operation["guild_id"]))
            if guild is None:
                continue
            operation_id = int(operation["id"])
            application_id = int(operation["application_id"])
            operation_type = str(operation["operation_type"]).upper()

            if str(operation.get("status") or "").upper() == "PENDING":
                try:
                    if operation_type == "APPLY":
                        await self.run_apply_operation(
                            guild,
                            application_id=application_id,
                            operation_id=operation_id,
                        )
                    else:
                        await self.run_restore_operation(
                            guild,
                            application_id=application_id,
                            operation_id=operation_id,
                        )
                    recovered += 1
                except Exception:
                    logger.exception(
                        "Could not resume pending Theme operation: guild=%s operation=%s",
                        guild.id,
                        operation_id,
                    )
                continue

            lock_token = f"theme-recovery:{operation_id}:{uuid.uuid4()}"
            if not acquire_theme_guild_lock(
                int(guild.id),
                lock_token,
                operation_type,
                operation_id=operation_id,
                application_id=application_id,
                ttl_seconds=900,
            ):
                continue
            try:
                snapshots = list_theme_application_snapshots(
                    int(guild.id),
                    application_id,
                )
                if operation_type == "APPLY":
                    status, app_status, release, result = await self._reconcile_apply(
                        guild,
                        application_id,
                        snapshots,
                        update_theme_snapshot_apply_status,
                    )
                else:
                    status, app_status, release, result = await self._reconcile_restore(
                        guild,
                        application_id,
                        snapshots,
                        update_theme_snapshot_restore_status,
                    )
                record_theme_operation_step(
                    int(guild.id),
                    operation_id,
                    "RECOVERED",
                    "SUCCEEDED" if status == "SUCCEEDED" else "FAILED",
                    "Operação Theme reconciliada após restart/stale lock.",
                    1,
                    1,
                    detail=result,
                )
                complete_theme_operation(
                    operation_id,
                    int(guild.id),
                    status=status,
                    application_status=app_status,
                    result=result,
                    error_code=None if status == "SUCCEEDED" else "theme_recovered_partial",
                    release_active=release,
                )
                recovered += 1
            except Exception:
                logger.exception(
                    "Could not recover Theme operation: guild=%s operation=%s",
                    guild.id,
                    operation_id,
                )
            finally:
                release_theme_guild_lock(int(guild.id), lock_token)
        return recovered

    async def _capture_apply_plans(
        self,
        guild: discord.Guild,
        application_id: int,
        definition: dict,
    ) -> list[dict]:
        plans: list[dict] = []

        for asset_name, resource_type, api_type in (
            ("icon", "GUILD_ICON", "ICON"),
            ("banner", "GUILD_BANNER", "BANNER"),
        ):
            asset_definition = definition[asset_name]
            action = asset_definition["action"]
            if action == "UNCHANGED":
                continue
            current_asset = getattr(guild, asset_name, None)
            before_bytes = await self._read_discord_asset(current_asset)
            before_hash = hashlib.sha256(before_bytes).hexdigest() if before_bytes else None
            before_value = "PRESENT" if before_bytes is not None else "ABSENT"

            desired: Optional[ThemeAssetBytes] = None
            if action == "SET":
                desired = await self.asset_api.fetch_desired(
                    int(guild.id),
                    application_id,
                    api_type,
                )
                expected_hash = str(asset_definition.get("sha256") or "").strip().lower()
                if desired.sha256.lower() != expected_hash:
                    raise ThemeRuntimeError("theme_desired_asset_hash_mismatch")
                if before_hash == desired.sha256:
                    continue
                applied_value = "PRESENT"
            else:
                if before_bytes is None:
                    continue
                applied_value = "ABSENT"

            rollback_metadata = None
            if before_bytes is not None:
                rollback_metadata = await self.asset_api.upload_rollback(
                    int(guild.id),
                    application_id,
                    api_type,
                    before_bytes,
                    self._detect_content_type(before_bytes),
                )

            plans.append({
                "assetType": api_type,
                "desired": desired,
                "snapshot": {
                    "resource_type": resource_type,
                    "resource_discord_id": 0,
                    "before_value": before_value,
                    "applied_value": applied_value,
                    "before_asset_key": (
                        rollback_metadata.get("key")
                        if rollback_metadata else None
                    ),
                    "before_asset_content_type": (
                        rollback_metadata.get("contentType")
                        if rollback_metadata else None
                    ),
                    "before_asset_sha256": (
                        rollback_metadata.get("sha256")
                        if rollback_metadata else None
                    ),
                    "applied_asset_key": asset_definition.get("assetKey"),
                    "applied_asset_content_type": asset_definition.get("contentType"),
                    "applied_asset_sha256": (
                        desired.sha256 if desired else None
                    ),
                },
            })

        for item in definition["resources"]:
            resource_type = item["resourceType"]
            resource_id = int(item["resourceId"])
            target_name = item["targetName"]
            resource = self._get_resource(guild, resource_type, resource_id)
            if resource is None:
                raise ThemeRuntimeError("theme_resource_missing_after_preflight")
            if resource.name == target_name:
                continue
            plans.append({
                "snapshot": {
                    "resource_type": resource_type,
                    "resource_discord_id": resource_id,
                    "before_value": resource.name,
                    "applied_value": target_name,
                },
            })
        return plans

    async def _apply_plan(
        self,
        guild: discord.Guild,
        application_id: int,
        plan: dict,
    ) -> str:
        snapshot = plan["snapshot"]
        resource_type = snapshot["resource_type"]
        before_value = snapshot.get("before_value")
        applied_value = snapshot.get("applied_value")

        if resource_type in {"GUILD_ICON", "GUILD_BANNER"}:
            asset_name = "icon" if resource_type == "GUILD_ICON" else "banner"
            current_bytes = await self._read_discord_asset(getattr(guild, asset_name, None))
            current_hash = hashlib.sha256(current_bytes).hexdigest() if current_bytes else None
            if self._asset_matches(
                current_bytes,
                current_hash,
                applied_value,
                snapshot.get("applied_asset_sha256"),
            ):
                return "SKIPPED"
            if not self._asset_matches(
                current_bytes,
                current_hash,
                before_value,
                snapshot.get("before_asset_sha256"),
            ):
                raise ThemeRuntimeError("theme_apply_drift_before_effect")
            desired = plan.get("desired")
            value = desired.data if desired is not None else None
            if resource_type == "GUILD_ICON":
                await guild.edit(icon=value, reason="Coddy Temporary Theme")
            else:
                await guild.edit(banner=value, reason="Coddy Temporary Theme")
            return "APPLIED"

        resource = self._get_resource(
            guild,
            resource_type,
            int(snapshot["resource_discord_id"]),
        )
        if resource is None:
            raise ThemeRuntimeError("theme_resource_missing_before_effect")
        if resource.name == applied_value:
            return "SKIPPED"
        if resource.name != before_value:
            raise ThemeRuntimeError("theme_apply_drift_before_effect")
        await resource.edit(name=applied_value, reason="Coddy Temporary Theme")
        return "APPLIED"

    async def _restore_snapshot(
        self,
        guild: discord.Guild,
        application_id: int,
        snapshot: dict,
        *,
        force: bool,
    ) -> str:
        from core.database import update_theme_snapshot_restore_status

        resource_type = str(snapshot["resource_type"]).upper()
        snapshot_id = int(snapshot["id"])

        if resource_type in {"GUILD_ICON", "GUILD_BANNER"}:
            asset_name = "icon" if resource_type == "GUILD_ICON" else "banner"
            api_type = "ICON" if resource_type == "GUILD_ICON" else "BANNER"
            current_bytes = await self._read_discord_asset(getattr(guild, asset_name, None))
            current_hash = hashlib.sha256(current_bytes).hexdigest() if current_bytes else None

            if self._asset_matches(
                current_bytes,
                current_hash,
                snapshot.get("before_value"),
                snapshot.get("before_asset_sha256"),
            ):
                update_theme_snapshot_restore_status(
                    int(guild.id),
                    application_id,
                    snapshot_id,
                    "RESTORED",
                )
                return "RESTORED"

            if not force and not self._asset_matches(
                current_bytes,
                current_hash,
                snapshot.get("applied_value"),
                snapshot.get("applied_asset_sha256"),
            ):
                update_theme_snapshot_restore_status(
                    int(guild.id),
                    application_id,
                    snapshot_id,
                    "DRIFTED",
                    error_code="theme_restore_drift",
                )
                return "DRIFTED"

            if snapshot.get("before_value") == "PRESENT":
                previous = await self.asset_api.fetch_rollback(
                    int(guild.id),
                    application_id,
                    api_type,
                )
                expected_hash = str(snapshot.get("before_asset_sha256") or "").strip().lower()
                if not expected_hash or previous.sha256.lower() != expected_hash:
                    raise ThemeRuntimeError("theme_rollback_asset_hash_mismatch")
                value = previous.data
            else:
                value = None
            if resource_type == "GUILD_ICON":
                await guild.edit(icon=value, reason="Coddy Temporary Theme restore")
            else:
                await guild.edit(banner=value, reason="Coddy Temporary Theme restore")
            update_theme_snapshot_restore_status(
                int(guild.id),
                application_id,
                snapshot_id,
                "RESTORED",
            )
            return "RESTORED"

        resource = self._get_resource(
            guild,
            resource_type,
            int(snapshot["resource_discord_id"]),
        )
        if resource is None:
            update_theme_snapshot_restore_status(
                int(guild.id),
                application_id,
                snapshot_id,
                "MISSING",
                error_code="theme_resource_missing",
            )
            return "MISSING"

        if resource.name == snapshot.get("before_value"):
            update_theme_snapshot_restore_status(
                int(guild.id),
                application_id,
                snapshot_id,
                "RESTORED",
            )
            return "RESTORED"

        if not force and resource.name != snapshot.get("applied_value"):
            update_theme_snapshot_restore_status(
                int(guild.id),
                application_id,
                snapshot_id,
                "DRIFTED",
                error_code="theme_restore_drift",
            )
            return "DRIFTED"

        await resource.edit(
            name=snapshot.get("before_value"),
            reason="Coddy Temporary Theme restore",
        )
        update_theme_snapshot_restore_status(
            int(guild.id),
            application_id,
            snapshot_id,
            "RESTORED",
        )
        return "RESTORED"

    async def _reconcile_apply(
        self,
        guild: discord.Guild,
        application_id: int,
        snapshots: list[dict],
        update_status,
    ) -> tuple[str, str, bool, dict]:
        applied = 0
        unresolved = 0
        failed = 0
        for snapshot in snapshots:
            current = await self._snapshot_live_state(guild, snapshot)
            if current == "APPLIED":
                applied += 1
                if str(snapshot.get("apply_status")) != "APPLIED":
                    update_status(
                        int(guild.id),
                        application_id,
                        int(snapshot["id"]),
                        "APPLIED",
                    )
            elif current == "BEFORE":
                unresolved += 1
            else:
                failed += 1

        if snapshots and applied == len(snapshots):
            return (
                "SUCCEEDED",
                "ACTIVE",
                False,
                {"recovered": True, "applied": applied, "unresolved": 0, "drifted": 0},
            )
        if applied > 0 or failed > 0:
            return (
                "PARTIAL",
                "APPLY_PARTIAL",
                False,
                {
                    "recovered": True,
                    "applied": applied,
                    "unresolved": unresolved,
                    "drifted": failed,
                },
            )
        return (
            "FAILED",
            "FAILED",
            True,
            {"recovered": True, "applied": 0, "unresolved": unresolved, "drifted": failed},
        )

    async def _reconcile_restore(
        self,
        guild: discord.Guild,
        application_id: int,
        snapshots: list[dict],
        update_status,
    ) -> tuple[str, str, bool, dict]:
        applied_snapshots = [
            row for row in snapshots if str(row.get("apply_status")) == "APPLIED"
        ]
        restored = 0
        unresolved = 0
        drifted = 0
        for snapshot in applied_snapshots:
            current = await self._snapshot_live_state(guild, snapshot)
            if current == "BEFORE":
                restored += 1
                update_status(
                    int(guild.id),
                    application_id,
                    int(snapshot["id"]),
                    "RESTORED",
                )
            elif current == "APPLIED":
                unresolved += 1
            else:
                drifted += 1
                update_status(
                    int(guild.id),
                    application_id,
                    int(snapshot["id"]),
                    "DRIFTED",
                    error_code="theme_restore_drift",
                )

        if restored == len(applied_snapshots):
            return (
                "SUCCEEDED",
                "RESTORED",
                True,
                {"recovered": True, "restored": restored, "unresolved": 0, "drifted": 0},
            )
        return (
            "PARTIAL",
            "RESTORE_PARTIAL",
            False,
            {
                "recovered": True,
                "restored": restored,
                "unresolved": unresolved,
                "drifted": drifted,
            },
        )

    async def _snapshot_live_state(self, guild: discord.Guild, snapshot: dict) -> str:
        resource_type = str(snapshot["resource_type"]).upper()
        if resource_type in {"GUILD_ICON", "GUILD_BANNER"}:
            asset_name = "icon" if resource_type == "GUILD_ICON" else "banner"
            current_bytes = await self._read_discord_asset(getattr(guild, asset_name, None))
            current_hash = hashlib.sha256(current_bytes).hexdigest() if current_bytes else None
            if self._asset_matches(
                current_bytes,
                current_hash,
                snapshot.get("before_value"),
                snapshot.get("before_asset_sha256"),
            ):
                return "BEFORE"
            if self._asset_matches(
                current_bytes,
                current_hash,
                snapshot.get("applied_value"),
                snapshot.get("applied_asset_sha256"),
            ):
                return "APPLIED"
            return "DRIFT"

        resource = self._get_resource(
            guild,
            resource_type,
            int(snapshot["resource_discord_id"]),
        )
        if resource is None:
            return "MISSING"
        if resource.name == snapshot.get("before_value"):
            return "BEFORE"
        if resource.name == snapshot.get("applied_value"):
            return "APPLIED"
        return "DRIFT"

    async def _heartbeat(
        self,
        guild_id: int,
        lock_token: str,
        refresh,
        lock_lost: asyncio.Event,
    ) -> None:
        while True:
            await asyncio.sleep(60)
            if not refresh(guild_id, lock_token, ttl_seconds=7200):
                lock_lost.set()
                return

    def _assert_lock_owned(
        self,
        guild_id: int,
        lock_token: str,
        refresh,
        lock_lost: asyncio.Event,
    ) -> None:
        if lock_lost.is_set():
            raise ThemeBusyError("theme_lock_lost")
        if not refresh(guild_id, lock_token, ttl_seconds=7200):
            lock_lost.set()
            raise ThemeBusyError("theme_lock_lost")

    async def _read_discord_asset(self, asset) -> Optional[bytes]:
        if asset is None:
            return None
        return bytes(await asset.read())

    def _get_resource(
        self,
        guild: discord.Guild,
        resource_type: str,
        resource_id: int,
    ):
        if resource_type == "ROLE":
            return guild.get_role(int(resource_id))
        resource = guild.get_channel(int(resource_id))
        if resource is None:
            return None
        if resource_type == "CATEGORY":
            return resource if isinstance(resource, discord.CategoryChannel) else None
        if resource_type == "CHANNEL":
            return None if isinstance(resource, discord.CategoryChannel) else resource
        return None

    def _validate_definition(self, guild: discord.Guild, definition: dict) -> dict:
        if not isinstance(definition, dict):
            raise ValueError("invalid_theme_definition")
        if str(definition.get("guildId")) != str(guild.id):
            raise ValueError("theme_guild_mismatch")
        try:
            theme_id = int(definition["themeId"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("invalid_theme_id")

        parsed = {
            "guildId": str(guild.id),
            "themeId": theme_id,
            "name": str(definition.get("name") or "Theme"),
            "icon": self._parse_asset_definition(definition.get("icon")),
            "banner": self._parse_asset_definition(definition.get("banner")),
            "resources": [],
        }
        resources = definition.get("resources") or []
        if not isinstance(resources, list):
            raise ValueError("invalid_theme_resources")
        for item in resources:
            if not isinstance(item, dict):
                raise ValueError("invalid_theme_resource")
            resource_type = str(item.get("resourceType") or "").upper()
            if resource_type not in {"CHANNEL", "CATEGORY", "ROLE"}:
                raise ValueError("invalid_theme_resource_type")
            try:
                resource_id = int(item.get("resourceId"))
            except (TypeError, ValueError):
                raise ValueError("invalid_theme_resource_id")
            target_name = str(item.get("targetName") or "").strip()
            if not target_name or len(target_name) > 100:
                raise ValueError("invalid_theme_target_name")
            parsed["resources"].append({
                "resourceType": resource_type,
                "resourceId": str(resource_id),
                "targetName": target_name,
            })
        return parsed

    def _parse_asset_definition(self, value: Any) -> dict:
        if not isinstance(value, dict):
            value = {}
        action = str(value.get("action") or "UNCHANGED").upper()
        if action not in {"UNCHANGED", "SET", "REMOVE"}:
            raise ValueError("invalid_theme_asset_action")
        return {
            "action": action,
            "assetKey": value.get("assetKey"),
            "contentType": value.get("contentType"),
            "sha256": value.get("sha256"),
            "sizeBytes": value.get("sizeBytes"),
        }

    def _validate_persisted_snapshot(self, row: dict, expected: dict) -> None:
        pairs = {
            "resource_type": expected.get("resource_type"),
            "resource_discord_id": int(expected.get("resource_discord_id") or 0),
            "before_value": expected.get("before_value"),
            "applied_value": expected.get("applied_value"),
            "before_asset_sha256": expected.get("before_asset_sha256"),
            "applied_asset_sha256": expected.get("applied_asset_sha256"),
        }
        for key, expected_value in pairs.items():
            actual = row.get(key)
            if key == "resource_discord_id":
                actual = int(actual or 0)
            if actual != expected_value:
                raise ThemeRuntimeError("theme_snapshot_identity_mismatch")

    def _asset_matches(
        self,
        current_bytes: Optional[bytes],
        current_hash: Optional[str],
        expected_value: Optional[str],
        expected_hash: Optional[str],
    ) -> bool:
        if expected_value == "ABSENT":
            return current_bytes is None
        if expected_value == "PRESENT":
            return (
                current_bytes is not None
                and bool(expected_hash)
                and current_hash == expected_hash
            )
        return False

    def _detect_content_type(self, data: bytes) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data[:6] in {b"GIF87a", b"GIF89a"}:
            return "image/gif"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        raise ThemeRuntimeError("unsupported_discord_asset_format")

    def _issue(
        self,
        code: str,
        resource_type: str,
        resource_id: str,
        message: str,
    ) -> dict:
        return {
            "code": code,
            "resourceType": resource_type,
            "resourceId": resource_id,
            "message": message,
        }

    def _plan_identity(self, plan: dict) -> dict:
        snapshot = plan["snapshot"]
        return {
            "resourceType": snapshot["resource_type"],
            "resourceId": str(snapshot.get("resource_discord_id") or 0),
        }

    def _apply_message(self, plan: dict) -> str:
        resource_type = plan["snapshot"]["resource_type"]
        return {
            "GUILD_ICON": "Ícone do servidor alterado.",
            "GUILD_BANNER": "Banner do servidor alterado.",
            "CHANNEL": "Canal renomeado.",
            "CATEGORY": "Categoria renomeada.",
            "ROLE": "Cargo renomeado.",
        }.get(resource_type, "Alteração aplicada.")

    def _restore_message(self, outcome: str) -> str:
        return {
            "RESTORED": "Campo restaurado ao estado anterior.",
            "SKIPPED": "Campo já estava restaurado.",
            "DRIFTED": "Alteração externa detectada; campo preservado.",
            "MISSING": "Recurso não existe mais; nada foi recriado.",
            "FAILED": "Falha ao restaurar o campo.",
        }.get(outcome, "Restore processado.")

    def _error_code(self, error: Exception, fallback: str) -> str:
        value = str(error).strip()
        if value and len(value) <= 96 and " " not in value:
            return value
        return fallback
