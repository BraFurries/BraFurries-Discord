from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
import time
import logging
from datetime import datetime, timezone
from collections.abc import Callable

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger
import discord

from core.bot_status import BotStatusSnapshot, build_status_payload
from core.backup_runtime import BackupBusyError, BackupRestoreEngine
from core.theme_runtime import ThemeRuntime
from core.guild_management import apply_guild_operation, build_guild_resources, build_structure_preview, can_manage_guild
from core.auto_join_roles import add_auto_join_role, read_auto_join, remove_auto_join_role, set_auto_join_enabled
from core.log_targets import log_target_metadata, selectable_log_targets, validate_log_target
from core.log_types import DEFAULT_LOG_TYPES, LEGACY_CALL_LOG_TYPES, LOG_TYPE_LABELS, effective_call_config
from core.recent_logs import RecentLogBufferHandler
from core.runtime_metrics import RuntimeMetrics, RuntimeMetricsProvider
from core.xp_runtime_control import refresh_xp_runtime, simulate_xp_runtime
from schemas.models.bot import MyBot


PUBLIC_MINIMAL_PATHS = {'/live', '/ready'}
logger = logging.getLogger(__name__)
access_logger = logging.getLogger(f'{__name__}.access')
access_logger.setLevel(logging.DEBUG)
access_logger.propagate = False


class StatusApiAccessLogger(AbstractAccessLogger):
    """Log Status API requests at a level appropriate for their outcome."""

    def log(self, request: web.Request, response: web.StreamResponse, duration: float) -> None:
        status = response.status
        if 200 <= status < 400:
            level = logging.DEBUG
        elif request.path == '/ready' and status == 503:
            level = logging.DEBUG
        elif request.path == '/health' and status == 503:
            level = logging.WARNING
        elif 400 <= status < 500:
            level = logging.WARNING
        else:
            level = logging.ERROR

        self.logger.log(
            level,
            '%s %s %s completed in %.3fs (%s bytes)',
            request.method,
            request.path,
            status,
            duration,
            response.body_length,
        )


def _build_auth_middleware(expected_token: str):
    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        if request.path in PUBLIC_MINIMAL_PATHS:
            return await handler(request)

        authorization = request.headers.get('Authorization', '')
        if authorization != f'Bearer {expected_token}':
            return web.json_response({'error': 'unauthorized'}, status=401)

        return await handler(request)

    return auth_middleware


def build_member_state(member: discord.Member) -> dict:
    """Return the minimal administrative member state without permission bits."""
    roles = sorted(
        (role for role in member.roles if not role.is_default()),
        reverse=True,
    )
    return {
        'guildId': str(member.guild.id),
        'discordUserId': str(member.id),
        'username': member.name,
        'displayName': member.display_name,
        'nickname': member.nick,
        'joinedAt': member.joined_at.isoformat() if member.joined_at else None,
        'roles': [
            {
                'id': str(role.id),
                'name': role.name,
                'color': str(role.color),
                'position': role.position,
                'managed': role.managed,
            }
            for role in roles
        ],
    }


