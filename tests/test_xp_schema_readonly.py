"""Voice XP startup must not repair schema or connect to real services."""

import ast
from pathlib import Path

import pytest

from core.xp_schema import (
    REQUIRED_COLUMNS,
    REQUIRED_UNIQUE_KEYS,
    check_voice_xp_schema,
)


class MetadataCursor:
    def __init__(self, *, missing_column=None, missing_unique=None):
        self.missing_column = missing_column
        self.missing_unique = missing_unique
        self.statements = []
        self._result = []

    def execute(self, statement, params=()):
        self.statements.append((statement, params))
        if "information_schema.COLUMNS" in statement:
            self._result = [
                {"TABLE_NAME": table, "COLUMN_NAME": col}
                for table, cols in REQUIRED_COLUMNS.items()
                for col in cols
                if (table, col) != self.missing_column
            ]
        elif "information_schema.STATISTICS" in statement:
            self._result = [
                {
                    "TABLE_NAME": table,
                    "INDEX_NAME": f"ux_{table}",
                    "NON_UNIQUE": 0,
                    "SEQ_IN_INDEX": order,
                    "COLUMN_NAME": col,
                }
                for table, fields in REQUIRED_UNIQUE_KEYS.items()
                if table != self.missing_unique
                for order, col in enumerate(fields, 1)
            ]
        else:
            raise AssertionError(f"Unexpected SQL: {statement}")

    def fetchall(self):
        return self._result


def _assert_readonly(cursor):
    assert cursor.statements
    for statement, params in cursor.statements:
        assert statement.lstrip().upper().startswith("SELECT ")
        assert "information_schema." in statement
        assert set(params) == set(REQUIRED_COLUMNS)


def test_voice_xp_schema_accepts_canonical_columns_and_indexes():
    cursor = MetadataCursor()
    check_voice_xp_schema(cursor)
    assert len(cursor.statements) == 2
    _assert_readonly(cursor)


def test_voice_xp_schema_fails_closed_on_missing_column_without_creating_it():
    cursor = MetadataCursor(missing_column=("voice_sessions", "last_tick_at"))
    with pytest.raises(RuntimeError, match="voice_sessions.last_tick_at"):
        check_voice_xp_schema(cursor)
    assert len(cursor.statements) == 1
    _assert_readonly(cursor)


def test_voice_xp_schema_fails_closed_on_missing_unique_key():
    cursor = MetadataCursor(missing_unique="voice_xp_daily_metrics")
    with pytest.raises(RuntimeError, match="xp_voice_schema_missing_unique_keys"):
        check_voice_xp_schema(cursor)
    _assert_readonly(cursor)


def test_voice_xp_cog_uses_preflight_and_no_longer_contains_ddl():
    root = Path(__file__).resolve().parents[1]
    cog = (root / "cogs/xp.py").read_text(encoding="utf-8")
    db = (root / "core/database.py").read_text(encoding="utf-8")
    assert "validate_voice_xp_schema()" in cog
    assert "_ensure_voice_xp_audit_tables" not in cog
    assert "_ensure_voice_sessions_table" not in cog
    assert "CREATE TABLE IF NOT EXISTS voice_xp_" not in cog
    assert "RENAME TABLE voice_xp_sessions" not in db
    assert "CREATE TABLE IF NOT EXISTS voice_sessions" not in db
    assert "def validate_voice_xp_schema()" in db
    ast.parse(cog)
    ast.parse(db)
