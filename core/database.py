from schemas.models.user import User, CustomRole, Warning, UserNote
from schemas.enums.server_messages import ServerMessagesEnum
from schemas.models.user import SimpleUserBirthday
from mysql.connector.cursor import MySQLCursorAbstract
from core.utilities import snake_to_camel
from core.service_commands import has_service_command
from mysql.connector import pooling, errorcode
from mysql.connector.errors import DatabaseError, IntegrityError, ProgrammingError
import mysql.connector
from datetime import date, datetime, timedelta
import calendar
from typing import Union, Optional, Literal, Any
from collections.abc import Mapping, Sequence
from decimal import Decimal
from threading import RLock
from enum import Enum
import json
import base64
import hashlib
from core.verifications import validate_birthdate
import discord
import os
import dotenv
import time
from contextlib import contextmanager
from contextlib import asynccontextmanager
import logging
import unicodedata
from cryptography.fernet import Fernet, InvalidToken
import asyncio
import aiomysql
from core.runtime_config import (
    get_database_name,
)
from core.temp_role_locks import temp_role_lock

logger = logging.getLogger(__name__)

dotenv_file = dotenv.find_dotenv()
dotenv.load_dotenv(dotenv_file)

db_config = {
    "host": os.getenv("BOT_DATABASE_HOST"),
    "user": os.getenv("BOT_DATABASE_USER"),
    "password": os.getenv("BOT_DATABASE_PASSWORD"),
    "database": get_database_name(),
}
    
_connection_pool_lock = RLock()


def _create_connection_pool() -> pooling.MySQLConnectionPool:
    return pooling.MySQLConnectionPool(
        pool_name="Discord",
        pool_size=10,
        pool_reset_session=False,
        **db_config,
    )


connection_pool = _create_connection_pool()
_async_pool: aiomysql.Pool | None = None
_async_pool_lock = asyncio.Lock()


def _refresh_connection_pool() -> None:
    global connection_pool
    with _connection_pool_lock:
        connection_pool = _create_connection_pool()


async def _get_async_pool() -> aiomysql.Pool:
    global _async_pool
    if _async_pool is not None:
        return _async_pool
    async with _async_pool_lock:
        if _async_pool is None:
            _async_pool = await aiomysql.create_pool(
                host=db_config["host"],
                user=db_config["user"],
                password=db_config["password"],
                db=db_config["database"],
                minsize=1,
                maxsize=20,
                autocommit=False,
                charset="utf8mb4",
            )
    return _async_pool


async def close_async_pool() -> None:
    global _async_pool
    if _async_pool is None:
        return
    _async_pool.close()
    await _async_pool.wait_closed()
    _async_pool = None


DISPLAY_NAME_MAX_LENGTH = 100


def normalize_text(text: Optional[str], max_length: int = DISPLAY_NAME_MAX_LENGTH) -> str:
    """Return NFC-normalized Unicode text that fits the persisted name columns.

    Slicing a Python string operates on Unicode code points rather than encoded
    UTF-8 bytes, so the result can never contain a partial encoded character.
    """

    if text is None:
        return ""

    return unicodedata.normalize("NFC", text)[:max_length]


@contextmanager
def pooled_connection(buffered: bool = True):
    """Yield a pooled dictionary cursor with managed commit/rollback."""

    connection = None
    cursor = None

    for attempt in range(2):
        try:
            connection = connection_pool.get_connection()
            cursor = connection.cursor(dictionary=True, buffered=buffered)
            break
        except mysql.connector.Error:
            logging.warning(
                "Database pooled connection setup failed (attempt %s); refreshing pool.",
                attempt + 1,
                exc_info=True,
            )
            if connection is not None:
                try:
                    connection.close()
                except mysql.connector.Error:
                    pass
            _refresh_connection_pool()
            if attempt == 1:
                raise

    try:
        yield cursor
    except Exception as error:
        connection.rollback()
        raise error
    else:
        connection.commit()
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()


@asynccontextmanager
async def async_pooled_connection():
    """Yield an async pooled DictCursor with managed commit/rollback."""
    pool = await _get_async_pool()
    connection = await pool.acquire()
    cursor = None
    try:
        cursor = await connection.cursor(aiomysql.DictCursor)
        yield cursor
    except Exception as error:
        await connection.rollback()
        raise error
    else:
        await connection.commit()
    finally:
        if cursor is not None:
            await cursor.close()
        pool.release(connection)


async def async_fetchall(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    async with async_pooled_connection() as cursor:
        await cursor.execute(query, params)
        rows = await cursor.fetchall()
        return list(rows or [])


async def async_execute(query: str, params: tuple[Any, ...] = ()) -> int:
    async with async_pooled_connection() as cursor:
        await cursor.execute(query, params)
        return int(cursor.rowcount or 0)


async def async_upsert_voice_session(session: dict[str, Any]) -> bool:
    try:
        async with async_pooled_connection() as cursor:
            await cursor.execute(
                """
                INSERT INTO voice_sessions
                    (server_guild_id, discord_user_id, voice_channel_id, started_at, last_tick_at,
                     is_eligible, is_self_muted, is_self_deafened, is_server_muted, is_server_deafened)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    voice_channel_id = VALUES(voice_channel_id),
                    started_at = VALUES(started_at),
                    last_tick_at = VALUES(last_tick_at),
                    is_eligible = VALUES(is_eligible),
                    is_self_muted = VALUES(is_self_muted),
                    is_self_deafened = VALUES(is_self_deafened),
                    is_server_muted = VALUES(is_server_muted),
                    is_server_deafened = VALUES(is_server_deafened)
                """,
                (
                    session["guild_id"],
                    session["user_id"],
                    session["channel_id"],
                    session["started_at"],
                    session["last_tick_at"],
                    int(bool(session.get("is_eligible"))),
                    int(bool(session.get("is_self_muted"))),
                    int(bool(session.get("is_self_deafened"))),
                    int(bool(session.get("is_server_muted"))),
                    int(bool(session.get("is_server_deafened"))),
                ),
            )
        return True
    except Exception as err:
        logging.error("Database error while upserting voice session (async): %s", err)
        return False


async def async_get_voice_sessions(guild_id: int) -> list[dict[str, Any]]:
    async with async_pooled_connection() as cursor:
        await cursor.execute(
            """
            SELECT server_guild_id, discord_user_id, voice_channel_id, started_at, last_tick_at,
                   is_eligible, is_self_muted, is_self_deafened, is_server_muted, is_server_deafened
            FROM voice_sessions
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )
        rows = await cursor.fetchall()
        return list(rows or [])


async def async_delete_voice_session(guild_id: int, user_id: int) -> bool:
    try:
        async with async_pooled_connection() as cursor:
            await cursor.execute(
                """
                DELETE FROM voice_sessions
                WHERE server_guild_id = %s AND discord_user_id = %s
                """,
                (guild_id, user_id),
            )
        return True
    except Exception as err:
        logging.error("Database error while deleting voice session (async): %s", err)
        return False


def _ensure_economy_entry(
    cursor: MySQLCursorAbstract,
    user_id: int,
    guild_id: int,
    lock: bool = False,
) -> dict:
    """Retrieve or create the economy row for ``user_id`` within ``guild_id``."""

    select_query = (
        "SELECT id, bank_balance FROM user_economy "
        "WHERE user_id = %s AND server_guild_id = %s"
    )
    if lock:
        select_query += " FOR UPDATE"

    cursor.execute(select_query, (user_id, guild_id))
    row = cursor.fetchone()
    if row:
        return row

    cursor.execute(
        "INSERT INTO user_economy (user_id, server_guild_id, bank_balance) VALUES (%s, %s, 0)",
        (user_id, guild_id),
    )
    return {"id": cursor.lastrowid, "bank_balance": 0}


def initialize_user_economy_table() -> None:
    """Ensure the ``user_economy`` table exists with the expected schema."""

    create_table_statement = """
    CREATE TABLE IF NOT EXISTS user_economy (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        server_guild_id BIGINT NOT NULL,
        bank_balance BIGINT NOT NULL DEFAULT 0,
        UNIQUE KEY uq_user_economy_user_server (user_id, server_guild_id),
        INDEX idx_user_economy_user (user_id),
        CONSTRAINT fk_user_economy_user FOREIGN KEY (user_id) REFERENCES users(id)
            ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """

    try:
        with pooled_connection() as cursor:
            cursor.execute(create_table_statement)

            cursor.execute("SHOW INDEX FROM user_economy")
            indexes = cursor.fetchall() or []

            index_columns: dict[str, list[str]] = {}
            index_unique: dict[str, bool] = {}

            for index in indexes:
                index_name = str(index.get("Key_name") or "")
                if not index_name:
                    continue
                index_columns.setdefault(index_name, []).append(str(index.get("Column_name") or ""))
                index_unique[index_name] = bool((index.get("Non_unique") or 0) == 0)

            has_expected_unique_index = False
            for index_name, columns in list(index_columns.items()):
                if index_unique.get(index_name) and columns == ["user_id", "server_guild_id"]:
                    has_expected_unique_index = True
                    continue

                if index_unique.get(index_name) and columns == ["user_id"]:
                    has_non_unique_user_id_index = any(
                        (not index_unique.get(existing_index_name))
                        and existing_columns == ["user_id"]
                        for existing_index_name, existing_columns in index_columns.items()
                    )
                    if not has_non_unique_user_id_index:
                        cursor.execute(
                            "ALTER TABLE user_economy "
                            "ADD INDEX idx_user_economy_user_fk_support (user_id)"
                        )
                        index_columns["idx_user_economy_user_fk_support"] = ["user_id"]
                        index_unique["idx_user_economy_user_fk_support"] = False
                    safe_index_name = index_name.replace("`", "``")
                    cursor.execute(f"ALTER TABLE user_economy DROP INDEX `{safe_index_name}`")

            if not has_expected_unique_index:
                cursor.execute(
                    "ALTER TABLE user_economy "
                    "ADD UNIQUE KEY uq_user_economy_user_server (user_id, server_guild_id)"
                )
    except mysql.connector.Error as err:
        logging.error("Failed to initialize user_economy table: %s", err)




def initialize_boss_event_tables() -> None:
    """Ensure community boss event tables exist."""

    statements = [
        """
        CREATE TABLE IF NOT EXISTS community_boss_event (
            id INT AUTO_INCREMENT PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            boss_name VARCHAR(120) NOT NULL DEFAULT 'Boss',
            max_hp INT NOT NULL,
            current_hp INT NOT NULL,
            active TINYINT(1) NOT NULL DEFAULT 1,
            attack_cooldown_seconds INT NOT NULL DEFAULT 30,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP NULL,
            UNIQUE KEY uq_community_boss_guild (guild_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """,
        """
        CREATE TABLE IF NOT EXISTS community_boss_contribution (
            id INT AUTO_INCREMENT PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            user_id INT NOT NULL,
            total_damage BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_community_boss_contribution (guild_id, user_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """,
        """
        CREATE TABLE IF NOT EXISTS community_boss_attack_cooldown (
            id INT AUTO_INCREMENT PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            user_id INT NOT NULL,
            cooldown_until BIGINT NOT NULL DEFAULT 0,
            UNIQUE KEY uq_community_boss_cooldown (guild_id, user_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """,
    ]

    try:
        with pooled_connection() as cursor:
            for stmt in statements:
                cursor.execute(stmt)
            cursor.execute(
                """
                SELECT 1
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'community_boss_event'
                  AND COLUMN_NAME = 'boss_name'
                LIMIT 1
                """
            )
            if not cursor.fetchone():
                cursor.execute(
                    "ALTER TABLE community_boss_event ADD COLUMN boss_name VARCHAR(120) NOT NULL DEFAULT 'Boss' AFTER guild_id"
                )
    except mysql.connector.Error as err:
        logging.error("Failed to initialize boss event tables: %s", err)


def initialize_expedition_tables() -> None:
    """Ensure cooperative expedition tables exist."""

    statement = """
    CREATE TABLE IF NOT EXISTS community_expedition (
        id INT AUTO_INCREMENT PRIMARY KEY,
        guild_id BIGINT NOT NULL,
        active TINYINT(1) NOT NULL DEFAULT 1,
        started_at BIGINT NOT NULL,
        ends_at BIGINT NOT NULL,
        participants_json LONGTEXT NOT NULL,
        finalized_at BIGINT NULL,
        UNIQUE KEY uq_community_expedition_guild (guild_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    try:
        with pooled_connection() as cursor:
            cursor.execute(statement)
    except mysql.connector.Error as err:
        logging.error("Failed to initialize expedition tables: %s", err)

def initialize_form_tables() -> None:
    """Ensure the form flow tables exist with the expected schema."""

    create_form_flows_statement = """
    CREATE TABLE IF NOT EXISTS form_flows (
        id INT AUTO_INCREMENT PRIMARY KEY,
        server_guild_id BIGINT NOT NULL,
        name VARCHAR(255) NOT NULL,
        type VARCHAR(50) NOT NULL,
        target_channel_id BIGINT NOT NULL,
        approved_target_channel_id BIGINT NULL,
        rejected_target_channel_id BIGINT NULL,
        rejection_feedback_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_form_flows_type (type),
        INDEX idx_form_flows_server_guild_id (server_guild_id),
        INDEX idx_form_flows_server_name (server_guild_id, name),
        INDEX idx_form_flows_server_type (server_guild_id, type),
        INDEX idx_form_flows_target_channel (target_channel_id),
        INDEX idx_form_flows_approved_target_channel (approved_target_channel_id),
        INDEX idx_form_flows_rejected_target_channel (rejected_target_channel_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """

    create_form_questions_statement = """
    CREATE TABLE IF NOT EXISTS form_questions (
        id INT AUTO_INCREMENT PRIMARY KEY,
        flow_id INT NOT NULL,
        question_text TEXT NOT NULL,
        placeholder_text VARCHAR(100) NULL,
        position INT NOT NULL,
        required BOOLEAN NOT NULL DEFAULT FALSE,
        INDEX idx_form_questions_flow (flow_id),
        INDEX idx_form_questions_position (position),
        CONSTRAINT fk_form_questions_flow
            FOREIGN KEY (flow_id) REFERENCES form_flows(id)
            ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """

    create_form_submissions_statement = """
    CREATE TABLE IF NOT EXISTS form_submissions (
        id INT AUTO_INCREMENT PRIMARY KEY,
        flow_id INT NOT NULL,
        user_id BIGINT NOT NULL,
        guild_id BIGINT NOT NULL,
        channel_id BIGINT NOT NULL,
        message_id BIGINT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_form_submissions_flow (flow_id),
        INDEX idx_form_submissions_status (status),
        INDEX idx_form_submissions_message (message_id),
        CONSTRAINT fk_form_submissions_flow
            FOREIGN KEY (flow_id) REFERENCES form_flows(id)
            ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """

    create_form_decisions_statement = """
    CREATE TABLE IF NOT EXISTS form_decisions (
        id INT AUTO_INCREMENT PRIMARY KEY,
        submission_id INT NOT NULL,
        decision VARCHAR(20) NOT NULL,
        decided_by BIGINT NOT NULL,
        decided_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_form_decisions_submission (submission_id),
        INDEX idx_form_decisions_decision (decision),
        CONSTRAINT fk_form_decisions_submission
            FOREIGN KEY (submission_id) REFERENCES form_submissions(id)
            ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """

    create_form_published_messages_statement = """
    CREATE TABLE IF NOT EXISTS form_published_messages (
        id INT AUTO_INCREMENT PRIMARY KEY,
        flow_id INT NOT NULL,
        message_id BIGINT NOT NULL,
        channel_id BIGINT NOT NULL,
        guild_id BIGINT NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_form_published_message (message_id),
        INDEX idx_form_published_flow (flow_id),
        CONSTRAINT fk_form_published_flow
            FOREIGN KEY (flow_id) REFERENCES form_flows(id)
            ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """

    try:
        with pooled_connection() as cursor:
            cursor.execute(create_form_flows_statement)
            cursor.execute("SHOW COLUMNS FROM form_flows LIKE 'approved_target_channel_id'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE form_flows "
                    "ADD COLUMN approved_target_channel_id BIGINT NULL AFTER target_channel_id"
                )
            cursor.execute("SHOW COLUMNS FROM form_flows LIKE 'rejected_target_channel_id'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE form_flows "
                    "ADD COLUMN rejected_target_channel_id BIGINT NULL AFTER approved_target_channel_id"
                )
            cursor.execute("SHOW COLUMNS FROM form_flows LIKE 'rejection_feedback_enabled'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE form_flows "
                    "ADD COLUMN rejection_feedback_enabled BOOLEAN NOT NULL DEFAULT FALSE "
                    "AFTER rejected_target_channel_id"
                )
            cursor.execute(create_form_questions_statement)
            cursor.execute("SHOW COLUMNS FROM form_questions LIKE 'placeholder_text'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE form_questions "
                    "ADD COLUMN placeholder_text VARCHAR(100) NULL AFTER question_text"
                )
            cursor.execute(create_form_submissions_statement)
            cursor.execute(create_form_decisions_statement)
            cursor.execute(create_form_published_messages_statement)
    except mysql.connector.Error as err:
        logging.error("Failed to initialize form flow tables: %s", err)


def initialize_active_call_logs_table() -> None:
    """Ensure active call log sessions are persisted across bot restarts."""

    create_table_statement = """
    CREATE TABLE IF NOT EXISTS active_call_logs (
        id INT AUTO_INCREMENT PRIMARY KEY,
        server_guild_id BIGINT NOT NULL,
        voice_channel_id BIGINT NOT NULL,
        log_message_id BIGINT NULL,
        payload_json LONGTEXT NULL,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_active_call_logs_channel (server_guild_id, voice_channel_id),
        INDEX idx_active_call_logs_updated_at (updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """

    try:
        with pooled_connection() as cursor:
            cursor.execute(create_table_statement)
    except mysql.connector.Error as err:
        logging.error("Failed to initialize active_call_logs table: %s", err)


def initialize_portaria_base_config_table() -> None:
    """Validate the Flyway-owned Portaria base schema without mutating it."""

    required = {
        "server_guild_id",
        "acesso_provisorio_role_id",
        "visitante_role_id",
        "maior_18_role_id",
        "menor_18_role_id",
        "aprovacao_acesso_provisorio_ativo",
        "aprovacao_acesso_provisorio_duracao_dias",
        "formulario_portaria_ativo",
        "portaria_enabled",
        "idade_minima_conta_ficha_ativa",
        "idade_minima_conta_ficha_dias",
        "idade_minima_conta_acesso_provisorio_ativa",
        "idade_minima_conta_acesso_provisorio_dias",
        "idade_minima_entrada_servidor_ativa",
        "idade_minima_entrada_servidor_anos",
    }
    try:
        with pooled_connection() as cursor:
            cursor.execute("SHOW COLUMNS FROM portaria_base_config")
            present = {str(row.get("Field")) for row in (cursor.fetchall() or [])}
        missing = sorted(required - present)
        if missing:
            logging.error(
                "Portaria schema is behind Flyway; missing columns: %s",
                ", ".join(missing),
            )
    except mysql.connector.Error as err:
        logging.error("Failed to validate portaria_base_config schema: %s", err)

def initialize_portaria_bypasses_table() -> None:
    """Validate the canonical Flyway-owned Portaria bypass schema."""

    required = {
        "server_guild_id",
        "bypass_type",
        "discord_user_id",
        "invite_code",
        "access_mode",
        "requires_form",
        "active",
        "expires_at",
        "expired_at",
        "removed_at",
    }
    try:
        with pooled_connection() as cursor:
            cursor.execute("SHOW COLUMNS FROM portaria_bypasses")
            present = {str(row.get("Field")) for row in (cursor.fetchall() or [])}
        missing = sorted(required - present)
        if missing:
            logging.error(
                "Canonical Portaria bypass schema is behind Flyway; missing columns: %s",
                ", ".join(missing),
            )
    except mysql.connector.Error as err:
        logging.error("Failed to validate portaria_bypasses schema: %s", err)



def initialize_allowed_feature_channels_table() -> None:
    """Store allowed channels per guild and feature key."""

    create_table_statement = """
    CREATE TABLE IF NOT EXISTS allowed_feature_channels (
        id INT AUTO_INCREMENT PRIMARY KEY,
        server_guild_id BIGINT NOT NULL,
        feature_key VARCHAR(100) NOT NULL,
        channel_id BIGINT NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_allowed_feature_channels (server_guild_id, feature_key, channel_id),
        INDEX idx_allowed_feature_channels_lookup (server_guild_id, feature_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """

    try:
        with pooled_connection() as cursor:
            cursor.execute(create_table_statement)
    except mysql.connector.Error as err:
        logging.error("Failed to initialize allowed_feature_channels table: %s", err)


def initialize_user_community_status_invite_columns() -> None:
    """Validate Flyway-owned membership invite provenance columns."""

    required = {"last_join_date", "invite_link_used", "invited_by"}
    try:
        with pooled_connection() as cursor:
            cursor.execute("SHOW COLUMNS FROM user_community_status")
            present = {str(row.get("Field")) for row in (cursor.fetchall() or [])}
        missing = sorted(required - present)
        if missing:
            logging.error(
                "Membership invite schema is behind Flyway; missing columns: %s",
                ", ".join(missing),
            )
    except mysql.connector.Error as err:
        logging.error("Failed to validate membership invite schema: %s", err)

def initialize_portaria_account_release_table() -> None:
    """Validate the Flyway-owned legacy bridge used during rollout."""

    required = {
        "server_guild_id",
        "user_id",
        "access_mode",
        "requires_form",
        "released_by",
    }
    try:
        with pooled_connection() as cursor:
            cursor.execute("SHOW COLUMNS FROM portaria_account_release")
            present = {str(row.get("Field")) for row in (cursor.fetchall() or [])}
        missing = sorted(required - present)
        if missing:
            logging.error(
                "Legacy Portaria account-release bridge is behind Flyway; missing columns: %s",
                ", ".join(missing),
            )
    except mysql.connector.Error as err:
        logging.error("Failed to validate legacy portaria_account_release schema: %s", err)

def initialize_bot_sensitive_permission_whitelist_table() -> None:
    """Store allowed sensitive permissions for specific members (bots/users) per guild."""

    create_table_statement = """
    CREATE TABLE IF NOT EXISTS bot_sensitive_permission_whitelist (
        id INT AUTO_INCREMENT PRIMARY KEY,
        server_guild_id BIGINT NOT NULL,
        actor_id BIGINT NOT NULL,
        actor_type VARCHAR(10) NOT NULL DEFAULT 'bot',
        permission_name VARCHAR(100) NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_bot_sensitive_permission_whitelist (server_guild_id, actor_id, actor_type, permission_name),
        INDEX idx_bot_sensitive_permission_whitelist_lookup (server_guild_id, actor_id, actor_type)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """

    try:
        with pooled_connection() as cursor:
            cursor.execute(create_table_statement)
            cursor.execute("SHOW COLUMNS FROM bot_sensitive_permission_whitelist LIKE 'actor_type'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE bot_sensitive_permission_whitelist "
                    "ADD COLUMN actor_type VARCHAR(10) NOT NULL DEFAULT 'bot' AFTER actor_id"
                )

            cursor.execute(
                "SHOW INDEX FROM bot_sensitive_permission_whitelist "
                "WHERE Key_name = 'uq_bot_sensitive_permission_whitelist'"
            )
            unique_rows = cursor.fetchall() or []
            if unique_rows and len(unique_rows) != 4:
                cursor.execute("ALTER TABLE bot_sensitive_permission_whitelist DROP INDEX uq_bot_sensitive_permission_whitelist")
                cursor.execute(
                    "ALTER TABLE bot_sensitive_permission_whitelist "
                    "ADD UNIQUE KEY uq_bot_sensitive_permission_whitelist "
                    "(server_guild_id, actor_id, actor_type, permission_name)"
                )
    except mysql.connector.Error as err:
        logging.error("Failed to initialize bot_sensitive_permission_whitelist table: %s", err)


def initialize_discord_backup_tables() -> None:
    """Fail clearly when the Flyway-owned Backup schema is unavailable."""

    required_columns = {
        "backup_discord": {
            "id", "guild_id", "original_name", "backup_type",
            "created_by_discord_user_id", "idempotency_key", "created_at",
        },
        "backup_roles": {
            "id", "backup_id", "discord_id", "name", "color", "permissions",
            "position", "hoist", "mentionable",
        },
        "backup_channels": {
            "id", "backup_id", "discord_id", "parent_id", "name", "type",
            "position", "topic", "nsfw",
        },
        "backup_overwrites": {
            "id", "channel_id", "role_name", "target_type", "target_discord_id",
            "role_id", "allow_bits", "deny_bits",
        },
        "backup_server_settings": {
            "id", "guild_id", "max_normal_backups", "max_periodic_backups",
            "periodic_backups_enabled", "periodicity_minutes",
            "periodicity_frequency", "bot_removed_at", "purge_after",
            "created_at", "updated_at",
        },
        "backup_restore_operations": {
            "id", "guild_id", "backup_id", "operation_key", "scope",
            "actor_discord_user_id", "actor_user_id", "status", "decision_json",
            "result_json", "error_code", "progress_current", "progress_total",
            "current_step", "started_at", "finished_at", "created_at", "updated_at",
        },
        "backup_snapshot_operations": {
            "id", "guild_id", "operation_key", "backup_type", "requested_name",
            "actor_discord_user_id", "actor_user_id", "status", "backup_id",
            "progress_current", "progress_total", "current_step", "result_json",
            "error_code", "started_at", "finished_at", "created_at", "updated_at",
        },
        "backup_operation_steps": {
            "id", "guild_id", "operation_kind", "operation_id", "step_code",
            "step_status", "message", "progress_current", "progress_total",
            "detail_json", "created_at",
        },
        "backup_guild_locks": {
            "guild_id", "lock_token", "operation_type", "operation_id",
            "expires_at", "created_at", "updated_at",
        },
    }
    required_indexes = {
        ("backup_discord", "idx_backup_discord_guild_type"),
        ("backup_discord", "uq_backup_discord_guild_idempotency"),
        ("backup_server_settings", "uq_backup_server_settings_guild"),
        ("backup_overwrites", "idx_backup_overwrites_target_discord"),
        ("backup_server_settings", "idx_backup_server_settings_purge_after"),
        ("backup_restore_operations", "uq_backup_restore_guild_operation_key"),
        ("backup_snapshot_operations", "uq_backup_snapshot_guild_operation_key"),
        ("backup_operation_steps", "idx_backup_operation_steps_operation"),
        ("backup_guild_locks", "PRIMARY"),
    }

    with pooled_connection() as cursor:
        for table, columns in required_columns.items():
            cursor.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                """,
                (table,),
            )
            present = {str(row["COLUMN_NAME"]) for row in (cursor.fetchall() or [])}
            missing = sorted(columns - present)
            if missing:
                raise RuntimeError(
                    f"backup_schema_missing:{table}:{','.join(missing)}"
                )

        for table, index in required_indexes:
            cursor.execute(
                """
                SELECT 1
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = %s
                  AND INDEX_NAME = %s
                LIMIT 1
                """,
                (table, index),
            )
            if cursor.fetchone() is None:
                raise RuntimeError(f"backup_schema_missing_index:{table}:{index}")

def initialize_discord_theme_tables() -> None:
    """Fail clearly when the Flyway-owned Theme schema is unavailable."""

    required_columns = {
        "discord_themes": {
            "id", "guild_id", "name", "icon_action", "banner_action",
            "created_by_discord_user_id", "deleted_at",
        },
        "discord_theme_assets": {
            "id", "theme_id", "guild_id", "asset_type", "storage_key",
            "content_type", "sha256", "size_bytes",
        },
        "discord_theme_resource_changes": {
            "id", "theme_id", "resource_type", "resource_discord_id", "target_name",
        },
        "discord_theme_applications": {
            "id", "theme_id", "guild_id", "actor_discord_user_id", "status",
            "frozen_definition_json", "applied_at", "restored_at",
            "rollback_assets_cleaned_at",
        },
        "discord_theme_application_snapshots": {
            "id", "application_id", "resource_type", "resource_discord_id",
            "before_value", "applied_value", "before_asset_key",
            "before_asset_content_type", "before_asset_sha256",
            "applied_asset_key", "applied_asset_content_type",
            "applied_asset_sha256", "apply_status", "restore_status",
        },
        "discord_theme_operations": {
            "id", "application_id", "guild_id", "operation_key",
            "operation_type", "actor_discord_user_id", "force_restore",
            "status", "progress_current", "progress_total", "current_step",
            "result_json", "error_code", "started_at", "finished_at",
        },
        "discord_theme_operation_steps": {
            "id", "operation_id", "guild_id", "step_code", "step_status",
            "message", "progress_current", "progress_total", "detail_json",
        },
        "discord_theme_active_guilds": {
            "guild_id", "application_id",
        },
        "discord_theme_guild_locks": {
            "guild_id", "lock_token", "operation_type", "operation_id",
            "application_id", "expires_at",
        },
    }
    required_indexes = {
        ("discord_themes", "uq_discord_themes_guild_active_name"),
        ("discord_theme_assets", "uq_discord_theme_asset_key"),
        ("discord_theme_resource_changes", "uq_discord_theme_resource"),
        ("discord_theme_application_snapshots", "uq_discord_theme_snapshot_resource"),
        ("discord_theme_operations", "uq_discord_theme_operation_key"),
        ("discord_theme_active_guilds", "PRIMARY"),
        ("discord_theme_guild_locks", "PRIMARY"),
    }

    with pooled_connection() as cursor:
        for table, columns in required_columns.items():
            cursor.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                """,
                (table,),
            )
            present = {str(row["COLUMN_NAME"]) for row in (cursor.fetchall() or [])}
            missing = sorted(columns - present)
            if missing:
                raise RuntimeError(
                    f"theme_schema_missing:{table}:{','.join(missing)}"
                )

        for table, index in required_indexes:
            cursor.execute(
                """
                SELECT 1
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = %s
                  AND INDEX_NAME = %s
                LIMIT 1
                """,
                (table, index),
            )
            if cursor.fetchone() is None:
                raise RuntimeError(f"theme_schema_missing_index:{table}:{index}")


def initialize_config_levels_table() -> None:
    """Ensure ``config_levels`` exists with unique guild rows and defaults."""

    create_table_statement = """
    CREATE TABLE IF NOT EXISTS config_levels (
        id INT AUTO_INCREMENT PRIMARY KEY,
        server_guild_id BIGINT NOT NULL,
        clear_on_exit TINYINT(1) NOT NULL DEFAULT 0,
        levelup_warning TINYINT(1) NOT NULL DEFAULT 0,
        levelup_warning_channel BIGINT NULL,
        level_up_message TEXT NULL,
        multiplier DECIMAL(20,3) NULL DEFAULT NULL,
        phase1_k DECIMAL(20,3) NOT NULL DEFAULT 45.000,
        phase1_p DECIMAL(20,3) NOT NULL DEFAULT 2.000,
        phase1_b DECIMAL(20,3) NOT NULL DEFAULT 0.000,
        daily_combo INT NULL DEFAULT NULL,
        combo_multiplier DECIMAL(20,3) NULL DEFAULT NULL,
        xp_base_per_min INT NOT NULL DEFAULT 2,
        voice_social_bonus_pct DECIMAL(8,4) NOT NULL DEFAULT 0.0000,
        voice_social_bonus_min_humans INT NOT NULL DEFAULT 2,
        voice_diminishing_window1_minutes INT NOT NULL DEFAULT 60,
        voice_diminishing_window2_minutes INT NOT NULL DEFAULT 120,
        voice_diminishing_factor2 DECIMAL(8,4) NOT NULL DEFAULT 0.6000,
        voice_diminishing_factor3 DECIMAL(8,4) NOT NULL DEFAULT 0.3000,
        voice_daily_cap_xp INT NOT NULL DEFAULT 600,
        text_daily_cap_xp INT NOT NULL DEFAULT 300,
        global_daily_cap_xp INT NOT NULL DEFAULT 900,
        UNIQUE KEY uq_config_levels_server_guild (server_guild_id),
        INDEX idx_config_levels_server_guild (server_guild_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """

    try:
        with pooled_connection() as cursor:
            cursor.execute(create_table_statement)
            cursor.execute("SHOW COLUMNS FROM config_levels LIKE 'levelup_warning'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE config_levels "
                    "ADD COLUMN levelup_warning TINYINT(1) NOT NULL DEFAULT 0 AFTER clear_on_exit"
                )
            for legacy_column in ("pivot_level", "phase2_c"):
                cursor.execute(f"SHOW COLUMNS FROM config_levels LIKE '{legacy_column}'")
                if cursor.fetchone() is not None:
                    cursor.execute(f"ALTER TABLE config_levels DROP COLUMN {legacy_column}")
            missing_columns = [
                ("xp_base_per_min", "INT NOT NULL DEFAULT 2"),
                ("voice_social_bonus_pct", "DECIMAL(8,4) NOT NULL DEFAULT 0.0000"),
                ("voice_social_bonus_min_humans", "INT NOT NULL DEFAULT 2"),
                ("voice_diminishing_window1_minutes", "INT NOT NULL DEFAULT 60"),
                ("voice_diminishing_window2_minutes", "INT NOT NULL DEFAULT 120"),
                ("voice_diminishing_factor2", "DECIMAL(8,4) NOT NULL DEFAULT 0.6000"),
                ("voice_diminishing_factor3", "DECIMAL(8,4) NOT NULL DEFAULT 0.3000"),
                ("voice_daily_cap_xp", "INT NOT NULL DEFAULT 600"),
                ("text_daily_cap_xp", "INT NOT NULL DEFAULT 300"),
                ("global_daily_cap_xp", "INT NOT NULL DEFAULT 900"),
            ]
            for column_name, definition in missing_columns:
                cursor.execute(f"SHOW COLUMNS FROM config_levels LIKE '{column_name}'")
                if cursor.fetchone() is None:
                    cursor.execute(f"ALTER TABLE config_levels ADD COLUMN {column_name} {definition}")
    except mysql.connector.Error as err:
        logging.error("Failed to initialize config_levels table: %s", err)


def initialize_minigame_scores_table() -> None:
    """Ensure weekly minigame scoring table exists."""
    create_statement = """
    CREATE TABLE IF NOT EXISTS minigame_scores (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        server_guild_id BIGINT NOT NULL,
        game_name VARCHAR(40) NOT NULL,
        points INT NOT NULL DEFAULT 0,
        won BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_minigame_scores_guild_week (server_guild_id, created_at),
        INDEX idx_minigame_scores_user (user_id),
        CONSTRAINT fk_minigame_scores_user FOREIGN KEY (user_id) REFERENCES users(id)
            ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    try:
        with pooled_connection() as cursor:
            cursor.execute(create_statement)
    except mysql.connector.Error as err:
        logging.error("Failed to initialize minigame_scores table: %s", err)


def initialize_community_discord_columns() -> None:
    """Ensure ``community_discord`` includes presence/admin/member metadata."""

    try:
        with pooled_connection() as cursor:
            cursor.execute("SHOW COLUMNS FROM community_discord LIKE 'active'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE community_discord "
                    "ADD COLUMN active TINYINT(1) NOT NULL DEFAULT 1 AFTER guild_id"
                )

            cursor.execute("SHOW COLUMNS FROM community_discord LIKE 'discord_admin_id'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE community_discord "
                    "ADD COLUMN discord_admin_id BIGINT NULL AFTER active"
                )

            cursor.execute("SHOW COLUMNS FROM community_discord LIKE 'users_quantity'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE community_discord "
                    "ADD COLUMN users_quantity INT NOT NULL DEFAULT 0 AFTER discord_admin_id"
                )
    except mysql.connector.Error as err:
        logging.error("Failed to initialize community_discord columns: %s", err)


def initialize_user_bans_registered_at_column() -> None:
    """Ensure ``user_bans`` has ``registered_at`` populated at insertion time."""

    try:
        with pooled_connection() as cursor:
            cursor.execute("SHOW COLUMNS FROM user_bans LIKE 'registered_at'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE user_bans "
                    "ADD COLUMN registered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                )
    except mysql.connector.Error as err:
        logging.error("Failed to initialize user_bans.registered_at column: %s", err)


def initialize_user_level_voice_xp_columns() -> None:
    """Ensure user-level XP cap tracking columns exist."""

    try:
        with pooled_connection() as cursor:
            cursor.execute("SHOW COLUMNS FROM user_level LIKE 'xp_awarded_voice_today'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE user_level "
                    "ADD COLUMN xp_awarded_voice_today INT NOT NULL DEFAULT 0"
                )
            cursor.execute("SHOW COLUMNS FROM user_level LIKE 'xp_awarded_voice_day'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE user_level "
                    "ADD COLUMN xp_awarded_voice_day DATE NULL"
                )
            cursor.execute("SHOW COLUMNS FROM user_level LIKE 'xp_awarded_text_today'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE user_level "
                    "ADD COLUMN xp_awarded_text_today INT NOT NULL DEFAULT 0"
                )
            cursor.execute("SHOW COLUMNS FROM user_level LIKE 'xp_awarded_text_day'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE user_level "
                    "ADD COLUMN xp_awarded_text_day DATE NULL"
                )
            cursor.execute("SHOW COLUMNS FROM user_level LIKE 'xp_awarded_today'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE user_level "
                    "ADD COLUMN xp_awarded_today INT NOT NULL DEFAULT 0"
                )
            cursor.execute("SHOW COLUMNS FROM user_level LIKE 'xp_awarded_day'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE user_level "
                    "ADD COLUMN xp_awarded_day DATE NULL"
                )
    except mysql.connector.Error as err:
        logging.error("Failed to initialize user_level voice xp columns: %s", err)


def initialize_user_birthday_registration_columns() -> None:
    """Ensure ``user_birthday`` has post-registration audit columns."""

    try:
        with pooled_connection() as cursor:
            cursor.execute("SHOW COLUMNS FROM user_birthday LIKE 'post_informed_date'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE user_birthday "
                    "ADD COLUMN post_informed_date DATE NULL AFTER birth_date"
                )

            cursor.execute("SHOW COLUMNS FROM user_birthday LIKE 'registered_at'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE user_birthday "
                    "ADD COLUMN registered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP AFTER registered"
                )

            cursor.execute("SHOW COLUMNS FROM user_birthday LIKE '18_plus'")
            if cursor.fetchone() is None:
                cursor.execute(
                    "ALTER TABLE user_birthday "
                    "ADD COLUMN `18_plus` TINYINT(1) NOT NULL DEFAULT 0 AFTER registered_at"
                )
    except mysql.connector.Error as err:
        logging.error("Failed to initialize user_birthday registration columns: %s", err)


def initialize_trending_presence_column() -> None:
    """Ensure ``config_server_settings.trending_presence_enabled`` exists at startup."""

    try:
        with pooled_connection() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'config_server_settings'
                  AND COLUMN_NAME = 'trending_presence_enabled'
                LIMIT 1
                """
            )
            if cursor.fetchone() is None:
                try:
                    cursor.execute(
                        "ALTER TABLE config_server_settings "
                        "ADD COLUMN trending_presence_enabled TINYINT(1) NOT NULL DEFAULT 0"
                    )
                except mysql.connector.Error as err:
                    if err.errno != errorcode.ER_DUP_FIELDNAME:
                        raise
    except mysql.connector.Error as err:
        error_message = (
            "Startup migration failed for config_server_settings.trending_presence_enabled. "
            "Apply migrations before serving events."
        )
        logging.critical("%s mysql_error=%s", error_message, err)
        raise RuntimeError(error_message) from err


def register_sensitive_permission_whitelist(
    guild_id: int,
    actor_id: int,
    actor_type: Literal['bot', 'user'],
    permission_name: str,
) -> bool:
    """Register a sensitive permission that is allowed for an actor (bot or user)."""

    if permission_name == "administrator":
        return False

    query = (
        "INSERT IGNORE INTO bot_sensitive_permission_whitelist "
        "(server_guild_id, actor_id, actor_type, permission_name) VALUES (%s, %s, %s, %s)"
    )
    with pooled_connection() as cursor:
        cursor.execute(query, (guild_id, actor_id, actor_type, permission_name))
        return cursor.rowcount > 0


def remove_sensitive_permission_whitelist(
    guild_id: int,
    actor_id: int,
    actor_type: Literal['bot', 'user'],
    permission_name: str,
) -> bool:
    """Remove a sensitive permission from actor whitelist."""

    query = (
        "DELETE FROM bot_sensitive_permission_whitelist "
        "WHERE server_guild_id = %s AND actor_id = %s AND actor_type = %s AND permission_name = %s"
    )
    with pooled_connection() as cursor:
        cursor.execute(query, (guild_id, actor_id, actor_type, permission_name))
        return cursor.rowcount > 0


def list_sensitive_permission_whitelist(
    guild_id: int,
    actor_id: int | None = None,
    actor_type: Literal['bot', 'user'] | None = None,
) -> list[dict]:
    """Return whitelist entries for a guild and optional actor filters."""

    base_query = (
        "SELECT actor_id, actor_type, permission_name "
        "FROM bot_sensitive_permission_whitelist WHERE server_guild_id = %s"
    )
    params: list[int | str] = [guild_id]

    if actor_id is not None:
        base_query += " AND actor_id = %s"
        params.append(actor_id)

    if actor_type is not None:
        base_query += " AND actor_type = %s"
        params.append(actor_type)

    base_query += " ORDER BY actor_type, actor_id, permission_name"

    with pooled_connection() as cursor:
        cursor.execute(base_query, tuple(params))
        return cursor.fetchall() or []


def is_sensitive_permission_whitelisted(
    guild_id: int,
    actor_id: int,
    actor_type: Literal['bot', 'user'],
    permission_name: str,
) -> bool:
    """Check whether a sensitive permission is whitelisted for an actor."""

    query = (
        "SELECT 1 FROM bot_sensitive_permission_whitelist "
        "WHERE server_guild_id = %s AND actor_id = %s AND actor_type = %s AND permission_name = %s LIMIT 1"
    )
    with pooled_connection() as cursor:
        cursor.execute(query, (guild_id, actor_id, actor_type, permission_name))
        return cursor.fetchone() is not None


def register_bot_sensitive_permission_whitelist(guild_id: int, bot_id: int, permission_name: str) -> bool:
    return register_sensitive_permission_whitelist(guild_id, bot_id, 'bot', permission_name)


def remove_bot_sensitive_permission_whitelist(guild_id: int, bot_id: int, permission_name: str) -> bool:
    return remove_sensitive_permission_whitelist(guild_id, bot_id, 'bot', permission_name)


def list_bot_sensitive_permission_whitelist(guild_id: int, bot_id: int | None = None) -> list[dict]:
    rows = list_sensitive_permission_whitelist(guild_id, bot_id, 'bot')
    return [
        {'bot_id': row['actor_id'], 'permission_name': row['permission_name']}
        for row in rows
    ]


def is_bot_sensitive_permission_whitelisted(guild_id: int, bot_id: int, permission_name: str) -> bool:
    return is_sensitive_permission_whitelisted(guild_id, bot_id, 'bot', permission_name)


def register_user_sensitive_permission_whitelist(guild_id: int, user_id: int, permission_name: str) -> bool:
    return register_sensitive_permission_whitelist(guild_id, user_id, 'user', permission_name)


def remove_user_sensitive_permission_whitelist(guild_id: int, user_id: int, permission_name: str) -> bool:
    return remove_sensitive_permission_whitelist(guild_id, user_id, 'user', permission_name)


def list_user_sensitive_permission_whitelist(guild_id: int, user_id: int | None = None) -> list[dict]:
    rows = list_sensitive_permission_whitelist(guild_id, user_id, 'user')
    return [
        {'user_id': row['actor_id'], 'permission_name': row['permission_name']}
        for row in rows
    ]


def is_user_sensitive_permission_whitelisted(guild_id: int, user_id: int, permission_name: str) -> bool:
    return is_sensitive_permission_whitelisted(guild_id, user_id, 'user', permission_name)


initialize_user_economy_table()
initialize_form_tables()
initialize_active_call_logs_table()
initialize_portaria_base_config_table()
initialize_user_community_status_invite_columns()
initialize_portaria_account_release_table()
initialize_portaria_bypasses_table()
initialize_allowed_feature_channels_table()
initialize_bot_sensitive_permission_whitelist_table()
initialize_config_levels_table()
initialize_minigame_scores_table()
initialize_boss_event_tables()
initialize_expedition_tables()
initialize_community_discord_columns()
initialize_user_bans_registered_at_column()
initialize_user_level_voice_xp_columns()
initialize_user_birthday_registration_columns()
initialize_trending_presence_column()

_USER_INVENTORY_SCHEMA_VALIDATED: Optional[bool] = None


def _convert_service_metadata_value(value: Any) -> Any:
    """Ensure ``value`` is JSON serializable for service metadata storage."""

    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)

    if isinstance(value, Enum):
        return value.value

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, Mapping):
        return {
            key: _convert_service_metadata_value(item)
            for key, item in value.items()
            if item is not None
        }

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        converted_items = [
            _convert_service_metadata_value(item)
            for item in value
            if item is not None
        ]
        return converted_items

    return str(value)


def _serialize_service_metadata(metadata: Optional[dict]) -> Optional[str]:
    """Serialize service metadata to JSON ensuring complex types are supported."""

    if not metadata:
        return None

    converted = {
        key: _convert_service_metadata_value(value)
        for key, value in metadata.items()
        if value is not None
    }

    if not converted:
        return None

    return json.dumps(converted)


def _ensure_user_inventory_schema(cursor: MySQLCursorAbstract) -> None:
    """Garantir que a tabela ``user_inventory`` possui as colunas esperadas."""

    global _USER_INVENTORY_SCHEMA_VALIDATED

    if _USER_INVENTORY_SCHEMA_VALIDATED:
        return

    required_columns = {
        "store_item_id": False,
        "used_in": False,
        "valid_until": False,
        "service_metadata": False,
    }

    for column in required_columns:
        cursor.execute(
            "SHOW COLUMNS FROM user_inventory LIKE %s",
            (column,),
        )
        required_columns[column] = cursor.fetchone() is not None

    if not required_columns["service_metadata"]:
        try:
            cursor.execute(
                "ALTER TABLE user_inventory ADD COLUMN service_metadata JSON NULL"
            )
        except (ProgrammingError, DatabaseError) as error:
            logging.warning(
                "Falha ao adicionar coluna JSON para metadados de serviços no inventário: %s. "
                "Aplicando solução alternativa utilizando LONGTEXT.",
                error,
            )
            cursor.execute(
                "ALTER TABLE user_inventory ADD COLUMN service_metadata LONGTEXT NULL"
            )
        required_columns["service_metadata"] = True

    if not all(required_columns.values()):
        missing = [column for column, exists in required_columns.items() if not exists]
        raise ValueError(
            "A tabela user_inventory precisa possuir as colunas 'store_item_id', 'used_in', 'valid_until' e 'service_metadata' para registrar serviços da loja. Faltando: "
            + ", ".join(missing)
        )

    _USER_INVENTORY_SCHEMA_VALIDATED = True


def _get_community_id(
    cursor: MySQLCursorAbstract, guild_id: int
) -> int:
    """Return the community identifier associated with ``guild_id``."""

    cursor.execute(
        "SELECT community_id FROM community_discord WHERE guild_id = %s",
        (guild_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise ValueError(
            "Não foi possível encontrar a comunidade associada a este servidor."
        )
    return int(row["community_id"])


def getCommunityId(guild_id: int) -> int:
    """Return the internal community identifier for a Discord guild."""

    with pooled_connection() as cursor:
        return _get_community_id(cursor, guild_id)


def _ensure_economy_entry_by_user_id(
    cursor: MySQLCursorAbstract, user_id: int, guild_id: int
) -> dict:
    """Wrapper to reuse ``_ensure_economy_entry`` when o usuário já é conhecido."""

    return _ensure_economy_entry(cursor, user_id, guild_id, lock=True)


def _adjust_user_balance_by_user_id(
    cursor: MySQLCursorAbstract, guild_id: int, user_id: int, delta: int
) -> int:
    """Aplicar ``delta`` diretamente no saldo de um usuário conhecido."""

    entry = _ensure_economy_entry_by_user_id(cursor, user_id, guild_id)
    new_balance = int(entry["bank_balance"] or 0) + delta
    if new_balance < 0:
        raise ValueError("O saldo não pode ser negativo.")
    cursor.execute(
        "UPDATE user_economy SET bank_balance = %s WHERE id = %s",
        (new_balance, entry["id"]),
    )
    return new_balance


def ensure_community_registration_for_guilds(guilds: Sequence[discord.Guild]) -> None:
    """Ensure runtime settings only for guilds already registered by the API.

    Community/network lifecycle is control-plane state. Coddy observes it
    through the API and must not create, reactivate, rename, or transfer
    ownership of Community rows through direct SQL.
    """

    if not guilds:
        return

    with pooled_connection() as cursor:
        for guild in guilds:
            cursor.execute(
                "SELECT community_id FROM community_discord WHERE guild_id = %s",
                (guild.id,),
            )
            if cursor.fetchone() is None:
                logging.warning(
                    "Guild %s ainda não foi registrada pela API; configuração local não será criada",
                    guild.id,
                )
                continue
            cursor.execute(
                "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
                (guild.id,),
            )


def initialize_guild_server_settings(guild_id: int) -> bool:
    """Create neutral defaults for a guild in ``config_server_settings``.

    Existing non-null values are preserved so re-joining the bot does not reset
    previously configured server behavior.
    """

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_ai_settings_columns(cursor):
            return False

        cursor.execute(
            """
            UPDATE config_server_settings
            SET
                has_gpt_enabled = COALESCE(has_gpt_enabled, FALSE),
                ai_channel_limit_enabled = COALESCE(ai_channel_limit_enabled, FALSE),
                ai_admin_channel_bypass_enabled = COALESCE(ai_admin_channel_bypass_enabled, FALSE),
                trending_presence_enabled = COALESCE(trending_presence_enabled, FALSE)
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )

    return True


def refresh_community_discord_presence(guilds: Sequence[discord.Guild]) -> None:
    """Deprecated compatibility hook.

    Presence reconciliation now belongs to the API control plane. This function
    deliberately performs no Community lifecycle DML.
    """

    logging.debug(
        "refresh_community_discord_presence ignorado; %s guild(s) serão reconciliadas via API",
        len(guilds),
    )


def getConfig(guild:discord.Guild, ensure_registration: bool = True):
    if ensure_registration:
        ensure_community_registration_for_guilds([guild])

    with pooled_connection() as cursor:
        # Recupera as configurações do servidor
        cursor.execute("""
        SELECT COLUMN_NAME
          FROM INFORMATION_SCHEMA.COLUMNS
         WHERE TABLE_NAME = 'config_server_settings'
        """)
        todas_colunas = cursor.fetchall()

        nomes = [
            row['COLUMN_NAME']
            for row in todas_colunas
            if row['COLUMN_NAME'] not in ('server_guild_id', 'id')
        ]

        extras = ", ".join(f"config_server_settings.{col} AS {snake_to_camel(col)}" for col in nomes)

        # 4) monta a query completa
        dynamic_query = f"""
            SELECT
            community_discord.name,
            community_discord.guild_id AS guildId
            {',' if extras else ''}{extras}
            FROM community_discord
            LEFT JOIN config_server_settings
            ON community_discord.guild_id = config_server_settings.server_guild_id
            WHERE community_discord.guild_id = %s
        """

        cursor.execute(dynamic_query, (guild.id,))
        config = cursor.fetchone()

        return config


def get_allowed_feature_channels(guild_id: int, feature_key: str) -> list[int]:
    """Return channel IDs explicitly allowed for ``feature_key`` in a guild."""
    try:
        with pooled_connection() as cursor:
            cursor.execute(
                """
                SELECT channel_id
                FROM allowed_feature_channels
                WHERE server_guild_id = %s AND feature_key = %s
                ORDER BY channel_id
                """,
                (guild_id, feature_key),
            )
            rows = cursor.fetchall() or []
            return [int(row["channel_id"]) for row in rows]
    except Exception as err:
        logging.exception(
            "Failed to list allowed feature channels for guild_id=%s feature_key=%s: %s",
            guild_id,
            feature_key,
            err,
        )
        return []


def set_allowed_feature_channels(guild_id: int, feature_key: str, channel_ids: list[int]) -> bool:
    """Replace the set of allowed channels for a feature in a guild."""

    normalized_ids = sorted({int(channel_id) for channel_id in channel_ids})
    try:
        with pooled_connection() as cursor:
            cursor.execute(
                """
                DELETE FROM allowed_feature_channels
                WHERE server_guild_id = %s AND feature_key = %s
                """,
                (guild_id, feature_key),
            )

            for channel_id in normalized_ids:
                cursor.execute(
                    """
                    INSERT INTO allowed_feature_channels
                        (server_guild_id, feature_key, channel_id)
                    VALUES (%s, %s, %s)
                    """,
                    (guild_id, feature_key, channel_id),
                )
        return True
    except mysql.connector.Error as err:
        logging.error("Database error while setting allowed feature channels: %s", err)
        return False


def _ensure_ai_settings_columns(cursor) -> bool:
    """Check the Database-owned AI schema without mutating it at runtime."""

    required_columns = {
        "has_gpt_enabled",
        "gpt_model",
        "ai_openai_token_encrypted",
        "ai_openai_token_updated_at",
        "ai_channel_limit_enabled",
        "ai_admin_channel_bypass_enabled",
        "ai_admin_user_ids",
    }
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'config_server_settings'
          AND COLUMN_NAME IN (%s, %s, %s, %s, %s, %s, %s)
        """,
        tuple(sorted(required_columns)),
    )
    available = {
        str(row.get("COLUMN_NAME"))
        for row in (cursor.fetchall() or [])
        if row.get("COLUMN_NAME")
    }
    missing = sorted(required_columns - available)
    if missing:
        logging.error(
            "AI schema incompatível em config_server_settings; colunas ausentes=%s",
            ",".join(missing),
        )
        return False
    return True




def _get_ai_fernet_key_from_env() -> bytes | None:
    raw_value = os.getenv("OPENAI_TOKEN_ENCRYPTION_KEY")
    if not isinstance(raw_value, str):
        return None

    normalized = raw_value.strip()
    if not normalized:
        return None

    try:
        decoded = base64.urlsafe_b64decode(normalized.encode("utf-8"))
        if len(decoded) == 32:
            return normalized.encode("utf-8")
    except Exception:
        pass

    derived = hashlib.sha256(normalized.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(derived)


def _get_ai_encryption_key() -> bytes | None:
    """Return the stable environment-configured Fernet key."""

    encryption_key = _get_ai_fernet_key_from_env()
    if encryption_key:
        return encryption_key

    logging.error(
        "Chave de criptografia OpenAI não configurada; defina OPENAI_TOKEN_ENCRYPTION_KEY."
    )
    return None


def _get_legacy_ai_encryption_key() -> str | None:
    key = os.getenv("OPENAI_TOKEN_ENCRYPTION_KEY")
    if not isinstance(key, str):
        return None
    normalized = key.strip()
    return normalized or None


def _decrypt_legacy_text(value: str) -> str | None:
    try:
        encryption_key = _get_legacy_ai_encryption_key()
        if not encryption_key:
            return None
        encrypted = base64.urlsafe_b64decode(value.encode("utf-8"))
        key_digest = hashlib.sha256(encryption_key.encode("utf-8")).digest()
        decrypted = bytes(
            byte ^ key_digest[index % len(key_digest)]
            for index, byte in enumerate(encrypted)
        )
        decoded = decrypted.decode("utf-8").strip()
        return decoded or None
    except Exception:
        return None


def _encrypt_text(value: str) -> str:
    encryption_key = _get_ai_encryption_key()
    if not encryption_key:
        raise ValueError("Não foi possível inicializar a chave de criptografia local.")
    encrypted = Fernet(encryption_key).encrypt(value.encode("utf-8"))
    return encrypted.decode("utf-8")


def _decrypt_text(value: str) -> tuple[str | None, bool]:
    encryption_key = _get_ai_encryption_key()
    if encryption_key:
        try:
            decrypted = Fernet(encryption_key).decrypt(value.encode("utf-8")).decode("utf-8").strip()
            if decrypted:
                return decrypted, False
        except InvalidToken:
            pass
        except Exception:
            return None, False

    legacy_decrypted = _decrypt_legacy_text(value)
    if legacy_decrypted:
        return legacy_decrypted, True
    return None, False


def get_ai_response_settings(guild_id: int) -> dict[str, Any]:
    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_ai_settings_columns(cursor):
            return {}

        cursor.execute(
            """
            SELECT
                has_gpt_enabled,
                gpt_model,
                ai_openai_token_encrypted,
                ai_openai_token_updated_at,
                ai_channel_limit_enabled,
                ai_admin_channel_bypass_enabled,
                ai_admin_user_ids
            FROM config_server_settings
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )
        row = cursor.fetchone() or {}

    raw_admin_ids = row.get("ai_admin_user_ids")
    admin_ids: list[int] = []
    if isinstance(raw_admin_ids, str):
        for item in raw_admin_ids.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                admin_ids.append(int(item))
            except (TypeError, ValueError):
                continue

    token = row.get("ai_openai_token_encrypted")
    token_configured = isinstance(token, str) and bool(token.strip())
    decrypted_token = None
    token_decryption_failed = False
    if token_configured:
        decrypted_token, should_migrate_token = _decrypt_text(token)
        token_decryption_failed = decrypted_token is None
        if token_decryption_failed:
            logging.error(
                "Falha ao descriptografar credencial OpenAI; guild_id=%s ciphertext preservado",
                guild_id,
            )
        elif should_migrate_token:
            try:
                with pooled_connection() as cursor:
                    cursor.execute(
                        """
                        UPDATE config_server_settings
                        SET ai_openai_token_encrypted = %s
                        WHERE server_guild_id = %s
                        """,
                        (_encrypt_text(decrypted_token), guild_id),
                    )
            except Exception as err:
                logging.warning(
                    "Falha ao migrar token OpenAI legado para Fernet; guild_id=%s error=%s",
                    guild_id,
                    type(err).__name__,
                )

    token_updated_at = row.get("ai_openai_token_updated_at")
    return {
        "enabled": bool(row.get("has_gpt_enabled")),
        "model": row.get("gpt_model"),
        "openaiToken": decrypted_token,
        "tokenConfigured": token_configured,
        "tokenUpdatedAt": token_updated_at.isoformat() if isinstance(token_updated_at, datetime) else None,
        "tokenDecryptionFailed": token_decryption_failed,
        "channelLimitEnabled": bool(row.get("ai_channel_limit_enabled")),
        "adminChannelBypassEnabled": bool(row.get("ai_admin_channel_bypass_enabled")),
        "adminUserIds": sorted(set(admin_ids)),
    }


_UNSET = object()


def set_ai_response_settings(
    guild_id: int,
    *,
    enabled: bool | None = None,
    model: str | None = None,
    openai_token: Any = _UNSET,
    channel_limit_enabled: bool | None = None,
    admin_channel_bypass_enabled: bool | None = None,
    admin_user_ids: list[int] | None = None,
) -> bool:
    updates: dict[str, Any] = {}

    if enabled is not None:
        updates["has_gpt_enabled"] = bool(enabled)
    if model is not None:
        updates["gpt_model"] = str(model).strip() or None
    if openai_token is not _UNSET:
        normalized_token = "" if openai_token is None else str(openai_token).strip()
        try:
            updates["ai_openai_token_encrypted"] = _encrypt_text(normalized_token) if normalized_token else None
        except ValueError:
            return False
    if channel_limit_enabled is not None:
        updates["ai_channel_limit_enabled"] = bool(channel_limit_enabled)
    if admin_channel_bypass_enabled is not None:
        updates["ai_admin_channel_bypass_enabled"] = bool(admin_channel_bypass_enabled)
    if admin_user_ids is not None:
        sanitized: list[int] = []
        for user_id in admin_user_ids:
            try:
                parsed = int(user_id)
            except (TypeError, ValueError):
                continue
            if parsed > 0 and parsed not in sanitized:
                sanitized.append(parsed)
        updates["ai_admin_user_ids"] = ",".join(str(user_id) for user_id in sanitized) or None

    if not updates:
        return False

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_ai_settings_columns(cursor):
            return False

    return updateServerConfig(guild_id, **updates)


def getCommandVisibleChannels(command_name: str, guild_id: int | None = None) -> list[int]:
    """Compatibility wrapper for command visibility channel lookup."""

    if guild_id is None:
        return []
    return get_allowed_feature_channels(guild_id, command_name)


def getLogConfig(guild_id: int, log_type: str):
    """Retrieve logging configuration for a given guild and log type."""
    with pooled_connection() as cursor:
        query = (
            "SELECT enabled, log_channel FROM config_logs "
            "WHERE server_guild_id = %s AND type = %s"
        )
        cursor.execute(query, (guild_id, log_type))
        return cursor.fetchone()


def getAllLogConfigs(guild_id: int) -> list[dict]:
    """Return every log configuration stored for a guild."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT type, enabled, log_channel
            FROM config_logs
            WHERE server_guild_id = %s
            ORDER BY type ASC
            """,
            (guild_id,),
        )
        return cursor.fetchall() or []


def upsertLogConfig(guild_id: int, log_type: str, enabled: bool, log_channel: int | None) -> bool:
    """Create or update a log configuration row for a guild and type."""

    with pooled_connection() as cursor:
        try:
            cursor.execute(
                "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
                (guild_id,),
            )
            cursor.execute(
                """
                INSERT INTO config_logs
                    (server_guild_id, type, enabled, log_channel)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    enabled = VALUES(enabled),
                    log_channel = VALUES(log_channel)
                """,
                (guild_id, log_type, int(bool(enabled)), log_channel),
            )
            return True
        except mysql.connector.Error as err:
            logging.error("Database error while upserting log config: %s", err)
            return False


def recordBanDiscordEffects(ban_id: int, effects) -> bool:
    """Persist the whole Discord-effect batch and effective membership state atomically."""
    effects = list(effects)
    try:
        with pooled_connection() as cursor:
            for effect in effects:
                cursor.execute(
                    """
                    INSERT INTO user_ban_discord_effects (
                        ban_id, identity_user_id, discord_user_id, is_origin,
                        outcome, error_code
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        identity_user_id = VALUES(identity_user_id),
                        is_origin = VALUES(is_origin),
                        error_code = IF(outcome = 'APPLIED', error_code, VALUES(error_code)),
                        attempted_at = IF(
                            outcome = 'APPLIED', attempted_at, UTC_TIMESTAMP(6)
                        ),
                        outcome = IF(outcome = 'APPLIED', outcome, VALUES(outcome))
                    """,
                    (
                        ban_id,
                        effect.identity_user_id,
                        effect.discord_user_id,
                        effect.is_origin,
                        effect.outcome,
                        effect.error_code,
                    ),
                )

            for identity_user_id in {
                int(effect.identity_user_id)
                for effect in effects
            }:
                cursor.execute(
                    """
                    UPDATE user_community_status
                    SET banned = 1
                    WHERE user_id = %s
                      AND community_id = (
                          SELECT community_id
                          FROM user_bans
                          WHERE id = %s
                      )
                    """,
                    (identity_user_id, ban_id),
                )
    except Exception as error:
        logging.error(
            "Erro ao registrar efeitos Discord do banimento %s: %s",
            ban_id,
            type(error).__name__,
        )
        return False
    return True


def banBelongsToGuild(ban_id: int, guild_id: int) -> bool:
    """Verify the parent moderation action belongs to the requested Discord guild."""
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM user_bans ub
            JOIN community_discord cd ON cd.community_id = ub.community_id
            WHERE ub.id = %s
              AND cd.guild_id = %s
              AND cd.active = 1
              AND ub.revoked_at IS NULL
              AND (
                  ub.valid_until IS NULL
                  OR ub.valid_until > NOW()
              )
            LIMIT 1
            """,
            (ban_id, guild_id),
        )
        return cursor.fetchone() is not None


def getSatisfiedBanEffectDiscordIds(ban_id: int) -> set[int]:
    """Return effects already satisfied for one administrative ban action."""
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT discord_user_id
            FROM user_ban_discord_effects
            WHERE ban_id = %s
              AND outcome IN ('APPLIED', 'ALREADY_BANNED')
            """,
            (ban_id,),
        )
        return {
            int(row["discord_user_id"])
            for row in (cursor.fetchall() or [])
        }


def hasOtherActiveBanRequirement(
    guild_id: int,
    ban_id: int,
    discord_user_id: int,
) -> bool:
    """Prevent unban while another active Community action still requires it."""
    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT community_id FROM community_discord WHERE guild_id = %s",
            (guild_id,),
        )
        community_row = cursor.fetchone()
        if not community_row:
            return False

        community_id = community_row["community_id"]
        cursor.execute(
            """
            SELECT 1
            FROM user_bans other_ban
            WHERE other_ban.community_id = %s
              AND other_ban.id <> %s
              AND other_ban.revoked_at IS NULL
              AND (other_ban.valid_until IS NULL OR other_ban.valid_until > NOW())
              AND (
                  EXISTS (
                      SELECT 1
                      FROM user_ban_discord_effects other_effect
                      WHERE other_effect.ban_id = other_ban.id
                        AND other_effect.discord_user_id = %s
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM user_discord legacy_account
                      WHERE legacy_account.user_id = other_ban.user_id
                        AND legacy_account.discord_user_id = %s
                  )
              )
            LIMIT 1
            """,
            (community_id, ban_id, discord_user_id, discord_user_id),
        )
        return cursor.fetchone() is not None


def upsertActiveCallLog(
    guild_id: int,
    channel_id: int,
    payload: dict,
    log_message_id: Optional[int] = None,
) -> bool:
    """Persist active call state so the running log survives process restarts."""

    payload_json = json.dumps(payload, ensure_ascii=False)
    with pooled_connection() as cursor:
        try:
            cursor.execute(
                """
                INSERT INTO active_call_logs
                    (server_guild_id, voice_channel_id, log_message_id, payload_json)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    log_message_id = VALUES(log_message_id),
                    payload_json = VALUES(payload_json)
                """,
                (guild_id, channel_id, log_message_id, payload_json),
            )
            return True
        except mysql.connector.Error as err:
            logging.error("Database error while upserting active call log: %s", err)
            return False


def getActiveCallLogs(guild_id: int) -> list[dict]:
    """Fetch all active call logs for a guild."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT voice_channel_id, log_message_id, payload_json
            FROM active_call_logs
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )
        return cursor.fetchall()


def deleteActiveCallLog(guild_id: int, channel_id: int) -> bool:
    """Delete persisted active call state when the call is finished."""

    with pooled_connection() as cursor:
        try:
            cursor.execute(
                """
                DELETE FROM active_call_logs
                WHERE server_guild_id = %s AND voice_channel_id = %s
                """,
                (guild_id, channel_id),
            )
            return True
        except mysql.connector.Error as err:
            logging.error("Database error while deleting active call log: %s", err)
            return False


def _ensure_voice_sessions_table() -> None:
    global _VOICE_SESSIONS_TABLE_READY
    with _VOICE_SESSIONS_TABLE_LOCK:
        if _VOICE_SESSIONS_TABLE_READY:
            return
        with pooled_connection() as cursor:
            try:
                cursor.execute("RENAME TABLE voice_xp_sessions TO voice_sessions")
            except mysql.connector.Error:
                pass

        with pooled_connection() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS voice_sessions (
                    server_guild_id BIGINT NOT NULL,
                    discord_user_id BIGINT NOT NULL,
                    voice_channel_id BIGINT NOT NULL,
                    started_at DATETIME NOT NULL,
                    last_tick_at DATETIME NOT NULL,
                    is_eligible TINYINT(1) NOT NULL DEFAULT 0,
                    is_self_muted TINYINT(1) NOT NULL DEFAULT 0,
                    is_self_deafened TINYINT(1) NOT NULL DEFAULT 0,
                    is_server_muted TINYINT(1) NOT NULL DEFAULT 0,
                    is_server_deafened TINYINT(1) NOT NULL DEFAULT 0,
                    PRIMARY KEY (server_guild_id, discord_user_id)
                )
                """
            )
        _VOICE_SESSIONS_TABLE_READY = True


def upsertVoiceSession(session: dict[str, Any]) -> bool:
    with pooled_connection() as cursor:
        try:
            cursor.execute(
                """
                INSERT INTO voice_sessions
                    (server_guild_id, discord_user_id, voice_channel_id, started_at, last_tick_at,
                     is_eligible, is_self_muted, is_self_deafened, is_server_muted, is_server_deafened)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    voice_channel_id = VALUES(voice_channel_id),
                    started_at = VALUES(started_at),
                    last_tick_at = VALUES(last_tick_at),
                    is_eligible = VALUES(is_eligible),
                    is_self_muted = VALUES(is_self_muted),
                    is_self_deafened = VALUES(is_self_deafened),
                    is_server_muted = VALUES(is_server_muted),
                    is_server_deafened = VALUES(is_server_deafened)
                """,
                (
                    session["guild_id"],
                    session["user_id"],
                    session["channel_id"],
                    session["started_at"],
                    session["last_tick_at"],
                    int(bool(session.get("is_eligible"))),
                    int(bool(session.get("is_self_muted"))),
                    int(bool(session.get("is_self_deafened"))),
                    int(bool(session.get("is_server_muted"))),
                    int(bool(session.get("is_server_deafened"))),
                ),
            )
            return True
        except mysql.connector.Error as err:
            logging.error("Database error while upserting voice session: %s", err)
            return False


def getVoiceSessions(guild_id: int) -> list[dict]:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT server_guild_id, discord_user_id, voice_channel_id, started_at, last_tick_at,
                   is_eligible, is_self_muted, is_self_deafened, is_server_muted, is_server_deafened
            FROM voice_sessions
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )
        return cursor.fetchall() or []


def deleteVoiceSession(guild_id: int, user_id: int) -> bool:
    with pooled_connection() as cursor:
        try:
            cursor.execute(
                """
                DELETE FROM voice_sessions
                WHERE server_guild_id = %s AND discord_user_id = %s
                """,
                (guild_id, user_id),
            )
            return True
        except mysql.connector.Error as err:
            logging.error("Database error while deleting voice session: %s", err)
            return False


_LEVEL_CONFIG_CACHE: dict[int, tuple[float, dict]] = {}
_LEVEL_CONFIG_CACHE_LOCK = RLock()
_LEVEL_CONFIG_CACHE_TTL_SECONDS = 300.0
_VOICE_SESSIONS_TABLE_READY = False
_VOICE_SESSIONS_TABLE_LOCK = RLock()

_DEFAULT_LEVEL_CONFIG: dict[str, Any] = {
    "guildId": 0,
    "clearOnExit": False,
    "levelupWarning": False,
    "levelupWarningChannel": None,
    "levelUpMessage": None,
    "multiplier": None,
    "phase1K": Decimal("45.000"),
    "phase1P": Decimal("2.000"),
    "phase1B": Decimal("0.000"),
    "dailyCombo": None,
    "comboMultiplier": None,
    "xpBasePerMin": 2,
    "voiceSocialBonusPct": Decimal("0.000"),
    "voiceSocialBonusMinHumans": 2,
    "voiceDiminishingWindow1Minutes": 60,
    "voiceDiminishingWindow2Minutes": 120,
    "voiceDiminishingFactor2": Decimal("0.600"),
    "voiceDiminishingFactor3": Decimal("0.300"),
    "voiceDailyCapXp": 600,
    "textDailyCapXp": 300,
    "globalDailyCapXp": 900,
    "textXpEnabled": True,
    "voiceXpEnabled": True,
    "textXpBaseMin": 8,
    "textXpBaseMax": 16,
    "textXpCooldownMinSeconds": 30,
    "textXpCooldownMaxSeconds": 60,
    "levelReconcileRequired": False,
    "levelReconcileRequestedAt": None,
    "levelReconciledAt": None,
}


def _normalize_level_config_row(row: Optional[dict], guild_id: int) -> dict:
    row_data = row or {}
    config = dict(_DEFAULT_LEVEL_CONFIG)
    config.update(
        {
            "guildId": guild_id,
            "clearOnExit": bool(row_data.get("clear_on_exit") or 0),
            "levelupWarning": bool(row_data.get("levelup_warning") if row_data.get("levelup_warning") is not None else 0),
            "levelupWarningChannel": row_data.get("levelup_warning_channel"),
            "levelUpMessage": row_data.get("level_up_message"),
            "multiplier": row_data.get("multiplier"),
            "phase1K": row_data.get("phase1_k")
            if row_data.get("phase1_k") is not None
            else _DEFAULT_LEVEL_CONFIG["phase1K"],
            "phase1P": row_data.get("phase1_p")
            if row_data.get("phase1_p") is not None
            else _DEFAULT_LEVEL_CONFIG["phase1P"],
            "phase1B": row_data.get("phase1_b")
            if row_data.get("phase1_b") is not None
            else _DEFAULT_LEVEL_CONFIG["phase1B"],
            "dailyCombo": row_data.get("daily_combo"),
            "comboMultiplier": row_data.get("combo_multiplier"),
            "xpBasePerMin": int(
                row_data.get("xp_base_per_min")
                if row_data.get("xp_base_per_min") is not None
                else _DEFAULT_LEVEL_CONFIG["xpBasePerMin"]
            ),
            "voiceSocialBonusPct": row_data.get("voice_social_bonus_pct")
            if row_data.get("voice_social_bonus_pct") is not None
            else _DEFAULT_LEVEL_CONFIG["voiceSocialBonusPct"],
            "voiceSocialBonusMinHumans": int(
                row_data.get("voice_social_bonus_min_humans")
                if row_data.get("voice_social_bonus_min_humans") is not None
                else _DEFAULT_LEVEL_CONFIG["voiceSocialBonusMinHumans"]
            ),
            "voiceDiminishingWindow1Minutes": int(
                row_data.get("voice_diminishing_window1_minutes")
                if row_data.get("voice_diminishing_window1_minutes") is not None
                else _DEFAULT_LEVEL_CONFIG["voiceDiminishingWindow1Minutes"]
            ),
            "voiceDiminishingWindow2Minutes": int(
                row_data.get("voice_diminishing_window2_minutes")
                if row_data.get("voice_diminishing_window2_minutes") is not None
                else _DEFAULT_LEVEL_CONFIG["voiceDiminishingWindow2Minutes"]
            ),
            "voiceDiminishingFactor2": row_data.get("voice_diminishing_factor2")
            if row_data.get("voice_diminishing_factor2") is not None
            else _DEFAULT_LEVEL_CONFIG["voiceDiminishingFactor2"],
            "voiceDiminishingFactor3": row_data.get("voice_diminishing_factor3")
            if row_data.get("voice_diminishing_factor3") is not None
            else _DEFAULT_LEVEL_CONFIG["voiceDiminishingFactor3"],
            "voiceDailyCapXp": int(
                row_data.get("voice_daily_cap_xp")
                if row_data.get("voice_daily_cap_xp") is not None
                else _DEFAULT_LEVEL_CONFIG["voiceDailyCapXp"]
            ),
            "textDailyCapXp": int(
                row_data.get("text_daily_cap_xp")
                if row_data.get("text_daily_cap_xp") is not None
                else _DEFAULT_LEVEL_CONFIG["textDailyCapXp"]
            ),
            "globalDailyCapXp": int(
                row_data.get("global_daily_cap_xp")
                if row_data.get("global_daily_cap_xp") is not None
                else _DEFAULT_LEVEL_CONFIG["globalDailyCapXp"]
            ),
            "textXpEnabled": bool(
                row_data.get("text_xp_enabled")
                if row_data.get("text_xp_enabled") is not None
                else _DEFAULT_LEVEL_CONFIG["textXpEnabled"]
            ),
            "voiceXpEnabled": bool(
                row_data.get("voice_xp_enabled")
                if row_data.get("voice_xp_enabled") is not None
                else _DEFAULT_LEVEL_CONFIG["voiceXpEnabled"]
            ),
            "textXpBaseMin": int(
                row_data.get("text_xp_base_min")
                if row_data.get("text_xp_base_min") is not None
                else _DEFAULT_LEVEL_CONFIG["textXpBaseMin"]
            ),
            "textXpBaseMax": int(
                row_data.get("text_xp_base_max")
                if row_data.get("text_xp_base_max") is not None
                else _DEFAULT_LEVEL_CONFIG["textXpBaseMax"]
            ),
            "textXpCooldownMinSeconds": int(
                row_data.get("text_xp_cooldown_min_seconds")
                if row_data.get("text_xp_cooldown_min_seconds") is not None
                else _DEFAULT_LEVEL_CONFIG["textXpCooldownMinSeconds"]
            ),
            "textXpCooldownMaxSeconds": int(
                row_data.get("text_xp_cooldown_max_seconds")
                if row_data.get("text_xp_cooldown_max_seconds") is not None
                else _DEFAULT_LEVEL_CONFIG["textXpCooldownMaxSeconds"]
            ),
            "levelReconcileRequired": bool(row_data.get("level_reconcile_required") or 0),
            "levelReconcileRequestedAt": row_data.get("level_reconcile_requested_at"),
            "levelReconciledAt": row_data.get("level_reconciled_at"),
        }
    )
    return config


def _validate_level_config_payload(payload: Mapping[str, Any], current_config: Mapping[str, Any] | None = None) -> None:
    validators: list[tuple[str, Any]] = [
        # Current progression formula: kL^p + bL.
        # Keep p >= 1 and at least one linear/power coefficient >= 1 to avoid
        # extremely flat integer-rounded curves that break inversion performance.
        ("phase1_k", lambda value: Decimal(str(value)) > 0),
        ("phase1_p", lambda value: Decimal(str(value)) >= 1),
        ("phase1_b", lambda value: Decimal(str(value)) >= 0),
        ("multiplier", lambda value: value is None or Decimal(str(value)) > 0),
        ("daily_combo", lambda value: value is None or int(value) >= 1),
        ("combo_multiplier", lambda value: value is None or Decimal(str(value)) >= 1),
        ("clear_on_exit", lambda value: int(value) in (0, 1)),
        ("levelup_warning", lambda value: int(value) in (0, 1)),
        ("xp_base_per_min", lambda value: int(value) >= 0),
        ("voice_social_bonus_pct", lambda value: Decimal(str(value)) >= 0),
        ("voice_social_bonus_min_humans", lambda value: int(value) >= 2),
        ("voice_diminishing_window1_minutes", lambda value: int(value) >= 1),
        ("voice_diminishing_window2_minutes", lambda value: int(value) >= 1),
        ("voice_diminishing_factor2", lambda value: Decimal(str(value)) >= 0),
        ("voice_diminishing_factor3", lambda value: Decimal(str(value)) >= 0),
        ("voice_daily_cap_xp", lambda value: int(value) >= 0),
        ("text_daily_cap_xp", lambda value: int(value) >= 0),
        ("global_daily_cap_xp", lambda value: int(value) >= 0),
        ("text_xp_enabled", lambda value: int(value) in (0, 1)),
        ("voice_xp_enabled", lambda value: int(value) in (0, 1)),
        ("text_xp_base_min", lambda value: int(value) >= 0),
        ("text_xp_base_max", lambda value: int(value) >= 0),
        ("text_xp_cooldown_min_seconds", lambda value: int(value) >= 0),
        ("text_xp_cooldown_max_seconds", lambda value: int(value) >= 0),
    ]

    for key, validator in validators:
        if key not in payload:
            continue
        try:
            if not validator(payload[key]):
                raise ValueError(f"Invalid value for {key}: {payload[key]}")
        except (ArithmeticError, ValueError, TypeError) as error:
            raise ValueError(f"Invalid value for {key}: {payload[key]}") from error

    if "phase1_k" in payload or "phase1_b" in payload:
        phase1_k = Decimal(str(payload.get("phase1_k", _DEFAULT_LEVEL_CONFIG["phase1K"])))
        phase1_b = Decimal(str(payload.get("phase1_b", _DEFAULT_LEVEL_CONFIG["phase1B"])))
        if phase1_k < 1 and phase1_b < 1:
            raise ValueError("Invalid value for phase1_k/phase1_b: at least one must be >= 1")
    if "voice_diminishing_window1_minutes" in payload or "voice_diminishing_window2_minutes" in payload:
        window1 = int(payload.get("voice_diminishing_window1_minutes", _DEFAULT_LEVEL_CONFIG["voiceDiminishingWindow1Minutes"]))
        window2 = int(payload.get("voice_diminishing_window2_minutes", _DEFAULT_LEVEL_CONFIG["voiceDiminishingWindow2Minutes"]))
        if window2 < window1:
            raise ValueError("Invalid voice diminishing windows: window2 must be >= window1")

    current_config = current_config or {}

    text_xp_min = int(
        payload.get(
            "text_xp_base_min",
            current_config.get("textXpBaseMin", _DEFAULT_LEVEL_CONFIG["textXpBaseMin"]),
        )
    )
    text_xp_max = int(
        payload.get(
            "text_xp_base_max",
            current_config.get("textXpBaseMax", _DEFAULT_LEVEL_CONFIG["textXpBaseMax"]),
        )
    )
    if text_xp_max < text_xp_min:
        raise ValueError("Invalid text XP range: max must be >= min")

    cooldown_min = int(
        payload.get(
            "text_xp_cooldown_min_seconds",
            current_config.get(
                "textXpCooldownMinSeconds",
                _DEFAULT_LEVEL_CONFIG["textXpCooldownMinSeconds"],
            ),
        )
    )
    cooldown_max = int(
        payload.get(
            "text_xp_cooldown_max_seconds",
            current_config.get(
                "textXpCooldownMaxSeconds",
                _DEFAULT_LEVEL_CONFIG["textXpCooldownMaxSeconds"],
            ),
        )
    )
    if cooldown_max < cooldown_min:
        raise ValueError("Invalid text XP cooldown range: max must be >= min")
    global_cap = int(
        payload.get(
            "global_daily_cap_xp",
            current_config.get("globalDailyCapXp", _DEFAULT_LEVEL_CONFIG["globalDailyCapXp"]),
        )
        or 0
    )
    text_cap = int(
        payload.get(
            "text_daily_cap_xp",
            current_config.get("textDailyCapXp", _DEFAULT_LEVEL_CONFIG["textDailyCapXp"]),
        )
        or 0
    )
    voice_cap = int(
        payload.get(
            "voice_daily_cap_xp",
            current_config.get("voiceDailyCapXp", _DEFAULT_LEVEL_CONFIG["voiceDailyCapXp"]),
        )
        or 0
    )
    positive_source_caps = [cap for cap in (text_cap, voice_cap) if cap > 0]
    if global_cap > 0 and positive_source_caps and global_cap > sum(positive_source_caps):
        raise ValueError(
            "Invalid daily caps: global_daily_cap_xp must be <= sum of positive source caps (text + voice)."
        )


def clearLevelConfigCache(guild_id: Optional[int] = None) -> None:
    with _LEVEL_CONFIG_CACHE_LOCK:
        if guild_id is None:
            _LEVEL_CONFIG_CACHE.clear()
        else:
            _LEVEL_CONFIG_CACHE.pop(guild_id, None)


def getLevelConfig(guild_id: int, use_cache: bool = True) -> dict:
    if guild_id is None:
        raise ValueError("guild_id é obrigatório para recuperar level config.")

    now_ts = time.time()
    if use_cache:
        with _LEVEL_CONFIG_CACHE_LOCK:
            cached_entry = _LEVEL_CONFIG_CACHE.get(guild_id)
            if cached_entry and now_ts - cached_entry[0] <= _LEVEL_CONFIG_CACHE_TTL_SECONDS:
                return dict(cached_entry[1])

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_levels (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        cursor.execute(
            """
            SELECT
                clear_on_exit,
                levelup_warning,
                levelup_warning_channel,
                level_up_message,
                multiplier,
                phase1_k,
                phase1_p,
                phase1_b,
                daily_combo,
                combo_multiplier,
                xp_base_per_min,
                voice_social_bonus_pct,
                voice_social_bonus_min_humans,
                voice_diminishing_window1_minutes,
                voice_diminishing_window2_minutes,
                voice_diminishing_factor2,
                voice_diminishing_factor3,
                voice_daily_cap_xp,
                text_daily_cap_xp,
                global_daily_cap_xp,
                text_xp_enabled,
                voice_xp_enabled,
                text_xp_base_min,
                text_xp_base_max,
                text_xp_cooldown_min_seconds,
                text_xp_cooldown_max_seconds,
                level_reconcile_required,
                level_reconcile_requested_at,
                level_reconciled_at
            FROM config_levels
            WHERE server_guild_id = %s
            LIMIT 1
            """,
            (guild_id,),
        )
        config = _normalize_level_config_row(cursor.fetchone(), guild_id)

    with _LEVEL_CONFIG_CACHE_LOCK:
        _LEVEL_CONFIG_CACHE[guild_id] = (now_ts, dict(config))
    return dict(config)


def updateLevelConfig(guild_id: int, **settings) -> dict:
    if guild_id is None:
        raise ValueError("guild_id é obrigatório para atualizar level config.")
    if not settings:
        return getLevelConfig(guild_id)

    current_config = getLevelConfig(guild_id)
    _validate_level_config_payload(settings, current_config=current_config)
    set_clause = ", ".join(f"{column} = %s" for column in settings.keys())
    curve_change_requested = any(
        field in settings for field in ("phase1_k", "phase1_p", "phase1_b")
    )
    if curve_change_requested:
        set_clause += (
            ", level_reconcile_required = 1, "
            "level_reconcile_requested_at = UTC_TIMESTAMP(6)"
        )
    values = list(settings.values()) + [guild_id]

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_levels (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        cursor.execute(
            f"""
            UPDATE config_levels
            SET {set_clause}
            WHERE server_guild_id = %s
            """,
            values,
        )

    clearLevelConfigCache(guild_id)
    return getLevelConfig(guild_id, use_cache=False)


def getPendingLevelReconciliationGuildIds(
    active_guild_ids: list[int] | tuple[int, ...] | set[int],
    limit: int = 10,
) -> list[int]:
    guild_ids = sorted({int(guild_id) for guild_id in active_guild_ids})
    if not guild_ids:
        return []

    safe_limit = max(1, min(int(limit), 100))
    placeholders = ", ".join(["%s"] * len(guild_ids))
    with pooled_connection() as cursor:
        cursor.execute(
            f"""
            SELECT server_guild_id
            FROM config_levels
            WHERE level_reconcile_required = 1
              AND server_guild_id IN ({placeholders})
            ORDER BY COALESCE(level_reconcile_requested_at, '1970-01-01'), server_guild_id
            LIMIT %s
            """,
            (*guild_ids, safe_limit),
        )
        return [int(row["server_guild_id"]) for row in (cursor.fetchall() or [])]


def reconcileGuildLevels(guild_id: int, batch_size: int = 500) -> dict[str, int | bool]:
    from core.levels import level_from_total_xp

    if guild_id is None:
        raise ValueError("guild_id é obrigatório para reconciliar níveis.")

    safe_batch_size = max(1, min(int(batch_size), 2000))
    config = getLevelConfig(guild_id, use_cache=False)
    if not bool(config.get("levelReconcileRequired")):
        return {"guildId": int(guild_id), "scanned": 0, "updated": 0, "completed": True}

    curve = (
        config["phase1K"],
        config["phase1P"],
        config["phase1B"],
    )
    last_id = 0
    scanned = 0
    updated = 0
    conflicts = 0

    while True:
        with pooled_connection() as cursor:
            cursor.execute(
                """
                SELECT id, total_xp, current_level
                FROM user_level
                WHERE server_guild_id = %s AND id > %s
                ORDER BY id ASC
                LIMIT %s
                """,
                (guild_id, last_id, safe_batch_size),
            )
            rows = list(cursor.fetchall() or [])
            if not rows:
                break

            changes: list[tuple[int, int, int, int]] = []
            for row in rows:
                row_id = int(row["id"])
                observed_total_xp = int(row.get("total_xp") or 0)
                new_level = level_from_total_xp(observed_total_xp, config)
                if int(row.get("current_level") or 0) != new_level:
                    changes.append((new_level, row_id, guild_id, observed_total_xp))
                last_id = row_id
            scanned += len(rows)

            for change in changes:
                cursor.execute(
                    """
                    UPDATE user_level
                    SET current_level = %s
                    WHERE id = %s
                      AND server_guild_id = %s
                      AND total_xp = %s
                    """,
                    change,
                )
                if cursor.rowcount == 1:
                    updated += 1
                else:
                    conflicts += 1

    completed = False
    if conflicts == 0:
        with pooled_connection() as cursor:
            cursor.execute(
                """
                UPDATE config_levels
                SET level_reconcile_required = 0,
                    level_reconciled_at = UTC_TIMESTAMP(6)
                WHERE server_guild_id = %s
                  AND level_reconcile_required = 1
                  AND phase1_k = %s
                  AND phase1_p = %s
                  AND phase1_b = %s
                """,
                (guild_id, curve[0], curve[1], curve[2]),
            )
            completed = cursor.rowcount == 1

    clearLevelConfigCache(guild_id)
    return {
        "guildId": int(guild_id),
        "scanned": scanned,
        "updated": updated,
        "conflicts": conflicts,
        "completed": completed,
    }


async def async_get_pending_level_reconciliation_guild_ids(
    active_guild_ids: list[int] | tuple[int, ...] | set[int],
    limit: int = 10,
) -> list[int]:
    return await asyncio.to_thread(
        getPendingLevelReconciliationGuildIds,
        active_guild_ids,
        limit,
    )


async def async_reconcile_guild_levels(guild_id: int, batch_size: int = 500) -> dict[str, int | bool]:
    return await asyncio.to_thread(reconcileGuildLevels, guild_id, batch_size)


async def async_get_level_config(guild_id: int, use_cache: bool = True) -> dict:
    return await asyncio.to_thread(getLevelConfig, guild_id, use_cache)


async def async_update_level_config(guild_id: int, **settings) -> dict:
    return await asyncio.to_thread(updateLevelConfig, guild_id, **settings)


def hasGPTEnabled(guild:discord.Guild):
    with pooled_connection() as cursor:
        query = f"""
        SELECT  has_gpt_enabled AS enabled,
                gpt_model AS model
        FROM config_server_settings
        WHERE server_guild_id = '{guild.id}';"""
        cursor.execute(query)
        return cursor.fetchone()


def updateServerConfig(guild_id: int, **settings) -> bool:
    """Update configuration flags for a guild.

    Parameters
    ----------
    guild_id: int
        Identifier of the guild whose configuration should be updated.
    **settings:
        Mapping of column names to the new values.

    Returns
    -------
    bool
        ``True`` if the update ran, ``False`` if no fields were provided.
    """
    if not settings:
        return False

    set_clause = ", ".join(f"{column} = %s" for column in settings)
    values = list(settings.values()) + [guild_id]

    with pooled_connection() as cursor:
        query = (
            "UPDATE config_server_settings "
            f"SET {set_clause} "
            "WHERE server_guild_id = %s"
        )
        cursor.execute(query, values)
        return True


def get_portaria_base_config(guild_id: int) -> dict:
    """Fetch base portaria config for a guild, creating an empty row if needed."""

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT INTO portaria_base_config (server_guild_id) VALUES (%s) "
            "ON DUPLICATE KEY UPDATE server_guild_id = VALUES(server_guild_id)",
            (guild_id,),
        )
        cursor.execute(
            """
            SELECT
                server_guild_id,
                acesso_provisorio_role_id,
                visitante_role_id,
                maior_18_role_id,
                menor_18_role_id,
                aprovacao_acesso_provisorio_ativo,
                aprovacao_acesso_provisorio_duracao_dias,
                formulario_portaria_ativo,
                idade_minima_conta_ficha_ativa,
                idade_minima_conta_ficha_dias,
                idade_minima_conta_acesso_provisorio_ativa,
                idade_minima_conta_acesso_provisorio_dias,
                idade_minima_entrada_servidor_ativa,
                idade_minima_entrada_servidor_anos
            FROM portaria_base_config
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )
        return cursor.fetchone() or {}


def update_user_community_status_invite_info(
    guild_id: int,
    user: Union[discord.Member, discord.User],
    invite_link_used: str | None,
    invited_by: str | None,
) -> bool:
    """Store invite metadata for a user's community status row."""

    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        cursor.execute(
            "SELECT user_id FROM user_discord WHERE discord_user_id = %s",
            (user.id,),
        )
        identity = cursor.fetchone()
        if not identity:
            return False
        cursor.execute(
            "UPDATE user_community_status "
            "SET invite_link_used = %s, invited_by = %s "
            "WHERE user_id = %s AND community_id = %s",
            (invite_link_used, invited_by, identity["user_id"], community_id),
        )
        return cursor.rowcount == 1


def get_user_community_status_invite_link(
    guild_id: int,
    user: Union[discord.Member, discord.User],
) -> str | None:
    """Return the invite link stored for this user in user_community_status."""

    user_id = includeUser(user, guild_id)
    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        cursor.execute(
            "SELECT invite_link_used "
            "FROM user_community_status "
            "WHERE user_id = %s AND community_id = %s "
            "LIMIT 1",
            (user_id, community_id),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return row.get("invite_link_used")


def normalize_discord_invite_code(invite_value: str | None) -> str | None:
    """Extract and normalize a Discord invite code from raw values/URLs."""

    if not invite_value:
        return None

    code = str(invite_value).strip()
    if not code:
        return None

    code = code.removeprefix("https://").removeprefix("http://")
    for host_prefix in (
        "discord.gg/",
        "discord.com/invite/",
        "www.discord.gg/",
        "www.discord.com/invite/",
        "discordapp.com/invite/",
    ):
        if code.casefold().startswith(host_prefix):
            code = code[len(host_prefix):]
            break

    code = code.split("?", 1)[0].split("#", 1)[0].split("/", 1)[0].strip()
    if not code:
        return None
    return code


def get_portaria_invite_bypass_codes(guild_id: int) -> list[str]:
    """Return effective canonical invite bypasses for this guild."""

    config = get_portaria_base_config(guild_id)
    if not bool(config.get("portaria_enabled", 1)):
        return []

    parsed: list[str] = []
    seen: set[str] = set()

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT invite_code
            FROM portaria_bypasses
            WHERE server_guild_id = %s
              AND bypass_type = 'invite'
              AND active = 1
              AND removed_at IS NULL
              AND expired_at IS NULL
              AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP(6))
            ORDER BY id
            """,
            (guild_id,),
        )
        for row in cursor.fetchall() or []:
            code = normalize_discord_invite_code(row.get("invite_code"))
            if code and code not in seen:
                seen.add(code)
                parsed.append(code)

    return parsed

def set_portaria_account_release_override(
    guild_id: int,
    user_id: int,
    access_mode: str,
    requires_form: bool = True,
    released_by: int | None = None,
) -> bool:
    """Deprecated compatibility hook.

    Administrative bypass writes moved to the API control plane. New runtime
    code must not call this function.
    """

    logging.warning(
        "Ignored deprecated runtime Portaria account-bypass write for guild=%s user=%s",
        guild_id,
        user_id,
    )
    return False

def get_portaria_account_release_override(guild_id: int, user_id: int) -> dict | None:
    """Get an effective persistent account bypass scoped to this guild.

    Any canonical row is authoritative, including inactive/expired/removed
    rows. The legacy bridge is consulted only when no canonical row exists.
    """

    config = get_portaria_base_config(guild_id)
    if not bool(config.get("portaria_enabled", 1)):
        return None

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT
                server_guild_id,
                discord_user_id AS user_id,
                access_mode,
                requires_form,
                active,
                expires_at,
                expired_at,
                removed_at,
                created_at,
                updated_at
            FROM portaria_bypasses
            WHERE server_guild_id = %s
              AND bypass_type = 'account'
              AND discord_user_id = %s
            LIMIT 1
            """,
            (guild_id, user_id),
        )
        canonical = cursor.fetchone()
        if canonical is not None:
            expires_at = canonical.get("expires_at")
            effective = (
                bool(canonical.get("active"))
                and canonical.get("removed_at") is None
                and canonical.get("expired_at") is None
                and (expires_at is None or expires_at > datetime.utcnow())
            )
            return canonical if effective else None

        # Rollout bridge: only accounts not represented in canonical storage
        # may still use rows written by the previously deployed runtime.
        cursor.execute(
            """
            SELECT
                server_guild_id,
                user_id,
                access_mode,
                requires_form,
                released_by,
                created_at,
                updated_at
            FROM portaria_account_release
            WHERE server_guild_id = %s
              AND user_id = %s
            LIMIT 1
            """,
            (guild_id, user_id),
        )
        return cursor.fetchone()

def clear_portaria_account_release_override(guild_id: int, user_id: int) -> bool:
    """Deprecated no-op kept for rollout compatibility.

    Account bypasses are persistent rules now. They remain effective until an
    administrator disables/removes them or their optional expiration is reached.
    """

    return False

def update_portaria_base_config(guild_id: int, **settings) -> bool:
    """Update base portaria config fields for a guild."""

    if not settings:
        return False

    set_clause = ", ".join(f"{column} = %s" for column in settings)
    values = [*settings.values(), guild_id]

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT INTO portaria_base_config (server_guild_id) VALUES (%s) "
            "ON DUPLICATE KEY UPDATE server_guild_id = VALUES(server_guild_id)",
            (guild_id,),
        )
        cursor.execute(
            f"UPDATE portaria_base_config SET {set_clause} WHERE server_guild_id = %s",
            values,
        )
        return True

def getAllLocals():
    with pooled_connection() as cursor:
        query = f"""SELECT * FROM locale"""
        cursor.execute(query)
        return cursor.fetchall()


def getUserId(discord_user_id: int):
    """Fetch the internal user id for a Discord member."""
    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT user_id FROM user_discord WHERE discord_user_id = %s",
            (discord_user_id,)
        )
        row = cursor.fetchone()
        return row["user_id"] if row else None


async def async_getUserId(discord_user_id: int):
    """Fetch the internal user id for a Discord member using async pool."""
    async with async_pooled_connection() as cursor:
        await cursor.execute(
            "SELECT user_id FROM user_discord WHERE discord_user_id = %s",
            (discord_user_id,),
        )
        row = await cursor.fetchone()
        return row["user_id"] if row else None




def _ensure_user_community_status(
    cursor: MySQLCursorAbstract,
    user_id: int,
    community_id: int,
    member_since: str | None,
    last_join_date: str | None,
    approved: int,
    approved_at_str: str | None,
    is_discord_member: bool,
) -> None:
    """Compatibility fallback for a missing Community aggregate row.

    Network membership and lifecycle transitions are API-owned. Existing
    user_community_status rows are therefore never refreshed here: using an
    XP/economy command must not revive presence, rewrite join dates, or change
    approval state. A missing aggregate may still be created so legacy feature
    tables can resolve the User/Community pair before the next API observation.
    """

    cursor.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
    if cursor.fetchone() is None:
        raise RuntimeError(f"User {user_id} disappeared while creating community membership")

    cursor.execute(
        "SELECT id FROM user_community_status "
        "WHERE user_id = %s AND community_id = %s "
        "ORDER BY id ASC LIMIT 1",
        (user_id, community_id),
    )
    if cursor.fetchone():
        return

    try:
        cursor.execute(
            "INSERT INTO user_community_status "
            "(user_id, community_id, member_since, last_join_date, approved, approved_at, "
            "is_vip, banned, is_present, left_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, 0, 0, %s, NULL)",
            (
                user_id,
                community_id,
                member_since,
                last_join_date,
                approved,
                approved_at_str,
                bool(is_discord_member),
            ),
        )
    except mysql.connector.Error as err:
        if err.errno != errorcode.ER_DUP_ENTRY:
            raise
        # Another writer won the race. The existing aggregate is authoritative
        # until the API recomputes it from network-scoped evidence.
        return



def includeUser(user: Union[discord.Member, discord.User, str], guildId: int | None = None, approvedAt: datetime = None) -> int:
    """Ensure a user and exactly one current Community membership exist."""

    if guildId is None:
        raise ValueError("guildId é obrigatório para incluir usuários com isolamento por servidor")

    with pooled_connection(True) as cursor:
        cursor.execute(
            "SELECT community_id FROM community_discord WHERE guild_id = %s",
            (guildId,),
        )
        community = cursor.fetchone()
        if not community:
            raise Exception(f"Comunidade não encontrada para guild_id={guildId}")
        community_id = community["community_id"]

        if isinstance(user, discord.Member):
            username = user.name
            display_name = user.global_name or user.name
            db_username = normalize_text(username)
            db_display_name = normalize_text(display_name)
            member_since = user.joined_at.strftime("%Y-%m-%d %H:%M:%S") if user.joined_at else None
            last_join_date = member_since if user.joined_at else None
            unverified_role_id = getGuildMemberNotVerifiedRoleId(guildId)
            has_unverified_role = bool(
                unverified_role_id
                and discord.utils.get(user.guild.roles, id=unverified_role_id) in user.roles
            )
            approved = 0 if has_unverified_role else 1
            cursor.execute(
                "SELECT user_id, username, display_name FROM user_discord WHERE discord_user_id = %s",
                (user.id,),
            )
            result = cursor.fetchone()
        elif isinstance(user, discord.User):
            username = user.name
            display_name = user.global_name or user.name
            db_username = normalize_text(username)
            db_display_name = normalize_text(display_name)
            member_since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            last_join_date = None
            approved = 0
            cursor.execute(
                "SELECT user_id, username, display_name FROM user_discord WHERE discord_user_id = %s",
                (user.id,),
            )
            result = cursor.fetchone()
        else:
            username = user
            display_name = user
            db_username = normalize_text(username)
            db_display_name = normalize_text(display_name)
            member_since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            last_join_date = None
            approved = 0
            cursor.execute(
                "SELECT user_id, username, display_name FROM user_telegram WHERE username = %s",
                (user,),
            )
            result = cursor.fetchone()

        approved_at_str = approvedAt.strftime("%Y-%m-%d %H:%M:%S") if approvedAt else None

        if result:
            user_id = result["user_id"]

            if result.get("username") != db_username or result["display_name"] != db_display_name:
                if isinstance(user, (discord.Member, discord.User)):
                    cursor.execute(
                        "UPDATE user_discord SET username = %s, display_name = %s WHERE user_id = %s",
                        (db_username, db_display_name, user_id),
                    )
                else:
                    cursor.execute(
                        "UPDATE user_telegram SET username = %s, display_name = %s WHERE user_id = %s",
                        (db_username, db_display_name, user_id),
                    )

            _ensure_user_community_status(
                cursor,
                user_id,
                community_id,
                member_since,
                last_join_date,
                approved,
                approved_at_str,
                isinstance(user, discord.Member),
            )
            return user_id

        try:
            cursor.execute(
                "INSERT INTO users (display_name, username) VALUES (NULL, NULL)"
            )
            user_id = cursor.lastrowid
        except mysql.connector.Error as err:
            if err.errno == errorcode.ER_DUP_ENTRY:
                cursor.execute("SELECT id FROM users WHERE username = %s", (db_username,))
                row = cursor.fetchone()
                user_id = row["id"] if row else None
            else:
                raise Exception(f"Erro ao registrar usuário no banco: {err.msg}") from err

        if not user_id:
            raise Exception("Não foi possível determinar o ID do usuário após o registro.")

        _ensure_user_community_status(
            cursor,
            user_id,
            community_id,
            member_since,
            last_join_date,
            approved,
            approved_at_str,
            isinstance(user, discord.Member),
        )

        try:
            if isinstance(user, (discord.Member, discord.User)):
                db_discord_display_name = db_display_name
                cursor.execute(
                    """
                    INSERT INTO user_discord (user_id, discord_user_id, username, display_name)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        username = VALUES(username),
                        display_name = VALUES(display_name)
                    """,
                    (user_id, user.id, db_username, db_discord_display_name),
                )
                cursor.execute(
                    "SELECT user_id FROM user_discord WHERE discord_user_id = %s",
                    (user.id,),
                )
                persisted_link = cursor.fetchone()
                if not persisted_link or persisted_link["user_id"] != user_id:
                    raise RuntimeError(
                        "Conta Discord foi vinculada concorrentemente a outro User; nenhuma identidade foi transferida"
                    )
            else:
                cursor.execute(
                    "INSERT IGNORE INTO user_telegram (user_id, username, display_name) VALUES (%s, %s, %s)",
                    (user_id, db_username, db_display_name),
                )
        except mysql.connector.Error as err:
            raise Exception(f"Erro ao registrar dados do usuário no banco: {err.msg}") from err

        return user_id


async def async_includeUser(
    user: Union[discord.Member, discord.User, str],
    guildId: int | None = None,
    approvedAt: datetime = None,
) -> int:
    """Async wrapper to include users without blocking the event loop."""
    if guildId is None:
        raise ValueError("guildId é obrigatório para incluir usuários com isolamento por servidor")

    return await asyncio.to_thread(includeUser, user, guildId, approvedAt)


def get_user_economy_balance(guild_id: int, user: Union[discord.Member, discord.User]) -> int:
    """Return the stored balance for ``user`` inside ``guild_id``."""

    user_id = includeUser(user, guild_id)
    with pooled_connection() as cursor:
        entry = _ensure_economy_entry(cursor, user_id, guild_id, lock=False)
        return int(entry["bank_balance"] or 0)


def set_user_economy_balance(
    guild_id: int,
    user: Union[discord.Member, discord.User],
    amount: int,
) -> int:
    """Replace the ``user`` balance in ``guild_id`` with ``amount``."""

    if amount < 0:
        raise ValueError("O saldo não pode ser negativo.")

    user_id = includeUser(user, guild_id)
    with pooled_connection() as cursor:
        entry = _ensure_economy_entry(cursor, user_id, guild_id, lock=True)
        cursor.execute(
            "UPDATE user_economy SET bank_balance = %s WHERE id = %s",
            (amount, entry["id"]),
        )
        return amount


def adjust_user_economy_balance(
    guild_id: int,
    user: Union[discord.Member, discord.User],
    delta: int,
) -> int:
    """Apply ``delta`` to the current balance, returning the new value."""

    user_id = includeUser(user, guild_id)
    with pooled_connection() as cursor:
        entry = _ensure_economy_entry(cursor, user_id, guild_id, lock=True)
        new_balance = int(entry["bank_balance"] or 0) + delta
        if new_balance < 0:
            raise ValueError("O saldo não pode ser negativo.")
        cursor.execute(
            "UPDATE user_economy SET bank_balance = %s WHERE id = %s",
            (new_balance, entry["id"]),
        )
        return new_balance


def transfer_user_economy_balance(
    guild_id: int,
    origin: Union[discord.Member, discord.User],
    destination: Union[discord.Member, discord.User],
    amount: int,
) -> tuple[int, int]:
    """Transfer ``amount`` coins from ``origin`` to ``destination``."""

    if amount <= 0:
        raise ValueError("Informe um valor maior que zero.")

    origin_id = includeUser(origin, guild_id)
    destination_id = includeUser(destination, guild_id)

    if origin_id == destination_id:
        raise ValueError("Não é possível transferir para o mesmo usuário.")

    with pooled_connection() as cursor:
        entries = {}
        for user_id in sorted([origin_id, destination_id]):
            entries[user_id] = _ensure_economy_entry(cursor, user_id, guild_id, lock=True)

        origin_entry = entries[origin_id]
        destination_entry = entries[destination_id]

        origin_balance = int(origin_entry["bank_balance"] or 0)
        if origin_balance < amount:
            raise ValueError("Saldo insuficiente para realizar a transferência.")

        new_origin_balance = origin_balance - amount
        new_destination_balance = int(destination_entry["bank_balance"] or 0) + amount

        cursor.execute(
            "UPDATE user_economy SET bank_balance = %s WHERE id = %s",
            (new_origin_balance, origin_entry["id"]),
        )
        cursor.execute(
            "UPDATE user_economy SET bank_balance = %s WHERE id = %s",
            (new_destination_balance, destination_entry["id"]),
        )

        return new_origin_balance, new_destination_balance


def create_or_reset_boss_event(guild_id: int, max_hp: int, cooldown_seconds: int = 30, boss_name: str = "Boss") -> None:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            INSERT INTO community_boss_event (guild_id, boss_name, max_hp, current_hp, active, attack_cooldown_seconds, finished_at)
            VALUES (%s, %s, %s, %s, 1, %s, NULL)
            ON DUPLICATE KEY UPDATE boss_name = VALUES(boss_name), max_hp = VALUES(max_hp), current_hp = VALUES(current_hp), active = 1,
                attack_cooldown_seconds = VALUES(attack_cooldown_seconds), finished_at = NULL
            """,
            (guild_id, str(boss_name)[:120], int(max_hp), int(max_hp), int(cooldown_seconds)),
        )
        cursor.execute("DELETE FROM community_boss_contribution WHERE guild_id = %s", (guild_id,))
        cursor.execute("DELETE FROM community_boss_attack_cooldown WHERE guild_id = %s", (guild_id,))


def get_boss_event_state(guild_id: int, discord_user_id: int | None = None) -> dict | None:
    with pooled_connection() as cursor:
        cursor.execute("SELECT * FROM community_boss_event WHERE guild_id = %s", (guild_id,))
        event = cursor.fetchone()
        if not event:
            return None
        state = {
            "active": bool(event.get("active")),
            "boss_name": str(event.get("boss_name") or "Boss"),
            "boss_hp": int(event.get("current_hp") or 0),
            "boss_max_hp": int(event.get("max_hp") or 0),
            "attack_cooldown_seconds": int(event.get("attack_cooldown_seconds") or 30),
            "user_cooldown_until": 0,
        }
        if discord_user_id is not None:
            db_uid = getUserId(discord_user_id)
            if db_uid is not None:
                cursor.execute("SELECT cooldown_until FROM community_boss_attack_cooldown WHERE guild_id = %s AND user_id = %s", (guild_id, db_uid))
                row = cursor.fetchone()
                if row:
                    state["user_cooldown_until"] = int(row.get("cooldown_until") or 0)
        return state


def record_boss_attack(guild_id: int, user_id: int, damage: int, now_ts: int) -> dict:
    with pooled_connection() as cursor:
        cursor.execute("SELECT * FROM community_boss_event WHERE guild_id = %s FOR UPDATE", (guild_id,))
        event = cursor.fetchone()
        if not event or not bool(event.get("active")):
            return {"status": "inactive", "boss_hp": 0, "boss_max_hp": 0, "defeated": False}

        cooldown_seconds = int(event.get("attack_cooldown_seconds") or 30)
        cursor.execute(
            "SELECT cooldown_until FROM community_boss_attack_cooldown WHERE guild_id = %s AND user_id = %s FOR UPDATE",
            (guild_id, user_id),
        )
        cooldown_row = cursor.fetchone()
        current_cooldown_until = int(cooldown_row.get("cooldown_until") or 0) if cooldown_row else 0
        if current_cooldown_until > int(now_ts):
            return {
                "status": "cooldown",
                "boss_hp": int(event.get("current_hp") or 0),
                "boss_max_hp": int(event.get("max_hp") or 0),
                "defeated": False,
                "cooldown_until": current_cooldown_until,
            }

        new_hp = max(0, int(event["current_hp"]) - int(damage))
        defeated = new_hp <= 0
        cooldown_until = int(now_ts) + cooldown_seconds
        cursor.execute("UPDATE community_boss_event SET current_hp = %s, active = %s, finished_at = IF(%s = 1, CURRENT_TIMESTAMP, NULL) WHERE guild_id = %s", (new_hp, 0 if defeated else 1, 1 if defeated else 0, guild_id))
        cursor.execute("""INSERT INTO community_boss_contribution (guild_id, user_id, total_damage) VALUES (%s, %s, %s)
                      ON DUPLICATE KEY UPDATE total_damage = total_damage + VALUES(total_damage)""", (guild_id, user_id, int(damage)))
        cursor.execute("""INSERT INTO community_boss_attack_cooldown (guild_id, user_id, cooldown_until) VALUES (%s, %s, %s)
                      ON DUPLICATE KEY UPDATE cooldown_until = VALUES(cooldown_until)""", (guild_id, user_id, cooldown_until))
        return {"status": "applied", "boss_hp": new_hp, "boss_max_hp": int(event["max_hp"]), "defeated": defeated, "cooldown_until": cooldown_until}


def get_boss_event_top_contributors(guild_id: int, limit: int = 10) -> list[dict]:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT c.user_id, d.discord_user_id, c.total_damage
            FROM community_boss_contribution c
            LEFT JOIN user_discord d ON d.user_id = c.user_id
            WHERE c.guild_id = %s
            ORDER BY c.total_damage DESC
            LIMIT %s
            """,
            (guild_id, int(limit)),
        )
        return cursor.fetchall() or []


def finish_boss_event(guild_id: int) -> None:
    with pooled_connection() as cursor:
        cursor.execute("UPDATE community_boss_event SET active = 0, finished_at = CURRENT_TIMESTAMP WHERE guild_id = %s", (guild_id,))


def create_expedition(guild_id: int, started_at: int, ends_at: int) -> None:
    participants = {"members": []}
    with pooled_connection() as cursor:
        cursor.execute(
            """
            INSERT INTO community_expedition (guild_id, active, started_at, ends_at, participants_json, finalized_at)
            VALUES (%s, 1, %s, %s, %s, NULL)
            ON DUPLICATE KEY UPDATE
                active = 1,
                started_at = VALUES(started_at),
                ends_at = VALUES(ends_at),
                participants_json = VALUES(participants_json),
                finalized_at = NULL
            """,
            (guild_id, int(started_at), int(ends_at), json.dumps(participants)),
        )


def get_expedition_state(guild_id: int) -> dict | None:
    with pooled_connection() as cursor:
        cursor.execute("SELECT * FROM community_expedition WHERE guild_id = %s", (guild_id,))
        row = cursor.fetchone()
        if not row:
            return None
        participants = {"members": []}
        try:
            participants = json.loads(row.get("participants_json") or "{\"members\": []}")
        except json.JSONDecodeError:
            participants = {"members": []}
        return {
            "active": bool(row.get("active")),
            "started_at": int(row.get("started_at") or 0),
            "ends_at": int(row.get("ends_at") or 0),
            "participants": participants.get("members", []),
            "finalized_at": int(row.get("finalized_at") or 0) if row.get("finalized_at") else None,
        }


def update_expedition_participants(guild_id: int, participant_ids: list[int]) -> None:
    with pooled_connection() as cursor:
        cursor.execute(
            "UPDATE community_expedition SET participants_json = %s WHERE guild_id = %s",
            (json.dumps({"members": [int(item) for item in participant_ids]}), guild_id),
        )


def update_expedition_participants_atomic(guild_id: int, user_id: int, joining: bool, now_ts: int | None = None) -> dict:
    now = int(now_ts or time.time())
    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT active, ends_at, participants_json FROM community_expedition WHERE guild_id = %s FOR UPDATE",
            (guild_id,),
        )
        row = cursor.fetchone()
        if not row or not bool(row.get("active")):
            return {"ok": False, "reason": "inactive"}

        ends_at = int(row.get("ends_at") or 0)
        if now >= ends_at:
            return {"ok": False, "reason": "closed"}

        try:
            participants = json.loads(row.get("participants_json") or '{"members": []}')
        except json.JSONDecodeError:
            participants = {"members": []}

        members = {int(item) for item in participants.get("members", [])}
        if joining:
            members.add(int(user_id))
        elif int(user_id) in members:
            members.remove(int(user_id))
        else:
            return {"ok": False, "reason": "missing"}

        member_list = sorted(members)
        cursor.execute(
            "UPDATE community_expedition SET participants_json = %s WHERE guild_id = %s",
            (json.dumps({"members": member_list}), guild_id),
        )
        return {"ok": True, "participants": member_list}


def get_active_expeditions() -> list[dict]:
    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT guild_id, started_at, ends_at FROM community_expedition WHERE active = 1"
        )
        return cursor.fetchall() or []


def finalize_expedition(guild_id: int, finalized_at: int) -> None:
    with pooled_connection() as cursor:
        cursor.execute(
            "UPDATE community_expedition SET active = 0, finalized_at = %s WHERE guild_id = %s",
            (int(finalized_at), guild_id),
        )


def _ensure_economy_config_columns(cursor: MySQLCursorAbstract) -> bool:
    """Ensure bump economy columns exist in ``config_economy``."""

    required_columns = {
        "bump_points": "INT NULL DEFAULT NULL",
        "bump_reward_enabled": "TINYINT(1) NOT NULL DEFAULT 0",
        "birthday_points": "INT NOT NULL DEFAULT 0",
    }
    created_columns: set[str] = set()

    for column_name, column_definition in required_columns.items():
        cursor.execute(
            """
            SELECT 1
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'config_economy'
              AND COLUMN_NAME = %s
            LIMIT 1
            """,
            (column_name,),
        )
        if cursor.fetchone():
            continue

        try:
            cursor.execute(
                f"ALTER TABLE config_economy ADD COLUMN {column_name} {column_definition}"
            )
            created_columns.add(column_name)
        except mysql.connector.Error as err:
            if err.errno == errorcode.ER_DUP_FIELDNAME:
                continue
            logging.error(
                "Falha ao criar coluna %s em config_economy: %s",
                column_name,
                err,
            )
            return False

    if "bump_reward_enabled" in created_columns:
        try:
            cursor.execute(
                """
                UPDATE config_economy
                SET bump_reward_enabled = 1
                WHERE bump_reward_enabled = 0
                  AND bump_points > 0
                """
            )
        except mysql.connector.Error as err:
            logging.error(
                "Falha ao migrar bump_reward_enabled em config_economy: %s",
                err,
            )
            return False

    return True


def get_economy_config(guild_id: int) -> dict[str, int]:
    """Return economy configuration for the given guild.

    Falls back to sensible defaults when the configuration is missing.
    """

    default_config = {
        "bet_odds_3x": 5,
        "bet_odds_2x": 15,
        "bet_odds_1x": 30,
        "bump_points": 0,
        "bump_reward_enabled": 0,
        "birthday_points": 0,
    }

    with pooled_connection() as cursor:
        if not _ensure_economy_config_columns(cursor):
            return default_config

        cursor.execute(
            """
            SELECT bet_odds_3x, bet_odds_2x, bet_odds_1x, bump_points, bump_reward_enabled, birthday_points
            FROM config_economy
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )
        row = cursor.fetchone()

    if not row:
        return default_config

    result: dict[str, int] = {}
    for key, fallback in default_config.items():
        try:
            value = int(row.get(key)) if row.get(key) is not None else fallback
        except (TypeError, ValueError):
            value = fallback
        result[key] = max(0, value)

    return result


def get_bump_reward_economy_config(guild_id: int) -> dict[str, int | bool | None]:
    """Return canonical Bump economy state while preserving NULL semantics."""

    with pooled_connection() as cursor:
        if not _ensure_economy_config_columns(cursor):
            return {"exists": False, "initialized": False, "enabled": False, "points": None}
        cursor.execute(
            """
            SELECT bump_reward_enabled, bump_points
            FROM config_economy
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )
        row = cursor.fetchone()

    if not row:
        return {"exists": False, "initialized": False, "enabled": False, "points": None}

    enabled = bool(row.get("bump_reward_enabled"))
    raw_points = row.get("bump_points")
    points = max(0, int(raw_points)) if raw_points is not None else None
    return {
        "exists": True,
        "initialized": enabled or raw_points is not None,
        "enabled": enabled,
        "points": points,
    }


def set_bump_reward_economy_config(
    guild_id: int,
    *,
    enabled: bool | None = None,
    points: int | None = None,
) -> bool:
    updates: dict[str, Any] = {}
    if enabled is not None:
        updates["bump_reward_enabled"] = 1 if enabled else 0
    if points is not None:
        updates["bump_points"] = max(0, int(points))

    if not updates:
        return False

    with pooled_connection() as cursor:
        if not _ensure_economy_config_columns(cursor):
            return False

        cursor.execute(
            "SELECT 1 FROM config_economy WHERE server_guild_id = %s LIMIT 1",
            (guild_id,),
        )
        if not cursor.fetchone():
            return False

        set_clause = ", ".join(f"{column} = %s" for column in updates)
        values = list(updates.values()) + [guild_id]
        cursor.execute(
            f"UPDATE config_economy SET {set_clause} WHERE server_guild_id = %s",
            values,
        )

    return True


def get_bump_reward_points(guild_id: int) -> int:
    """Return the amount of coins configured for bump rewards."""

    return int(get_bump_reward_economy_config(guild_id).get("points") or 0)


def get_birthday_reward_points(guild_id: int) -> int:
    """Return the amount of coins configured for birthday rewards.

    Falls back to ``0`` if the configuration or column is missing.
    """

    default_points = 0

    try:
        with pooled_connection() as cursor:
            cursor.execute(
                "SELECT birthday_points FROM config_economy WHERE server_guild_id = %s",
                (guild_id,),
            )
            row = cursor.fetchone()
    except mysql.connector.Error:
        return default_points

    if not row:
        return default_points

    try:
        points = int(row.get("birthday_points")) if row.get("birthday_points") is not None else default_points
    except (TypeError, ValueError):
        return default_points

    return max(default_points, points)


def award_bump_reward(guild_id: int, user: Union[discord.Member, discord.User]) -> int:
    """Grant bump reward coins to ``user`` returning the new balance.

    When the configuration is missing or zero, the balance is left untouched
    and ``0`` is returned.
    """

    bump_points = get_bump_reward_points(guild_id)
    if bump_points <= 0:
        return 0

    return adjust_user_economy_balance(guild_id, user, bump_points)


def fetch_community_store_items(guild_id: int) -> list[dict]:
    """Retorna todos os itens cadastrados na loja da comunidade."""

    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        cursor.execute(
            """
            SELECT
                cs.id,
                cs.owner_user_id,
                cs.item_name,
                cs.item_price,
                cs.quantity_available,
                cs.quantity_total,
                cs.allow_multiple,
                cs.is_service,
                cs.duration,
                u.display_name AS owner_display_name
            FROM community_store AS cs
            LEFT JOIN users AS u ON u.id = cs.owner_user_id
            WHERE cs.community_id = %s
            ORDER BY cs.id ASC
            """,
            (community_id,),
        )
        rows = cursor.fetchall() or []

    for row in rows:
        row["allow_multiple"] = bool(row["allow_multiple"])
        row["is_service"] = bool(row["is_service"])
        duration_value = row.get("duration")
        row["duration"] = int(duration_value) if duration_value is not None else None
        if row["is_service"]:
            row["quantity_total"] = None
            row["quantity_available"] = None
    return rows


def get_community_store_item(
    guild_id: int, item_id: int, *, lock: bool = False
) -> dict:
    """Recupera um item específico da loja da comunidade."""

    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        query = (
            "SELECT * FROM community_store WHERE community_id = %s AND id = %s"
        )
        if lock:
            query += " FOR UPDATE"
        cursor.execute(query, (community_id, item_id))
        item = cursor.fetchone()

    if not item:
        raise ValueError("Item não encontrado na loja da comunidade.")

    item["allow_multiple"] = bool(item["allow_multiple"])
    item["is_service"] = bool(item["is_service"])
    duration_value = item.get("duration")
    item["duration"] = int(duration_value) if duration_value is not None else None
    if item["is_service"]:
        item["quantity_total"] = None
        item["quantity_available"] = None
    return item


def create_community_store_item(
    guild_id: int,
    *,
    item_name: str,
    item_price: int,
    quantity_total: Optional[int] = None,
    quantity_available: Optional[int] = None,
    allow_multiple: bool = False,
    is_service: bool = False,
    duration: Optional[int] = None,
    owner: Union[discord.Member, discord.User, None] = None,
) -> dict:
    """Cria um item disponível na loja da comunidade."""

    if item_price <= 0:
        raise ValueError("O preço do item deve ser maior que zero.")

    if not item_name.strip():
        raise ValueError("O nome do item não pode ser vazio.")

    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        owner_user_id: Optional[int] = None

        if owner:
            owner_user_id = includeUser(owner, guild_id)

        if is_service:
            quantity_total = 0
            quantity_available = 0
            owner_user_id = None
            if duration is None or int(duration) <= 0:
                raise ValueError(
                    "Serviços precisam ter a duração configurada em segundos."
                )
            duration_value = int(duration)
        else:
            if quantity_total is not None and quantity_total <= 0:
                raise ValueError(
                    "Itens com quantidade limitada precisam de um total maior que zero."
                )

            if quantity_available is not None:
                if quantity_available < 0:
                    raise ValueError(
                        "A quantidade disponível não pode ser negativa."
                    )
                if quantity_total is not None and quantity_available > quantity_total:
                    raise ValueError(
                        "A quantidade disponível não pode exceder a quantidade total."
                    )
            elif quantity_total is not None:
                quantity_available = quantity_total

            duration_value = None

        cursor.execute(
            """
            INSERT INTO community_store (
                community_id,
                owner_user_id,
                item_name,
                item_price,
                quantity_available,
                quantity_total,
                allow_multiple,
                is_service,
                duration
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                community_id,
                owner_user_id,
                item_name,
                item_price,
                quantity_available,
                quantity_total,
                allow_multiple,
                is_service,
                duration_value,
            ),
        )
        cursor.execute("SELECT LAST_INSERT_ID() AS id")
        new_id = cursor.fetchone()["id"]

    return get_community_store_item(guild_id, int(new_id))


def update_community_store_item(
    guild_id: int,
    item_id: int,
    *,
    item_name: Optional[str] = None,
    item_price: Optional[int] = None,
    quantity_total: Optional[int] = None,
    allow_multiple: Optional[bool] = None,
    is_service: Optional[bool] = None,
    duration: Optional[int] = None,
    owner: Union[discord.Member, discord.User, None, Literal[False]] = None,
) -> dict:
    """Atualiza as informações de um item da loja da comunidade."""

    updates: list[str] = []
    params: list = []

    if item_name is not None:
        if not item_name.strip():
            raise ValueError("O nome do item não pode ser vazio.")
        updates.append("item_name = %s")
        params.append(item_name)

    if item_price is not None:
        if item_price <= 0:
            raise ValueError("O preço do item deve ser maior que zero.")
        updates.append("item_price = %s")
        params.append(item_price)

    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        cursor.execute(
            "SELECT * FROM community_store WHERE community_id = %s AND id = %s FOR UPDATE",
            (community_id, item_id),
        )
        item = cursor.fetchone()
        if not item:
            raise ValueError("Item não encontrado na loja da comunidade.")

        current_is_service = bool(item["is_service"])
        current_duration = item.get("duration")

        if allow_multiple is not None:
            updates.append("allow_multiple = %s")
            params.append(allow_multiple)

        new_is_service = current_is_service if is_service is None else is_service

        if is_service is not None:
            updates.append("is_service = %s")
            params.append(is_service)

        owner_user_id = item["owner_user_id"]
        if owner is not None and owner is not False:
            owner_user_id = includeUser(owner, guild_id)
            updates.append("owner_user_id = %s")
            params.append(owner_user_id)
        elif owner is False:
            owner_user_id = None
            updates.append("owner_user_id = %s")
            params.append(None)

        if new_is_service:
            updates.extend(
                ["quantity_total = 0", "quantity_available = 0", "owner_user_id = NULL"]
            )
            duration_candidate = duration if duration is not None else current_duration
            try:
                duration_int = int(duration_candidate) if duration_candidate is not None else None
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "Informe um valor numérico válido para a duração do serviço."
                ) from error
            if duration_int is None or duration_int <= 0:
                raise ValueError(
                    "Serviços precisam ter a duração configurada em segundos."
                )
            updates.append("duration = %s")
            params.append(duration_int)
        elif quantity_total is not None:
            if quantity_total <= 0:
                raise ValueError(
                    "Itens com quantidade limitada precisam de um total maior que zero."
                )
            current_total = int(item["quantity_total"] or 0)
            current_available = int(item["quantity_available"] or 0)
            diff = quantity_total - current_total
            new_available = max(0, current_available + diff)
            updates.append("quantity_total = %s")
            params.append(quantity_total)
            updates.append("quantity_available = %s")
            params.append(new_available)

        if not new_is_service and duration is not None:
            try:
                duration_value = int(duration)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "Informe um valor numérico válido para a duração do serviço."
                ) from error
            if duration_value <= 0:
                updates.append("duration = NULL")
            else:
                updates.append("duration = %s")
                params.append(duration_value)

        if not updates:
            return get_community_store_item(guild_id, item_id)

        params.extend([community_id, item_id])
        cursor.execute(
            f"UPDATE community_store SET {', '.join(updates)} WHERE community_id = %s AND id = %s",
            params,
        )

    return get_community_store_item(guild_id, item_id)


def _fetch_inventory_entry_by_id(
    cursor: MySQLCursorAbstract, entry_id: int
) -> Optional[dict]:
    cursor.execute(
        """
        SELECT id, store_item_id, quantity, used_in, valid_until, service_metadata
        FROM user_inventory
        WHERE id = %s
        """,
        (entry_id,),
    )
    return cursor.fetchone()


def _get_inventory_entry(
    cursor: MySQLCursorAbstract,
    community_id: int,
    owner_user_id: int,
    store_item_id: int,
    *,
    lock: bool = False,
) -> Optional[dict]:
    _ensure_user_inventory_schema(cursor)
    query = (
        "SELECT id, store_item_id, quantity, used_in, valid_until, service_metadata FROM user_inventory WHERE community_id = %s "
        "AND owner_user_id = %s AND store_item_id = %s"
    )
    if lock:
        query += " FOR UPDATE"
    cursor.execute(query, (community_id, owner_user_id, store_item_id))
    return cursor.fetchone()


def _get_service_inventory_entries(
    cursor: MySQLCursorAbstract,
    community_id: int,
    owner_user_id: int,
    store_item_id: int,
    *,
    lock: bool = False,
) -> list[dict]:
    _ensure_user_inventory_schema(cursor)
    query = (
        "SELECT id, store_item_id, quantity, used_in, valid_until, service_metadata FROM user_inventory "
        "WHERE community_id = %s AND owner_user_id = %s AND store_item_id = %s"
    )
    if lock:
        query += " FOR UPDATE"
    cursor.execute(query, (community_id, owner_user_id, store_item_id))
    entries = cursor.fetchall() or []
    for entry in entries:
        _normalize_inventory_entry(entry)
    return entries


def _normalize_inventory_entry(entry: Optional[dict]) -> Optional[dict]:
    if entry is None:
        return None

    if "quantity" in entry:
        try:
            entry["quantity"] = int(entry.get("quantity") or 0)
        except (TypeError, ValueError):
            entry["quantity"] = 0

    metadata = entry.get("service_metadata")
    if metadata:
        if isinstance(metadata, bytes):
            metadata = metadata.decode()
        if isinstance(metadata, str):
            try:
                entry["service_metadata"] = json.loads(metadata)
            except json.JSONDecodeError:
                entry["service_metadata"] = None
        elif isinstance(metadata, dict):
            entry["service_metadata"] = metadata
    else:
        entry["service_metadata"] = None

    return entry


def _set_inventory_quantity(
    cursor: MySQLCursorAbstract,
    community_id: int,
    owner_user_id: int,
    store_item_id: int,
    quantity: int,
) -> None:
    if quantity < 0:
        raise ValueError("A quantidade não pode ser negativa.")

    _ensure_user_inventory_schema(cursor)

    existing = _get_inventory_entry(
        cursor, community_id, owner_user_id, store_item_id, lock=True
    )

    if existing:
        if quantity == 0:
            cursor.execute(
                "DELETE FROM user_inventory WHERE id = %s",
                (existing["id"],),
            )
        else:
            cursor.execute(
                "UPDATE user_inventory SET quantity = %s WHERE id = %s",
                (quantity, existing["id"]),
            )
    elif quantity > 0:
        cursor.execute(
            """
            INSERT INTO user_inventory (community_id, owner_user_id, store_item_id, quantity)
            VALUES (%s, %s, %s, %s)
            """,
            (community_id, owner_user_id, store_item_id, quantity),
        )


def get_user_inventory_items(
    guild_id: int, user: Union[discord.Member, discord.User]
) -> list[dict]:
    """Retorna o inventário de um usuário."""

    owner_user_id = includeUser(user, guild_id)
    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        _ensure_user_inventory_schema(cursor)
        cursor.execute(
            """
            SELECT
                ui.id,
                ui.store_item_id,
                ui.quantity,
                ui.used_in,
                ui.valid_until,
                ui.service_metadata,
                cs.owner_user_id,
                cs.item_name,
                cs.is_service,
                cs.allow_multiple,
                cs.duration
            FROM user_inventory AS ui
            LEFT JOIN community_store AS cs ON cs.id = ui.store_item_id
            WHERE ui.community_id = %s AND ui.owner_user_id = %s
            ORDER BY COALESCE(cs.item_name, ui.store_item_id) ASC
            """,
            (community_id, owner_user_id),
        )
        rows = cursor.fetchall() or []

    aggregated: dict[int, dict[str, Any]] = {}

    for row in rows:
        _normalize_inventory_entry(row)
        store_item_id = int(row.get("store_item_id") or 0)

        owner_user_id: Optional[int]
        try:
            owner_user_id = (
                int(row["owner_user_id"])
                if row.get("owner_user_id") is not None
                else None
            )
        except (TypeError, ValueError):
            owner_user_id = None
        row["owner_user_id"] = owner_user_id

        row["is_service"] = bool(row.get("is_service", False)) or bool(
            has_service_command(store_item_id)
        )
        row["allow_multiple"] = bool(row.get("allow_multiple", False))

        metadata = row.get("service_metadata") or {}
        duration_value = row.get("duration")
        if duration_value is None and isinstance(metadata, dict):
            duration_value = metadata.get("duration")
        normalized_duration = (
            int(duration_value) if duration_value is not None else None
        )
        row["duration"] = normalized_duration
        if not row.get("item_name") and isinstance(metadata, dict):
            row["item_name"] = metadata.get("item_name")

        aggregated_entry = aggregated.setdefault(
            store_item_id,
            {
                "store_item_id": store_item_id,
                "item_name": row.get("item_name"),
                "is_service": row.get("is_service"),
                "allow_multiple": row.get("allow_multiple"),
                "owner_user_id": row.get("owner_user_id"),
                "duration": normalized_duration,
                "service_metadata": row.get("service_metadata"),
                "available_quantity": 0,
                "active_quantity": 0,
                "total_quantity": 0,
                "quantity": 0,
                "active_usages": [],
            },
        )

        if not aggregated_entry.get("item_name") and row.get("item_name"):
            aggregated_entry["item_name"] = row.get("item_name")

        aggregated_entry["is_service"] = bool(aggregated_entry.get("is_service")) or bool(
            row.get("is_service")
        )
        aggregated_entry["allow_multiple"] = (
            bool(aggregated_entry.get("allow_multiple"))
            or bool(row.get("allow_multiple"))
        )
        if aggregated_entry.get("owner_user_id") is None and row.get("owner_user_id") is not None:
            aggregated_entry["owner_user_id"] = row.get("owner_user_id")

        if aggregated_entry.get("duration") is None and normalized_duration is not None:
            aggregated_entry["duration"] = normalized_duration

        if aggregated_entry.get("service_metadata") is None and row.get("service_metadata"):
            aggregated_entry["service_metadata"] = row.get("service_metadata")

        try:
            quantity = int(row.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0

        aggregated_entry["total_quantity"] += quantity
        aggregated_entry["quantity"] = aggregated_entry["total_quantity"]

        used_in = row.get("used_in")
        if used_in:
            aggregated_entry["active_quantity"] += quantity
            aggregated_entry["active_usages"].append(
                {
                    "id": row.get("id"),
                    "quantity": quantity,
                    "used_in": used_in,
                    "valid_until": row.get("valid_until"),
                    "service_metadata": row.get("service_metadata"),
                }
            )
        else:
            aggregated_entry["available_quantity"] += quantity

    inventory = list(aggregated.values())

    for entry in inventory:
        entry["available_quantity"] = int(entry.get("available_quantity") or 0)
        entry["active_quantity"] = int(entry.get("active_quantity") or 0)
        entry["total_quantity"] = int(entry.get("total_quantity") or 0)
        entry["quantity"] = entry["total_quantity"]

        def _usage_sort_key(usage: dict) -> tuple[float, int]:
            target = usage.get("valid_until") or usage.get("used_in")
            timestamp: float
            if isinstance(target, datetime):
                try:
                    timestamp = float(target.timestamp())
                except (OSError, OverflowError, ValueError):
                    timestamp = float("inf")
            else:
                timestamp = float("inf")
            return (timestamp, int(usage.get("id") or 0))

        entry["active_usages"].sort(key=_usage_sort_key)

    def _inventory_sort_key(entry: dict[str, Any]) -> tuple[int, int]:
        owner_user_id = entry.get("owner_user_id")
        is_service = bool(entry.get("is_service"))
        if is_service:
            group = 0
        elif owner_user_id is None:
            group = 1
        else:
            group = 2
        return (group, int(entry.get("store_item_id") or 0))

    inventory.sort(key=_inventory_sort_key)

    return inventory


def purchase_community_store_item(
    guild_id: int,
    buyer: Union[discord.Member, discord.User],
    item_id: int,
    quantity: int = 1,
) -> dict:
    """Realiza a compra de um item na loja da comunidade."""

    if quantity <= 0:
        raise ValueError("A quantidade precisa ser maior que zero.")

    buyer_user_id = includeUser(buyer, guild_id)

    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        _ensure_user_inventory_schema(cursor)

        cursor.execute(
            "SELECT * FROM community_store WHERE community_id = %s AND id = %s FOR UPDATE",
            (community_id, item_id),
        )
        item = cursor.fetchone()
        if not item:
            raise ValueError("Item não encontrado na loja da comunidade.")

        is_service = bool(item["is_service"])
        allow_multiple = bool(item["allow_multiple"])
        owner_user_id = item["owner_user_id"]
        duration_value = item.get("duration")
        try:
            service_duration = (
                int(duration_value) if duration_value is not None else None
            )
        except (TypeError, ValueError):
            service_duration = None
        item["duration"] = service_duration

        should_update_stock = False
        new_available: Optional[int] = None
        inventory_entry: Optional[dict] = None
        existing_inventory = None

        if is_service:
            quantity = 1
            if not has_service_command(int(item["id"])):
                raise ValueError(
                    "Não há um serviço implementado para este item. A compra foi cancelada."
                )
            service_entries = _get_service_inventory_entries(
                cursor,
                community_id,
                buyer_user_id,
                item_id,
                lock=True,
            )
        else:
            quantity_total = item.get("quantity_total")
            available_value = item.get("quantity_available")

            if quantity_total is not None:
                available = int(available_value or 0)
                if available < quantity:
                    raise ValueError(
                        "Não há quantidade suficiente disponível para este item."
                    )
                new_available = available - quantity
                should_update_stock = True
            elif available_value is not None:
                available = int(available_value)
                if available < quantity:
                    raise ValueError(
                        "Não há quantidade suficiente disponível para este item."
                    )
                new_available = available - quantity
                should_update_stock = True
            existing_inventory = None

        cursor.execute(
            "SELECT id, bank_balance FROM user_economy WHERE user_id = %s AND server_guild_id = %s FOR UPDATE",
            (buyer_user_id, guild_id),
        )
        entry = cursor.fetchone()
        if not entry:
            entry = _ensure_economy_entry(cursor, buyer_user_id, guild_id, lock=True)
        if int(entry["bank_balance"] or 0) < item["item_price"] * quantity:
            raise ValueError("Saldo insuficiente para completar a compra.")

        new_balance = int(entry["bank_balance"] or 0) - item["item_price"] * quantity
        cursor.execute(
            "UPDATE user_economy SET bank_balance = %s WHERE id = %s",
            (new_balance, entry["id"]),
        )

        if owner_user_id:
            _adjust_user_balance_by_user_id(
                cursor,
                guild_id,
                owner_user_id,
                item["item_price"] * quantity,
            )

        if not is_service:
            if should_update_stock:
                cursor.execute(
                    "UPDATE community_store SET quantity_available = %s WHERE id = %s",
                    (new_available, item_id),
                )
                item["quantity_available"] = new_available

            if not allow_multiple:
                existing_inventory = _get_inventory_entry(
                    cursor, community_id, buyer_user_id, item_id, lock=True
                )
                if existing_inventory:
                    raise ValueError(
                        "Você já possui este item e ele não permite múltiplas unidades."
                    )

            if existing_inventory is None:
                existing_inventory = _get_inventory_entry(
                    cursor, community_id, buyer_user_id, item_id, lock=True
                )
            current_qty = existing_inventory["quantity"] if existing_inventory else 0
            _set_inventory_quantity(
                cursor, community_id, buyer_user_id, item_id, current_qty + quantity
            )
        else:
            inventory_entry = None
            unused_entry = next(
                (
                    entry
                    for entry in service_entries
                    if entry.get("used_in") is None
                ),
                None,
            )

            if unused_entry:
                existing_metadata = unused_entry.get("service_metadata") or {}
                service_metadata = {
                    "item_name": item.get("item_name"),
                    "duration": service_duration,
                    "allow_multiple": allow_multiple,
                    "store_item_id": int(item["id"]),
                }
                merged_metadata = {
                    key: value
                    for key, value in existing_metadata.items()
                    if value is not None
                }
                for key, value in service_metadata.items():
                    if value is not None:
                        merged_metadata[key] = value

                cursor.execute(
                    """
                    UPDATE user_inventory
                    SET quantity = quantity + %s, service_metadata = %s
                    WHERE id = %s
                    """,
                    (
                        quantity,
                        _serialize_service_metadata(merged_metadata),
                        unused_entry["id"],
                    ),
                )
                inventory_entry = _normalize_inventory_entry(
                    _fetch_inventory_entry_by_id(cursor, unused_entry["id"])
                )
            else:
                service_metadata = _serialize_service_metadata(
                    {
                        "item_name": item.get("item_name"),
                        "duration": service_duration,
                        "allow_multiple": allow_multiple,
                        "store_item_id": int(item["id"]),
                    }
                )
                cursor.execute(
                    """
                    INSERT INTO user_inventory (
                        community_id,
                        owner_user_id,
                        store_item_id,
                        quantity,
                        used_in,
                        valid_until,
                        service_metadata
                    ) VALUES (%s, %s, %s, %s, NULL, NULL, %s)
                    """,
                    (
                        community_id,
                        buyer_user_id,
                        item_id,
                        quantity,
                        service_metadata,
                    ),
                )
                inventory_entry = _normalize_inventory_entry(
                    _fetch_inventory_entry_by_id(cursor, cursor.lastrowid)
                )

        if (
            owner_user_id
            and not is_service
            and should_update_stock
            and new_available is not None
            and new_available <= 0
        ):
            cursor.execute(
                "DELETE FROM community_store WHERE id = %s",
                (item_id,),
            )

    return {
        "item": item,
        "quantity": quantity,
        "buyer_balance": new_balance,
        "service_duration": service_duration,
        "inventory_entry": inventory_entry,
    }


def ensure_service_inventory_available(
    guild_id: int,
    user: Union[discord.Member, discord.User],
    store_item_id: int,
) -> dict:
    """Verifica se há unidades disponíveis de um serviço antes de executá-lo."""

    owner_user_id = includeUser(user, guild_id)

    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        entries = _get_service_inventory_entries(
            cursor,
            community_id,
            owner_user_id,
            store_item_id,
            lock=True,
        )

        if not entries:
            raise ValueError(
                "Este serviço não está disponível no seu inventário para uso."
            )

        unused_entry = next(
            (entry for entry in entries if entry.get("used_in") is None), None
        )
        if not unused_entry or int(unused_entry.get("quantity") or 0) <= 0:
            raise ValueError(
                "Não há unidades disponíveis deste serviço para serem usadas."
            )

        return _normalize_inventory_entry(dict(unused_entry)) or {}


def mark_service_inventory_usage(
    guild_id: int,
    user: Union[discord.Member, discord.User],
    store_item_id: int,
    *,
    used_at: datetime,
    valid_until: Optional[datetime],
) -> dict:
    """Atualiza o registro de serviço utilizado no inventário do usuário."""

    owner_user_id = includeUser(user, guild_id)

    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        entries = _get_service_inventory_entries(
            cursor,
            community_id,
            owner_user_id,
            store_item_id,
            lock=True,
        )

        if not entries:
            raise ValueError(
                "Este serviço não está disponível no seu inventário para uso."
            )

        unused_entry = next(
            (entry for entry in entries if entry.get("used_in") is None), None
        )
        if not unused_entry or int(unused_entry.get("quantity") or 0) <= 0:
            raise ValueError(
                "Não há unidades disponíveis deste serviço para serem usadas."
            )

        active_entry = next(
            (
                entry
                for entry in entries
                if entry.get("used_in") is not None
            ),
            None,
        )

        metadata_snapshot = unused_entry.get("service_metadata") or {}
        if not isinstance(metadata_snapshot, dict):
            metadata_snapshot = {}

        quantity_to_consume = 1
        remaining_quantity = int(unused_entry.get("quantity") or 0) - quantity_to_consume

        if remaining_quantity < 0:
            raise ValueError(
                "Não há unidades suficientes deste serviço disponíveis para uso."
            )

        unused_entry_snapshot = dict(unused_entry)
        active_entry_snapshot = dict(active_entry) if active_entry else None
        created_active_entry_id: Optional[int] = None

        # Atualizar o registro de inventário pendente conforme necessário
        if remaining_quantity == 0:
            cursor.execute(
                "DELETE FROM user_inventory WHERE id = %s",
                (unused_entry["id"],),
            )
            updated_unused_entry = None
        else:
            cursor.execute(
                "UPDATE user_inventory SET quantity = %s WHERE id = %s",
                (remaining_quantity, unused_entry["id"]),
            )
            updated_unused_entry = _normalize_inventory_entry(
                _fetch_inventory_entry_by_id(cursor, unused_entry["id"])
            )
            if not updated_unused_entry:
                updated_unused_entry = _normalize_inventory_entry(
                    {
                        "id": unused_entry["id"],
                        "store_item_id": unused_entry.get("store_item_id"),
                        "quantity": remaining_quantity,
                        "used_in": None,
                        "valid_until": None,
                        "service_metadata": metadata_snapshot,
                    }
                )

        updated_active_entry: Optional[dict]

        if active_entry is None or (
            active_entry.get("valid_until") is not None
            and valid_until is not None
            and active_entry["valid_until"] < used_at
        ):
            if active_entry is None:
                metadata_json = _serialize_service_metadata(metadata_snapshot)
                cursor.execute(
                    """
                    INSERT INTO user_inventory (
                        community_id,
                        owner_user_id,
                        store_item_id,
                        quantity,
                        used_in,
                        valid_until,
                        service_metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        community_id,
                        owner_user_id,
                        store_item_id,
                        quantity_to_consume,
                        used_at,
                        valid_until,
                        metadata_json,
                    ),
                )
                updated_active_entry = _normalize_inventory_entry(
                    _fetch_inventory_entry_by_id(cursor, cursor.lastrowid)
                )
                if updated_active_entry:
                    created_active_entry_id = updated_active_entry.get("id")
                else:
                    created_active_entry_id = cursor.lastrowid
                    updated_active_entry = _normalize_inventory_entry(
                        {
                            "id": created_active_entry_id,
                            "store_item_id": store_item_id,
                            "quantity": quantity_to_consume,
                            "used_in": used_at,
                            "valid_until": valid_until,
                            "service_metadata": metadata_snapshot,
                        }
                    )
            else:
                active_metadata = active_entry.get("service_metadata") or {}
                if not isinstance(active_metadata, dict):
                    active_metadata = {}
                merged_metadata = {
                    key: value
                    for key, value in active_metadata.items()
                    if value is not None
                }
                for key, value in metadata_snapshot.items():
                    if value is not None:
                        merged_metadata[key] = value
                metadata_json = _serialize_service_metadata(merged_metadata)
                cursor.execute(
                    """
                    UPDATE user_inventory
                    SET quantity = %s, used_in = %s, valid_until = %s, service_metadata = %s
                    WHERE id = %s
                    """,
                    (
                        quantity_to_consume,
                        used_at,
                        valid_until,
                        metadata_json,
                        active_entry["id"],
                    ),
                )
                updated_active_entry = _normalize_inventory_entry(
                    _fetch_inventory_entry_by_id(cursor, active_entry["id"])
                )
                if not updated_active_entry:
                    updated_active_entry = _normalize_inventory_entry(
                        {
                            "id": active_entry["id"],
                            "store_item_id": store_item_id,
                            "quantity": quantity_to_consume,
                            "used_in": used_at,
                            "valid_until": valid_until,
                            "service_metadata": merged_metadata,
                        }
                    )
        else:
            cursor.execute(
                "SELECT valid_until FROM user_inventory WHERE id = %s",
                (active_entry["id"],),
            )
            current_active_row = cursor.fetchone()
            current_valid_until = (
                current_active_row["valid_until"]
                if current_active_row
                else active_entry.get("valid_until")
            )

            duration_delta = (
                valid_until - used_at if valid_until is not None else None
            )
            base_valid_until = current_valid_until or valid_until
            if duration_delta and base_valid_until is not None:
                if base_valid_until < used_at:
                    base_valid_until = used_at
                new_valid_until = base_valid_until + duration_delta
            else:
                new_valid_until = base_valid_until

            new_quantity = int(active_entry.get("quantity") or 0) + quantity_to_consume

            active_metadata = active_entry.get("service_metadata") or {}
            if not isinstance(active_metadata, dict):
                active_metadata = {}
            merged_metadata = {
                key: value
                for key, value in active_metadata.items()
                if value is not None
            }
            for key, value in metadata_snapshot.items():
                if value is not None:
                    merged_metadata[key] = value
            metadata_json = _serialize_service_metadata(merged_metadata)

            cursor.execute(
                """
                UPDATE user_inventory
                SET quantity = %s, valid_until = %s, service_metadata = %s
                WHERE id = %s
                """,
                (new_quantity, new_valid_until, metadata_json, active_entry["id"]),
            )
            updated_active_entry = _normalize_inventory_entry(
                _fetch_inventory_entry_by_id(cursor, active_entry["id"])
            )
            if not updated_active_entry:
                updated_active_entry = _normalize_inventory_entry(
                    {
                        "id": active_entry["id"],
                        "store_item_id": store_item_id,
                        "quantity": new_quantity,
                        "used_in": active_entry.get("used_in", used_at),
                        "valid_until": new_valid_until,
                        "service_metadata": merged_metadata,
                    }
                )

        if not updated_active_entry:
            raise ValueError(
                "Não foi possível atualizar o serviço ativo no inventário."
            )

    return {
        "active_entry": updated_active_entry,
        "unused_entry": updated_unused_entry,
        "rollback_snapshot": {
            "unused_entry": unused_entry_snapshot,
            "active_entry": active_entry_snapshot,
            "created_active_entry_id": created_active_entry_id,
        },
    }


def restore_service_inventory_usage(
    guild_id: int,
    user: Union[discord.Member, discord.User],
    store_item_id: int,
    snapshot: Optional[dict],
) -> None:
    """Restaura o inventário do usuário para o estado anterior ao consumo do serviço."""

    if not snapshot:
        return

    owner_user_id = includeUser(user, guild_id)

    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        entries = _get_service_inventory_entries(
            cursor,
            community_id,
            owner_user_id,
            store_item_id,
            lock=True,
        )

        previous_unused = snapshot.get("unused_entry") if snapshot else None
        previous_active = snapshot.get("active_entry") if snapshot else None
        created_active_entry_id = (
            snapshot.get("created_active_entry_id") if snapshot else None
        )

        existing_unused = next(
            (entry for entry in entries if entry.get("used_in") is None), None
        )

        if previous_unused:
            metadata = previous_unused.get("service_metadata") or {}
            metadata_json = _serialize_service_metadata(metadata)
            previous_quantity = int(previous_unused.get("quantity") or 0)

            if existing_unused:
                cursor.execute(
                    """
                    UPDATE user_inventory
                    SET quantity = %s, service_metadata = %s
                    WHERE id = %s
                    """,
                    (
                        previous_quantity,
                        metadata_json,
                        existing_unused["id"],
                    ),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO user_inventory (
                        community_id,
                        owner_user_id,
                        store_item_id,
                        quantity,
                        used_in,
                        valid_until,
                        service_metadata
                    ) VALUES (%s, %s, %s, %s, NULL, NULL, %s)
                    """,
                    (
                        community_id,
                        owner_user_id,
                        store_item_id,
                        previous_quantity,
                        metadata_json,
                    ),
                )

        if created_active_entry_id:
            cursor.execute(
                "DELETE FROM user_inventory WHERE id = %s",
                (created_active_entry_id,),
            )
        elif previous_active:
            metadata = previous_active.get("service_metadata") or {}
            metadata_json = _serialize_service_metadata(metadata)
            cursor.execute(
                """
                UPDATE user_inventory
                SET quantity = %s, used_in = %s, valid_until = %s, service_metadata = %s
                WHERE id = %s
                """,
                (
                    int(previous_active.get("quantity") or 0),
                    previous_active.get("used_in"),
                    previous_active.get("valid_until"),
                    metadata_json,
                    previous_active.get("id"),
                ),
            )


def includeLocale(guildId: int, abbrev:str, user:discord.User, availableLocals:list):
    user_id = includeUser(user, guildId)
    with pooled_connection() as cursor:
        for local in availableLocals:
            if local['locale_abbrev'] == abbrev:
                try:
                    query = f"""INSERT INTO user_locale (user_id, locale_id) VALUES ('{user_id}','{local['id']}');"""
                    cursor.execute(query)
                    return True
                except:
                    return False

def getUsersByLocale(abbrev:str, availableLocals:list):
    with pooled_connection() as cursor:
        for local in availableLocals:
            if local['locale_abbrev'] == abbrev:
                query = """
                SELECT
                    users.username AS stored_username,
                    user_discord.discord_user_id,
                    user_discord.username,
                    user_discord.display_name
                FROM users
                JOIN user_locale ON users.id = user_locale.user_id
                JOIN locale ON user_locale.locale_id = locale.id
                LEFT JOIN (
                    SELECT MAX(id) AS id, user_id
                    FROM user_discord
                    GROUP BY user_id
                ) latest_discord ON latest_discord.user_id = users.id
                LEFT JOIN user_discord ON user_discord.id = latest_discord.id
                WHERE locale.locale_abbrev = %s;"""
                cursor.execute(query, (abbrev,))
                return cursor.fetchall()


def is_user_18_plus(birthday: date, today: date | None = None) -> bool:
    reference = today or datetime.now().date()
    age = reference.year - birthday.year - (
        (reference.month, reference.day) < (birthday.month, birthday.day)
    )
    return age >= 18


def _classify_birthday_divergence(existing_date: date, informed_date: date) -> dict[str, str]:
    today = datetime.now().date()
    existing_is_18_plus = is_user_18_plus(existing_date, today)
    informed_is_18_plus = is_user_18_plus(informed_date, today)

    if not existing_is_18_plus and informed_is_18_plus:
        return {
            "divergence_type": "diferença de maioridade (antes menor, agora maior)",
            "risk_level": "risco grande",
        }

    if existing_is_18_plus and not informed_is_18_plus:
        return {
            "divergence_type": "rejuvenescimento (antes maior, agora menor)",
            "risk_level": "risco crítico",
        }

    year_diff = abs(existing_date.year - informed_date.year)
    month_diff = abs(
        (existing_date.year - informed_date.year) * 12
        + (existing_date.month - informed_date.month)
    )
    day_diff = abs((existing_date - informed_date).days)
    small_difference = (
        year_diff <= 1
        or month_diff <= 1
        or day_diff <= 31
    )

    if small_difference:
        return {
            "divergence_type": "diferença pequena",
            "risk_level": "risco mínimo",
        }

    return {
        "divergence_type": "diferença grande dentro do mesmo grupo etário",
        "risk_level": "risco menor",
    }


def register_user_informed_birthday(
    guild_id: int,
    discord_user: discord.Member | discord.User,
    informed_birthday: date,
    approved_date: date | None = None,
    user_id: int | None = None,
    actor_is_staff: bool = False,
) -> dict[str, Any]:
    """Handle birthday registration from manual user registration flow."""

    validate_birthdate(informed_birthday)

    normalized_approved = approved_date
    if normalized_approved is None:
        joined_at = getattr(discord_user, "joined_at", None)
        normalized_approved = joined_at if joined_at is not None else datetime.now()

    resolved_user_id = user_id or includeUser(discord_user, guild_id, normalized_approved)
    informed_is_18_plus = is_user_18_plus(informed_birthday)

    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT birth_date, verified, registered_at, `18_plus` FROM user_birthday WHERE user_id = %s",
            (resolved_user_id,),
        )
        row = cursor.fetchone()

    if not row:
        with pooled_connection() as cursor:
            cursor.execute(
                """
                INSERT INTO user_birthday
                    (user_id, birth_date, post_informed_date, verified, registered, registered_at, `18_plus`)
                VALUES (%s, %s, NULL, 0, 0, CURRENT_TIMESTAMP, %s)
                """,
                (resolved_user_id, informed_birthday, int(informed_is_18_plus)),
            )
        return {
            "status": "created",
            "message": "Aniversário registrado com sucesso.",
        }

    existing_birthday = row["birth_date"]
    if isinstance(existing_birthday, datetime):
        existing_birthday = existing_birthday.date()

    if existing_birthday == informed_birthday:
        registered_at = row.get("registered_at")
        should_verify = False
        if isinstance(registered_at, datetime):
            should_verify = (datetime.now() - registered_at).days > 45

        if should_verify:
            with pooled_connection() as cursor:
                cursor.execute(
                    """
                    UPDATE user_birthday
                    SET verified = 1,
                        post_informed_date = %s,
                        `18_plus` = %s
                    WHERE user_id = %s
                    """,
                    (informed_birthday, int(informed_is_18_plus), resolved_user_id),
                )

        return {
            "status": "duplicate",
            "message": (
                "Já existia registro dessa data de nascimento."
                + (
                    " Como o registro anterior tem mais de 45 dias, o perfil foi verificado."
                    if should_verify
                    else ""
                )
            ),
        }

    classification = _classify_birthday_divergence(existing_birthday, informed_birthday)
    with pooled_connection() as cursor:
        cursor.execute(
            "UPDATE user_birthday SET post_informed_date = %s WHERE user_id = %s",
            (informed_birthday, resolved_user_id),
        )

        if classification["risk_level"] == "risco mínimo" and actor_is_staff:
            cursor.execute(
                """
                UPDATE user_birthday
                SET birth_date = %s,
                    verified = 0,
                    `18_plus` = %s
                WHERE user_id = %s
                """,
                (informed_birthday, int(informed_is_18_plus), resolved_user_id),
            )
            return {
                "status": "updated_minimal_risk",
                "existing_birthday": existing_birthday,
                "informed_birthday": informed_birthday,
                "divergence_type": classification["divergence_type"],
                "risk_level": classification["risk_level"],
            }

        if classification["risk_level"] == "risco mínimo" and not actor_is_staff:
            return {
                "status": "blocked_no_staff",
                "existing_birthday": existing_birthday,
                "informed_birthday": informed_birthday,
                "divergence_type": classification["divergence_type"],
                "risk_level": classification["risk_level"],
            }

    return {
        "status": "divergent",
        "existing_birthday": existing_birthday,
        "informed_birthday": informed_birthday,
        "divergence_type": classification["divergence_type"],
        "risk_level": classification["risk_level"],
    }


def includeBirthday(
    guildId: int,
    date: date,
    user: discord.User,
    mentionable: bool,
    userId: int | None = None,
    registered: bool = False,
    actor_is_staff: bool = False,
) -> bool:
    """Create or update a birthday entry for a user using audit/divergence rules."""

    validate_birthdate(date)

    user_id = userId if userId is not None else includeUser(user, guildId)
    current_community_mentionable = _get_community_birthday_mentionable(guildId, user_id)
    mentionable_changed = (
        current_community_mentionable is None
        or bool(current_community_mentionable) != mentionable
    )
    _set_community_birthday_mentionable(guildId, user_id, mentionable)

    birthday_result = register_user_informed_birthday(
        guildId,
        user,
        date,
        None,
        user_id,
        actor_is_staff,
    )

    if birthday_result["status"] == "created":
        with pooled_connection() as cursor:
            cursor.execute(
                "UPDATE user_birthday SET registered = %s WHERE user_id = %s",
                (int(bool(registered)), user_id),
            )
        return True

    if birthday_result["status"] == "duplicate":
        marked_as_registered = False
        if registered:
            with pooled_connection() as cursor:
                cursor.execute(
                    """
                    UPDATE user_birthday
                    SET registered = 1
                    WHERE user_id = %s
                      AND registered = 0
                    """,
                    (user_id,),
                )
                marked_as_registered = cursor.rowcount > 0

        if mentionable_changed:
            raise Exception("Changed Entry")
        if marked_as_registered:
            return True
        raise Exception("Duplicate entry")

    if birthday_result["status"] == "divergent":
        raise RuntimeError(
            "Birthday divergence: "
            f"{birthday_result['divergence_type']} ({birthday_result['risk_level']})"
        )

    if birthday_result["status"] == "blocked_no_staff":
        raise PermissionError("Somente a staff pode alterar data de aniversário cadastrada.")

    if birthday_result["status"] == "updated_minimal_risk":
        return True

    return False

def getAllBirthdays(guild_id: int | None = None):
    with pooled_connection() as cursor:
        if not _ensure_birthday_mentionable_column(cursor):
            return []

        if guild_id is None:
            query = """SELECT DISTINCT user_discord.discord_user_id, user_birthday.birth_date
        FROM user_discord
        JOIN user_birthday ON user_discord.user_id = user_birthday.user_id
        JOIN user_community_status ON user_community_status.user_id = user_birthday.user_id
        WHERE user_community_status.birthday_mentionable = 1;"""
            cursor.execute(query)
        else:
            community_id = _get_community_id(cursor, guild_id)
            query = """SELECT user_discord.discord_user_id, user_birthday.birth_date
        FROM user_discord
        JOIN user_birthday ON user_discord.user_id = user_birthday.user_id
        JOIN user_community_status ON user_community_status.user_id = user_birthday.user_id
        WHERE user_community_status.community_id = %s
          AND user_community_status.birthday_mentionable = 1;"""
            cursor.execute(query, (community_id,))
        myresult = cursor.fetchall()
        #convertendo para uma lista de dicionários
        myresult = [{'user_id': i["discord_user_id"], 'birth_date': i["birth_date"]} for i in myresult]
        return myresult


def _ensure_birthday_mentionable_column(cursor) -> bool:
    """Ensure ``user_community_status.birthday_mentionable`` exists."""

    cursor.execute(
        """
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'user_community_status'
          AND COLUMN_NAME = 'birthday_mentionable'
        """
    )
    if cursor.fetchone():
        return True

    try:
        cursor.execute(
            "ALTER TABLE user_community_status ADD COLUMN birthday_mentionable TINYINT(1) NOT NULL DEFAULT 0"
        )
        cursor.execute(
            """
            SELECT 1
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'user_birthday'
              AND COLUMN_NAME = 'mentionable'
            """
        )
        if cursor.fetchone():
            cursor.execute(
                """
                UPDATE user_community_status ucs
                JOIN user_birthday ub ON ub.user_id = ucs.user_id
                SET ucs.birthday_mentionable = ub.mentionable
                """
            )
        return True
    except mysql.connector.Error as err:
        logging.error(
            "Falha ao criar coluna birthday_mentionable em user_community_status: %s",
            err,
        )
        return False


def _set_community_birthday_mentionable(guild_id: int, user_id: int, mentionable: bool) -> None:
    """Persist birthday mention preference scoped to the current community."""

    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        if not _ensure_birthday_mentionable_column(cursor):
            return

        cursor.execute(
            """
            UPDATE user_community_status
            SET birthday_mentionable = %s
            WHERE user_id = %s AND community_id = %s
            """,
            (int(mentionable), user_id, community_id),
        )


def _get_community_birthday_mentionable(guild_id: int, user_id: int) -> bool | None:
    """Return birthday mention preference scoped to the given community."""

    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        if not _ensure_birthday_mentionable_column(cursor):
            return None

        cursor.execute(
            """
            SELECT birthday_mentionable
            FROM user_community_status
            WHERE user_id = %s AND community_id = %s
            """,
            (user_id, community_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return bool(row["birthday_mentionable"])

def getUserInfo(user: Union[discord.Member, discord.User], guildId: int, userId: int = None) -> User :
    """Retrieve a user from the database. Optionally registers the user if missing."""
    user_id = includeUser(user, guildId)

    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guildId)
        query = (
            "SELECT user_discord.discord_user_id, user_discord.display_name, "
            "user_community_status.member_since, user_community_status.approved, "
            "user_community_status.approved_at, user_community_status.is_vip, "
            "user_community_status.is_partner, user_level.current_level, "
            "user_birthday.birth_date, user_birthday.verified, locale.locale_name, "
            "user_economy.bank_balance "
            "FROM users "
            "LEFT JOIN user_discord ON users.id = user_discord.user_id "
            "LEFT JOIN user_community_status ON users.id = user_community_status.user_id "
            "AND user_community_status.community_id = %s "
            "LEFT JOIN user_birthday ON user_birthday.user_id = users.id "
            "LEFT JOIN user_level ON user_level.user_id = users.id AND user_level.server_guild_id = %s "
            "LEFT JOIN user_locale ON user_locale.user_id = users.id "
            "LEFT JOIN locale ON locale.id = user_locale.locale_id "
            "LEFT JOIN user_warnings ON user_warnings.user_id = users.id "
            "LEFT JOIN user_economy ON user_economy.user_id = users.id AND user_economy.server_guild_id = %s "
            "WHERE user_discord.user_id = %s"
        )
        cursor.execute(query, (community_id, guildId, guildId, user_id))
        dbUser = cursor.fetchone()
        if not dbUser:
            return None

        userToReturn = User(
            id=user_id,
            discordId=user.id,
            username=user.name,
            displayName=getattr(user, 'display_name', user.name),
            memberSince=dbUser["member_since"],
            approved=dbUser["approved"],
            approvedAt=dbUser["approved_at"],
            isVip=dbUser["is_vip"],
            isPartner=dbUser["is_partner"],
            level=dbUser["current_level"],
            birthday=dbUser["birth_date"],
            birthdayVerified=dbUser["verified"],
            locale=dbUser["locale_name"],
            coins=dbUser["bank_balance"],
            warnings=[],
            inventory=[],
            staffOf=[],
        )

        cursor.execute(
            "SELECT user_warnings.date, user_warnings.reason, user_warnings.expired "
            "FROM user_warnings JOIN users ON user_warnings.user_id = users.id "
            "WHERE users.id = %s "
            "ORDER BY user_warnings.date DESC",
            (user_id,),
        )
        warnings = cursor.fetchall() or []
        userToReturn.warnings = [Warning(i["date"], i["reason"], i["expired"]) for i in warnings]

        return userToReturn


def getAltAccounts(member: discord.Member | discord.User) -> list[int]:
    """Return a list of other Discord IDs linked to the same user."""
    user_id = getUserId(member.id)
    if user_id is None:
        return []

    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT discord_user_id FROM user_discord WHERE user_id = %s AND discord_user_id <> %s",
            (user_id, member.id),
        )
        rows = cursor.fetchall() or []
        return [row["discord_user_id"] for row in rows]
    

def getAllEvents():
    with pooled_connection() as cursor:
        query = f"""
        SELECT  events.id, 
                events.event_name, 
                events.address, 
                events.point_name, 
                events.price, 
                events.max_price, 
                events.starting_datetime, 
                events.ending_datetime, 
                events.description, 
                events.group_chat_link, 
                users.username, 
                locale.locale_name AS state, 
                locale.locale_abbrev AS state_abbrev, 
                events.city, 
                events.website, 
                events.out_of_tickets, 
                events.sales_ended
        FROM events
        JOIN users ON events.host_user_id = users.id
        JOIN locale ON events.locale_id = locale.id
        WHERE events.approved = 1"""
        cursor.execute(query)
        events = cursor.fetchall()
        for e in events:
            e['starting_datetime'] = datetime.strptime(f"{e['starting_datetime']}", '%Y-%m-%d %H:%M:%S')
            e['ending_datetime'] = datetime.strptime(f"{e['ending_datetime']}", '%Y-%m-%d %H:%M:%S')
            if e['website'] != None and not e['website'].__contains__('http'):
                e['website'] = f'https://{e["website"]}'
            if e['group_chat_link'] != None and not e['group_chat_link'].__contains__('http'):
                e['group_chat_link'] = f'https://{e["group_chat_link"]}'
        events = [e for e in events if e['ending_datetime'] >= datetime.now()]
        return events

def getEventsByState(locale_id:int):
    with pooled_connection() as cursor:
        query = f"""
        SELECT  events.id, 
                events.event_name, 
                events.address, 
                events.point_name, 
                events.price, 
                events.max_price, 
                events.starting_datetime, 
                events.ending_datetime, 
                events.description, 
                events.group_chat_link, 
                users.username, 
                locale.locale_name AS state, 
                locale.locale_abbrev AS state_abbrev, 
                events.city, 
                events.website, 
                events.out_of_tickets, 
                events.sales_ended
        FROM events
        JOIN users ON events.host_user_id = users.id
        JOIN locale ON events.locale_id = locale.id
        WHERE events.locale_id = '{locale_id}'
        AND events.approved = 1;"""
        cursor.execute(query)
        events = cursor.fetchall()
        for e in events:
            e['starting_datetime'] = datetime.strptime(f"{e['starting_datetime']}", '%Y-%m-%d %H:%M:%S')
            e['ending_datetime'] = datetime.strptime(f"{e['ending_datetime']}", '%Y-%m-%d %H:%M:%S')
            if e['website'] != None and not e['website'].__contains__('http'):
                e['website'] = f'https://{e["website"]}'
            if e['group_chat_link'] != None and not e['group_chat_link'].__contains__('http'):
                e['group_chat_link'] = f'https://{e["group_chat_link"]}'
        events = [e for e in events if e['ending_datetime'] >= datetime.now()]
        return events

def getEventByName(event_name:str):
    with pooled_connection() as cursor:
        query = f"""
        SELECT  events.id, 
                events.event_name, 
                events.address, 
                events.point_name, 
                events.price, 
                events.max_price, 
                events.starting_datetime, 
                events.ending_datetime, 
                events.description, 
                events.group_chat_link, 
                users.username, 
                locale.locale_name AS state, 
                locale.locale_abbrev AS state_abbrev, 
                events.city, 
                events.website, 
                events.event_logo_url, 
                events.max_price, 
                events.out_of_tickets, 
                events.sales_ended
        FROM events
        JOIN users ON events.host_user_id = users.id
        JOIN locale ON events.locale_id = locale.id"""
        cursor.execute(query + f""" WHERE events.event_name = '{event_name}' AND events.approved = 1;""")
        events = cursor.fetchall()
        if events == []:
            cursor.execute(query + f""" WHERE events.event_name LIKE '%{event_name}%' AND events.approved = 1;""")
            events = cursor.fetchall()
        for e in events:
            e['starting_datetime'] = datetime.strptime(f"{e['starting_datetime']}", '%Y-%m-%d %H:%M:%S')
            e['ending_datetime'] = datetime.strptime(f"{e['ending_datetime']}", '%Y-%m-%d %H:%M:%S')
            if e['website'] != None and not e['website'].__contains__('http'):
                e['website'] = f'https://{e["website"]}'
            if e['group_chat_link'] != None and not e['group_chat_link'].__contains__('http'):
                e['group_chat_link'] = f'https://{e["group_chat_link"]}'
        return events[0] if events != [] else None 
    
def getEventsByOwner(owner_name:str):
    with pooled_connection() as cursor:
        query = f"""
        SELECT  events.id, 
                events.event_name, 
                events.address, 
                events.point_name, 
                events.price, 
                events.max_price, 
                events.starting_datetime, 
                events.ending_datetime, 
                events.description, 
                events.group_chat_link, 
                users.username, 
                locale.locale_name AS state, 
                locale.locale_abbrev AS state_abbrev, 
                events.city, 
                events.website, 
                events.out_of_tickets, 
                events.sales_ended
        FROM events
        JOIN users ON events.host_user_id = users.id
        JOIN locale ON events.locale_id = locale.id
        WHERE users.username = '{owner_name}';"""
        cursor.execute(query)
        eventos = cursor.fetchall()
        for e in eventos:
            e['starting_datetime'] = datetime.strptime(f"{e['starting_datetime']}", '%Y-%m-%d %H:%M:%S')
            e['ending_datetime'] = datetime.strptime(f"{e['ending_datetime']}", '%Y-%m-%d %H:%M:%S')
            if e['website'] != None and not e['website'].__contains__('http'):
                e['website'] = f'https://{e["website"]}'
            if e['group_chat_link'] != None and not e['group_chat_link'].__contains__('http'):
                e['group_chat_link'] = f'https://{e["group_chat_link"]}'
        return eventos

def admConnectTelegramAccount(guild_id: int, discord_user:discord.Member, user_telegram:str):
    with pooled_connection() as cursor:
        #checa se o usuário já está cadastrado no banco de dados
        user_id = includeUser(discord_user, guild_id)
        #checa se o user_telegram já está cadastrado no banco de dados
        query = f"""SELECT * FROM user_telegram WHERE user_id = '{user_id}';"""
        cursor.execute(query)
        myresult = cursor.fetchall()
        if myresult == []:
            try:
                query = f"""INSERT INTO user_telegram (user_id, username, display_name, banned)
            VALUES ('{user_id}', '{user_telegram}', '{discord_user.nick}', 'FALSE');"""
                cursor.execute(query)
                return True
            except:
                return False
        else:
            return True


async def assignTempRole(
    guild_id: int,
    discord_user: discord.Member,
    role_id: int | str,
    expiring_date: datetime,
    reason: str,
) -> bool:
    """Add or extend a temporary role grant for ``discord_user``."""
    if discord_user is None:
        logging.error("Usuário nulo ao registrar temp role")
        return False

    role = discord_user.guild.get_role(int(role_id))
    if role is None:
        logging.error(
            "Cargo temporário %s não encontrado na guild %s", role_id, guild_id
        )
        return False

    async with temp_role_lock(guild_id, discord_user.id, role_id):
        return await _assignTempRoleLocked(
            guild_id,
            discord_user,
            role,
            expiring_date,
            reason,
        )


async def _assignTempRoleLocked(
    guild_id: int,
    discord_user: discord.Member,
    role: discord.Role,
    expiring_date: datetime,
    reason: str,
) -> bool:
    """Apply one temporary-role grant while its lifecycle lock is held."""

    try:
        with pooled_connection() as cursor:
            cursor.execute(
                "SELECT id FROM community_discord WHERE guild_id = %s", (guild_id,)
            )
            row = cursor.fetchone()
            if not row:
                logging.error(
                    "Servidor %s não encontrado ao registrar temp role", guild_id
                )
                return False
            discord_community_id = row["id"]

            user_id = includeUser(discord_user, guild_id)
            cursor.execute(
                "SELECT id FROM user_discord WHERE user_id = %s", (user_id,)
            )
            row = cursor.fetchone()
            if not row:
                logging.error(
                    "Usuário %s não encontrado ao registrar temp role", discord_user.id
                )
                return False
            discord_user_id = row["id"]
    except Exception as e:
        logging.error("Erro ao resolver identidade para temp role: %s", e)
        return False

    role_was_present = role in discord_user.roles
    role_added_now = False
    if not role_was_present:
        try:
            await discord_user.add_roles(role)
            role_added_now = True
        except Exception as e:
            logging.error("Erro ao adicionar cargo temporário: %s", e)
            return False

    try:
        with pooled_connection() as cursor:
            cursor.execute(
                """
                SELECT id, expiring_date, reason
                FROM user_temp_roles
                WHERE disc_community_id = %s
                  AND disc_user_id = %s
                  AND role_id = %s
                ORDER BY expiring_date DESC, id ASC
                FOR UPDATE
                """,
                (discord_community_id, discord_user_id, role.id),
            )
            grants = cursor.fetchall()
            if grants:
                keeper = grants[0]
                if expiring_date > keeper["expiring_date"]:
                    cursor.execute(
                        """
                        UPDATE user_temp_roles
                        SET expiring_date = %s, reason = %s
                        WHERE id = %s
                        """,
                        (expiring_date, reason, keeper["id"]),
                    )
                for duplicate in grants[1:]:
                    cursor.execute(
                        "DELETE FROM user_temp_roles WHERE id = %s",
                        (duplicate["id"],),
                    )
            else:
                cursor.execute(
                    "INSERT INTO user_temp_roles (disc_community_id, disc_user_id, role_id, expiring_date, reason)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (
                        discord_community_id,
                        discord_user_id,
                        role.id,
                        expiring_date,
                        reason,
                    ),
                )
    except Exception as e:
        logging.error("Erro ao registrar temp role: %s", e)
        if role_added_now:
            try:
                await discord_user.remove_roles(role)
            except Exception as compensation_error:
                logging.error(
                    "Erro ao compensar cargo temporário sem persistência: %s",
                    compensation_error,
                )
        return False

    return True
    
def getExpiringTempRoles(guild_id:int):
    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT id FROM community_discord WHERE guild_id = %s", (guild_id,)
        )
        community = cursor.fetchone()
        if not community:
            return []
        cursor.execute(
            """
            SELECT user_temp_roles.id,
                   user_temp_roles.disc_community_id,
                   user_temp_roles.disc_user_id,
                   user_temp_roles.role_id,
                   user_discord.discord_user_id AS user_id
            FROM user_temp_roles
            JOIN user_discord ON user_temp_roles.disc_user_id = user_discord.id
            WHERE user_temp_roles.disc_community_id = %s
              AND user_temp_roles.expiring_date <= NOW()
            ORDER BY user_temp_roles.expiring_date ASC, user_temp_roles.id ASC
            """,
            (community["id"],),
        )
        return cursor.fetchall()


def hasActiveTempRoleSibling(
    discord_community_id: int,
    discord_user_id: int,
    role_id: int,
    excluded_id: int,
) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM user_temp_roles
            WHERE disc_community_id = %s
              AND disc_user_id = %s
              AND role_id = %s
              AND id <> %s
              AND expiring_date > NOW()
            LIMIT 1
            """,
            (discord_community_id, discord_user_id, role_id, excluded_id),
        )
        return cursor.fetchone() is not None


def getTempRoleExpirationState(temp_role_id: int) -> dict | None:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT expiring_date, expiring_date <= NOW() AS is_expired
            FROM user_temp_roles
            WHERE id = %s
            """,
            (temp_role_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "expiring_date": row["expiring_date"],
            "is_expired": bool(row["is_expired"]),
        }


def deleteTempRole(tempRoleDBId:int):
    with pooled_connection() as cursor:
        try:
            cursor.execute(
                "DELETE FROM user_temp_roles WHERE id = %s", (tempRoleDBId,)
            )
            return True
        except Exception as e:
            logger.error(
                'Falha ao remover cargo temporário do banco (tipo=%s)',
                type(e).__name__,
            )
            return False

def warnMember(
    guild_id: int,
    discord_user: Union[discord.Member, discord.User],
    reason: str,
    applied_by: discord.Member,
):
    with pooled_connection() as cursor:
        user_id = includeUser(discord_user, guild_id)
        applied_by_id = includeUser(applied_by, guild_id)
        query = f"""SELECT community_id FROM community_discord WHERE guild_id = '{guild_id}';"""
        cursor.execute(query)
        community_id = cursor.fetchone()["community_id"]
        date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            query = f"""INSERT INTO user_warnings (user_id, community_id, date, reason, expired, applied_by)
            VALUES ('{user_id}', '{community_id}', '{date}', '{reason}', FALSE, '{applied_by_id}');"""
            cursor.execute(query)
            query = f"""SELECT COUNT(*) AS warnings_count FROM user_warnings
    WHERE user_id = '{user_id}'
    AND community_id = '{community_id}';"""
            cursor.execute(query)
            warningsCount = cursor.fetchone()["warnings_count"]
            #pegar o número de warnings necessários para banir o usuário
            query = f"""SELECT warnings_limit FROM config_warnings_settings
    WHERE community_id = '{community_id}';"""
            cursor.execute(query)
            warningsLimit = cursor.fetchone()["warnings_limit"]
            return {'warningsCount': warningsCount, 'warningsLimit': warningsLimit}
        except Exception as e:
            # The SQL includes a moderation reason, so avoid exposing the
            # exception/query text through the administrative log endpoint.
            logger.error(
                'Falha ao registrar advertência no banco (tipo=%s)',
                type(e).__name__,
            )
            return False


def registerUserBan(
    guild_id: int,
    discord_user: Union[discord.Member, discord.User],
    reason: str,
    applied_by: Union[discord.Member, discord.User, None],
    *,
    valid_until: datetime | None,
    can_appeal: bool,
    ban_date: datetime | None = None,
    allow_incomplete_record: bool = False,
) -> int | None:
    """Register one administrative ban and membership flag atomically."""
    try:
        with pooled_connection() as cursor:
            user_id = includeUser(discord_user, guild_id)
            if not applied_by and not allow_incomplete_record:
                logging.error(
                    "Tentativa de registrar banimento do usuário %s sem 'applied_by' fora do fluxo permitido",
                    user_id,
                )
                return False

            applied_by_id = includeUser(applied_by, guild_id) if applied_by else None

            cursor.execute(
                "SELECT community_id FROM community_discord WHERE guild_id = %s",
                (guild_id,),
            )
            community_row = cursor.fetchone()
            if not community_row:
                logging.error(
                    "Comunidade do servidor %s não encontrada ao registrar banimento",
                    guild_id,
                )
                return False

            community_id = community_row["community_id"]
            ban_date_str = ban_date.strftime('%Y-%m-%d %H:%M:%S') if ban_date else None
            if not ban_date_str and not allow_incomplete_record:
                ban_date_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            valid_until_str = valid_until.strftime('%Y-%m-%d %H:%M:%S') if valid_until else None

            cursor.execute(
                """
                INSERT INTO user_bans (
                    user_id,
                    community_id,
                    date,
                    reason,
                    can_appeal,
                    valid_until,
                    applied_by,
                    registered_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                """,
                (user_id, community_id, ban_date_str, reason, can_appeal, valid_until_str, applied_by_id),
            )
            ban_id = int(cursor.lastrowid)

            cursor.execute(
                """
                UPDATE user_community_status
                SET banned = 1
                WHERE user_id = %s AND community_id = %s
                """,
                (user_id, community_id),
            )
            return ban_id
    except Exception as error:
        logging.error("Erro ao registrar banimento: %s", type(error).__name__)
        return False


def getExpiredBans(guild_id: int) -> list[dict]:
    """Retrieve expired ban effects plus parent actions ready for finalization."""
    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT community_id FROM community_discord WHERE guild_id = %s",
            (guild_id,),
        )
        community_row = cursor.fetchone()
        if not community_row:
            logging.error(
                "Comunidade do servidor %s não encontrada ao buscar banimentos expirados",
                guild_id,
            )
            return []

        community_id = community_row["community_id"]
        cursor.execute(
            """
            SELECT
                ub.id AS ban_id,
                ub.user_id,
                effects.id AS effect_id,
                effects.discord_user_id
            FROM user_bans ub
            JOIN user_ban_discord_effects effects ON effects.ban_id = ub.id
            WHERE ub.community_id = %s
              AND effects.outcome = 'APPLIED'
              AND effects.reverted_at IS NULL
              AND (
                  (
                      ub.revoked_at IS NULL
                      AND ub.valid_until IS NOT NULL
                      AND ub.valid_until <= NOW()
                  )
                  OR ub.revoked_at IS NOT NULL
              )
            UNION ALL
            SELECT
                ub.id AS ban_id,
                ub.user_id,
                NULL AS effect_id,
                ud.discord_user_id
            FROM user_bans ub
            JOIN user_discord ud ON ud.user_id = ub.user_id
            WHERE ub.community_id = %s
              AND ub.valid_until IS NOT NULL
              AND ub.valid_until <= NOW()
              AND ub.revoked_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM user_ban_discord_effects effects
                  WHERE effects.ban_id = ub.id
              )
            UNION ALL
            SELECT
                ub.id AS ban_id,
                ub.user_id,
                NULL AS effect_id,
                NULL AS discord_user_id
            FROM user_bans ub
            WHERE ub.community_id = %s
              AND ub.valid_until IS NOT NULL
              AND ub.valid_until <= NOW()
              AND ub.revoked_at IS NULL
              AND EXISTS (
                  SELECT 1 FROM user_ban_discord_effects effects
                  WHERE effects.ban_id = ub.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_ban_discord_effects effects
                  WHERE effects.ban_id = ub.id
                    AND effects.outcome = 'APPLIED'
                    AND effects.reverted_at IS NULL
              )
            """,
            (community_id, community_id, community_id),
        )

        return cursor.fetchall() or []


def getLatestBanDate(guild_id: int, discord_user_id: int) -> datetime | None:
    """Return the most recent ban date for a Discord user in a guild if still marked as banned."""

    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT community_id FROM community_discord WHERE guild_id = %s",
            (guild_id,),
        )
        community_row = cursor.fetchone()
        if not community_row:
            return None

        community_id = community_row["community_id"]
        cursor.execute(
            """
            SELECT ub.date
            FROM user_bans ub
            JOIN user_discord ud ON ud.user_id = ub.user_id
            JOIN user_community_status ucs
              ON ucs.user_id = ub.user_id
             AND ucs.community_id = ub.community_id
            WHERE ub.community_id = %s
              AND ud.discord_user_id = %s
              AND ucs.banned = 1
            ORDER BY ub.date DESC
            LIMIT 1
            """,
            (community_id, discord_user_id),
        )
        row = cursor.fetchone()
        return row["date"] if row else None


def hasActiveBanRecord(guild_id: int, discord_user_id: int) -> bool:
    """Return whether a Discord user has any active ban record in the guild."""

    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT community_id FROM community_discord WHERE guild_id = %s",
            (guild_id,),
        )
        community_row = cursor.fetchone()
        if not community_row:
            return False

        community_id = community_row["community_id"]
        cursor.execute(
            """
            SELECT 1
            FROM user_bans ub
            JOIN user_discord ud ON ud.user_id = ub.user_id
            JOIN user_community_status ucs
              ON ucs.user_id = ub.user_id
             AND ucs.community_id = ub.community_id
            WHERE ub.community_id = %s
              AND ud.discord_user_id = %s
              AND ucs.banned = 1
            LIMIT 1
            """,
            (community_id, discord_user_id),
        )
        return cursor.fetchone() is not None


def getBanRecords(
    guild_id: int,
    discord_user_id: int,
) -> list[dict]:
    """Return all ban records for a Discord user in a guild sorted by most recent date."""

    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT community_id FROM community_discord WHERE guild_id = %s",
            (guild_id,),
        )
        community_row = cursor.fetchone()
        if not community_row:
            return []

        community_id = community_row["community_id"]
        cursor.execute(
            "SELECT user_id FROM user_discord WHERE discord_user_id = %s",
            (discord_user_id,),
        )
        user_row = cursor.fetchone()
        if not user_row:
            return []
        user_id = user_row["user_id"]
        cursor.execute(
            """
            SELECT ub.date, ub.reason, ub.valid_until, ub.registered_at
            FROM user_bans ub
            WHERE ub.community_id = %s
              AND ub.user_id = %s
            ORDER BY ub.date DESC, ub.registered_at DESC
            """,
            (community_id, user_id),
        )
        return cursor.fetchall() or []


def _refresh_identity_ban_membership_flags(
    cursor,
    ban_id: int,
    community_id: int,
) -> None:
    """Recompute existing membership flags after one identity-wide ban is revoked."""
    cursor.execute(
        """
        SELECT user_id
        FROM user_bans
        WHERE id = %s
        UNION
        SELECT identity_user_id AS user_id
        FROM user_ban_discord_effects
        WHERE ban_id = %s
        """,
        (ban_id, ban_id),
    )
    affected_user_ids = [
        int(row["user_id"])
        for row in (cursor.fetchall() or [])
    ]

    for user_id in affected_user_ids:
        cursor.execute(
            """
            SELECT 1
            FROM user_bans active_ban
            WHERE active_ban.community_id = %s
              AND active_ban.revoked_at IS NULL
              AND (
                  active_ban.valid_until IS NULL
                  OR active_ban.valid_until > NOW()
              )
              AND (
                  active_ban.user_id = %s
                  OR EXISTS (
                      SELECT 1
                      FROM user_ban_discord_effects active_effect
                      WHERE active_effect.ban_id = active_ban.id
                        AND active_effect.identity_user_id = %s
                  )
              )
            LIMIT 1
            """,
            (community_id, user_id, user_id),
        )
        still_banned = cursor.fetchone() is not None
        cursor.execute(
            """
            UPDATE user_community_status
            SET banned = %s
            WHERE user_id = %s
              AND community_id = %s
            """,
            (1 if still_banned else 0, user_id, community_id),
        )


def removeBanRecord(ban_id: int) -> bool:
    """Revoke a ban record and membership flag in one transaction."""
    try:
        with pooled_connection() as cursor:
            cursor.execute(
                "SELECT user_id, community_id FROM user_bans WHERE id = %s",
                (ban_id,),
            )
            ban_row = cursor.fetchone()
            if not ban_row:
                logging.warning("Ban record %s not found", ban_id)
                return False

            user_id = ban_row["user_id"]
            community_id = ban_row["community_id"]

            cursor.execute(
                """
                UPDATE user_bans
                SET revoked_at = COALESCE(revoked_at, UTC_TIMESTAMP(6)),
                    revocation_reason = COALESCE(
                        revocation_reason, 'Ban temporário expirado'
                    )
                WHERE id = %s
                """,
                (ban_id,),
            )

            _refresh_identity_ban_membership_flags(
                cursor,
                ban_id,
                community_id,
            )
            return True
    except Exception as error:
        logging.error(
            "Erro ao remover registro de banimento %s: %s",
            ban_id,
            type(error).__name__,
        )
        return False


def markBanDiscordEffectReverted(
    effect_id: int,
    outcome: str,
    error_code: str | None = None,
) -> bool:
    """Record one reversal attempt; terminal successful outcomes are idempotent."""
    terminal = outcome in {"REVERTED", "ALREADY_UNBANNED", "NOT_FOUND"}
    with pooled_connection() as cursor:
        try:
            cursor.execute(
                """
                UPDATE user_ban_discord_effects
                SET revert_outcome = %s,
                    error_code = %s,
                    reverted_at = IF(%s, UTC_TIMESTAMP(6), NULL)
                WHERE id = %s AND reverted_at IS NULL
                """,
                (outcome, error_code, terminal, effect_id),
            )
            return cursor.rowcount == 1
        except Exception as error:
            logging.error(
                "Erro ao registrar reversão do efeito %s: %s",
                effect_id,
                type(error).__name__,
            )
            return False


def finalizeExpiredBan(ban_id: int) -> bool:
    """Revoke an expired action only after every APPLIED effect is terminal."""
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) AS pending
            FROM user_ban_discord_effects
            WHERE ban_id = %s AND outcome = 'APPLIED' AND reverted_at IS NULL
            """,
            (ban_id,),
        )
        if cursor.fetchone()["pending"]:
            return False
    return removeBanRecord(ban_id)


def registerWarnIfAbsent(
    guild_id: int,
    discord_user: Union[discord.Member, discord.User],
    reason: str,
    applied_by: discord.Member | discord.User | None,
    warn_date: datetime,
) -> dict | None:
    """Register a warn only if it has not been stored already.

    A warn is considered a duplicate when another entry exists for the same
    member, community, reason and timestamp.
    """

    with pooled_connection() as cursor:
        user_id = includeUser(discord_user, guild_id)
        applied_by_id = includeUser(applied_by, guild_id) if applied_by else None

        cursor.execute(
            "SELECT community_id FROM community_discord WHERE guild_id = %s",
            (guild_id,),
        )
        row = cursor.fetchone()
        if not row:
            logging.error(
                "Servidor %s não encontrado ao registrar warn importado", guild_id
            )
            return None

        community_id = row["community_id"]

        cursor.execute(
            """
            SELECT id FROM user_warnings
            WHERE user_id = %s AND community_id = %s AND reason = %s AND date = %s
            """,
            (user_id, community_id, reason, warn_date),
        )

        if cursor.fetchone():
            cursor.execute(
                """
                SELECT COUNT(*) AS warnings_count FROM user_warnings
                WHERE user_id = %s AND community_id = %s
                """,
                (user_id, community_id),
            )
            warnings_count = cursor.fetchone()["warnings_count"]
            return {"created": False, "warningsCount": warnings_count}

        created = False
        try:
            cursor.execute(
                """
                INSERT INTO user_warnings (user_id, community_id, date, reason, expired, applied_by)
                VALUES (%s, %s, %s, %s, FALSE, %s)
                """,
                (user_id, community_id, warn_date, reason, applied_by_id),
            )
            created = True
        except IntegrityError as err:
            if err.errno != errorcode.ER_DUP_ENTRY:
                raise

        cursor.execute(
            """
            SELECT COUNT(*) AS warnings_count FROM user_warnings
            WHERE user_id = %s AND community_id = %s
            """,
            (user_id, community_id),
        )
        warnings_count = cursor.fetchone()["warnings_count"]

        cursor.execute(
            """
            SELECT warnings_limit FROM config_warnings_settings
            WHERE community_id = %s
            """,
            (community_id,),
        )
        warnings_limit_row = cursor.fetchone()
        warnings_limit = (
            warnings_limit_row["warnings_limit"] if warnings_limit_row else None
        )

        return {
            "created": created,
            "warningsCount": warnings_count,
            "warningsLimit": warnings_limit,
        }

def getMemberWarnings(
    guild_id: int, discord_user: Union[discord.Member, discord.User]
) -> list[Warning]:
    with pooled_connection() as cursor:
        user_id = includeUser(discord_user, guild_id)
        query = f"""SELECT community_id FROM community_discord WHERE guild_id = '{guild_id}';"""
        cursor.execute(query)
        community_id = cursor.fetchone()["community_id"]
        query = f"""SELECT date, reason, expired FROM user_warnings
    WHERE user_id = '{user_id}'
    AND community_id = '{community_id}'
    ORDER BY date DESC;"""
        cursor.execute(query)
        results:list[Warning] = cursor.fetchall()
        warnings = [
            Warning(warning["date"], warning["reason"], warning["expired"])
            for warning in results
        ]
        return warnings


def getMemberNotes(
    guild_id: int,
    discord_user: Union[discord.Member, discord.User],
) -> list[UserNote]:
    with pooled_connection() as cursor:
        user_id = includeUser(discord_user, guild_id)
        cursor.execute(
            """
            SELECT un.id, un.note,
                   (SELECT MIN(aud.discord_user_id) FROM user_discord aud WHERE aud.user_id = un.author_user_id) AS author_discord_id
            FROM user_notes un
            WHERE un.user_id = %s
            ORDER BY un.id DESC
            """,
            (user_id,),
        )
        notes = cursor.fetchall() or []
        return [
            UserNote(
                note=note_row["note"],
                author_discord_id=note_row["author_discord_id"],
                note_id=note_row["id"],
            )
            for note_row in notes
        ]


def getMemberNotesCount(
    guild_id: int,
    discord_user: Union[discord.Member, discord.User],
) -> int:
    with pooled_connection() as cursor:
        user_id = includeUser(discord_user, guild_id)
        cursor.execute(
            "SELECT COUNT(*) AS notes_count FROM user_notes WHERE user_id = %s",
            (user_id,),
        )
        result = cursor.fetchone()
        return result["notes_count"] if result else 0


def addMemberNote(
    guild_id: int,
    discord_user: Union[discord.Member, discord.User],
    note: str,
    author: Union[discord.Member, discord.User],
) -> Optional[int]:
    with pooled_connection() as cursor:
        user_id = includeUser(discord_user, guild_id)
        author_id = includeUser(author, guild_id)
        try:
            cursor.execute(
                "INSERT INTO user_notes (user_id, note, author_user_id) VALUES (%s, %s, %s)",
                (user_id, note, author_id),
            )
        except Exception as e:
            logging.error("Erro ao adicionar nota para o usuário %s: %s", user_id, e)
            return None

        return cursor.lastrowid

def _ensure_staff_roles_column(cursor) -> bool:
    """Check the Flyway-owned staff_roles column; never mutate schema at runtime."""

    cursor.execute(
        """
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'config_server_settings'
          AND COLUMN_NAME = 'staff_roles'
        LIMIT 1
        """
    )
    exists = cursor.fetchone() is not None
    if not exists:
        logging.error(
            "config_server_settings.staff_roles is missing; apply Database migrations"
        )
    return exists

def _ensure_auto_join_roles_columns(cursor) -> bool:
    """Ensure auto-join role columns exist in ``config_server_settings``."""

    required_columns = {
        "auto_join_roles_enabled": "TINYINT(1) NOT NULL DEFAULT 0",
        "auto_join_role_ids": "TEXT NULL",
    }

    for column_name, column_definition in required_columns.items():
        cursor.execute(
            """
            SELECT 1
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'config_server_settings'
              AND COLUMN_NAME = %s
            LIMIT 1
            """,
            (column_name,),
        )
        if cursor.fetchone():
            continue

        try:
            cursor.execute(
                f"ALTER TABLE config_server_settings ADD COLUMN {column_name} {column_definition}"
            )
        except mysql.connector.Error as err:
            if err.errno == errorcode.ER_DUP_FIELDNAME:
                continue
            logging.error(
                "Falha ao criar coluna %s em config_server_settings: %s",
                column_name,
                err,
            )
            return False

    return True


def _ensure_birthday_channel_column(cursor) -> bool:
    """Ensure ``config_server_settings.birthday_channel_id`` exists."""

    cursor.execute(
        """
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'config_server_settings'
          AND COLUMN_NAME = 'birthday_channel_id'
        LIMIT 1
        """
    )
    if cursor.fetchone():
        return True

    try:
        cursor.execute(
            "ALTER TABLE config_server_settings ADD COLUMN birthday_channel_id BIGINT NULL"
        )
        return True
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_DUP_FIELDNAME:
            return True
        logging.error(
            "Falha ao criar coluna birthday_channel_id em config_server_settings: %s",
            err,
        )
        return False


def _ensure_collaborative_moderation_columns(cursor) -> bool:
    """Check Flyway-owned collaborative moderation columns."""

    required_columns = {
        "collab_moderation_enabled",
        "collab_moderation_emoji",
        "collab_moderation_min_reactions",
    }
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'config_server_settings'
          AND COLUMN_NAME IN (
              'collab_moderation_enabled',
              'collab_moderation_emoji',
              'collab_moderation_min_reactions'
          )
        """
    )
    present = {str(row.get("COLUMN_NAME")) for row in (cursor.fetchall() or [])}
    missing = sorted(required_columns - present)
    if missing:
        logging.error(
            "Collaborative moderation schema is behind Flyway; missing columns: %s",
            ", ".join(missing),
        )
        return False
    return True

def _ensure_hashtag_role_mentions_columns(cursor) -> bool:
    """Ensure hashtag-driven role mention columns exist in ``config_server_settings``."""

    required_columns = {
        "hashtag_role_mentions_enabled": "TINYINT(1) NOT NULL DEFAULT 0",
        "hashtag_role_mentions_channel_ids": "TEXT NULL",
        "hashtag_role_mentions_author_role_id": "BIGINT NULL",
        "hashtag_role_mentions_map": "TEXT NULL",
    }

    for column_name, column_definition in required_columns.items():
        cursor.execute(
            """
            SELECT 1
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'config_server_settings'
              AND COLUMN_NAME = %s
            LIMIT 1
            """,
            (column_name,),
        )
        if cursor.fetchone():
            continue

        try:
            cursor.execute(
                f"ALTER TABLE config_server_settings ADD COLUMN {column_name} {column_definition}"
            )
        except mysql.connector.Error as err:
            if err.errno == errorcode.ER_DUP_FIELDNAME:
                continue
            logging.error(
                "Falha ao criar coluna %s em config_server_settings: %s",
                column_name,
                err,
            )
            return False

    return True


def _ensure_thread_owner_only_columns(cursor) -> bool:
    """Ensure thread-author-only posting columns exist in ``config_server_settings``."""

    required_columns = {
        "thread_owner_only_enabled": "TINYINT(1) NOT NULL DEFAULT 0",
        "thread_owner_only_forum_channel_ids": "TEXT NULL",
    }

    for column_name, column_definition in required_columns.items():
        cursor.execute(
            """
            SELECT 1
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'config_server_settings'
              AND COLUMN_NAME = %s
            LIMIT 1
            """,
            (column_name,),
        )
        if cursor.fetchone():
            continue

        try:
            cursor.execute(
                f"ALTER TABLE config_server_settings ADD COLUMN {column_name} {column_definition}"
            )
        except mysql.connector.Error as err:
            if err.errno == errorcode.ER_DUP_FIELDNAME:
                continue
            logging.error(
                "Falha ao criar coluna %s em config_server_settings: %s",
                column_name,
                err,
            )
            return False

    return True


def _check_columns(cursor, table_name: str, column_names: tuple[str, ...], domain: str) -> bool:
    """Check formal schema without mutating it at runtime."""

    for column_name in column_names:
        cursor.execute(
            """
            SELECT 1
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND COLUMN_NAME = %s
            LIMIT 1
            """,
            (table_name, column_name),
        )
        if cursor.fetchone():
            continue
        logging.error(
            "Schema %s incompleto: coluna %s.%s ausente. Aplique a migration oficial.",
            domain,
            table_name,
            column_name,
        )
        return False
    return True


def _ensure_vip_role_division_columns(cursor) -> bool:
    return _check_columns(
        cursor,
        "config_server_settings",
        ("vip_role_division_start_id", "vip_role_division_end_id"),
        "VIP",
    )


def _ensure_vip_roles_column(cursor) -> bool:
    return _check_columns(cursor, "config_server_settings", ("vip_roles",), "VIP")


def _ensure_vip_custom_role_prefix_column(cursor) -> bool:
    return _check_columns(
        cursor,
        "config_server_settings",
        ("vip_custom_role_prefix",),
        "VIP",
    )


def _ensure_vip_allow_staff_colors_column(cursor) -> bool:
    return _check_columns(
        cursor,
        "config_server_settings",
        ("vip_allow_staff_colors",),
        "VIP",
    )


def _ensure_user_custom_roles_columns(cursor) -> bool:
    return _check_columns(
        cursor,
        "user_custom_roles",
        ("color2", "role_id", "owner_discord_user_id"),
        "VIP",
    )


def _ensure_bump_columns(cursor) -> bool:
    """Ensure bump warning/reward columns exist in ``config_server_settings``."""

    required_columns = {
        "bump_warn_enabled": "TINYINT(1) NOT NULL DEFAULT 0",
        "bump_warn_disboard_channel_id": "BIGINT NULL",
        "bump_warn_target_channel_id": "BIGINT NULL",
        "bump_warn_messages": "TEXT NULL",
        "bump_warn_next_at": "DATETIME NULL",
        "bump_warn_last_bump_at": "DATETIME NULL",
        "bump_reward_enabled": "TINYINT(1) NOT NULL DEFAULT 0",
        "bump_reward_temp_role_id": "BIGINT NULL",
        "bump_reward_role_minutes": "INT NOT NULL DEFAULT 0",
        "bump_reward_coins": "INT NOT NULL DEFAULT 0",
        "bump_reward_message": "TEXT NULL",
        "bump_monthly_enabled": "TINYINT(1) NOT NULL DEFAULT 0",
        "bump_monthly_disboard_channel_id": "BIGINT NULL",
        "bump_monthly_reward_role_id": "BIGINT NULL",
        "bump_monthly_reward_days_1": "INT NOT NULL DEFAULT 21",
        "bump_monthly_reward_days_2": "INT NOT NULL DEFAULT 14",
        "bump_monthly_reward_days_3": "INT NOT NULL DEFAULT 7",
        "bump_monthly_reward_coins_1": "INT NOT NULL DEFAULT 0",
        "bump_monthly_reward_coins_2": "INT NOT NULL DEFAULT 0",
        "bump_monthly_reward_coins_3": "INT NOT NULL DEFAULT 0",
    }

    for column_name, column_definition in required_columns.items():
        cursor.execute(
            """
            SELECT 1
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'config_server_settings'
              AND COLUMN_NAME = %s
            LIMIT 1
            """,
            (column_name,),
        )
        if cursor.fetchone():
            continue

        try:
            cursor.execute(
                f"ALTER TABLE config_server_settings ADD COLUMN {column_name} {column_definition}"
            )
        except mysql.connector.Error as err:
            if err.errno == errorcode.ER_DUP_FIELDNAME:
                continue
            logging.error(
                "Falha ao criar coluna %s em config_server_settings: %s",
                column_name,
                err,
            )
            return False

    return True


def isTrendingPresenceEnabled(guild_id: int) -> bool:
    """Return whether trending presence tracking is enabled for a guild."""

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )

        cursor.execute(
            """
            SELECT trending_presence_enabled
            FROM config_server_settings
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )
        row = cursor.fetchone() or {}

    value = row.get("trending_presence_enabled")
    if value is None:
        return False
    return bool(int(value))


def getTrendingPresenceEnabledBatch(guild_ids: list[int]) -> dict[int, bool]:
    """Return trending presence enabled settings for many guilds at once."""

    if not guild_ids:
        return {}

    unique_guild_ids = list(dict.fromkeys(guild_ids))
    placeholders = ", ".join(["%s"] * len(unique_guild_ids))

    with pooled_connection() as cursor:
        cursor.executemany(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            [(guild_id,) for guild_id in unique_guild_ids],
        )

        cursor.execute(
            f"""
            SELECT server_guild_id, trending_presence_enabled
            FROM config_server_settings
            WHERE server_guild_id IN ({placeholders})
            """,
            tuple(unique_guild_ids),
        )
        rows = cursor.fetchall() or []

    enabled_by_guild = {
        int(row["server_guild_id"]): bool(int(row.get("trending_presence_enabled") or 0))
        for row in rows
    }
    for guild_id in unique_guild_ids:
        enabled_by_guild.setdefault(guild_id, False)
    return enabled_by_guild


def getBumpConfig(guild_id: int) -> dict:
    """Return bump warning/reward settings for a guild."""

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_bump_columns(cursor):
            return {}

        cursor.execute(
            """
            SELECT
                bump_warn_enabled,
                bump_warn_disboard_channel_id,
                bump_warn_target_channel_id,
                bump_warn_messages,
                bump_warn_next_at,
                bump_warn_last_bump_at,
                bump_reward_enabled,
                bump_reward_temp_role_id,
                bump_reward_role_minutes,
                bump_reward_coins,
                bump_reward_message,
                bump_monthly_enabled,
                bump_monthly_disboard_channel_id,
                bump_monthly_reward_role_id,
                bump_monthly_reward_days_1,
                bump_monthly_reward_days_2,
                bump_monthly_reward_days_3,
                bump_monthly_reward_coins_1,
                bump_monthly_reward_coins_2,
                bump_monthly_reward_coins_3
            FROM config_server_settings
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )
        row = cursor.fetchone() or {}

    raw_messages = row.get("bump_warn_messages")
    messages: list[str] = []
    if isinstance(raw_messages, str) and raw_messages.strip():
        try:
            loaded = json.loads(raw_messages)
            if isinstance(loaded, list):
                messages = [str(item).strip() for item in loaded if str(item).strip()]
        except json.JSONDecodeError:
            messages = [part.strip() for part in raw_messages.split("||") if part.strip()]

    economy_reward_config = get_bump_reward_economy_config(guild_id)
    legacy_reward_coins = max(0, int(row.get("bump_reward_coins") or 0))
    if not economy_reward_config.get("initialized") and legacy_reward_coins > 0:
        economy_reward_config = {"enabled": True, "points": legacy_reward_coins}

    return {
        "warnEnabled": bool(row.get("bump_warn_enabled")),
        "warnDisboardChannelId": int(row["bump_warn_disboard_channel_id"]) if row.get("bump_warn_disboard_channel_id") else None,
        "warnTargetChannelId": int(row["bump_warn_target_channel_id"]) if row.get("bump_warn_target_channel_id") else None,
        "warnMessages": messages,
        "warnNextAt": row.get("bump_warn_next_at"),
        "warnLastBumpAt": row.get("bump_warn_last_bump_at"),
        "rewardEnabled": bool(row.get("bump_reward_enabled")),
        "rewardTempRoleId": int(row["bump_reward_temp_role_id"]) if row.get("bump_reward_temp_role_id") else None,
        "rewardRoleMinutes": max(0, int(row.get("bump_reward_role_minutes") or 0)),
        "rewardMessage": (row.get("bump_reward_message") or "").strip() or None,
        "rewardCoinsEnabled": bool(economy_reward_config.get("enabled")),
        "rewardCoins": int(economy_reward_config.get("points") or 0),
        "monthlyEnabled": bool(row.get("bump_monthly_enabled")),
        "monthlyDisboardChannelId": int(row["bump_monthly_disboard_channel_id"]) if row.get("bump_monthly_disboard_channel_id") else None,
        "monthlyRewardRoleId": int(row["bump_monthly_reward_role_id"]) if row.get("bump_monthly_reward_role_id") else None,
        "monthlyRewardDays": [
            max(0, int(row.get("bump_monthly_reward_days_1") or 0)),
            max(0, int(row.get("bump_monthly_reward_days_2") or 0)),
            max(0, int(row.get("bump_monthly_reward_days_3") or 0)),
        ],
        "monthlyRewardCoins": [
            max(0, int(row.get("bump_monthly_reward_coins_1") or 0)),
            max(0, int(row.get("bump_monthly_reward_coins_2") or 0)),
            max(0, int(row.get("bump_monthly_reward_coins_3") or 0)),
        ],
    }


def setBumpWarningConfig(
    guild_id: int,
    *,
    enabled: bool | None = None,
    disboard_channel_id: int | None = None,
    target_channel_id: int | None = None,
    messages: list[str] | None = None,
) -> bool:
    updates: dict[str, Any] = {}
    if enabled is not None:
        updates["bump_warn_enabled"] = bool(enabled)
    if disboard_channel_id is not None:
        updates["bump_warn_disboard_channel_id"] = int(disboard_channel_id) if disboard_channel_id else None
    if target_channel_id is not None:
        updates["bump_warn_target_channel_id"] = int(target_channel_id) if target_channel_id else None
    if messages is not None:
        clean_messages = [str(message).strip() for message in messages if str(message).strip()]
        updates["bump_warn_messages"] = json.dumps(clean_messages, ensure_ascii=False)

    if not updates:
        return False

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_bump_columns(cursor):
            return False

    return updateServerConfig(guild_id, **updates)


def setBumpWarningSchedule(guild_id: int, *, next_at: datetime | None, last_bump_at: datetime | None = None) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_bump_columns(cursor):
            return False

    updates: dict[str, Any] = {"bump_warn_next_at": next_at}
    if last_bump_at is not None:
        updates["bump_warn_last_bump_at"] = last_bump_at
    return updateServerConfig(guild_id, **updates)


def listBumpWarningConfigs() -> list[dict]:
    """Return bump warning configs that are currently enabled."""

    with pooled_connection() as cursor:
        if not _ensure_bump_columns(cursor):
            return []
        cursor.execute(
            """
            SELECT
                server_guild_id,
                bump_warn_disboard_channel_id,
                bump_warn_target_channel_id,
                bump_warn_messages,
                bump_warn_next_at
            FROM config_server_settings
            WHERE bump_warn_enabled = 1
            """
        )
        rows = cursor.fetchall() or []

    parsed_rows: list[dict] = []
    for row in rows:
        raw_messages = row.get("bump_warn_messages")
        messages: list[str] = []
        if isinstance(raw_messages, str) and raw_messages.strip():
            try:
                loaded = json.loads(raw_messages)
                if isinstance(loaded, list):
                    messages = [str(item).strip() for item in loaded if str(item).strip()]
            except json.JSONDecodeError:
                messages = [part.strip() for part in raw_messages.split("||") if part.strip()]

        parsed_rows.append(
            {
                "guildId": int(row["server_guild_id"]),
                "disboardChannelId": int(row["bump_warn_disboard_channel_id"]) if row.get("bump_warn_disboard_channel_id") else None,
                "targetChannelId": int(row["bump_warn_target_channel_id"]) if row.get("bump_warn_target_channel_id") else None,
                "messages": messages,
                "nextAt": row.get("bump_warn_next_at"),
            }
        )

    return parsed_rows


def setBumpRewardConfig(
    guild_id: int,
    *,
    enabled: bool | None = None,
    temp_role_id: int | None = None,
    role_minutes: int | None = None,
    message: str | None = None,
) -> bool:
    updates: dict[str, Any] = {}
    if enabled is not None:
        updates["bump_reward_enabled"] = bool(enabled)
    if temp_role_id is not None:
        updates["bump_reward_temp_role_id"] = int(temp_role_id) if temp_role_id else None
    if role_minutes is not None:
        updates["bump_reward_role_minutes"] = max(0, int(role_minutes))
    if message is not None:
        updates["bump_reward_message"] = message.strip() or None

    if not updates:
        return False

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_bump_columns(cursor):
            return False

    return updateServerConfig(guild_id, **updates)



def setBumpMonthlyRewardConfig(
    guild_id: int,
    *,
    enabled: bool | None = None,
    disboard_channel_id: int | None = None,
    reward_role_id: int | None = None,
    reward_days: list[int] | None = None,
    reward_coins: list[int] | None = None,
) -> bool:
    updates: dict[str, Any] = {}
    if enabled is not None:
        updates["bump_monthly_enabled"] = bool(enabled)
    if disboard_channel_id is not None:
        updates["bump_monthly_disboard_channel_id"] = int(disboard_channel_id) if disboard_channel_id else None
    if reward_role_id is not None:
        updates["bump_monthly_reward_role_id"] = int(reward_role_id) if reward_role_id else None

    if reward_days is not None:
        normalized_days = [max(0, int(value)) for value in reward_days[:3]]
        while len(normalized_days) < 3:
            normalized_days.append(0)
        updates["bump_monthly_reward_days_1"] = normalized_days[0]
        updates["bump_monthly_reward_days_2"] = normalized_days[1]
        updates["bump_monthly_reward_days_3"] = normalized_days[2]

    if reward_coins is not None:
        normalized_coins = [max(0, int(value)) for value in reward_coins[:3]]
        while len(normalized_coins) < 3:
            normalized_coins.append(0)
        updates["bump_monthly_reward_coins_1"] = normalized_coins[0]
        updates["bump_monthly_reward_coins_2"] = normalized_coins[1]
        updates["bump_monthly_reward_coins_3"] = normalized_coins[2]

    if not updates:
        return False

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_bump_columns(cursor):
            return False

    return updateServerConfig(guild_id, **updates)


def listBumpMonthlyRewardConfigs() -> list[dict]:
    with pooled_connection() as cursor:
        if not _ensure_bump_columns(cursor):
            return []

        cursor.execute(
            """
            SELECT
                server_guild_id,
                bump_monthly_disboard_channel_id,
                bump_monthly_reward_role_id,
                bump_monthly_reward_days_1,
                bump_monthly_reward_days_2,
                bump_monthly_reward_days_3,
                bump_monthly_reward_coins_1,
                bump_monthly_reward_coins_2,
                bump_monthly_reward_coins_3
            FROM config_server_settings
            WHERE bump_monthly_enabled = 1
            """
        )
        rows = cursor.fetchall() or []

    parsed: list[dict] = []
    for row in rows:
        parsed.append(
            {
                "guildId": int(row["server_guild_id"]),
                "disboardChannelId": int(row["bump_monthly_disboard_channel_id"]) if row.get("bump_monthly_disboard_channel_id") else None,
                "rewardRoleId": int(row["bump_monthly_reward_role_id"]) if row.get("bump_monthly_reward_role_id") else None,
                "rewardDays": [
                    max(0, int(row.get("bump_monthly_reward_days_1") or 0)),
                    max(0, int(row.get("bump_monthly_reward_days_2") or 0)),
                    max(0, int(row.get("bump_monthly_reward_days_3") or 0)),
                ],
                "rewardCoins": [
                    max(0, int(row.get("bump_monthly_reward_coins_1") or 0)),
                    max(0, int(row.get("bump_monthly_reward_coins_2") or 0)),
                    max(0, int(row.get("bump_monthly_reward_coins_3") or 0)),
                ],
            }
        )

    return parsed

def getVipRoleDivisionConfig(guild_id: int) -> dict:
    with pooled_connection() as cursor:
        if not _ensure_vip_role_division_columns(cursor):
            return {
                "startRoleId": None,
                "endRoleId": None,
            }

        cursor.execute(
            """
            SELECT vip_role_division_start_id, vip_role_division_end_id
            FROM config_server_settings
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )
        row = cursor.fetchone() or {}
        return {
            "startRoleId": int(row["vip_role_division_start_id"]) if row.get("vip_role_division_start_id") else None,
            "endRoleId": int(row["vip_role_division_end_id"]) if row.get("vip_role_division_end_id") else None,
        }


def setVipRoleDivisionConfig(
    guild_id: int,
    start_role_id: int | None,
    end_role_id: int | None = None,
) -> bool:
    if end_role_id is not None and start_role_id is None:
        return False

    with pooled_connection() as cursor:
        if not _ensure_vip_role_division_columns(cursor):
            return False

    return updateServerConfig(
        guild_id,
        vip_role_division_start_id=int(start_role_id) if start_role_id else None,
        vip_role_division_end_id=int(end_role_id) if end_role_id else None,
    )


def _parse_vip_role_ids(raw_roles) -> list[int]:
    if not raw_roles:
        return []

    if isinstance(raw_roles, str):
        role_values = [value.strip() for value in raw_roles.split(",") if value.strip()]
    elif isinstance(raw_roles, (list, tuple, set)):
        role_values = [str(value).strip() for value in raw_roles if str(value).strip()]
    else:
        role_values = [str(raw_roles).strip()]

    parsed_roles: list[int] = []
    for role_value in role_values:
        try:
            parsed_roles.append(int(role_value))
        except (TypeError, ValueError):
            continue
    return parsed_roles


def getVipRolesConfig(guild_id: int) -> list[int]:
    with pooled_connection() as cursor:
        if not _ensure_vip_roles_column(cursor):
            return []

        cursor.execute(
            "SELECT vip_roles FROM config_server_settings WHERE server_guild_id = %s",
            (guild_id,),
        )
        row = cursor.fetchone() or {}
        return _parse_vip_role_ids(row.get("vip_roles"))


async def async_getGuildVipRoleIds(guild_id: int) -> list[int]:
    rows = await async_fetchall(
        "SELECT vip_roles FROM config_server_settings WHERE server_guild_id = %s",
        (guild_id,),
    )
    row = rows[0] if rows else {}
    return _parse_vip_role_ids(row.get("vip_roles"))


def getGuildVipRoleIds(guild_id: int) -> list[int]:
    """Return VIP role IDs for a guild.

    Resolution priority:
    1) ``config_server_settings.vip_roles``
    """

    configured_roles = getVipRolesConfig(guild_id)
    return configured_roles or []


def setVipRolesConfig(guild_id: int, role_ids: list[int]) -> bool:
    normalized_roles: list[int] = []
    for role_id in role_ids:
        try:
            parsed_role_id = int(role_id)
        except (TypeError, ValueError):
            continue
        if parsed_role_id > 0 and parsed_role_id not in normalized_roles:
            normalized_roles.append(parsed_role_id)

    with pooled_connection() as cursor:
        if not _ensure_vip_roles_column(cursor):
            return False

    return updateServerConfig(
        guild_id,
        vip_roles=",".join(str(role_id) for role_id in normalized_roles) or None,
    )


def getVipCustomRolePrefixConfig(guild_id: int) -> str | None:
    with pooled_connection() as cursor:
        if not _ensure_vip_custom_role_prefix_column(cursor):
            return None

        cursor.execute(
            "SELECT vip_custom_role_prefix FROM config_server_settings WHERE server_guild_id = %s",
            (guild_id,),
        )
        row = cursor.fetchone() or {}
        prefix = row.get("vip_custom_role_prefix")
        if not isinstance(prefix, str):
            return None

        normalized = prefix.strip()
        return normalized if normalized else None


def setVipCustomRolePrefixConfig(guild_id: int, prefix: str | None) -> bool:
    normalized_prefix = None
    if isinstance(prefix, str):
        normalized_prefix = prefix.strip() or None

    if normalized_prefix is not None and len(normalized_prefix) > 100:
        return False

    with pooled_connection() as cursor:
        if not _ensure_vip_custom_role_prefix_column(cursor):
            return False

    return updateServerConfig(
        guild_id,
        vip_custom_role_prefix=normalized_prefix,
    )


def getVipAllowStaffColorsConfig(guild_id: int) -> bool:
    with pooled_connection() as cursor:
        if not _ensure_vip_allow_staff_colors_column(cursor):
            return False
        cursor.execute(
            "SELECT vip_allow_staff_colors FROM config_server_settings WHERE server_guild_id = %s",
            (guild_id,),
        )
        row = cursor.fetchone() or {}
        return bool(row.get("vip_allow_staff_colors"))


def setVipAllowStaffColorsConfig(guild_id: int, enabled: bool) -> bool:
    with pooled_connection() as cursor:
        if not _ensure_vip_allow_staff_colors_column(cursor):
            return False
    return updateServerConfig(guild_id, vip_allow_staff_colors=bool(enabled))


def getStaffRoles(guild_id:int):
    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_staff_roles_column(cursor):
            return []
        cursor.execute(
            "SELECT staff_roles FROM config_server_settings WHERE server_guild_id = %s",
            (guild_id,),
        )
        row = cursor.fetchone() or {}
        raw_roles = row.get("staff_roles")

        if not raw_roles:
            return []

        if isinstance(raw_roles, str):
            roles = [role_id.strip() for role_id in raw_roles.split(",") if role_id.strip()]
        elif isinstance(raw_roles, (list, tuple, set)):
            roles = [str(role_id).strip() for role_id in raw_roles if str(role_id).strip()]
        else:
            roles = [str(raw_roles).strip()]

        valid_roles: list[int] = []
        for role_id in roles:
            try:
                valid_roles.append(int(role_id))
            except (TypeError, ValueError):
                continue
        return valid_roles


def getAutoJoinRolesConfig(guild_id: int) -> dict:
    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_auto_join_roles_columns(cursor):
            return {"enabled": False, "roleIds": []}

        cursor.execute(
            """
            SELECT auto_join_roles_enabled, auto_join_role_ids
            FROM config_server_settings
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )
        row = cursor.fetchone() or {}
        raw_roles = row.get("auto_join_role_ids")

        if not raw_roles:
            return {"enabled": bool(row.get("auto_join_roles_enabled")), "roleIds": []}

        if isinstance(raw_roles, str):
            roles = [role_id.strip() for role_id in raw_roles.split(",") if role_id.strip()]
        elif isinstance(raw_roles, (list, tuple, set)):
            roles = [str(role_id).strip() for role_id in raw_roles if str(role_id).strip()]
        else:
            roles = [str(raw_roles).strip()]

        valid_roles: list[int] = []
        for role_id in roles:
            try:
                valid_roles.append(int(role_id))
            except (TypeError, ValueError):
                continue

        return {
            "enabled": bool(row.get("auto_join_roles_enabled")),
            "roleIds": sorted(set(valid_roles)),
        }


def setAutoJoinRolesConfig(
    guild_id: int,
    *,
    enabled: bool | None = None,
    role_ids: list[int] | None = None,
) -> bool:
    updates: dict[str, Any] = {}
    if enabled is not None:
        updates["auto_join_roles_enabled"] = bool(enabled)
    if role_ids is not None:
        sanitized_ids = sorted({int(role_id) for role_id in role_ids if role_id})
        updates["auto_join_role_ids"] = ",".join(str(role_id) for role_id in sanitized_ids)

    if not updates:
        return False

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_auto_join_roles_columns(cursor):
            return False

    return updateServerConfig(guild_id, **updates)


def getBirthdayMessageChannelId(guild_id: int) -> Optional[int]:
    """Return the configured birthday message channel id for a guild."""

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_birthday_channel_column(cursor):
            return None

        cursor.execute(
            "SELECT birthday_channel_id FROM config_server_settings WHERE server_guild_id = %s",
            (guild_id,),
        )
        row = cursor.fetchone() or {}
        channel_id = row.get("birthday_channel_id")
        return int(channel_id) if channel_id else None


def getGuildBirthdayMessageChannelId(guild_id: int, fallback_channel_id: int | None = None) -> Optional[int]:
    """Return birthday channel for a guild.

    Resolution priority:
    1) ``config_server_settings.birthday_channel_id``
    2) ``fallback_channel_id`` (typically environment/default constant)
    """

    configured_channel_id = getBirthdayMessageChannelId(guild_id)
    if configured_channel_id:
        return configured_channel_id
    return int(fallback_channel_id) if fallback_channel_id else None


def setBirthdayMessageChannelId(guild_id: int, channel_id: int) -> bool:
    """Persist the birthday message destination channel for a guild."""

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_birthday_channel_column(cursor):
            return False

    return updateServerConfig(guild_id, birthday_channel_id=int(channel_id))


def _get_config_server_settings_values(
    guild_id: int,
    *,
    columns: Sequence[str],
) -> dict:
    """Fetch selected ``config_server_settings`` columns for a guild.

    Only columns that currently exist in the schema are queried. Returned keys
    are camelCased to match ``getConfig`` output shape.
    """

    if not columns:
        return {}

    with pooled_connection() as cursor:
        placeholders = ", ".join(["%s"] * len(columns))
        cursor.execute(
            f"""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'config_server_settings'
              AND COLUMN_NAME IN ({placeholders})
            """,
            tuple(columns),
        )
        available_columns = [row["COLUMN_NAME"] for row in cursor.fetchall()]

        if not available_columns:
            return {}

        selected_columns = ", ".join(available_columns)
        cursor.execute(
            f"""
            SELECT {selected_columns}
            FROM config_server_settings
            WHERE server_guild_id = %s
            """,
            (int(guild_id),),
        )
        row = cursor.fetchone() or {}

    return {snake_to_camel(key): value for key, value in row.items()}


def getGuildMemberNotVerifiedRoleId(guild_id: int) -> int | None:
    """Return the unverified-member role for a guild.

    Resolution priority:
    1) ``config_server_settings.member_not_verified_role`` / ``member_not_verified_role_id``
    2) ``portaria_base_config.visitante_role_id``
    """

    guild_config = _get_config_server_settings_values(
        int(guild_id),
        columns=("member_not_verified_role", "member_not_verified_role_id"),
    )

    config_keys = (
        "memberNotVerifiedRoleId",
        "memberNotVerifiedRole",
        "visitorRoleId",
    )
    for key in config_keys:
        value = guild_config.get(key)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue

    portaria_config = get_portaria_base_config(guild_id)
    visitante_role_id = portaria_config.get("visitante_role_id")
    if visitante_role_id:
        try:
            return int(visitante_role_id)
        except (TypeError, ValueError):
            pass

    return None


def getGuildStaffColors(guild_id: int) -> list[str]:
    """Return reserved staff colors for a guild.

    Resolution priority:
    1) ``config_server_settings.staff_colors`` (if present in schema)
    """

    guild_config = _get_config_server_settings_values(
        int(guild_id),
        columns=("staff_colors",),
    )

    raw_colors = guild_config.get("staffColors")
    if isinstance(raw_colors, str):
        parsed_colors = [color.strip() for color in raw_colors.split(",") if color.strip()]
        if parsed_colors:
            return parsed_colors
    elif isinstance(raw_colors, (list, tuple, set)):
        parsed_colors = [str(color).strip() for color in raw_colors if str(color).strip()]
        if parsed_colors:
            return parsed_colors

    return []


def setGuildStaffColors(guild_id: int, colors: list[str]) -> bool:
    normalized: list[str] = []
    for color in colors:
        value = str(color or "").strip().lower()
        if not value:
            continue
        if not value.startswith("#"):
            value = f"#{value}"
        if re.fullmatch(r"#[0-9a-f]{6}", value) and value not in normalized:
            normalized.append(value)

    return updateServerConfig(
        guild_id,
        staff_colors=",".join(normalized) if normalized else None,
    )


def getGuildAgeRoleIds(
    guild_id: int,
    *,
    fallback_adult_role_id: int | None = None,
    fallback_minor_role_id: int | None = None,
) -> dict[str, int | None]:
    """Return age-based role IDs for a guild.

    Resolution priority:
    1) ``portaria_base_config.maior_18_role_id`` / ``menor_18_role_id``
    2) Explicit fallback IDs (typically environment/default constants)
    """

    portaria_config = get_portaria_base_config(guild_id)

    adult_role_id = portaria_config.get("maior_18_role_id")
    minor_role_id = portaria_config.get("menor_18_role_id")

    try:
        adult_role_id = int(adult_role_id) if adult_role_id else None
    except (TypeError, ValueError):
        adult_role_id = None

    try:
        minor_role_id = int(minor_role_id) if minor_role_id else None
    except (TypeError, ValueError):
        minor_role_id = None

    if adult_role_id is None and fallback_adult_role_id:
        adult_role_id = int(fallback_adult_role_id)
    if minor_role_id is None and fallback_minor_role_id:
        minor_role_id = int(fallback_minor_role_id)

    return {
        "adultRoleId": adult_role_id,
        "minorRoleId": minor_role_id,
    }


def getCollaborativeModerationConfig(guild_id: int) -> dict:
    """Return collaborative moderation settings for a guild."""

    with pooled_connection() as cursor:
        if not _ensure_collaborative_moderation_columns(cursor):
            return {"enabled": False, "emoji": None, "minReactions": 3}

        cursor.execute(
            """
            SELECT
                collab_moderation_enabled,
                collab_moderation_emoji,
                collab_moderation_min_reactions
            FROM config_server_settings
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )
        row = cursor.fetchone() or {}
        min_reactions = max(1, int(row.get("collab_moderation_min_reactions") or 3))
        emoji = row.get("collab_moderation_emoji")
        return {
            "enabled": bool(row.get("collab_moderation_enabled")),
            "emoji": emoji if isinstance(emoji, str) and emoji.strip() else None,
            "minReactions": min_reactions,
        }

def getHashtagRoleMentionsConfig(guild_id: int) -> dict:
    """Return hashtag-role mention automation settings for a guild."""

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_hashtag_role_mentions_columns(cursor):
            return {
                "enabled": False,
                "channelIds": [],
                "authorRoleId": None,
                "hashtagMap": {},
            }

        cursor.execute(
            """
            SELECT
                hashtag_role_mentions_enabled,
                hashtag_role_mentions_channel_ids,
                hashtag_role_mentions_author_role_id,
                hashtag_role_mentions_map
            FROM config_server_settings
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )
        row = cursor.fetchone() or {}
        raw_map = row.get("hashtag_role_mentions_map")
        raw_channel_ids = row.get("hashtag_role_mentions_channel_ids")

        channel_ids: list[int] = []
        if isinstance(raw_channel_ids, str) and raw_channel_ids.strip():
            for value in raw_channel_ids.split(","):
                parsed = str(value).strip()
                if not parsed:
                    continue
                try:
                    channel_ids.append(int(parsed))
                except (TypeError, ValueError):
                    continue

        parsed_map: dict[str, int] = {}
        if isinstance(raw_map, str) and raw_map.strip():
            try:
                loaded_map = json.loads(raw_map)
                if isinstance(loaded_map, dict):
                    for key, value in loaded_map.items():
                        token = str(key).strip()
                        if not token:
                            continue
                        try:
                            parsed_map[token] = int(value)
                        except (TypeError, ValueError):
                            continue
            except json.JSONDecodeError:
                parsed_map = {}

        return {
            "enabled": bool(row.get("hashtag_role_mentions_enabled")),
            "channelIds": sorted(set(channel_ids)),
            "authorRoleId": int(row["hashtag_role_mentions_author_role_id"]) if row.get("hashtag_role_mentions_author_role_id") else None,
            "hashtagMap": parsed_map,
        }


def setHashtagRoleMentionsConfig(
    guild_id: int,
    *,
    enabled: bool | None = None,
    channel_ids: list[int] | None = None,
    author_role_id: int | None | object = None,
    hashtag_map: dict[str, int] | None = None,
) -> bool:
    """Persist hashtag-role mention automation settings for a guild."""

    updates: dict[str, Any] = {}
    if enabled is not None:
        updates["hashtag_role_mentions_enabled"] = bool(enabled)

    if channel_ids is not None:
        sanitized_channels = sorted({int(channel_id) for channel_id in channel_ids if channel_id})
        updates["hashtag_role_mentions_channel_ids"] = ",".join(str(channel_id) for channel_id in sanitized_channels)

    if author_role_id is not None:
        updates["hashtag_role_mentions_author_role_id"] = int(author_role_id) if author_role_id else None

    if hashtag_map is not None:
        normalized_map: dict[str, int] = {}
        for hashtag, role_id in hashtag_map.items():
            normalized_tag = str(hashtag).strip()
            if not normalized_tag:
                continue
            if not normalized_tag.startswith("#"):
                normalized_tag = f"#{normalized_tag}"
            try:
                normalized_map[normalized_tag] = int(role_id)
            except (TypeError, ValueError):
                continue
        updates["hashtag_role_mentions_map"] = json.dumps(normalized_map, ensure_ascii=False)

    if not updates:
        return False

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_hashtag_role_mentions_columns(cursor):
            return False

    return updateServerConfig(guild_id, **updates)


def getThreadOwnerOnlyPostingConfig(guild_id: int) -> dict:
    """Return per-guild thread posting restriction settings."""

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_thread_owner_only_columns(cursor):
            return {"enabled": False, "forumChannelIds": []}

        cursor.execute(
            """
            SELECT
                thread_owner_only_enabled,
                thread_owner_only_forum_channel_ids
            FROM config_server_settings
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )
        row = cursor.fetchone() or {}
        raw_channel_ids = row.get("thread_owner_only_forum_channel_ids")
        channel_ids: list[int] = []

        if isinstance(raw_channel_ids, str) and raw_channel_ids.strip():
            for value in raw_channel_ids.split(","):
                parsed = str(value).strip()
                if not parsed:
                    continue
                try:
                    channel_ids.append(int(parsed))
                except (TypeError, ValueError):
                    continue

        return {
            "enabled": bool(row.get("thread_owner_only_enabled")),
            "forumChannelIds": sorted(set(channel_ids)),
        }


def setThreadOwnerOnlyPostingConfig(
    guild_id: int,
    *,
    enabled: bool | None = None,
    forum_channel_ids: list[int] | None = None,
) -> bool:
    """Persist thread posting restriction settings for a guild."""

    updates: dict[str, Any] = {}
    if enabled is not None:
        updates["thread_owner_only_enabled"] = bool(enabled)

    if forum_channel_ids is not None:
        sanitized_channels = sorted({int(channel_id) for channel_id in forum_channel_ids if channel_id})
        updates["thread_owner_only_forum_channel_ids"] = ",".join(str(channel_id) for channel_id in sanitized_channels)

    if not updates:
        return False

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO config_server_settings (server_guild_id) VALUES (%s)",
            (guild_id,),
        )
        if not _ensure_thread_owner_only_columns(cursor):
            return False

    return updateServerConfig(guild_id, **updates)


def getArtMentionsConfig(guild_id: int) -> dict:
    """Backward-compatible alias for the legacy art mentions config name."""

    config = getHashtagRoleMentionsConfig(guild_id)
    return {
        "enabled": config.get("enabled", False),
        "parentChannelId": (config.get("channelIds") or [None])[0],
        "requiredRoleId": config.get("authorRoleId"),
        "mentionMap": config.get("hashtagMap", {}),
    }


def setCollaborativeModerationConfig(
    guild_id: int,
    *,
    enabled: bool | None = None,
    emoji: str | None = None,
    min_reactions: int | None = None,
) -> bool:
    """Deprecated compatibility hook.

    Collaborative-moderation configuration belongs to the API control plane.
    The Discord runtime only consumes the stored rule.
    """

    logging.warning(
        "Ignored deprecated runtime collaborative-moderation write for guild=%s",
        guild_id,
    )
    return False

def setStaffRoles(guild_id: int, role_ids: list[int]) -> bool:
    """Deprecated compatibility hook.

    Staff-role configuration belongs to the API control plane. The Discord
    runtime must not mutate this administrative rule.
    """

    logging.warning(
        "Ignored deprecated runtime staff-role write for guild=%s",
        guild_id,
    )
    return False

def getUserBirthday(guild_id: int, discord_user: discord.Member) -> Optional[date]:
    """Retrieve the stored birthday for a user, if any."""

    user_id = includeUser(discord_user, guild_id)

    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT birth_date FROM user_birthday WHERE user_id = %s",
            (user_id,),
        )
        row = cursor.fetchone()

    if not row:
        return None

    stored_birthday = row["birth_date"]
    if isinstance(stored_birthday, datetime):
        return stored_birthday.date()
    return stored_birthday


def registerUser(
    guild_id: int,
    discord_user: discord.Member,
    birthday: date | None = None,
    approved_date: date = None,
    birthday_action: Literal["keep", "replace"] | None = None,
) -> bool:
    """Register a member in the database and save the birthday.

    A flag "registered" em ``user_birthday`` continua sendo utilizada apenas
    para identificar quando o próprio membro foi ao bot confirmar o
    cadastramento após a aprovação. A existência do registro por si só já
    garante que o aniversário está salvo na base, então evitamos marcá-lo
    automaticamente durante resoluções de conflito executadas pelo staff.
    """
    inferred_approved_date = approved_date
    if inferred_approved_date is None:
        joined_at = getattr(discord_user, "joined_at", None)
        inferred_approved_date = joined_at if joined_at is not None else datetime.now()

    user_id = includeUser(
        discord_user,
        guild_id,
        inferred_approved_date,
    )

    if user_id is None:
        raise RuntimeError("Não foi possível registrar o usuário")

    if birthday is not None:
        existing_birthday = None
        existing_row = None
        with pooled_connection() as cursor:
            cursor.execute(
                "SELECT birth_date, registered FROM user_birthday WHERE user_id = %s",
                (user_id,),
            )
            existing_row = cursor.fetchone()

        if existing_row:
            existing_birthday = existing_row["birth_date"]
            if isinstance(existing_birthday, datetime):
                existing_birthday = existing_birthday.date()

        if birthday_action == "keep" and existing_row:
            # O fluxo de "keep" apenas sinaliza que o conflito foi resolvido pelo staff;
            # mantemos o valor de "registered" como indicador de que o membro registrou
            # ativamente o aniversário após a aprovação.
            birthdayRegistered = True
        elif birthday_action == "replace" and existing_row:
            try:
                with pooled_connection(True) as cursor:
                    cursor.execute(
                        """
                        UPDATE user_birthday
                        SET birth_date = %s,
                            post_informed_date = %s,
                            verified = 0,
                            `18_plus` = %s
                        WHERE user_id = %s
                        """,
                        (birthday, birthday, int(is_user_18_plus(birthday)), user_id),
                    )
            except Exception as e:
                raise RuntimeError("Erro ao atualizar aniversário") from e
            else:
                birthdayRegistered = True
        else:
            try:
                birthdayRegistered = includeBirthday(
                    guild_id,
                    birthday,
                    discord_user,
                    False,
                    user_id,
                    False,
                )
            except Exception as e:
                error_message = str(e)
                if (
                    error_message in ("Duplicate entry", "Changed Entry")
                    and existing_birthday is not None
                    and existing_birthday == birthday
                ) or error_message == "Changed Entry":
                    birthdayRegistered = True
                else:
                    raise RuntimeError("Erro ao registrar aniversário: " + error_message) from e

        if not birthdayRegistered:
            raise RuntimeError("Não foi possível registrar o aniversário")

    return True

def saveCustomRole(
    guild_id: int,
    discord_user: discord.Member,
    color: str | None = None,
    color2: str | None = None,
    iconId: int | None = None,
    roleId: int | None = None,
):
    if color is None and color2 is None and iconId is None and roleId is None:
        return False

    try:
        # Keep the platform identity mapping current, but user_custom_roles is
        # account-scoped: the Discord account is the row owner.
        includeUser(discord_user, guild_id)
        owner_discord_user_id = int(discord_user.id)

        with pooled_connection(True) as cursor:
            if not _ensure_user_custom_roles_columns(cursor):
                return False

            cursor.execute(
                """
                SELECT id
                FROM user_custom_roles
                WHERE server_guild_id = %s
                  AND owner_discord_user_id = %s
                LIMIT 1
                """,
                (guild_id, owner_discord_user_id),
            )
            existing = cursor.fetchone()

            updates: dict[str, object] = {}
            if color is not None:
                updates["color"] = str(color)
            if color2 is not None:
                updates["color2"] = str(color2)
            if iconId is not None:
                updates["icon_id"] = int(iconId)
            if roleId is not None:
                updates["role_id"] = int(roleId)

            if existing:
                set_clause = ", ".join(f"{column} = %s" for column in updates)
                cursor.execute(
                    f"""
                    UPDATE user_custom_roles
                    SET {set_clause}
                    WHERE id = %s
                    """,
                    tuple(updates.values()) + (existing["id"],),
                )
                return True

            cursor.execute(
                """
                INSERT INTO user_custom_roles (
                    server_guild_id,
                    owner_discord_user_id,
                    color,
                    color2,
                    icon_id,
                    role_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    guild_id,
                    owner_discord_user_id,
                    updates.get("color"),
                    updates.get("color2"),
                    updates.get("icon_id"),
                    updates.get("role_id"),
                ),
            )
            return True
    except Exception:
        return False


def getAllCustomRoles(guild_id:int):
    with pooled_connection() as cursor:
        if not _ensure_user_custom_roles_columns(cursor):
            return []
        cursor.execute(
            """
            SELECT id,
                   owner_discord_user_id,
                   color,
                   color2,
                   icon_id,
                   role_id
            FROM user_custom_roles
            WHERE server_guild_id = %s
            """,
            (guild_id,),
        )
        custom_roles: list[dict] = cursor.fetchall() or []
        return [
            CustomRole(
                int(role["owner_discord_user_id"]),
                role["color"],
                role["icon_id"],
                role.get("color2"),
                role.get("role_id"),
                rowId=role.get("id"),
                ownerDiscordUserId=int(role["owner_discord_user_id"]),
                linkedDiscordUserIds=[int(role["owner_discord_user_id"])],
            )
            for role in custom_roles
            if role.get("owner_discord_user_id") is not None
        ]


def getCustomRoleEntry(guild_id: int, discord_user_id: int) -> dict | None:
    with pooled_connection() as cursor:
        if not _ensure_user_custom_roles_columns(cursor):
            return None

        cursor.execute(
            """
            SELECT id,
                   owner_discord_user_id,
                   role_id,
                   color,
                   color2,
                   icon_id
            FROM user_custom_roles
            WHERE server_guild_id = %s
              AND owner_discord_user_id = %s
            LIMIT 1
            """,
            (guild_id, discord_user_id),
        )
        row = cursor.fetchone()
        if not row:
            return None
        entry = dict(row)
        entry["linked_discord_user_ids"] = [int(discord_user_id)]
        return entry


async def async_getCustomRoleOwnerDiscordIdsByRoleIds(
    guild_id: int,
    role_ids: set[int] | list[int] | tuple[int, ...],
) -> set[int]:
    normalized_role_ids = sorted({int(role_id) for role_id in role_ids if role_id})
    if not normalized_role_ids:
        return set()

    placeholders = ", ".join(["%s"] * len(normalized_role_ids))
    rows = await async_fetchall(
        f"""
        SELECT DISTINCT owner_discord_user_id AS discord_user_id
        FROM user_custom_roles
        WHERE server_guild_id = %s
          AND role_id IN ({placeholders})
          AND owner_discord_user_id IS NOT NULL
        """,
        (guild_id, *normalized_role_ids),
    )
    return {
        int(row["discord_user_id"])
        for row in rows
        if row.get("discord_user_id") is not None
    }


async def async_getVipCustomRolesForGuildIds(
    guild_ids: set[int] | list[int] | tuple[int, ...],
    *,
    chunk_size: int = 250,
) -> dict[int, list[CustomRole]]:
    """Load persisted account-scoped VIP custom roles for session recovery."""

    normalized_guild_ids = sorted({int(guild_id) for guild_id in guild_ids if guild_id})
    if not normalized_guild_ids:
        return {}

    grouped: dict[int, list[CustomRole]] = {}
    safe_chunk_size = max(1, int(chunk_size))

    for offset in range(0, len(normalized_guild_ids), safe_chunk_size):
        batch = normalized_guild_ids[offset:offset + safe_chunk_size]
        placeholders = ", ".join(["%s"] * len(batch))
        rows = await async_fetchall(
            f"""
            SELECT id,
                   server_guild_id,
                   owner_discord_user_id,
                   color,
                   color2,
                   icon_id,
                   role_id
            FROM user_custom_roles
            WHERE server_guild_id IN ({placeholders})
              AND role_id IS NOT NULL
              AND owner_discord_user_id IS NOT NULL
            """,
            tuple(batch),
        )
        for row in rows:
            guild_id = row.get("server_guild_id")
            owner_discord_user_id = row.get("owner_discord_user_id")
            if guild_id is None or owner_discord_user_id is None:
                continue

            owner_id = int(owner_discord_user_id)
            grouped.setdefault(int(guild_id), []).append(
                CustomRole(
                    owner_id,
                    row.get("color"),
                    row.get("icon_id"),
                    row.get("color2"),
                    row.get("role_id"),
                    rowId=row.get("id"),
                    ownerDiscordUserId=owner_id,
                    linkedDiscordUserIds=[owner_id],
                )
            )

    return grouped


async def async_claimCustomRoleOwner(row_id: int, discord_user_id: int) -> bool:
    """Compatibility helper for pre-contraction schemas; normally no-op."""

    affected = await async_execute(
        """
        UPDATE user_custom_roles
        SET owner_discord_user_id = %s
        WHERE id = %s
          AND owner_discord_user_id IS NULL
        """,
        (discord_user_id, row_id),
    )
    return affected > 0


async def async_clearCustomRoleRoleIdById(row_id: int) -> bool:
    affected = await async_execute(
        """
        UPDATE user_custom_roles
        SET role_id = NULL
        WHERE id = %s
        """,
        (row_id,),
    )
    return affected > 0


async def async_getVipRecoveryConfigsForGuildIds(
    guild_ids: set[int] | list[int] | tuple[int, ...],
    *,
    chunk_size: int = 250,
) -> dict[int, dict[str, Any]]:
    """Load VIP config in batches only for guilds that need session recovery."""

    normalized_guild_ids = sorted({int(guild_id) for guild_id in guild_ids if guild_id})
    if not normalized_guild_ids:
        return {}

    configs: dict[int, dict[str, Any]] = {}
    safe_chunk_size = max(1, int(chunk_size))

    for offset in range(0, len(normalized_guild_ids), safe_chunk_size):
        batch = normalized_guild_ids[offset:offset + safe_chunk_size]
        placeholders = ", ".join(["%s"] * len(batch))
        rows = await async_fetchall(
            f"""
            SELECT server_guild_id,
                   vip_roles,
                   vip_custom_role_prefix,
                   vip_role_division_start_id,
                   vip_role_division_end_id
            FROM config_server_settings
            WHERE server_guild_id IN ({placeholders})
            """,
            tuple(batch),
        )
        for row in rows:
            guild_id = row.get("server_guild_id")
            if guild_id is None:
                continue
            prefix = row.get("vip_custom_role_prefix")
            configs[int(guild_id)] = {
                "roleIds": _parse_vip_role_ids(row.get("vip_roles")),
                "customRolePrefix": (
                    prefix.strip()
                    if isinstance(prefix, str) and prefix.strip()
                    else "VIP"
                ),
                "startRoleId": (
                    int(row["vip_role_division_start_id"])
                    if row.get("vip_role_division_start_id") is not None
                    else None
                ),
                "endRoleId": (
                    int(row["vip_role_division_end_id"])
                    if row.get("vip_role_division_end_id") is not None
                    else None
                ),
            }

    return configs


async def async_clearCustomRoleRoleId(guild_id: int, discord_user_id: int) -> bool:
    """Clear a persisted custom-role link without blocking the event loop."""

    affected = await async_execute(
        """
        UPDATE user_custom_roles
        SET role_id = NULL
        WHERE server_guild_id = %s
          AND owner_discord_user_id = %s
        """,
        (guild_id, discord_user_id),
    )
    return affected > 0


def clearCustomRoleRoleId(guild_id: int, discord_user_id: int) -> bool:
    with pooled_connection(True) as cursor:
        if not _ensure_user_custom_roles_columns(cursor):
            return False

        cursor.execute(
            """
            UPDATE user_custom_roles
            SET role_id = NULL
            WHERE server_guild_id = %s
              AND owner_discord_user_id = %s
            """,
            (guild_id, discord_user_id),
        )
        return True


def clearCustomRoleRoleIdById(row_id: int) -> bool:
    with pooled_connection(True) as cursor:
        if not _ensure_user_custom_roles_columns(cursor):
            return False
        cursor.execute(
            """
            UPDATE user_custom_roles
            SET role_id = NULL
            WHERE id = %s
            """,
            (row_id,),
        )
        return True


def getServerMessage(messageType:ServerMessagesEnum, guild_id:int):
    with pooled_connection(True) as cursor:
        query = f"""SELECT {messageType} 
        FROM discord_server_messages
        WHERE server_guild_id = {guild_id}"""
        cursor.execute(query)
        message = cursor.fetchone()
        return message if message != None else None

def setServerMessage(guild_id:int, messageType:ServerMessagesEnum, message:str):
    with pooled_connection(True) as cursor:
        query = f"""SELECT * 
    FROM discord_server_messages
    WHERE server_guild_id = {guild_id}"""
        cursor.execute(query)
        myresult = cursor.fetchone()
        try:
            if myresult == None:
                query = f"""INSERT INTO discord_server_messages (server_guild_id, {messageType})
        VALUES ({guild_id}, '{message}')"""
                cursor.execute(query)
                return True
            else:
                query = f"""UPDATE discord_server_messages
        SET {messageType} = '{message}'
        WHERE server_guild_id = {guild_id}"""
                cursor.execute(query)
                return True
        except Exception as e:
            return False


def getTodayBirthdays(guild_id:int):
    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        if not _ensure_birthday_mentionable_column(cursor):
            return []

        query = """SELECT user_discord.discord_user_id, user_birthday.birth_date
    FROM user_birthday
    JOIN user_discord ON user_birthday.user_id = user_discord.user_id
    JOIN user_community_status ON user_community_status.user_id = user_birthday.user_id
    WHERE MONTH(birth_date) = MONTH(NOW())
    AND DAY(birth_date) = DAY(NOW())
    AND user_community_status.community_id = %s
    AND user_community_status.birthday_mentionable = 1;"""
        cursor.execute(query, (community_id,))
        myresult = cursor.fetchall()
        users:list[SimpleUserBirthday] = []
        for ub in myresult:
            users.append(SimpleUserBirthday(ub["discord_user_id"],ub["birth_date"]))
        return users


def getUsersTurning18Today(guild_id: int):
    """Return users who are turning 18 today in the specified guild."""
    with pooled_connection() as cursor:
        query = f"""SELECT user_discord.discord_user_id, user_birthday.birth_date FROM user_birthday
    JOIN user_discord ON user_birthday.user_id = user_discord.user_id
    WHERE DATE_ADD(birth_date, INTERVAL 18 YEAR) = CURDATE();"""
        cursor.execute(query)
        myresult = cursor.fetchall()
        users: list[SimpleUserBirthday] = []
        for ub in myresult:
            users.append(SimpleUserBirthday(ub["discord_user_id"], ub["birth_date"]))
        return users


def updateVoiceRecord(guild_id:int, discord_user:discord.Member, seconds:int):
    """Update the longest continuous voice call time for a member"""
    user_id = includeUser(discord_user, guild_id)
    with pooled_connection() as cursor:
        try:
            query = f"""INSERT INTO user_records (user_id, server_guild_id, voice_time)
    VALUES ({user_id}, {guild_id}, {seconds})
    ON DUPLICATE KEY UPDATE voice_time = IF({seconds} > voice_time, {seconds}, voice_time);"""
            cursor.execute(query)
            return True
        except mysql.connector.Error as err:
            logging.error(f"Database error occurred: {err}")
            return False


def getVoiceTime(guild_id:int, discord_user:discord.Member) -> int:
    """Retrieve the total recorded voice time in seconds for a member"""
    user_id = includeUser(discord_user, guild_id)
    with pooled_connection() as cursor:
        query = f"""SELECT voice_time FROM user_records
    WHERE user_id = {user_id} AND server_guild_id = {guild_id};"""
        cursor.execute(query)
        myresult = cursor.fetchone()
        return myresult[0] if myresult else 0


def getAllVoiceRecords(guild_id: int, limit: int = 10):
    """Retrieve top voice call records for a guild sorted by duration"""
    with pooled_connection() as cursor:
        query = f"""SELECT user_discord.discord_user_id, user_records.voice_time
    FROM user_records
    JOIN user_discord ON user_discord.user_id = user_records.user_id
    WHERE user_records.server_guild_id = {guild_id}
    ORDER BY user_records.voice_time DESC
    LIMIT {limit};"""
        cursor.execute(query)
        records = cursor.fetchall()
        return [{'user_id': row["discord_user_id"], 'seconds': row["voice_time"]} for row in records]


def updateGameRecord(guild_id:int, discord_user:discord.Member, seconds:int, game_name:str):
    """Update the longest continuous game time for a member and store the game name"""
    user_id = includeUser(discord_user, guild_id)
    with pooled_connection() as cursor:
        try:
            sql = """
            INSERT INTO user_records
              (user_id, server_guild_id, game_time, game_name)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              game_time  = IF(%s > game_time, %s,  game_time),
              game_name  = IF(%s > game_time, %s,  game_name)
            """
            params = (
                user_id, guild_id, seconds, game_name,  # INSERT
                seconds, seconds,                        # UPDATE game_time
                seconds, game_name                       # UPDATE game_name
            )
            cursor.execute(sql, params)
            return True
        except mysql.connector.Error as err:
            logging.error(f"Database error occurred: {err}")
            return False


def increment_boop_counts(guild_id: int, booper: discord.Member, target: discord.Member) -> bool:
    """Increment boop counters for the acting user and the target."""

    booper_id = includeUser(booper, guild_id)
    target_id = includeUser(target, guild_id)

    with pooled_connection(True) as cursor:
        try:
            cursor.execute(
                """
                INSERT INTO user_records (user_id, server_guild_id, boops)
                VALUES (%s, %s, 1)
                ON DUPLICATE KEY UPDATE boops = boops + 1
                """,
                (booper_id, guild_id),
            )
            cursor.execute(
                """
                INSERT INTO user_records (user_id, server_guild_id, boops_received)
                VALUES (%s, %s, 1)
                ON DUPLICATE KEY UPDATE boops_received = boops_received + 1
                """,
                (target_id, guild_id),
            )
            return True
        except mysql.connector.Error as err:
            logging.error(f"Database error occurred: {err}")
            return False


def record_minigame_match(
    guild_id: int,
    discord_user: discord.Member,
    game_name: str,
    points: int,
    won: bool,
) -> bool:
    """Persist one minigame match result."""
    user_id = includeUser(discord_user, guild_id)
    with pooled_connection(True) as cursor:
        try:
            cursor.execute(
                """
                INSERT INTO minigame_scores (user_id, server_guild_id, game_name, points, won)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, guild_id, game_name, points, won),
            )
            return True
        except mysql.connector.Error as err:
            logging.error(f"Database error occurred: {err}")
            return False


def get_weekly_minigame_ranking(guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Return weekly ranking by total points and wins for a guild."""
    with pooled_connection(True) as cursor:
        cursor.execute(
            """
            SELECT
                ud.discord_user_id AS discord_user_id,
                COALESCE(SUM(ms.points), 0) AS points,
                COALESCE(SUM(CASE WHEN ms.won THEN 1 ELSE 0 END), 0) AS wins
            FROM minigame_scores ms
            JOIN user_discord ud ON ud.user_id = ms.user_id
            WHERE ms.server_guild_id = %s
              AND YEARWEEK(ms.created_at, 1) = YEARWEEK(UTC_TIMESTAMP(), 1)
            GROUP BY ud.discord_user_id
            ORDER BY points DESC, wins DESC
            LIMIT %s
            """,
            (guild_id, limit),
        )
        return cursor.fetchall() or []


def getGameTime(guild_id:int, discord_user:discord.Member) -> int:
    """Retrieve the total recorded game time in seconds for a member"""
    user_id = includeUser(discord_user, guild_id)
    with pooled_connection() as cursor:
        query = f"""SELECT game_time FROM user_records
    WHERE user_id = {user_id} AND server_guild_id = {guild_id};"""
        cursor.execute(query)
        myresult = cursor.fetchone()
        return myresult[0] if myresult else 0


def getAllGameRecords(guild_id: int, limit: int = 10, blacklist: list[str] = None):
    """Retrieve top game time records for a guild sorted by duration"""
    with pooled_connection() as cursor:
        exclusion = ""
        if blacklist:
            names = "', '".join(name.replace("'", "\\'") for name in blacklist)
            exclusion = f" AND user_records.game_name NOT IN ('{names}')"
        query = f"""SELECT user_discord.discord_user_id, user_records.game_time, user_records.game_name
    FROM user_records
    JOIN user_discord ON user_discord.user_id = user_records.user_id
    WHERE user_records.server_guild_id = {guild_id}{exclusion}
    ORDER BY user_records.game_time DESC
    LIMIT {limit};"""
        cursor.execute(query)
        records = cursor.fetchall()
        return [{'user_id': row["discord_user_id"], 'seconds': row["game_time"], 'game': row["game_name"]} for row in records]


def getBlacklistedGames(guild_id: int) -> list[str]:
    """Retrieve all blacklisted games for a guild"""
    with pooled_connection() as cursor:
        query = f"""SELECT game_name FROM blacklisted_games
    WHERE server_guild_id = {guild_id};"""
        cursor.execute(query)
        rows = cursor.fetchall()
        return [row["game_name"] for row in rows]


def addGameToBlacklist(guild_id: int, game_name: str) -> bool:
    """Add a game to the blacklist for a guild"""
    with pooled_connection() as cursor:
        try:
            query = """INSERT IGNORE INTO blacklisted_games (server_guild_id, game_name)
    VALUES (%s, %s);"""
            cursor.execute(query, (guild_id, game_name))
            return True
        except mysql.connector.Error as err:
            logging.error(f"Database error occurred: {err}")
            return False


def getVoiceRecordPosition(guild_id: int, discord_user: discord.Member | discord.User):
    """Return a member's voice record rank and value in seconds."""
    user_id = includeUser(discord_user, guild_id)
    sql = """
    SELECT voice_time, rank
    FROM (
      SELECT
        user_id,
        voice_time,
        RANK() OVER (
          PARTITION BY server_guild_id
          ORDER BY voice_time DESC
        ) AS rank
      FROM user_records
      WHERE server_guild_id = %s
    ) AS ranked
    WHERE user_id = %s
    """
    with pooled_connection() as cursor:
        cursor.execute(sql, (guild_id, user_id))
        row = cursor.fetchone()
        if not row or row["voice_time"] is None:
            return None
        return {"rank": row["rank"], "seconds": row["voice_time"]}


def getGameRecordPosition(
    guild_id: int,
    discord_user: discord.Member | discord.User,
    user_id: int | None = None,
):
    """Return a member's game record rank, value in seconds and game name."""
    user_id = includeUser(discord_user, guild_id) if user_id is None else user_id
    sql = """
    SELECT
      ur.game_time,
      ur.game_name,
      (
        SELECT COUNT(*) + 1
          FROM user_records u2
         WHERE u2.server_guild_id = ur.server_guild_id
           AND u2.game_time > ur.game_time
           AND NOT EXISTS (
             SELECT 1
               FROM blacklisted_games bg2
              WHERE bg2.server_guild_id = u2.server_guild_id
                AND bg2.game_name     = u2.game_name
           )
      ) AS rank
    FROM user_records ur
    WHERE ur.server_guild_id = %s
      AND ur.user_id        = %s
      AND NOT EXISTS (
        SELECT 1
          FROM blacklisted_games bg
         WHERE bg.server_guild_id = ur.server_guild_id
           AND bg.game_name       = ur.game_name
      )
    """
    with pooled_connection() as cursor:
        cursor.execute(sql, (guild_id, user_id))
        row = cursor.fetchone()
        if not row or row["game_time"] is None:
            return None
        return {
            "rank":    row["rank"],
            "seconds": row["game_time"],
            "game":    row["game_name"]
        }


def getProfileData(
    guild_id: int,
    user: discord.Member | discord.User,
):
    """Return a :class:`User` object with all profile information for the ``/perfil`` command using a single connection."""

    user_id = includeUser(user, guild_id)

    with pooled_connection() as cursor:
        community_id = _get_community_id(cursor, guild_id)
        info_query = (
            "SELECT user_discord.discord_user_id, user_discord.display_name, "
            "user_community_status.member_since, user_community_status.approved, "
            "user_community_status.approved_at, user_community_status.is_vip, "
            "user_community_status.is_partner, user_level.current_level, "
            "user_birthday.birth_date, user_birthday.verified, locale.locale_name, "
            "user_economy.bank_balance "
            "FROM users "
            "LEFT JOIN user_discord ON users.id = user_discord.user_id "
            "LEFT JOIN user_community_status ON users.id = user_community_status.user_id "
            "AND user_community_status.community_id = %s "
            "LEFT JOIN user_birthday ON user_birthday.user_id = users.id "
            "LEFT JOIN user_level ON user_level.user_id = users.id AND user_level.server_guild_id = %s "
            "LEFT JOIN user_locale ON user_locale.user_id = users.id "
            "LEFT JOIN locale ON locale.id = user_locale.locale_id "
            "LEFT JOIN user_economy ON user_economy.user_id = users.id AND user_economy.server_guild_id = %s "
            "WHERE user_discord.user_id = %s"
        )

        cursor.execute(info_query, (community_id, guild_id, guild_id, user_id))
        db_user = cursor.fetchone()
        if not db_user:
            return None

        approved = db_user["approved"]
        if isinstance(user, discord.Member):
            unverified_role_id = getGuildMemberNotVerifiedRoleId(guild_id)
            has_unverified_role = any(
                role.id == unverified_role_id for role in user.roles
            )
            # The live Discord role may make the profile fresher than the
            # aggregate until the lifecycle event reaches the API, but reads
            # must never make Coddy a second writer of API-owned approval state.
            approved = not has_unverified_role

        profile = User(
            id=user_id,
            discordId=user.id,
            username=user.name,
            displayName=getattr(user, "display_name", user.name),
            memberSince=db_user["member_since"],
            approved=approved,
            approvedAt=db_user["approved_at"],
            isVip=db_user["is_vip"],
            isPartner=db_user["is_partner"],
            level=db_user["current_level"],
            birthday=db_user["birth_date"],
            birthdayVerified=db_user["verified"],
            locale=db_user["locale_name"],
            coins=db_user["bank_balance"],
            warnings=[],
            inventory=[],
            staffOf=[],
        )

        cursor.execute(
            "SELECT user_warnings.date, user_warnings.reason, user_warnings.expired "
            "FROM user_warnings JOIN users ON user_warnings.user_id = users.id "
            "WHERE users.id = %s "
            "AND user_warnings.community_id = %s "
            "ORDER BY user_warnings.date DESC",
            (user_id, community_id),
        )
        warnings = cursor.fetchall() or []
        profile.warnings = [
            Warning(i["date"], i["reason"], i["expired"])
            for i in warnings
        ]

        cursor.execute(
            "SELECT COUNT(*) AS notes_count FROM user_notes WHERE user_id = %s",
            (user_id,),
        )
        notes_count_row = cursor.fetchone()
        profile.notesCount = notes_count_row["notes_count"] if notes_count_row else 0

        cursor.execute(
            "SELECT discord_user_id FROM user_discord WHERE user_id = %s AND discord_user_id <> %s",
            (user_id, user.id),
        )
        alt_rows = cursor.fetchall() or []
        profile.altAccounts = [row["discord_user_id"] for row in alt_rows]

        voice_sql = (
            "SELECT voice_time, rank "
            "FROM ("
            "  SELECT user_id, voice_time, "
            "    RANK() OVER (PARTITION BY server_guild_id ORDER BY voice_time DESC) AS rank "
            "  FROM user_records "
            "  WHERE server_guild_id = %s"
            ") AS ranked "
            "WHERE user_id = %s"
        )
        cursor.execute(voice_sql, (guild_id, user_id))
        row = cursor.fetchone()
        profile.voiceRecord = (
            {"rank": row["rank"], "seconds": row["voice_time"]} if row and row["voice_time"] is not None else None
        )

        game_sql = (
            "SELECT ur.game_time, ur.game_name, ("
            "    SELECT COUNT(*) + 1"
            "      FROM user_records u2"
            "     WHERE u2.server_guild_id = ur.server_guild_id"
            "       AND u2.game_time > ur.game_time"
            "       AND NOT EXISTS ("
            "         SELECT 1"
            "           FROM blacklisted_games bg2"
            "          WHERE bg2.server_guild_id = u2.server_guild_id"
            "            AND bg2.game_name     = u2.game_name"
            "       )"
            ") AS rank "
            "FROM user_records ur "
            "WHERE ur.server_guild_id = %s "
            "  AND ur.user_id        = %s "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM blacklisted_games bg "
            "    WHERE bg.server_guild_id = ur.server_guild_id "
            "      AND bg.game_name       = ur.game_name"
            "  )"
        )
        cursor.execute(game_sql, (guild_id, user_id))
        row = cursor.fetchone()
        if row and row["game_time"] is not None:
            profile.gameRecord = {"rank": row["rank"], "seconds": row["game_time"], "game": row["game_name"]}
        else:
            profile.gameRecord = None

        return profile


def list_form_flows(guild_id: int) -> list[dict]:
    """Lista todos os fluxos de formulários."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT id, server_guild_id, name, type, target_channel_id,
                   approved_target_channel_id, rejected_target_channel_id,
                   rejection_feedback_enabled, created_at
            FROM form_flows
            WHERE server_guild_id = %s ORDER BY id ASC
            """, (guild_id,)
        )
        return cursor.fetchall() or []


def create_form_flow(guild_id: int, name: str, flow_type: str, target_channel_id: int) -> int:
    """Cria um fluxo de formulário e retorna o ID."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            INSERT INTO form_flows (server_guild_id, name, type, target_channel_id)
            VALUES (%s, %s, %s, %s)
            """,
            (guild_id, name, flow_type, target_channel_id),
        )
        return int(cursor.lastrowid)


_UNSET = object()


def update_form_flow(
    guild_id: int, flow_id: int,
    name: Optional[str] | object = _UNSET,
    flow_type: Optional[str] | object = _UNSET,
    target_channel_id: Optional[int] | object = _UNSET,
    approved_target_channel_id: Optional[int] | object = _UNSET,
    rejected_target_channel_id: Optional[int] | object = _UNSET,
    rejection_feedback_enabled: Optional[bool] | object = _UNSET,
) -> None:
    """Atualiza os dados de um fluxo de formulário."""

    updates = []
    params: list[object] = []
    if name is not _UNSET:
        updates.append("name = %s")
        params.append(name)
    if flow_type is not _UNSET:
        updates.append("type = %s")
        params.append(flow_type)
    if target_channel_id is not _UNSET:
        updates.append("target_channel_id = %s")
        params.append(target_channel_id)
    if approved_target_channel_id is not _UNSET:
        updates.append("approved_target_channel_id = %s")
        params.append(approved_target_channel_id)
    if rejected_target_channel_id is not _UNSET:
        updates.append("rejected_target_channel_id = %s")
        params.append(rejected_target_channel_id)
    if rejection_feedback_enabled is not _UNSET:
        updates.append("rejection_feedback_enabled = %s")
        params.append(rejection_feedback_enabled)

    if not updates:
        raise ValueError("Nenhuma alteração informada.")

    params.extend((flow_id, guild_id))
    with pooled_connection() as cursor:
        cursor.execute(
            f"""
            UPDATE form_flows
            SET {', '.join(updates)}
            WHERE id = %s AND server_guild_id = %s
            """,
            tuple(params),
        )


def get_form_flow(guild_id: int, flow_id: int) -> dict:
    """Busca um fluxo de formulário pelo ID."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT id, server_guild_id, name, type, target_channel_id,
                   approved_target_channel_id, rejected_target_channel_id,
                   rejection_feedback_enabled, created_at
            FROM form_flows
            WHERE id = %s AND server_guild_id = %s
            """, (flow_id, guild_id),
        )
        flow = cursor.fetchone()

    if not flow:
        raise ValueError("Fluxo de formulário não encontrado.")
    return flow


def get_form_flow_by_name(guild_id: int, flow_name: str) -> dict:
    """Busca um fluxo de formulário pelo nome."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT id, server_guild_id, name, type, target_channel_id,
                   approved_target_channel_id, rejected_target_channel_id,
                   rejection_feedback_enabled, created_at
            FROM form_flows
            WHERE server_guild_id = %s AND LOWER(name) = LOWER(%s)
            """, (guild_id, flow_name),
        )
        flows = cursor.fetchall() or []

    if not flows:
        raise ValueError("Fluxo de formulário não encontrado.")
    if len(flows) > 1:
        raise ValueError("Nome de fluxo ambíguo neste servidor; informe o ID.")
    return flows[0]


def get_form_questions(flow_id: int) -> list[dict]:
    """Busca perguntas ordenadas de um fluxo de formulário."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT id, flow_id, question_text, placeholder_text, position, required
            FROM form_questions
            WHERE flow_id = %s
            ORDER BY position ASC, id ASC
            """,
            (flow_id,),
        )
        return cursor.fetchall() or []


def replace_form_questions(flow_id: int, questions: list[dict]) -> None:
    """Substitui as perguntas de um fluxo de formulário."""

    if not questions:
        raise ValueError("Informe ao menos uma pergunta para o formulário.")

    with pooled_connection() as cursor:
        cursor.execute(
            """
            DELETE FROM form_questions
            WHERE flow_id = %s
            """,
            (flow_id,),
        )
        for position, question in enumerate(questions, start=1):
            cursor.execute(
                """
                INSERT INTO form_questions (flow_id, question_text, placeholder_text, position, required)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    flow_id,
                    question["question_text"],
                    question.get("placeholder_text"),
                    position,
                    bool(question.get("required")),
                ),
            )


def create_form_submission(
    flow_id: int,
    user_id: int,
    guild_id: int,
    channel_id: int,
) -> int:
    """Cria um registro de envio de formulário e retorna o ID."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            INSERT INTO form_submissions (flow_id, user_id, guild_id, channel_id)
            VALUES (%s, %s, %s, %s)
            """,
            (flow_id, user_id, guild_id, channel_id),
        )
        return int(cursor.lastrowid)


def update_form_submission_message_id(submission_id: int, message_id: int) -> None:
    """Atualiza o ID da mensagem associada a um envio de formulário."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE form_submissions
            SET message_id = %s
            WHERE id = %s
            """,
            (message_id, submission_id),
        )


def create_form_published_message(
    flow_id: int,
    message_id: int,
    channel_id: int,
    guild_id: int,
) -> None:
    """Registra a publicação de um formulário."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            INSERT INTO form_published_messages (flow_id, message_id, channel_id, guild_id)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                flow_id = VALUES(flow_id),
                channel_id = VALUES(channel_id),
                guild_id = VALUES(guild_id)
            """,
            (flow_id, message_id, channel_id, guild_id),
        )


def list_form_published_messages() -> list[dict]:
    """Lista mensagens de formulários publicadas para reconstrução das views."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT flow_id, message_id
            FROM form_published_messages
            ORDER BY id DESC
            """,
        )
        return cursor.fetchall() or []


def list_portaria_published_channels(guild_id: int) -> list[dict]:
    """Return the distinct channels where Portaria forms were published.

    ``target_channel_id`` is the submission destination, not the public entry
    channel.  This read intentionally derives the latter from the publication
    records already maintained by the forms feature.
    """
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT published.flow_id, published.message_id, published.channel_id, published.guild_id
            FROM form_published_messages published
            INNER JOIN form_flows flow ON flow.id = published.flow_id
            WHERE published.guild_id = %s AND LOWER(flow.type) = 'portaria'
            """,
            (guild_id,),
        )
        return cursor.fetchall() or []


def get_form_submission(submission_id: int) -> Optional[dict]:
    """Retorna os dados de um envio de formulário."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT id, flow_id, user_id, guild_id, channel_id, message_id, status
            FROM form_submissions
            WHERE id = %s
            """,
            (submission_id,),
        )
        return cursor.fetchone()


def get_form_submission_by_message(message_id: int, guild_id: int | None = None) -> Optional[dict]:
    """Retorna um envio de formulário pelo ID da mensagem."""

    with pooled_connection() as cursor:
        if guild_id is None:
            cursor.execute(
                """
                SELECT id, flow_id, user_id, guild_id, channel_id, message_id, status
                FROM form_submissions
                WHERE message_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (message_id,),
            )
        else:
            cursor.execute(
                """
                SELECT id, flow_id, user_id, guild_id, channel_id, message_id, status
                FROM form_submissions
                WHERE message_id = %s
                  AND guild_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (message_id, guild_id),
            )
        return cursor.fetchone()


def fix_form_submission_message_binding(
    submission_id: int,
    guild_id: int,
    channel_id: int,
    message_id: int,
) -> bool:
    """Atualiza vínculo de guild/canal/mensagem de um envio de formulário."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE form_submissions
            SET guild_id = %s,
                channel_id = %s,
                message_id = %s
            WHERE id = %s
            """,
            (guild_id, channel_id, message_id, submission_id),
        )
        return cursor.rowcount == 1


def reset_form_submission_to_pending(submission_id: int) -> bool:
    """Força um envio para pendente e remove decisões registradas."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT id
            FROM form_submissions
            WHERE id = %s
            FOR UPDATE
            """,
            (submission_id,),
        )
        submission = cursor.fetchone()
        if not submission:
            return False

        cursor.execute(
            """
            DELETE FROM form_decisions
            WHERE submission_id = %s
            """,
            (submission_id,),
        )
        cursor.execute(
            """
            UPDATE form_submissions
            SET status = 'pending'
            WHERE id = %s
            """,
            (submission_id,),
        )
        return cursor.rowcount == 1


def get_pending_portaria_submission_for_user(user_id: int, guild_id: int) -> Optional[dict]:
    """Retorna um envio pendente de portaria para um usuário, se existir."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT fs.id, fs.message_id, fs.channel_id
            FROM form_submissions fs
            INNER JOIN form_flows ff ON ff.id = fs.flow_id
            WHERE ff.type = 'portaria'
              AND fs.status = 'pending'
              AND fs.message_id IS NOT NULL
              AND fs.user_id = %s
              AND fs.guild_id = %s
            ORDER BY fs.id DESC
            LIMIT 1
            """,
            (user_id, guild_id),
        )
        return cursor.fetchone()


def record_form_decision(submission_id: int, decision: str, decided_by: int) -> None:
    """Registra a decisão do formulário e atualiza o status do envio."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT status
            FROM form_submissions
            WHERE id = %s
            FOR UPDATE
            """,
            (submission_id,),
        )
        submission = cursor.fetchone()
        if not submission:
            raise ValueError("Envio de formulário não encontrado.")
        if submission["status"] != "pending":
            raise ValueError("Este envio já foi analisado.")

        cursor.execute(
            """
            INSERT INTO form_decisions (submission_id, decision, decided_by)
            VALUES (%s, %s, %s)
            """,
            (submission_id, decision, decided_by),
        )
        cursor.execute(
            """
            UPDATE form_submissions
            SET status = %s
            WHERE id = %s
            """,
            (decision, submission_id),
        )


def rollback_form_decision(
    submission_id: int,
    decision: str,
    decided_by: int,
) -> bool:
    """Desfaz uma decisão recém-registrada e devolve o envio para pendente.

    O rollback só ocorre se o status atual do envio corresponder à decisão informada
    e se a decisão mais recente para esse envio também corresponder ao par
    (decision, decided_by).
    """

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT status
            FROM form_submissions
            WHERE id = %s
            FOR UPDATE
            """,
            (submission_id,),
        )
        submission = cursor.fetchone()
        if not submission:
            return False
        if submission["status"] != decision:
            return False

        cursor.execute(
            """
            SELECT id, decision, decided_by
            FROM form_decisions
            WHERE submission_id = %s
            ORDER BY id DESC
            LIMIT 1
            FOR UPDATE
            """,
            (submission_id,),
        )
        latest_decision = cursor.fetchone()
        if not latest_decision:
            return False
        if (
            latest_decision["decision"] != decision
            or int(latest_decision["decided_by"]) != int(decided_by)
        ):
            return False

        cursor.execute(
            """
            DELETE FROM form_decisions
            WHERE id = %s
            """,
            (latest_decision["id"],),
        )
        if cursor.rowcount != 1:
            return False

        cursor.execute(
            """
            UPDATE form_submissions
            SET status = 'pending'
            WHERE id = %s
            """,
            (submission_id,),
        )
        return cursor.rowcount == 1


def list_pending_portaria_submissions() -> list[dict]:
    """Lista envios de formulário pendentes para fluxo de portaria."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT fs.id, fs.message_id
            FROM form_submissions fs
            INNER JOIN form_flows ff ON ff.id = fs.flow_id
            WHERE ff.type = 'portaria'
              AND fs.status = 'pending'
              AND fs.message_id IS NOT NULL
            """,
        )
        return cursor.fetchall() or []


def get_portaria_form_decision_report(
    guild_id: int,
    initial_date,
    final_date,
) -> dict:
    """Retorna estatísticas de análises de fichas da portaria por staff."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT fd.decided_by, COUNT(*) AS total
            FROM form_decisions fd
            INNER JOIN form_submissions fs ON fs.id = fd.submission_id
            INNER JOIN form_flows ff ON ff.id = fs.flow_id
            WHERE ff.type = 'portaria'
              AND fs.guild_id = %s
              AND fd.decided_at >= %s
              AND fd.decided_at <= %s
            GROUP BY fd.decided_by
            """,
            (guild_id, initial_date, final_date),
        )
        decisions_by_staff = cursor.fetchall() or []

        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM form_decisions fd
            INNER JOIN form_submissions fs ON fs.id = fd.submission_id
            INNER JOIN form_flows ff ON ff.id = fs.flow_id
            WHERE ff.type = 'portaria'
              AND fs.guild_id = %s
              AND fd.decided_at >= %s
              AND fd.decided_at <= %s
            """,
            (guild_id, initial_date, final_date),
        )
        total_row = cursor.fetchone() or {"total": 0}

        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM form_decisions fd
            INNER JOIN form_submissions fs ON fs.id = fd.submission_id
            INNER JOIN form_flows ff ON ff.id = fs.flow_id
            WHERE ff.type = 'portaria'
              AND fs.guild_id = %s
              AND fd.decision = 'rejected'
              AND fd.decided_at >= %s
              AND fd.decided_at <= %s
            """,
            (guild_id, initial_date, final_date),
        )
        rejected_row = cursor.fetchone() or {"total": 0}

    return {
        "decisions_by_staff": decisions_by_staff,
        "total_decisions": int(total_row.get("total", 0) or 0),
        "total_rejected_decisions": int(rejected_row.get("total", 0) or 0),
    }


def list_portaria_auto_rejected_submissions(
    guild_id: int,
    initial_date,
    final_date,
) -> list[dict]:
    """Lista recusas automáticas da portaria no período informado."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT fs.user_id, fd.decided_at
            FROM form_decisions fd
            INNER JOIN form_submissions fs ON fs.id = fd.submission_id
            INNER JOIN form_flows ff ON ff.id = fs.flow_id
            WHERE ff.type = 'portaria'
              AND fs.guild_id = %s
              AND fd.decision = 'rejected'
              AND fs.message_id IS NULL
              AND fd.decided_at >= %s
              AND fd.decided_at <= %s
            """,
            (guild_id, initial_date, final_date),
        )
        return cursor.fetchall() or []


def validate_form_target_channel_id(target_channel_id: int) -> bool:
    """Valida se o target_channel_id existe em um fluxo cadastrado."""

    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT 1 FROM form_flows WHERE target_channel_id = %s LIMIT 1",
            (target_channel_id,),
        )
        return cursor.fetchone() is not None



def acquire_theme_guild_lock(
    guild_id: int,
    lock_token: str,
    operation_type: str,
    *,
    operation_id: int,
    application_id: int,
    ttl_seconds: int = 7200,
) -> bool:
    """Acquire one durable Theme lock per guild, replacing only expired locks."""

    normalized_type = str(operation_type or "").upper()
    if normalized_type not in {"APPLY", "RESTORE"}:
        raise ValueError("invalid_theme_operation_type")
    expires_seconds = max(60, min(int(ttl_seconds), 21600))
    with pooled_connection() as cursor:
        cursor.execute(
            """
            INSERT INTO discord_theme_guild_locks
                (guild_id,lock_token,operation_type,operation_id,application_id,expires_at)
            VALUES
                (%s,%s,%s,%s,%s,DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND))
            ON DUPLICATE KEY UPDATE
                lock_token=IF(
                    expires_at <= UTC_TIMESTAMP(6) OR lock_token=VALUES(lock_token),
                    VALUES(lock_token),
                    lock_token
                ),
                operation_type=IF(
                    expires_at <= UTC_TIMESTAMP(6) OR lock_token=VALUES(lock_token),
                    VALUES(operation_type),
                    operation_type
                ),
                operation_id=IF(
                    expires_at <= UTC_TIMESTAMP(6) OR lock_token=VALUES(lock_token),
                    VALUES(operation_id),
                    operation_id
                ),
                application_id=IF(
                    expires_at <= UTC_TIMESTAMP(6) OR lock_token=VALUES(lock_token),
                    VALUES(application_id),
                    application_id
                ),
                expires_at=IF(
                    expires_at <= UTC_TIMESTAMP(6) OR lock_token=VALUES(lock_token),
                    VALUES(expires_at),
                    expires_at
                )
            """,
            (
                guild_id,
                lock_token,
                normalized_type,
                operation_id,
                application_id,
                expires_seconds,
            ),
        )
        cursor.execute(
            "SELECT lock_token FROM discord_theme_guild_locks WHERE guild_id=%s",
            (guild_id,),
        )
        row = cursor.fetchone()
        return bool(row and str(row["lock_token"]) == str(lock_token))


def refresh_theme_guild_lock(
    guild_id: int,
    lock_token: str,
    *,
    ttl_seconds: int = 7200,
) -> bool:
    expires_seconds = max(60, min(int(ttl_seconds), 21600))
    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE discord_theme_guild_locks
            SET expires_at=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND)
            WHERE guild_id=%s AND lock_token=%s
            """,
            (expires_seconds, guild_id, lock_token),
        )
        return cursor.rowcount == 1


def release_theme_guild_lock(guild_id: int, lock_token: str) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            "DELETE FROM discord_theme_guild_locks WHERE guild_id=%s AND lock_token=%s",
            (guild_id, lock_token),
        )
        return cursor.rowcount == 1


def get_theme_application(application_id: int, guild_id: int) -> Optional[dict]:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT id,theme_id,guild_id,actor_discord_user_id,actor_user_id,status,
                   frozen_definition_json,applied_at,restored_at,error_summary,
                   rollback_assets_cleaned_at,created_at,updated_at
            FROM discord_theme_applications
            WHERE id=%s AND guild_id=%s
            LIMIT 1
            """,
            (application_id, guild_id),
        )
        return cursor.fetchone()


def get_theme_operation(operation_id: int, guild_id: int) -> Optional[dict]:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT id,application_id,guild_id,operation_key,operation_type,
                   actor_discord_user_id,actor_user_id,force_restore,status,
                   progress_current,progress_total,current_step,result_json,error_code,
                   started_at,finished_at,created_at,updated_at
            FROM discord_theme_operations
            WHERE id=%s AND guild_id=%s
            LIMIT 1
            """,
            (operation_id, guild_id),
        )
        return cursor.fetchone()


def mark_theme_operation_running(operation_id: int, guild_id: int) -> bool:
    """Atomically claim a pending Theme operation before lock/effects."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE discord_theme_operations
            SET status='RUNNING',
                started_at=COALESCE(started_at,UTC_TIMESTAMP(6)),
                current_step='STARTING',
                error_code=NULL
            WHERE id=%s AND guild_id=%s AND status='PENDING'
            """,
            (operation_id, guild_id),
        )
        if cursor.rowcount != 1:
            return False

        cursor.execute(
            """
            SELECT application_id,operation_type
            FROM discord_theme_operations
            WHERE id=%s AND guild_id=%s
            """,
            (operation_id, guild_id),
        )
        operation = cursor.fetchone()
        if operation:
            next_status = (
                "RESTORING"
                if str(operation["operation_type"]).upper() == "RESTORE"
                else "APPLYING"
            )
            cursor.execute(
                """
                UPDATE discord_theme_applications
                SET status=%s
                WHERE id=%s AND guild_id=%s
                """,
                (next_status, int(operation["application_id"]), guild_id),
            )
        return True


def complete_theme_operation(
    operation_id: int,
    guild_id: int,
    *,
    status: Literal["SUCCEEDED", "PARTIAL", "FAILED"],
    application_status: str,
    result: Optional[dict] = None,
    error_code: Optional[str] = None,
    release_active: bool = False,
) -> bool:
    if status not in {"SUCCEEDED", "PARTIAL", "FAILED"}:
        raise ValueError("invalid_theme_operation_status")
    if application_status not in {
        "ACTIVE", "APPLY_PARTIAL", "FAILED",
        "RESTORED", "RESTORE_PARTIAL",
    }:
        raise ValueError("invalid_theme_application_status")

    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE discord_theme_operations
            SET status=%s,
                result_json=%s,
                error_code=%s,
                progress_current=CASE
                    WHEN progress_total > 0 THEN progress_total
                    ELSE progress_current
                END,
                finished_at=UTC_TIMESTAMP(6)
            WHERE id=%s AND guild_id=%s AND status='RUNNING'
            """,
            (
                status,
                json.dumps(result or {}, ensure_ascii=False),
                str(error_code)[:96] if error_code else None,
                operation_id,
                guild_id,
            ),
        )
        if cursor.rowcount != 1:
            return False

        cursor.execute(
            """
            SELECT application_id,operation_type
            FROM discord_theme_operations
            WHERE id=%s AND guild_id=%s
            """,
            (operation_id, guild_id),
        )
        operation = cursor.fetchone()
        if not operation:
            return False
        application_id = int(operation["application_id"])
        if application_status == "ACTIVE":
            cursor.execute(
                """
                UPDATE discord_theme_applications
                SET status='ACTIVE',
                    applied_at=COALESCE(applied_at,UTC_TIMESTAMP(6)),
                    error_summary=%s
                WHERE id=%s AND guild_id=%s
                """,
                (
                    None if status == "SUCCEEDED" else str(error_code or "partial")[:512],
                    application_id,
                    guild_id,
                ),
            )
        elif application_status == "RESTORED":
            cursor.execute(
                """
                UPDATE discord_theme_applications
                SET status='RESTORED',
                    restored_at=COALESCE(restored_at,UTC_TIMESTAMP(6)),
                    error_summary=NULL
                WHERE id=%s AND guild_id=%s
                """,
                (application_id, guild_id),
            )
        else:
            cursor.execute(
                """
                UPDATE discord_theme_applications
                SET status=%s,error_summary=%s
                WHERE id=%s AND guild_id=%s
                """,
                (
                    application_status,
                    str(error_code or application_status.lower())[:512],
                    application_id,
                    guild_id,
                ),
            )

        if release_active:
            cursor.execute(
                """
                DELETE FROM discord_theme_active_guilds
                WHERE guild_id=%s AND application_id=%s
                """,
                (guild_id, application_id),
            )
        return True


def record_theme_operation_step(
    guild_id: int,
    operation_id: int,
    step_code: str,
    step_status: str,
    message: str,
    progress_current: int,
    progress_total: int,
    *,
    detail: Optional[dict] = None,
) -> int:
    normalized_status = str(step_status or "").upper()
    if normalized_status not in {
        "PENDING", "RUNNING", "SUCCEEDED", "SKIPPED",
        "FAILED", "DRIFTED", "MISSING",
    }:
        raise ValueError("invalid_theme_step_status")
    current = max(0, int(progress_current))
    total = max(current, int(progress_total))
    with pooled_connection() as cursor:
        cursor.execute(
            """
            INSERT INTO discord_theme_operation_steps
                (operation_id,guild_id,step_code,step_status,message,
                 progress_current,progress_total,detail_json)
            SELECT id,guild_id,%s,%s,%s,%s,%s,%s
            FROM discord_theme_operations
            WHERE id=%s AND guild_id=%s
            """,
            (
                str(step_code)[:96],
                normalized_status,
                str(message)[:512],
                current,
                total,
                json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                operation_id,
                guild_id,
            ),
        )
        if cursor.rowcount != 1:
            raise LookupError("theme_operation_not_found")
        step_id = int(cursor.lastrowid)
        cursor.execute(
            """
            UPDATE discord_theme_operations
            SET progress_current=%s,
                progress_total=%s,
                current_step=%s
            WHERE id=%s AND guild_id=%s AND status='RUNNING'
            """,
            (current, total, str(step_code)[:96], operation_id, guild_id),
        )
        return step_id


def create_theme_application_snapshot(
    guild_id: int,
    application_id: int,
    *,
    resource_type: str,
    resource_discord_id: int = 0,
    before_value: Optional[str] = None,
    applied_value: Optional[str] = None,
    before_asset_key: Optional[str] = None,
    before_asset_content_type: Optional[str] = None,
    before_asset_sha256: Optional[str] = None,
    applied_asset_key: Optional[str] = None,
    applied_asset_content_type: Optional[str] = None,
    applied_asset_sha256: Optional[str] = None,
) -> dict:
    normalized_type = str(resource_type or "").upper()
    if normalized_type not in {
        "GUILD_ICON", "GUILD_BANNER", "CHANNEL", "CATEGORY", "ROLE",
    }:
        raise ValueError("invalid_theme_snapshot_resource_type")

    with pooled_connection() as cursor:
        cursor.execute(
            """
            INSERT IGNORE INTO discord_theme_application_snapshots
                (application_id,resource_type,resource_discord_id,
                 before_value,applied_value,
                 before_asset_key,before_asset_content_type,before_asset_sha256,
                 applied_asset_key,applied_asset_content_type,applied_asset_sha256,
                 apply_status)
            SELECT id,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING'
            FROM discord_theme_applications
            WHERE id=%s AND guild_id=%s
            """,
            (
                normalized_type,
                int(resource_discord_id or 0),
                before_value,
                applied_value,
                before_asset_key,
                before_asset_content_type,
                before_asset_sha256,
                applied_asset_key,
                applied_asset_content_type,
                applied_asset_sha256,
                application_id,
                guild_id,
            ),
        )
        cursor.execute(
            """
            SELECT snap.*
            FROM discord_theme_application_snapshots snap
            JOIN discord_theme_applications app ON app.id=snap.application_id
            WHERE app.guild_id=%s
              AND snap.application_id=%s
              AND snap.resource_type=%s
              AND snap.resource_discord_id=%s
            LIMIT 1
            """,
            (
                guild_id,
                application_id,
                normalized_type,
                int(resource_discord_id or 0),
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise LookupError("theme_snapshot_not_persisted")
        return row


def list_theme_application_snapshots(
    guild_id: int,
    application_id: int,
) -> list[dict]:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT snap.*
            FROM discord_theme_application_snapshots snap
            JOIN discord_theme_applications app ON app.id=snap.application_id
            WHERE app.guild_id=%s AND snap.application_id=%s
            ORDER BY snap.id
            """,
            (guild_id, application_id),
        )
        return cursor.fetchall() or []


def update_theme_snapshot_apply_status(
    guild_id: int,
    application_id: int,
    snapshot_id: int,
    status: Literal["APPLIED", "SKIPPED", "FAILED"],
    *,
    error_code: Optional[str] = None,
) -> bool:
    if status not in {"APPLIED", "SKIPPED", "FAILED"}:
        raise ValueError("invalid_theme_snapshot_apply_status")
    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE discord_theme_application_snapshots snap
            JOIN discord_theme_applications app ON app.id=snap.application_id
            SET snap.apply_status=%s,
                snap.error_code=%s
            WHERE snap.id=%s
              AND snap.application_id=%s
              AND app.guild_id=%s
            """,
            (
                status,
                str(error_code)[:96] if error_code else None,
                snapshot_id,
                application_id,
                guild_id,
            ),
        )
        return cursor.rowcount == 1


def update_theme_snapshot_restore_status(
    guild_id: int,
    application_id: int,
    snapshot_id: int,
    status: Literal["RESTORED", "DRIFTED", "MISSING", "SKIPPED", "FAILED"],
    *,
    error_code: Optional[str] = None,
) -> bool:
    if status not in {"RESTORED", "DRIFTED", "MISSING", "SKIPPED", "FAILED"}:
        raise ValueError("invalid_theme_snapshot_restore_status")
    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE discord_theme_application_snapshots snap
            JOIN discord_theme_applications app ON app.id=snap.application_id
            SET snap.restore_status=%s,
                snap.error_code=%s
            WHERE snap.id=%s
              AND snap.application_id=%s
              AND app.guild_id=%s
            """,
            (
                status,
                str(error_code)[:96] if error_code else None,
                snapshot_id,
                application_id,
                guild_id,
            ),
        )
        return cursor.rowcount == 1


def get_theme_vip_custom_role_ids(guild_id: int) -> set[int]:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT role_id
            FROM user_custom_roles
            WHERE server_guild_id=%s AND role_id IS NOT NULL
            """,
            (guild_id,),
        )
        return {
            int(row["role_id"])
            for row in (cursor.fetchall() or [])
            if row.get("role_id") is not None
        }


def list_recoverable_theme_operations() -> list[dict]:
    """Find pending requests or RUNNING operations whose durable lock is stale."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT op.id,op.application_id,op.guild_id,op.operation_type,
                   op.actor_discord_user_id,op.force_restore,op.status,
                   op.started_at,op.updated_at
            FROM discord_theme_operations op
            LEFT JOIN discord_theme_guild_locks lock_row
              ON lock_row.guild_id=op.guild_id
             AND lock_row.operation_id=op.id
            WHERE op.status='PENDING'
               OR (
                    op.status='RUNNING'
                    AND (
                        lock_row.guild_id IS NULL
                        OR lock_row.expires_at <= UTC_TIMESTAMP(6)
                    )
               )
            ORDER BY op.id
            """
        )
        return cursor.fetchall() or []


def acquire_backup_guild_lock(
    guild_id: int,
    lock_token: str,
    operation_type: str,
    *,
    operation_id: Optional[int] = None,
    ttl_seconds: int = 1800,
) -> bool:
    """Acquire one durable Backup lock per guild, replacing only expired locks."""

    expires_seconds = max(60, min(int(ttl_seconds), 7200))
    with pooled_connection() as cursor:
        cursor.execute(
            """
            INSERT INTO backup_guild_locks
                (guild_id, lock_token, operation_type, operation_id, expires_at)
            VALUES
                (%s, %s, %s, %s,
                 DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND))
            ON DUPLICATE KEY UPDATE
                lock_token = IF(
                    expires_at <= UTC_TIMESTAMP(6) OR lock_token = VALUES(lock_token),
                    VALUES(lock_token),
                    lock_token
                ),
                operation_type = IF(
                    expires_at <= UTC_TIMESTAMP(6) OR lock_token = VALUES(lock_token),
                    VALUES(operation_type),
                    operation_type
                ),
                operation_id = IF(
                    expires_at <= UTC_TIMESTAMP(6) OR lock_token = VALUES(lock_token),
                    VALUES(operation_id),
                    operation_id
                ),
                expires_at = IF(
                    expires_at <= UTC_TIMESTAMP(6) OR lock_token = VALUES(lock_token),
                    VALUES(expires_at),
                    expires_at
                )
            """,
            (
                guild_id,
                lock_token,
                operation_type,
                operation_id,
                expires_seconds,
            ),
        )
        cursor.execute(
            "SELECT lock_token FROM backup_guild_locks WHERE guild_id = %s",
            (guild_id,),
        )
        row = cursor.fetchone()
        return bool(row and str(row["lock_token"]) == str(lock_token))


def refresh_backup_guild_lock(
    guild_id: int,
    lock_token: str,
    *,
    ttl_seconds: int = 1800,
) -> bool:
    expires_seconds = max(60, min(int(ttl_seconds), 7200))
    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE backup_guild_locks
            SET expires_at = DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND)
            WHERE guild_id = %s AND lock_token = %s
            """,
            (expires_seconds, guild_id, lock_token),
        )
        return cursor.rowcount == 1


def release_backup_guild_lock(guild_id: int, lock_token: str) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            "DELETE FROM backup_guild_locks WHERE guild_id = %s AND lock_token = %s",
            (guild_id, lock_token),
        )
        return cursor.rowcount == 1


def create_backup_restore_operation(
    guild_id: int,
    backup_id: int,
    operation_key: str,
    scope: Literal["full", "roles", "channels", "permissions"],
    actor_discord_user_id: int,
    *,
    actor_user_id: Optional[int] = None,
    decision: Optional[dict] = None,
) -> Optional[int]:
    """Create or reuse one guild-scoped restore operation by idempotency key."""

    normalized_scope = str(scope or "").lower()
    if normalized_scope not in {"full", "roles", "channels", "permissions"}:
        raise ValueError("invalid_backup_restore_scope")
    normalized_key = str(operation_key or "").strip()
    if not normalized_key or len(normalized_key) > 96:
        raise ValueError("invalid_backup_restore_operation_key")

    with pooled_connection() as cursor:
        cursor.execute(
            """
            INSERT IGNORE INTO backup_restore_operations
                (guild_id, backup_id, operation_key, scope,
                 actor_discord_user_id, actor_user_id, status, decision_json)
            SELECT
                %s, bd.id, %s, %s, %s, %s, 'PENDING', %s
            FROM backup_discord bd
            WHERE bd.id = %s AND bd.guild_id = %s
            """,
            (
                guild_id,
                normalized_key,
                normalized_scope,
                actor_discord_user_id,
                actor_user_id,
                json.dumps(decision or {}, ensure_ascii=False),
                backup_id,
                guild_id,
            ),
        )
        if cursor.rowcount == 1:
            return int(cursor.lastrowid)

        cursor.execute(
            """
            SELECT id
            FROM backup_restore_operations
            WHERE guild_id = %s AND operation_key = %s
            LIMIT 1
            """,
            (guild_id, normalized_key),
        )
        existing = cursor.fetchone()
        return int(existing["id"]) if existing else None


def get_backup_restore_operation(operation_id: int, guild_id: int) -> Optional[dict]:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT id, guild_id, backup_id, operation_key, scope,
                   actor_discord_user_id, actor_user_id, status, decision_json,
                   result_json, error_code, progress_current, progress_total,
                   current_step, started_at, finished_at, created_at, updated_at
            FROM backup_restore_operations
            WHERE id = %s AND guild_id = %s
            LIMIT 1
            """,
            (operation_id, guild_id),
        )
        return cursor.fetchone()


def fail_pending_backup_restore_operation(
    operation_id: int,
    guild_id: int,
    error_code: str,
) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE backup_restore_operations
            SET status = 'FAILED',
                error_code = %s,
                result_json = %s,
                current_step = 'FAILED',
                finished_at = UTC_TIMESTAMP(6)
            WHERE id = %s AND guild_id = %s AND status = 'PENDING'
            """,
            (
                str(error_code)[:96],
                json.dumps({"warnings": [str(error_code)]}, ensure_ascii=False),
                operation_id,
                guild_id,
            ),
        )
        return cursor.rowcount == 1


def mark_backup_restore_operation_running(operation_id: int, guild_id: int) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE backup_restore_operations
            SET status = 'RUNNING',
                started_at = COALESCE(started_at, UTC_TIMESTAMP(6)),
                current_step = 'STARTING',
                error_code = NULL
            WHERE id = %s AND guild_id = %s AND status = 'PENDING'
            """,
            (operation_id, guild_id),
        )
        return cursor.rowcount == 1


def complete_backup_restore_operation(
    operation_id: int,
    guild_id: int,
    *,
    status: Literal["SUCCEEDED", "PARTIAL", "FAILED"],
    result: Optional[dict] = None,
    error_code: Optional[str] = None,
) -> bool:
    if status not in {"SUCCEEDED", "PARTIAL", "FAILED"}:
        raise ValueError("invalid_backup_restore_status")
    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE backup_restore_operations
            SET status = %s,
                result_json = %s,
                error_code = %s,
                finished_at = UTC_TIMESTAMP(6)
            WHERE id = %s AND guild_id = %s AND status = 'RUNNING'
            """,
            (
                status,
                json.dumps(result or {}, ensure_ascii=False),
                error_code,
                operation_id,
                guild_id,
            ),
        )
        return cursor.rowcount == 1


def create_backup_snapshot_operation(
    guild_id: int,
    operation_key: str,
    backup_type: Literal["normal", "periodic"],
    requested_name: str,
    *,
    actor_discord_user_id: Optional[int] = None,
    actor_user_id: Optional[int] = None,
) -> int:
    normalized_type = "periodic" if backup_type == "periodic" else "normal"
    normalized_key = str(operation_key or "").strip()
    normalized_name = str(requested_name or "").strip()
    if not normalized_key or len(normalized_key) > 96:
        raise ValueError("invalid_backup_snapshot_operation_key")
    if not normalized_name or len(normalized_name) > 255:
        raise ValueError("invalid_backup_name")

    with pooled_connection() as cursor:
        cursor.execute(
            """
            INSERT IGNORE INTO backup_snapshot_operations
                (guild_id, operation_key, backup_type, requested_name,
                 actor_discord_user_id, actor_user_id, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'PENDING')
            """,
            (
                guild_id,
                normalized_key,
                normalized_type,
                normalized_name,
                actor_discord_user_id,
                actor_user_id,
            ),
        )
        if cursor.rowcount == 1:
            return int(cursor.lastrowid)

        cursor.execute(
            """
            SELECT id
            FROM backup_snapshot_operations
            WHERE guild_id = %s AND operation_key = %s
            LIMIT 1
            """,
            (guild_id, normalized_key),
        )
        existing = cursor.fetchone()
        if existing:
            return int(existing["id"])
        raise RuntimeError("backup_snapshot_operation_not_created")


def get_backup_snapshot_operation(operation_id: int, guild_id: int) -> Optional[dict]:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT id, guild_id, operation_key, backup_type, requested_name,
                   actor_discord_user_id, actor_user_id, status, backup_id,
                   progress_current, progress_total, current_step, result_json,
                   error_code, started_at, finished_at, created_at, updated_at
            FROM backup_snapshot_operations
            WHERE id = %s AND guild_id = %s
            LIMIT 1
            """,
            (operation_id, guild_id),
        )
        return cursor.fetchone()


def mark_backup_snapshot_operation_running(operation_id: int, guild_id: int) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE backup_snapshot_operations
            SET status = 'RUNNING',
                started_at = COALESCE(started_at, UTC_TIMESTAMP(6)),
                current_step = 'STARTING',
                error_code = NULL
            WHERE id = %s AND guild_id = %s AND status = 'PENDING'
            """,
            (operation_id, guild_id),
        )
        return cursor.rowcount == 1


def fail_pending_backup_snapshot_operation(
    operation_id: int,
    guild_id: int,
    error_code: str,
) -> bool:
    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE backup_snapshot_operations
            SET status = 'FAILED',
                error_code = %s,
                current_step = 'FAILED',
                result_json = %s,
                finished_at = UTC_TIMESTAMP(6)
            WHERE id = %s AND guild_id = %s AND status = 'PENDING'
            """,
            (
                str(error_code)[:96],
                json.dumps({"warnings": [str(error_code)]}, ensure_ascii=False),
                operation_id,
                guild_id,
            ),
        )
        return cursor.rowcount == 1


def complete_backup_snapshot_operation(
    operation_id: int,
    guild_id: int,
    *,
    status: Literal["SUCCEEDED", "PARTIAL", "FAILED"],
    backup_id: Optional[int] = None,
    result: Optional[dict] = None,
    error_code: Optional[str] = None,
) -> bool:
    if status not in {"SUCCEEDED", "PARTIAL", "FAILED"}:
        raise ValueError("invalid_backup_snapshot_status")
    with pooled_connection() as cursor:
        cursor.execute(
            """
            UPDATE backup_snapshot_operations
            SET status = %s,
                backup_id = COALESCE(%s, backup_id),
                result_json = %s,
                error_code = %s,
                finished_at = UTC_TIMESTAMP(6)
            WHERE id = %s AND guild_id = %s AND status = 'RUNNING'
            """,
            (
                status,
                backup_id,
                json.dumps(result or {}, ensure_ascii=False),
                error_code,
                operation_id,
                guild_id,
            ),
        )
        return cursor.rowcount == 1


def recover_completed_backup_snapshot_operations() -> int:
    """Reconcile snapshots that were persisted but left RUNNING by finalization failure.

    Recovery is deliberately narrow: a RUNNING operation is completed only when
    it already has a durable COMPLETED/SUCCEEDED step whose backupId still
    belongs to the same guild.
    """
    recovered = 0
    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT o.id, o.guild_id, o.progress_current, o.progress_total,
                   s.detail_json
            FROM backup_snapshot_operations o
            INNER JOIN backup_operation_steps s
                ON s.guild_id = o.guild_id
               AND s.operation_kind = 'SNAPSHOT'
               AND s.operation_id = o.id
               AND s.step_code = 'COMPLETED'
               AND s.step_status = 'SUCCEEDED'
            WHERE o.status = 'RUNNING'
            ORDER BY o.id ASC, s.id DESC
            """
        )
        rows = cursor.fetchall() or []
        seen: set[tuple[int, int]] = set()

        for row in rows:
            operation_id = int(row["id"])
            guild_id = int(row["guild_id"])
            key = (guild_id, operation_id)
            if key in seen:
                continue
            seen.add(key)

            detail = row.get("detail_json")
            if not detail:
                continue
            try:
                parsed = json.loads(detail) if isinstance(detail, str) else detail
                backup_id = int((parsed or {}).get("backupId"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue

            cursor.execute(
                """
                SELECT 1
                FROM backup_discord
                WHERE id = %s AND guild_id = %s
                LIMIT 1
                """,
                (backup_id, guild_id),
            )
            if cursor.fetchone() is None:
                continue

            progress_total = max(
                int(row.get("progress_total") or 0),
                int(row.get("progress_current") or 0),
            )
            cursor.execute(
                """
                UPDATE backup_snapshot_operations
                SET status = 'SUCCEEDED',
                    backup_id = %s,
                    error_code = NULL,
                    current_step = 'RECOVERED',
                    progress_current = %s,
                    progress_total = %s,
                    finished_at = COALESCE(finished_at, UTC_TIMESTAMP(6))
                WHERE id = %s
                  AND guild_id = %s
                  AND status = 'RUNNING'
                """,
                (
                    backup_id,
                    progress_total,
                    progress_total,
                    operation_id,
                    guild_id,
                ),
            )
            if cursor.rowcount != 1:
                continue

            cursor.execute(
                """
                INSERT INTO backup_operation_steps
                    (guild_id, operation_kind, operation_id, step_code,
                     step_status, message, progress_current, progress_total,
                     detail_json)
                VALUES (%s, 'SNAPSHOT', %s, 'RECOVERED', 'SUCCEEDED',
                        %s, %s, %s, %s)
                """,
                (
                    guild_id,
                    operation_id,
                    "Operação reconciliada após confirmar o snapshot persistido.",
                    progress_total,
                    progress_total,
                    json.dumps(
                        {"backupId": backup_id, "reason": "terminal_state_recovery"},
                        ensure_ascii=False,
                    ),
                ),
            )
            recovered += 1
    return recovered


def record_backup_operation_step(
    guild_id: int,
    operation_kind: Literal["SNAPSHOT", "RESTORE"],
    operation_id: int,
    step_code: str,
    step_status: Literal["PENDING", "RUNNING", "SUCCEEDED", "SKIPPED", "FAILED"],
    message: str,
    progress_current: int,
    progress_total: int,
    *,
    detail: Optional[dict] = None,
) -> int:
    normalized_kind = str(operation_kind or "").upper()
    if normalized_kind not in {"SNAPSHOT", "RESTORE"}:
        raise ValueError("invalid_backup_operation_kind")
    normalized_status = str(step_status or "").upper()
    if normalized_status not in {"PENDING", "RUNNING", "SUCCEEDED", "SKIPPED", "FAILED"}:
        raise ValueError("invalid_backup_operation_step_status")
    table = (
        "backup_snapshot_operations"
        if normalized_kind == "SNAPSHOT"
        else "backup_restore_operations"
    )
    current = max(0, int(progress_current))
    total = max(current, int(progress_total))
    code = str(step_code or "STEP")[:96]
    text = str(message or code)[:512]

    with pooled_connection() as cursor:
        cursor.execute(
            f"""
            UPDATE {table}
            SET progress_current = %s,
                progress_total = %s,
                current_step = %s
            WHERE id = %s AND guild_id = %s
            """,
            (current, total, code, operation_id, guild_id),
        )
        if cursor.rowcount != 1:
            cursor.execute(
                f"SELECT 1 FROM {table} WHERE id = %s AND guild_id = %s LIMIT 1",
                (operation_id, guild_id),
            )
            if cursor.fetchone() is None:
                raise LookupError("backup_operation_not_found")
        cursor.execute(
            """
            INSERT INTO backup_operation_steps
                (guild_id, operation_kind, operation_id, step_code, step_status,
                 message, progress_current, progress_total, detail_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                guild_id,
                normalized_kind,
                operation_id,
                code,
                normalized_status,
                text,
                current,
                total,
                json.dumps(detail, ensure_ascii=False) if detail is not None else None,
            ),
        )
        saved_step_id = int(cursor.lastrowid)

    # A database commit must succeed before notifying stream subscribers.
    # This signal is optional and never blocks or aborts backup execution.
    try:
        from core.backup_progress_events import notify_backup_progress
        notify_backup_progress(guild_id, normalized_kind, operation_id, saved_step_id)
    except Exception:
        logger.debug("Non-critical Backup progress notification failed", exc_info=True)
    return saved_step_id


def purge_discord_backup_guild_data(guild_id: int) -> None:
    """Remove all Backup-domain state after its retention deadline expires.

    Community audit logs live outside this domain and intentionally remain.
    """
    with pooled_connection() as cursor:
        cursor.execute(
            "DELETE FROM backup_operation_steps WHERE guild_id = %s",
            (guild_id,),
        )
        cursor.execute(
            "DELETE FROM backup_snapshot_operations WHERE guild_id = %s",
            (guild_id,),
        )
        cursor.execute(
            "DELETE FROM backup_restore_operations WHERE guild_id = %s",
            (guild_id,),
        )
        cursor.execute(
            "DELETE FROM backup_discord WHERE guild_id = %s",
            (guild_id,),
        )
        cursor.execute(
            "DELETE FROM backup_server_settings WHERE guild_id = %s",
            (guild_id,),
        )
        cursor.execute(
            "DELETE FROM backup_guild_locks WHERE guild_id = %s",
            (guild_id,),
        )


def purge_expired_discord_backup_guilds(
    *,
    limit: int = 100,
    active_guild_ids: Optional[set[int]] = None,
) -> list[int]:
    """Purge expired Backup data, never deleting a guild observed live by Coddy."""
    bounded_limit = max(1, min(int(limit), 1000))
    active_ids = sorted({int(item) for item in (active_guild_ids or set())})
    exclusion = ""
    params: tuple = ()
    if active_ids:
        placeholders = ", ".join(["%s"] * len(active_ids))
        exclusion = f" AND guild_id NOT IN ({placeholders})"
        params = tuple(active_ids)

    with pooled_connection() as cursor:
        cursor.execute(
            f"""
            SELECT guild_id
            FROM backup_server_settings
            WHERE purge_after IS NOT NULL
              AND purge_after <= UTC_TIMESTAMP(6)
              {exclusion}
            ORDER BY purge_after ASC
            LIMIT {bounded_limit}
            """,
            params,
        )
        guild_ids = [int(row["guild_id"]) for row in (cursor.fetchall() or [])]

    for expired_guild_id in guild_ids:
        purge_discord_backup_guild_data(expired_guild_id)
    return guild_ids


def create_discord_backup_snapshot(
    guild_id: int,
    original_name: str,
    roles: list[dict],
    channels: list[dict],
    overwrites: list[dict],
    backup_type: Literal["normal", "periodic"] = "normal",
    *,
    created_by_discord_user_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
) -> int:
    """Persist one structural snapshot atomically, then prune retention.

    Snapshot rows are written before retention pruning. Any failure rolls the
    transaction back, so an unsuccessful capture cannot delete older backups.
    """

    normalized_backup_type = "periodic" if backup_type == "periodic" else "normal"
    normalized_name = str(original_name or "").strip()
    if not normalized_name or len(normalized_name) > 255:
        raise ValueError("invalid_backup_name")
    if any(ord(char) < 32 and char not in "\t" for char in normalized_name):
        raise ValueError("invalid_backup_name")

    normalized_key = str(idempotency_key or "").strip() or None
    if normalized_key is not None and len(normalized_key) > 96:
        raise ValueError("invalid_backup_idempotency_key")

    with pooled_connection() as cursor:
        if normalized_key is not None:
            cursor.execute(
                """
                SELECT id
                FROM backup_discord
                WHERE guild_id = %s AND idempotency_key = %s
                LIMIT 1
                """,
                (guild_id, normalized_key),
            )
            existing = cursor.fetchone()
            if existing:
                return int(existing["id"])

        # V1 policy is intentionally fixed: one manual and one periodic
        # snapshot per guild. The previous snapshot is removed only after the
        # new one has been persisted successfully in this transaction.
        backup_limit = 1

        try:
            cursor.execute(
                """
                INSERT INTO backup_discord
                    (guild_id, original_name, backup_type,
                     created_by_discord_user_id, idempotency_key)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    guild_id,
                    normalized_name,
                    normalized_backup_type,
                    int(created_by_discord_user_id)
                    if created_by_discord_user_id is not None else None,
                    normalized_key,
                ),
            )
        except IntegrityError:
            if normalized_key is None:
                raise
            cursor.execute(
                """
                SELECT id
                FROM backup_discord
                WHERE guild_id = %s AND idempotency_key = %s
                LIMIT 1
                """,
                (guild_id, normalized_key),
            )
            existing = cursor.fetchone()
            if not existing:
                raise
            return int(existing["id"])

        backup_id = int(cursor.lastrowid)

        role_pk_by_discord_id: dict[int, int] = {}
        for role_data in roles:
            cursor.execute(
                """
                INSERT INTO backup_roles
                    (backup_id, discord_id, name, color, permissions, position, hoist, mentionable)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    backup_id,
                    int(role_data["discord_id"]),
                    role_data["name"],
                    int(role_data["color"]),
                    int(role_data["permissions"]),
                    int(role_data["position"]),
                    int(bool(role_data["hoist"])),
                    int(bool(role_data["mentionable"])),
                ),
            )
            role_pk_by_discord_id[int(role_data["discord_id"])] = int(cursor.lastrowid)

        channel_pk_by_discord_id: dict[int, int] = {}
        for channel_data in channels:
            cursor.execute(
                """
                INSERT INTO backup_channels
                    (backup_id, discord_id, parent_id, name, type, position, topic, nsfw)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    backup_id,
                    int(channel_data["discord_id"]),
                    int(channel_data["parent_id"]) if channel_data.get("parent_id") else None,
                    channel_data["name"],
                    int(channel_data["type"]),
                    int(channel_data["position"]) if channel_data.get("position") is not None else None,
                    channel_data.get("topic"),
                    int(channel_data["nsfw"]) if channel_data.get("nsfw") is not None else None,
                ),
            )
            channel_pk_by_discord_id[int(channel_data["discord_id"])] = int(cursor.lastrowid)

        for overwrite_data in overwrites:
            channel_pk = channel_pk_by_discord_id.get(
                int(overwrite_data["channel_discord_id"])
            )
            target_type = str(
                overwrite_data.get("target_type") or "ROLE"
            ).upper()
            target_discord_id = int(
                overwrite_data.get(
                    "target_discord_id",
                    overwrite_data.get("role_discord_id"),
                )
            )
            role_pk = (
                role_pk_by_discord_id.get(target_discord_id)
                if target_type == "ROLE"
                else None
            )
            if channel_pk is None:
                continue
            if target_type == "ROLE" and role_pk is None:
                continue
            if target_type not in {"ROLE", "EVERYONE", "MANAGED_ROLE"}:
                continue
            cursor.execute(
                """
                INSERT INTO backup_overwrites
                    (channel_id, role_name, target_type, target_discord_id,
                     role_id, allow_bits, deny_bits)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    channel_pk,
                    overwrite_data["role_name"],
                    target_type,
                    target_discord_id,
                    role_pk,
                    int(overwrite_data["allow_bits"]),
                    int(overwrite_data["deny_bits"]),
                ),
            )

        cursor.execute(
            """
            SELECT id
            FROM backup_discord
            WHERE guild_id = %s AND backup_type = %s
            ORDER BY id DESC
            """,
            (guild_id, normalized_backup_type),
        )
        existing_ids = [int(row["id"]) for row in (cursor.fetchall() or [])]
        for backup_id_to_delete in existing_ids[backup_limit:]:
            cursor.execute(
                "DELETE FROM backup_discord WHERE id = %s AND guild_id = %s",
                (backup_id_to_delete, guild_id),
            )

        return backup_id

def create_discord_backup(guild_id: int, original_name: str) -> int:
    """Compat: cria apenas o registro mestre de backup e retorna o ID."""

    return create_discord_backup_snapshot(
        guild_id=guild_id,
        original_name=original_name,
        roles=[],
        channels=[],
        overwrites=[],
        backup_type="normal",
    )


def create_backup_role(
    backup_id: int,
    discord_id: int,
    name: str,
    color: int,
    permissions: int,
    position: int,
    hoist: bool,
    mentionable: bool,
) -> int:
    """Compat: insere cargo e retorna ID interno."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            INSERT INTO backup_roles
                (backup_id, discord_id, name, color, permissions, position, hoist, mentionable)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                backup_id,
                discord_id,
                name,
                color,
                permissions,
                position,
                int(hoist),
                int(mentionable),
            ),
        )
        return int(cursor.lastrowid)


def create_backup_channel(
    backup_id: int,
    discord_id: int,
    parent_id: Optional[int],
    name: str,
    channel_type: int,
    position: Optional[int],
    topic: Optional[str],
    nsfw: Optional[bool],
) -> int:
    """Compat: insere canal/categoria e retorna ID interno."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            INSERT INTO backup_channels
                (backup_id, discord_id, parent_id, name, type, position, topic, nsfw)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                backup_id,
                discord_id,
                parent_id,
                name,
                channel_type,
                position,
                topic,
                int(nsfw) if nsfw is not None else None,
            ),
        )
        return int(cursor.lastrowid)


def create_backup_overwrite(
    channel_id: int,
    role_name: str,
    role_id: int,
    allow_bits: int,
    deny_bits: int,
) -> int:
    """Compat: insert a normal-role overwrite using the canonical target shape."""

    with pooled_connection() as cursor:
        cursor.execute(
            "SELECT discord_id FROM backup_roles WHERE id = %s LIMIT 1",
            (role_id,),
        )
        role = cursor.fetchone()
        if not role:
            raise ValueError("backup_role_not_found")
        cursor.execute(
            """
            INSERT INTO backup_overwrites
                (channel_id, role_name, target_type, target_discord_id,
                 role_id, allow_bits, deny_bits)
            VALUES
                (%s, %s, 'ROLE', %s, %s, %s, %s)
            """,
            (
                channel_id,
                role_name,
                int(role["discord_id"]),
                role_id,
                allow_bits,
                deny_bits,
            ),
        )
        return int(cursor.lastrowid)


def list_discord_backups(guild_id: int) -> list[dict]:
    """List snapshot metadata and structural counts for exactly one guild."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT
                bd.id,
                bd.original_name,
                bd.backup_type,
                bd.created_by_discord_user_id,
                bd.created_at,
                COUNT(DISTINCT br.id) AS role_count,
                COUNT(DISTINCT bc.id) AS channel_count,
                COUNT(DISTINCT bo.id) AS overwrite_count
            FROM backup_discord bd
            LEFT JOIN backup_roles br ON br.backup_id = bd.id
            LEFT JOIN backup_channels bc ON bc.backup_id = bd.id
            LEFT JOIN backup_overwrites bo ON bo.channel_id = bc.id
            WHERE bd.guild_id = %s
            GROUP BY
                bd.id,
                bd.original_name,
                bd.backup_type,
                bd.created_by_discord_user_id,
                bd.created_at
            ORDER BY bd.id DESC
            """,
            (guild_id,),
        )
        return cursor.fetchall() or []

def get_discord_backup_payload(backup_id: int, guild_id: int) -> Optional[dict]:
    """Return a structural snapshot only when it belongs to the requested guild."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT id, guild_id, original_name, backup_type,
                   created_by_discord_user_id, created_at
            FROM backup_discord
            WHERE id = %s AND guild_id = %s
            LIMIT 1
            """,
            (backup_id, guild_id),
        )
        backup_row = cursor.fetchone()
        if not backup_row:
            return None

        cursor.execute(
            """
            SELECT id, discord_id, name, color, permissions, position, hoist, mentionable
            FROM backup_roles
            WHERE backup_id = %s
              AND NOT (name = '@everyone' AND position = 0)
            ORDER BY position ASC, id ASC
            """,
            (backup_id,),
        )
        roles = cursor.fetchall() or []

        cursor.execute(
            """
            SELECT id, discord_id, parent_id, name, type, position, topic, nsfw
            FROM backup_channels
            WHERE backup_id = %s
            ORDER BY type DESC, position ASC, id ASC
            """,
            (backup_id,),
        )
        channels = cursor.fetchall() or []

        cursor.execute(
            """
            SELECT
                bo.id,
                bo.channel_id,
                bo.role_name,
                CASE
                    WHEN bo.target_type = 'ROLE'
                         AND br.name = '@everyone'
                         AND br.position = 0
                        THEN 'EVERYONE'
                    ELSE bo.target_type
                END AS target_type,
                COALESCE(bo.target_discord_id, br.discord_id) AS target_discord_id,
                CASE
                    WHEN bo.target_type = 'ROLE'
                         AND br.name = '@everyone'
                         AND br.position = 0
                        THEN NULL
                    ELSE bo.role_id
                END AS role_id,
                bo.allow_bits,
                bo.deny_bits
            FROM backup_overwrites bo
            INNER JOIN backup_channels bc ON bc.id = bo.channel_id
            LEFT JOIN backup_roles br ON br.id = bo.role_id
            WHERE bc.backup_id = %s
            ORDER BY bo.id ASC
            """,
            (backup_id,),
        )
        overwrites = cursor.fetchall() or []

        return {
            "backup": backup_row,
            "roles": roles,
            "channels": channels,
            "overwrites": overwrites,
        }

def get_discord_backup_settings(
    guild_id: int,
    cursor: Optional[MySQLCursorAbstract] = None,
) -> dict:
    """Read guild Backup policy without making the runtime its authority."""

    def _read_settings(active_cursor: MySQLCursorAbstract) -> dict:
        active_cursor.execute(
            """
            SELECT guild_id, max_normal_backups, max_periodic_backups,
                   periodic_backups_enabled, periodicity_minutes, periodicity_frequency,
                   bot_removed_at, purge_after
            FROM backup_server_settings
            WHERE guild_id = %s
            LIMIT 1
            """,
            (guild_id,),
        )
        row = active_cursor.fetchone()
        if not row:
            return {
                "guild_id": int(guild_id),
                "max_normal_backups": 1,
                "max_periodic_backups": 1,
                "periodic_backups_enabled": False,
                "periodicity_minutes": 10080,
                "periodicity_frequency": "weekly",
                "bot_removed_at": None,
                "purge_after": None,
            }

        frequency = str(row.get("periodicity_frequency") or "weekly").lower()
        if frequency not in {"daily", "weekly", "monthly"}:
            frequency = "weekly"
        return {
            "guild_id": int(row["guild_id"]),
            "max_normal_backups": 1,
            "max_periodic_backups": 1,
            "periodic_backups_enabled": bool(row.get("periodic_backups_enabled")),
            "periodicity_minutes": max(1, int(row.get("periodicity_minutes") or 10080)),
            "periodicity_frequency": frequency,
            "bot_removed_at": row.get("bot_removed_at"),
            "purge_after": row.get("purge_after"),
        }

    if cursor is not None:
        return _read_settings(cursor)
    with pooled_connection() as local_cursor:
        return _read_settings(local_cursor)

def update_discord_backup_settings(
    guild_id: int,
    *,
    max_normal_backups: Optional[int] = None,
    max_periodic_backups: Optional[int] = None,
    periodic_backups_enabled: Optional[bool] = None,
    periodicity_minutes: Optional[int] = None,
    periodicity_frequency: Optional[Literal["daily", "weekly", "monthly"]] = None,
) -> dict:
    """Legacy compatibility writer with frequency as the canonical rule."""

    # Retention is no longer configurable in V1: one manual and one
    # periodic snapshot are the canonical slots for every guild.
    updates: dict[str, Any] = {
        "max_normal_backups": 1,
        "max_periodic_backups": 1,
    }
    if periodic_backups_enabled is not None:
        updates["periodic_backups_enabled"] = int(bool(periodic_backups_enabled))

    minutes_by_frequency = {
        "daily": 1440,
        "weekly": 10080,
        "monthly": 43200,
    }
    if periodicity_frequency is not None:
        normalized_frequency = str(periodicity_frequency).lower()
        if normalized_frequency not in minutes_by_frequency:
            raise ValueError("invalid_backup_frequency")
        updates["periodicity_frequency"] = normalized_frequency
        updates["periodicity_minutes"] = minutes_by_frequency[normalized_frequency]
    elif periodicity_minutes is not None:
        legacy_minutes = max(1, int(periodicity_minutes))
        normalized_frequency = (
            "daily"
            if legacy_minutes <= 1440
            else "monthly"
            if legacy_minutes >= 43200
            else "weekly"
        )
        updates["periodicity_frequency"] = normalized_frequency
        updates["periodicity_minutes"] = minutes_by_frequency[normalized_frequency]

    with pooled_connection() as cursor:
        cursor.execute(
            "INSERT IGNORE INTO backup_server_settings (guild_id) VALUES (%s)",
            (guild_id,),
        )
        if updates:
            set_clause = ", ".join(f"{column} = %s" for column in updates.keys())
            cursor.execute(
                f"UPDATE backup_server_settings SET {set_clause} WHERE guild_id = %s",
                (*updates.values(), guild_id),
            )
        return get_discord_backup_settings(guild_id, cursor=cursor)

def list_due_periodic_backup_guild_ids() -> list[int]:
    """Return guilds whose enabled periodic policy is due and retains snapshots."""

    with pooled_connection() as cursor:
        cursor.execute(
            """
            SELECT bss.guild_id, bss.periodicity_frequency,
                   MAX(bd.created_at) AS last_backup_created_at
            FROM backup_server_settings bss
            LEFT JOIN backup_discord bd
              ON bd.guild_id = bss.guild_id
             AND bd.backup_type = 'periodic'
            WHERE bss.periodic_backups_enabled = 1
              AND bss.purge_after IS NULL
            GROUP BY bss.guild_id, bss.periodicity_frequency
            """
        )
        rows = cursor.fetchall() or []

    now = datetime.utcnow()
    due_guild_ids: list[int] = []
    for row in rows:
        guild_id = int(row["guild_id"])
        frequency = str(row.get("periodicity_frequency") or "weekly").lower()
        if frequency not in {"daily", "weekly", "monthly"}:
            frequency = "weekly"

        last_backup_created_at = row.get("last_backup_created_at")
        if not last_backup_created_at:
            due_guild_ids.append(guild_id)
            continue

        if frequency == "daily":
            next_backup_at = last_backup_created_at.replace(microsecond=0) + timedelta(days=1)
        elif frequency == "weekly":
            next_backup_at = last_backup_created_at.replace(microsecond=0) + timedelta(weeks=1)
        else:
            next_month = last_backup_created_at.month + 1
            next_year = last_backup_created_at.year
            if next_month > 12:
                next_month = 1
                next_year += 1
            max_day_of_next_month = calendar.monthrange(next_year, next_month)[1]
            next_day = min(last_backup_created_at.day, max_day_of_next_month)
            next_backup_at = last_backup_created_at.replace(
                year=next_year,
                month=next_month,
                day=next_day,
                microsecond=0,
            )
        if next_backup_at <= now:
            due_guild_ids.append(guild_id)

    return due_guild_ids