class BotStatusApi:
    def __init__(
        self,
        bot: MyBot,
        host: str,
        port: int,
        token: str | None = None,
        initialized_getter: Callable[[], bool] | None = None,
        metrics_provider: RuntimeMetricsProvider | None = None,
        log_handler: RecentLogBufferHandler | None = None,
    ):
        self.bot = bot
        self.host = host
        self.port = port
        self.token = token.strip() if isinstance(token, str) and token.strip() else None
        self._initialized_getter = initialized_getter or (lambda: True)
        self.started_at = datetime.now(timezone.utc)
        self._started_monotonic = time.monotonic()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._metrics_provider = metrics_provider or RuntimeMetricsProvider()
        self._log_handler = log_handler or RecentLogBufferHandler(capacity=10000)
        self._logging_attached = False
        self._active_admin_streams = 0
        self._admin_stream_stopping = asyncio.Event()
        self._handlers_added_by_api: list[tuple[logging.Logger, logging.Handler]] = []
        self._owned_access_stream_handler: logging.StreamHandler | None = None
        self._backup_runtime = BackupRestoreEngine(bot)
        self._theme_runtime = ThemeRuntime(bot)
        self._background_tasks: set[asyncio.Task] = set()

    def _get_bot_name(self) -> str:
        chatbot = getattr(self.bot, 'chatBot', None)
        if isinstance(chatbot, dict):
            bot_name = chatbot.get('name')
            if isinstance(bot_name, str) and bot_name.strip():
                return bot_name.strip()

        if self.bot.user:
            return self.bot.user.name

        return 'Bot'

    def _build_snapshot(self) -> BotStatusSnapshot:
        try:
            metrics = self._metrics_provider.collect()
        except Exception:
            # Telemetry must never turn a healthy status endpoint into an error.
            metrics = RuntimeMetrics(None, None, None)

        user_count = sum(
            guild.member_count if guild.member_count is not None else len(guild.members)
            for guild in self.bot.guilds
        )

        return BotStatusSnapshot(
            bot_name=self._get_bot_name(),
            ready=self.bot.is_ready(),
            connected=not self.bot.is_closed(),
            guild_count=len(self.bot.guilds),
            user_count=user_count,
            commands_loaded=len(self.bot.tree.get_commands()),
            cogs_loaded=len(self.bot.cogs),
            latency_ms=(self.bot.latency * 1000) if self.bot.latency is not None else None,
            uptime_seconds=time.monotonic() - self._started_monotonic,
            started_at=self.started_at,
            last_ready_at=getattr(self.bot, '_last_ready_at', None),
            python_version=platform.python_version(),
            discord_py_version=discord.__version__,
            process_id=os.getpid(),
            memory_used_mb=metrics.memory_used_mb,
            memory_limit_mb=metrics.memory_limit_mb,
            cpu_usage_percent=metrics.cpu_usage_percent,
        )

    async def _handle_status(self, request: web.Request) -> web.Response:
        payload = build_status_payload(self._build_snapshot())
        return web.json_response(payload)

    async def _handle_health(self, request: web.Request) -> web.Response:
        payload = build_status_payload(self._build_snapshot())
        return web.json_response(payload, status=200 if payload['ok'] else 503)

    async def _handle_live(self, request: web.Request) -> web.Response:
        return web.json_response({'ok': True, 'state': 'live'})

    async def _handle_ready(self, request: web.Request) -> web.Response:
        ready = bool(self._initialized_getter()) and self.bot.is_ready()
        return web.json_response(
            {'ok': ready, 'state': 'ready' if ready else 'not_ready'},
            status=200 if ready else 503,
        )

    async def _handle_managed_guilds(self, request: web.Request) -> web.Response:
        if not bool(self._initialized_getter()) or not self.bot.is_ready():
            return web.json_response({'error': 'bot_not_ready'}, status=503)

        try:
            discord_user_id = int(request.match_info['discord_user_id'])
        except (KeyError, TypeError, ValueError):
            return web.json_response({'error': 'invalid_discord_user_id'}, status=400)

        guilds = []
        for guild in self.bot.guilds:
            member = guild.get_member(discord_user_id)
            if member is None:
                continue

            permissions = member.guild_permissions
            can_manage = (
                guild.owner_id == discord_user_id
                or bool(getattr(permissions, 'administrator', False))
                or bool(getattr(permissions, 'manage_guild', False))
            )
            if not can_manage:
                continue

            guilds.append({
                'guild_id': str(guild.id),
                'name': guild.name,
                'member_count': guild.member_count,
                'icon_url': str(guild.icon.url) if getattr(guild, 'icon', None) else None,
            })

        return web.json_response({'guilds': sorted(guilds, key=lambda guild: int(guild['guild_id']))})

    async def _handle_guild_owner(self, request: web.Request) -> web.Response:
        # Community ownership claim is a security-sensitive internal read.
        # Fail closed when internal authentication is not configured, even in
        # legacy optional-token status mode.
        if not self.token:
            return web.json_response({'error': 'guild_owner_auth_not_configured'}, status=503)
        if not bool(self._initialized_getter()) or not self.bot.is_ready():
            return web.json_response({'error': 'bot_not_ready'}, status=503)

        try:
            guild_id = int(request.match_info['guild_id'])
        except (KeyError, TypeError, ValueError):
            return web.json_response({'error': 'invalid_guild_id'}, status=400)

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return web.json_response({'error': 'guild_not_found'}, status=404)
        if guild.owner_id is None:
            return web.json_response({'error': 'guild_owner_unavailable'}, status=503)

        return web.json_response({
            'guildId': str(guild.id),
            'ownerId': str(guild.owner_id),
        })

    async def _handle_discord_user_lookup(self, request: web.Request) -> web.Response:
        # The API uses this guild-independent lookup to preview/link Discord
        # identities, including accounts outside all guilds managed by Coddy.
        # It must never become available through the legacy public status mode.
        if not self.token:
            return web.json_response({'error': 'discord_user_auth_not_configured'}, status=503)
        if not bool(self._initialized_getter()) or not self.bot.is_ready():
            return web.json_response({'error': 'bot_not_ready'}, status=503)

        raw_id = request.match_info.get('discord_user_id', '')
        if not raw_id.isascii() or not raw_id.isdecimal() or len(raw_id) > 20:
            return web.json_response({'error': 'invalid_discord_user_id'}, status=400)
        discord_user_id = int(raw_id)
        if not 0 < discord_user_id <= 9_223_372_036_854_775_807:
            return web.json_response({'error': 'invalid_discord_user_id'}, status=400)

        try:
            user = await self.bot.fetch_user(discord_user_id)
        except discord.NotFound:
            return web.json_response({'error': 'discord_user_not_found'}, status=404)
        except discord.HTTPException:
            # Discord API outages/permission issues are not missing users.
            return web.json_response({'error': 'discord_user_lookup_unavailable'}, status=503)

        avatar = getattr(user, 'display_avatar', None)
        return web.json_response({
            'discordUserId': str(user.id),
            'username': user.name,
            'displayName': getattr(user, 'global_name', None) or user.display_name,
            'avatarUrl': str(avatar.url) if avatar is not None else None,
            'bot': bool(user.bot),
        })

    async def _handle_guild_resources(self, request: web.Request) -> web.Response:
        guild, error = self._authorized_guild(request)
        if error is not None:
            return error
        return web.json_response(build_guild_resources(guild))

    async def _handle_structure_preview(self, request: web.Request) -> web.Response:
        guild, error = self._authorized_guild(request)
        if error is not None:
            return error
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError('preview request must be an object')
            preview = build_structure_preview(guild, payload)
        except (ValueError, TypeError):
            return web.json_response({'error': 'invalid_preview_request'}, status=400)
        return web.json_response(preview)

    async def _handle_member_state(self, request: web.Request) -> web.Response:
        # Member state is administrative data. Unlike legacy read-only status
        # routes, it must fail closed when internal authentication is absent.
        if not self.token:
            return web.json_response({'error': 'member_state_auth_not_configured'}, status=503)
        if not bool(self._initialized_getter()) or not self.bot.is_ready():
            return web.json_response({'error': 'bot_not_ready'}, status=503)
        try:
            guild_id = int(request.match_info['guild_id'])
            discord_user_id = int(request.match_info['discord_user_id'])
        except (KeyError, TypeError, ValueError):
            return web.json_response({'error': 'invalid_member_reference'}, status=400)

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return web.json_response({'error': 'guild_not_found'}, status=404)
        member = guild.get_member(discord_user_id)
        if member is None:
            return web.json_response({'error': 'member_not_found'}, status=404)
        return web.json_response(build_member_state(member))

    async def _handle_identity_ban_propagation(
        self,
        request: web.Request,
    ) -> web.Response:
        from core.database import (
            banBelongsToGuild,
            getSatisfiedBanEffectDiscordIds,
            recordBanDiscordEffects,
        )
        from core.identity_bans import (
            compensate_unrecorded_propagated_bans,
            propagate_new_confirmed_identity_ban,
            summarize_ban_effects,
        )
        from core.discord_events import logIdentityBanPropagation

        if not self.token:
            return web.json_response(
                {'error': 'identity_ban_auth_not_configured'},
                status=503,
            )
        if not bool(self._initialized_getter()) or not self.bot.is_ready():
            return web.json_response({'error': 'bot_not_ready'}, status=503)

        try:
            guild_id = int(request.match_info['guild_id'])
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError
            ban_id = int(payload['banId'])
            reason = str(payload.get('reason') or '').strip()
            raw_identities = payload['identities']
            if ban_id <= 0 or not reason or not isinstance(raw_identities, list):
                raise ValueError

            identities: list[tuple[int, int]] = []
            for item in raw_identities:
                if not isinstance(item, dict):
                    raise ValueError
                user_id = int(item['userId'])
                discord_user_id = int(item['discordUserId'])
                if user_id <= 0 or discord_user_id <= 0:
                    raise ValueError
                identities.append((user_id, discord_user_id))
        except (KeyError, TypeError, ValueError):
            return web.json_response(
                {'error': 'invalid_identity_ban_request'},
                status=400,
            )

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return web.json_response({'error': 'guild_not_found'}, status=404)
        if not banBelongsToGuild(ban_id, guild_id):
            return web.json_response(
                {'error': 'ban_not_found_for_guild'},
                status=404,
            )

        satisfied = getSatisfiedBanEffectDiscordIds(ban_id)
        candidates = [
            (user_id, discord_user_id)
            for user_id, discord_user_id in identities
            if discord_user_id not in satisfied
        ]
        if not candidates:
            return web.json_response({
                'banId': ban_id,
                'processed': 0,
                'effects': {},
            })

        effects = await propagate_new_confirmed_identity_ban(
            guild,
            candidates,
            reason=reason,
        )
        if effects and not recordBanDiscordEffects(ban_id, effects):
            compensation_failed = await compensate_unrecorded_propagated_bans(
                guild,
                effects,
            )
            if compensation_failed:
                logger.critical(
                    "Falha ao compensar propagação automática sem ledger: "
                    "guild=%s ban_id=%s discord_ids=%s",
                    guild_id,
                    ban_id,
                    compensation_failed,
                )
            return web.json_response(
                {
                    'error': 'ban_effect_persistence_failed',
                    'compensationFailedDiscordIds': compensation_failed,
                },
                status=500,
            )

        if effects:
            await logIdentityBanPropagation(
                guild,
                ban_id,
                reason=reason,
                effects=effects,
            )

        return web.json_response({
            'banId': ban_id,
            'processed': len(effects),
            'effects': summarize_ban_effects(effects),
        })

    async def _handle_guild_operation(self, request: web.Request) -> web.Response:
        # Read-only status endpoints retain their legacy optional-token mode,
        # but Discord mutations must never be exposed in that mode.
        if not self.token:
            return web.json_response({'error': 'operation_auth_not_configured'}, status=503)
        guild, error = self._authorized_guild(request)
        if error is not None:
            return error
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("operation request must be an object")
            return web.json_response(await apply_guild_operation(guild, payload))
        except RuntimeError as error:
            if str(error).startswith("ambiguous_resource:"):
                return web.json_response({"error": str(error)}, status=409)
            return web.json_response({"error": "operation_failed"}, status=422)
        except PermissionError:
            return web.json_response({'error': 'discord_permission_denied'}, status=403)
        except discord.Forbidden:
            return web.json_response({'error': 'discord_permission_denied'}, status=403)
        except discord.NotFound:
            return web.json_response({'error': 'discord_resource_not_found'}, status=404)
        except discord.HTTPException:
            return web.json_response({'error': 'discord_operation_failed'}, status=503)
        except (ValueError, TypeError):
            return web.json_response({"error": "invalid_operation_request"}, status=422)

    async def _handle_auto_join_get(self, request: web.Request) -> web.Response:
        guild, error = self._authorized_guild(request)
        if error is not None:
            return error
        return web.json_response(read_auto_join(guild.id))

    async def _handle_auto_join_add(self, request: web.Request) -> web.Response:
        if not self.token:
            return web.json_response({'error': 'operation_auth_not_configured'}, status=503)
        guild, error = self._authorized_guild(request)
        if error is not None:
            return error
        try:
            return web.json_response(add_auto_join_role(guild, int(request.match_info['role_id'])))
        except ValueError as exc:
            return web.json_response({'error': str(exc)}, status=422)
        except RuntimeError:
            return web.json_response({'error': 'auto_join_save_failed'}, status=503)

    async def _handle_auto_join_delete(self, request: web.Request) -> web.Response:
        if not self.token:
            return web.json_response({'error': 'operation_auth_not_configured'}, status=503)
        guild, error = self._authorized_guild(request)
        if error is not None:
            return error
        try:
            return web.json_response(remove_auto_join_role(guild.id, int(request.match_info['role_id'])))
        except (ValueError, TypeError):
            return web.json_response({'error': 'invalid_role_id'}, status=400)
        except RuntimeError:
            return web.json_response({'error': 'auto_join_save_failed'}, status=503)

    async def _handle_auto_join_enabled(self, request: web.Request) -> web.Response:
        if not self.token:
            return web.json_response({'error': 'operation_auth_not_configured'}, status=503)
        guild, error = self._authorized_guild(request)
        if error is not None:
            return error
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or not isinstance(payload.get('enabled'), bool):
                raise ValueError
            return web.json_response(set_auto_join_enabled(guild.id, payload['enabled']))
        except ValueError:
            return web.json_response({'error': 'invalid_enabled_request'}, status=400)
        except RuntimeError:
            return web.json_response({'error': 'auto_join_save_failed'}, status=503)

    async def _handle_log_configs(self, request: web.Request) -> web.Response:
        guild, error = self._authorized_guild(request)
        if error is not None:
            return error
        # core.database opens its legacy pool on import: keep it lazy so test
        # collection and status-only imports never touch MariaDB.
        from core.database import getAllLogConfigs
        stored = {item['type']: item for item in getAllLogConfigs(guild.id)}
        logs = []
        for log_type, label in LOG_TYPE_LABELS.items():
            config = effective_call_config(lambda _, kind: stored.get(kind), guild.id) if log_type == 'call' else stored.get(log_type)
            target = None
            warnings: list[str] = []
            if config and config.get('log_channel'):
                channel, target_warnings = await validate_log_target(guild, config['log_channel'])
                if channel is None:
                    target = {'id': str(config['log_channel']), 'type': None, 'name': None, 'parentId': None, 'parentName': None, 'writable': False, 'missing': True}
                else:
                    target = log_target_metadata(channel, writable=not target_warnings)
                warnings.extend(target_warnings)
            logs.append({'type': log_type, 'label': label, 'enabled': bool(config and config.get('enabled')), 'target': target, 'warnings': warnings})
        legacy = [item['type'] for item in stored.values() if item['type'] not in DEFAULT_LOG_TYPES and item['type'] not in LEGACY_CALL_LOG_TYPES]
        return web.json_response({'logs': logs, 'targets': selectable_log_targets(guild), 'legacyTypes': legacy})

    async def _handle_log_update(self, request: web.Request) -> web.Response:
        if not self.token:
            return web.json_response({'error': 'operation_auth_not_configured'}, status=503)
        guild, error = self._authorized_guild(request)
        if error is not None:
            return error
        log_type = request.match_info.get('log_type')
        if log_type not in LOG_TYPE_LABELS:
            return web.json_response({'error': 'log_type_not_found'}, status=404)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError
            enabled = payload.get('enabled')
            target_id = payload.get('targetId')
            if not isinstance(enabled, bool) or (target_id is not None and not str(target_id).isdigit()):
                raise ValueError
            # Omitting targetId means "keep the stored destination", which is
            # how disabling a log preserves its convenient future destination.
            from core.database import getAllLogConfigs, upsertLogConfig
            existing = next((item for item in getAllLogConfigs(guild.id) if item['type'] == log_type), None)
            if target_id is None and existing is not None:
                target_id = existing.get('log_channel')
            if enabled and target_id is None:
                return web.json_response({'error': 'enabled_log_requires_target'}, status=422)
            target_changed = target_id is not None and (existing is None or str(existing.get('log_channel')) != str(target_id))
            if enabled or target_changed:
                _, warnings = await validate_log_target(guild, target_id)
                if warnings:
                    return web.json_response({'error': 'invalid_log_target', 'message': warnings[0]}, status=422)
            if not upsertLogConfig(guild.id, log_type, enabled, int(target_id) if target_id is not None else None):
                return web.json_response({'error': 'log_save_failed'}, status=503)
        except (ValueError, TypeError):
            return web.json_response({'error': 'invalid_log_request'}, status=400)
        return await self._handle_log_configs(request)

    async def _handle_log_test(self, request: web.Request) -> web.Response:
        if not self.token:
            return web.json_response({'error': 'operation_auth_not_configured'}, status=503)
        guild, error = self._authorized_guild(request)
        if error is not None:
            return error
        log_type = request.match_info.get('log_type')
        if log_type not in LOG_TYPE_LABELS:
            return web.json_response({'error': 'log_type_not_found'}, status=404)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError
            channel, warnings = await validate_log_target(guild, payload.get('targetId'))
            if channel is None or warnings:
                return web.json_response({'error': 'invalid_log_target', 'message': warnings[0] if warnings else 'Destino inválido.'}, status=422)
            embed = discord.Embed(title='Teste de logs do Coddy', description='Este destino está configurado corretamente para receber logs.', color=discord.Color.blurple())
            embed.set_footer(text=f'{LOG_TYPE_LABELS[log_type]} • {guild.name}')
            await channel.send(embed=embed)
        except (ValueError, TypeError):
            return web.json_response({'error': 'invalid_log_test_request'}, status=400)
        except (discord.Forbidden, discord.HTTPException):
            return web.json_response({'error': 'log_test_failed'}, status=422)
        return web.json_response({'ok': True})

    def _authorized_guild(self, request: web.Request):
        if not bool(self._initialized_getter()) or not self.bot.is_ready():
            return None, web.json_response({'error': 'bot_not_ready'}, status=503)
        try:
            guild_id = int(request.match_info['guild_id'])
            discord_user_id = int(request.match_info['discord_user_id'])
        except (KeyError, TypeError, ValueError):
            return None, web.json_response({'error': 'invalid_identifier'}, status=400)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None, web.json_response({'error': 'guild_not_found'}, status=404)
        if not can_manage_guild(guild, discord_user_id):
            return None, web.json_response({'error': 'guild_access_denied'}, status=403)
        return guild, None

    def _authorized_owner_guild(self, request: web.Request):
        if not bool(self._initialized_getter()) or not self.bot.is_ready():
            return None, None, web.json_response({'error': 'bot_not_ready'}, status=503)
        try:
            guild_id = int(request.match_info['guild_id'])
            discord_user_id = int(request.match_info['discord_user_id'])
        except (KeyError, TypeError, ValueError):
            return None, None, web.json_response({'error': 'invalid_identifier'}, status=400)

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None, None, web.json_response({'error': 'guild_not_found'}, status=404)
        if int(guild.owner_id or 0) != discord_user_id:
            return None, None, web.json_response({'error': 'guild_owner_required'}, status=403)
        return guild, discord_user_id, None

    async def _handle_backup_snapshot(self, request: web.Request) -> web.Response:
        if not self.token:
            return web.json_response({'error': 'backup_runtime_auth_not_configured'}, status=503)
        guild, error = self._authorized_guild(request)
        if error is not None:
            return error

        try:
            from core.database import get_backup_snapshot_operation

            actor_id = int(request.match_info['discord_user_id'])
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError
            operation_id = int(payload['operationId'])
            operation = get_backup_snapshot_operation(
                operation_id,
                int(guild.id),
            )
            if operation is None:
                return web.json_response(
                    {'error': 'backup_snapshot_operation_not_found'},
                    status=404,
                )
            if str(operation.get('backup_type') or '') != 'normal':
                return web.json_response(
                    {'error': 'backup_snapshot_type_mismatch'},
                    status=409,
                )
            if int(operation.get('actor_discord_user_id') or 0) != actor_id:
                return web.json_response(
                    {'error': 'backup_snapshot_actor_mismatch'},
                    status=403,
                )

            status = str(operation.get('status') or '')
            if status != 'PENDING':
                return web.json_response({
                    'operationId': operation_id,
                    'status': status,
                    'alreadyDispatched': True,
                })

            task = asyncio.create_task(
                self._backup_runtime.run_snapshot_operation(
                    guild,
                    operation_id=operation_id,
                ),
                name=f'backup-snapshot-{guild.id}-{operation_id}',
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except (KeyError, ValueError, TypeError):
            return web.json_response(
                {'error': 'invalid_backup_snapshot_request'},
                status=422,
            )
        except Exception:
            logger.exception(
                'Backup snapshot dispatch failed for guild %s',
                guild.id,
            )
            return web.json_response(
                {'error': 'backup_snapshot_dispatch_failed'},
                status=503,
            )

        return web.json_response({
            'operationId': operation_id,
            'status': 'PENDING',
            'accepted': True,
        }, status=202)

    async def _handle_backup_restore_preview(self, request: web.Request) -> web.Response:
        if not self.token:
            return web.json_response({'error': 'backup_runtime_auth_not_configured'}, status=503)
        guild, _actor_id, error = self._authorized_owner_guild(request)
        if error is not None:
            return error

        try:
            backup_id = int(request.match_info['backup_id'])
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError
            scope = str(payload.get('scope') or 'full').strip().lower()
            preflight = self._backup_runtime.build_restore_preflight(
                guild,
                backup_id,
                scope,
            )
        except LookupError:
            return web.json_response({'error': 'backup_not_found_for_guild'}, status=404)
        except (ValueError, TypeError):
            return web.json_response({'error': 'invalid_backup_restore_preview'}, status=422)
        except Exception:
            logger.exception('Backup restore preview failed for guild %s', guild.id)
            return web.json_response({'error': 'backup_restore_preview_failed'}, status=503)

        return web.json_response(preflight)

    async def _handle_backup_restore_dispatch(self, request: web.Request) -> web.Response:
        if not self.token:
            return web.json_response({'error': 'backup_runtime_auth_not_configured'}, status=503)
        guild, actor_id, error = self._authorized_owner_guild(request)
        if error is not None:
            return error

        try:
            from core.database import get_backup_restore_operation

            backup_id = int(request.match_info['backup_id'])
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError
            operation_id = int(payload['operationId'])
            scope = str(payload.get('scope') or '').strip().lower()
            decision = payload.get('decision') or {}
            if not isinstance(decision, dict):
                raise ValueError

            operation = get_backup_restore_operation(operation_id, int(guild.id))
            if operation is None or int(operation['backup_id']) != backup_id:
                return web.json_response(
                    {'error': 'backup_restore_operation_not_found'},
                    status=404,
                )
            if int(operation['actor_discord_user_id']) != int(actor_id):
                return web.json_response({'error': 'backup_restore_actor_mismatch'}, status=403)
            if str(operation['scope']) != scope:
                return web.json_response({'error': 'backup_restore_scope_mismatch'}, status=409)

            status = str(operation.get('status') or '')
            if status != 'PENDING':
                return web.json_response({
                    'operationId': operation_id,
                    'status': status,
                    'alreadyDispatched': True,
                })

            task = asyncio.create_task(
                self._backup_runtime.run_restore_operation(
                    guild,
                    operation_id=operation_id,
                    backup_id=backup_id,
                    scope=scope,
                    decision=decision,
                ),
                name=f'backup-restore-{guild.id}-{operation_id}',
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except (KeyError, ValueError, TypeError):
            return web.json_response({'error': 'invalid_backup_restore_request'}, status=422)
        except Exception:
            logger.exception('Backup restore dispatch failed for guild %s', guild.id)
            return web.json_response({'error': 'backup_restore_dispatch_failed'}, status=503)

        return web.json_response({
            'operationId': operation_id,
            'status': 'PENDING',
            'accepted': True,
        }, status=202)

    async def _handle_theme_preview(self, request: web.Request) -> web.Response:
        if not self.token:
            return web.json_response({'error': 'theme_runtime_auth_not_configured'}, status=503)
        guild, error = self._authorized_guild(request)
        if error is not None:
            return error
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError
            preview = self._theme_runtime.build_preflight(guild, payload)
        except (ValueError, TypeError):
            return web.json_response({'error': 'invalid_theme_preview'}, status=422)
        except Exception:
            logger.exception('Theme preview failed for guild %s', guild.id)
            return web.json_response({'error': 'theme_preview_failed'}, status=503)
        return web.json_response(preview)

    async def _handle_theme_apply_dispatch(self, request: web.Request) -> web.Response:
        return await self._handle_theme_operation_dispatch(request, expected_type='APPLY')

    async def _handle_theme_restore_dispatch(self, request: web.Request) -> web.Response:
        return await self._handle_theme_operation_dispatch(request, expected_type='RESTORE')

    async def _handle_theme_operation_dispatch(
        self,
        request: web.Request,
        *,
        expected_type: str,
    ) -> web.Response:
        if not self.token:
            return web.json_response({'error': 'theme_runtime_auth_not_configured'}, status=503)
        guild, error = self._authorized_guild(request)
        if error is not None:
            return error
        try:
            from core.database import get_theme_application, get_theme_operation

            actor_id = int(request.match_info['discord_user_id'])
            application_id = int(request.match_info['application_id'])
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError
            operation_id = int(payload['operationId'])
            operation = get_theme_operation(operation_id, int(guild.id))
            application = get_theme_application(application_id, int(guild.id))
            if (
                operation is None
                or application is None
                or int(operation.get('application_id') or 0) != application_id
            ):
                return web.json_response(
                    {'error': 'theme_operation_not_found'},
                    status=404,
                )
            if str(operation.get('operation_type') or '').upper() != expected_type:
                return web.json_response(
                    {'error': 'theme_operation_type_mismatch'},
                    status=409,
                )
            if int(operation.get('actor_discord_user_id') or 0) != actor_id:
                return web.json_response(
                    {'error': 'theme_operation_actor_mismatch'},
                    status=403,
                )
            status = str(operation.get('status') or '')
            if status != 'PENDING':
                return web.json_response({
                    'operationId': operation_id,
                    'applicationId': application_id,
                    'status': status,
                    'alreadyDispatched': True,
                })

            coroutine = (
                self._theme_runtime.run_apply_operation(
                    guild,
                    application_id=application_id,
                    operation_id=operation_id,
                )
                if expected_type == 'APPLY'
                else self._theme_runtime.run_restore_operation(
                    guild,
                    application_id=application_id,
                    operation_id=operation_id,
                )
            )
            task = asyncio.create_task(
                coroutine,
                name=(
                    f'theme-{expected_type.lower()}-'
                    f'{guild.id}-{application_id}-{operation_id}'
                ),
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except (KeyError, ValueError, TypeError):
            return web.json_response({'error': 'invalid_theme_operation_request'}, status=422)
        except Exception:
            logger.exception(
                'Theme %s dispatch failed for guild %s',
                expected_type.lower(),
                guild.id,
            )
            return web.json_response(
                {'error': 'theme_operation_dispatch_failed'},
                status=503,
            )

        return web.json_response({
            'operationId': operation_id,
            'applicationId': application_id,
            'status': 'PENDING',
            'accepted': True,
        }, status=202)

    async def recover_theme_operations(self) -> int:
        return await self._theme_runtime.recover_stale_operations()

    async def _handle_xp_simulation(self, request: web.Request) -> web.Response:
        if not self.token:
            return web.json_response({'error': 'xp_runtime_auth_not_configured'}, status=503)

        guild, error = self._authorized_guild(request)
        if error is not None:
            return error

        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError('simulation request must be an object')
            result = await simulate_xp_runtime(guild.id, payload)
        except (ValueError, TypeError) as exc:
            return web.json_response(
                {'error': 'invalid_xp_simulation', 'message': str(exc)},
                status=400,
            )
        except Exception:
            logger.exception('XP simulation failed for guild %s', guild.id)
            return web.json_response({'error': 'xp_simulation_unavailable'}, status=503)

        return web.json_response(result)

    async def _handle_xp_runtime_refresh(self, request: web.Request) -> web.Response:
        if not self.token:
            return web.json_response({'error': 'xp_runtime_auth_not_configured'}, status=503)

        guild, error = self._authorized_guild(request)
        if error is not None:
            return error

        try:
            result = await refresh_xp_runtime(guild.id)
        except Exception:
            logger.exception('XP runtime refresh failed for guild %s', guild.id)
            return web.json_response({'error': 'xp_runtime_refresh_unavailable'}, status=503)

        return web.json_response(result)

    async def _handle_logs(self, request: web.Request) -> web.Response:
        try:
            limit = _parse_log_limit(request.query.get('limit'))
            minimum_level, exact_level = _parse_log_level(request.query.get('level'))
            after = _parse_log_after(request.query.get('after'))
            before = _parse_log_before(request.query.get('before'))
            if after is not None and before is not None:
                raise ValueError('before and after cannot be used together')
        except ValueError as exc:
            return web.json_response({'error': str(exc)}, status=400)

        snapshot = self._log_handler.snapshot()
        oldest_sequence = snapshot.oldest_sequence
        latest_sequence = snapshot.latest_sequence
        cursor_expired = (
            after is not None and (
                (oldest_sequence is not None and after < oldest_sequence - 1)
                or after > latest_sequence
            )
        ) or (
            before is not None and (
                (oldest_sequence is None and before > 0)
                or (oldest_sequence is not None and before < oldest_sequence)
                or before > latest_sequence + 1
            )
        )
        items = [] if cursor_expired and before is not None else snapshot.filtered_items(
            minimum_level=minimum_level, exact_level=exact_level, after=after, before=before,
        )
        page = items[:limit] if after is not None else items[-limit:]
        # Only skip to the head when no matching records remain on another page.
        next_sequence = page[-1]['sequence'] if after is not None and len(items) > limit else latest_sequence
        return web.json_response(
            {
                'items': page,
                'totalBuffered': snapshot.total_buffered,
                'limit': limit,
                'oldestSequence': oldest_sequence,
                'latestSequence': latest_sequence,
                'nextSequence': next_sequence,
                'cursorExpired': cursor_expired,
            }
        )

    async def _handle_admin_stream(self, request: web.Request) -> web.StreamResponse:
        # Unlike legacy status reads, diagnostic streaming must fail closed.
        if not self.token:
            return web.json_response({'error': 'internal_auth_not_configured'}, status=503)
        try:
            minimum_level, exact_level = _parse_log_level(request.query.get('level'))
            cursor = _parse_log_after(request.query.get('after'))
        except ValueError as exc:
            return web.json_response({'error': str(exc)}, status=400)
        if self._admin_stream_stopping.is_set():
            return web.json_response({'error': 'stream_shutting_down'}, status=503)
        if self._active_admin_streams >= 8:
            return web.json_response({'error': 'stream_capacity_exceeded'}, status=429)
        self._active_admin_streams += 1
        response = web.StreamResponse(
            headers={
                'Content-Type': 'text/event-stream; charset=utf-8',
                'Cache-Control': 'no-cache, no-transform',
                'X-Accel-Buffering': 'no',
            }
        )
        try:
            await response.prepare(request)
            if cursor is None:
                cursor = self._log_handler.snapshot().latest_sequence
            loop = asyncio.get_running_loop()
            next_status = loop.time()
            while not self._admin_stream_stopping.is_set():
                snapshot = self._log_handler.snapshot()
                expired = (
                    cursor > snapshot.latest_sequence
                    or (snapshot.oldest_sequence is not None
                        and cursor < snapshot.oldest_sequence - 1)
                )
                if expired:
                    frame = {
                        'status': 'available', 'items': [],
                        'cursorExpired': True, 'nextSequence': snapshot.latest_sequence,
                        'latestSequence': snapshot.latest_sequence,
                        'oldestSequence': snapshot.oldest_sequence,
                        'totalBuffered': snapshot.total_buffered, 'limit': 1000,
                    }
                    cursor = snapshot.latest_sequence
                    await asyncio.wait_for(response.write(
                        ('event: logs\ndata: ' + json.dumps(frame) + '\n\n').encode()
                    ), timeout=5)
                elif snapshot.latest_sequence > cursor:
                    items = snapshot.filtered_items(
                        minimum_level=minimum_level,
                        exact_level=exact_level,
                        after=cursor,
                    )
                    page = items[:1000]
                    cursor = (
                        page[-1]['sequence']
                        if len(items) > 1000 else snapshot.latest_sequence
                    )
                    frame = {
                        'status': 'available', 'items': page,
                        'cursorExpired': False, 'nextSequence': cursor,
                        'latestSequence': snapshot.latest_sequence,
                        'oldestSequence': snapshot.oldest_sequence,
                        'totalBuffered': snapshot.total_buffered, 'limit': 1000,
                    }
                    await asyncio.wait_for(response.write(
                        ('event: logs\ndata: ' + json.dumps(frame) + '\n\n').encode()
                    ), timeout=5)
                now = loop.time()
                if now >= next_status:
                    status = build_status_payload(self._build_snapshot())
                    await asyncio.wait_for(response.write(
                        ('event: status\ndata: ' + json.dumps(status) + '\n\n').encode()
                    ), timeout=5)
                    next_status = loop.time() + 5.0
                await self._log_handler.wait_for_new_logs(
                    cursor,
                    timeout=max(0.01, min(5.0, next_status - loop.time())),
                )
        except (ConnectionError, ConnectionResetError, asyncio.TimeoutError, RuntimeError, OSError):
            # Client disconnected or stream write timed out. Never affect bot runtime.
            pass
        finally:
            self._active_admin_streams -= 1
            try:
                await response.write_eof()
            except (ConnectionError, RuntimeError, OSError):
                pass
        return response

    def _attach_log_handler(self) -> None:
        if self._logging_attached:
            return

        root_logger = logging.getLogger()
        self._add_handler_if_missing(root_logger, self._log_handler)

        self._add_handler_if_missing(access_logger, self._log_handler)
        root_stream_handler = next(
            (
                handler
                for handler in root_logger.handlers
                if _is_standard_stream_handler(handler)
                and handler is not self._log_handler
            ),
            None,
        )
        access_stream_handler = logging.StreamHandler(
            root_stream_handler.stream if root_stream_handler is not None else None
        )
        access_stream_handler.setLevel(logging.INFO)
        if root_stream_handler is not None:
            access_stream_handler.setFormatter(root_stream_handler.formatter)
        self._owned_access_stream_handler = access_stream_handler
        self._add_handler_if_missing(access_logger, access_stream_handler)

        # discord.py configures its own non-propagating logger, so root alone
        # would not retain gateway and websocket diagnostics.
        discord_logger = logging.getLogger('discord')
        if not discord_logger.propagate:
            self._add_handler_if_missing(discord_logger, self._log_handler)
        self._logging_attached = True
        logger.info('Admin log buffer initialized')

    def _add_handler_if_missing(self, logger: logging.Logger, handler: logging.Handler) -> None:
        if handler in logger.handlers:
            return
        logger.addHandler(handler)
        self._handlers_added_by_api.append((logger, handler))

    def _detach_log_handler(self) -> None:
        for target_logger, handler in self._handlers_added_by_api:
            target_logger.removeHandler(handler)
        self._handlers_added_by_api.clear()
        if self._owned_access_stream_handler is not None:
            self._owned_access_stream_handler.close()
            self._owned_access_stream_handler = None
        self._logging_attached = False

    async def start(self) -> None:
        if self._runner is not None:
            return
        self._admin_stream_stopping.clear()

        app = web.Application(middlewares=[_build_auth_middleware(self.token)] if self.token else [])
        app.router.add_get('/', self._handle_status)
        app.router.add_get('/status', self._handle_status)
        app.router.add_get('/health', self._handle_health)
        app.router.add_get('/live', self._handle_live)
        app.router.add_get('/ready', self._handle_ready)
        app.router.add_get('/managed-guilds/{discord_user_id}', self._handle_managed_guilds)
        app.router.add_get('/users/{discord_user_id}', self._handle_discord_user_lookup)
        app.router.add_get('/guilds/{guild_id}/owner', self._handle_guild_owner)
        app.router.add_get('/guilds/{guild_id}/members/{discord_user_id}', self._handle_member_state)
        app.router.add_get('/guilds/{guild_id}/resources/{discord_user_id}', self._handle_guild_resources)
        app.router.add_post('/guilds/{guild_id}/structure-preview/{discord_user_id}', self._handle_structure_preview)
        app.router.add_post('/guilds/{guild_id}/xp/simulation/{discord_user_id}', self._handle_xp_simulation)
        app.router.add_post('/guilds/{guild_id}/xp/runtime-refresh/{discord_user_id}', self._handle_xp_runtime_refresh)
        app.router.add_post('/guilds/{guild_id}/operations/{discord_user_id}', self._handle_guild_operation)
        app.router.add_post('/guilds/{guild_id}/identity-bans/propagate', self._handle_identity_ban_propagation)
        app.router.add_get('/guilds/{guild_id}/auto-join-roles/{discord_user_id}', self._handle_auto_join_get)
        app.router.add_get('/guilds/{guild_id}/logs/{discord_user_id}', self._handle_log_configs)
        app.router.add_post('/guilds/{guild_id}/logs/{log_type}/{discord_user_id}/test', self._handle_log_test)
        app.router.add_put('/guilds/{guild_id}/logs/{log_type}/{discord_user_id}', self._handle_log_update)
        app.router.add_post('/guilds/{guild_id}/auto-join-roles/{discord_user_id}/enabled', self._handle_auto_join_enabled)
        app.router.add_post('/guilds/{guild_id}/auto-join-roles/{role_id}/{discord_user_id}', self._handle_auto_join_add)
        app.router.add_delete('/guilds/{guild_id}/auto-join-roles/{role_id}/{discord_user_id}', self._handle_auto_join_delete)
        app.router.add_patch('/guilds/{guild_id}/auto-join-roles/{discord_user_id}', self._handle_auto_join_enabled)
        app.router.add_post('/guilds/{guild_id}/backups/{discord_user_id}', self._handle_backup_snapshot)
        app.router.add_post('/guilds/{guild_id}/backups/{backup_id}/restore-preview/{discord_user_id}', self._handle_backup_restore_preview)
        app.router.add_post('/guilds/{guild_id}/backups/{backup_id}/restore/{discord_user_id}', self._handle_backup_restore_dispatch)
        app.router.add_post('/guilds/{guild_id}/themes/preview/{discord_user_id}', self._handle_theme_preview)
        app.router.add_post('/guilds/{guild_id}/themes/applications/{application_id}/apply/{discord_user_id}', self._handle_theme_apply_dispatch)
        app.router.add_post('/guilds/{guild_id}/themes/applications/{application_id}/restore/{discord_user_id}', self._handle_theme_restore_dispatch)
        app.router.add_get('/logs', self._handle_logs)
        app.router.add_get('/admin-stream', self._handle_admin_stream)

        self._runner = web.AppRunner(
            app,
            access_log=access_logger,
            access_log_class=StatusApiAccessLogger,
        )
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self.host, port=self.port)
        await self._site.start()
        self._attach_log_handler()

    async def stop(self) -> None:
        # Wake live admin streams for a normal EOF before aiohttp runner.cleanup.
        # They check this at most five seconds apart (metrics interval).
        self._admin_stream_stopping.set()
        runner = self._runner
        try:
            if runner is not None:
                await runner.cleanup()
        finally:
            tasks = list(self._background_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._background_tasks.clear()
            self._detach_log_handler()
            self._runner = None
            self._site = None


def _parse_log_limit(raw_limit: str | None) -> int:
    if raw_limit is None:
        return 200
    try:
        limit = int(raw_limit)
    except ValueError as exc:
        raise ValueError('limit must be an integer between 1 and 1000') from exc
    if not 1 <= limit <= 1000:
        raise ValueError('limit must be an integer between 1 and 1000')
    return limit


def _is_standard_stream_handler(handler: logging.Handler) -> bool:
    return isinstance(handler, logging.StreamHandler) and getattr(handler, 'stream', None) in {
        sys.stdout,
        sys.stderr,
    }


def _parse_log_level(raw_level: str | None) -> tuple[int | None, int | None]:
    """Parse LEVEL (minimum) or LEVEL:exact (only that level)."""
    if raw_level is None:
        return None, None

    level_name, separator, mode = raw_level.upper().partition(':')
    level = logging.getLevelName(level_name)
    if not isinstance(level, int) or mode not in {'', 'EXACT'}:
        raise ValueError('level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL, optionally with :exact')
    return (None, level) if separator else (level, None)


def _parse_log_after(raw_after: str | None) -> int | None:
    if raw_after is None:
        return None
    try:
        after = int(raw_after)
    except ValueError as exc:
        raise ValueError('after must be a non-negative integer') from exc
    if after < 0:
        raise ValueError('after must be a non-negative integer')
    return after


def _parse_log_before(raw_before: str | None) -> int | None:
    if raw_before is None:
        return None
    try:
        before = int(raw_before)
    except ValueError as exc:
        raise ValueError('before must be a non-negative integer') from exc
    if before < 0:
        raise ValueError('before must be a non-negative integer')
    return before
