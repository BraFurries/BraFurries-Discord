from colormath.color_objects import sRGBColor, LabColor
from colormath.color_conversions import convert_color
from colormath.color_diff import _get_lab_color1_vector, _get_lab_color2_matrix
from colormath import color_diff_matrix
from core.AI_Functions.terceiras.openAI import CasualAiResult, OPENAI_TOKEN_INVALID_MARKER, retornaRespostaGPT
import discord, asyncio, re, random, math, logging, weakref
from discord.ext import commands
from core.database import *
from core.temp_role_locks import temp_role_lock
from core.levels import level_from_total_xp
from core.xp_policy import XpPolicy
from core.runtime_config import get_optional_discord_snowflake
from schemas.models.bot import Config
from schemas.models.bot import MyBot
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

_vip_custom_role_locks: weakref.WeakValueDictionary[tuple[int, int], asyncio.Lock] = weakref.WeakValueDictionary()
_vip_reconcile_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
_vip_role_position_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()


def _vip_reconcile_lock(guild_id: int) -> asyncio.Lock:
    key = int(guild_id)
    lock = _vip_reconcile_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _vip_reconcile_locks[key] = lock
    return lock


def _vip_custom_role_lock(guild_id: int, user_id: int) -> asyncio.Lock:
    key = (int(guild_id), int(user_id))
    lock = _vip_custom_role_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _vip_custom_role_locks[key] = lock
    return lock


def _vip_role_position_lock(guild_id: int) -> asyncio.Lock:
    key = int(guild_id)
    lock = _vip_role_position_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _vip_role_position_locks[key] = lock
    return lock


def shouldReconcileVipRoleUpdate(before: discord.Role, after: discord.Role) -> bool:
    """Return whether a role-update event represents VIP metadata drift.

    Position-only updates are intentionally ignored. VIP reconciliation itself
    can move roles and Discord emits role-update events for positions shifted by
    that move; feeding those events back into reconciliation creates a
    self-sustaining PATCH /guilds/{guild_id}/roles storm.
    """

    return before.name != after.name


timezone_offset = -3.0  # Pacific Standard Time (UTC−08:00)
def now() -> datetime: return (datetime.now(timezone(timedelta(hours=timezone_offset)))).replace(tzinfo=None)

ADULT_ROLE_ID = get_optional_discord_snowflake("CODDY_LEGACY_ADULT_ROLE_ID") or 0
MINOR_ROLE_ID = get_optional_discord_snowflake("CODDY_LEGACY_MINOR_ROLE_ID") or 0
BIRTHDAY_CHANNEL_DEFAULT_ID = get_optional_discord_snowflake("CODDY_LEGACY_BIRTHDAY_CHANNEL_ID") or 0
BIRTHDAY_ROLE_ID = get_optional_discord_snowflake("CODDY_LEGACY_BIRTHDAY_ROLE_ID")
CREATOR_DISCORD_ID = get_optional_discord_snowflake("CODDY_CREATOR_DISCORD_ID")
XP_BLOCKED_CHANNELS_FEATURE_KEY = "xp_blocked_channels"
XP_MESSAGE_BASE_MIN = 8
XP_MESSAGE_BASE_MAX = 16
XP_ANTI_FARM_MIN_SECONDS = 30
XP_ANTI_FARM_MAX_SECONDS = 60
XP_FLUSH_INTERVAL_SECONDS = 5

_xp_anti_farm_bucket: dict[tuple[int, int, int], float] = {}
_xp_pending_buffer: dict[tuple[int, int], int] = {}
_xp_member_cache: dict[tuple[int, int], discord.Member] = {}
_xp_origin_channel_cache: dict[tuple[int, int], discord.abc.Messageable] = {}
_xp_buffer_lock = asyncio.Lock()
_xp_flush_task: asyncio.Task | None = None

async def _collect_text_xp_context(
    message: discord.Message,
    level_config: dict | None = None,
) -> tuple[int, dict, int, int]:
    """Collect text XP DB context using async pool."""
    level_config = level_config or await async_get_level_config(message.guild.id)
    user_id = await async_includeUser(message.author, message.guild.id)
    async with async_pooled_connection() as cursor:
        await cursor.execute("""
            INSERT INTO user_level (
                user_id, server_guild_id, total_xp, current_level,
                xp_awarded_today, xp_awarded_day,
                xp_awarded_text_today, xp_awarded_text_day
            )
            VALUES (%s, %s, 0, 0, 0, UTC_DATE(), 0, UTC_DATE())
            ON DUPLICATE KEY UPDATE user_id = VALUES(user_id)
        """, (user_id, message.guild.id))
        await cursor.execute("""
            UPDATE user_level
            SET xp_awarded_today = CASE WHEN xp_awarded_day = UTC_DATE() THEN xp_awarded_today ELSE 0 END,
                xp_awarded_day = UTC_DATE(),
                xp_awarded_text_today = CASE WHEN xp_awarded_text_day = UTC_DATE() THEN xp_awarded_text_today ELSE 0 END,
                xp_awarded_text_day = UTC_DATE()
            WHERE user_id = %s AND server_guild_id = %s
        """, (user_id, message.guild.id))
        await cursor.execute(
            "SELECT xp_awarded_today, xp_awarded_text_today FROM user_level WHERE user_id = %s AND server_guild_id = %s LIMIT 1",
            (user_id, message.guild.id),
        )
        row = await cursor.fetchone() or {}

    today_total = int(row.get("xp_awarded_today") or 0)
    today_text = int(row.get("xp_awarded_text_today") or 0)
    return user_id, level_config, today_total, today_text

def create_google_maps_url(address: str) -> str:
    """Creates a Google Maps search URL for the given address."""
    encoded_address = quote_plus(address)
    return f"https://www.google.com/maps/search/?api=1&query={encoded_address}"

def sanitize_mentions(content: str) -> str:
    content = re.sub(r'@everyone', '@​everyone', content, flags=re.IGNORECASE)
    content = re.sub(r'@here', '@​here', content, flags=re.IGNORECASE)
    content = re.sub(r'<@&\d+>', '', content)
    return content


async def _notify_invalid_ai_token(message: discord.Message) -> None:
    if not message.guild:
        return
    warning = (
        "⚠️ O token OpenAI configurado para este servidor não está funcional. "
        "Adicione um novo token ou desative as respostas por IA."
    )
    if isinstance(message.author, discord.Member) and message.author.guild_permissions.administrator:
        try:
            await message.author.send(warning)
            return
        except discord.HTTPException:
            pass
    try:
        await message.guild.owner.send(warning)
    except discord.HTTPException:
        logging.warning("Não foi possível notificar o dono do servidor sobre token OpenAI inválido.")


async def _resolve_casual_ai_result(result, message: discord.Message) -> str:
    """Preserve legacy token handling while allowing casual fallback metadata."""

    if isinstance(result, CasualAiResult):
        if result.openai_token_invalid:
            await _notify_invalid_ai_token(message)
        response = result.text
    else:
        response = result

    if response == OPENAI_TOKEN_INVALID_MARKER:
        if not isinstance(result, CasualAiResult):
            await _notify_invalid_ai_token(message)
        return (
            "Token OpenAI inválido neste servidor. "
            "Peça para um admin atualizar o token ou desativar respostas por IA."
        )
    return response


def getServerLevelConfig(guild: discord.Guild | int):
    guild_id = guild.id if isinstance(guild, discord.Guild) else guild
    if guild_id is None:
        raise ValueError("guild_id é obrigatório para recuperar level config.")
    levelConfig = getLevelConfig(guild_id)
    return levelConfig

def getVIPConfigurations(guild):
    vip_division_config = getVipRoleDivisionConfig(guild.id)
    start_role_id = vip_division_config.get('startRoleId')
    end_role_id = vip_division_config.get('endRoleId')
    vip_role_ids = getGuildVipRoleIds(guild.id)

    VIPStatus = {'VIPRoles': [], 'hasVIPCustomization': True, 'hasRoleDivision': bool(start_role_id),
                    'VIPRoleDivisionStartID': start_role_id, 'VIPRoleDivisionEndID': end_role_id}
    VIPRoles = []
    for role_id in vip_role_ids:
        vip_role = discord.utils.get(guild.roles, id=role_id)
        if vip_role is not None:
            VIPRoles.append(vip_role)
    VIPStatus['VIPRoles'] = VIPRoles
    return VIPStatus

def getVIPMembers(guild):
    VIPConfig = getVIPConfigurations(guild)
    VIPMembers = []
    for VIPRole in VIPConfig['VIPRoles']:
        if VIPRole is None:
            continue
        for member in VIPRole.members:
            VIPMembers.append(member)
    return VIPMembers

def getMemberInRole(guild, roleID):
    role = discord.utils.get(guild.roles, id=roleID)
    members = []
    for member in role.members:
        members.append(member)
    return members


