"""No live DB: regression checks for three legacy startup CREATE TABLE sites.

The sample metadata below mirrors the production MariaDB 10.6 schema-only
export of 2026-10-19; no secrets or table rows are included.
"""

import ast
from pathlib import Path

import pytest

from core.runtime_schema import (
    REQUIRED_COLUMNS, REQUIRED_UNIQUE_KEYS, check_startup_schema_table,
)


SNAPSHOT_COLUMNS = {
    "active_call_logs": {
        "id", "server_guild_id", "voice_channel_id", "log_message_id",
        "payload_json", "updated_at",
    },
    "allowed_feature_channels": {
        "id", "server_guild_id", "feature_key", "channel_id", "created_at",
    },
    "minigame_scores": {
        "id", "user_id", "server_guild_id", "game_name",
        "points", "won", "created_at",
    },
}
SNAPSHOT_UNIQUE = {
    "active_call_logs": ("server_guild_id", "voice_channel_id"),
    "allowed_feature_channels": ("server_guild_id", "feature_key", "channel_id"),
    "minigame_scores": ("id",),
}


class MetadataCursor:
    def __init__(self, table, *, missing_column=None, non_unique=False):
        self.table = table
        self.missing_column = missing_column
        self.non_unique = non_unique
        self.queries = []
        self.rows = []

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        assert sql.lstrip().startswith("SELECT ")
        assert "information_schema." in sql
        assert params == (self.table,)
        if "information_schema.COLUMNS" in sql:
            self.rows = [
                {"TABLE_NAME": self.table, "COLUMN_NAME": col}
                for col in SNAPSHOT_COLUMNS[self.table]
                if col != self.missing_column
            ]
        elif "information_schema.STATISTICS" in sql:
            self.rows = [
                {
                    "TABLE_NAME": self.table,
                    "INDEX_NAME": "production_unique",
                    "NON_UNIQUE": int(self.non_unique),
                    "SEQ_IN_INDEX": idx,
                    "COLUMN_NAME": col,
                }
                for idx, col in enumerate(SNAPSHOT_UNIQUE[self.table], 1)
            ]
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self.rows


@pytest.mark.parametrize("table", sorted(SNAPSHOT_COLUMNS))
def test_expected_columns_match_production_snapshot(table):
    assert REQUIRED_COLUMNS[table] == SNAPSHOT_COLUMNS[table]
    assert REQUIRED_UNIQUE_KEYS[table] == SNAPSHOT_UNIQUE[table]


@pytest.mark.parametrize("table", sorted(SNAPSHOT_COLUMNS))
def test_production_snapshot_passes_select_only_preflight(table):
    cursor = MetadataCursor(table)
    check_startup_schema_table(cursor, table)
    assert len(cursor.queries) == 2


@pytest.mark.parametrize("table", sorted(SNAPSHOT_COLUMNS))
def test_schema_drift_missing_column_fails_closed(table):
    missing = sorted(SNAPSHOT_COLUMNS[table])[0]
    cursor = MetadataCursor(table, missing_column=missing)
    with pytest.raises(RuntimeError, match="coddy_startup_schema_missing_columns"):
        check_startup_schema_table(cursor, table)
    assert len(cursor.queries) == 1


@pytest.mark.parametrize("table", sorted(SNAPSHOT_COLUMNS))
def test_missing_uniqueness_fails_closed(table):
    cursor = MetadataCursor(table, non_unique=True)
    with pytest.raises(RuntimeError, match="coddy_startup_schema_missing_unique_keys"):
        check_startup_schema_table(cursor, table)


def test_minigame_events_require_guild_column_but_no_new_guild_unique_key():
    assert "server_guild_id" in REQUIRED_COLUMNS["minigame_scores"]
    assert REQUIRED_UNIQUE_KEYS["minigame_scores"] == ("id",)


def test_three_initializers_are_readonly_and_only_called_on_real_startup():
    root = Path(__file__).resolve().parents[1]
    db = (root / "core/database.py").read_text(encoding="utf-8")
    tree = ast.parse(db)
    expected = (
        "initialize_active_call_logs_table",
        "initialize_allowed_feature_channels_table",
        "initialize_minigame_scores_table",
    )
    for name in expected:
        fn = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == name
        )
        body = ast.get_source_segment(db, fn)
        assert "check_startup_schema_table(cursor," in body
        for stmt in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "DROP INDEX"):
            assert stmt not in body

    top_level_calls = {
        n.value.func.id for n in tree.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    }
    assert not (top_level_calls & set(expected))

    service = (root / "message_services/discord_service.py").read_text(
        encoding="utf-8"
    )
    startup = next(
        n for n in ast.parse(service).body
        if isinstance(n, ast.FunctionDef) and n.name == "run_discord_client"
    )
    calls = [
        n.value.func.id for n in startup.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    ]
    assert calls[-9:-6] == list(expected)
    assert calls[-6:-4] == [
        "initialize_boss_event_tables",
        "initialize_expedition_tables",
    ]
    assert calls[-4:] == [
        "initialize_form_tables",
        "initialize_user_economy_table",
        "initialize_bot_sensitive_permission_whitelist_table",
        "initialize_config_levels_table",
    ]
