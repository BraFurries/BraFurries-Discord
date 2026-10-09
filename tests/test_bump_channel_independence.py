from contextlib import contextmanager
from unittest.mock import patch


with patch("mysql.connector.pooling.MySQLConnectionPool"):
    from core import database


class Cursor:
    def execute(self, *_args, **_kwargs):
        pass


@contextmanager
def connection():
    yield Cursor()


def test_warning_channel_update_does_not_change_monthly_channel(monkeypatch):
    saved = {}
    monkeypatch.setattr(database, "pooled_connection", connection)
    monkeypatch.setattr(database, "_ensure_bump_columns", lambda _cursor: True)
    monkeypatch.setattr(database, "updateServerConfig", lambda _guild_id, **updates: saved.update(updates) or True)

    assert database.setBumpWarningConfig(1, disboard_channel_id=11)
    assert saved == {"bump_warn_disboard_channel_id": 11}


def test_monthly_channel_update_does_not_change_warning_channel(monkeypatch):
    saved = {}
    monkeypatch.setattr(database, "pooled_connection", connection)
    monkeypatch.setattr(database, "_ensure_bump_columns", lambda _cursor: True)
    monkeypatch.setattr(database, "updateServerConfig", lambda _guild_id, **updates: saved.update(updates) or True)

    assert database.setBumpMonthlyRewardConfig(1, disboard_channel_id=22)
    assert saved == {"bump_monthly_disboard_channel_id": 22}
