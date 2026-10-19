"""Regression checks for the production form schema; no real database or Discord."""

import ast
from pathlib import Path

import pytest

from core.runtime_schema import (
    FORM_REQUIRED_COLUMNS,
    FORM_REQUIRED_UNIQUE_KEYS,
    check_form_schema,
)


class FormMetadataCursor:
    def __init__(self, *, missing_table=None, missing_column=None, missing_key=None):
        self.missing_table = missing_table
        self.missing_column = missing_column
        self.missing_key = missing_key
        self.queries = []
        self.rows = []

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        assert set(params) == set(FORM_REQUIRED_COLUMNS)
        assert sql.strip().upper().startswith("SELECT ")
        assert "information_schema." in sql

        if "information_schema.COLUMNS" in sql:
            self.rows = [
                {"TABLE_NAME": table, "COLUMN_NAME": column}
                for table, columns in FORM_REQUIRED_COLUMNS.items()
                if table != self.missing_table
                for column in columns
                if (table, column) != self.missing_column
            ]
        elif "information_schema.STATISTICS" in sql:
            self.rows = [
                {
                    "TABLE_NAME": table,
                    "INDEX_NAME": "unique_form_key",
                    "NON_UNIQUE": 0,
                    "SEQ_IN_INDEX": position,
                    "COLUMN_NAME": column,
                }
                for table, key_columns in FORM_REQUIRED_UNIQUE_KEYS.items()
                if table != self.missing_key
                for position, column in enumerate(key_columns, 1)
            ]
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self):
        return self.rows


def test_production_form_snapshot_contract_is_accepted_read_only():
    cursor = FormMetadataCursor()
    check_form_schema(cursor)
    assert len(cursor.queries) == 2


@pytest.mark.parametrize("table", sorted(FORM_REQUIRED_COLUMNS))
def test_missing_form_table_fails_before_index_check(table):
    cursor = FormMetadataCursor(missing_table=table)
    with pytest.raises(RuntimeError, match=rf"{table}\\."):
        check_form_schema(cursor)
    assert len(cursor.queries) == 1


@pytest.mark.parametrize("table", sorted(FORM_REQUIRED_COLUMNS))
def test_missing_form_column_fails_closed(table):
    missing = sorted(FORM_REQUIRED_COLUMNS[table])[0]
    cursor = FormMetadataCursor(missing_column=(table, missing))
    with pytest.raises(RuntimeError, match=rf"{table}\\.{missing}"):
        check_form_schema(cursor)
    assert len(cursor.queries) == 1


@pytest.mark.parametrize("table", sorted(FORM_REQUIRED_UNIQUE_KEYS))
def test_missing_form_unique_key_fails_closed(table):
    cursor = FormMetadataCursor(missing_key=table)
    with pytest.raises(RuntimeError, match="coddy_form_schema_missing_unique_keys"):
        check_form_schema(cursor)
    assert len(cursor.queries) == 2


def test_form_contract_preserves_legacy_guild_scope():
    assert "server_guild_id" in FORM_REQUIRED_COLUMNS["form_flows"]
    assert "guild_id" in FORM_REQUIRED_COLUMNS["form_submissions"]
    assert "guild_id" in FORM_REQUIRED_COLUMNS["form_published_messages"]
    # Child tables inherit tenant scope through their parent keys, not
    # through new columns invented by the runtime.
    assert "guild_id" not in FORM_REQUIRED_COLUMNS["form_questions"]
    assert "guild_id" not in FORM_REQUIRED_COLUMNS["form_decisions"]
    assert FORM_REQUIRED_UNIQUE_KEYS["form_published_messages"] == ("message_id",)


def test_form_initialization_no_longer_mutates_schema_or_runs_at_import():
    root = Path(__file__).resolve().parents[1]
    database = (root / "core/database.py").read_text(encoding="utf-8")
    tree = ast.parse(database)
    function = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "initialize_form_tables"
    )
    source = ast.get_source_segment(database, function)
    assert "check_form_schema(cursor)" in source
    for forbidden in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "DROP INDEX"):
        assert forbidden not in source

    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            assert not (
                isinstance(node.value.func, ast.Name)
                and node.value.func.id == "initialize_form_tables"
            )

    service = (root / "message_services/discord_service.py").read_text(
        encoding="utf-8"
    )
    runner = next(
        n for n in ast.parse(service).body
        if isinstance(n, ast.FunctionDef) and n.name == "run_discord_client"
    )
    startup_calls = [
        node.value.func.id
        for node in runner.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]
    assert startup_calls[-4:] == [
        "initialize_form_tables",
        "initialize_user_economy_table",
        "initialize_bot_sensitive_permission_whitelist_table",
        "initialize_config_levels_table",
    ]
