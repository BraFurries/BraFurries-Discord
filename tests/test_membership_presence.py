from contextlib import nullcontext
from datetime import datetime

from core import membership_presence


class FakeCursor:
    def __init__(self, one=None, many=None, update_rowcounts=None):
        self.one = list(one or [])
        self.many = list(many or [])
        self.update_rowcounts = list(update_rowcounts or [])
        self.calls = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql.startswith("UPDATE"):
            self.rowcount = self.update_rowcounts.pop(0)

    def fetchone(self):
        return self.one.pop(0)

    def fetchall(self):
        return self.many.pop(0)


def configure(monkeypatch, cursor):
    monkeypatch.setattr(membership_presence, "pooled_connection", lambda: nullcontext(cursor))
    monkeypatch.setattr(membership_presence, "_get_community_id", lambda _cursor, _guild_id: 77)


def updates(cursor):
    return [sql for sql, _params in cursor.calls if sql.startswith("UPDATE")]


def test_mark_member_removed_updates_presence_without_touching_banned(monkeypatch):
    cursor = FakeCursor(one=[{"user_id": 10}], update_rowcounts=[1])
    configure(monkeypatch, cursor)

    assert membership_presence.mark_member_removed(1, 2, datetime(2026, 9, 10, 12, 0)) is True

    assert updates(cursor) == [
        "UPDATE user_community_status SET is_present = FALSE, left_at = %s WHERE user_id = %s AND community_id = %s"
    ]
    assert cursor.calls[-1][1] == ("2026-09-10 12:00:00", 10, 77)


def test_mark_member_removed_without_link_does_not_create_status(monkeypatch):
    cursor = FakeCursor(one=[None])
    configure(monkeypatch, cursor)

    assert membership_presence.mark_member_removed(1, 2) is False
    assert updates(cursor) == []


def test_record_unban_revokes_then_clears_banned(monkeypatch):
    cursor = FakeCursor(one=[{"user_id": 10}, {"user_id": 20}], many=[[{"id": 99}]], update_rowcounts=[1, 1])
    configure(monkeypatch, cursor)

    assert membership_presence.record_unban(1, 2, 3, "ok") is True

    assert updates(cursor)[0].startswith("UPDATE user_bans SET revoked_at")
    assert updates(cursor)[1] == "UPDATE user_community_status SET banned = FALSE WHERE user_id = %s AND community_id = %s"
    assert cursor.calls[-1][1] == (10, 77)


def test_record_unban_without_active_ban_leaves_banned_unchanged(monkeypatch):
    cursor = FakeCursor(one=[{"user_id": 10}], many=[[]])
    configure(monkeypatch, cursor)

    assert membership_presence.record_unban(1, 2, None, None) is False
    assert updates(cursor) == []


def test_record_unban_with_multiple_active_bans_leaves_banned_unchanged(monkeypatch):
    cursor = FakeCursor(one=[{"user_id": 10}], many=[[{"id": 1}, {"id": 2}]])
    configure(monkeypatch, cursor)

    assert membership_presence.record_unban(1, 2, None, None) is False
    assert updates(cursor) == []


def test_record_unban_lost_update_race_does_not_clear_banned(monkeypatch):
    cursor = FakeCursor(one=[{"user_id": 10}], many=[[{"id": 99}]], update_rowcounts=[0])
    configure(monkeypatch, cursor)

    assert membership_presence.record_unban(1, 2, None, None) is False
    assert len(updates(cursor)) == 1
    assert "user_bans" in updates(cursor)[0]


def test_record_unban_allows_unknown_actor_and_missing_reason(monkeypatch):
    cursor = FakeCursor(one=[{"user_id": 10}, None], many=[[{"id": 99}]], update_rowcounts=[1, 1])
    configure(monkeypatch, cursor)

    assert membership_presence.record_unban(1, 2, 3, None) is True
    ban_params = cursor.calls[[sql for sql, _ in cursor.calls].index(next(sql for sql, _ in cursor.calls if sql.startswith("UPDATE user_bans")))][1]
    assert ban_params[1] is None
    assert ban_params[2] is None


def test_manual_unban_revokes_identity_action_and_returns_owned_siblings(monkeypatch):
    cursor = FakeCursor(
        one=[{"user_id": 20}],
        many=[
            [{"ban_id": 99}],
            [
                {
                    "effect_id": 8,
                    "ban_id": 99,
                    "discord_user_id": 111,
                },
                {
                    "effect_id": 9,
                    "ban_id": 99,
                    "discord_user_id": 222,
                },
            ],
        ],
        update_rowcounts=[1, 1],
    )
    configure(monkeypatch, cursor)
    refreshed = []
    monkeypatch.setattr(
        membership_presence,
        "_refresh_identity_ban_membership_flags",
        lambda _cursor, ban_id, community_id: refreshed.append(
            (ban_id, community_id)
        ),
    )

    siblings = membership_presence.begin_identity_unban(
        1,
        111,
        3,
        "revogado pela staff",
    )

    assert siblings == [
        {
            "discord_user_id": 222,
            "effect_ids": [9],
        }
    ]
    assert refreshed == [(99, 77)]
    assert any(
        "UPDATE user_bans" in sql
        for sql, _params in cursor.calls
    )
    assert any(
        "UPDATE user_ban_discord_effects" in sql
        for sql, _params in cursor.calls
    )


def test_manual_unban_without_identity_action_falls_back_to_legacy_path(monkeypatch):
    cursor = FakeCursor(many=[[]])
    configure(monkeypatch, cursor)

    assert membership_presence.begin_identity_unban(
        1,
        111,
        None,
        None,
    ) is None
    assert updates(cursor) == []

