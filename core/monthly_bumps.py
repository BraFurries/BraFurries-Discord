import logging

import discord
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import tz

from core.database import (
    _ensure_economy_entry,
    assignTempRole,
    getUserId,
    includeUser,
    listBumpMonthlyRewardConfigs,
    pooled_connection,
)
from core.disboard_bump import resolve_assignable_bump_role


logger = logging.getLogger(__name__)
DISBOARD_BOT_ID = 302050872383242240
try:
    SAO_PAULO = ZoneInfo("America/Sao_Paulo")
except ZoneInfoNotFoundError:  # Windows may not ship an IANA timezone database.
    SAO_PAULO = tz.gettz("America/Sao_Paulo")
UTC = timezone.utc
MONTHLY_REWARD_REASON = "Bump Monthly Top 3 Reward"


@dataclass(frozen=True)
class MonthlyPeriod:
    start: date
    end: date
    start_utc: datetime
    end_utc: datetime


class MissingDiscordIdentity(RuntimeError):
    pass


def previous_closed_month(reference_time: datetime | None = None) -> MonthlyPeriod:
    current = reference_time or datetime.now(SAO_PAULO)
    current = (
        current.replace(tzinfo=SAO_PAULO)
        if current.tzinfo is None
        else current.astimezone(SAO_PAULO)
    )
    current_month = date(current.year, current.month, 1)
    previous_day = current_month - timedelta(days=1)
    period_start = date(previous_day.year, previous_day.month, 1)
    local_start = datetime.combine(period_start, time.min, tzinfo=SAO_PAULO)
    local_end = datetime.combine(current_month, time.min, tzinfo=SAO_PAULO)
    return MonthlyPeriod(
        period_start,
        current_month,
        local_start.astimezone(UTC),
        local_end.astimezone(UTC),
    )


def is_monthly_preparation_window(reference_time: datetime | None = None) -> bool:
    current = reference_time or datetime.now(SAO_PAULO)
    if current.tzinfo is not None:
        current = current.astimezone(SAO_PAULO)
    return current.day <= 3


def sanitize_error(error: BaseException | str) -> str:
    message = (
        f"{type(error).__name__}: {error}"
        if isinstance(error, BaseException)
        else str(error)
    )
    return " ".join(message.split())[:512]


def _three_nonnegative(values: Iterable[int] | None) -> list[int]:
    result = [max(0, int(value)) for value in list(values or [])[:3]]
    return result + [0] * (3 - len(result))


def _fetch_run(cursor, run_id: int) -> dict | None:
    cursor.execute("SELECT * FROM bump_monthly_reward_runs WHERE id = %s", (run_id,))
    return cursor.fetchone()


def get_or_create_monthly_reward_run(config: dict, period: MonthlyPeriod) -> dict:
    """Create the immutable configuration snapshot for one guild/month."""
    days = _three_nonnegative(config.get("rewardDays"))
    coins = _three_nonnegative(config.get("rewardCoins"))
    role_id = config.get("rewardRoleId")
    role_id = int(role_id) if role_id else None
    guild_id = int(config["guildId"])

    with pooled_connection() as cursor:
        cursor.execute(
            """
            INSERT IGNORE INTO bump_monthly_reward_runs (
                server_guild_id, period_start, period_end, source_channel_id,
                reward_role_id, reward_days_1, reward_days_2, reward_days_3,
                reward_coins_1, reward_coins_2, reward_coins_3
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                guild_id,
                period.start,
                period.end,
                int(config["disboardChannelId"]),
                role_id,
                *days,
                *coins,
            ),
        )
        cursor.execute(
            """
            SELECT * FROM bump_monthly_reward_runs
            WHERE server_guild_id = %s AND period_start = %s
            """,
            (guild_id, period.start),
        )
        run = cursor.fetchone()
        if not run:
            raise RuntimeError("Falha ao criar ou recuperar execução mensal de bump")
        return run


def list_incomplete_monthly_reward_runs() -> list[dict]:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT * FROM bump_monthly_reward_runs
            WHERE completed_at IS NULL
            ORDER BY period_start ASC, server_guild_id ASC, id ASC
            """
        )
        return cursor.fetchall() or []


