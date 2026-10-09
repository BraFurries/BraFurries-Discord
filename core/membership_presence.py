"""DML-only membership presence helpers. Call reconciliation only with a complete guild view."""

from __future__ import annotations

from datetime import datetime
import logging

from core.database import (
    _get_community_id,
    _refresh_identity_ban_membership_flags,
    pooled_connection,
)

logger = logging.getLogger(__name__)


def begin_identity_unban(
    guild_id: int,
    discord_user_id: int,
    revoked_by_discord_id: int | None,
    reason: str | None,
) -> list[dict] | None:
    """Revoke active identity-wide ban actions and return sibling APPLIED effects to unban."""
    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)

        cursor.execute(
            """
            SELECT DISTINCT bans.id AS ban_id
            FROM user_bans bans
            WHERE bans.community_id = %s
              AND bans.revoked_at IS NULL
              AND (bans.valid_until IS NULL OR bans.valid_until >= CURDATE())
              AND (
                  EXISTS (
                      SELECT 1
                      FROM user_ban_discord_effects effects
                      WHERE effects.ban_id = bans.id
                        AND effects.discord_user_id = %s
                        AND effects.outcome IN ('APPLIED', 'ALREADY_BANNED')
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM user_discord direct_account
                      WHERE direct_account.user_id = bans.user_id
                        AND direct_account.discord_user_id = %s
                  )
              )
            ORDER BY bans.id
            """,
            (community_id, discord_user_id, discord_user_id),
        )
        ban_ids = [
            int(row["ban_id"])
            for row in (cursor.fetchall() or [])
        ]
        if not ban_ids:
            return None

        revoked_by = None
        if revoked_by_discord_id is not None:
            cursor.execute(
                "SELECT user_id FROM user_discord WHERE discord_user_id = %s",
                (revoked_by_discord_id,),
            )
            actor = cursor.fetchone()
            revoked_by = actor["user_id"] if actor else None

        placeholders = ", ".join(["%s"] * len(ban_ids))
        cursor.execute(
            f"""
            SELECT id AS effect_id, ban_id, discord_user_id
            FROM user_ban_discord_effects
            WHERE ban_id IN ({placeholders})
              AND outcome = 'APPLIED'
              AND reverted_at IS NULL
            ORDER BY discord_user_id, effect_id
            """,
            tuple(ban_ids),
        )
        applied_effects = list(cursor.fetchall() or [])

        cursor.execute(
            f"""
            UPDATE user_ban_discord_effects
            SET reverted_at = CURRENT_TIMESTAMP(6),
                revert_outcome = 'REVERTED'
            WHERE ban_id IN ({placeholders})
              AND discord_user_id = %s
              AND outcome = 'APPLIED'
              AND reverted_at IS NULL
            """,
            (*ban_ids, discord_user_id),
        )

        cursor.execute(
            f"""
            UPDATE user_bans
            SET revoked_at = CURRENT_TIMESTAMP(6),
                revoked_by = %s,
                revocation_reason = %s
            WHERE id IN ({placeholders})
              AND revoked_at IS NULL
            """,
            (
                revoked_by,
                reason or "Unban manual de identidade confirmada",
                *ban_ids,
            ),
        )

        for ban_id in ban_ids:
            _refresh_identity_ban_membership_flags(
                cursor,
                ban_id,
                community_id,
            )

        grouped: dict[int, list[int]] = {}
        for effect in applied_effects:
            effect_discord_id = int(effect["discord_user_id"])
            if effect_discord_id == discord_user_id:
                continue
            grouped.setdefault(effect_discord_id, []).append(
                int(effect["effect_id"])
            )

        return [
            {
                "discord_user_id": effect_discord_id,
                "effect_ids": effect_ids,
            }
            for effect_discord_id, effect_ids in sorted(grouped.items())
        ]


def mark_member_removed(guild_id: int, discord_user_id: int, observed_at: datetime | None = None) -> bool:
    """Record an observed leave for an already-linked Discord user; never creates identities."""
    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        cursor.execute("SELECT user_id FROM user_discord WHERE discord_user_id = %s", (discord_user_id,))
        linked = cursor.fetchone()
        if not linked:
            logger.info("Saída ignorada: Discord user %s não possui vínculo interno", discord_user_id)
            return False
        cursor.execute(
            "UPDATE user_community_status SET is_present = FALSE, left_at = %s WHERE user_id = %s AND community_id = %s",
            ((observed_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"), linked["user_id"], community_id),
        )
        if cursor.rowcount != 1:
            return False
        return True


def reconcile_membership_presence(guild_id: int, complete_member_ids: set[int]) -> int:
    """Reconcile only after the caller has established a complete guild member view.

    Legacy absences intentionally retain left_at=NULL because their leave timestamp is unknown.
    """
    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        cursor.execute(
            "SELECT ucs.user_id, ud.discord_user_id FROM user_community_status ucs "
            "JOIN user_discord ud ON ud.user_id = ucs.user_id WHERE ucs.community_id = %s",
            (community_id,),
        )
        rows = cursor.fetchall()
        for row in rows:
            if row["discord_user_id"] in complete_member_ids:
                cursor.execute("UPDATE user_community_status SET is_present = TRUE, left_at = NULL WHERE user_id = %s AND community_id = %s", (row["user_id"], community_id))
            elif row["discord_user_id"] is not None:
                cursor.execute("UPDATE user_community_status SET is_present = FALSE WHERE user_id = %s AND community_id = %s", (row["user_id"], community_id))
        return len(rows)


def record_unban(guild_id: int, discord_user_id: int, revoked_by_discord_id: int | None, reason: str | None) -> bool:
    """Idempotently revoke exactly one active ban; ambiguity is intentionally not mutated."""
    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        cursor.execute("SELECT user_id FROM user_discord WHERE discord_user_id = %s", (discord_user_id,))
        subject = cursor.fetchone()
        if not subject:
            return False
        cursor.execute(
            "SELECT id FROM user_bans WHERE user_id = %s AND community_id = %s AND revoked_at IS NULL "
            "AND (valid_until IS NULL OR valid_until >= CURDATE()) ORDER BY id DESC",
            (subject["user_id"], community_id),
        )
        bans = cursor.fetchall()
        if len(bans) != 1:
            logger.warning("Unban não registrado para user=%s community=%s: %s bans ativos", subject["user_id"], community_id, len(bans))
            return False
        revoked_by = None
        if revoked_by_discord_id is not None:
            cursor.execute("SELECT user_id FROM user_discord WHERE discord_user_id = %s", (revoked_by_discord_id,))
            actor = cursor.fetchone()
            revoked_by = actor["user_id"] if actor else None
        cursor.execute(
            "UPDATE user_bans SET revoked_at = %s, revoked_by = %s, revocation_reason = %s WHERE id = %s AND revoked_at IS NULL",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), revoked_by, reason, bans[0]["id"]),
        )
        if cursor.rowcount != 1:
            return False
        cursor.execute(
            "UPDATE user_community_status SET banned = FALSE WHERE user_id = %s AND community_id = %s",
            (subject["user_id"], community_id),
        )
        return True
