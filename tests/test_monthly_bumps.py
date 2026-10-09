from contextlib import contextmanager
from unittest.mock import patch


with patch("mysql.connector.pooling.MySQLConnectionPool"):
    from core import monthly_bumps


class FakeCursor:
    def __init__(self, fail_for_user_id: int | None = None):
        self.executed = []
        self.fail_for_user_id = fail_for_user_id

    def execute(self, query, params=None):
        if params and params[1] == self.fail_for_user_id:
            raise RuntimeError("write failed")
        self.executed.append((" ".join(query.split()), params))


def install_monthly_bump_fakes(monkeypatch, *, user_ids, cursor):
    @contextmanager
    def fake_connection(*_args, **_kwargs):
        yield cursor

    monkeypatch.setattr(monthly_bumps, "pooled_connection", fake_connection)
    monkeypatch.setattr(monthly_bumps, "getUserId", lambda discord_user_id: user_ids[discord_user_id])


def test_save_monthly_bumps_converts_discord_id_to_internal_user_id(monkeypatch):
    cursor = FakeCursor()
    install_monthly_bump_fakes(monkeypatch, user_ids={123456789012345678: 42}, cursor=cursor)

    monthly_bumps.save_monthly_bumps(99, {123456789012345678: 3})

    assert cursor.executed == [
        (
            "INSERT INTO user_records (server_guild_id, user_id, bumps) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE bumps = VALUES(bumps);",
            (99, 42, 3),
        )
    ]


def test_save_monthly_bumps_persists_more_than_one_user(monkeypatch):
    cursor = FakeCursor()
    install_monthly_bump_fakes(
        monkeypatch,
        user_ids={111111111111111111: 10, 222222222222222222: 20},
        cursor=cursor,
    )

    monthly_bumps.save_monthly_bumps(99, {111111111111111111: 1, 222222222222222222: 2})

    assert [params for _query, params in cursor.executed] == [
        (99, 10, 1),
        (99, 20, 2),
    ]


def test_save_monthly_bumps_skips_user_without_internal_association(monkeypatch, caplog):
    cursor = FakeCursor()
    install_monthly_bump_fakes(
        monkeypatch,
        user_ids={111111111111111111: 10, 999999999999999999: None},
        cursor=cursor,
    )

    monthly_bumps.save_monthly_bumps(99, {111111111111111111: 1, 999999999999999999: 7})

    assert [params for _query, params in cursor.executed] == [(99, 10, 1)]
    assert "Usuário sem associação interna ignorado" in caplog.text
    assert "999999999999999999" in caplog.text


def test_save_monthly_bumps_continues_after_user_id_resolution_error(monkeypatch, caplog):
    cursor = FakeCursor()

    @contextmanager
    def fake_connection(*_args, **_kwargs):
        yield cursor

    def fake_get_user_id(discord_user_id):
        if discord_user_id == 222222222222222222:
            raise RuntimeError("lookup failed")
        return {
            111111111111111111: 10,
            333333333333333333: 30,
        }[discord_user_id]

    monkeypatch.setattr(monthly_bumps, "pooled_connection", fake_connection)
    monkeypatch.setattr(monthly_bumps, "getUserId", fake_get_user_id)

    monthly_bumps.save_monthly_bumps(
        99,
        {
            111111111111111111: 1,
            222222222222222222: 2,
            333333333333333333: 3,
        },
    )

    assert [params for _query, params in cursor.executed] == [
        (99, 10, 1),
        (99, 30, 3),
    ]
    assert "Falha ao resolver user_id interno para bump mensal" in caplog.text
    assert "222222222222222222" in caplog.text


def test_save_monthly_bumps_continues_after_single_user_write_error(monkeypatch, caplog):
    cursor = FakeCursor(fail_for_user_id=20)
    install_monthly_bump_fakes(
        monkeypatch,
        user_ids={111111111111111111: 10, 222222222222222222: 20, 333333333333333333: 30},
        cursor=cursor,
    )

    monthly_bumps.save_monthly_bumps(
        99,
        {
            111111111111111111: 1,
            222222222222222222: 2,
            333333333333333333: 3,
        },
    )

    assert [params for _query, params in cursor.executed] == [
        (99, 10, 1),
        (99, 30, 3),
    ]
    assert "Falha ao persistir bump mensal" in caplog.text
    assert "222222222222222222" in caplog.text