def start_monthly_reward_attempt(run_id: int) -> dict:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE bump_monthly_reward_runs
            SET attempt_count = attempt_count + 1,
                last_attempt_at = NOW(), last_error = NULL
            WHERE id = %s AND completed_at IS NULL
            """,
            (run_id,),
        )
        run = _fetch_run(cursor, run_id)
        if not run:
            raise RuntimeError(f"Execução mensal de bump inexistente: {run_id}")
        return run


def set_monthly_run_error(run_id: int, error: BaseException | str) -> None:
    with pooled_connection() as cursor:
        cursor.execute(
            "UPDATE bump_monthly_reward_runs SET last_error = %s WHERE id = %s",
            (sanitize_error(error), run_id),
        )


def set_monthly_grant_error(grant_id: int, error: BaseException | str) -> None:
    with pooled_connection() as cursor:
        cursor.execute(
            "UPDATE bump_monthly_reward_grants SET last_error = %s WHERE id = %s",
            (sanitize_error(error), grant_id),
        )


def _record_run_error(run_id: int, error: BaseException | str) -> None:
    try:
        set_monthly_run_error(run_id, error)
    except Exception:
        logger.exception("Falha ao registrar erro da execução mensal: run_id=%s", run_id)


def _record_grant_error(grant_id: int, error: BaseException | str) -> None:
    try:
        set_monthly_grant_error(grant_id, error)
    except Exception:
        logger.exception("Falha ao registrar erro da concessão mensal: grant_id=%s", grant_id)


async def collect_monthly_bump_ranking(channel, period: MonthlyPeriod) -> list[dict]:
    bumpers: dict[int, dict] = {}
    history_after = period.start_utc - timedelta(milliseconds=1)
    async for message in channel.history(
        limit=None,
        after=history_after,
        before=period.end_utc,
        oldest_first=True,
    ):
        interaction = getattr(message, "interaction", None)
        interaction_user = getattr(interaction, "user", None)
        if getattr(getattr(message, "author", None), "id", None) != DISBOARD_BOT_ID:
            continue
        if interaction_user is None:
            continue
        created_at = getattr(message, "created_at", None)
        if created_at is None:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        created_at = created_at.astimezone(UTC)
        if created_at < period.start_utc or created_at >= period.end_utc:
            continue

        user_id = int(interaction_user.id)
        first_at = created_at.astimezone(SAO_PAULO).replace(tzinfo=None)
        if user_id not in bumpers:
            bumpers[user_id] = {
                "discord_user_id": user_id,
                "bump_count": 1,
                "first_bump_at": first_at,
            }
        else:
            bumpers[user_id]["bump_count"] += 1
            bumpers[user_id]["first_bump_at"] = min(
                bumpers[user_id]["first_bump_at"], first_at
            )

    return sorted(
        bumpers.values(),
        key=lambda item: (
            -item["bump_count"],
            item["first_bump_at"],
            item["discord_user_id"],
        ),
    )


def filter_monthly_ranking_members(guild, ranking: list[dict]) -> list[dict]:
    """Keep membership eligibility fixed at ranking preparation time."""
    return [
        entry
        for entry in ranking
        if guild.get_member(int(entry["discord_user_id"])) is not None
    ]


async def resolve_monthly_source_channel(bot, guild, channel_id: int):
    """Resolve the immutable monthly source channel beyond the normal guild cache.

    Old runs keep using their snapshotted source channel. A cold restart can
    nevertheless leave a valid channel outside guild.get_channel (notably a
    legacy thread or a cache miss), so resolve cache/thread variants first and
    finally use Discord REST before declaring the run unrecoverable.
    """

    source_channel_id = int(channel_id)
    channel = None

    get_channel_or_thread = getattr(guild, "get_channel_or_thread", None)
    if callable(get_channel_or_thread):
        channel = get_channel_or_thread(source_channel_id)

    if channel is None:
        get_guild_channel = getattr(guild, "get_channel", None)
        if callable(get_guild_channel):
            channel = get_guild_channel(source_channel_id)

    if channel is None:
        get_bot_channel = getattr(bot, "get_channel", None)
        if callable(get_bot_channel):
            channel = get_bot_channel(source_channel_id)

    if channel is None:
        fetch_channel = getattr(bot, "fetch_channel", None)
        if not callable(fetch_channel):
            raise RuntimeError(
                f"Canal mensal {source_channel_id} não está disponível no cache e o bot não oferece fetch_channel"
            )
        try:
            channel = await fetch_channel(source_channel_id)
        except discord.NotFound as error:
            raise RuntimeError(
                f"Canal mensal {source_channel_id} não existe mais no Discord"
            ) from error
        except discord.Forbidden as error:
            raise RuntimeError(
                f"Coddy não tem acesso ao canal mensal {source_channel_id}"
            ) from error
        except discord.HTTPException as error:
            raise RuntimeError(
                f"Falha HTTP ao resolver canal mensal {source_channel_id}: status={error.status}"
            ) from error

    channel_guild = getattr(channel, "guild", None)
    channel_guild_id = getattr(channel_guild, "id", None)
    if channel_guild_id is None:
        raise RuntimeError(
            f"Canal mensal {source_channel_id} não pertence a uma guild resolvível"
        )
    if int(channel_guild_id) != int(guild.id):
        raise RuntimeError(
            f"Canal mensal {source_channel_id} pertence a outra guild"
        )
    if not callable(getattr(channel, "history", None)):
        raise RuntimeError(
            f"Canal mensal {source_channel_id} não suporta leitura de histórico"
        )
    return channel


def _persist_monthly_statistics(cursor, guild_id: int, ranking: list[dict]) -> None:
    cursor.execute(
        "UPDATE user_records SET bumps = 0 "
        "WHERE server_guild_id = %s AND bumps IS NOT NULL",
        (guild_id,),
    )
    for entry in ranking:
        cursor.execute(
            "SELECT user_id FROM user_discord WHERE discord_user_id = %s",
            (entry["discord_user_id"],),
        )
        identity = cursor.fetchone()
        if not identity:
            logger.warning(
                "Usuário sem associação interna ignorado na estatística mensal: "
                "guild_id=%s discord_user_id=%s",
                guild_id,
                entry["discord_user_id"],
            )
            continue
        cursor.execute(
            """
            INSERT INTO user_records (server_guild_id, user_id, bumps)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE bumps = VALUES(bumps)
            """,
            (guild_id, identity["user_id"], entry["bump_count"]),
        )


def prepare_monthly_reward_ranking(run_id: int, ranking: list[dict]) -> bool:
    """Freeze ranking, reward values, and derived statistics exactly once."""
    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT * FROM bump_monthly_reward_runs WHERE id = %s FOR UPDATE",
            (run_id,),
        )
        run = cursor.fetchone()
        if not run:
            raise RuntimeError(f"Execução mensal de bump inexistente: {run_id}")
        if run.get("ranking_prepared_at") is not None:
            return False

        _persist_monthly_statistics(cursor, int(run["server_guild_id"]), ranking)
        winners = ranking[:3]
        for position, entry in enumerate(winners, start=1):
            cursor.execute(
                """
                INSERT INTO bump_monthly_reward_grants (
                    run_id, discord_user_id, rank_position, bump_count,
                    first_bump_at, coins_amount, role_id, role_days
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    entry["discord_user_id"],
                    position,
                    entry["bump_count"],
                    entry["first_bump_at"],
                    int(run[f"reward_coins_{position}"]),
                    run.get("reward_role_id"),
                    int(run[f"reward_days_{position}"]),
                ),
            )
        cursor.execute(
            """
            UPDATE bump_monthly_reward_runs
            SET ranking_prepared_at = NOW(),
                completed_at = CASE WHEN %s = 0 THEN NOW() ELSE completed_at END,
                last_error = NULL
            WHERE id = %s
            """,
            (len(winners), run_id),
        )
        return True


