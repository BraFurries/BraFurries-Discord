from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch
from types import SimpleNamespace


with patch("mysql.connector.pooling.MySQLConnectionPool"):
    from core import database


class FakeDiscordUser:
    def __init__(self, user_id, name):
        self.id = user_id
        self.name = name
        self.global_name = name


class FakeDiscordMember(FakeDiscordUser):
    def __init__(self, user_id, name, display_name):
        super().__init__(user_id, name)
        self.display_name = display_name
        self.nick = display_name
        self.joined_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.guild = type("Guild", (), {"roles": []})()
        self.roles = []


class FakeCursor:
    def __init__(self, responses, lastrowid=42):
        self.responses = iter(responses)
        self.current = None
        self.lastrowid = lastrowid
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((" ".join(query.split()), params))
        if query.lstrip().upper().startswith("SELECT"):
            self.current = next(self.responses)

    def fetchone(self):
        return self.current

    def fetchall(self):
        return self.current


class DuplicateUsernameCursor(FakeCursor):
    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.executed.append((normalized, params))
        if normalized.startswith("INSERT INTO users"):
            raise database.mysql.connector.Error(
                msg="Duplicate username",
                errno=database.errorcode.ER_DUP_ENTRY,
            )
        if query.lstrip().upper().startswith("SELECT"):
            self.current = next(self.responses)


class DuplicateMembershipCursor(FakeCursor):
    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.executed.append((normalized, params))
        if normalized.startswith("INSERT INTO user_community_status"):
            raise database.mysql.connector.Error(
                msg="Duplicate membership",
                errno=database.errorcode.ER_DUP_ENTRY,
            )
        if query.lstrip().upper().startswith("SELECT"):
            self.current = next(self.responses)


def install_database_fakes(monkeypatch, cursor):
    @contextmanager
    def fake_connection(*_args, **_kwargs):
        yield cursor

    monkeypatch.setattr(database, "pooled_connection", fake_connection)
    monkeypatch.setattr(database, "getGuildMemberNotVerifiedRoleId", lambda _guild_id: None)
    monkeypatch.setattr(database.discord, "User", FakeDiscordUser)
    monkeypatch.setattr(database.discord, "Member", FakeDiscordMember)


def test_new_discord_guild_is_not_created_by_runtime_sql(monkeypatch):
    cursor = FakeCursor([None])

    @contextmanager
    def fake_connection(*_args, **_kwargs):
        yield cursor

    monkeypatch.setattr(database, "pooled_connection", fake_connection)
    guild = SimpleNamespace(id=456, name="Nova guild", owner_id=123, member_count=10)

    database.ensure_community_registration_for_guilds([guild])

    assert any(
        query.startswith("SELECT community_id FROM community_discord")
        and params == (456,)
        for query, params in cursor.executed
    )
    assert not any("INSERT INTO communities" in query for query, _params in cursor.executed)
    assert not any("INSERT INTO community_discord" in query for query, _params in cursor.executed)
    assert not any("UPDATE community_discord" in query for query, _params in cursor.executed)


def test_registered_discord_guild_only_gets_runtime_settings(monkeypatch):
    cursor = FakeCursor([{"community_id": 7}])

    @contextmanager
    def fake_connection(*_args, **_kwargs):
        yield cursor

    monkeypatch.setattr(database, "pooled_connection", fake_connection)
    guild = SimpleNamespace(id=456, name="Guild", owner_id=123, member_count=10)

    database.ensure_community_registration_for_guilds([guild])

    assert any(
        query.startswith("INSERT IGNORE INTO config_server_settings")
        and params == (456,)
        for query, params in cursor.executed
    )
    assert not any("UPDATE community_discord" in query for query, _params in cursor.executed)


def test_normalize_text_preserves_32_character_discord_name():
    name = "á" * 16 + "猫" * 16
    assert database.normalize_text(name) == name
    assert len(database.normalize_text(name)) == 32


def test_normalize_text_does_not_expand_nfkd_ligatures():
    name = "ﬆ" * 32
    assert database.normalize_text(name) == name
    assert len(database.normalize_text(name)) == 32


def test_normalize_text_uses_nfc_and_preserves_non_ascii():
    assert database.normalize_text("Cafe\u0301 — 東京") == "Café — 東京"


def test_include_user_updates_existing_discord_user_from_user(monkeypatch):
    cursor = FakeCursor([
        {"community_id": 7},
        {"user_id": 9, "username": "nome_antigo", "display_name": "Nome antigo"},
        {"id": 9},
        {"id": 100, "approved_at": None},
    ])
    install_database_fakes(monkeypatch, cursor)

    user_id = database.includeUser(FakeDiscordUser(123, "José 猫"), guildId=456)

    assert user_id == 9
    assert any(
        query.startswith("UPDATE user_discord SET username")
        and params == ("José 猫", "José 猫", 9)
        for query, params in cursor.executed
    )
    assert not any(
        query.startswith("UPDATE users SET display_name")
        for query, _params in cursor.executed
    )
    assert not any("UPDATE user_telegram" in query for query, _params in cursor.executed)


