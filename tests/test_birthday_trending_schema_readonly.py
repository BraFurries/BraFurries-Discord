"""Birthday and trending legacy DDL replacement, without MariaDB/Discord.

The independently declared metadata mirrors the 2026-10-19 production
MariaDB structure-only export. No database rows are needed.
"""

import ast
from pathlib import Path

import pytest

from core.runtime_schema import REQUIRED_COLUMNS, REQUIRED_UNIQUE_KEYS, check_startup_schema_table


SCHEMA = {
    "user_birthday": {
        "columns": {
            "user_id", "birth_date", "post_informed_date", "verified",
            "registered", "registered_at", "18_plus",
        },
        "unique": ("user_id",),
    },
    "config_server_settings": {
        "columns": {"id", "server_guild_id", "trending_presence_enabled"},
        "unique": ("server_guild_id",),
    },
}


class MetadataCursor:
    def __init__(self, table, *, missing_column=None, nonunique=False):
        self.table, self.missing_column, self.nonunique = table, missing_column, nonunique
        self.sql = []
        self.rows = []

    def execute(self, statement, params=()):
        assert statement.strip().startswith("SELECT ")
        assert "information_schema." in statement
        assert params == (self.table,)
        self.sql.append((statement, params))
        if "information_schema.COLUMNS" in statement:
            self.rows = [
                {"TABLE_NAME": self.table, "COLUMN_NAME": col}
                for col in SCHEMA[self.table]["columns"]
                if col != self.missing_column
            ]
        elif "information_schema.STATISTICS" in statement:
            self.rows = [
                {"TABLE_NAME": self.table, "INDEX_NAME": "prod_pk_or_unique",
                 "NON_UNIQUE": int(self.nonunique), "SEQ_IN_INDEX": n, "COLUMN_NAME": col}
                for n, col in enumerate(SCHEMA[self.table]["unique"], 1)
            ]
        else:
            raise AssertionError(statement)

    def fetchall(self):
        return self.rows


@pytest.mark.parametrize("table", sorted(SCHEMA))
def test_matches_production_schema_structure(table):
    assert REQUIRED_COLUMNS[table] == SCHEMA[table]["columns"]
    assert REQUIRED_UNIQUE_KEYS[table] == SCHEMA[table]["unique"]


@pytest.mark.parametrize("table", sorted(SCHEMA))
def test_read_only_preflight_success(table):
    cursor = MetadataCursor(table)
    check_startup_schema_table(cursor, table)
    assert len(cursor.sql) == 2


@pytest.mark.parametrize("table", sorted(SCHEMA))
def test_missing_column_fails_closed(table):
    cursor = MetadataCursor(table, missing_column=sorted(SCHEMA[table]["columns"])[0])
    with pytest.raises(RuntimeError, match="coddy_startup_schema_missing_columns"):
        check_startup_schema_table(cursor, table)
    assert len(cursor.sql) == 1


@pytest.mark.parametrize("table", sorted(SCHEMA))
def test_nonunique_key_fails_closed(table):
    cursor = MetadataCursor(table, nonunique=True)
    with pytest.raises(RuntimeError, match="coddy_startup_schema_missing_unique_keys"):
        check_startup_schema_table(cursor, table)


def test_birthday_identity_is_user_scoped_and_settings_are_tenant_scoped():
    assert REQUIRED_UNIQUE_KEYS["user_birthday"] == ("user_id",)
    assert REQUIRED_UNIQUE_KEYS["config_server_settings"] == ("server_guild_id",)
    assert "trending_presence_enabled" in REQUIRED_COLUMNS["config_server_settings"]


def test_no_ddl_or_module_import_checks_for_birthday_and_trending():
    root = Path(__file__).resolve().parents[1]
    src = (root / "core/database.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = (
        "initialize_user_birthday_registration_columns",
        "initialize_trending_presence_column",
    )
    for name in names:
        fun = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        body = ast.get_source_segment(src, fun)
        assert "check_startup_schema_table(cursor," in body
        for forbidden in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "DROP INDEX"):
            assert forbidden not in body
    import_calls = {
        n.value.func.id for n in tree.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    }
    assert set(names).isdisjoint(import_calls)
    service = (root / "message_services/discord_service.py").read_text(encoding="utf-8")
    runner = next(
        n for n in ast.parse(service).body
        if isinstance(n, ast.FunctionDef) and n.name == "run_discord_client"
    )
    startup = [
        n.value.func.id for n in runner.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    ]
    assert startup[-14:-12] == list(names)
