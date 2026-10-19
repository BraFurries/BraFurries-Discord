"""Lazy inventory DDL retirement against the 2026-10-19 production schema dump.

The snapshot is schema-only: inventory rows are never accessed here.
In MariaDB the service_metadata column is LONGTEXT with a JSON_VALID check.
"""

import ast
from pathlib import Path
from unittest.mock import patch

import pytest

from core.runtime_schema import (
    INVENTORY_REQUIRED_COLUMNS,
    INVENTORY_REQUIRED_UNIQUE_KEYS,
    check_inventory_schema,
)

with patch("mysql.connector.pooling.MySQLConnectionPool"):
    from core import database


SNAPSHOT_COLUMNS = {
    "id", "community_id", "store_item_id", "owner_user_id",
    "quantity", "used_in", "valid_until", "service_metadata",
}


class InventoryMetadataCursor:
    def __init__(self, *, missing_column=None, non_unique=False):
        self.missing_column = missing_column
        self.non_unique = non_unique
        self.queries = []
        self.rows = []

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        assert sql.lstrip().startswith("SELECT ")
        assert "information_schema." in sql
        assert params == ("user_inventory",)
        if "information_schema.COLUMNS" in sql:
            self.rows = [
                {"TABLE_NAME": "user_inventory", "COLUMN_NAME": col}
                for col in SNAPSHOT_COLUMNS
                if col != self.missing_column
            ]
        elif "information_schema.STATISTICS" in sql:
            self.rows = [{
                "TABLE_NAME": "user_inventory",
                "INDEX_NAME": "PRIMARY",
                "NON_UNIQUE": int(self.non_unique),
                "SEQ_IN_INDEX": 1,
                "COLUMN_NAME": "id",
            }]
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self.rows


def test_inventory_contract_is_community_scoped_and_legacy_pk_only():
    assert INVENTORY_REQUIRED_COLUMNS["user_inventory"] == SNAPSHOT_COLUMNS
    assert INVENTORY_REQUIRED_UNIQUE_KEYS["user_inventory"] == ("id",)
    assert "community_id" in SNAPSHOT_COLUMNS
    assert "server_guild_id" not in SNAPSHOT_COLUMNS
    assert "service_metadata" in SNAPSHOT_COLUMNS


def test_inventory_preflight_queries_only_metadata():
    cursor = InventoryMetadataCursor()
    check_inventory_schema(cursor)
    assert len(cursor.queries) == 2
    assert all(sql.lstrip().startswith("SELECT ") for sql, _ in cursor.queries)


@pytest.mark.parametrize("missing_column", ["service_metadata", "community_id", "owner_user_id"])
def test_inventory_fails_closed_on_missing_column(missing_column):
    cursor = InventoryMetadataCursor(missing_column=missing_column)
    with pytest.raises(RuntimeError, match="coddy_inventory_schema_missing_columns"):
        check_inventory_schema(cursor)
    assert len(cursor.queries) == 1


def test_inventory_fails_closed_on_nonunique_primary_key():
    cursor = InventoryMetadataCursor(non_unique=True)
    with pytest.raises(RuntimeError, match="coddy_inventory_schema_missing_unique_keys"):
        check_inventory_schema(cursor)
    assert len(cursor.queries) == 2


def test_inventory_helper_validates_once_without_ddl(monkeypatch):
    monkeypatch.setattr(database, "_USER_INVENTORY_SCHEMA_VALIDATED", False)
    cursor = InventoryMetadataCursor()
    database._ensure_user_inventory_schema(cursor)
    assert database._USER_INVENTORY_SCHEMA_VALIDATED is True
    database._ensure_user_inventory_schema(cursor)
    assert len(cursor.queries) == 2


def test_failed_preflight_does_not_mark_schema_as_validated(monkeypatch):
    monkeypatch.setattr(database, "_USER_INVENTORY_SCHEMA_VALIDATED", False)
    cursor = InventoryMetadataCursor(missing_column="service_metadata")
    with pytest.raises(RuntimeError, match="coddy_inventory_schema_missing_columns"):
        database._ensure_user_inventory_schema(cursor)
    assert database._USER_INVENTORY_SCHEMA_VALIDATED is False


def test_inventory_runtime_function_contains_no_ddl_and_preserves_lazy_call_sites():
    source = (Path(__file__).resolve().parents[1] / "core/database.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    method = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_ensure_user_inventory_schema"
    )
    body = ast.get_source_segment(source, method)
    assert "check_inventory_schema(cursor)" in body
    assert "ALTER TABLE" not in body
    assert "CREATE TABLE" not in body
    assert "DROP TABLE" not in body

    # Existing services still invoke this guard before using inventory data.
    assert source.count("_ensure_user_inventory_schema(cursor)") >= 2
