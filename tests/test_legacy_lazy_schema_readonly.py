"""Last two lazy DDL paths: economy and birthday preference schema guards.

Production MariaDB 10.6 export (2026-10-19) has all required columns.
The legacy backfills only ran after the runtime itself created a column.
"""

import ast
from pathlib import Path
from unittest.mock import patch

import pytest

with patch("mysql.connector.pooling.MySQLConnectionPool"):
    from core import database


# Expected from the schema-only snapshot (no data is accessed).
EXPECTED = {
    "_ensure_economy_config_columns": (
        "config_economy", {"bump_points", "bump_reward_enabled", "birthday_points"}
    ),
    "_ensure_birthday_mentionable_column": (
        "user_community_status", {"birthday_mentionable"}
    ),
}


class MetadataCursor:
    def __init__(self, table, *, missing=None):
        self.table = table
        self.missing = missing
        self.queries = []
        self.column = None

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        assert sql.lstrip().startswith("SELECT ")
        assert "INFORMATION_SCHEMA.COLUMNS" in sql
        assert "TABLE_SCHEMA = DATABASE()" in sql
        assert params[0] == self.table
        self.column = params[1]

    def fetchone(self):
        return None if self.column == self.missing else {"exists": 1}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_schema_already_provisioned_requires_selects_only(name):
    table, cols = EXPECTED[name]
    cursor = MetadataCursor(table)
    assert getattr(database, name)(cursor) is True
    assert set(params[1] for _sql, params in cursor.queries) == cols
    assert len(cursor.queries) == len(cols)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_missing_schema_returns_false_without_ddl_or_data_updates(name, caplog):
    table, cols = EXPECTED[name]
    missing = sorted(cols)[0]
    cursor = MetadataCursor(table, missing=missing)
    assert getattr(database, name)(cursor) is False
    assert missing in caplog.text
    assert all(sql.lstrip().startswith("SELECT ") for sql, _ in cursor.queries)


def test_birthday_preference_remains_community_scoped():
    # Production uniqueness is (community_id, user_id), never global user_id.
    source = (Path(__file__).resolve().parents[1] / "core/database.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    for name in ("getAllBirthdays", "_set_community_birthday_mentionable"):
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        body = ast.get_source_segment(source, fn)
        assert "_get_community_id(cursor, guild_id)" in body
        assert "community_id" in body
    setter = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_set_community_birthday_mentionable"
    )
    assert "WHERE user_id = %s AND community_id = %s" in ast.get_source_segment(source, setter)


def test_neither_guard_performs_ddl_or_cross_tenant_backfill():
    src = (Path(__file__).resolve().parents[1] / "core/database.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    for function_name in EXPECTED:
        fn = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == function_name
        )
        body = ast.get_source_segment(src, fn)
        assert "_check_columns(" in body
        for forbidden in ("ALTER TABLE", "CREATE TABLE", "UPDATE ", "JOIN user_birthday"):
            assert forbidden not in body
