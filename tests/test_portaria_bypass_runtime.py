from contextlib import contextmanager
from datetime import datetime

import core.database as database


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None


@contextmanager
def fake_connection(cursor):
    yield cursor


def test_invite_code_normalization_preserves_case():
    assert database.normalize_discord_invite_code("https://discord.gg/AbC123?x=1") == "AbC123"


def test_bypasses_are_ineffective_when_portaria_is_disabled(monkeypatch):
    monkeypatch.setattr(
        database,
        "get_portaria_base_config",
        lambda guild_id: {"portaria_enabled": 0},
    )
    monkeypatch.setattr(
        database,
        "pooled_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("storage must not be queried while Portaria is disabled")
        ),
    )

    assert database.get_portaria_invite_bypass_codes(123) == []
    assert database.get_portaria_account_release_override(123, 555) is None


def test_invite_bypass_reads_canonical_codes_only(monkeypatch):
    cursor = FakeCursor([
        {"invite_code": "CaseSensitive"},
        {"invite_code": "AnotherCode"},
    ])
    monkeypatch.setattr(
        database,
        "get_portaria_base_config",
        lambda guild_id: {"portaria_enabled": 1},
    )
    monkeypatch.setattr(
        database,
        "pooled_connection",
        lambda *args, **kwargs: fake_connection(cursor),
    )

    assert database.get_portaria_invite_bypass_codes(123) == [
        "CaseSensitive",
        "AnotherCode",
    ]



class SequentialCursor(FakeCursor):
    def __init__(self, result_sets):
        super().__init__([])
        self.result_sets = list(result_sets)
        self.current = []

    def execute(self, query, params=None):
        super().execute(query, params)
        self.current = self.result_sets.pop(0) if self.result_sets else []

    def fetchall(self):
        return list(self.current)


def test_inactive_canonical_account_row_tombstones_legacy_release(monkeypatch):
    cursor = SequentialCursor([
        [{
            "server_guild_id": 123,
            "user_id": 555,
            "access_mode": "completo",
            "requires_form": 0,
            "active": 0,
            "expires_at": None,
            "expired_at": None,
            "removed_at": None,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }],
        [{
            "server_guild_id": 123,
            "user_id": 555,
            "access_mode": "completo",
            "requires_form": 0,
            "released_by": 1,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }],
    ])
    monkeypatch.setattr(
        database,
        "get_portaria_base_config",
        lambda guild_id: {"portaria_enabled": 1},
    )
    monkeypatch.setattr(
        database,
        "pooled_connection",
        lambda *args, **kwargs: fake_connection(cursor),
    )

    assert database.get_portaria_account_release_override(123, 555) is None
    assert len(cursor.executed) == 1


def test_account_release_uses_legacy_bridge_only_without_canonical_row(monkeypatch):
    legacy = {
        "server_guild_id": 123,
        "user_id": 555,
        "access_mode": "provisorio",
        "requires_form": 1,
        "released_by": 1,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    cursor = SequentialCursor([[], [legacy]])
    monkeypatch.setattr(
        database,
        "get_portaria_base_config",
        lambda guild_id: {"portaria_enabled": 1},
    )
    monkeypatch.setattr(
        database,
        "pooled_connection",
        lambda *args, **kwargs: fake_connection(cursor),
    )

    assert database.get_portaria_account_release_override(123, 555) == legacy
    assert len(cursor.executed) == 2
