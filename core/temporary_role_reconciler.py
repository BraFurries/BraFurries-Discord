"""Reconcile API-owned temporary Discord grants without mutating legacy rewards.

API is the authority for administrative intent; Coddy owns Discord effects.
No DDL, no administrative policy writes and no cross-guild operations here.
"""
from __future__ import annotations

import logging
from core.database import pooled_connection
from core.temp_role_locks import temp_role_lock

log = logging.getLogger(__name__)


def _due_grants(limit: int = 20) -> list[dict]:
    with pooled_connection() as cursor:
        # Expiration is durable even across bot restarts. Only Coddy changes
        # operational status; no Web user impersonation is involved.
        cursor.execute(
            """UPDATE community_temporary_role_grants
               SET status='EXPIRED',sync_status='PENDING',version=version+1
               WHERE status='ACTIVE' AND expires_at<=UTC_TIMESTAMP(6)
               ORDER BY expires_at,id LIMIT 100"""
        )
        cursor.execute(
            """SELECT g.id,g.community_id,g.guild_id,g.discord_user_id,g.role_id,
                      g.status,g.sync_status,g.expires_at,g.role_added_by_bot,g.version
               FROM community_temporary_role_grants g
               JOIN community_discord cd ON cd.community_id=g.community_id
                    AND cd.guild_id=g.guild_id AND cd.active=1
               WHERE g.sync_status='PENDING'
                  OR (g.sync_status='FAILED'
                      AND g.updated_at<UTC_TIMESTAMP(6)-INTERVAL 5 MINUTE)
                  OR (g.status='ACTIVE' AND g.sync_status='APPLIED'
                      AND (g.last_checked_at IS NULL OR
                           g.last_checked_at<UTC_TIMESTAMP(6)-INTERVAL 10 MINUTE))
               ORDER BY CASE WHEN g.sync_status='PENDING' THEN 0
                             WHEN g.sync_status='FAILED' THEN 1 ELSE 2 END,
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
            """UPDATE community_temporary_role_grants SET version=version+1
               WHERE id=%s AND community_id=%s AND guild_id=%s AND version=%s
               AND status=%s AND sync_status=%s""",
            (row["id"], row["community_id"], row["guild_id"], row["version"],
             row["status"], row["sync_status"]),
        )
        return int(row["version"])+1 if cursor.rowcount==1 else None


def _finish(row: dict, claimed_version: int, *, added: bool, error: str | None) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            """UPDATE community_temporary_role_grants
               SET sync_status=%s,last_error_code=%s,role_added_by_bot=%s,
                   applied_at=CASE WHEN %s IS NULL THEN UTC_TIMESTAMP(6) ELSE applied_at END,
                   last_checked_at=UTC_TIMESTAMP(6),version=version+1
               WHERE id=%s AND community_id=%s AND guild_id=%s AND version=%s
                 AND status=%s""",
            ("FAILED" if error else "APPLIED", error, int(added), error,
             row["id"], row["community_id"], row["guild_id"], claimed_version,
             row["status"]),
        )
        return cursor.rowcount==1


def _other_active_grant(row: dict) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            """SELECT 1 FROM community_temporary_role_grants
               WHERE community_id=%s AND guild_id=%s AND discord_user_id=%s
                 AND role_id=%s AND id<>%s AND status='ACTIVE'
                 AND expires_at>UTC_TIMESTAMP(6) LIMIT 1""",
            (row["community_id"],row["guild_id"],row["discord_user_id"],
             row["role_id"],row["id"]),
        )
        if cursor.fetchone():
            return True
        # The original legacy system handles bumps/rewards independently.
        # Never revoke a role while a legacy grant still covers the target.
        cursor.execute(
            """SELECT 1 FROM user_temp_roles t
               JOIN community_discord cd ON cd.id=t.disc_community_id
               JOIN user_discord ud ON ud.id=t.disc_user_id
               WHERE cd.community_id=%s AND cd.guild_id=%s
                 AND ud.discord_user_id=%s AND t.role_id=%s
                 AND t.expiring_date>NOW() LIMIT 1""",
            (row["community_id"],row["guild_id"],
             row["discord_user_id"],row["role_id"]),
        )
        return cursor.fetchone() is not None


def has_active_manual_grant(
    community_id: int, guild_id: int, discord_user_id: int, role_id: int
) -> bool:
    """Called by legacy expiration before removing a shared Discord role."""
    with pooled_connection() as cursor:
        cursor.execute(
            """SELECT 1 FROM community_temporary_role_grants
               WHERE community_id=%s AND guild_id=%s AND discord_user_id=%s
                 AND role_id=%s AND status='ACTIVE'
                 AND expires_at>UTC_TIMESTAMP(6) LIMIT 1""",
            (community_id,guild_id,discord_user_id,role_id),
        )
        return cursor.fetchone() is not None


async def reconcile_grant(bot, row: dict) -> None:
    guild_id=int(row["guild_id"])
    user_id=int(row["discord_user_id"])
    role_id=int(row["role_id"])
    async with temp_role_lock(guild_id,user_id,role_id):
        claimed=_claim(row)
        if claimed is None:
            return
        added=bool(row["role_added_by_bot"])
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
                except Exception:
                    if row["status"]=="ACTIVE":
                        raise ValueError("MEMBER_NOT_FOUND")
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
        if not _finish(row,claimed,added=added,error=error):
            log.info("Temporary role changed concurrently; retry next scan grant=%s",
                     row["id"])
        elif error:
            log.warning("Temporary role awaiting retry guild=%s grant=%s reason=%s",
                        guild_id,row["id"],error)


async def reconcile_temporary_roles(bot, *, limit: int = 20) -> None:
    try:
        rows=_due_grants(limit)
    except Exception:
        log.exception("Cannot load manual temporary role desired state")
        return
    for row in rows:
        await reconcile_grant(bot,row)