def list_monthly_reward_grants(run_id: int) -> list[dict]:
    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT * FROM bump_monthly_reward_grants "
            "WHERE run_id = %s ORDER BY rank_position ASC",
            (run_id,),
        )
        return cursor.fetchall() or []


def apply_monthly_reward_coins(grant_id: int) -> bool:
    """Apply coins and the application marker in the same transaction."""
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT reward_grant.*, run.server_guild_id
            FROM bump_monthly_reward_grants AS reward_grant
            JOIN bump_monthly_reward_runs AS run ON run.id = reward_grant.run_id
            WHERE reward_grant.id = %s FOR UPDATE
            """,
            (grant_id,),
        )
        grant = cursor.fetchone()
        if not grant:
            raise RuntimeError(f"Concessão mensal de bump inexistente: {grant_id}")
        if int(grant["coins_amount"] or 0) <= 0 or grant.get("coins_applied_at") is not None:
            return False

        cursor.execute(
            "SELECT user_id FROM user_discord WHERE discord_user_id = %s",
            (grant["discord_user_id"],),
        )
        identity = cursor.fetchone()
        if not identity:
            raise MissingDiscordIdentity(str(grant["discord_user_id"]))
        economy = _ensure_economy_entry(
            cursor,
            int(identity["user_id"]),
            int(grant["server_guild_id"]),
            lock=True,
        )
        balance = int(economy["bank_balance"] or 0) + int(grant["coins_amount"])
        cursor.execute(
            "UPDATE user_economy SET bank_balance = %s WHERE id = %s",
            (balance, economy["id"]),
        )
        cursor.execute(
            "UPDATE bump_monthly_reward_grants "
            "SET coins_applied_at = NOW(), last_error = NULL WHERE id = %s",
            (grant_id,),
        )
        return True


def ensure_monthly_role_expiration(
    grant_id: int, reference_time: datetime | None = None
) -> datetime | None:
    current = reference_time or datetime.now(SAO_PAULO)
    if current.tzinfo is not None:
        current = current.astimezone(SAO_PAULO).replace(tzinfo=None)
    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT * FROM bump_monthly_reward_grants WHERE id = %s FOR UPDATE",
            (grant_id,),
        )
        grant = cursor.fetchone()
        if not grant:
            raise RuntimeError(f"Concessão mensal de bump inexistente: {grant_id}")
        if not grant.get("role_id") or int(grant["role_days"] or 0) <= 0:
            return None
        if grant.get("role_expires_at") is not None:
            return grant["role_expires_at"]
        expiration = current + timedelta(days=int(grant["role_days"]))
        cursor.execute(
            "UPDATE bump_monthly_reward_grants SET role_expires_at = %s WHERE id = %s",
            (expiration, grant_id),
        )
        return expiration


def mark_monthly_role_applied(grant_id: int) -> None:
    with pooled_connection() as cursor:
        cursor.execute(
            "UPDATE bump_monthly_reward_grants "
            "SET role_applied_at = COALESCE(role_applied_at, NOW()), last_error = NULL "
            "WHERE id = %s",
            (grant_id,),
        )


def mark_monthly_role_skipped(grant_id: int, reason: str) -> None:
    with pooled_connection() as cursor:
        cursor.execute(
            "UPDATE bump_monthly_reward_grants "
            "SET role_skipped_at = COALESCE(role_skipped_at, NOW()), last_error = %s "
            "WHERE id = %s",
            (sanitize_error(reason), grant_id),
        )


async def apply_monthly_reward_role(
    grant_id: int, guild, member, role_id: int, expiration: datetime
) -> bool:
    """Serialize the Discord role effect with the persisted grant marker."""
    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT * FROM bump_monthly_reward_grants WHERE id = %s FOR UPDATE",
            (grant_id,),
        )
        locked_grant = cursor.fetchone()
        if not locked_grant:
            raise RuntimeError(f"Concessão mensal de bump inexistente: {grant_id}")
        if locked_grant.get("role_applied_at") is not None:
            return False
        if locked_grant.get("role_skipped_at") is not None:
            return False

        applied = await assignTempRole(
            int(guild.id), member, role_id, expiration, MONTHLY_REWARD_REASON
        )
        if not applied:
            raise RuntimeError("Falha transitória ao atribuir cargo temporário mensal")
        cursor.execute(
            "UPDATE bump_monthly_reward_grants "
            "SET role_applied_at = NOW(), last_error = NULL WHERE id = %s",
            (grant_id,),
        )
        return True


def reserve_monthly_notification(grant_id: int) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            "UPDATE bump_monthly_reward_grants SET notification_attempted_at = NOW() "
            "WHERE id = %s AND notification_attempted_at IS NULL",
            (grant_id,),
        )
        return cursor.rowcount == 1


def mark_monthly_notification_sent(grant_id: int) -> None:
    with pooled_connection() as cursor:
        cursor.execute(
            "UPDATE bump_monthly_reward_grants "
            "SET notification_sent_at = NOW(), last_error = NULL WHERE id = %s",
            (grant_id,),
        )


def grant_effects_are_terminal(grant: dict) -> bool:
    coins_done = int(grant.get("coins_amount") or 0) <= 0 or grant.get("coins_applied_at") is not None
    role_done = (
        not grant.get("role_id")
        or int(grant.get("role_days") or 0) <= 0
        or grant.get("role_applied_at") is not None
        or grant.get("role_skipped_at") is not None
    )
    return coins_done and role_done


def monthly_grant_has_reward(grant: dict) -> bool:
    return int(grant.get("coins_amount") or 0) > 0 or (
        grant.get("role_id") is not None
        and int(grant.get("role_days") or 0) > 0
    )


def try_complete_monthly_reward_run(run_id: int) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE bump_monthly_reward_runs AS run
            SET completed_at = NOW(), last_error = NULL
            WHERE run.id = %s
              AND run.ranking_prepared_at IS NOT NULL
              AND run.completed_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM bump_monthly_reward_grants AS reward_grant
                  WHERE reward_grant.run_id = run.id
                    AND (
                        (reward_grant.coins_amount > 0 AND reward_grant.coins_applied_at IS NULL)
                        OR (reward_grant.role_id IS NOT NULL AND reward_grant.role_days > 0
                            AND reward_grant.role_applied_at IS NULL
                            AND reward_grant.role_skipped_at IS NULL)
                        OR ((reward_grant.coins_amount > 0
                             OR (reward_grant.role_id IS NOT NULL AND reward_grant.role_days > 0))
                            AND reward_grant.notification_attempted_at IS NULL)
                    )
              )
            """,
            (run_id,),
        )
        return cursor.rowcount == 1


