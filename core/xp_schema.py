"""Read-only preflight for XP voice persistence owned by BraFurries-Database.

Canonical schema: Flyway V20261004_001__formalize_xp_leveling_schema.sql.
No runtime CREATE / ALTER / RENAME or table-repair fallback is permitted.
"""

from core.schema_preflight import check_schema_readonly


REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "voice_sessions": frozenset({
        "server_guild_id", "discord_user_id", "voice_channel_id",
        "started_at", "last_tick_at", "is_eligible", "is_self_muted",
        "is_self_deafened", "is_server_muted", "is_server_deafened",
    }),
    "voice_xp_audit_log": frozenset({
        "id", "server_guild_id", "discord_user_id", "voice_channel_id",
        "event_type", "xp_amount", "eligible_seconds",
        "ineligible_seconds", "meta_json", "created_at",
    }),
    "voice_xp_daily_metrics": frozenset({
        "id", "metric_day", "server_guild_id", "modality",
        "voice_channel_id", "total_xp", "grants_count", "blocks_count",
        "eligible_seconds", "ineligible_seconds",
    }),
}

REQUIRED_UNIQUE_KEYS: dict[str, tuple[str, ...]] = {
    "voice_sessions": ("server_guild_id", "discord_user_id"),
    "voice_xp_daily_metrics": (
        "metric_day", "server_guild_id", "modality", "voice_channel_id"
    ),
}


def check_voice_xp_schema(cursor) -> None:
    """Fail closed on missing XP tables/columns/keys; never mutate a schema."""
    check_schema_readonly(
        cursor,
        required_columns=REQUIRED_COLUMNS,
        required_unique_keys=REQUIRED_UNIQUE_KEYS,
        error_prefix="xp_voice_schema",
        guidance="validate Database/Flyway V20261004_001 before loading Coddy",
    )
