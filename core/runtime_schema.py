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
    "active_call_logs": frozenset({
        "id", "server_guild_id", "voice_channel_id", "log_message_id",
        "payload_json", "updated_at",
    }),
    "allowed_feature_channels": frozenset({
        "id", "server_guild_id", "feature_key", "channel_id", "created_at",
    }),
    "minigame_scores": frozenset({
        "id", "user_id", "server_guild_id", "game_name",
        "points", "won", "created_at",
    }),
    # These four schemas use the historical guild_id tenant column, not
    # server_guild_id. Production dump (2026-10-19) confirms each unique key.
    "community_boss_event": frozenset({
        "id", "guild_id", "boss_name", "max_hp", "current_hp", "active",
        "attack_cooldown_seconds", "created_at", "finished_at",
    }),
    "community_boss_contribution": frozenset({
        "id", "guild_id", "user_id", "total_damage", "updated_at",
    }),
    "community_boss_attack_cooldown": frozenset({
        "id", "guild_id", "user_id", "cooldown_until",
    }),
    "community_expedition": frozenset({
        "id", "guild_id", "active", "started_at", "ends_at",
        "participants_json", "finalized_at",
    }),
    "community_discord": frozenset({
        "id", "community_id", "guild_id", "active", "discord_admin_id",
        "users_quantity", "membership_sync_state",
        "last_full_reconciliation_at", "last_membership_event_at",
    }),
    "user_bans": frozenset({
        "id", "user_id", "community_id", "registered_at", "date",
        "reason", "revoked_at",
    }),
    "user_level": frozenset({
        "id", "server_guild_id", "user_id", "total_xp", "current_level",
        "xp_awarded_voice_today", "xp_awarded_voice_day",
        "xp_awarded_text_today", "xp_awarded_text_day",
        "xp_awarded_today", "xp_awarded_day",
    }),
    # Legacy birthday records are global by users.id (not guild-owned).
    "user_birthday": frozenset({
        "user_id", "birth_date", "post_informed_date", "verified",
        "registered", "registered_at", "18_plus",
    }),
    # Guild-specific settings already own the trending_presence flag.
    "config_server_settings": frozenset({
        "id", "server_guild_id", "trending_presence_enabled",
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
    "active_call_logs": ("server_guild_id", "voice_channel_id"),
    "allowed_feature_channels": ("server_guild_id", "feature_key", "channel_id"),
    # Minigame is an append-only, guild-filtered event table. Production has
    # no unique key per guild; enforce only its existing id primary key.
    "minigame_scores": ("id",),
    "user_birthday": ("user_id",),
    "config_server_settings": ("server_guild_id",),
    # MariaDB production dump: community_discord has a unique guild_id,
    # user_level is unique for each user+guild, and bans are historical rows.
    "community_discord": ("guild_id",),
    "user_bans": ("id",),
    "user_level": ("user_id", "server_guild_id"),
    "community_boss_event": ("guild_id",),
    "community_boss_contribution": ("guild_id", "user_id"),
    "community_boss_attack_cooldown": ("guild_id", "user_id"),
    "community_expedition": ("guild_id",),
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

# Inventory is scoped by community_id (tenant). The production MariaDB
# 10.6 export contains service_metadata as JSON-validating LONGTEXT, not a
# native JSON column; no type conversion or schema repair belongs in Coddy.
INVENTORY_REQUIRED_COLUMNS = {
    "user_inventory": frozenset({
        "id", "community_id", "store_item_id", "owner_user_id",
        "quantity", "used_in", "valid_until", "service_metadata",
    }),
}
INVENTORY_REQUIRED_UNIQUE_KEYS = {"user_inventory": ("id",)}


def check_inventory_schema(cursor) -> None:
    """Check the existing inventory contract lazily, using metadata SELECTs only."""
    check_schema_readonly(
        cursor,
        required_columns=INVENTORY_REQUIRED_COLUMNS,
        required_unique_keys=INVENTORY_REQUIRED_UNIQUE_KEYS,
        error_prefix="coddy_inventory_schema",
        guidance="review BraFurries-Database migrations before using inventory services",
    )


# Production form schema intentionally keeps nullable form_flows.server_guild_id
# for legacy records. Do not silently enforce NOT NULL or re-scope old flows.
FORM_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "form_flows": frozenset({
        "id", "name", "type", "server_guild_id", "target_channel_id",
        "approved_target_channel_id", "rejected_target_channel_id",
        "rejection_feedback_enabled", "created_at",
    }),
    "form_questions": frozenset({
        "id", "flow_id", "question_text", "placeholder_text", "position", "required",
    }),
    "form_submissions": frozenset({
        "id", "flow_id", "user_id", "guild_id", "channel_id", "message_id",
        "status", "created_at",
    }),
    "form_decisions": frozenset({
        "id", "submission_id", "decision", "decided_by", "decided_at",
    }),
    "form_published_messages": frozenset({
        "id", "flow_id", "message_id", "channel_id", "guild_id", "created_at",
    }),
}

# Do not invent per-guild uniqueness for the legacy form tables.
# Questions and decisions are tenant-scoped through their parent flow/submission.
FORM_REQUIRED_UNIQUE_KEYS: dict[str, tuple[str, ...]] = {
    "form_flows": ("id",),
    "form_questions": ("id",),
    "form_submissions": ("id",),
    "form_decisions": ("id",),
    "form_published_messages": ("message_id",),
}


def check_form_schema(cursor) -> None:
    """Read-only verification of the existing forms schema; no auto-migration."""
    check_schema_readonly(
        cursor,
        required_columns=FORM_REQUIRED_COLUMNS,
        required_unique_keys=FORM_REQUIRED_UNIQUE_KEYS,
        error_prefix="coddy_form_schema",
        guidance="review BraFurries-Database migrations and legacy form data",
    )
