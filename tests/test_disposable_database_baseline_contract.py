"""Coddy read-only startup schema preflight against a real disposable Flyway baseline.

Opt-in CI contract test only: the database is initialized by the Database repo's
isolated Flyway/MariaDB Compose fixture. No Discord connection or runtime DDL.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

from core.runtime_schema import (
    FORM_REQUIRED_COLUMNS,
    FORM_REQUIRED_UNIQUE_KEYS,
    INVENTORY_REQUIRED_COLUMNS,
    INVENTORY_REQUIRED_UNIQUE_KEYS,
    REQUIRED_COLUMNS,
    check_form_schema,
    check_inventory_schema,
    check_startup_schema_table,
)

# Metadata queries are restricted to known startup and lazy-service contracts.
ALLOWED_TABLES = (
    set(REQUIRED_COLUMNS)
    | set(FORM_REQUIRED_COLUMNS)
    | set(INVENTORY_REQUIRED_COLUMNS)
)


pytestmark = pytest.mark.skipif(
    os.environ.get("BRF_DISPOSABLE_SCHEMA_COMPAT") != "true",
    reason="requires explicit isolated GitHub Actions schema-compatibility run",
)

BASELINE_REPO = Path(__file__).resolve().parents[1] / ".ci" / "database-baseline"
VALIDATION_SCRIPTS = BASELINE_REPO / "validation" / "scripts"


class MetadataCursor:
    """Coddy's cursor protocol, backed by real information_schema SELECT results."""

    def __init__(self, metadata, *, missing_column=None, hide_unique_table=None):
        self.metadata = metadata["sections"]
        self.missing_column = missing_column
        self.hide_unique_table = hide_unique_table
        self.statements = []
        self.rows = []

    def execute(self, sql, parameters=()):
        if not sql.lstrip().upper().startswith("SELECT "):
            raise AssertionError("Coddy schema preflight must never change schema")
        if "information_schema." not in sql or "TABLE_SCHEMA = DATABASE()" not in sql:
            raise AssertionError("metadata queries must be scoped to the current schema")
        tables = {str(table) for table in parameters}
        if not tables or not tables.issubset(ALLOWED_TABLES):
            raise AssertionError("unexpected metadata table target")
        self.statements.append((sql, parameters))
        if "information_schema.COLUMNS" in sql:
            self.rows = [
                {"TABLE_NAME": row["table"], "COLUMN_NAME": row["column"]}
                for row in self.metadata["columns"]
                if row["table"] in tables
                and (row["table"], row["column"]) != self.missing_column
            ]
        elif "information_schema.STATISTICS" in sql:
            self.rows = [
                {
                    "TABLE_NAME": row["table"],
                    "INDEX_NAME": row["index"],
                    "NON_UNIQUE": (
                        1 if row["table"] == self.hide_unique_table else row["non_unique"]
                    ),
                    "SEQ_IN_INDEX": row["sequence"],
                    "COLUMN_NAME": row["column"],
                }
                for row in self.metadata["indexes"]
                if row["table"] in tables
            ]
        else:
            raise AssertionError("unexpected Coddy metadata query")

    def fetchall(self):
        return self.rows


@pytest.fixture(scope="module")
def schema_snapshot():
    if not BASELINE_REPO.is_dir():
        pytest.fail("expected pinned Database repo checkout is missing")
    sys.path.insert(0, str(VALIDATION_SCRIPTS))
    from smoke_test import (
        SmokeFailure, assert_no_resources_remain, compose, down,
        safety_preflight, start_empty_database,
    )
    from physical_schema_fingerprint import capture_disposable

    accepted = False
    try:
        safety_preflight()
        accepted = True
        down()
        start_empty_database()
        compose(
            "run", "--rm", "--no-deps", "flyway",
            "-locations=filesystem:/flyway/baseline,filesystem:/flyway/official",
            "migrate",
        )
        snapshot = capture_disposable()
        assert len(snapshot["sections"]["tables"]) == 107
        assert len(snapshot["sections"]["columns"]) == 946
        yield snapshot
    finally:
        if accepted:
            try:
                down()
                assert_no_resources_remain()
            except SmokeFailure:
                pytest.fail("failed to clean disposable MariaDB schema fixtures")


