"""Read-only preflight for XP voice persistence owned by BraFurries-Database.

Canonical schema: Flyway V20261004_001__formalize_xp_leveling_schema.sql.
No runtime CREATE / ALTER / RENAME or table-repair fallback is permitted.
"""

from collections import defaultdict


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
    """Fail closed on missing tables/columns/keys; never mutate a schema."""
    table_names = tuple(sorted(REQUIRED_COLUMNS))
    placeholders = ", ".join("%s" for _ in table_names)

    cursor.execute(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() "
        f"AND TABLE_NAME IN ({placeholders})",
        table_names,
    )
    columns: dict[str, set[str]] = defaultdict(set)
    for row in cursor.fetchall() or []:
        columns[str(row["TABLE_NAME"])].add(str(row["COLUMN_NAME"]))

    missing = [
        f"{table}.{column}"
        for table, required in sorted(REQUIRED_COLUMNS.items())
        for column in sorted(required - columns[table])
    ]
    if missing:
        raise RuntimeError(
            "xp_voice_schema_missing_columns:"
            + ",".join(missing)
            + "; validate Database/Flyway V20261004_001 before loading Coddy"
        )

    cursor.execute(
        "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME "
        "FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() "
        f"AND TABLE_NAME IN ({placeholders}) "
        "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX",
        table_names,
    )
    indexes: dict[tuple[str, str], list[tuple[int, str, int]]] = defaultdict(list)
    for row in cursor.fetchall() or []:
        indexes[(str(row["TABLE_NAME"]), str(row["INDEX_NAME"]))].append(
            (
                int(row["SEQ_IN_INDEX"]),
                str(row["COLUMN_NAME"]),
                int(row["NON_UNIQUE"]),
            )
        )

    unique_sets: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    for (table, _name), segments in indexes.items():
        if segments and all(non_unique == 0 for _, _, non_unique in segments):
            unique_sets[table].add(
                tuple(col for _, col, _ in sorted(segments))
            )

    missing_keys = [
        table
        for table, required in REQUIRED_UNIQUE_KEYS.items()
        if required not in unique_sets[table]
    ]
    if missing_keys:
        raise RuntimeError(
            "xp_voice_schema_missing_unique_keys:"
            + ",".join(sorted(missing_keys))
            + "; validate Database/Flyway V20261004_001 before loading Coddy"
        )