def getVipCustomRolePrefix(guild: discord.Guild) -> str:
    configured_prefix = getVipCustomRolePrefixConfig(guild.id)
    if configured_prefix:
        return configured_prefix

    return "VIP"

async def rearrangeRoleInsideInterval(guild, roleID, start, end) -> bool:
    async with _vip_role_position_lock(guild.id):
        # Re-resolve the live roles after waiting for the guild-scoped lock so
        # concurrent VIP events cannot act on stale positions.
        role = discord.utils.get(guild.roles, id=roleID)
        start = guild.get_role(start.id) if start is not None else None
        end = guild.get_role(end.id) if end is not None else None
        bot_member = getattr(guild, "me", None)
        permissions = getattr(bot_member, "guild_permissions", None)
        bot_top_role = getattr(bot_member, "top_role", None)
        if role is None or start is None:
            logging.warning(
                "Não foi possível reorganizar cargo VIP: role=%s start=%s end=%s",
                getattr(role, "id", None),
                getattr(start, "id", None),
                getattr(end, "id", None),
            )
            return False
        if not bool(getattr(permissions, "manage_roles", False)) or bot_top_role is None:
            logging.warning("Coddy sem Manage Roles/hierarquia para reposicionar VIP na guild_id=%s", guild.id)
            return False
        if not (bot_top_role > role) or not (bot_top_role > start):
            logging.warning(
                "Faixa VIP fora da hierarquia gerenciável do Coddy: guild_id=%s role_id=%s start_id=%s",
                guild.id,
                role.id,
                start.id,
            )
            return False
        if end is not None and (end.position >= start.position or role == end):
            logging.warning(
                "Faixa VIP inválida: guild_id=%s start=%s end=%s",
                guild.id,
                start.id,
                end.id,
            )
            return False

        target_position = start.position - 1
        if end is None:
            if role.position != target_position:
                await guild.edit_role_positions(positions={role: target_position})
            return True

        if not (role.position > end.position and role.position < start.position):
            await guild.edit_role_positions(positions={role: target_position})
        return True

async def getLocalId(locale):
    availableLocals = getAllLocals()
    if locale.upper() in [local_dict['locale_abbrev'] for local_dict in availableLocals]:
        #pegaremos o id do local
        locale_id = [local_dict['id'] for local_dict in availableLocals if local_dict['locale_abbrev'] == locale.upper()][0]
        return locale_id
    else:
        raise ValueError(f'O local {locale} não está disponível. Por favor, escolha um local válido.')
    
def hex_to_rgb(hex_color):
    return sRGBColor.new_from_rgb_hex(hex_color)
    
async def colorIsAvailable(color:str, guild: discord.Guild):
    if guild is None:
        raise ValueError("guild é obrigatório para validar cor de cargo VIP")

    if getVipAllowStaffColorsConfig(guild.id):
        return True

    staff_colors: list[str] = []

    configured_staff_colors = getGuildStaffColors(guild.id)
    for configured_color in configured_staff_colors:
        normalized = str(configured_color).strip().lower()
        if not normalized:
            continue
        if not normalized.startswith("#"):
            normalized = f"#{normalized}"
        if re.fullmatch(r"#[0-9a-f]{6}", normalized) and normalized not in staff_colors:
            staff_colors.append(normalized)

    staff_role_ids = getStaffRoles(guild.id)
    for role_id in staff_role_ids:
        role = guild.get_role(int(role_id))
        if role is None or role.color.value == 0:
            continue
        role_color = f"#{role.color.value:06x}"
        if role_color not in staff_colors:
            staff_colors.append(role_color)

    if not staff_colors:
        return True

    for staff_color in staff_colors:
        color1_rgb = hex_to_rgb(color)
        color2_rgb = hex_to_rgb(staff_color)

        color1_lab = convert_color(color1_rgb, LabColor)
        color2_lab = convert_color(color2_rgb, LabColor)
        color1_vector = _get_lab_color1_vector(color1_lab)
        color2_matrix = _get_lab_color2_matrix(color2_lab)
        delta_e = color_diff_matrix.delta_e_cie2000(
            color1_vector, color2_matrix, Kl=1, Kc=1, Kh=1)[0]
        
        color_distance = delta_e.item()

        if color_distance < 11.0:
            return False
    return True


def _vip_member_has_access(member: discord.Member, vip_role_ids: set[int]) -> bool:
    return any(role.id in vip_role_ids for role in getattr(member, "roles", ()))


def _legacy_vip_role_candidates(
    guild: discord.Guild,
    member: discord.Member,
    current_prefix: str,
    vip_role_ids: set[int] | None = None,
) -> list[discord.Role]:
    """Recover a legacy custom role without treating its prefix as identity.

    Prefer the exact current/default legacy names. When the prefix was changed
    before role_id was persisted, accept only a uniquely member-owned,
    non-managed role whose name ends with the member name. This keeps recovery
    bounded to an observable Discord relationship instead of guessing by a
    historical prefix we no longer know.
    """

    names = {
        f"{current_prefix} {member.name}".casefold(),
        f"VIP {member.name}".casefold(),
    }
    exact = [
        role
        for role in guild.roles
        if getattr(role, "name", "").casefold() in names
    ]
    if exact:
        return exact

    excluded_ids = vip_role_ids or set()
    default_role = getattr(guild, "default_role", None)
    member_suffix = f" {member.name}".casefold()
    candidates = []
    for role in getattr(member, "roles", ()) or ():
        if role == default_role or getattr(role, "managed", False) or role.id in excluded_ids:
            continue
        if not getattr(role, "name", "").casefold().endswith(member_suffix):
            continue
        role_members = list(getattr(role, "members", ()) or ())
        if len(role_members) != 1 or role_members[0].id != member.id:
            continue
        candidates.append(role)
    return candidates


async def _position_vip_role(
    guild: discord.Guild,
    role: discord.Role,
    vip_config: dict | None = None,
) -> bool:
    vip_config = vip_config or getVIPConfigurations(guild)
    start_id = vip_config["VIPRoleDivisionStartID"]
    if start_id is None:
        return True
    start = guild.get_role(start_id)
    end_id = vip_config["VIPRoleDivisionEndID"]
    end = guild.get_role(end_id) if end_id is not None else None
    return await rearrangeRoleInsideInterval(guild, role.id, start, end)