def test_include_user_updates_existing_discord_user_from_member(monkeypatch):
    cursor = FakeCursor([
        {"community_id": 7},
        {"user_id": 9, "username": "nome_antigo", "display_name": "Nome antigo"},
        {"id": 9},
        {"id": 100, "approved_at": None},
    ])
    install_database_fakes(monkeypatch, cursor)

    user_id = database.includeUser(
        FakeDiscordMember(123, "discord_user", "Apelido novo"),
        guildId=456,
    )

    assert user_id == 9
    assert any(
        query.startswith("UPDATE user_discord SET username")
        and params == ("discord_user", "discord_user", 9)
        for query, params in cursor.executed
    )
    assert not any(
        params and "Apelido novo" in params
        for query, params in cursor.executed
        if "user_discord" in query
    )
    assert not any("UPDATE user_telegram" in query for query, _params in cursor.executed)


def test_include_user_updates_existing_telegram_user_from_text(monkeypatch):
    cursor = FakeCursor([
        {"community_id": 7},
        {"user_id": 9, "username": "nome_antigo", "display_name": "Nome antigo"},
        {"id": 9},
        {"id": 100, "approved_at": None},
    ])
    install_database_fakes(monkeypatch, cursor)

    user_id = database.includeUser("telegram_novo", guildId=456)

    assert user_id == 9
    assert any(
        query.startswith("UPDATE user_telegram SET username")
        and params == ("telegram_novo", "telegram_novo", 9)
        for query, params in cursor.executed
    )
    assert not any("UPDATE user_discord" in query for query, _params in cursor.executed)


def test_include_user_inserts_users_and_user_discord(monkeypatch):
    display_name = "á" * 16 + "猫" * 16
    cursor = FakeCursor([
        {"community_id": 7},
        None,
        {"id": 42},
        None,
        {"user_id": 42},
    ], lastrowid=42)
    install_database_fakes(monkeypatch, cursor)

    user_id = database.includeUser(
        FakeDiscordMember(123, "discord_user", display_name),
        guildId=456,
    )

    assert user_id == 42
    assert any(
        query.startswith("INSERT INTO users") and params is None
        for query, params in cursor.executed
    )
    assert any(
        query.startswith("INSERT INTO user_discord")
        and params == (42, 123, "discord_user", "discord_user")
        for query, params in cursor.executed
    )


def test_include_user_duplicate_username_reuses_existing_membership(monkeypatch):
    cursor = DuplicateUsernameCursor([
        {"community_id": 7},
        None,
        {"id": 9},
        {"id": 9},
        {"id": 100, "approved_at": None},
        {"user_id": 9},
    ])
    install_database_fakes(monkeypatch, cursor)

    user_id = database.includeUser(
        FakeDiscordMember(123, "discord_user", "Nome"),
        guildId=456,
    )

    assert user_id == 9
    assert any(
        query == "SELECT id FROM users WHERE id = %s FOR UPDATE" and params == (9,)
        for query, params in cursor.executed
    )
    assert not any(
        query.startswith("INSERT INTO user_community_status")
        for query, _params in cursor.executed
    )
    assert not any(
        query.startswith("UPDATE user_community_status")
        for query, _params in cursor.executed
    )


def test_include_user_duplicate_membership_insert_reloads_and_refreshes(monkeypatch):
    cursor = DuplicateMembershipCursor([
        {"community_id": 7},
        {"user_id": 9, "username": "discord_user", "display_name": "Nome"},
        {"id": 9},
        None,
        {"id": 100, "approved_at": None},
    ])
    install_database_fakes(monkeypatch, cursor)

    user_id = database.includeUser(
        FakeDiscordMember(123, "discord_user", "Nome"),
        guildId=456,
    )

    assert user_id == 9
    assert any(
        query.startswith("INSERT INTO user_community_status")
        for query, _params in cursor.executed
    )
    assert not any(
        query.startswith("UPDATE user_community_status")
        for query, _params in cursor.executed
    )


