"""Discord-only exceptional Portaria ticket operational persistence.

Database migration is owned by BraFurries-Database. The website only
configures enablement/category via API; it does not handle form decisions.
"""
from __future__ import annotations

from core.database import pooled_connection


ACTIVE_STATES = ("REQUESTED", "OPENING", "OPEN", "CLOSING")


def ticket_settings(guild_id: int) -> dict:
    """Read-only; missing config fails closed instead of creating implicit settings."""
    with pooled_connection() as cursor:
        cursor.execute(
            """SELECT clarification_tickets_enabled, clarification_ticket_category_id
               FROM portaria_base_config WHERE server_guild_id = %s""",
            (guild_id,),
        )
        return cursor.fetchone() or {}


def reserve_ticket(submission_id: int, guild_id: int, actor_id: int, reason: str) -> dict:
    """Lock the original form to serialize ticket requests. One ticket per form."""
    clean = (reason or "").strip()
    if not 10 <= len(clean) <= 1024:
        raise ValueError("Justificativa deve ter entre 10 e 1024 caracteres.")
    with pooled_connection() as cursor:
        cursor.execute(
            """SELECT fs.id, fs.user_id, cd.community_id
               FROM form_submissions fs
               INNER JOIN form_flows ff ON ff.id = fs.flow_id
               INNER JOIN community_discord cd ON cd.guild_id = fs.guild_id
               WHERE fs.id = %s AND fs.guild_id = %s
                 AND fs.status = 'pending' AND ff.type = 'portaria'
                 AND (ff.server_guild_id IS NULL OR ff.server_guild_id = fs.guild_id)
                 AND cd.active = 1
               FOR UPDATE""", (submission_id, guild_id))
        form = cursor.fetchone()
        if not form:
            raise ValueError("Ficha pendente de Portaria não encontrada neste servidor.")
        cursor.execute(
            """SELECT clarification_tickets_enabled, clarification_ticket_category_id
               FROM portaria_base_config WHERE server_guild_id = %s FOR UPDATE""",
            (guild_id,))
        config = cursor.fetchone()
        if not config or not config.get("clarification_tickets_enabled") or not config.get("clarification_ticket_category_id"):
            raise ValueError("Tickets de esclarecimento não estão habilitados neste servidor.")
        # The applicant, not a particular form submission, owns the active
        # ticket slot in this Community/guild. CLOSED records remain history.
        cursor.execute(
            """SELECT id, submission_id, status, discord_channel_id,
                      applicant_discord_user_id
               FROM portaria_clarification_tickets
               WHERE community_id = %s AND guild_id = %s
                 AND applicant_discord_user_id = %s AND status <> 'CLOSED'
               ORDER BY id DESC LIMIT 1 FOR UPDATE""",
            (form["community_id"], guild_id, form["user_id"]))
        existing = cursor.fetchone()
        if existing:
            if int(existing["submission_id"]) != int(submission_id):
                raise ValueError(
                    "Este candidato já possui um atendimento não finalizado "
                    "em outra ficha deste servidor."
                )
            return existing
        cursor.execute(
            """INSERT INTO portaria_clarification_tickets
                (community_id, guild_id, submission_id, applicant_discord_user_id,
                 requested_by_discord_user_id, reason)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (form["community_id"], guild_id, submission_id, form["user_id"], actor_id, clean))
        return {
            "id": int(cursor.lastrowid),
            "status": "REQUESTED",
            "discord_channel_id": None,
            "applicant_discord_user_id": form["user_id"],
        }


def get_ticket(ticket_id: int, guild_id: int) -> dict | None:
    with pooled_connection() as cursor:
        cursor.execute(
            """SELECT id, guild_id, submission_id, applicant_discord_user_id,
                      status, discord_channel_id
               FROM portaria_clarification_tickets
               WHERE id = %s AND guild_id = %s""",
            (ticket_id, guild_id))
        return cursor.fetchone()


def list_open_tickets() -> list[dict]:
    with pooled_connection() as cursor:
        cursor.execute(
            """SELECT id, guild_id, discord_channel_id
               FROM portaria_clarification_tickets
               WHERE status IN ('OPEN', 'CLOSING')
                 AND discord_channel_id IS NOT NULL""")
        return cursor.fetchall() or []



def list_recoverable_tickets(*, limit: int = 8) -> list[dict]:
    """Bounded scan for channels created before an interrupted DB/Discord flow."""
    with pooled_connection() as cursor:
        cursor.execute(
            """SELECT id, guild_id, submission_id, discord_channel_id,
                      applicant_discord_user_id, status
               FROM portaria_clarification_tickets
               WHERE discord_channel_id IS NOT NULL
                 AND status IN ('FAILED','OPENING')
                 AND opening_attempted_at < UTC_TIMESTAMP(6) - INTERVAL 10 MINUTE
               ORDER BY requested_at ASC LIMIT %s""",
            (min(20, max(1, limit)),))
        return cursor.fetchall() or []



def has_active_ticket(submission_id: int, guild_id: int) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            """SELECT id FROM portaria_clarification_tickets
               WHERE guild_id = %s
                 AND applicant_discord_user_id = (
                    SELECT fs.user_id FROM form_submissions fs
                    WHERE fs.id = %s AND fs.guild_id = %s
                 )
                 AND (status IN ('REQUESTED','OPENING','OPEN','CLOSING')
                      OR (status = 'FAILED' AND discord_channel_id IS NOT NULL))
               LIMIT 1""", (guild_id, submission_id, guild_id))
        return cursor.fetchone() is not None



def reset_missing_open_channel(ticket_id: int, guild_id: int, channel_id: int) -> bool:
    """Mark an OPEN ticket retriable only after Discord confirmed channel absence.

    Keep the ticket row/ID, so the next request cannot create a second ticket
    for the same form. This is intentionally a compare-and-swap against the
    exact old Discord channel ID and guild.
    """
    with pooled_connection() as cursor:
        cursor.execute(
            """UPDATE portaria_clarification_tickets
               SET status = 'FAILED', discord_channel_id = NULL,
                   last_error_code = 'CHANNEL_NOT_FOUND',
                   version = version + 1
               WHERE id = %s AND guild_id = %s
                 AND discord_channel_id = %s AND status = 'OPEN'""",
            (ticket_id, guild_id, channel_id))
        return cursor.rowcount == 1


def claim_open(ticket_id: int, guild_id: int) -> bool:
    """Lease prevents overlapping Discord creates and enables crash recovery."""
    with pooled_connection() as cursor:
        cursor.execute(
            """UPDATE portaria_clarification_tickets
               SET status = 'OPENING', opening_attempted_at = UTC_TIMESTAMP(6),
                   last_error_code = NULL, version = version + 1
               WHERE id = %s AND guild_id = %s
                 AND (status IN ('REQUESTED','FAILED')
                      OR (status = 'OPENING' AND
                          opening_attempted_at < UTC_TIMESTAMP(6) - INTERVAL 10 MINUTE))""",
            (ticket_id, guild_id))
        return cursor.rowcount == 1


def remember_channel(ticket_id: int, guild_id: int, channel_id: int) -> bool:
    """Persist Discord's side effect before posting the close controls.

    When channel creation succeeds but posting fails, keeping its ID blocks
    a competing Portaria decision and makes a retry recover the same channel.
    """
    with pooled_connection() as cursor:
        cursor.execute(
            """UPDATE portaria_clarification_tickets
               SET discord_channel_id = %s, version = version + 1
               WHERE id = %s AND guild_id = %s AND status = 'OPENING'
                 AND (discord_channel_id IS NULL OR discord_channel_id = %s)""",
            (channel_id, ticket_id, guild_id, channel_id))
        return cursor.rowcount == 1


def mark_open(ticket_id: int, guild_id: int, channel_id: int) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            """UPDATE portaria_clarification_tickets
               SET status = 'OPEN', discord_channel_id = %s,
                   opened_at = COALESCE(opened_at, UTC_TIMESTAMP(6)),
                   last_error_code = NULL, version = version + 1
               WHERE id = %s AND guild_id = %s AND status = 'OPENING'""",
            (channel_id, ticket_id, guild_id))
        return cursor.rowcount == 1


def mark_open_failed(ticket_id: int, guild_id: int, code: str) -> None:
    with pooled_connection() as cursor:
        cursor.execute(
            """UPDATE portaria_clarification_tickets
               SET status = 'FAILED', last_error_code = %s, version = version + 1
               WHERE id = %s AND guild_id = %s AND status = 'OPENING'""",
            (code[:64], ticket_id, guild_id))


def claim_close(ticket_id: int, guild_id: int, actor_id: int, reason: str) -> bool:
    """Save the closing intent *before* revoking Discord access.

    If Discord succeeds and the database commit fails, a later recovery can
    complete the same ticket with the original actor/reason. Stale retries may
    refresh the lease but must not overwrite the initial closing intent.
    """
    clean = (reason or "").strip()
    if not 10 <= len(clean) <= 1024:
        raise ValueError("Informe um motivo de 10 a 1024 caracteres.")
    with pooled_connection() as cursor:
        cursor.execute(
            """UPDATE portaria_clarification_tickets
               SET status = 'CLOSING', closing_attempted_at = UTC_TIMESTAMP(6),
                   closed_by_discord_user_id = COALESCE(closed_by_discord_user_id, %s),
                   close_reason = COALESCE(close_reason, %s),
                   version = version + 1
               WHERE id = %s AND guild_id = %s
                 AND (status = 'OPEN'
                      OR (status = 'CLOSING' AND
                          closing_attempted_at < UTC_TIMESTAMP(6) - INTERVAL 10 MINUTE))""",
            (actor_id, clean, ticket_id, guild_id))
        return cursor.rowcount == 1


def list_stale_closing_tickets(*, limit: int = 8) -> list[dict]:
    """Only recover durable closing intents; older rows need Staff intervention."""
    with pooled_connection() as cursor:
        cursor.execute(
            """SELECT id, guild_id, submission_id, discord_channel_id,
                      applicant_discord_user_id, closed_by_discord_user_id, close_reason
               FROM portaria_clarification_tickets
               WHERE status = 'CLOSING' AND discord_channel_id IS NOT NULL
                 AND closed_by_discord_user_id IS NOT NULL AND close_reason IS NOT NULL
                 AND closing_attempted_at < UTC_TIMESTAMP(6) - INTERVAL 10 MINUTE
               ORDER BY closing_attempted_at ASC LIMIT %s""",
            (min(20, max(1, limit)),))
        return cursor.fetchall() or []


def mark_closed(ticket_id: int, guild_id: int, actor_id: int, reason: str) -> bool:
    clean = (reason or "").strip()
    if not 10 <= len(clean) <= 1024:
        raise ValueError("Informe um motivo de 10 a 1024 caracteres.")
    with pooled_connection() as cursor:
        cursor.execute(
            """UPDATE portaria_clarification_tickets
               SET status = 'CLOSED', closed_at = UTC_TIMESTAMP(6),
                   closed_by_discord_user_id = %s, close_reason = %s,
                   version = version + 1
               WHERE id = %s AND guild_id = %s AND status = 'CLOSING'""",
            (actor_id, clean, ticket_id, guild_id))
        return cursor.rowcount == 1


def restore_open(ticket_id: int, guild_id: int) -> None:
    with pooled_connection() as cursor:
        cursor.execute(
            """UPDATE portaria_clarification_tickets
               SET status = 'OPEN', closing_attempted_at = NULL,
                   closed_by_discord_user_id = NULL, close_reason = NULL,
                   version = version + 1
               WHERE id = %s AND guild_id = %s AND status = 'CLOSING'""",
            (ticket_id, guild_id))
