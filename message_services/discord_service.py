from core.AI_Functions.terceiras.openAI import *
from core.routine_functions import *
from core.discord_events import *
import core.discord_events as discord_events
from core.verifications import *
from core.database import *
from core.disboard_bump import resolve_assignable_bump_role, resolve_realtime_bump_channel_id
from core.monthly_bumps import run_monthly_reward_cycle
from core.auto_join_roles import is_sensitive
from core.form_views import register_published_form_views
from core.portaria_views import register_pending_portaria_views
from message_services.bot_status_api import BotStatusApi
from discord.ext import tasks
from discord import app_commands
from schemas.models.bot import *
from schemas.models.locals import *
from schemas.types.server_messages import *
from datetime import datetime, timedelta, time, timezone
from dateutil import tz
from typing import Literal
import aiohttp
import asyncio
import discord
import logging
import re
import os
from dotenv import load_dotenv
import random
from core.runtime_config import env_flag_enabled
from core.community_lifecycle_api import (
    CommunityLifecycleApiClient,
    CommunityLifecycleApiError,
    observe_network_with_retry,
)
from core.community_reconciliation import (
    reconcile_claimed_run,
    reconcile_guild,
    reconciliation_trigger_for_observation,
)
from core.membership_operation_gate import membership_event_guard
from core.membership_recovery_queue import MembershipRecoveryQueue
from core.discord_runtime import (
    configure_application_logging,
    configure_discord_logging,
    run_discord_bot,
)
from settings import DISCORD_BOT_PREFIX, DISCORD_INTENTS

logger = logging.getLogger(__name__)

BIRTHDAY_REGEX = re.compile(r'(\d{1,2})(?:\s?(?:d[eo]|\/|\\|\.|-)\s?|\s?)(\d{1,2}|(?:janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro))(?:\s?(?:d[eo]|\/|\\|\.|-)\s?|\s?)(\d{2}|\d{4})')
MONTHS = ['00','janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho', 'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro']

intents = discord.Intents.default()
for DISCORD_INTENT in DISCORD_INTENTS:
    setattr(intents, DISCORD_INTENT, True)
bot = MyBot(config=None,command_prefix=DISCORD_BOT_PREFIX, intents=discord.Intents.all())
bot.add_shutdown_callback(flush_xp_buffer_on_shutdown)
bot.add_shutdown_callback(close_async_pool)
levelConfig = None
timezone_offset = -3.0  # Pacific Standard Time (UTC−08:00)
def now() -> datetime: return (datetime.now(timezone(timedelta(hours=timezone_offset)))).replace(tzinfo=None)
initialized = False
DISBOARD_BOT_ID = 302050872383242240
processed_disboard_messages: set[int] = set()
_bump_warning_locks: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
AUTO_JOIN_SENSITIVE_PERMISSIONS = ("ban_members", "manage_roles", "manage_channels")
invite_cache: dict[int, dict[str, int]] = {}
status_api: BotStatusApi | None = None
community_lifecycle_client: CommunityLifecycleApiClient | None = None
_membership_recovery = MembershipRecoveryQueue()
_membership_startup_complete = asyncio.Event()
_vip_session_recovery_lock = asyncio.Lock()
_vip_session_recovery_last_at = 0.0
VIP_SESSION_RECOVERY_COOLDOWN_SECONDS = 60.0
LIFECYCLE_EVENT_RETRY_ATTEMPTS = 3
LIFECYCLE_EVENT_RETRY_BASE_SECONDS = 0.5
MAX_RECOVERY_RECONCILIATIONS_PER_PASS = 2
MAX_DRIFT_RECONCILIATIONS_PER_PASS = 3
MEMBERSHIP_RECOVERY_RETRY_DELAY_SECONDS = 15 * 60
TRANSIENT_LIFECYCLE_RECOVERY_RETRY_DELAY_SECONDS = 60


def _lifecycle_client() -> CommunityLifecycleApiClient:
    if community_lifecycle_client is None:
        raise CommunityLifecycleApiError("Client de lifecycle ainda não inicializado")
    return community_lifecycle_client


def _approved_resolver_for_guild(guild: discord.Guild):
    visitor_role_id = getGuildMemberNotVerifiedRoleId(guild.id)

    def resolve(member: discord.Member) -> bool:
        if not visitor_role_id:
            return True
        return all(role.id != visitor_role_id for role in member.roles)

    return resolve


async def _run_membership_event_with_retry(
    guild_id: int,
    member_id: int,
    operation,
    *,
    description: str,
) -> bool:
    async with membership_event_guard(guild_id, member_id):
        for attempt in range(1, LIFECYCLE_EVENT_RETRY_ATTEMPTS + 1):
            try:
                await operation()
                return True
            except CommunityLifecycleApiError as error:
                if error.retryable and attempt < LIFECYCLE_EVENT_RETRY_ATTEMPTS:
                    await asyncio.sleep(
                        LIFECYCLE_EVENT_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                    )
                    continue
                _membership_recovery.mark_required(guild_id)
                logger.exception(
                    "%s; guild %s marcada para recovery status=%s retryable=%s error=%s",
                    description,
                    guild_id,
                    error.status,
                    error.retryable,
                    error,
                )
                return False


async def _observe_member_via_api(member: discord.Member) -> bool:
    async def operation():
        await _lifecycle_client().observe_member(
            member,
            approved=_approved_resolver_for_guild(member.guild)(member),
        )

    return await _run_membership_event_with_retry(
        member.guild.id,
        member.id,
        operation,
        description=f"Falha ao sincronizar membro {member.id} com a API",
    )