def test_include_user_fallback_does_not_mark_non_member_discord_user_present(monkeypatch):
    cursor = FakeCursor([
        {"community_id": 7},
        None,
        {"id": 42},
        None,
        {"user_id": 42},
    ], lastrowid=42)
    install_database_fakes(monkeypatch, cursor)

    user_id = database.includeUser(
        FakeDiscordUser(123, "discord_user"),
        guildId=456,
    )

    assert user_id == 42
    membership_inserts = [
        (query, params)
        for query, params in cursor.executed
        if query.startswith("INSERT INTO user_community_status")
    ]
    assert len(membership_inserts) == 1
    assert membership_inserts[0][1][-1] is False


def test_include_user_existing_membership_never_rewrites_member_since(monkeypatch):
    cursor = FakeCursor([
        {"community_id": 7},
        {"user_id": 9, "username": "discord_user", "display_name": "Nome"},
        {"id": 9},
        {"id": 100, "approved_at": None},
    ])
    install_database_fakes(monkeypatch, cursor)

    database.includeUser(
        FakeDiscordMember(123, "discord_user", "Nome"),
        guildId=456,
    )

    assert not any(
        query.startswith("UPDATE user_community_status")
        for query, _params in cursor.executed
    )


def test_profile_read_does_not_rewrite_api_owned_approval(monkeypatch):
    db_user = {
        "discord_user_id": 123,
        "display_name": "Nome",
        "member_since": datetime(2026, 1, 1),
        "approved": True,
        "approved_at": datetime(2026, 1, 2),
        "is_vip": False,
        "is_partner": False,
        "current_level": 1,
        "birth_date": None,
        "verified": False,
        "locale_name": None,
        "bank_balance": 0,
    }
    cursor = FakeCursor([
        {"community_id": 7},
        db_user,
        [],
        {"notes_count": 0},
        [],
        None,
        None,
    ])
    install_database_fakes(monkeypatch, cursor)
    monkeypatch.setattr(database, "includeUser", lambda _user, _guild_id: 9)
    monkeypatch.setattr(
        database,
        "getGuildMemberNotVerifiedRoleId",
        lambda _guild_id: 999,
    )

    member = FakeDiscordMember(123, "discord_user", "Nome")
    member.roles = [SimpleNamespace(id=999)]

    profile = database.getProfileData(456, member)

    assert profile.approved is False
    assert not any(
        query.startswith("UPDATE user_community_status SET approved")
        for query, _params in cursor.executed
    )


def test_portaria_invite_bypass_is_ineffective_when_portaria_is_disabled(monkeypatch):
    monkeypatch.setattr(
        database,
        "get_portaria_base_config",
        lambda _guild_id: {"portaria_enabled": 0, "invite_bypass_codes": "legacy"},
    )

    def unexpected_connection(*_args, **_kwargs):
        raise AssertionError("bypass storage must not be read while Portaria is disabled")

    monkeypatch.setattr(database, "pooled_connection", unexpected_connection)

    assert database.get_portaria_invite_bypass_codes(456) == []


def test_portaria_account_bypass_is_ineffective_when_portaria_is_disabled(monkeypatch):
    monkeypatch.setattr(
        database,
        "get_portaria_base_config",
        lambda _guild_id: {"portaria_enabled": 0},
    )

    def unexpected_connection(*_args, **_kwargs):
        raise AssertionError("bypass storage must not be read while Portaria is disabled")

    monkeypatch.setattr(database, "pooled_connection", unexpected_connection)

    assert database.get_portaria_account_release_override(456, 123) is None


def test_collaborative_moderation_reads_only_requested_guild(monkeypatch):
    cursor = FakeCursor([
        [
            {"COLUMN_NAME": "collab_moderation_enabled"},
            {"COLUMN_NAME": "collab_moderation_emoji"},
            {"COLUMN_NAME": "collab_moderation_min_reactions"},
        ],
        {
            "collab_moderation_enabled": 1,
            "collab_moderation_emoji": "🧹",
            "collab_moderation_min_reactions": 4,
        },
    ])

    @contextmanager
    def fake_connection(*_args, **_kwargs):
        yield cursor

    monkeypatch.setattr(database, "pooled_connection", fake_connection)

    result = database.getCollaborativeModerationConfig(456)

    assert result == {"enabled": True, "emoji": "🧹", "minReactions": 4}
    assert any(
        "FROM config_server_settings WHERE server_guild_id = %s" in query
        and params == (456,)
        for query, params in cursor.executed
    )


def test_runtime_moderation_configuration_writers_are_disabled(monkeypatch):
    def unexpected_connection(*_args, **_kwargs):
        raise AssertionError("runtime administrative writers must not touch the database")

    monkeypatch.setattr(database, "pooled_connection", unexpected_connection)

    assert database.setStaffRoles(456, [10, 11]) is False
    assert database.setCollaborativeModerationConfig(
        456,
        enabled=True,
        emoji="🧹",
        min_reactions=3,
    ) is False
