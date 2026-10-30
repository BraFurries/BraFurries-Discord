"""Reconcile API-owned temporary Discord grants without mutating legacy rewards.

API is the authority for administrative intent; Coddy owns Discord effects.
No DDL, no administrative policy writes and no cross-guild operations here.
"""
from __future__ import annotations

import logging
import discord
from datetime import datetime, timezone
from core.database import pooled_connection
from core.temp_role_locks import temp_role_lock

log = logging.getLogger(__name__)



def _backfill_legacy_identity() -> None:
    """Repair legacy writes made between the schema deployment and bot rollout."""
    with pooled_connection() as cursor:
        cursor.execute(
            """UPDATE user_temp_roles t
               JOIN community_discord cd ON cd.id=t.disc_community_id
               JOIN user_discord ud ON ud.id=t.disc_user_id
               SET t.community_id=cd.community_id, t.guild_id=cd.guild_id,
                   t.discord_user_id=ud.discord_user_id
               WHERE t.grant_source='LEGACY' AND
                 (t.community_id IS NULL OR t.guild_id IS NULL
                  OR t.discord_user_id IS NULL)"""
        )


def transfer_owned_legacy_to_manual(
    legacy_grant_id: int, guild_id: int, discord_user_id: int, role_id: int
) -> None:
    """Only transfer a legacy-owned role to an active manual grant, never an unknown one."""
    with pooled_connection() as cursor:
        cursor.execute(
            """UPDATE user_temp_roles manual
               JOIN user_temp_roles legacy ON legacy.id=%s
               SET manual.role_added_by_bot=1
               WHERE legacy.grant_source='LEGACY'
                 AND legacy.role_added_by_bot=1
                 AND manual.grant_source='MANUAL'
                 AND manual.disc_community_id=legacy.disc_community_id
                 AND manual.guild_id=%s AND manual.discord_user_id=%s
                 AND manual.role_id=%s AND manual.status='ACTIVE'
                 AND manual.expiring_date>UTC_TIMESTAMP(6)""",
            (legacy_grant_id,guild_id,discord_user_id,role_id),
        )


def _due_grants(limit: int = 20) -> list[dict]:
    with pooled_connection() as cursor:
        # Expiration is durable even across bot restarts. Only Coddy changes
        # operational status; no Web user impersonation is involved.
        cursor.execute(
            """UPDATE user_temp_roles
               SET status='EXPIRED',sync_status='PENDING',version=version+1
               WHERE grant_source='MANUAL' AND status='ACTIVE' AND expiring_date<=UTC_TIMESTAMP(6)
               ORDER BY expiring_date,id LIMIT 100"""
        )
        cursor.execute(
            """SELECT g.id,g.community_id,g.guild_id,g.discord_user_id,g.role_id,
                      g.status,g.sync_status,g.expiring_date AS expires_at,g.role_added_by_bot,g.version
               FROM user_temp_roles g
               JOIN community_discord cd ON cd.community_id=g.community_id
                    AND cd.guild_id=g.guild_id AND cd.active=1
               WHERE g.grant_source='MANUAL' AND (g.sync_status='PENDING'
                  OR (g.sync_status='FAILED'
                      AND g.updated_at<UTC_TIMESTAMP(6)-INTERVAL 5 MINUTE)
                  OR (g.status='ACTIVE' AND g.sync_status='APPLIED'
                      AND (g.last_checked_at IS NULL OR
                           g.last_checked_at<UTC_TIMESTAMP(6)-INTERVAL 10 MINUTE))
                  OR (g.sync_status='PROCESSING'
                      AND g.last_checked_at<UTC_TIMESTAMP(6)-INTERVAL 5 MINUTE))
               ORDER BY CASE WHEN g.sync_status='PENDING' THEN 0
                             WHEN g.sync_status='PROCESSING' THEN 1
                             WHEN g.sync_status='FAILED' THEN 2 ELSE 3 END,
                        g.last_checked_at,g.id
               LIMIT %s""",
            (max(1, min(limit, 100)),),
        )
        return cursor.fetchall() or []