async def _observe_guild_owner_via_api(guild: discord.Guild) -> bool:
    owner_id = guild.owner_id or getattr(getattr(guild, "owner", None), "id", None)
    if not owner_id:
        _membership_recovery.mark_required(guild.id)
        logger.error(
            "Owner indisponível durante bootstrap da guild %s; recovery agendado",
            guild.id,
        )
        return False

    owner = guild.get_member(owner_id)
    if owner is None:
        try:
            owner = await guild.fetch_member(owner_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            _membership_recovery.mark_required(guild.id)
            logger.exception(
                "Falha ao obter owner da guild %s durante bootstrap",
                guild.id,
            )
            return False

    return await _observe_member_via_api(owner)


async def _observe_live_guilds() -> dict[int, dict]:
    client = _lifecycle_client()
    observations: dict[int, dict] = {}
    for guild in bot.guilds:
        try:
            observations[guild.id] = await client.observe_network(guild, active=True)
        except CommunityLifecycleApiError as error:
            _membership_recovery.mark_required(guild.id)
            logger.exception(
                "Falha ao observar guild %s na API status=%s retryable=%s error=%s",
                guild.id,
                error.status,
                error.retryable,
                error,
            )
    try:
        await client.reconcile_presence(bot.guilds)
    except CommunityLifecycleApiError as error:
        logger.exception(
            "Falha ao reconciliar presença global das guilds na API status=%s retryable=%s error=%s",
            error.status,
            error.retryable,
            error,
        )
    return observations


async def _reconcile_all_guilds(trigger: str) -> None:
    for guild in list(bot.guilds):
        await _reconcile_one_guild_logged(guild, trigger)


async def _recover_memberships_from_network_health(*, startup: bool) -> None:
    observations = await _observe_live_guilds()

    for guild in list(bot.guilds):
        observation = observations.get(guild.id)
        if not observation:
            _membership_recovery.mark_required(guild.id)
            continue

        if _membership_recovery.is_deferred(guild.id):
            logger.info(
                "Recovery de membership da guild %s permanece em cooldown",
                guild.id,
            )
            continue

        sync_state = observation.get("membershipSyncState")
        trigger = reconciliation_trigger_for_observation(
            sync_state=sync_state,
            tracked_present_members=observation.get("trackedPresentMembers"),
            provider_member_count=guild.member_count,
            local_recovery_required=_membership_recovery.is_required(guild.id),
            required_state_trigger="STARTUP" if startup else "DRIFT",
            recover_stale_reconciling=startup,
            membership_reconciliation_running=bool(
                observation.get("membershipReconciliationRunning")
            ),
        )
        if trigger is None:
            continue

        await _reconcile_one_guild_logged(guild, trigger)


async def _bootstrap_membership_sync_runtime() -> None:
    try:
        await _recover_memberships_from_network_health(startup=True)
    except Exception:
        logger.exception("Falha inesperada na recuperação inicial de membership")
    finally:
        # Manual/queued reconciliations must not race the fresh-process recovery
        # that decides whether RECONCILING was inherited from a dead process.
        _membership_startup_complete.set()


async def _reconcile_one_guild_logged(
    guild: discord.Guild,
    trigger: str,
) -> bool:
    generation_at_start = _membership_recovery.generation(guild.id)
    try:
        await reconcile_guild(
            guild,
            _lifecycle_client(),
            trigger=trigger,
            approved_resolver=_approved_resolver_for_guild(guild),
        )
        if trigger in {"GUILD_JOIN", "RECOVERY", "STARTUP"}:
            initialize_guild_server_settings(guild.id)
            refresh_bot_guild_configs()
            await _refresh_invite_cache_for_guild(guild)
        _membership_recovery.clear_if_unchanged(
            guild.id,
            generation_at_start,
        )
        return True
    except CommunityLifecycleApiError as error:
        retry_delay = (
            TRANSIENT_LIFECYCLE_RECOVERY_RETRY_DELAY_SECONDS
            if error.retryable
            else MEMBERSHIP_RECOVERY_RETRY_DELAY_SECONDS
        )
        _membership_recovery.mark_required(
            guild.id,
            delay_seconds=retry_delay,
        )
        logger.exception(
            "Falha na reconciliação da guild %s (%s) status=%s retryable=%s error=%s retry_in=%ss",
            guild.id,
            trigger,
            error.status,
            error.retryable,
            error,
            retry_delay,
        )
        return False
    except Exception as error:
        _membership_recovery.mark_required(
            guild.id,
            delay_seconds=MEMBERSHIP_RECOVERY_RETRY_DELAY_SECONDS,
        )
        logger.exception(
            "Falha na reconciliação completa da guild %s (%s) error=%s retry_in=%ss",
            guild.id,
            trigger,
            error,
            MEMBERSHIP_RECOVERY_RETRY_DELAY_SECONDS,
        )
        return False


def _bump_warning_lock(guild_id: int) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    entry = _bump_warning_locks.get(guild_id)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _bump_warning_locks[guild_id] = entry
    return entry[1]


async def load_cogs():
    """Carrega todas as cogs do bot e retorna uma lista de erros."""
    errors = []
    for filename in os.listdir('cogs'):
        if filename.endswith('.py'):
            try:
                await bot.load_extension(f'cogs.{filename[:-3]}')
            except Exception as e:
                errors.append((filename, e))
    return errors


def refresh_bot_guild_configs() -> None:
    # Lifecycle is already observed through the API. This compatibility helper
    # now creates only runtime config rows for API-registered guilds.
    ensure_community_registration_for_guilds(bot.guilds)

    guild_configs: list[Config] = []
    for guild in bot.guilds:
        guild_config = getConfig(guild, ensure_registration=False)
        if guild_config:
            guild_configs.append(Config(guild_config))

    bot.config = guild_configs


async def _recover_vip_custom_roles_after_session_reset(trigger: str) -> None:
    """Repair only persisted VIP custom roles after a fresh Gateway session."""

    global _vip_session_recovery_last_at

    async with _vip_session_recovery_lock:
        loop = asyncio.get_running_loop()
        now_monotonic = loop.time()
        if (
            _vip_session_recovery_last_at > 0
            and now_monotonic - _vip_session_recovery_last_at
                < VIP_SESSION_RECOVERY_COOLDOWN_SECONDS
        ):
            logger.info(
                "VIP session recovery ignorado por cooldown: trigger=%s",
                trigger,
            )
            return
        _vip_session_recovery_last_at = now_monotonic

        guild_by_id = {int(guild.id): guild for guild in bot.guilds}
        if not guild_by_id:
            return

        try:
            grouped_entries = await async_getVipCustomRolesForGuildIds(
                set(guild_by_id)
            )
        except Exception:
            logger.exception(
                "Falha ao carregar estado VIP para recovery de sessão: trigger=%s",
                trigger,
            )
            return

        total_processed = 0
        total_updated = 0
        total_deleted = 0
        total_warnings = 0

        for guild_id, entries in grouped_entries.items():
            guild = guild_by_id.get(int(guild_id))
            if guild is None or not entries:
                continue

            try:
                result = await recoverVipCustomRolesAfterSessionReset(
                    guild,
                    entries,
                )
            except Exception:
                logger.exception(
                    "Falha no recovery VIP da guild_id=%s trigger=%s",
                    guild_id,
                    trigger,
                )
                continue

            total_processed += int(result.get("processed", 0))
            total_updated += int(result.get("updated", 0))
            total_deleted += int(result.get("deleted", 0))
            total_warnings += len(result.get("warnings") or [])

        logger.info(
            "VIP session recovery concluído: trigger=%s guilds=%s processed=%s updated=%s deleted=%s warnings=%s",
            trigger,
            len(grouped_entries),
            total_processed,
            total_updated,
            total_deleted,
            total_warnings,
        )


async def _recover_theme_operations_logged(trigger: str) -> None:
    if status_api is None:
        return
    try:
        recovered = await status_api.recover_theme_operations()
        if recovered:
            logger.warning(
                "Operações de Theme reconciliadas após %s: %s",
                trigger,
                recovered,
            )
    except Exception:
        logger.exception(
            "Falha ao reconciliar operações stale de Theme após %s",
            trigger,
        )


async def initialize_bot():
    global initialized
    if initialized: return

    # guild = bot.get_guild(DISCORD_GUILD_ID)
    # bot.config.append(Config(getConfig(guild)))
    errors = await load_cogs()
    await _observe_live_guilds()
    refresh_bot_guild_configs()
    if errors:
        logger.error('Falha ao carregar todas as cogs. Sincronização de comandos cancelada.')
        for filename, err in errors:
            logger.error(
                'Falha ao carregar cog %s',
                filename,
                exc_info=(type(err), err, err.__traceback__),
            )
    else:
        logger.info('Todas as cogs carregadas com sucesso!')
        try:
            synced = await bot.tree.sync()
            logger.info('%d Comandos sincronizados com sucesso!', len(synced))
        except Exception:
            logger.exception('Falha ao sincronizar comandos do Discord')
    bumpWarning.start()
    bumpReward.start()
    cronJobs12h.start()
    cronJobs30m.start()
    timeSpecificTasks.start()
    communityMembershipDriftWatch.start()
    communityMembershipRecoveryWorker.start()
    communitySyncQueueWorker.start()
    if env_flag_enabled('BOT_DAILY_RESTART_ENABLED', default=True):
        dailyRestart.start()
    register_pending_portaria_views(bot)
    register_published_form_views(bot)
    for guild in bot.guilds:
        await _refresh_invite_cache_for_guild(guild)
    initialized = True
    asyncio.create_task(_bootstrap_membership_sync_runtime())
    asyncio.create_task(
        _recover_vip_custom_roles_after_session_reset("COLD_START")
    )
    asyncio.create_task(_recover_theme_operations_logged("COLD_START"))


async def _refresh_invite_cache_for_guild(guild: discord.Guild) -> None:
    me = guild.me or guild.get_member(bot.user.id if bot.user else 0)
    if me is None or not me.guild_permissions.manage_guild:
        return

    try:
        invites = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        return

    invite_cache[guild.id] = {invite.code: int(invite.uses or 0) for invite in invites}


async def _detect_used_invite(member: discord.Member) -> tuple[str | None, str | None]:
    guild = member.guild
    previous_snapshot = invite_cache.get(guild.id, {})
    me = guild.me or guild.get_member(bot.user.id if bot.user else 0)
    if me is None or not me.guild_permissions.manage_guild:
        return None, None

    try:
        current_invites = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        return None, None

    invite_deltas: list[tuple[discord.Invite, int]] = []
    current_snapshot: dict[str, int] = {}
    for invite in current_invites:
        uses = int(invite.uses or 0)
        current_snapshot[invite.code] = uses
        previous_uses = int(previous_snapshot.get(invite.code, 0))
        if uses > previous_uses:
            invite_deltas.append((invite, uses - previous_uses))

    invite_cache[guild.id] = current_snapshot
    if not invite_deltas:
        return None, None

    used_invite: discord.Invite | None = None
    if len(invite_deltas) == 1:
        used_invite = invite_deltas[0][0]
    else:
        max_delta = max(delta for _, delta in invite_deltas)
        top_candidates = [invite for invite, delta in invite_deltas if delta == max_delta]
        if len(top_candidates) == 1:
            used_invite = top_candidates[0]

    if used_invite is None:
        return None, None

    invite_link = f"https://discord.gg/{used_invite.code}"
    inviter = used_invite.inviter
    invited_by = f"{inviter} ({inviter.id})" if inviter else None
    return invite_link, invited_by


@bot.event
async def on_ready():
    logger.info('Logado como %s', bot.user)
    bot._last_ready_at = datetime.now(timezone.utc)
    if initialized:
        logger.info(
            "Nova sessão READY após inicialização; verificando health/drift e custom roles VIP persistidos"
        )
        asyncio.create_task(_recover_memberships_from_network_health(startup=False))
        asyncio.create_task(
            _recover_vip_custom_roles_after_session_reset("NEW_GATEWAY_SESSION")
        )
        asyncio.create_task(_recover_theme_operations_logged("NEW_GATEWAY_SESSION"))
        return
    await initialize_bot()


@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        await observe_network_with_retry(
            _lifecycle_client(),
            guild,
            active=True,
            attempts=LIFECYCLE_EVENT_RETRY_ATTEMPTS,
            base_delay_seconds=LIFECYCLE_EVENT_RETRY_BASE_SECONDS,
        )
    except CommunityLifecycleApiError:
        _membership_recovery.mark_required(guild.id)
        logger.exception(
            "Falha ao provisionar Community para guild %s; recovery agendado",
            guild.id,
        )
        return

    # Runtime config only comes after API-owned tenant/network provisioning.
    initialize_guild_server_settings(guild.id)
    refresh_bot_guild_configs()

    # Resolve the Discord owner immediately so the new Community becomes
    # visible/manageable on the site without waiting for the full member scan.
    await _observe_guild_owner_via_api(guild)

    await _refresh_invite_cache_for_guild(guild)
    asyncio.create_task(_reconcile_one_guild_logged(guild, "GUILD_JOIN"))


@bot.event
async def on_guild_remove(guild: discord.Guild):
    try:
        await _lifecycle_client().observe_network(guild, active=False)
    except CommunityLifecycleApiError:
        logger.exception("Falha ao registrar saída da guild %s na API", guild.id)
    refresh_bot_guild_configs()
    invite_cache.pop(guild.id, None)


@bot.event
async def on_guild_update(before: discord.Guild, after: discord.Guild):
    owner_changed = before.owner_id != after.owner_id
    name_changed = before.name != after.name
    if not owner_changed and not name_changed:
        return

    if owner_changed and after.owner_id:
        owner = after.get_member(after.owner_id)
        if owner is None:
            try:
                owner = await after.fetch_member(after.owner_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                owner = None
        if owner is not None:
            await _observe_member_via_api(owner)

    try:
        await _lifecycle_client().observe_network(after, active=True)
    except CommunityLifecycleApiError:
        logger.exception("Falha ao sincronizar atualização da guild %s", after.id)


@bot.event
async def on_invite_create(invite: discord.Invite):
    guild = invite.guild
    if guild is None:
        return
    await _refresh_invite_cache_for_guild(guild)


@bot.event
async def on_invite_delete(invite: discord.Invite):
    guild = invite.guild
    if guild is None:
        return
    await _refresh_invite_cache_for_guild(guild)


@bot.event
async def on_member_join(member: discord.Member):
    invite_link_used, invited_by = await _detect_used_invite(member)

    membership_synced = await _observe_member_via_api(member)

    if membership_synced:
        try:
            update_user_community_status_invite_info(
                member.guild.id,
                member,
                invite_link_used,
                invited_by,
            )
        except Exception:
            logger.exception('Falha ao registrar convite de entrada para %s', member.id)

    config = getAutoJoinRolesConfig(member.guild.id)
    if not config.get("enabled"):
        return

    configured_role_ids = config.get("roleIds") or []
    guild_roles = [member.guild.get_role(int(role_id)) for role_id in configured_role_ids]
    guild_roles = [role for role in guild_roles if role is not None]
    if not guild_roles:
        return

    bot_member = member.guild.me or member.guild.get_member(bot.user.id if bot.user else 0)
    if bot_member is None or not bot_member.guild_permissions.manage_roles:
        logger.warning(
            'Falha ao aplicar auto-cargos para %s: bot sem permissão de gerenciar cargos',
            member.id,
        )
        return

    assignable_roles: list[discord.Role] = []
    skipped_role_ids: list[int] = []
    staff_role_ids = set(getStaffRoles(member.guild.id))
    for role in guild_roles:
        has_sensitive_permissions = is_sensitive(role)
        if (
            role.id in staff_role_ids
            or has_sensitive_permissions
            or role.is_default()
            or role.managed
            or role >= bot_member.top_role
        ):
            skipped_role_ids.append(role.id)
            continue
        assignable_roles.append(role)

    if not assignable_roles:
        if skipped_role_ids:
            logger.warning(
                'Nenhum auto-cargo atribuível para %s. Ignorados: %s',
                member.id,
                ', '.join(str(role_id) for role_id in skipped_role_ids),
            )
        return

    try:
        await member.add_roles(
            *assignable_roles,
            reason="Auto-cargos configurados para entrada de novos membros",
        )
    except (discord.Forbidden, discord.HTTPException):
        logger.exception('Falha ao aplicar auto-cargos para %s', member.id)


@bot.event
async def on_member_remove(member: discord.Member):
    async def operation():
        await _lifecycle_client().remove_member(member.guild.id, member.id)

    await _run_membership_event_with_retry(
        member.guild.id,
        member.id,
        operation,
        description=f"Falha ao registrar saída do membro {member.id}",
    )

    try:
        await removeVipCustomRoleForMember(
            member.guild,
            member.id,
            reason="Membro saiu do servidor",
        )
    except (discord.Forbidden, discord.HTTPException):
        logger.exception(
            "Falha ao remover cargo VIP personalizado após saída: guild_id=%s user_id=%s",
            member.guild.id,
            member.id,
        )


@bot.tree.error
async def on_app_command_error(ctx, error):
    async def _reply_user(message: str):
        try:
            if ctx.response.is_done():
                return await ctx.followup.send(message, ephemeral=True)
            return await ctx.response.send_message(message, ephemeral=True)
        except Exception:
            return None

    async def _notify_admin(error_message: str):
        channel_name = getattr(ctx.channel, 'name', str(ctx.channel))
        user_name = getattr(ctx.user, 'display_name', str(ctx.user))
        command_name = getattr(getattr(ctx, 'command', None), 'name', 'desconhecido')
        text = (
            'Coddy apresentou um erro: \n'
            f'**Canal**: {channel_name} \n'
            f'**Usuário**: {user_name} \n'
            f'**Comando**: {command_name} \n'
            f'**Erro**:{error_message}'
        )

        telegram_token = os.getenv('TELEGRAM_TOKEN')
        telegram_admin = os.getenv('TELEGRAM_ADMIN')
        if not telegram_token or not telegram_admin:
            return

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(
                    f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                    json={"chat_id": telegram_admin, "text": text},
                ) as response:
                    response.raise_for_status()
        except Exception as notify_error:
            # Telegram request errors can include the URL, which embeds the bot
            # token. Keep the administrative log useful without exposing it.
            logger.error(
                'Erro ao notificar no Telegram (tipo=%s)',
                type(notify_error).__name__,
            )

    if isinstance(error, app_commands.AppCommandError) and 'Member' in str(error) and 'not found' in str(error):
        match = re.search(r"(\d{17,20})", str(error))
        if match:
            member_id = int(match.group(1))
            try:
                user = await bot.fetch_user(member_id)
            except Exception:
                user = None
            includeUser(user if user else str(member_id), ctx.guild.id)
        return await _reply_user('Não foi possível encontrar o membro informado.')
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        return await _reply_user('Você não informou todos os argumentos necessários para o comando.')
    if isinstance(error, commands.BadArgument):
        return await _reply_user('Você informou um argumento inválido para o comando.')
    if isinstance(error, commands.MissingPermissions):
        return await _reply_user('Você não tem permissão para fazer isso.')
    if isinstance(error, commands.CommandOnCooldown):
        return await _reply_user(f'Esse comando está em cooldown! Tente novamente em {error.retry_after:.2f} segundos.')

    original_error = getattr(error, 'original', error)
    if isinstance(original_error, discord.DiscordServerError):
        reply = await _reply_user('Discord indisponível temporariamente. Tente novamente em instantes.')
        asyncio.create_task(_notify_admin(original_error))
        return reply

    reply = await _reply_user('Ocorreu um erro ao processar seu comando. Tente novamente em instantes.')
    asyncio.create_task(_notify_admin(original_error))
    return reply

@bot.event
async def on_message(message: discord.Message):
    if message.author.id == DISBOARD_BOT_ID:
        await handle_disboard_bump(message)
        return

    if message.author.bot:
        return

    await mentionHashtagRoles(bot, message)
    await enforceThreadAuthorOnlyPosting(message)
    await handle_message_xp(bot, message)
    await handle_ai_response(bot, message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    await logMessageEdit(before, after)


@bot.event
async def on_message_delete(message: discord.Message):
    await logMessageDelete(message)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if payload.cached_message is None:
        await logMessageDelete(None, bot=bot, payload=payload)

@bot.event
async def on_reaction_add(reaction, user):
    if user is None or getattr(user, "bot", False):
        return

    message = getattr(reaction, "message", None)
    if message is None or message.guild is None:
        return

    config = getCollaborativeModerationConfig(message.guild.id)
    if not config.get("enabled"):
        return

    configured_emoji = config.get("emoji")
    if not configured_emoji:
        return

    if str(reaction.emoji) != str(configured_emoji):
        return

    author = message.author
    if isinstance(author, discord.Member):
        author_member = author
    else:
        author_member = message.guild.get_member(author.id) if author else None
        if author_member is None and author is not None:
            try:
                author_member = await message.guild.fetch_member(author.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                author_member = None

    if author_member is not None:
        staff_roles = discord_events.getStaffRoles(message.guild)
        if any(role in author_member.roles for role in staff_roles):
            return

    required_reactions = max(1, int(config.get("minReactions") or 1))
    valid_reactions = 0
    async for reacting_user in reaction.users():
        if getattr(reacting_user, "bot", False):
            continue
        valid_reactions += 1
        if valid_reactions >= required_reactions:
            break

    if valid_reactions < required_reactions:
        return

    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return


def is_disboard_bump_confirmation(message: discord.Message) -> bool:
    """Return True when a Disboard message confirms a successful /bump."""

    interaction = getattr(message, "interaction", None)
    command_name = getattr(interaction, "name", None)

    if command_name and command_name.lower() != "bump":
        return False

    content = (message.content or "").lower()
    if "bump done" in content:
        return True

    for embed in message.embeds:
        description = (embed.description or "").lower()
        title = (embed.title or "").lower()

        if "bump done" in description or "bump done" in title:
            return True
        if "you can bump again" in description or "thanks for bumping" in description:
            return True

    return False


async def handle_disboard_bump(message: discord.Message):
    """Handle bump confirmation scheduling warnings and rewards."""

    if message.id in processed_disboard_messages:
        return

    processed_disboard_messages.add(message.id)

    interaction = getattr(message, "interaction", None)
    bumper = getattr(interaction, "user", None)

    if not interaction or not is_disboard_bump_confirmation(message):
        return

    if not bumper or not message.guild:
        return

    bump_config = await asyncio.to_thread(getBumpConfig, message.guild.id)
    allowed_processing_channels = await asyncio.to_thread(
        get_allowed_feature_channels, message.guild.id, "disboard_bump_processing"
    )
    if len(allowed_processing_channels) > 1:
        logger.warning(
            'Mais de um Canal de Bump configurado na guild %s; usando deterministicamente o primeiro (%s)',
            message.guild.id,
            allowed_processing_channels[0],
        )
    realtime_bump_channel_id = resolve_realtime_bump_channel_id(
        allowed_processing_channels,
        bump_config.get("warnDisboardChannelId"),
    )
    if realtime_bump_channel_id is not None and getattr(message.channel, "id", None) != realtime_bump_channel_id:
        return
    bump_interval = timedelta(hours=2)
    bump_moment = message.created_at.astimezone(tz.gettz('America/Sao_Paulo')).replace(tzinfo=None)

    async with _bump_warning_lock(message.guild.id):
        try:
            await asyncio.to_thread(
                setBumpWarningSchedule,
                message.guild.id,
                next_at=bump_moment + bump_interval if bump_config.get("warnEnabled") else None,
                last_bump_at=bump_moment,
            )
        except Exception:
            logger.exception('Falha ao registrar horário operacional do bump na guild %s', message.guild.id)

    reward_coins_enabled = bool(bump_config.get("rewardCoinsEnabled"))
    reward_coins = max(0, int(bump_config.get("rewardCoins") or 0))

    if reward_coins_enabled and reward_coins > 0:
        try:
            await asyncio.to_thread(adjust_user_economy_balance, message.guild.id, bumper, reward_coins)
        except Exception:
            logger.exception('Falha ao adicionar recompensa em moedas de bump')

    reward_role_enabled = bool(bump_config.get("rewardEnabled"))
    reward_role_id = bump_config.get("rewardTempRoleId")
    reward_role_minutes = max(0, int(bump_config.get("rewardRoleMinutes") or 0))
    role_granted = False
    if reward_role_enabled and reward_role_id and reward_role_minutes > 0:
        member = bumper if isinstance(bumper, discord.Member) else message.guild.get_member(bumper.id)
        role = resolve_assignable_bump_role(message.guild, reward_role_id)
        if member is None:
            logger.warning('Membro do bump não está disponível na guild %s', message.guild.id)
        elif role is None:
            logger.warning('Cargo individual de bump %s não é atribuível na guild %s', reward_role_id, message.guild.id)
        else:
            try:
                role_granted = bool(await assignTempRole(
                    message.guild.id,
                    member,
                    role.id,
                    now() + timedelta(minutes=reward_role_minutes),
                    "Bump Individual Reward",
                ))
            except Exception:
                logger.exception('Falha ao adicionar cargo individual de bump')

    reward_message = (bump_config.get("rewardMessage") or "").strip()
    if role_granted and reward_message:
        try:
            await message.channel.send(f"{bumper.mention}, {reward_message}", delete_after=30)
        except Exception:
            logger.exception('Falha ao enviar mensagem da recompensa individual de bump')

@bot.event
async def on_guild_role_delete(role: discord.Role):
    owner_ids = await getVipCustomRoleOwnerIds(role.guild.id, {role.id})
    for discord_user_id in owner_ids:
        clearCustomRoleRoleId(role.guild.id, discord_user_id)


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    # Position-only updates are not VIP business-state changes. Reconciliation
    # can itself reposition a role, and Discord emits GUILD_ROLE_UPDATE events
    # for roles shifted by that operation. Re-entering reconciliation from
    # those events creates a feedback loop of PATCH /guilds/{guild_id}/roles.
    if not shouldReconcileVipRoleUpdate(before, after):
        return

    owner_ids = await getVipCustomRoleOwnerIds(after.guild.id, {after.id})
    for discord_user_id in owner_ids:
        try:
            await reconcileVipMember(after.guild, discord_user_id)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Falha na reconciliação VIP por alteração do cargo: guild_id=%s role_id=%s user_id=%s",
                after.guild.id,
                after.id,
                discord_user_id,
            )


@bot.event
async def on_member_update(before:discord.member.Member, after:discord.member.Member):
    moment = now()
    changes = checkRolesUpdate(before, after)

    visitor_role_id = getGuildMemberNotVerifiedRoleId(after.guild.id)
    visitor_role = after.guild.get_role(visitor_role_id) if visitor_role_id else None
    approval = None
    if changes and visitor_role is not None:
        removed_roles_for_approval = changes.get('removidos', [])
        added_roles_for_approval = changes.get('adicionados', [])
        if visitor_role in removed_roles_for_approval:
            approval = True
        elif visitor_role in added_roles_for_approval:
            approval = False

    if changes or before.nick != after.nick:
        async def member_update_operation():
            await _lifecycle_client().observe_member(
                after,
                approved=_approved_resolver_for_guild(after.guild)(after),
            )
            if approval is not None:
                await _lifecycle_client().observe_member_approval(
                    after.guild.id,
                    after.id,
                    approved=approval,
                )

        await _run_membership_event_with_retry(
            after.guild.id,
            after.id,
            member_update_operation,
            description=f"Falha ao sincronizar atualização do membro {after.id}",
        )

    if changes:
        changed_roles = [
            *(changes.get('adicionados', []) or []),
            *(changes.get('removidos', []) or []),
        ]
        changed_role_ids = {role.id for role in changed_roles}
        configured_vip_role_ids, affected_vip_users = await asyncio.gather(
            async_getGuildVipRoleIds(after.guild.id),
            getVipCustomRoleOwnerIds(after.guild.id, changed_role_ids),
        )
        guild_vip_role_ids = set(configured_vip_role_ids)
        if changed_role_ids.intersection(guild_vip_role_ids):
            affected_vip_users.add(after.id)

        for discord_user_id in affected_vip_users:
            try:
                await reconcileVipMember(after.guild, discord_user_id)
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Falha na reconciliação VIP por evento de cargo: guild_id=%s user_id=%s",
                    after.guild.id,
                    discord_user_id,
                )

    if before.nick != after.nick:
        await logProfileChange(
            bot,
            after.guild,
            after,
            {"Apelido": (before.nick or before.name, after.nick or after.name)},
        )
    before_timeout = getattr(before, "timed_out_until", None)
    after_timeout = getattr(after, "timed_out_until", None)
    if after_timeout and after_timeout != before_timeout:
        after_timeout_naive = (
            after_timeout.replace(tzinfo=None)
            if getattr(after_timeout, "tzinfo", None)
            else after_timeout
        )
        if after_timeout_naive > moment:
            await logMute(after.guild, after, after_timeout, moment)


@bot.event
async def on_user_update(before: discord.User, after: discord.User):
    changes = {}
    username_changed = before.name != after.name
    if before.global_name != after.global_name:
        changes["Nome de exibição"] = (before.global_name, after.global_name)
    if username_changed:
        changes["Nome de usuário"] = (before.name, after.name)
    if not changes:
        return

    for guild in bot.guilds:
        member = guild.get_member(after.id)
        if member:
            await _observe_member_via_api(member)
            if username_changed:
                try:
                    await reconcileVipMember(guild, member.id)
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "Falha ao reconciliar cargo VIP após mudança de username: guild_id=%s user_id=%s",
                        guild.id,
                        member.id,
                    )
            await logProfileChange(bot, guild, member, changes)



@tasks.loop(hours=12)
async def cronJobs12h():
    await removeTempRoles(bot)

@tasks.loop(minutes=30)
async def cronJobs30m():
    # getServerConfigurations(bot)
    for guild_config in bot.config or []:
        if getattr(guild_config, "hasLevels", None) != False:
            logger.warning(
                'Configurações de níveis não encontradas para guild %s',
                guild_config.guildId,
            )
    
@tasks.loop(hours=1)
async def timeSpecificTasks():
    """Executa verificações que dependem de horários específicos."""
    if now().hour == 8:
        await update_adult_roles(bot)

    if now().hour == 12:
        await sendBirthdayMessages(bot)


@tasks.loop(time=time(hour=4, tzinfo=timezone(timedelta(hours=timezone_offset))))
async def dailyRestart():
    await bot.close()
    os.execv(sys.executable, [sys.executable] + sys.argv)


@tasks.loop(hours=1)
async def communityMembershipDriftWatch():
    observations = await _observe_live_guilds()
    attempted = 0

    for guild in list(bot.guilds):
        observation = observations.get(guild.id)
        if not observation:
            continue

        trigger = reconciliation_trigger_for_observation(
            sync_state=observation.get("membershipSyncState"),
            tracked_present_members=observation.get("trackedPresentMembers"),
            provider_member_count=guild.member_count,
            local_recovery_required=False,
        )
        if _membership_recovery.is_required(guild.id):
            # The dedicated FIFO recovery worker owns locally failed event
            # delivery so drift checks cannot race or starve that queue.
            continue
        if trigger is None:
            continue
        if attempted >= MAX_DRIFT_RECONCILIATIONS_PER_PASS:
            logger.warning(
                "Limite de reconciliações por drift atingido; guild %s ficará para a próxima passagem",
                guild.id,
            )
            continue

        attempted += 1
        await _reconcile_one_guild_logged(guild, trigger)


@communityMembershipDriftWatch.before_loop
async def beforeCommunityMembershipDriftWatch():
    await bot.wait_until_ready()
    await asyncio.sleep(60 * 60)


@tasks.loop(minutes=2)
async def communityMembershipRecoveryWorker():
    for _ in range(MAX_RECOVERY_RECONCILIATIONS_PER_PASS):
        guild_id = _membership_recovery.pop_next()
        if guild_id is None:
            return

        guild = bot.get_guild(guild_id)
        if guild is None:
            _membership_recovery.discard(guild_id)
            continue

        await _reconcile_one_guild_logged(guild, "RECOVERY")


@communityMembershipRecoveryWorker.before_loop
async def beforeCommunityMembershipRecoveryWorker():
    await bot.wait_until_ready()
    await asyncio.sleep(2 * 60)


@tasks.loop(minutes=1)
async def communitySyncQueueWorker():
    client = _lifecycle_client()
    for _ in range(3):
        try:
            run = await client.claim_next_sync()
        except CommunityLifecycleApiError as error:
            logger.exception(
                "Falha ao consultar fila de sincronização de Communities status=%s retryable=%s error=%s",
                error.status,
                error.retryable,
                error,
            )
            return
        if run is None:
            return
        try:
            await reconcile_claimed_run(
                bot,
                client,
                run,
                approved_resolver_factory=_approved_resolver_for_guild,
            )
        except Exception:
            logger.exception(
                "Falha ao executar sincronização solicitada da guild %s (run=%s)",
                run.guild_id,
                run.run_id,
            )


@communitySyncQueueWorker.before_loop
async def beforeCommunitySyncQueueWorker():
    await bot.wait_until_ready()
    await _membership_startup_complete.wait()


@tasks.loop(minutes=1)
async def bumpWarning():
    bump_interval = timedelta(hours=2)
    current_time = now()
    configs = listBumpWarningConfigs()
    from core.messages import bump as default_bump_messages

    for config in configs:
        next_at = config.get("nextAt")
        if not next_at or current_time < next_at:
            continue

        guild = bot.get_guild(config["guildId"])
        if guild is None:
            continue

        target_channel_id = config.get("targetChannelId")
        if not target_channel_id:
            continue

        target_channel = guild.get_channel(target_channel_id)
        if target_channel is None:
            continue

        async with _bump_warning_lock(guild.id):
            persisted_config = getBumpConfig(guild.id)
            persisted_next_at = persisted_config.get("warnNextAt")
            if (
                not persisted_config.get("warnEnabled")
                or persisted_next_at != next_at
                or (persisted_next_at and current_time < persisted_next_at)
            ):
                continue

            allowed_processing_channels = get_allowed_feature_channels(
                config["guildId"], "disboard_bump_processing"
            )
            if len(allowed_processing_channels) > 1:
                logger.warning(
                    'Mais de um Canal de Bump configurado na guild %s; usando deterministicamente o primeiro (%s)',
                    config["guildId"],
                    allowed_processing_channels[0],
                )
            disboard_channel_id = resolve_realtime_bump_channel_id(
                allowed_processing_channels,
                config.get("disboardChannelId"),
            )

            last_message = None
            if disboard_channel_id:
                disboard_channel = guild.get_channel(disboard_channel_id)
                if disboard_channel is None:
                    continue
                async for msg in disboard_channel.history(limit=20):
                    if msg.author.id == DISBOARD_BOT_ID:
                        last_message = msg
                        break
                if last_message is None:
                    continue

            messages = config.get("messages") or default_bump_messages
            selected_message = messages[int(random.random() * len(messages))]
            jump_url = f" {last_message.jump_url}" if last_message is not None else ""
            try:
                await target_channel.send(
                    f"{selected_message}{jump_url}"
                )
            except Exception:
                logger.exception('Falha ao enviar aviso de bump na guild %s', guild.id)
                continue

            latest_config = getBumpConfig(guild.id)
            if not latest_config.get("warnEnabled") or latest_config.get("warnNextAt") != next_at:
                continue
            schedule_anchor = max(current_time, next_at)
            setBumpWarningSchedule(guild.id, next_at=schedule_anchor + bump_interval)

@tasks.loop(hours=6) #0.16   10 minutos
async def bumpReward():
    await run_monthly_reward_cycle(bot)


@bot.tree.command(name=f'testes', description=f'teste')
async def test(ctx: discord.Interaction):
    pass

def run_discord_client(chatBot):
    global status_api, community_lifecycle_client
    bot.chatBot = chatBot
    configure_application_logging()
    configure_discord_logging()
    load_dotenv()
    community_lifecycle_client = CommunityLifecycleApiClient()
    token = os.getenv('DISCORD_TOKEN')
    status_port = os.getenv('BOT_STATUS_API_PORT')
    status_host = os.getenv('BOT_STATUS_API_HOST', '127.0.0.1')
    status_token = os.getenv('BOT_STATUS_API_TOKEN')

    if status_port and status_port.strip():
        status_api = BotStatusApi(
            bot,
            host=status_host,
            port=int(status_port),
            token=status_token,
            initialized_getter=lambda: initialized,
        )
        bot.add_startup_callback(status_api.start)
        bot.add_shutdown_callback(status_api.stop)

    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN não encontrado. Defina a variável no ambiente ou no arquivo .env antes de iniciar o bot."
        )

    asyncio.run(run_discord_bot(bot, token))