def _period_from_run(run: dict) -> MonthlyPeriod:
    start = run["period_start"]
    end = run["period_end"]
    start = start.date() if isinstance(start, datetime) else start
    end = end.date() if isinstance(end, datetime) else end
    local_start = datetime.combine(start, time.min, tzinfo=SAO_PAULO)
    local_end = datetime.combine(end, time.min, tzinfo=SAO_PAULO)
    return MonthlyPeriod(start, end, local_start.astimezone(UTC), local_end.astimezone(UTC))


async def _apply_grant_coins(guild, grant: dict) -> None:
    if int(grant.get("coins_amount") or 0) <= 0 or grant.get("coins_applied_at") is not None:
        return
    try:
        apply_monthly_reward_coins(int(grant["id"]))
    except MissingDiscordIdentity:
        member = guild.get_member(int(grant["discord_user_id"]))
        if member is None:
            raise
        includeUser(member, int(guild.id))
        apply_monthly_reward_coins(int(grant["id"]))


async def _apply_grant_role(guild, grant: dict, reference_time: datetime) -> bool:
    if (
        not grant.get("role_id")
        or int(grant.get("role_days") or 0) <= 0
        or grant.get("role_applied_at") is not None
        or grant.get("role_skipped_at") is not None
    ):
        return grant.get("role_applied_at") is not None

    grant_id = int(grant["id"])
    expiration = ensure_monthly_role_expiration(grant_id, reference_time)
    member = guild.get_member(int(grant["discord_user_id"]))
    if member is None:
        mark_monthly_role_skipped(grant_id, "Membro não está mais na guild")
        return False
    try:
        live_role = guild.get_role(int(grant["role_id"]))
    except (TypeError, ValueError):
        live_role = None
    if live_role is None:
        mark_monthly_role_skipped(grant_id, "Cargo de recompensa não existe mais")
        return False
    if resolve_assignable_bump_role(guild, grant["role_id"]) is None:
        raise RuntimeError("Cargo de recompensa existe, mas não está atribuível no momento")

    applied_now = await apply_monthly_reward_role(
        grant_id,
        guild,
        member,
        int(grant["role_id"]),
        expiration,
    )
    return applied_now or grant.get("role_applied_at") is not None