async def _ensureVipCustomRoleUnlocked(ctx:discord.Interaction) -> discord.Role:
    guild = ctx.guild
    member = ctx.user
    if guild is None:
        raise ValueError("guild é obrigatória para criar cargo VIP")

    custom_role_prefix = getVipCustomRolePrefix(guild)
    entry = getCustomRoleEntry(guild.id, member.id)
    custom_role = None
    live_roles_by_id: dict[int, discord.Role] | None = None

    async def get_live_roles_by_id() -> dict[int, discord.Role]:
        nonlocal live_roles_by_id
        if live_roles_by_id is None:
            # guild.get_role()/guild.roles are Gateway cache views and can still
            # contain a role that Discord has already deleted. The REST list is
            # authoritative before we reuse any persisted/legacy role.
            live_roles_by_id = {
                int(role.id): role
                for role in await guild.fetch_roles()
            }
        return live_roles_by_id

    entry_owner_id = _custom_role_entry_owner_id(entry)
    if entry_owner_id is not None and entry_owner_id != member.id:
        raise RuntimeError(
            f"vip_custom_role_owned_by_linked_account:{entry_owner_id}"
        )

    if entry and entry.get("role_id"):
        persisted_role_id = int(entry["role_id"])
        custom_role = (await get_live_roles_by_id()).get(persisted_role_id)

        if custom_role is not None and entry_owner_id is None:
            proven_owner_id = await _claim_proven_legacy_custom_role_owner(
                entry,
                custom_role,
            )
            if proven_owner_id is None:
                raise RuntimeError(
                    f"ambiguous_vip_custom_role_owner:{member.id}"
                )
            if proven_owner_id != member.id:
                raise RuntimeError(
                    f"vip_custom_role_owned_by_linked_account:{proven_owner_id}"
                )
            entry_owner_id = proven_owner_id

        if custom_role is None:
            linked_ids = _custom_role_entry_linked_discord_ids(entry)
            if entry_owner_id is None and (
                len(linked_ids) != 1 or member.id not in linked_ids
            ):
                raise RuntimeError(
                    f"ambiguous_vip_custom_role_owner:{member.id}"
                )
            if entry_owner_id is None:
                row_id = _custom_role_entry_row_id(entry)
                if row_id is not None:
                    claimed = await async_claimCustomRoleOwner(row_id, member.id)
                    if not claimed:
                        raise RuntimeError(
                            f"ambiguous_vip_custom_role_owner:{member.id}"
                        )
                entry_owner_id = member.id

            logger.warning(
                "Cargo VIP persistido não existe mais no Discord; limpando vínculo stale: guild_id=%s user_id=%s role_id=%s",
                guild.id,
                member.id,
                persisted_role_id,
            )
            clearCustomRoleRoleId(guild.id, member.id)

    if custom_role is None:
        vip_role_ids = {
            role.id for role in getVIPConfigurations(guild)["VIPRoles"]
        }
        candidates = _legacy_vip_role_candidates(
            guild,
            member,
            custom_role_prefix,
            vip_role_ids,
        )
        if candidates:
            live_roles = await get_live_roles_by_id()
            candidates = [
                live_roles[candidate.id]
                for candidate in candidates
                if candidate.id in live_roles
            ]

        if len(candidates) > 1:
            raise RuntimeError(f"ambiguous_vip_custom_role:{member.id}")
        if candidates:
            custom_role = candidates[0]
            saveCustomRole(guild.id, member, roleId=custom_role.id)

    async def create_custom_role() -> discord.Role:
        logger.info(
            "Cargo VIP personalizado não encontrado; criando para user_id=%s",
            member.id,
        )
        role = await guild.create_role(
            name=f"{custom_role_prefix} {member.name}",
            mentionable=False,
            reason="Cargo criado para membros VIPs",
        )
        saveCustomRole(guild.id, member, roleId=role.id)
        return role

    async def prepare_custom_role(role: discord.Role) -> discord.Role:
        if role not in member.roles:
            await member.add_roles(role)

        expected_name = f"{custom_role_prefix} {member.name}"
        if role.name != expected_name:
            await role.edit(
                name=expected_name,
                reason="Reconciliação do prefixo VIP",
            )

        await _position_vip_role(guild, role)
        saveCustomRole(guild.id, member, roleId=role.id)
        return role

    if custom_role is None:
        custom_role = await create_custom_role()

    try:
        return await prepare_custom_role(custom_role)
    except discord.NotFound as error:
        if getattr(error, "code", None) != 10011:
            raise

        # The role can disappear after fetch_roles() but before add/edit/move
        # (Gateway delay, another process or manual deletion). Heal once instead
        # of sending the stale ID further into /vip customizar.
        logger.warning(
            "Cargo VIP desapareceu durante a preparação; recriando uma vez: guild_id=%s user_id=%s role_id=%s",
            guild.id,
            member.id,
            custom_role.id,
        )
        clearCustomRoleRoleId(guild.id, member.id)
        replacement = await create_custom_role()
        return await prepare_custom_role(replacement)


@asynccontextmanager
async def vipCustomRoleMutation(ctx: discord.Interaction):
    """Hold the per-member VIP role lock for one complete customization operation.

    The role is ensured while the lock is held, and callers may safely mutate
    the returned role before reconciliation can inspect transient default state.
    """
    guild = ctx.guild
    member = ctx.user
    if guild is None:
        raise ValueError("guild é obrigatória para criar cargo VIP")

    async with _vip_custom_role_lock(guild.id, member.id):
        yield await _ensureVipCustomRoleUnlocked(ctx)


async def addVipRole(ctx:discord.Interaction) -> discord.Role:
    async with vipCustomRoleMutation(ctx) as custom_role:
        return custom_role


def _new_vip_reconcile_summary() -> dict:
    return {
        "processed": 0,
        "updated": 0,
        "deleted": 0,
        "recovered": 0,
        "warnings": [],
    }


def _custom_role_entry_role_id(entry) -> int | None:
    if entry is None:
        return None
    value = entry.get("role_id") if isinstance(entry, dict) else getattr(entry, "roleId", None)
    return int(value) if value else None


def _custom_role_entry_owner_id(entry, fallback_user_id: int | None = None) -> int | None:
    if entry is None:
        return fallback_user_id
    if isinstance(entry, dict):
        if "owner_discord_user_id" in entry:
            value = entry.get("owner_discord_user_id")
            return int(value) if value is not None else None
        value = entry.get("discord_user_id") or entry.get("userId")
        return int(value) if value is not None else fallback_user_id
    if hasattr(entry, "ownerDiscordUserId"):
        value = getattr(entry, "ownerDiscordUserId")
        return int(value) if value is not None else None
    value = getattr(entry, "userId", None)
    return int(value) if value is not None else fallback_user_id


def _custom_role_entry_row_id(entry) -> int | None:
    if entry is None:
        return None
    value = entry.get("id") if isinstance(entry, dict) else getattr(entry, "rowId", None)
    return int(value) if value else None


def _custom_role_entry_linked_discord_ids(entry) -> set[int]:
    if entry is None:
        return set()
    raw = (
        entry.get("linked_discord_user_ids")
        if isinstance(entry, dict)
        else getattr(entry, "linkedDiscordUserIds", [])
    ) or []
    if isinstance(raw, str):
        raw = [item for item in raw.split(",") if item]
    return {int(value) for value in raw if value is not None}


VIP_DEFAULT_ROLE_CLEANUP_GRACE = timedelta(minutes=5)


def _custom_role_entry_has_persisted_customization(entry) -> bool:
    if entry is None:
        return False
    if isinstance(entry, dict):
        values = (
            entry.get("color"),
            entry.get("color2"),
            entry.get("icon_id"),
        )
    else:
        values = (
            getattr(entry, "color", None),
            getattr(entry, "color2", None),
            getattr(entry, "iconId", None),
        )
    return any(value is not None and value != "" for value in values)


def _should_delete_default_vip_role(
    role: discord.Role,
    entry,
    *,
    reference_time: datetime | None = None,
) -> bool:
    if role.color != discord.Color.default() or getattr(role, "display_icon", None) is not None:
        return False

    # Raw role-color PATCHes can succeed before discord.py receives the matching
    # GUILD_ROLE_UPDATE, so the local role cache may still look default. A
    # persisted customization is stronger evidence than that stale cache.
    if _custom_role_entry_has_persisted_customization(entry):
        return False

    # A newly-created VIP role is legitimately default while /vip customizar is
    # still applying its first color/icon. Do not let another process/session
    # cleanup delete that transient state.
    created_at = getattr(role, "created_at", None)
    if not isinstance(created_at, datetime):
        return False

    now_utc = reference_time or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    return now_utc - created_at >= VIP_DEFAULT_ROLE_CLEANUP_GRACE


def _resolve_legacy_custom_role_owner(entry, role: discord.Role | None) -> int | None:
    if role is None:
        return None
    linked_ids = _custom_role_entry_linked_discord_ids(entry)
    if not linked_ids:
        return None
    assigned_linked_ids = {
        int(member.id)
        for member in list(role.members)
        if int(member.id) in linked_ids
    }
    if len(assigned_linked_ids) != 1:
        return None
    return next(iter(assigned_linked_ids))


async def _claim_proven_legacy_custom_role_owner(entry, role: discord.Role) -> int | None:
    owner_id = _resolve_legacy_custom_role_owner(entry, role)
    if owner_id is None:
        return None

    row_id = _custom_role_entry_row_id(entry)
    if row_id is not None:
        claimed = await async_claimCustomRoleOwner(row_id, owner_id)
        if not claimed:
            return None

    if isinstance(entry, dict):
        entry["owner_discord_user_id"] = owner_id
    else:
        entry.userId = owner_id
        if hasattr(entry, "ownerDiscordUserId"):
            entry.ownerDiscordUserId = owner_id
    return owner_id


async def getVipCustomRoleOwnerIds(guild_id: int, role_ids: set[int]) -> set[int]:
    """Resolve custom-role owners with a targeted indexed async database lookup."""

    return await async_getCustomRoleOwnerDiscordIdsByRoleIds(guild_id, role_ids)


async def removeVipCustomRoleForMember(
    guild: discord.Guild,
    discord_user_id: int,
    *,
    reason: str = "Membro saiu do servidor",
) -> bool:
    """Delete one persisted VIP custom role without scanning guild members/roles."""

    async with _vip_custom_role_lock(guild.id, discord_user_id):
        entry = getCustomRoleEntry(guild.id, discord_user_id)
        role_id = _custom_role_entry_role_id(entry)
        if role_id is None:
            return False

        owner_id = _custom_role_entry_owner_id(
            entry,
            fallback_user_id=discord_user_id,
        )
        if owner_id is None or owner_id != discord_user_id:
            # Legacy rows or rows owned by another linked Discord account are
            # not safe to mutate from this member-removal event.
            return False

        role = guild.get_role(role_id)
        if role is not None:
            await role.delete(reason=reason)
        clearCustomRoleRoleId(guild.id, discord_user_id)
        return True


