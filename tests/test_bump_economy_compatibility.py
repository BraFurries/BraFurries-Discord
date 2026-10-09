from contextlib import contextmanager
from unittest.mock import Mock, patch


with patch("mysql.connector.pooling.MySQLConnectionPool"):
    from core import database


class Cursor:
    def __init__(self, row=None):
        self.row = row
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((" ".join(query.split()), params))

    def fetchone(self):
        return self.row


def connection_for(cursor):
    @contextmanager
    def connection():
        yield cursor
    return connection


def test_canonical_reader_preserves_missing_null_zero_and_explicit_values(monkeypatch):
    monkeypatch.setattr(database, "_ensure_economy_config_columns", lambda _cursor: True)
    cases = [
        (None, {"exists": False, "initialized": False, "enabled": False, "points": None}),
        ({"bump_reward_enabled": 0, "bump_points": None},
         {"exists": True, "initialized": False, "enabled": False, "points": None}),
        ({"bump_reward_enabled": 0, "bump_points": 0},
         {"exists": True, "initialized": True, "enabled": False, "points": 0}),
        ({"bump_reward_enabled": 1, "bump_points": 25},
         {"exists": True, "initialized": True, "enabled": True, "points": 25}),
        ({"bump_reward_enabled": 0, "bump_points": 25},
         {"exists": True, "initialized": True, "enabled": False, "points": 25}),
    ]
    for row, expected in cases:
        cursor = Cursor(row)
        monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))
        assert database.get_bump_reward_economy_config(1) == expected
        assert all(not query.startswith(("UPDATE ", "INSERT ")) for query, _params in cursor.executed)


def test_effective_bump_config_uses_legacy_only_for_uninitialized_canonical_state(monkeypatch):
    server_row = {"bump_reward_coins": 25}
    cursor = Cursor(server_row)
    monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))
    monkeypatch.setattr(database, "_ensure_bump_columns", lambda _cursor: True)
    setter = Mock()
    monkeypatch.setattr(database, "set_bump_reward_economy_config", setter)

    cases = [
        ({"exists": False, "initialized": False, "enabled": False, "points": None}, True, 25),
        ({"exists": True, "initialized": False, "enabled": False, "points": None}, True, 25),
        ({"exists": True, "initialized": True, "enabled": False, "points": 0}, False, 0),
        ({"exists": True, "initialized": True, "enabled": True, "points": 10}, True, 10),
        ({"exists": True, "initialized": True, "enabled": False, "points": 10}, False, 10),
    ]
    for canonical, enabled, points in cases:
        monkeypatch.setattr(database, "get_bump_reward_economy_config", lambda _guild_id, value=canonical: value)
        config = database.getBumpConfig(1)
        assert config["rewardCoinsEnabled"] is enabled
        assert config["rewardCoins"] == points

    setter.assert_not_called()
    assert all(not query.startswith("UPDATE config_economy") for query, _params in cursor.executed)


def test_get_bump_reward_points_treats_null_as_zero(monkeypatch):
    monkeypatch.setattr(database, "get_bump_reward_economy_config", lambda _guild_id: {"points": None})
    assert database.get_bump_reward_points(1) == 0


def test_setter_does_not_insert_incomplete_economy_row(monkeypatch):
    cursor = Cursor(None)
    monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))
    monkeypatch.setattr(database, "_ensure_economy_config_columns", lambda _cursor: True)

    assert not database.set_bump_reward_economy_config(1, enabled=True, points=25)
    queries = [query for query, _params in cursor.executed]
    assert queries == ["SELECT 1 FROM config_economy WHERE server_guild_id = %s LIMIT 1"]
    assert not any(query.startswith("INSERT") for query in queries)