async def _attempt_grant_notification(guild, grant: dict, role_applied: bool) -> None:
    grant_id = int(grant["id"])
    if not reserve_monthly_notification(grant_id):
        return
    member = guild.get_member(int(grant["discord_user_id"]))
    if member is None:
        raise RuntimeError("Membro indisponível para notificação mensal")
    coins = int(grant.get("coins_amount") or 0)
    coins_text = f" e recebeu {coins} moedas" if coins > 0 else ""
    role_text = ""
    if role_applied:
        role_text = (
            f" e ganhou o cargo <@&{grant['role_id']}> "
            f"por {int(grant['role_days'])} dia(s)"
        )
    await member.send(
        f"Parabéns! Você ficou em {int(grant['rank_position'])}º no ranking mensal "
        f"de bump com {int(grant['bump_count'])} bump(s){coins_text}{role_text}."
    )
    mark_monthly_notification_sent(grant_id)


async def process_monthly_reward_grant(
    guild, grant: dict, reference_time: datetime | None = None
) -> None:
    current = reference_time or datetime.now(SAO_PAULO)
    grant_id = int(grant["id"])
    role_applied = grant.get("role_applied_at") is not None
    try:
        await _apply_grant_coins(guild, grant)
    except Exception as error:
        _record_grant_error(grant_id, error)
        logger.exception("Falha ao aplicar moedas mensais: grant_id=%s", grant_id)
    try:
        role_applied = await _apply_grant_role(guild, grant, current)
    except Exception as error:
        _record_grant_error(grant_id, error)
        logger.exception("Falha ao aplicar cargo mensal: grant_id=%s", grant_id)

    refreshed = next(
        (
            item
            for item in list_monthly_reward_grants(int(grant["run_id"]))
            if int(item["id"]) == grant_id
        ),
        grant,
    )
    if not grant_effects_are_terminal(refreshed):
        return
    if not monthly_grant_has_reward(refreshed):
        return
    role_applied = refreshed.get("role_applied_at") is not None or role_applied
    try:
        await _attempt_grant_notification(guild, refreshed, role_applied)
    except Exception as error:
        _record_grant_error(grant_id, error)
        logger.warning(
            "Falha na tentativa única de DM mensal: grant_id=%s",
            grant_id,
            exc_info=True,
        )


