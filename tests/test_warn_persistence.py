"""Warning persistence contracts without Discord or real MariaDB connections."""
from contextlib import contextmanager
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

with patch("mysql.connector.pooling.MySQLConnectionPool"):
    from core import database


class WarningCursor:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
        self.lastrowid = 440

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchone(self):
        return next(self.replies)


def install_cursor(monkeypatch, cursor):
    @contextmanager
    def fake_pooled_connection():
        yield cursor

    monkeypatch.setattr(database, "pooled_connection", fake_pooled_connection)
    monkeypatch.setattr(
        database, "includeUser",
        lambda user, _guild: 10 if user.id == 111 else 99,
    )


def test_normal_warn_persists_date_only_and_counts_in_community(monkeypatch):
    cursor = WarningCursor([
        {"community_id": 77},
        {"warnings_count": 1},
        {"warnings_limit": 3},
    ])
    install_cursor(monkeypatch, cursor)
    result = database.warnMember(
        1234, SimpleNamespace(id=111), "Motivo legítimo",
        SimpleNamespace(id=999),
    )
    assert result == {"warningId": 440, "warningsCount": 1, "warningsLimit": 3}
    sql, bound = next((sql, params) for sql, params in cursor.calls
                      if "INSERT INTO user_warnings" in sql)
    assert "user_warnings" in sql
    assert bound[0:2] == (10, 77)
    assert type(bound[2]) is date
    assert bound[3:] == ("Motivo legítimo", 99)


def test_invalid_reason_is_rejected_without_database_or_discord_write(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("Unexpected database operation")

    monkeypatch.setattr(database, "pooled_connection", fail)
    for reason in ["", "   ", "x" * 1025]:
        assert database.warnMember(
            1234, SimpleNamespace(id=111), reason,
            SimpleNamespace(id=999),
        ) is False


def test_import_serializes_lookup_then_binds_date_without_time(monkeypatch):
    cursor = WarningCursor([
        {"community_id": 77},
        {"id": 10},  # SELECT existing user FOR UPDATE
        None,  # no duplicate warning
        {"warnings_count": 1},
        {"warnings_limit": 3},
    ])
    install_cursor(monkeypatch, cursor)
    result = database.registerWarnIfAbsent(
        1234, SimpleNamespace(id=111), "Mesmo motivo",
        SimpleNamespace(id=999), datetime(2026, 10, 29, 14, 23, 6),
    )
    assert result == {"created": True, "warningsCount": 1, "warningsLimit": 3}
    statements = [sql for sql, _ in cursor.calls]
    assert "FOR UPDATE" in statements[1]
    assert statements.index(next(s for s in statements if "FOR UPDATE" in s)) < (
        next(i for i, s in enumerate(statements) if "INSERT INTO user_warnings" in s))
    warn_lookup_sql = next(sql for sql, _ in cursor.calls
                           if "SELECT id FROM user_warnings" in sql)
    assert "FOR UPDATE" in warn_lookup_sql
    lookup = next(params for sql, params in cursor.calls
                  if "SELECT id FROM user_warnings" in sql)
    inserted = next(params for sql, params in cursor.calls
                    if "INSERT INTO user_warnings" in sql)
    assert lookup[-1] == date(2026, 10, 29)
    assert inserted[2] == date(2026, 10, 29)


def test_import_skips_legacy_duplicate_under_lock(monkeypatch):
    cursor = WarningCursor([
        {"community_id": 77},
        {"id": 10},
        {"id": 300},  # same-day reason already imported
        {"warnings_count": 2},
    ])
    install_cursor(monkeypatch, cursor)
    result = database.registerWarnIfAbsent(
        1234, SimpleNamespace(id=111), "Mesmo motivo",
        SimpleNamespace(id=999), datetime(2026, 10, 29, 14, 23, 6),
    )
    assert result == {"created": False, "warningsCount": 2}
    assert not any("INSERT INTO user_warnings" in sql for sql, _ in cursor.calls)


def test_manual_repeat_warnings_are_independent_actions(monkeypatch):
    """The index migration must not be undermined by runtime deduplication."""
    inserts = []
    for warning_id in (440, 441):
        cursor = WarningCursor([
            {"community_id": 77},
            {"warnings_count": warning_id - 439},
            {"warnings_limit": 3},
        ])
        cursor.lastrowid = warning_id
        install_cursor(monkeypatch, cursor)
        result = database.warnMember(
            1234, SimpleNamespace(id=111), "Mesma ocorrência descrita",
            SimpleNamespace(id=999),
        )
        assert result["warningId"] == warning_id
        statements = [sql for sql, _ in cursor.calls]
        assert not any("SELECT id FROM user_warnings" in sql for sql in statements)
        inserts.extend(params for sql, params in cursor.calls
                       if "INSERT INTO user_warnings" in sql)

    assert len(inserts) == 2
    assert inserts[0] == inserts[1]
    assert type(inserts[0][2]) is date


def test_warn_dataerror_logs_only_sanitized_metadata(monkeypatch, caplog):
    import mysql.connector

    cursor = WarningCursor([{"community_id": 77}])
    original_execute = cursor.execute

    def reject_insert(sql, params):
        if "INSERT INTO user_warnings" in sql:
            raise mysql.connector.DataError(
                msg="private moderation explanation must stay secret",
                errno=1406,
                sqlstate="22001",
            )
        original_execute(sql, params)

    cursor.execute = reject_insert
    install_cursor(monkeypatch, cursor)
    assert database.warnMember(
        1234, SimpleNamespace(id=111), "Motivo privado",
        SimpleNamespace(id=999),
    ) is False
    assert "errno=1406" in caplog.text
    assert "sqlstate=22001" in caplog.text
    assert "guild_id=1234" in caplog.text
    assert "Motivo privado" not in caplog.text
    assert "private moderation explanation" not in caplog.text


def test_import_rejects_invalid_reason_without_database_access(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("Unexpected database operation")

    monkeypatch.setattr(database, "pooled_connection", fail)
    for reason in ["", "   ", "x" * 1025]:
        assert database.registerWarnIfAbsent(
            1234, SimpleNamespace(id=111), reason,
            SimpleNamespace(id=999), datetime(2026, 10, 29, 14, 23, 6),
        ) is None