@pytest.mark.parametrize("table", sorted(REQUIRED_COLUMNS))
def test_canonical_flyway_schema_satisfies_coddy_startup_preflight(schema_snapshot, table):
    cursor = MetadataCursor(schema_snapshot)
    check_startup_schema_table(cursor, table)
    assert len(cursor.statements) == 2
    assert all(statement.lstrip().upper().startswith("SELECT ") for statement, _ in cursor.statements)


def test_missing_column_fails_closed_with_real_schema_metadata(schema_snapshot):
    table = "config_levels"
    first = sorted(REQUIRED_COLUMNS[table])[0]
    cursor = MetadataCursor(schema_snapshot, missing_column=(table, first))
    with pytest.raises(RuntimeError, match="coddy_startup_schema_missing_columns"):
        check_startup_schema_table(cursor, table)
    assert len(cursor.statements) == 1


def test_missing_unique_index_fails_closed_with_real_schema_metadata(schema_snapshot):
    cursor = MetadataCursor(schema_snapshot, hide_unique_table="user_economy")
    with pytest.raises(RuntimeError, match="coddy_startup_schema_missing_unique_keys"):
        check_startup_schema_table(cursor, "user_economy")
    assert len(cursor.statements) == 2


@pytest.mark.parametrize(
    "name,validator,required_columns,required_keys",
    [
        ("forms", check_form_schema, FORM_REQUIRED_COLUMNS, FORM_REQUIRED_UNIQUE_KEYS),
        ("inventory", check_inventory_schema, INVENTORY_REQUIRED_COLUMNS, INVENTORY_REQUIRED_UNIQUE_KEYS),
    ],
)
def test_canonical_baseline_satisfies_lazy_service_preflights(
    schema_snapshot, name, validator, required_columns, required_keys
):
    """Run actual Coddy checks on metadata collected from the disposable MariaDB."""
    assert required_columns
    assert set(required_keys) == set(required_columns)
    cursor = MetadataCursor(schema_snapshot)
    validator(cursor)
    assert len(cursor.statements) == 2
    assert all(statement.lstrip().upper().startswith("SELECT ") for statement, _ in cursor.statements)
    assert {str(table) for table in cursor.statements[0][1]} == set(required_columns)


@pytest.mark.parametrize(
    "validator,required_columns,error_prefix",
    [
        (check_form_schema, FORM_REQUIRED_COLUMNS, "coddy_form_schema"),
        (check_inventory_schema, INVENTORY_REQUIRED_COLUMNS, "coddy_inventory_schema"),
    ],
)
def test_lazy_preflights_fail_closed_on_missing_column(
    schema_snapshot, validator, required_columns, error_prefix
):
    table = sorted(required_columns)[0]
    column = sorted(required_columns[table])[0]
    cursor = MetadataCursor(schema_snapshot, missing_column=(table, column))
    with pytest.raises(RuntimeError, match=error_prefix + "_missing_columns"):
        validator(cursor)
    assert len(cursor.statements) == 1


@pytest.mark.parametrize(
    "validator,required_keys,error_prefix",
    [
        (check_form_schema, FORM_REQUIRED_UNIQUE_KEYS, "coddy_form_schema"),
        (check_inventory_schema, INVENTORY_REQUIRED_UNIQUE_KEYS, "coddy_inventory_schema"),
    ],
)
def test_lazy_preflights_fail_closed_on_missing_unique_index(
    schema_snapshot, validator, required_keys, error_prefix
):
    table = sorted(required_keys)[0]
    cursor = MetadataCursor(schema_snapshot, hide_unique_table=table)
    with pytest.raises(RuntimeError, match=error_prefix + "_missing_unique_keys"):
        validator(cursor)
    assert len(cursor.statements) == 2


def test_legacy_form_and_inventory_tenant_shape_is_not_silently_rewritten(schema_snapshot):
    """Preserve nullable historical guild IDs and parent-scoped inventory semantics."""
    columns = {
        (record["table"], record["column"]): record
        for record in schema_snapshot["sections"]["columns"]
    }
    assert columns["form_flows", "server_guild_id"]["nullable"] == "YES"
    assert columns["user_inventory", "community_id"]["nullable"] == "NO"
    assert "server_guild_id" in FORM_REQUIRED_COLUMNS["form_flows"]
    assert "community_id" in INVENTORY_REQUIRED_COLUMNS["user_inventory"]
