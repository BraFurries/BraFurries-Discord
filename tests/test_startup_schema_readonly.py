"""No DB/Discord connections: metadata-only regression coverage for retired startup DDL."""

import ast
from pathlib import Path

import pytest

from core.runtime_schema import (
    REQUIRED_COLUMNS,
    REQUIRED_UNIQUE_KEYS,
    check_startup_schema_table,
)


class FakeMetadataCursor:
    def __init__(self, table: str, *, missing_column=None, key_order=None, non_unique=0):
        self.table = table
        self.missing_column = missing_column
        self.key_order = key_order
        self.non_unique = non_unique
        self.sql = []
        self.rows = []

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        assert params == (self.table,)
        if "information_schema.COLUMNS" in sql:
            self.rows = [
                {"TABLE_NAME": self.table, "COLUMN_NAME": c}
                for c in REQUIRED_COLUMNS[self.table]
                if c != self.missing_column
            ]
        elif "information_schema.STATISTICS" in sql:
            cols = self.key_order or REQUIRED_UNIQUE_KEYS[self.table]
            self.rows = [
                {
                    "TABLE_NAME": self.table,
                    "INDEX_NAME": "unique_for_test",
                    "NON_UNIQUE": self.non_unique,
                    "SEQ_IN_INDEX": order,
                    "COLUMN_NAME": c,
                }
                for order, c in enumerate(cols, 1)
            ]
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchall(self):
        return self.rows


def assert_only_readonly_metadata(cursor):
    assert cursor.sql
    assert all(s.lstrip().upper().startswith("SELECT ") for s, _ in cursor.sql)
    assert all("information_schema." in s for s, _ in cursor.sql)


@pytest.mark.parametrize("table", sorted(REQUIRED_COLUMNS))
def test_canonical_production_metadata_passes_without_ddl(table):
    cursor = FakeMetadataCursor(table)
    check_startup_schema_table(cursor, table)
    assert len(cursor.sql) == 2
    assert_only_readonly_metadata(cursor)


@pytest.mark.parametrize("table", sorted(REQUIRED_COLUMNS))
def test_missing_column_fails_before_modifying_any_schema(table):
    col = sorted(REQUIRED_COLUMNS[table])[0]
    cursor = FakeMetadataCursor(table, missing_column=col)
    with pytest.raises(RuntimeError, match=rf"{table}\.{col}"):
        check_startup_schema_table(cursor, table)
    assert len(cursor.sql) == 1
    assert_only_readonly_metadata(cursor)


@pytest.mark.parametrize("table", sorted(REQUIRED_COLUMNS))
def test_missing_exact_unique_key_fails_closed(table):
    cursor = FakeMetadataCursor(table, non_unique=1)
    with pytest.raises(RuntimeError, match="coddy_startup_schema_missing_unique_keys"):
        check_startup_schema_table(cursor, table)
    assert_only_readonly_metadata(cursor)


@pytest.mark.parametrize("table", sorted(REQUIRED_COLUMNS))
def test_unique_column_order_is_not_interchangeable(table):
    expected = REQUIRED_UNIQUE_KEYS[table]
    if len(expected) == 1:
        pytest.skip("Single-column unique key has no order ambiguity")
    cursor = FakeMetadataCursor(table, key_order=tuple(reversed(expected)))
    with pytest.raises(RuntimeError, match="coddy_startup_schema_missing_unique_keys"):
        check_startup_schema_table(cursor, table)
    assert_only_readonly_metadata(cursor)


def test_guild_scoped_configuration_unique_keys_and_minigame_primary_key():
    # Configuration identities must not alias guilds. The append-only score
    # events intentionally have only the surrogate id PK in production.
    for table in (
        "user_economy",
        "bot_sensitive_permission_whitelist",
        "config_levels",
        "active_call_logs",
        "allowed_feature_channels",
    ):
        assert "server_guild_id" in REQUIRED_UNIQUE_KEYS[table], table
    assert REQUIRED_UNIQUE_KEYS["minigame_scores"] == ("id",)
    assert "server_guild_id" in REQUIRED_COLUMNS["minigame_scores"]


def test_unrecognized_table_cannot_be_injected_into_metadata_preflight():
    cursor = FakeMetadataCursor("user_economy")
    with pytest.raises(ValueError, match="unsupported_startup_schema_table"):
        check_startup_schema_table(cursor, "user_economy; DROP TABLE users")
    assert cursor.sql == []


def test_three_startup_functions_no_longer_contain_ddl():
    db = (Path(__file__).resolve().parents[1] / "core/database.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(db)
    for function_name in (
        "initialize_user_economy_table",
        "initialize_bot_sensitive_permission_whitelist_table",
        "initialize_config_levels_table",
    ):
        fn = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == function_name
        )
        code = ast.get_source_segment(db, fn)
        assert "check_startup_schema_table(" in code
        for statement in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "DROP INDEX"):
            assert statement not in code
    # Unit tests import core.database without a database; validation must not
    # happen as an import side effect, only when launching the real service.
    tree = ast.parse(db)
    startup_calls = {
        node.value.func.id
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    }
    for name in (
        "initialize_user_economy_table",
        "initialize_bot_sensitive_permission_whitelist_table",
        "initialize_config_levels_table",
    ):
        assert name not in startup_calls

    service = (Path(__file__).resolve().parents[1] / "message_services/discord_service.py").read_text(
        encoding="utf-8"
    )
    service_tree = ast.parse(service)
    startup = next(
        n for n in service_tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "run_discord_client"
    )
    names = [
        n.value.func.id for n in startup.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    ]
    assert names[-3:] == [
        "initialize_user_economy_table",
        "initialize_bot_sensitive_permission_whitelist_table",
        "initialize_config_levels_table",
    ]
