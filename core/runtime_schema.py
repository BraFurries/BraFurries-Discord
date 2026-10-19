"""Startup schema contract audited against a production MariaDB 10.6 DDL dump.

Column names and unique keys were checked against the schema-only export
from 2026-10-19; no production rows are available here.
"""
from core.schema_preflight import check_schema_readonly

REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "user_economy": frozenset({"id", "server_guild_id", "user_id", "bank_balance"}),
    "bot_sensitive_permission_whitelist": frozenset({
        "id", "server_guild_id", "actor_id", "actor_type", "permission_name", "created_at",
    }),
    "config_levels": frozenset({
        "id", "server_guild_id", "clear_on_exit", "levelup_warning",
        "levelup_warning_channel", "level_up_message", "multiplier",
        "phase1_k", "phase1_p", "phase1_b", "daily_combo",
        "combo_multiplier", "xp_base_per_min", "voice_social_bonus_pct",
        "voice_social_bonus_min_humans", "voice_diminishing_window1_minutes",
        "voice_diminishing_window2_minutes", "voice_diminishing_factor2",
        "voice_diminishing_factor3", "voice_daily_cap_xp",
        "text_daily_cap_xp", "global_daily_cap_xp", "text_xp_enabled",
        "voice_xp_enabled", "text_xp_base_min", "text_xp_base_max",
        "text_xp_cooldown_min_seconds", "text_xp_cooldown_max_seconds",
        "level_reconcile_required", "level_reconcile_requested_at",
        "level_reconciled_at",
    }),
}
REQUIRED_UNIQUE_KEYS: dict[str, tuple[str, ...]] = {
    "user_economy": ("user_id", "server_guild_id"),
    "bot_sensitive_permission_whitelist": (
        "server_guild_id", "actor_id", "actor_type", "permission_name"
    ),
    "config_levels": ("server_guild_id",),
}


def check_startup_schema_table(cursor, table: str) -> None:
    """Fail closed on missing Flyway-owned objects. Never repair the schema."""
    if table not in REQUIRED_COLUMNS:
        raise ValueError(f"unsupported_startup_schema_table:{table}")
    check_schema_readonly(
        cursor,
        required_columns={table: REQUIRED_COLUMNS[table]},
        required_unique_keys={table: REQUIRED_UNIQUE_KEYS[table]},
        error_prefix="coddy_startup_schema",
        guidance="check BraFurries-Database migrations and production schema before loading Coddy",
    )