async def _reconcile_vip_member_unlocked(
    guild: discord.Guild,
    discord_user_id: int,
    *,
    member: discord.Member | None,
    entry,
    custom_role_prefix: str,
    vip_role_ids: set[int],
    vip_config: dict | None = None,
) -> tuple[dict, int | None]:
    summary = _new_vip_reconcile_summary()
    role_id = _custom_role_entry_role_id(entry)
    owner_id = _custom_role_entry_owner_id(
        entry,
        fallback_user_id=discord_user_id if entry is None else None,
    )

    if owner_id is not None and owner_id != discord_user_id:
        summary["warnings"].append(f"owner_mismatch:{discord_user_id}")
        return summary, role_id

    # A member with no persisted custom role only needs legacy recovery when
    # they currently have VIP access. Never create a role from reconciliation.
    if entry is None and (member is None or not _vip_member_has_access(member, vip_role_ids)):
        return summary, None

    summary["processed"] = 1
    role = guild.get_role(role_id) if role_id else None

    if entry is not None and owner_id is None and role is not None:
        proven_owner_id = await _claim_proven_legacy_custom_role_owner(
            entry,
            role,
        )
        if proven_owner_id is None:
            summary["warnings"].append(f"legacy_owner_ambiguous:{discord_user_id}")
            return summary, role_id
        if proven_owner_id != discord_user_id:
            summary["warnings"].append(f"owner_mismatch:{discord_user_id}")
            return summary, role_id
        owner_id = proven_owner_id

    if role is None and member is not None and _vip_member_has_access(member, vip_role_ids):
        candidates = _legacy_vip_role_candidates(
            guild,
            member,
            custom_role_prefix,
            vip_role_ids,
        )
        if len(candidates) == 1:
            role = candidates[0]
            role_id = role.id
            saveCustomRole(guild.id, member, roleId=role.id)
            owner_id = discord_user_id
            summary["recovered"] += 1
        elif len(candidates) > 1:
            summary["warnings"].append(f"ambiguous_role:{discord_user_id}")
            return summary, None

    if role is None:
        if role_id:
            if entry is not None and _custom_role_entry_owner_id(entry) is None:
                summary["warnings"].append(f"legacy_owner_ambiguous:{discord_user_id}")
                return summary, role_id
            clearCustomRoleRoleId(guild.id, discord_user_id)
            summary["warnings"].append(f"role_missing:{discord_user_id}")
        return summary, None

    if _should_delete_default_vip_role(role, entry):
        await role.delete(reason="Reconciliação VIP: cargo sem customização")
        clearCustomRoleRoleId(guild.id, discord_user_id)
        summary["deleted"] += 1
        return summary, None

    has_vip = member is not None and _vip_member_has_access(member, vip_role_ids)
    if not has_vip:
        for assigned_member in list(role.members):
            await assigned_member.remove_roles(role, reason="Reconciliação VIP: acesso expirado")
        if not role.members:
            await role.delete(reason="Reconciliação VIP: membro sem VIP")
            summary["deleted"] += 1
        clearCustomRoleRoleId(guild.id, discord_user_id)
        return summary, None

    for extra in list(role.members):
        if extra.id != member.id:
            await extra.remove_roles(role, reason="Reconciliação VIP: vínculo exclusivo")
            summary["updated"] += 1
    if role not in member.roles:
        await member.add_roles(role, reason="Reconciliação VIP")
        summary["updated"] += 1

    expected_name = f"{custom_role_prefix} {member.name}"
    if role.name != expected_name:
        await role.edit(name=expected_name, reason="Reconciliação do prefixo VIP")
        summary["updated"] += 1

    await _position_vip_role(guild, role, vip_config)

    if role.color == discord.Color.default():
        # Do not overwrite persisted customization with a transient stale cache
        # value after the raw Discord color PATCH.
        saveCustomRole(guild.id, member, roleId=role.id)
    else:
        hex_color = '#%02x%02x%02x' % (role.color.r, role.color.g, role.color.b)
        saveCustomRole(guild.id, member, color=hex_color, roleId=role.id)
    return summary, role.id


async def reconcileVipMember(guild: discord.Guild, discord_user_id: int) -> dict:
    """Reconcile one VIP member from a Discord event without a guild-wide scan."""

    custom_role_prefix = getVipCustomRolePrefix(guild)
    vip_config = getVIPConfigurations(guild)
    vip_role_ids = {role.id for role in vip_config["VIPRoles"]}
    summary = _new_vip_reconcile_summary()
    if not vip_role_ids:
        summary["warnings"].append("vip_roles_unavailable")
        return summary

    async with _vip_custom_role_lock(guild.id, discord_user_id):
        member = guild.get_member(discord_user_id)
        entry = getCustomRoleEntry(guild.id, discord_user_id)
        summary, _ = await _reconcile_vip_member_unlocked(
            guild,
            discord_user_id,
            member=member,
            entry=entry,
            custom_role_prefix=custom_role_prefix,
            vip_role_ids=vip_role_ids,
            vip_config=vip_config,
        )
        return summary