async def process_monthly_reward_run(
    bot, run: dict, reference_time: datetime | None = None
) -> None:
    run_id = int(run["id"])
    current = reference_time or datetime.now(SAO_PAULO)
    stage = "start_attempt"
    try:
        run = start_monthly_reward_attempt(run_id)
        if run.get("completed_at") is not None:
            return

        stage = "resolve_guild"
        guild = bot.get_guild(int(run["server_guild_id"]))
        if guild is None:
            raise RuntimeError("Guild não está disponível no cache do Discord")

        if run.get("ranking_prepared_at") is None:
            stage = "resolve_source_channel"
            channel = await resolve_monthly_source_channel(
                bot,
                guild,
                int(run["source_channel_id"]),
            )

            stage = "collect_history"
            ranking = await collect_monthly_bump_ranking(
                channel,
                _period_from_run(run),
            )
            eligible_ranking = filter_monthly_ranking_members(guild, ranking)

            stage = "prepare_ranking"
            prepare_monthly_reward_ranking(run_id, eligible_ranking)

        stage = "load_grants"
        grants = list_monthly_reward_grants(run_id)
        for grant in grants:
            try:
                await process_monthly_reward_grant(guild, grant, current)
            except Exception as error:
                _record_grant_error(int(grant["id"]), error)
                logger.exception(
                    "Falha isolada em concessão mensal: run_id=%s grant_id=%s error=%s",
                    run_id,
                    grant["id"],
                    sanitize_error(error),
                )

        stage = "complete_run"
        try_complete_monthly_reward_run(run_id)
    except Exception as error:
        _record_run_error(run_id, error)
        logger.exception(
            "Falha em execução mensal de bump: run_id=%s stage=%s guild_id=%s "
            "source_channel_id=%s period_start=%s period_end=%s error=%s",
            run_id,
            stage,
            run.get("server_guild_id"),
            run.get("source_channel_id"),
            run.get("period_start"),
            run.get("period_end"),
            sanitize_error(error),
        )


