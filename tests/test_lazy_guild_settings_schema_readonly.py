"""Guild-settings DDL retirement verified with the 2026-10-19 MariaDB schema.

No database connections or Discord clients are used in these tests.
"""

import ast
from pathlib import Path
from unittest.mock import patch

import pytest

with patch("mysql.connector.pooling.MySQLConnectionPool"):
    from core import database


# Independently mirrored from production's config_server_settings definition.
PRODUCTION_COLUMNS = {
    "auto_join_roles_enabled", "auto_join_role_ids", "birthday_channel_id",
    "hashtag_role_mentions_enabled", "hashtag_role_mentions_channel_ids",
    "hashtag_role_mentions_author_role_id", "hashtag_role_mentions_map",
    "thread_owner_only_enabled", "thread_owner_only_forum_channel_ids",
    "bump_warn_enabled", "bump_warn_disboard_channel_id",
    "bump_warn_target_channel_id", "bump_warn_messages",
    "bump_warn_next_at", "bump_warn_last_bump_at", "bump_reward_enabled",
    "bump_reward_temp_role_id", "bump_reward_role_minutes",
    "bump_reward_coins", "bump_reward_message",
    "bump_monthly_enabled", "bump_monthly_disboard_channel_id",
    "bump_monthly_reward_role_id", "bump_monthly_reward_days_1",
    "bump_monthly_reward_days_2", "bump_monthly_reward_days_3",
    "bump_monthly_reward_coins_1", "bump_monthly_reward_coins_2",
    "bump_monthly_reward_coins_3",
}

DOMAINS = {
    "_ensure_auto_join_roles_columns": {
        "auto_join_roles_enabled", "auto_join_role_ids",
    },
    "_ensure_birthday_channel_column": {"birthday_channel_id"},
    "_ensure_hashtag_role_mentions_columns": {
        "hashtag_role_mentions_enabled", "hashtag_role_mentions_channel_ids",
        "hashtag_role_mentions_author_role_id", "hashtag_role_mentions_map",
    },
    "_ensure_thread_owner_only_columns": {
        "thread_owner_only_enabled", "thread_owner_only_forum_channel_ids",
    },
    "_ensure_bump_columns": {
        "bump_warn_enabled", "bump_warn_disboard_channel_id",
        "bump_warn_target_channel_id", "bump_warn_messages",
        "bump_warn_next_at", "bump_warn_last_bump_at",
        "bump_reward_enabled", "bump_reward_temp_role_id",
        "bump_reward_role_minutes", "bump_reward_coins",
        "bump_reward_message", "bump_monthly_enabled",
        "bump_monthly_disboard_channel_id", "bump_monthly_reward_role_id",
        "bump_monthly_reward_days_1", "bump_monthly_reward_days_2",
        "bump_monthly_reward_days_3", "bump_monthly_reward_coins_1",
        "bump_monthly_reward_coins_2", "bump_monthly_reward_coins_3",
    },
}


class ColumnsCursor:
    def __init__(self, *, missing=None):
        self.missing = missing
        self.queries = []
        self.current = None

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        assert sql.lstrip().startswith("SELECT ")
        assert "INFORMATION_SCHEMA.COLUMNS" in sql
        assert "TABLE_SCHEMA = DATABASE()" in sql
        assert len(params) == 2 and params[0] == "config_server_settings"
        self.current = params[1]

    def fetchone(self):
        return None if self.current == self.missing else {"exists": 1}


def test_snapshot_contains_all_lazily_checked_columns():
    assert set().union(*DOMAINS.values()) == PRODUCTION_COLUMNS


@pytest.mark.parametrize("function_name", sorted(DOMAINS))
def test_readonly_guard_accepts_existing_schema(function_name):
    cursor = ColumnsCursor()
    assert getattr(database, function_name)(cursor) is True
    checked = [params[1] for _sql, params in cursor.queries]
    assert set(checked) == DOMAINS[function_name]
    assert len(checked) == len(DOMAINS[function_name])


@pytest.mark.parametrize("function_name", sorted(DOMAINS))
def test_guard_fails_closed_without_ddl_when_any_column_missing(function_name, caplog):
    missing = sorted(DOMAINS[function_name])[0]
    cursor = ColumnsCursor(missing=missing)
    assert getattr(database, function_name)(cursor) is False
    assert all(sql.lstrip().startswith("SELECT ") for sql, _ in cursor.queries)
    assert missing in caplog.text


def test_all_five_lazy_guards_call_shared_readonly_schema_checker():
    root = Path(__file__).resolve().parents[1]
    src = (root / "core/database.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for function_name in DOMAINS:
        fn = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == function_name
        )
        body = ast.get_source_segment(src, fn)
        assert "_check_columns(" in body
        assert "config_server_settings" in body
        for forbidden in ("ALTER TABLE", "CREATE TABLE", "DROP TABLE", "UPDATE "):
            assert forbidden not in body