def _claim(row: dict) -> int | None:
    # Distributed compare-and-set lease. Concurrent workers cannot claim the
    # same version, and an API renewal/cancellation invalidates the result.
    with pooled_connection() as cursor:
        cursor.execute(
            """UPDATE user_temp_roles
               SET sync_status='PROCESSING',last_checked_at=UTC_TIMESTAMP(6),
                   version=version+1
               WHERE id=%s AND community_id=%s AND guild_id=%s AND grant_source='MANUAL' AND version=%s
               AND status=%s AND sync_status=%s
               AND (status<>'ACTIVE' OR expiring_date>UTC_TIMESTAMP(6))
               AND (sync_status<>'PROCESSING' OR
                    last_checked_at<UTC_TIMESTAMP(6)-INTERVAL 5 MINUTE)""",
            (row["id"], row["community_id"], row["guild_id"], row["version"],
             row["status"], row["sync_status"]),
        )
        return int(row["version"])+1 if cursor.rowcount==1 else None


def _finish(row: dict, claimed_version: int, *, added: bool, error: str | None) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            """UPDATE user_temp_roles
               SET sync_status=%s,last_error_code=%s,
                   role_added_by_bot=CASE WHEN role_added_by_bot=1 THEN 1 ELSE %s END,
                   applied_at=CASE WHEN %s IS NULL THEN UTC_TIMESTAMP(6) ELSE applied_at END,
                   last_checked_at=UTC_TIMESTAMP(6),version=version+1
               WHERE id=%s AND community_id=%s AND guild_id=%s AND grant_source='MANUAL' AND version=%s
                 AND status=%s AND sync_status='PROCESSING'""",
            ("FAILED" if error else "APPLIED", error, int(added), error,
             row["id"], row["community_id"], row["guild_id"], claimed_version,
             row["status"]),
        )
        return cursor.rowcount==1


def _remember_added_after_race(row: dict) -> None:
    """A successful Discord add must remain owned if API changed the grant.

    A later cancellation or renewal can race between the Discord side effect
    and _finish. Preserve only confirmed additions; never claim ownership of
    pre-existing member roles.
    """
    with pooled_connection() as cursor:
        cursor.execute(
            """UPDATE user_temp_roles
               SET role_added_by_bot=1
               WHERE grant_source='MANUAL' AND id=%s AND community_id=%s AND guild_id=%s
                 AND role_added_by_bot=0""",
            (row["id"],row["community_id"],row["guild_id"]),
        )


def _other_active_grant(row: dict) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            """SELECT 1 FROM user_temp_roles t
               LEFT JOIN user_discord ud ON ud.id=t.disc_user_id
               WHERE t.community_id=%s AND t.guild_id=%s
                 AND (t.discord_user_id=%s OR ud.discord_user_id=%s)
                 AND t.role_id=%s AND t.id<>%s AND t.status='ACTIVE'
                 AND t.expiring_date>UTC_TIMESTAMP(6) LIMIT 1""",
            (row["community_id"],row["guild_id"],row["discord_user_id"],
             row["discord_user_id"],row["role_id"],row["id"]),
        )
        if cursor.fetchone():
            if row["role_added_by_bot"]:
                # Ownership of a role previously added by this manual source
                # moves to the newer ACTIVE manual grant. The newer grant
                # must remove it when it ultimately ends.
                cursor.execute(
                    """UPDATE user_temp_roles
                       SET role_added_by_bot=1
                       WHERE grant_source='MANUAL' AND community_id=%s AND guild_id=%s
                         AND discord_user_id=%s AND role_id=%s AND id<>%s
                         AND grant_source='MANUAL' AND status='ACTIVE' AND expiring_date>UTC_TIMESTAMP(6)""",
                    (row["community_id"],row["guild_id"],row["discord_user_id"],
                     row["role_id"],row["id"]),
                )
            return True
        return False