async def run_monthly_reward_cycle(bot, reference_time: datetime | None = None) -> None:
    """Resume pending work, then prepare a new run only during days 1-3."""
    current = reference_time or datetime.now(SAO_PAULO)
    processed: set[int] = set()
    try:
        incomplete = list_incomplete_monthly_reward_runs()
    except Exception:
        logger.exception("Falha ao listar execuções mensais pendentes")
        incomplete = []
    for run in incomplete:
        processed.add(int(run["id"]))
        try:
            await process_monthly_reward_run(bot, run, current)
        except Exception:
            logger.exception(
                "Falha não tratada isolada na execução mensal: run_id=%s", run["id"]
            )

    if not is_monthly_preparation_window(current):
        return
    period = previous_closed_month(current)
    try:
        configs = listBumpMonthlyRewardConfigs()
    except Exception:
        logger.exception("Falha ao listar configurações mensais")
        return
    for config in configs:
        if not config.get("disboardChannelId"):
            logger.warning(
                "Configuração mensal sem canal de origem: guild_id=%s",
                config.get("guildId"),
            )
            continue
        try:
            run = get_or_create_monthly_reward_run(config, period)
            run_id = int(run["id"])
            if run_id in processed or run.get("completed_at") is not None:
                continue
            processed.add(run_id)
            await process_monthly_reward_run(bot, run, current)
        except Exception:
            logger.exception(
                "Falha isolada ao preparar ranking mensal: guild_id=%s period_start=%s",
                config.get("guildId"),
                period.start,
            )


def save_monthly_bumps(guild_id: int, bumps: Dict[int, int]) -> None:
    """Persist bump counts in the legacy statistics view."""
    sql = (
        "INSERT INTO user_records (server_guild_id, user_id, bumps) "
        "VALUES (%s, %s, %s) "
        "ON DUPLICATE KEY UPDATE bumps = VALUES(bumps);"
    )
    for discord_user_id, count in bumps.items():
        try:
            internal_user_id = getUserId(discord_user_id)
        except Exception:
            logger.warning(
                "Falha ao resolver user_id interno para bump mensal: "
                "guild_id=%s discord_user_id=%s",
                guild_id,
                discord_user_id,
                exc_info=True,
            )
            continue
        if internal_user_id is None:
            logger.warning(
                "Usuário sem associação interna ignorado no bump mensal: "
                "guild_id=%s discord_user_id=%s",
                guild_id,
                discord_user_id,
            )
            continue
        try:
            with pooled_connection() as cursor:
                cursor.execute(sql, (guild_id, internal_user_id, count))
        except Exception:
            logger.warning(
                "Falha ao persistir bump mensal: guild_id=%s discord_user_id=%s "
                "internal_user_id=%s bumps=%s",
                guild_id,
                discord_user_id,
                internal_user_id,
                count,
                exc_info=True,
            )


def get_monthly_bump_records(guild_id: int, limit: int = 10) -> list[dict]:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT user_discord.discord_user_id, r.bumps AS bumps
            FROM user_records AS r
            JOIN user_discord ON user_discord.user_id = r.user_id
            WHERE r.server_guild_id = %s AND r.bumps IS NOT NULL
            ORDER BY r.bumps DESC LIMIT %s
            """,
            (guild_id, limit),
        )
        return [
            {"user_id": row["discord_user_id"], "bumps": row["bumps"]}
            for row in (cursor.fetchall() or [])
        ]
