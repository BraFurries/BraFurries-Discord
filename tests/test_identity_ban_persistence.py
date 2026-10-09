from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

with patch("mysql.connector.pooling.MySQLConnectionPool"):
    from core import database


class FailingCursor:
    def __init__(self):
        self.calls = 0

    def execute(self, _sql, _params):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("synthetic persistence failure")


def test_effect_batch_failure_escapes_transaction_for_rollback(monkeypatch):
    cursor = FailingCursor()
    state = {"rolled_back": False}

    @contextmanager
    def fake_pooled_connection():
        try:
            yield cursor
        except Exception:
            state["rolled_back"] = True
            raise

    monkeypatch.setattr(database, "pooled_connection", fake_pooled_connection)

    effects = [
        SimpleNamespace(
            identity_user_id=1,
            discord_user_id=111,
            is_origin=True,
            outcome="APPLIED",
            error_code=None,
        ),
        SimpleNamespace(
            identity_user_id=2,
            discord_user_id=222,
            is_origin=False,
            outcome="APPLIED",
            error_code=None,
        ),
    ]

    assert database.recordBanDiscordEffects(99, effects) is False
    assert cursor.calls == 2
    assert state["rolled_back"] is True


class ExpiredCursor:
    def __init__(self):
        self.calls = []
        self._community_read = False

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchone(self):
        if not self._community_read:
            self._community_read = True
            return {"community_id": 77}
        return None

    def fetchall(self):
        return [
            {
                "ban_id": 99,
                "user_id": 10,
                "effect_id": None,
                "discord_user_id": None,
            }
        ]


def test_expired_query_emits_finalize_only_action_when_no_applied_effect_is_pending(monkeypatch):
    cursor = ExpiredCursor()

    @contextmanager
    def fake_pooled_connection():
        yield cursor

    monkeypatch.setattr(database, "pooled_connection", fake_pooled_connection)

    rows = database.getExpiredBans(1234)

    assert rows[0]["discord_user_id"] is None
    sql, params = cursor.calls[-1]
    assert "NULL AS discord_user_id" in sql
    assert "effects.outcome = 'APPLIED'" in sql
    assert "effects.reverted_at IS NULL" in sql
    assert "OR ub.revoked_at IS NOT NULL" in sql
    assert params == (77, 77, 77)


class ScriptedCursor:
    def __init__(self, *, fail_on, fetchone_values, lastrowid=123):
        self.fail_on = fail_on
        self.fetchone_values = list(fetchone_values)
        self.lastrowid = lastrowid
        self.calls = 0

    def execute(self, _sql, _params):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("synthetic transaction failure")

    def fetchone(self):
        return self.fetchone_values.pop(0)


def rollback_context(cursor, state):
    @contextmanager
    def fake_pooled_connection():
        try:
            yield cursor
        except Exception:
            state["rolled_back"] = True
            raise

    return fake_pooled_connection


def test_register_ban_rolls_back_parent_if_membership_flag_update_fails(monkeypatch):
    cursor = ScriptedCursor(
        fail_on=3,
        fetchone_values=[{"community_id": 77}],
    )
    state = {"rolled_back": False}
    monkeypatch.setattr(
        database,
        "pooled_connection",
        rollback_context(cursor, state),
    )
    monkeypatch.setattr(
        database,
        "includeUser",
        lambda user, _guild_id: 10 if user.id == 111 else 99,
    )

    result = database.registerUserBan(
        1234,
        SimpleNamespace(id=111),
        "regra",
        SimpleNamespace(id=999),
        valid_until=None,
        can_appeal=True,
    )

    assert result is False
    assert cursor.calls == 3
    assert state["rolled_back"] is True


def test_remove_ban_rolls_back_revocation_if_membership_flag_update_fails(monkeypatch):
    cursor = ScriptedCursor(
        fail_on=3,
        fetchone_values=[{"user_id": 10, "community_id": 77}],
    )
    state = {"rolled_back": False}
    monkeypatch.setattr(
        database,
        "pooled_connection",
        rollback_context(cursor, state),
    )

    assert database.removeBanRecord(99) is False
    assert cursor.calls == 3
    assert state["rolled_back"] is True


class OtherActiveBanCursor:
    def __init__(self):
        self.calls = []
        self._reads = iter([
            {"community_id": 77},
            {"present": 1},
        ])

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchone(self):
        return next(self._reads)


def test_other_active_ban_requirement_blocks_early_unban(monkeypatch):
    cursor = OtherActiveBanCursor()

    @contextmanager
    def fake_pooled_connection():
        yield cursor

    monkeypatch.setattr(database, "pooled_connection", fake_pooled_connection)

    assert database.hasOtherActiveBanRequirement(1234, 99, 222) is True
    sql, params = cursor.calls[-1]
    assert "other_ban.id <> %s" in sql
    assert "other_ban.valid_until > NOW()" in sql
    assert "other_effect.discord_user_id = %s" in sql
    assert "legacy_account.discord_user_id = %s" in sql
    assert params == (77, 99, 222, 222)


class ActiveBanTenantCursor:
    def __init__(self):
        self.sql = ""
        self.params = ()
        self._row = {"present": 1}

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchone(self):
        return self._row


def test_ban_belongs_to_guild_requires_parent_action_to_still_be_active(monkeypatch):
    cursor = ActiveBanTenantCursor()

    @contextmanager
    def fake_pooled_connection():
        yield cursor

    monkeypatch.setattr(database, "pooled_connection", fake_pooled_connection)

    assert database.banBelongsToGuild(77, 1234) is True
    assert "ub.revoked_at IS NULL" in cursor.sql
    assert "ub.valid_until IS NULL" in cursor.sql
    assert "ub.valid_until > NOW()" in cursor.sql
    assert cursor.params == (77, 1234)


class SatisfiedEffectsCursor:
    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return [
            {"discord_user_id": 111},
            {"discord_user_id": 222},
        ]


def test_satisfied_effect_lookup_is_scoped_to_one_ban(monkeypatch):
    cursor = SatisfiedEffectsCursor()

    @contextmanager
    def fake_pooled_connection():
        yield cursor

    monkeypatch.setattr(database, "pooled_connection", fake_pooled_connection)

    assert database.getSatisfiedBanEffectDiscordIds(77) == {111, 222}
    assert "ban_id = %s" in cursor.sql
    assert "APPLIED" in cursor.sql
    assert "ALREADY_BANNED" in cursor.sql
    assert cursor.params == (77,)


class MembershipEffectCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))


def test_recorded_ban_effects_mark_existing_identity_memberships_banned(monkeypatch):
    cursor = MembershipEffectCursor()

    @contextmanager
    def fake_pooled_connection():
        yield cursor

    monkeypatch.setattr(database, "pooled_connection", fake_pooled_connection)

    effects = [
        SimpleNamespace(
            identity_user_id=10,
            discord_user_id=111,
            is_origin=True,
            outcome="APPLIED",
            error_code=None,
        ),
        SimpleNamespace(
            identity_user_id=20,
            discord_user_id=222,
            is_origin=False,
            outcome="FAILED",
            error_code="Forbidden",
        ),
    ]

    assert database.recordBanDiscordEffects(77, effects) is True

    membership_updates = [
        (sql, params)
        for sql, params in cursor.calls
        if "UPDATE user_community_status" in sql
    ]
    assert len(membership_updates) == 2
    assert {params[0] for _sql, params in membership_updates} == {10, 20}
    assert all(params[1] == 77 for _sql, params in membership_updates)
