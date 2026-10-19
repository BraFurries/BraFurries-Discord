"""Checks for retiring boss and expedition runtime schema creation.

Schema expectations below are grounded in the production MariaDB 10.6
structure-only export from 2026-10-19. Does not read or mutate live data.
"""

import ast
from pathlib import Path

import pytest

from core.runtime_schema import REQUIRED_COLUMNS, REQUIRED_UNIQUE_KEYS, check_startup_schema_table


PRODUCTION_COLUMNS = {
    "community_boss_event": frozenset({
        "id", "guild_id", "boss_name", "max_hp", "current_hp", "active",
        "attack_cooldown_seconds", "created_at", "finished_at",
    }),
    "community_boss_contribution": frozenset({
        "id", "guild_id", "user_id", "total_damage", "updated_at",
    }),
    "community_boss_attack_cooldown": frozenset({
        "id", "guild_id", "user_id", "cooldown_until",
    }),
    "community_expedition": frozenset({
        "id", "guild_id", "active", "started_at", "ends_at",
        "participants_json", "finalized_at",
    }),
}
PRODUCTION_UNIQUE_KEYS = {
    "community_boss_event": ("guild_id",),
    "community_boss_contribution": ("guild_id", "user_id"),
    "community_boss_attack_cooldown": ("guild_id", "user_id"),
    "community_expedition": ("guild_id",),
}


class MetadataCursor:
    def __init__(self, table, missing_column=None, non_unique=False):
        self.table = table
        self.missing_column = missing_column
        self.non_unique = non_unique
        self.rows = []
        self.queries = []

    def execute(self, sql, params=()):
        assert params == (self.table,)
        assert sql.lstrip().startswith("SELECT ")
        assert "information_schema." in sql
        self.queries.append((sql, params))
        if "information_schema.COLUMNS" in sql:
            self.rows = [
                {"TABLE_NAME": self.table, "COLUMN_NAME": col}
                for col in PRODUCTION_COLUMNS[self.table]
                if col != self.missing_column
            ]
        elif "information_schema.STATISTICS" in sql:
            self.rows = [
                {
                    "TABLE_NAME": self.table,
                    "INDEX_NAME": "production_unique",
                    "NON_UNIQUE": int(self.non_unique),
                    "SEQ_IN_INDEX": order,
                    "COLUMN_NAME": col,
                }
                for order, col in enumerate(PRODUCTION_UNIQUE_KEYS[self.table], 1)
            ]
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self.rows


@pytest.mark.parametrize("table", sorted(PRODUCTION_COLUMNS))
def test_existing_boss_expedition_contract_matches_production_dump(table):
    assert REQUIRED_COLUMNS[table] == PRODUCTION_COLUMNS[table]
    assert REQUIRED_UNIQUE_KEYS[table] == PRODUCTION_UNIQUE_KEYS[table]


@pytest.mark.parametrize("table", sorted(PRODUCTION_COLUMNS))
def test_preflight_accepts_real_schema_using_only_selects(table):
    cursor = MetadataCursor(table)
    check_startup_schema_table(cursor, table)
    assert len(cursor.queries) == 2


@pytest.mark.parametrize("table", sorted(PRODUCTION_COLUMNS))
def test_missing_column_stops_before_index_checks(table):
    column = sorted(PRODUCTION_COLUMNS[table])[0]
    cursor = MetadataCursor(table, missing_column=column)
    with pytest.raises(RuntimeError, match="coddy_startup_schema_missing_columns"):
        check_startup_schema_table(cursor, table)
    assert len(cursor.queries) == 1


@pytest.mark.parametrize("table", sorted(PRODUCTION_COLUMNS))
def test_non_unique_index_not_accepted_for_tenant_key(table):
    cursor = MetadataCursor(table, non_unique=True)
    with pytest.raises(RuntimeError, match="coddy_startup_schema_missing_unique_keys"):
        check_startup_schema_table(cursor, table)
    assert len(cursor.queries) == 2


def test_boss_and_expedition_unique_keys_scope_each_guild():
    for table, key in PRODUCTION_UNIQUE_KEYS.items():
        assert "guild_id" in PRODUCTION_COLUMNS[table]
        assert key[0] == "guild_id"


def test_no_runtime_ddl_or_eager_import_for_boss_and_expedition():
    root = Path(__file__).resolve().parents[1]
    db = (root / "core/database.py").read_text(encoding="utf-8")
    tree = ast.parse(db)
    funcs = ("initialize_boss_event_tables", "initialize_expedition_tables")
    for name in funcs:
        function = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == name
        )
        source = ast.get_source_segment(db, function)
        assert "check_startup_schema_table(cursor," in source
        for statement in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "DROP INDEX"):
            assert statement not in source

    top_level = {
        n.value.func.id for n in tree.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    }
    assert top_level.isdisjoint(funcs)

    service = (root / "message_services/discord_service.py").read_text(
        encoding="utf-8"
    )
    runner = next(
        n for n in ast.parse(service).body
        if isinstance(n, ast.FunctionDef) and n.name == "run_discord_client"
    )
    startup_calls = [
        n.value.func.id for n in runner.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    ]
    assert startup_calls[-6:-4] == list(funcs)