def has_active_manual_grant(
    community_id: int, guild_id: int, discord_user_id: int, role_id: int
) -> bool:
    """Called by legacy expiration before removing a shared Discord role."""
    with pooled_connection() as cursor:
        cursor.execute(
            """SELECT 1 FROM user_temp_roles g
               JOIN community_discord cd ON cd.community_id=g.community_id
                 AND cd.guild_id=g.guild_id
               WHERE cd.id=%s AND g.guild_id=%s AND g.discord_user_id=%s
                 AND g.role_id=%s AND g.grant_source='MANUAL' AND g.status='ACTIVE'
                 AND g.expiring_date>UTC_TIMESTAMP(6) LIMIT 1""",
            (community_id,guild_id,discord_user_id,role_id),
        )
        return cursor.fetchone() is not None


async def reconcile_grant(bot, row: dict) -> None:
    guild_id=int(row["guild_id"])
    user_id=int(row["discord_user_id"])
    role_id=int(row["role_id"])
    async with temp_role_lock(guild_id,user_id,role_id):
        # Do not add an already expired role just because its row was
        # selected immediately before the DB expiry transition.
        if row["status"]=="ACTIVE" and row.get("expires_at") is not None:
            expiry=row["expires_at"]
            if expiry.tzinfo is None:
                expiry=expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                return
        claimed=_claim(row)
        if claimed is None:
            return
        added=bool(row["role_added_by_bot"])
        added_now=False
        error: str | None=None
        try:
            guild=bot.get_guild(guild_id)
            if guild is None:
                raise ValueError("GUILD_UNAVAILABLE")
            role=guild.get_role(role_id)
            if role is None:
                # Missing role is harmless when we are cancelling/expiring.
                if row["status"]=="ACTIVE":
                    raise ValueError("ROLE_NOT_FOUND")
            member=guild.get_member(user_id)
            if member is None:
                try:
                    member=await guild.fetch_member(user_id)
                except discord.NotFound:
                    if row["status"]=="ACTIVE":
                        raise ValueError("MEMBER_NOT_FOUND")
                except discord.Forbidden:
                    raise ValueError("MEMBER_LOOKUP_FORBIDDEN")
                except discord.HTTPException:
                    raise ValueError("MEMBER_LOOKUP_FAILED")
            if member is not None and role is not None:
                bot_member=guild.me
                if bot_member is None or not bot_member.guild_permissions.manage_roles:
                    raise ValueError("MISSING_MANAGE_ROLES")
                if role.managed or role.is_default() or role >= bot_member.top_role:
                    raise ValueError("ROLE_HIERARCHY")
                if row["status"]=="ACTIVE":
                    if role not in member.roles:
                        await member.add_roles(role,reason=f"BraFurries: cargo temporário #{row['id']}")
                        added=True
                        added_now=True
                elif added and role in member.roles and not _other_active_grant(row):
                    await member.remove_roles(role,reason=f"BraFurries: cargo temporário encerrado #{row['id']}")
            # A missing member on revoke means there is no accessible Discord
            # side effect left to perform. History remains durable.
        except ValueError as exc:
            error=str(exc)
        except Exception:
            error="DISCORD_OPERATION_ERROR"
            log.exception("Temporary role sync failed guild=%s grant=%s",
                          guild_id,row["id"])
        try:
            finished=_finish(row,claimed,added=added,error=error)
        except Exception:
            log.exception("Temporary role result persistence failed guild=%s grant=%s",
                          guild_id,row["id"])
            finished=False
        if not finished and added_now:
            try:
                _remember_added_after_race(row)
            except Exception:
                log.exception("Manual role may need ownership reconciliation guild=%s grant=%s",
                              guild_id,row["id"])
        if not finished:
            log.info("Temporary role result requires retry guild=%s grant=%s",
                     guild_id,row["id"])
        elif error:
            log.warning("Temporary role awaiting retry guild=%s grant=%s reason=%s",
                        guild_id,row["id"],error)


async def reconcile_temporary_roles(bot, *, limit: int = 20) -> None:
    try:
        _backfill_legacy_identity()
        rows=_due_grants(limit)
    except Exception:
        log.exception("Cannot load manual temporary role desired state")
        return
    for row in rows:
        await reconcile_grant(bot,row)