async def recoverVipCustomRolesAfterSessionReset(
    guild: discord.Guild,
    entries: list[CustomRole],
) -> dict:
    """Catch up persisted VIP custom roles after a fresh Gateway session.

    Recovery is bounded by persisted custom-role rows. Legacy rows without an
    account owner are claimed only when the live role has exactly one linked
    Discord account assigned; ambiguous rows are skipped without mutation.
    """

    summary = _new_vip_reconcile_summary()
    if not entries:
        return summary

    async with _vip_reconcile_lock(guild.id):
        configs = await async_getVipRecoveryConfigsForGuildIds({guild.id})
        raw_config = configs.get(int(guild.id))
        configured_role_ids = set(raw_config.get("roleIds", ())) if raw_config else set()
        vip_roles = [
            role
            for role_id in configured_role_ids
            if (role := guild.get_role(role_id)) is not None
        ]
        vip_role_ids = {role.id for role in vip_roles}
        if not vip_role_ids:
            summary["warnings"].append("vip_roles_unavailable")
            return summary

        vip_config = {
            "VIPRoles": vip_roles,
            "hasVIPCustomization": True,
            "hasRoleDivision": bool(raw_config.get("startRoleId")),
            "VIPRoleDivisionStartID": raw_config.get("startRoleId"),
            "VIPRoleDivisionEndID": raw_config.get("endRoleId"),
        }
        custom_role_prefix = raw_config.get("customRolePrefix") or "VIP"

        for index, entry in enumerate(entries, start=1):
            role_id = _custom_role_entry_role_id(entry)
            row_id = _custom_role_entry_row_id(entry)
            owner_id = _custom_role_entry_owner_id(entry)
            role = guild.get_role(role_id) if role_id else None

            if role is None:
                if row_id is not None:
                    await async_clearCustomRoleRoleIdById(row_id)
                elif owner_id is not None:
                    await async_clearCustomRoleRoleId(guild.id, owner_id)
                summary["warnings"].append(f"role_missing:{role_id or 'unknown'}")
                continue

            if owner_id is None:
                owner_id = _resolve_legacy_custom_role_owner(entry, role)
                if owner_id is None:
                    summary["warnings"].append(f"legacy_owner_ambiguous:{role.id}")
                    continue

                if row_id is not None:
                    claimed = await async_claimCustomRoleOwner(row_id, owner_id)
                    if not claimed:
                        summary["warnings"].append(f"owner_claim_failed:{role.id}")
                        continue

                entry.userId = owner_id
                if hasattr(entry, "ownerDiscordUserId"):
                    entry.ownerDiscordUserId = owner_id

            discord_user_id = int(owner_id)
            summary["processed"] += 1

            async with _vip_custom_role_lock(guild.id, discord_user_id):
                member = guild.get_member(discord_user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(discord_user_id)
                    except discord.NotFound:
                        member = None
                    except (discord.Forbidden, discord.HTTPException):
                        summary["warnings"].append(
                            f"member_unavailable:{discord_user_id}"
                        )
                        continue

                if _should_delete_default_vip_role(role, entry):
                    try:
                        await role.delete(
                            reason="Recuperação VIP: cargo sem customização"
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        summary["warnings"].append(
                            f"role_delete_failed:{discord_user_id}"
                        )
                        continue
                    await async_clearCustomRoleRoleId(guild.id, discord_user_id)
                    summary["deleted"] += 1
                    continue

                if member is None or not _vip_member_has_access(member, vip_role_ids):
                    try:
                        for assigned_member in list(role.members):
                            await assigned_member.remove_roles(
                                role,
                                reason="Recuperação VIP: acesso expirado durante offline",
                            )
                        await role.delete(
                            reason="Recuperação VIP: acesso expirado durante offline"
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        summary["warnings"].append(
                            f"role_delete_failed:{discord_user_id}"
                        )
                        continue
                    await async_clearCustomRoleRoleId(guild.id, discord_user_id)
                    summary["deleted"] += 1
                    continue

                for extra in list(role.members):
                    if extra.id == member.id:
                        continue
                    try:
                        await extra.remove_roles(
                            role,
                            reason="Recuperação VIP: vínculo exclusivo",
                        )
                        summary["updated"] += 1
                    except (discord.Forbidden, discord.HTTPException):
                        summary["warnings"].append(
                            f"extra_member_cleanup_failed:{extra.id}"
                        )

                if role not in member.roles:
                    try:
                        await member.add_roles(
                            role,
                            reason="Recuperação VIP após nova sessão",
                        )
                        summary["updated"] += 1
                    except (discord.Forbidden, discord.HTTPException):
                        summary["warnings"].append(
                            f"role_assign_failed:{discord_user_id}"
                        )

                expected_name = f"{custom_role_prefix} {member.name}"
                if role.name != expected_name:
                    try:
                        await role.edit(
                            name=expected_name,
                            reason="Recuperação do prefixo VIP",
                        )
                        summary["updated"] += 1
                    except (discord.Forbidden, discord.HTTPException):
                        summary["warnings"].append(
                            f"role_rename_failed:{discord_user_id}"
                        )

                try:
                    await _position_vip_role(guild, role, vip_config)
                except (discord.Forbidden, discord.HTTPException):
                    summary["warnings"].append(
                        f"role_position_failed:{discord_user_id}"
                    )

            if index % 25 == 0:
                await asyncio.sleep(0)

    return summary


async def reconcileVipCustomRoles(guild: discord.Guild) -> dict:
    """Serialize an explicit full VIP reconciliation per guild."""

    async with _vip_reconcile_lock(guild.id):
        return await _reconcileVipCustomRolesUnlocked(guild)


async def _reconcileVipCustomRolesUnlocked(guild: discord.Guild) -> dict:
    """Full reconciliation for explicit config changes or administrative repair."""

    custom_role_prefix = getVipCustomRolePrefix(guild)
    vip_role_ids = {role.id for role in getVIPConfigurations(guild)["VIPRoles"]}
    summary = _new_vip_reconcile_summary()

    # An empty resolved set is ambiguous: VIP may genuinely be disabled, but
    # it can also mean schema/config drift or deleted/unavailable grant roles.
    # Never interpret that state as mass VIP expiration.
    if not vip_role_ids:
        summary["warnings"].append("vip_roles_unavailable")
        return summary

    entries = getAllCustomRoles(guild.id) or []
    known_role_ids: set[int] = set()

    for entry in entries:
        role_id = _custom_role_entry_role_id(entry)
        owner_id = _custom_role_entry_owner_id(entry)

        # Any role_id already persisted belongs to this row even when legacy
        # ownership is ambiguous. Never let the fallback rediscover it as an
        # unlinked role and create a second mapping.
        if role_id is not None:
            known_role_ids.add(role_id)

        if owner_id is None:
            role = guild.get_role(role_id) if role_id else None
            if role is None:
                row_id = _custom_role_entry_row_id(entry)
                if row_id is not None and role_id is not None:
                    clearCustomRoleRoleIdById(row_id)
                    summary["warnings"].append(f"role_missing:{role_id}")
                else:
                    summary["warnings"].append(
                        f"legacy_owner_ambiguous:{role_id or 'unknown'}"
                    )
                continue

            owner_id = await _claim_proven_legacy_custom_role_owner(entry, role)
            if owner_id is None:
                summary["warnings"].append(f"legacy_owner_ambiguous:{role.id}")
                continue

            member = guild.get_member(owner_id)
            if member is None:
                summary["warnings"].append(f"member_unavailable:{owner_id}")
                continue

        discord_user_id = int(owner_id)
        async with _vip_custom_role_lock(guild.id, discord_user_id):
            member = guild.get_member(discord_user_id)
            member_summary, resolved_role_id = await _reconcile_vip_member_unlocked(
                guild,
                discord_user_id,
                member=member,
                entry=entry,
                custom_role_prefix=custom_role_prefix,
                vip_role_ids=vip_role_ids,
            )
            if resolved_role_id is not None:
                known_role_ids.add(resolved_role_id)
            for key in ("processed", "updated", "deleted", "recovered"):
                summary[key] += member_summary[key]
            summary["warnings"].extend(member_summary["warnings"])

    # Transitional recovery for legacy roles that were never linked to a DB
    # row. This remains only in the rare full safety sweep; normal operation is
    # role_id/event driven.
    prefixes = {custom_role_prefix.casefold(), "vip"}
    for role in guild.roles:
        if role.id in known_role_ids:
            continue
        name = getattr(role, "name", "")
        if not any(name.casefold().startswith(f"{prefix} ") for prefix in prefixes):
            continue
        members = list(getattr(role, "members", ()) or ())
        if len(members) != 1:
            continue
        member = members[0]
        if not _vip_member_has_access(member, vip_role_ids):
            continue
        if getCustomRoleEntry(guild.id, member.id) is not None:
            continue
        saveCustomRole(guild.id, member, roleId=role.id)
        summary["recovered"] += 1

    return summary


def formatEventList(eventList):
    sortedEventList = sorted(eventList, key=lambda event: event["starting_datetime"])
    messages = []
    
    for event in sortedEventList:
        google_maps_url = create_google_maps_url(event["address"])
        formattedEvent = (f'''> # {event["event_name"].title()}
>    **Data**: {event["starting_datetime"].strftime("%d/%m/%Y") if event["starting_datetime"].strftime("%d/%m/%Y") == event["ending_datetime"].strftime("%d/%m/%Y") else f"{event['starting_datetime'].strftime('%d')} a {event['ending_datetime'].strftime('%d/%m/%Y')}"}{" - das "+event["starting_datetime"].strftime("%H:%M")+" às "+event["ending_datetime"].strftime("%H:%M") if event["starting_datetime"] == event["ending_datetime"] else ''}
>    **Local**: {event["city"]}, {event["state_abbrev"]}
>    **Endereço**: [{event["address"]}]({google_maps_url}) ''' + '\n'+
        '\n'.join(filter(None, [
            f">    **Chat do evento**: <{event['group_chat_link']}>" if event['group_chat_link']!=None else '',
            f">    **Site**: <{event['website']}>" if event['website']!=None else '',
            f'''>    **Preço**: {"Ingressos esgotados" if event['out_of_tickets']
        else "Vendas encerradas" if event['sales_ended']
        else "A partir de R$"+str(f"{event['price']:.2f}").replace('.',',') if event['price']!=0 else 'Gratuito'}'''
            ])))
        if messages != []:
            if messages[-1].__len__() + '\n\n'.__len__() + formattedEvent.__len__() < 1500:
                messages[-1] += '\n\n' + formattedEvent
            else:
                messages.append(formattedEvent)
        else:
            messages.append(formattedEvent)
    return messages

def formatSingleEvent(event):
    google_maps_url = create_google_maps_url(event["address"])
    embeded_description = f'''**Data**: {event["starting_datetime"].strftime("%d/%m/%Y") if event["starting_datetime"].strftime("%d/%m/%Y") == event["ending_datetime"].strftime("%d/%m/%Y") 
                                            else f"{event['starting_datetime'].strftime('%d')} a {event['ending_datetime'].strftime('%d/%m/%Y')}"}{" - das "+event["starting_datetime"].strftime("%H:%M")+" às "+event["ending_datetime"].strftime("%H:%M") if event["starting_datetime"].strftime("%d/%m/%Y") == event["ending_datetime"].strftime("%d/%m/%Y") else ''}
**Local**: {event["city"]}, {event["state_abbrev"]}
**Endereço**: [{event["address"]}]({google_maps_url})'''
    if event['group_chat_link']!=None: embeded_description += f"""
**Chat do evento**: <{event['group_chat_link']}>"""
    if event['website']!=None: embeded_description += f"""
**Site**: <{event['website']}>""" 
    embeded_description += f"""
**Preço**: {"Ingressos esgotados" if event['out_of_tickets']
else "Vendas encerradas" if event['sales_ended']
else "De R$"+str(f"{event['price']:.0f}").replace('.',',')+" a "+"R${:,.0f}".format(event['max_price']).replace(",", "x").replace(".", ",").replace("x", ".") if (event['price']!=0 and event['max_price']!=0) 
else f'R$'+str(f"{event['price']:.0f}").replace('.',',') if (event['max_price']==0 or event['max_price']==event['price']) and event['price']!=0 else 'Gratuito'}"""
    if event['description']!=None: embeded_description += f"""

{event['description']}
"""
    eventEmbeded = discord.Embed(
        color=discord.Color.blue(),
        title=event["event_name"].title(),
        description=embeded_description
    )
    if event["event_logo_url"]!=None:
        eventEmbeded.set_thumbnail(url=event["event_logo_url"])
    else: eventEmbeded.set_author(name='')
    return eventEmbeded

async def removeTempRoles(bot:commands.Bot):
    handled_expired_roles = set()
    for guild_config in bot.config or []:
        guild_id = guild_config.guildId
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue

        expiringTempRoles = getExpiringTempRoles(guild_id)
        if expiringTempRoles == []:
            continue

        for TempRole in expiringTempRoles:
            async with temp_role_lock(
                guild_id,
                TempRole['user_id'],
                TempRole['role_id'],
            ):
                expiration_state = getTempRoleExpirationState(TempRole['id'])
                if expiration_state is None or not expiration_state['is_expired']:
                    continue

                member = guild.get_member(TempRole['user_id'])
                role = guild.get_role(TempRole['role_id'])
                grant_key = (
                    TempRole['disc_community_id'],
                    TempRole['disc_user_id'],
                    TempRole['role_id'],
                )
                has_active_sibling = hasActiveTempRoleSibling(
                    TempRole['disc_community_id'],
                    TempRole['disc_user_id'],
                    TempRole['role_id'],
                    TempRole['id'],
                )
                if has_active_sibling:
                    logger.info(
                        'Cargo temporário preservado por concessão ativa: guild_id=%s user_id=%s role_id=%s',
                        guild_id,
                        TempRole['user_id'],
                        TempRole['role_id'],
                    )
                elif (
                    grant_key not in handled_expired_roles
                    and member != None
                    and role != None
                ):
                    if member.roles.__contains__(role):
                        await member.remove_roles(role)
                        logger.info(
                            'Cargo temporário removido: guild_id=%s user_id=%s role_id=%s',
                            guild_id,
                            member.id,
                            role.id,
                        )
                    else:
                        logger.warning(
                            'Cargo temporário já ausente: guild_id=%s user_id=%s role_id=%s',
                            guild_id,
                            member.id,
                            role.id,
                        )
                    handled_expired_roles.add(grant_key)
                deleteTempRole(TempRole['id'])
    return

async def mentionHashtagRoles(bot, message:discord.Message):
    guild = message.guild
    if guild is None:
        return

    if message.channel.type != discord.ChannelType.public_thread:
        return

    parent_channel = getattr(message.channel, "parent", None)
    if parent_channel is None:
        return

    feature_config = getHashtagRoleMentionsConfig(guild.id)
    if not feature_config.get("enabled"):
        return

    channel_ids = feature_config.get("channelIds") or []
    required_role_id = feature_config.get("authorRoleId")
    mention_map = feature_config.get("hashtagMap") or {}
    if not channel_ids or not mention_map:
        return

    if parent_channel.id not in channel_ids:
        return

    thread_owner = message.channel.owner
    if thread_owner is None and getattr(message.channel, "owner_id", None):
        thread_owner = guild.get_member(message.channel.owner_id)

    if thread_owner is None or thread_owner.id != message.author.id:
        return

    if required_role_id:
        required_role = guild.get_role(required_role_id)
        if required_role is None or required_role not in thread_owner.roles:
            return

    answer = ""
    message_content = (message.content or "").casefold()
    for trigger, role_id in mention_map.items():
        if trigger.casefold() not in message_content:
            continue
        role = guild.get_role(role_id)
        if role is not None:
            answer += f"{role.mention} "

    if answer:
        await asyncio.sleep(2)
        return await message.channel.send(answer.strip(), delete_after=5)


async def mentionArtRoles(bot, message:discord.Message):
    """Backward-compatible alias for previous function name."""
    return await mentionHashtagRoles(bot, message)
        

async def enforceThreadAuthorOnlyPosting(message:discord.Message):
    guild = message.guild
    if guild is None:
        return

    if message.channel.type != discord.ChannelType.public_thread:
        return

    parent_channel = getattr(message.channel, "parent", None)
    if parent_channel is None:
        return

    restriction_config = getThreadOwnerOnlyPostingConfig(guild.id)
    if not restriction_config.get("enabled"):
        return

    forum_channel_ids = restriction_config.get("forumChannelIds") or []
    if not forum_channel_ids:
        return

    if parent_channel.id not in forum_channel_ids:
        return

    thread_owner = message.channel.owner
    if thread_owner is None and getattr(message.channel, "owner_id", None):
        thread_owner = guild.get_member(message.channel.owner_id)

    if thread_owner is None:
        return

    if thread_owner.id != message.author.id:
        await message.delete()
        await message.author.send(
            "Você não tem permissão para enviar mensagens nesta thread. Apenas o autor pode responder aqui."
        )
    return


async def secureArtPosts(message:discord.Message):
    """Backward-compatible alias for old art-specific thread restriction."""
    return await enforceThreadAuthorOnlyPosting(message)


def is_late_holiday(current_time: datetime) -> bool:
    """Allow responses during early hours on specific holidays."""

    holidays = {
        (12, 24),  # véspera de Natal
        (12, 25),  # Natal
        (12, 31),  # véspera de Ano Novo
        (1, 1),    # Ano Novo
    }
    return (current_time.month, current_time.day) in holidays and current_time.hour < 3


def _replace_member_mentions(text: str, mentions) -> str:
    """Replace Discord user mentions while preserving display names literally."""

    for member in mentions:
        text = text.replace(f"<@{member.id}>", member.display_name)
        text = text.replace(f"<@!{member.id}>", member.display_name)
    return text


async def handle_ai_response(bot, message: discord.Message):
    """Responde automaticamente a menções ou respostas ao bot."""

    inputChat = _replace_member_mentions(message.content, message.mentions)
    response = None
    current_time = now()
    allow_late_holiday_responses = is_late_holiday(current_time)

    if isinstance(message.channel, discord.channel.DMChannel):
        return

    is_dm = isinstance(message.channel, discord.channel.DMChannel)
    is_mention = bot.user in message.mentions
    is_reply = False
    if message.reference and message.reference.message_id:
        try:
            replied = message.reference.resolved or await message.channel.fetch_message(message.reference.message_id)
            is_reply = replied.author == bot.user
        except Exception:
            pass

    if not (is_dm or is_mention or is_reply):
        return

    if current_time.hour < 8 and not allow_late_holiday_responses and random.random() > 0.7:
        return

    guild_id = message.guild.id if message.guild else None
    ai_settings = get_ai_response_settings(guild_id) if guild_id else {}
    allowed_channels = get_allowed_feature_channels(guild_id, "ai_responses") if guild_id else []
    is_server_admin = isinstance(message.author, discord.Member) and (
        message.author.guild_permissions.administrator or message.author.guild_permissions.manage_guild
    )
    is_db_admin = int(message.author.id) in (ai_settings.get("adminUserIds") or [])
    admin_override = bool(ai_settings.get("adminChannelBypassEnabled")) and (is_server_admin or is_db_admin)

    should_limit_channels = bool(ai_settings.get("channelLimitEnabled"))
    if (
        not is_dm
        and should_limit_channels
        and message.channel.id not in allowed_channels
        and not admin_override
    ):
        return
    
    # se for horário de dormir, responde que está dormindo
    if current_time.hour < 8 and not allow_late_holiday_responses:
        response = """coddy está a mimir, às 8 horas eu volto 😴"""
    
    await asyncio.sleep(2)
    async with message.channel.typing():
        if not response:
            if ai_settings.get('enabled'):
                member_roles = [role.name for role in message.author.roles if role.name != "@everyone"]
                message_nature = 'direct' if is_dm or is_reply else 'mention'
                try:
                    result = await retornaRespostaGPT(
                        inputChat,
                        message.author.display_name if message.author.display_name else message.author.name,
                        member_roles,
                        bot,
                        message.channel.id,
                        'Discord',
                        ai_settings.get('model'),
                        message_nature,
                        ai_settings.get("openaiToken"),
                    )
                    response = await _resolve_casual_ai_result(result, message)
                except Exception as e:
                    logging.error("Erro ao obter resposta do GPT", exc_info=True)
            else:
                response = '''Eu to desativado por enquanto, mas logo logo eu volto! \nFale com o titio derg se você quiser saber mais sobre como ajudar a me manter ativo ;3'''

        if not response or not str(response).strip():
            response = 'Desculpa, deu ruim aqui, não consegui responder agora :c'

        await message.channel.send(
            sanitize_mentions(response),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False)
        )
    await bot.process_commands(message)


def _is_command_message(bot, message: discord.Message) -> bool:
    content = (message.content or "").strip()
    if not content:
        return False

    if content.startswith("/"):
        return True

    prefixes: list[str] = []
    command_prefix = getattr(bot, "command_prefix", None)
    if isinstance(command_prefix, str):
        prefixes.append(command_prefix)
    elif isinstance(command_prefix, (list, tuple)):
        prefixes.extend(str(prefix) for prefix in command_prefix)

    return any(prefix and content.startswith(prefix) for prefix in prefixes)


def _format_level_up_message(template: str | None, member: discord.Member, previous_level: int, new_level: int) -> str:
    default_message = "🎉 {member} subiu do nível **{old_level}** para o **{new_level}**!"
    base_template = template if template and str(template).strip() else default_message
    safe_template = str(base_template).replace("{level}", "{new_level}")

    try:
        return safe_template.format(
            user=member.mention,
            member=member.mention,
            username=member.display_name,
            old_level=previous_level,
            last_level=previous_level,
            previous_level=previous_level,
            new_level=new_level,
            actual_level=new_level,
        )
    except Exception:
        return default_message.format(
            member=member.mention,
            old_level=previous_level,
            new_level=new_level,
        )


def _levelup_warning_enabled(level_config: dict[str, object]) -> bool:
    levelup_warning_enabled = level_config.get("levelupWarning")
    if levelup_warning_enabled is None:
        levelup_warning_enabled = level_config.get("levelup_warning")
    return bool(levelup_warning_enabled)


def _resolve_levelup_channel(
    member: discord.Member,
    level_config: dict[str, object],
    fallback_channel: discord.abc.Messageable | None,
) -> discord.abc.Messageable | None:
    if not _levelup_warning_enabled(level_config):
        return None

    configured_channel_id = (
        level_config.get("levelupWarningChannel")
        or level_config.get("levelup_warning_channel")
    )
    guild = member.guild
    channel = None
    if configured_channel_id:
        try:
            channel_id = int(configured_channel_id)
            channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
        except (TypeError, ValueError):
            logging.warning(
                "levelup_warning_channel inválido para guild %s: %s",
                guild.id,
                configured_channel_id,
            )

    if channel is not None:
        return channel

    if fallback_channel is not None:
        return fallback_channel

    return None


async def _flush_pending_xp_once() -> None:
    async with _xp_buffer_lock:
        if not _xp_pending_buffer:
            return

        pending = list(_xp_pending_buffer.items())
        cached_members = dict(_xp_member_cache)
        cached_origin_channels = dict(_xp_origin_channel_cache)
        _xp_pending_buffer.clear()
        _xp_member_cache.clear()
        _xp_origin_channel_cache.clear()
    try:
        guild_batches: dict[int, list[tuple[int, int]]] = {}
        announced_level_ups: set[tuple[int, int]] = set()
        for (guild_id, discord_user_id), xp_delta in pending:
            guild_batches.setdefault(guild_id, []).append((discord_user_id, xp_delta))

        async def _run_xp_batch_db_updates(
            guild_id: int,
            rows: list[tuple[int, int]],
            level_config: dict,
        ) -> tuple[dict[int, int], dict[int, int], dict[int, int]]:
            xp_deltas_to_upsert: list[tuple[int, int, int, int]] = []
            db_user_by_discord_id: dict[int, int] = {}
            async with async_pooled_connection() as cursor:
                for discord_user_id, xp_delta in rows:
                    if xp_delta <= 0:
                        continue

                    member = cached_members.get((guild_id, discord_user_id))
                    if member is None:
                        continue

                    user_id = await async_includeUser(member, guild_id)
                    db_user_by_discord_id[discord_user_id] = user_id
                    xp_deltas_to_upsert.append((user_id, guild_id, int(xp_delta), int(xp_delta), int(xp_delta)))

                if not xp_deltas_to_upsert:
                    return {}, {}, {}

                await cursor.executemany(
                    """
                    INSERT INTO user_level (
                        user_id, server_guild_id, total_xp, current_level,
                        xp_awarded_today, xp_awarded_day,
                        xp_awarded_text_today, xp_awarded_text_day
                    )
                    VALUES (%s, %s, %s, 0, %s, UTC_DATE(), %s, UTC_DATE())
                    ON DUPLICATE KEY UPDATE
                        total_xp = total_xp + VALUES(total_xp),
                        xp_awarded_today = CASE WHEN xp_awarded_day = UTC_DATE() THEN xp_awarded_today + VALUES(xp_awarded_today) ELSE VALUES(xp_awarded_today) END,
                        xp_awarded_day = UTC_DATE(),
                        xp_awarded_text_today = CASE WHEN xp_awarded_text_day = UTC_DATE() THEN xp_awarded_text_today + VALUES(xp_awarded_text_today) ELSE VALUES(xp_awarded_text_today) END,
                        xp_awarded_text_day = UTC_DATE()
                    """,
                    xp_deltas_to_upsert,
                )

                user_ids = sorted({row[0] for row in xp_deltas_to_upsert})
                placeholders = ",".join(["%s"] * len(user_ids))
                await cursor.execute(
                    f"""
                    SELECT user_id, total_xp, current_level
                    FROM user_level
                    WHERE server_guild_id = %s
                      AND user_id IN ({placeholders})
                    """,
                    (guild_id, *user_ids),
                )
                totals_by_user_id: dict[int, int] = {}
                previous_levels_by_user_id: dict[int, int] = {}
                for row in (await cursor.fetchall() or []):
                    db_user_id = int(row["user_id"])
                    totals_by_user_id[db_user_id] = int(row.get("total_xp") or 0)
                    previous_levels_by_user_id[db_user_id] = int(row.get("current_level") or 0)

                level_updates = [
                    (level_from_total_xp(total_xp, level_config), db_user_id, guild_id)
                    for db_user_id, total_xp in totals_by_user_id.items()
                ]
                if not level_updates:
                    return db_user_by_discord_id, previous_levels_by_user_id, {}

                await cursor.executemany(
                    """
                    UPDATE user_level
                    SET current_level = %s
                    WHERE user_id = %s AND server_guild_id = %s
                    """,
                    level_updates,
                )
            return (
                db_user_by_discord_id,
                previous_levels_by_user_id,
                {row[1]: row[0] for row in level_updates},
            )

        for guild_id, rows in guild_batches.items():
            level_config = await async_get_level_config(guild_id)
            db_user_by_discord_id, previous_levels_by_user_id, new_levels_by_user_id = await _run_xp_batch_db_updates(
                guild_id, rows, level_config
            )
            if not new_levels_by_user_id:
                continue
            discord_id_by_db_user = {v: k for k, v in db_user_by_discord_id.items()}
            for db_user_id, new_level in new_levels_by_user_id.items():
                previous_level = previous_levels_by_user_id.get(db_user_id, 0)
                if new_level <= previous_level:
                    continue

                discord_user_id = discord_id_by_db_user.get(db_user_id)
                if discord_user_id is None:
                    continue

                dedup_key = (guild_id, discord_user_id)
                if dedup_key in announced_level_ups:
                    continue

                member = cached_members.get((guild_id, discord_user_id))
                if member is None:
                    continue

                if not _levelup_warning_enabled(level_config):
                    continue

                destination_channel = _resolve_levelup_channel(
                    member,
                    level_config,
                    cached_origin_channels.get((guild_id, discord_user_id)),
                )
                if destination_channel is None:
                    logging.warning(
                        "Sem canal disponível para aviso de level up: guild=%s user=%s",
                        guild_id,
                        discord_user_id,
                    )
                    continue

                level_message = _format_level_up_message(
                    level_config.get("levelUpMessage") or level_config.get("level_up_message"),
                    member,
                    previous_level,
                    new_level,
                )
                try:
                    await destination_channel.send(
                        sanitize_mentions(level_message),
                        allowed_mentions=discord.AllowedMentions(
                            users=False,
                            roles=False,
                            everyone=False,
                            replied_user=False,
                        ),
                    )
                except Exception:
                    logging.exception(
                        "Falha ao enviar aviso de level up: guild=%s user=%s channel=%s",
                        guild_id,
                        discord_user_id,
                        getattr(destination_channel, "id", "unknown"),
                    )
                    continue

                announced_level_ups.add(dedup_key)
    except Exception:
        async with _xp_buffer_lock:
            for key, xp_delta in pending:
                _xp_pending_buffer[key] = _xp_pending_buffer.get(key, 0) + xp_delta
            _xp_member_cache.update(cached_members)
            _xp_origin_channel_cache.update(cached_origin_channels)
        raise


async def _xp_flush_loop() -> None:
    global _xp_flush_task
    try:
        while True:
            await asyncio.sleep(XP_FLUSH_INTERVAL_SECONDS)
            await _flush_pending_xp_once()
    except asyncio.CancelledError:
        await _flush_pending_xp_once()
        raise
    finally:
        _xp_flush_task = None


def _ensure_xp_flush_task() -> None:
    global _xp_flush_task
    if _xp_flush_task is None or _xp_flush_task.done():
        _xp_flush_task = asyncio.create_task(_xp_flush_loop())


async def flush_xp_buffer_on_shutdown() -> None:
    global _xp_flush_task
    task = _xp_flush_task
    if task is None or task.done():
        await _flush_pending_xp_once()
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def handle_message_xp(bot, message: discord.Message) -> None:
    if message.guild is None or message.author.bot:
        return

    if _is_command_message(bot, message):
        return

    blocked_channels = set(get_allowed_feature_channels(message.guild.id, XP_BLOCKED_CHANNELS_FEATURE_KEY))
    if message.channel.id in blocked_channels:
        return

    level_config = await async_get_level_config(message.guild.id)
    if not bool(level_config.get("textXpEnabled", True)):
        return

    anti_farm_key = (message.guild.id, message.author.id, message.channel.id)
    now_monotonic = asyncio.get_running_loop().time()
    if now_monotonic < _xp_anti_farm_bucket.get(anti_farm_key, 0):
        return

    cooldown_min_raw = level_config.get("textXpCooldownMinSeconds")
    cooldown_max_raw = level_config.get("textXpCooldownMaxSeconds")
    cooldown_min = max(
        0,
        int(
            cooldown_min_raw
            if cooldown_min_raw is not None
            else XP_ANTI_FARM_MIN_SECONDS
        ),
    )
    cooldown_max = max(
        cooldown_min,
        int(
            cooldown_max_raw
            if cooldown_max_raw is not None
            else XP_ANTI_FARM_MAX_SECONDS
        ),
    )
    cooldown_seconds = random.randint(cooldown_min, cooldown_max)
    _xp_anti_farm_bucket[anti_farm_key] = now_monotonic + cooldown_seconds

    user_id, level_config, today_total, today_text = await _collect_text_xp_context(
        message,
        level_config,
    )
    base_min_raw = level_config.get("textXpBaseMin")
    base_max_raw = level_config.get("textXpBaseMax")
    base_min = max(
        0,
        int(base_min_raw if base_min_raw is not None else XP_MESSAGE_BASE_MIN),
    )
    base_max = max(
        base_min,
        int(base_max_raw if base_max_raw is not None else XP_MESSAGE_BASE_MAX),
    )
    base_xp = random.randint(base_min, base_max)

    award = XpPolicy.award_text_xp(base_xp, level_config, today_total, today_text)
    gained_xp = award.granted_xp
    if gained_xp <= 0:
        return

    async with _xp_buffer_lock:
        key = (message.guild.id, message.author.id)
        _xp_pending_buffer[key] = _xp_pending_buffer.get(key, 0) + gained_xp
        if isinstance(message.author, discord.Member):
            _xp_member_cache[key] = message.author
        _xp_origin_channel_cache[key] = message.channel

    _ensure_xp_flush_task()


def generateUserDescription(
    member: User,
    in_guild: bool = True,
    show_approval_status: bool = False,
    ban_date: datetime | None = None,
    banned_in_discord: bool = False,
):
    userDescription = ''
    userDescription += f'## Criador da BraFurries\n' if CREATOR_DISCORD_ID is not None and member.discordId == CREATOR_DISCORD_ID else ''
    userDescription += '### ID {0:64} - {1}'.format(str(member.discordId), member.username)
    userDescription += f'\n<@{member.discordId}>'
    userDescription += f'\n**Tipo de VIF:** {member.vipType}' if member.isVip else ''
    if in_guild:
        userDescription += f'\n**Entrou em:** {member.memberSince.strftime("%d/%m/%Y")}'
        if show_approval_status:
            approval_status = (
                member.approvedAt.strftime("%d/%m/%Y")
                + (
                    f" (a {(datetime.now() - member.approvedAt).days} dias)"
                    if (datetime.now() - member.approvedAt).days < 45
                    else ""
                )
                if member.approvedAt
                else "Desconhecido"
                if member.approved == 1
                else "Não aprovado"
            )
            userDescription += f'\n**Aprovado em:** {approval_status}'
    else:
        if ban_date:
            userDescription += f'\n**Banido em:** {ban_date.strftime("%d/%m/%Y")}'
        elif banned_in_discord:
            userDescription += '\n**Banido (data indisponível)**'
        else:
            userDescription += f'\n**Não está no servidor**'
    registration_status = "verificado" if member.birthdayVerified else "não verificado"
    age_text = "Não informada"
    if member.birthday is not None:
        age_years = math.floor((datetime.now().date() - member.birthday).days / 365.2425)
        age_text = f"{age_years} anos"
    userDescription += f'\n**Idade:** {age_text} ({registration_status})'
    userDescription += f'\n'
    if member.locale or member.coins or member.inventory:
        userDescription += f'\nRegistrado em **{member.locale}**' if member.locale else ''
        userDescription += f'\n**Moedas: ** {member.coins}' if member.coins else ''
        userDescription += f'\n**Inventário: {member.inventory.__len__} itens no inventário**' if member.inventory else ''
    if member.staffOf.__len__() > 0:
        userDescription += f'\n'
        userDescription += f'\n**Staff {"dos eventos" if member.staffOf.__len__ > 1 else "do evento"}**: {member.staffOf}'
        for event in member.staffOf:
            userDescription += f'\n- *{event.name}*'

    return userDescription


async def sendBirthdayMessages(bot: MyBot):
    for guild_config in bot.config or []:
        guild_id = guild_config.guildId
        guild = bot.get_guild(guild_id)
        if not guild:
            continue

        birthday_points = get_birthday_reward_points(guild_id)

        # getServerMessage returns a dict with the requested column as a key
        message_record = getServerMessage(messageType="birthday", guild_id=guild_id)
        if isinstance(message_record, dict):
            message = message_record.get("birthday")
        else:
            message = message_record

        if not message:
            message = 'Hoje é aniversário de {users}! Parabéns!'

        users = getTodayBirthdays(guild_id)
        if not users:
            continue

        valid_users = []
        for user in users:
            member = guild.get_member(user.DiscordId)
            if not member:
                continue

            birthday_role = guild.get_role(BIRTHDAY_ROLE_ID) if BIRTHDAY_ROLE_ID else None
            if birthday_role:
                await assignTempRole(
                    guild.id,
                    member,
                    birthday_role.id,
                    now() + timedelta(days=3),
                    "Aniversário",
                )
            valid_users.append(user)

        if not valid_users:
            continue

        users_for_message = [f'<@{user.DiscordId}>' for user in valid_users]
        if len(users_for_message) > 1:
            users_str = ', '.join(users_for_message[:-1]) + f' e {users_for_message[-1]}'
        else:
            users_str = users_for_message[0]

        message_to_send = message.replace('{users}', users_str)
        target_channel_id = getGuildBirthdayMessageChannelId(guild_id)
        if not target_channel_id:
            logging.warning("Canal de aniversário não configurado para guild %s", guild.id)
            continue
        channel = guild.get_channel(target_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(target_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logging.warning(
                    "Canal de aniversário inválido ou inacessível para guild %s (canal %s)",
                    guild.id,
                    target_channel_id,
                )
                continue

        try:
            await channel.send(message_to_send)
        except discord.Forbidden:
            logging.warning(
                "Sem permissão para enviar mensagem de aniversário na guild %s (canal %s)",
                guild.id,
                target_channel_id,
            )
            continue

        for user in valid_users:
            member = guild.get_member(user.DiscordId)
            if member:
                adjust_user_economy_balance(guild.id, member, birthday_points)

        try:
            await channel.send(
                f"Ahh! e como presente de aniversário, cada aniversáriante recebe {birthday_points} moedas! Faça bom proveito :3"
            )
        except discord.Forbidden:
            logging.warning(
                "Sem permissão para enviar mensagem de recompensa de aniversário na guild %s (canal %s)",
                guild.id,
                target_channel_id,
            )


async def update_adult_roles(bot: MyBot):
    for guild_config in bot.config or []:
        guild_id = guild_config.guildId
        guild = bot.get_guild(guild_id)
        if not guild:
            continue

        age_roles_config = getGuildAgeRoleIds(guild_id)
        adult_role = guild.get_role(age_roles_config.get("adultRoleId"))
        minor_role = guild.get_role(age_roles_config.get("minorRoleId"))

        if not adult_role and not minor_role:
            continue

        users = getUsersTurning18Today(guild_id)

        for user in users:
            member = guild.get_member(user.DiscordId)
            if not member:
                continue

            tasks_to_run = []

            if adult_role and adult_role not in member.roles:
                tasks_to_run.append(
                    member.add_roles(
                        adult_role, reason="Completing 18 years old"
                    )
                )

            if minor_role and minor_role in member.roles:
                tasks_to_run.append(
                    member.remove_roles(
                        minor_role, reason="Completing 18 years old"
                    )
                )

            if tasks_to_run:
                await asyncio.gather(*tasks_to_run)
        
