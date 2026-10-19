"""Import-side-effect-free checks for historical identity/ban/XP column DDL.

The expected columns/index orders below are independently transcribed from
the 2026-10-19 MariaDB production schema-only dump. No database connection
or credential is required.
"""

import ast
from pathlib import Path

import pytest

from core.runtime_schema import (
    REQUIRED_COLUMNS, REQUIRED_UNIQUE_KEYS, check_startup_schema_table,
)


PRODUCTION_COLUMNS = {
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
}

PRODUCTION_UNIQUE = {
    "community_discord": ("guild_id",),
    # Ban rows are scoped by their community_id; each ban record gets its own id.
    "user_bans": ("id",),
    "user_level": ("user_id", "server_guild_id"),
}


class FakeMetadataCursor:
    def __init__(self, table, *, missing_column=None, non_unique=False):
        self.table = table
        self.missing_column = missing_column
        self.non_unique = non_unique
        self.statements = []
        self.rows = []

    def execute(self, statement, params=()):
        assert statement.lstrip().startswith("SELECT ")
        assert "information_schema." in statement
        assert params == (self.table,)
        self.statements.append((statement, params))
        if "information_schema.COLUMNS" in statement:
            self.rows = [
                {"TABLE_NAME": self.table, "COLUMN_NAME": col}
                for col in PRODUCTION_COLUMNS[self.table]
                if col != self.missing_column
            ]
        elif "information_schema.STATISTICS" in statement:
            self.rows = [
                {
                    "TABLE_NAME": self.table,
                    "INDEX_NAME": "expected_key",
                    "NON_UNIQUE": int(self.non_unique),
                    "SEQ_IN_INDEX": i,
                    "COLUMN_NAME": col,
                }
                for i, col in enumerate(PRODUCTION_UNIQUE[self.table], 1)
            ]
        else:
            raise AssertionError(statement)

    def fetchall(self):
        return self.rows


@pytest.mark.parametrize("table", sorted(PRODUCTION_COLUMNS))
def test_column_and_key_contracts_match_production_snapshot(table):
    assert REQUIRED_COLUMNS[table] == PRODUCTION_COLUMNS[table]
    assert REQUIRED_UNIQUE_KEYS[table] == PRODUCTION_UNIQUE[table]


@pytest.mark.parametrize("table", sorted(PRODUCTION_COLUMNS))
def test_readonly_preflight_accepts_production_schema(table):
    cursor = FakeMetadataCursor(table)
    check_startup_schema_table(cursor, table)
    assert len(cursor.statements) == 2


@pytest.mark.parametrize("table", sorted(PRODUCTION_COLUMNS))
def test_column_drift_fails_closed(table):
    cursor = FakeMetadataCursor(table, missing_column=sorted(PRODUCTION_COLUMNS[table])[0])
    with pytest.raises(RuntimeError, match="coddy_startup_schema_missing_columns"):
        check_startup_schema_table(cursor, table)
    assert len(cursor.statements) == 1


@pytest.mark.parametrize("table", sorted(PRODUCTION_COLUMNS))
def test_nonunique_key_is_not_accepted(table):
    cursor = FakeMetadataCursor(table, non_unique=True)
    with pytest.raises(RuntimeError, match="coddy_startup_schema_missing_unique_keys"):
        check_startup_schema_table(cursor, table)
    assert len(cursor.statements) == 2


def test_community_identity_and_user_level_isolation_keys_not_weakened():
    assert "guild_id" in REQUIRED_COLUMNS["community_discord"]
    assert REQUIRED_UNIQUE_KEYS["community_discord"] == ("guild_id",)
    assert "community_id" in REQUIRED_COLUMNS["user_bans"]
    assert REQUIRED_UNIQUE_KEYS["user_bans"] == ("id",)
    assert REQUIRED_UNIQUE_KEYS["user_level"] == ("user_id", "server_guild_id")


def test_three_initializers_no_longer_have_ddl_or_import_side_effects():
    root = Path(__file__).resolve().parents[1]
    source = (root / "core/database.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = (
        "initialize_community_discord_columns",
        "initialize_user_bans_registered_at_column",
        "initialize_user_level_voice_xp_columns",
    )
    for name in names:
        f = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
        body = ast.get_source_segment(source, f)
        assert "check_startup_schema_table(cursor," in body
        for ddl in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "DROP INDEX"):
            assert ddl not in body

    eager = {
        n.value.func.id for n in tree.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    }
    assert eager.isdisjoint(names)

    service = (root / "message_services/discord_service.py").read_text(encoding="utf-8")
    runner = next(
        node for node in ast.parse(service).body
        if isinstance(node, ast.FunctionDef) and node.name == "run_discord_client"
    )
    calls = [
        n.value.func.id for n in runner.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    ]
    assert calls[-12:-9] == list(names)
