import asyncio
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from core.backup_runtime import BackupDecisionRequired, BackupRestoreEngine, periodic_idempotency_key

with patch("mysql.connector.pooling.MySQLConnectionPool"):
    from core import database


class FakeCursor:
    def __init__(self):
        self.executed = []
        self.current = None
        self.lastrowid = 100

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.executed.append((normalized, params))

    def fetchone(self):
        return self.current

    def fetchall(self):
        return self.current or []


@contextmanager
def fake_connection(cursor):
    yield cursor


class SchemaCursor(FakeCursor):
    required_columns = {
        "backup_discord": {
            "id", "guild_id", "original_name", "backup_type",
            "created_by_discord_user_id", "idempotency_key", "created_at",
        },
        "backup_roles": {
            "id", "backup_id", "discord_id", "name", "color", "permissions",
            "position", "hoist", "mentionable",
        },
        "backup_channels": {
            "id", "backup_id", "discord_id", "parent_id", "name", "type",
            "position", "topic", "nsfw",
        },
        "backup_overwrites": {
            "id", "channel_id", "role_name", "target_type", "target_discord_id",
            "role_id", "allow_bits", "deny_bits",
        },
        "backup_server_settings": {
            "id", "guild_id", "max_normal_backups", "max_periodic_backups",
            "periodic_backups_enabled", "periodicity_minutes",
            "periodicity_frequency", "bot_removed_at", "purge_after",
            "created_at", "updated_at",
        },
        "backup_restore_operations": {
            "id", "guild_id", "backup_id", "operation_key", "scope",
            "actor_discord_user_id", "actor_user_id", "status", "decision_json",
            "result_json", "error_code", "progress_current", "progress_total",
            "current_step", "started_at", "finished_at", "created_at", "updated_at",
        },
        "backup_snapshot_operations": {
            "id", "guild_id", "operation_key", "backup_type", "requested_name",
            "actor_discord_user_id", "actor_user_id", "status", "backup_id",
            "progress_current", "progress_total", "current_step", "result_json",
            "error_code", "started_at", "finished_at", "created_at", "updated_at",
        },
        "backup_operation_steps": {
            "id", "guild_id", "operation_kind", "operation_id", "step_code",
            "step_status", "message", "progress_current", "progress_total",
            "detail_json", "created_at",
        },
        "backup_guild_locks": {
            "guild_id", "lock_token", "operation_type", "operation_id",
            "expires_at", "created_at", "updated_at",
        },
    }

    def __init__(self, *, missing_table=None):
        super().__init__()
        self.missing_table = missing_table

    def execute(self, query, params=None):
        super().execute(query, params)
        normalized = " ".join(query.split())
        if "information_schema.COLUMNS" in normalized:
            table = params[0]
            columns = set() if table == self.missing_table else self.required_columns[table]
            self.current = [{"COLUMN_NAME": item} for item in columns]
        elif "information_schema.STATISTICS" in normalized:
            self.current = [{"ok": 1}]


def test_backup_schema_validator_is_check_only(monkeypatch):
    cursor = SchemaCursor()
    monkeypatch.setattr(database, "pooled_connection", lambda *_a, **_k: fake_connection(cursor))

    database.initialize_discord_backup_tables()

    sql = "\n".join(query.upper() for query, _params in cursor.executed)
    assert "CREATE TABLE" not in sql
    assert "ALTER TABLE" not in sql
    assert "DROP TABLE" not in sql


def test_backup_schema_validator_fails_clearly_when_flyway_schema_is_missing(monkeypatch):
    cursor = SchemaCursor(missing_table="backup_restore_operations")
    monkeypatch.setattr(database, "pooled_connection", lambda *_a, **_k: fake_connection(cursor))

    with pytest.raises(RuntimeError, match="backup_schema_missing:backup_restore_operations"):
        database.initialize_discord_backup_tables()


class SnapshotCursor(FakeCursor):
    def __init__(self, *, limit=5, existing_id=None, fail_role=False, retained_ids=None):
        super().__init__()
        self.limit = limit
        self.existing_id = existing_id
        self.fail_role = fail_role
        self.retained_ids = retained_ids or []
        self.lastrowid = 501

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SELECT id FROM backup_discord") and "idempotency_key" in normalized:
            self.current = {"id": self.existing_id} if self.existing_id is not None else None
        elif normalized.startswith("SELECT guild_id, max_normal_backups"):
            self.current = {
                "guild_id": 10,
                "max_normal_backups": self.limit,
                "max_periodic_backups": 2,
                "periodic_backups_enabled": 1,
                "periodicity_minutes": 10080,
                "periodicity_frequency": "weekly",
            }
        elif normalized.startswith("INSERT INTO backup_roles") and self.fail_role:
            raise RuntimeError("role insert failed")
        elif normalized.startswith("SELECT id FROM backup_discord") and "backup_type" in normalized:
            self.current = [{"id": item} for item in self.retained_ids]
        elif normalized.startswith("INSERT INTO backup_discord"):
            self.lastrowid = 501
            self.current = None
        else:
            self.current = None


def test_snapshot_failure_never_prunes_valid_backups_before_new_snapshot_is_complete(monkeypatch):
    cursor = SnapshotCursor(limit=2, fail_role=True)
    monkeypatch.setattr(database, "pooled_connection", lambda *_a, **_k: fake_connection(cursor))

    with pytest.raises(RuntimeError, match="role insert failed"):
        database.create_discord_backup_snapshot(
            guild_id=10,
            original_name="Before refactor",
            roles=[{
                "discord_id": 1,
                "name": "Role",
                "color": 0,
                "permissions": 0,
                "position": 1,
                "hoist": False,
                "mentionable": False,
            }],
            channels=[],
            overwrites=[],
        )

    assert not any(query.startswith("DELETE FROM backup_discord") for query, _ in cursor.executed)


def test_single_slot_policy_ignores_legacy_zero_setting_and_keeps_new_snapshot(monkeypatch):
    cursor = SnapshotCursor(limit=0, retained_ids=[501, 400])
    monkeypatch.setattr(database, "pooled_connection", lambda *_a, **_k: fake_connection(cursor))

    assert database.create_discord_backup_snapshot(
        guild_id=10,
        original_name="Replacement",
        roles=[],
        channels=[],
        overwrites=[],
    ) == 501

    assert any(query.startswith("INSERT INTO backup_discord") for query, _ in cursor.executed)
    assert any(
        query.startswith("DELETE FROM backup_discord") and params == (400, 10)
        for query, params in cursor.executed
    )


def test_snapshot_retry_reuses_guild_scoped_idempotency_key(monkeypatch):
    cursor = SnapshotCursor(existing_id=777)
    monkeypatch.setattr(database, "pooled_connection", lambda *_a, **_k: fake_connection(cursor))

    backup_id = database.create_discord_backup_snapshot(
        guild_id=10,
        original_name="Retry",
        roles=[],
        channels=[],
        overwrites=[],
        idempotency_key="web-request-1",
    )

    assert backup_id == 777
    assert not any(query.startswith("INSERT INTO backup_discord") for query, _ in cursor.executed)


def test_single_slot_replaces_previous_snapshots_only_after_new_snapshot(monkeypatch):
    cursor = SnapshotCursor(limit=9, retained_ids=[501, 400, 300])
    monkeypatch.setattr(database, "pooled_connection", lambda *_a, **_k: fake_connection(cursor))

    assert database.create_discord_backup_snapshot(
        guild_id=10,
        original_name="New",
        roles=[],
        channels=[],
        overwrites=[],
        backup_type="normal",
    ) == 501

    writes = [query for query, _ in cursor.executed]
    insert_index = next(i for i, query in enumerate(writes) if query.startswith("INSERT INTO backup_discord"))
    delete_indices = [i for i, query in enumerate(writes) if query.startswith("DELETE FROM backup_discord")]
    assert delete_indices and all(insert_index < index for index in delete_indices)
    deleted = [
        params
        for query, params in cursor.executed
        if query.startswith("DELETE FROM backup_discord")
    ]
    assert deleted == [(400, 10), (300, 10)]


def test_expired_backup_purge_excludes_guilds_observed_live(monkeypatch):
    class PurgeCursor(FakeCursor):
        def execute(self, query, params=None):
            normalized = " ".join(query.split())
            self.executed.append((normalized, params))
            if normalized.startswith("SELECT guild_id FROM backup_server_settings"):
                active = {int(item) for item in (params or ())}
                self.current = [
                    {"guild_id": guild_id}
                    for guild_id in (10, 20)
                    if guild_id not in active
                ]

    cursor = PurgeCursor()
    purged = []
    monkeypatch.setattr(
        database,
        "pooled_connection",
        lambda *_a, **_k: fake_connection(cursor),
    )
    monkeypatch.setattr(
        database,
        "purge_discord_backup_guild_data",
        lambda guild_id: purged.append(guild_id),
    )

    result = database.purge_expired_discord_backup_guilds(
        active_guild_ids={10},
    )

    assert result == [20]
    assert purged == [20]
    query, params = next(
        item
        for item in cursor.executed
        if item[0].startswith("SELECT guild_id FROM backup_server_settings")
    )
    assert "guild_id NOT IN (%s)" in query
    assert params == (10,)


class SnapshotCompletionCursor(FakeCursor):
    def __init__(self):
        super().__init__()
        self.rowcount = 1


def test_snapshot_completion_binds_exact_sql_parameters(monkeypatch):
    cursor = SnapshotCompletionCursor()
    monkeypatch.setattr(
        database,
        "pooled_connection",
        lambda *_a, **_k: fake_connection(cursor),
    )

    assert database.complete_backup_snapshot_operation(
        70,
        10,
        status="SUCCEEDED",
        backup_id=501,
        result={"backupId": 501},
    ) is True

    query, params = cursor.executed[-1]
    assert query.startswith("UPDATE backup_snapshot_operations")
    assert params == (
        "SUCCEEDED",
        501,
        json.dumps({"backupId": 501}, ensure_ascii=False),
        None,
        70,
        10,
    )


class SnapshotRecoveryCursor(FakeCursor):
    def __init__(self):
        super().__init__()
        self.rowcount = 0

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SELECT o.id, o.guild_id"):
            self.current = [{
                "id": 70,
                "guild_id": 10,
                "progress_current": 6,
                "progress_total": 6,
                "detail_json": json.dumps({"backupId": 501}),
            }]
        elif normalized.startswith("SELECT 1 FROM backup_discord"):
            self.current = {"present": 1}
        elif normalized.startswith("UPDATE backup_snapshot_operations SET status = 'SUCCEEDED'"):
            self.rowcount = 1
            self.current = None
        elif normalized.startswith("INSERT INTO backup_operation_steps"):
            self.rowcount = 1
            self.current = None
        else:
            self.current = None

    def fetchall(self):
        return self.current if isinstance(self.current, list) else []

    def fetchone(self):
        return self.current if isinstance(self.current, dict) else None


def test_recovery_marks_only_durably_completed_snapshot_as_succeeded(monkeypatch):
    cursor = SnapshotRecoveryCursor()
    monkeypatch.setattr(
        database,
        "pooled_connection",
        lambda *_a, **_k: fake_connection(cursor),
    )

    assert database.recover_completed_backup_snapshot_operations() == 1

    update = next(
        (query, params)
        for query, params in cursor.executed
        if query.startswith("UPDATE backup_snapshot_operations SET status = 'SUCCEEDED'")
    )
    assert update[1] == (501, 6, 6, 70, 10)
    assert any(
        query.startswith("INSERT INTO backup_operation_steps")
        and params[0:2] == (10, 70)
        for query, params in cursor.executed
    )


class OperationInsertRaceCursor(FakeCursor):
    def __init__(self, existing_id):
        super().__init__()
        self.existing_id = existing_id
        self.rowcount = 0

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.executed.append((normalized, params))
        if normalized.startswith("INSERT IGNORE INTO backup_snapshot_operations"):
            self.rowcount = 0
            self.current = None
        elif normalized.startswith("INSERT IGNORE INTO backup_restore_operations"):
            self.rowcount = 0
            self.current = None
        elif normalized.startswith("SELECT id FROM backup_snapshot_operations"):
            self.current = {"id": self.existing_id}
        elif normalized.startswith("SELECT id FROM backup_restore_operations"):
            self.current = {"id": self.existing_id}
        else:
            self.current = None


def test_snapshot_operation_insert_race_reuses_winning_row(monkeypatch):
    cursor = OperationInsertRaceCursor(existing_id=777)
    monkeypatch.setattr(
        database,
        "pooled_connection",
        lambda *_a, **_k: fake_connection(cursor),
    )

    operation_id = database.create_backup_snapshot_operation(
        10,
        "periodic:10:weekly:2026-W41",
        "periodic",
        "Automático",
    )

    assert operation_id == 777
    assert any(
        query.startswith("INSERT IGNORE INTO backup_snapshot_operations")
        for query, _params in cursor.executed
    )


def test_restore_operation_insert_race_reuses_winning_row(monkeypatch):
    cursor = OperationInsertRaceCursor(existing_id=888)
    monkeypatch.setattr(
        database,
        "pooled_connection",
        lambda *_a, **_k: fake_connection(cursor),
    )

    operation_id = database.create_backup_restore_operation(
        10,
        123,
        "restore-request-1",
        "full",
        42,
        decision={"duplicateStrategy": "explicit"},
    )

    assert operation_id == 888
    assert any(
        query.startswith("INSERT IGNORE INTO backup_restore_operations")
        for query, _params in cursor.executed
    )


def test_periodic_idempotency_key_is_stable_inside_frequency_window():
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    assert periodic_idempotency_key(42, "daily", now) == "periodic:42:daily:2026-10-07"
    assert periodic_idempotency_key(42, "monthly", now) == "periodic:42:monthly:2026-10"


class FakeRole:
    def __init__(
        self,
        role_id,
        name,
        position,
        *,
        managed=False,
        color=0,
        permissions=0,
        hoist=False,
        mentionable=False,
    ):
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = managed
        self.color = SimpleNamespace(value=color)
        self.permissions = SimpleNamespace(value=permissions)
        self.hoist = hoist
        self.mentionable = mentionable
        self.edits = []

    def __gt__(self, other):
        return self.position > other.position

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "name" in kwargs:
            self.name = kwargs["name"]
        return self


class FakeGuild:
    def __init__(self, *, guild_id=10, blocking=False, duplicate=False):
        self.id = guild_id
        self.default_role = FakeRole(1, "@everyone", 0)
        self.bot_role = FakeRole(2, "Coddy", 20, managed=True)
        self.roles = [self.default_role, self.bot_role]
        if blocking:
            self.roles.append(FakeRole(3, "Above", 30))
        if duplicate:
            self.roles.extend([
                FakeRole(20, "Moderador", 10),
                FakeRole(21, "Moderador", 9),
            ])
        self.channels = []
        self.categories = []
        self.position_updates = []
        self.created_roles = []
        self._next_role_id = 1000
        self.me = SimpleNamespace(
            top_role=self.bot_role,
            guild_permissions=SimpleNamespace(manage_roles=True, manage_channels=True),
        )

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)

    def get_channel(self, _channel_id):
        return None

    def get_member(self, _user_id):
        return None

    async def create_role(self, *, name, **_kwargs):
        role = FakeRole(self._next_role_id, name, 1)
        self._next_role_id += 1
        self.created_roles.append(role)
        # Mirrors discord.py 2.3.2: create_role() returns the new Role but does
        # not add it to Guild._roles synchronously. guild.get_role() therefore
        # cannot be relied on until a later gateway event updates the cache.
        return role

    async def edit_role_positions(self, positions):
        self.position_updates.append(dict(positions))
        for role, position in positions.items():
            role.position = position
        return positions


def install_runtime_database(monkeypatch, **attributes):
    module = ModuleType("core.database")
    module.record_backup_operation_step = lambda *_a, **_k: 1
    for name, value in attributes.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "core.database", module)
    return module


def test_snapshot_excludes_everyone_and_managed_roles_but_keeps_their_overwrites():
    guild = FakeGuild()
    normal = FakeRole(10, "Membro", 5)
    managed = FakeRole(11, "Integration", 6, managed=True)
    guild.roles.extend([normal, managed])

    class FakeOverwrite:
        def __init__(self, allow, deny):
            self.allow = allow
            self.deny = deny

        def pair(self):
            return (
                SimpleNamespace(value=self.allow),
                SimpleNamespace(value=self.deny),
            )

    channel = SimpleNamespace(
        id=100,
        overwrites={
            guild.default_role: FakeOverwrite(0, 1024),
            managed: FakeOverwrite(1024, 0),
            normal: FakeOverwrite(2048, 0),
        },
    )
    guild.channels = [channel]

    with patch("core.backup_runtime.discord.Role", FakeRole):
        roles = BackupRestoreEngine._capture_roles(guild)
        overwrites = BackupRestoreEngine._capture_overwrites(guild)

    assert [role["discord_id"] for role in roles] == [10]
    by_target = {item["target_type"]: item for item in overwrites}
    assert by_target["EVERYONE"]["target_discord_id"] == guild.default_role.id
    assert by_target["MANAGED_ROLE"]["target_discord_id"] == managed.id
    assert by_target["ROLE"]["target_discord_id"] == normal.id


def test_legacy_everyone_backup_role_reuses_default_role_instead_of_creating_clone():
    guild = FakeGuild(guild_id=10)
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))

    role, action = asyncio.run(engine._resolve_role(
        guild,
        123,
        {
            "id": 5,
            "discord_id": 999999,
            "name": "@everyone",
            "color": 0,
            "permissions": 0,
            "position": 0,
            "hoist": 0,
            "mentionable": 0,
        },
        restore_roles=True,
        duplicate_strategy="explicit",
        explicit_resolution=None,
    ))

    assert role is guild.default_role
    assert action == "reused"
    assert guild.created_roles == []


def test_permission_restore_resolves_everyone_and_existing_managed_role_without_cloning():
    guild = FakeGuild()
    managed = FakeRole(55, "Integration", 7, managed=True)
    guild.roles.append(managed)

    class FakeChannel:
        def __init__(self):
            self.id = 77
            self.name = "privado"
            self.calls = []

        async def set_permissions(self, role, **kwargs):
            self.calls.append((role, kwargs))

    channel = FakeChannel()
    guild.get_channel = lambda channel_id: channel if int(channel_id) in {77, 700} else None

    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    payload = {
        "roles": [],
        "channels": [{
            "id": 700,
            "discord_id": 77,
            "parent_id": None,
            "name": "privado",
            "type": 0,
            "position": 0,
            "topic": None,
            "nsfw": 0,
        }],
        "overwrites": [
            {
                "id": 1,
                "channel_id": 700,
                "role_name": "@everyone",
                "target_type": "EVERYONE",
                "target_discord_id": guild.default_role.id,
                "role_id": None,
                "allow_bits": 0,
                "deny_bits": 1024,
            },
            {
                "id": 2,
                "channel_id": 700,
                "role_name": "Integration",
                "target_type": "MANAGED_ROLE",
                "target_discord_id": managed.id,
                "role_id": None,
                "allow_bits": 1024,
                "deny_bits": 0,
            },
        ],
    }

    result = asyncio.run(engine.execute_restore(
        guild,
        123,
        payload,
        scope="permissions",
        decision={},
    ))

    assert result["status"] == "SUCCEEDED"
    assert result["permissions"]["updated"] == 2
    assert [call[0] for call in channel.calls] == [guild.default_role, managed]
    assert guild.created_roles == []


def test_missing_managed_overwrite_target_is_skipped_without_creating_role():
    guild = FakeGuild()
    channel = SimpleNamespace(id=77, name="privado")
    guild.get_channel = lambda channel_id: channel if int(channel_id) in {77, 700} else None

    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    payload = {
        "roles": [],
        "channels": [{
            "id": 700,
            "discord_id": 77,
            "parent_id": None,
            "name": "privado",
            "type": 0,
            "position": 0,
            "topic": None,
            "nsfw": 0,
        }],
        "overwrites": [{
            "id": 1,
            "channel_id": 700,
            "role_name": "Missing Integration",
            "target_type": "MANAGED_ROLE",
            "target_discord_id": 555,
            "role_id": None,
            "allow_bits": 1024,
            "deny_bits": 0,
        }],
    }

    result = asyncio.run(engine.execute_restore(
        guild,
        123,
        payload,
        scope="permissions",
        decision={},
    ))

    assert result["status"] == "PARTIAL"
    assert result["permissions"]["skipped"] == 1
    assert guild.created_roles == []


def test_restore_preflight_is_guild_scoped(monkeypatch):
    install_runtime_database(
        monkeypatch,
        get_discord_backup_payload=lambda backup_id, guild_id: None,
    )
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))

    with pytest.raises(LookupError, match="backup_not_found_for_guild"):
        engine.build_restore_preflight(FakeGuild(guild_id=10), 123, "full")


def test_restore_preflight_clamps_hierarchy_and_still_requires_duplicate_resolution(monkeypatch):
    payload = {
        "backup": {
            "id": 123,
            "original_name": "Before",
            "backup_type": "normal",
            "created_at": datetime(2026, 10, 7),
        },
        "roles": [{
            "id": 5,
            "discord_id": 999,
            "name": "Moderador",
            "color": 0,
            "permissions": 0,
            "position": 5,
            "hoist": 0,
            "mentionable": 0,
        }],
        "channels": [],
        "overwrites": [],
    }
    install_runtime_database(
        monkeypatch,
        get_discord_backup_payload=lambda backup_id, guild_id: payload,
    )
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))

    preflight = engine.build_restore_preflight(
        FakeGuild(blocking=True, duplicate=True),
        123,
        "full",
    )

    blocker_codes = {item["code"] for item in preflight["blockers"]}
    warning_codes = {item["code"] for item in preflight["warnings"]}
    assert "BOT_ROLE_HIERARCHY" not in blocker_codes
    assert "ROLE_RESOLUTION_REQUIRED" in blocker_codes
    assert "ROLE_POSITIONS_CLAMPED_BELOW_BOT" in warning_codes
    assert preflight["ready"] is False
    assert preflight["summary"]["roleOverwritesOnly"] is True


def test_roles_above_coddy_are_restored_with_specs_but_clamped_below_bot():
    guild = FakeGuild()
    role = FakeRole(20, "Staff antiga", 10)
    guild.roles.append(role)
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    payload = {
        "roles": [{
            "id": 5,
            "discord_id": 20,
            "name": "Staff",
            "color": 0x336699,
            "permissions": 8,
            "position": 30,
            "hoist": 1,
            "mentionable": 1,
        }],
        "channels": [],
        "overwrites": [],
    }

    result = asyncio.run(engine.execute_restore(
        guild,
        123,
        payload,
        scope="roles",
        decision={},
    ))

    assert result["status"] == "SUCCEEDED"
    assert role.name == "Staff"
    assert role.edits
    assert role.edits[-1]["hoist"] is True
    assert role.edits[-1]["mentionable"] is True
    assert role.edits[-1]["permissions"].value == 8
    assert guild.position_updates[-1][role] == guild.bot_role.position - 1
    assert role.position < guild.bot_role.position


def test_newly_created_roles_keep_snapshot_relative_order_before_gateway_cache_updates():
    guild = FakeGuild()
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    payload = {
        # Database payloads are read bottom-to-top (position ASC).
        "roles": [
            {
                "id": 11,
                "discord_id": 111,
                "name": "Bottom",
                "color": 0,
                "permissions": 0,
                "position": 5,
                "hoist": 0,
                "mentionable": 0,
            },
            {
                "id": 12,
                "discord_id": 112,
                "name": "Middle",
                "color": 0,
                "permissions": 0,
                "position": 10,
                "hoist": 0,
                "mentionable": 0,
            },
            {
                "id": 13,
                "discord_id": 113,
                "name": "Top",
                "color": 0,
                "permissions": 0,
                "position": 15,
                "hoist": 0,
                "mentionable": 0,
            },
        ],
        "channels": [],
        "overwrites": [],
    }

    with patch("core.backup_runtime.asyncio.sleep", new=AsyncMock()) as sleep:
        result = asyncio.run(engine.execute_restore(
            guild,
            123,
            payload,
            scope="roles",
            decision={},
        ))

    assert result["status"] == "SUCCEEDED"
    assert [role.name for role in guild.created_roles] == [
        "Top",
        "Middle",
        "Bottom",
    ]
    # create_role already receives all restorable role specs; a second edit
    # would duplicate one Discord API mutation per newly-created role.
    assert all(role.edits == [] for role in guild.created_roles)
    assert sleep.await_count == 3
    assert all(call.args == (0.5,) for call in sleep.await_args_list)

    # Newly-created roles intentionally remain absent from the guild cache.
    assert all(guild.get_role(role.id) is None for role in guild.created_roles)

    positions = guild.position_updates[-1]
    by_name = {role.name: position for role, position in positions.items()}
    assert set(by_name) == {"Bottom", "Middle", "Top"}
    assert by_name["Top"] > by_name["Middle"] > by_name["Bottom"]
    assert by_name["Top"] < guild.bot_role.position


def test_role_restore_reports_current_role_before_discord_mutation(monkeypatch):
    guild = FakeGuild()
    existing = FakeRole(20, "Antes", 10)
    guild.roles.append(existing)
    recorded = []
    install_runtime_database(
        monkeypatch,
        record_backup_operation_step=lambda *args, **kwargs: recorded.append(
            (args, kwargs)
        ) or len(recorded),
    )
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    payload = {
        "roles": [{
            "id": 5,
            "discord_id": 20,
            "name": "Depois",
            "color": 0,
            "permissions": 0,
            "position": 10,
            "hoist": 0,
            "mentionable": 0,
        }],
        "channels": [],
        "overwrites": [],
    }

    with patch("core.backup_runtime.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(engine.execute_restore(
            guild,
            123,
            payload,
            scope="roles",
            decision={},
            operation_id=77,
            progress_start=3,
            progress_total=6,
        ))

    assert result["status"] == "SUCCEEDED"
    started = next(
        args for args, _kwargs in recorded
        if args[3] == "ROLE_RESTORE_STARTED"
    )
    restored = next(
        args for args, _kwargs in recorded
        if args[3] == "ROLE_RESTORED"
    )
    assert started[4] == "RUNNING"
    assert started[6] == 3
    assert "Depois" in started[5]
    assert restored[6] == 4


def test_role_restore_timeout_aborts_without_mutating_next_role():
    guild = FakeGuild()
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    restored = FakeRole(1001, "Second", 1)
    engine._resolve_role = AsyncMock(side_effect=[
        asyncio.TimeoutError(),
        (restored, "created"),
    ])
    payload = {
        "roles": [
            {
                "id": 1,
                "discord_id": 101,
                "name": "First",
                "color": 0,
                "permissions": 0,
                "position": 20,
                "hoist": 0,
                "mentionable": 0,
            },
            {
                "id": 2,
                "discord_id": 102,
                "name": "Second",
                "color": 0,
                "permissions": 0,
                "position": 10,
                "hoist": 0,
                "mentionable": 0,
            },
        ],
        "channels": [],
        "overwrites": [],
    }

    with patch("core.backup_runtime.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(engine.execute_restore(
            guild,
            123,
            payload,
            scope="roles",
            decision={},
        ))

    assert result["status"] == "PARTIAL"
    assert result["roles"]["failed"] == 1
    assert result["roles"]["created"] == 0
    assert "role_timeout:First" in result["warnings"]
    assert "restore_aborted_after_discord_timeout" in result["warnings"]
    assert engine._resolve_role.await_count == 1


def test_role_create_bucket_telemetry_is_read_only():
    guild = FakeGuild()
    route = discord.http.Route(
        "POST", "/guilds/{guild_id}/roles", guild_id=int(guild.id)
    )

    async def inspect():
        current = asyncio.get_running_loop().time()
        bucket = SimpleNamespace(
            limit=2, remaining=0, outgoing=0,
            _pending_requests=[], reset_after=3600.0, expires=current + 3600.0,
        )
        http = SimpleNamespace(
            _bucket_hashes={route.key: "test-bucket"},
            _buckets={f"test-bucket:{route.major_parameters}": bucket},
        )
        engine = BackupRestoreEngine(SimpleNamespace(http=http))
        before = (bucket.remaining, bucket.expires)
        state = engine._role_create_bucket_state(guild)
        assert (bucket.remaining, bucket.expires) == before
        return state

    state = asyncio.run(inspect())
    assert state["known"] is True
    assert state["remaining"] == 0
    assert state["pending"] == 0
    assert state["secondsUntilReset"] > 3500


def test_role_creation_cooldown_aborts_and_reconciles_uncertain_create(monkeypatch):
    guild = FakeGuild()
    possibly_created = FakeRole(1500, "First", 1)
    guild.fetch_roles = AsyncMock(return_value=[*guild.roles, possibly_created])
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    engine._resolve_role = AsyncMock(side_effect=[
        discord.RateLimited(86400.0),
        (FakeRole(1600, "Second", 1), "created"),
    ])
    steps = []
    install_runtime_database(
        monkeypatch,
        record_backup_operation_step=lambda *args, **kwargs: steps.append((args, kwargs)),
    )
    payload = {
        "roles": [
            {"id": 1, "discord_id": 101, "name": "First", "color": 0,
             "permissions": 0, "position": 20, "hoist": 0, "mentionable": 0},
            {"id": 2, "discord_id": 102, "name": "Second", "color": 0,
             "permissions": 0, "position": 10, "hoist": 0, "mentionable": 0},
        ],
        "channels": [],
        "overwrites": [],
    }

    result = asyncio.run(engine.execute_restore(
        guild, 123, payload, scope="roles", decision={},
        operation_id=55, progress_start=3, progress_total=7,
    ))

    assert result["status"] == "PARTIAL"
    assert engine._resolve_role.await_count == 1
    assert guild.fetch_roles.await_count == 1
    assert "discord_role_creation_cooldown" in result["warnings"]
    step = next((args, kwargs) for args, kwargs in steps
                if args[3] == "ROLE_RATE_LIMITED")
    assert step[1]["detail"]["retryAfterSeconds"] == 86400.0
    assert step[1]["detail"]["possibleCreatedRole"] == {
        "state": "possible", "roleId": "1500"
    }


def test_reconciliation_rejects_ambiguous_new_roles():
    guild = FakeGuild()
    guild.fetch_roles = AsyncMock(return_value=[
        *guild.roles, FakeRole(1500, "First", 1), FakeRole(1501, "First", 1),
    ])
    role_data = {"name": "First", "permissions": 0, "color": 0,
                 "hoist": 0, "mentionable": 0}
    result = asyncio.run(BackupRestoreEngine._find_possible_created_role(
        guild, role_data, {int(role.id) for role in guild.roles}
    ))
    assert result == {"state": "ambiguous"}


def test_role_edit_timeout_marks_partial():
    guild = FakeGuild()
    role = FakeRole(20, "Antes", 10)
    role.edit = AsyncMock(side_effect=asyncio.TimeoutError())
    guild.roles.append(role)
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    payload = {
        "roles": [{
            "id": 5,
            "discord_id": 20,
            "name": "Depois",
            "color": 0,
            "permissions": 0,
            "position": 10,
            "hoist": 0,
            "mentionable": 0,
        }],
        "channels": [],
        "overwrites": [],
    }

    result = asyncio.run(engine.execute_restore(
        guild,
        123,
        payload,
        scope="roles",
        decision={},
    ))

    assert result["status"] == "PARTIAL"
    assert result["roles"]["failed"] == 1
    assert "role_edit_timeout:Depois" in result["warnings"]
    assert "restore_aborted_after_discord_timeout" in result["warnings"]


def test_role_position_timeout_is_single_partial_progress_step(monkeypatch):
    guild = FakeGuild()
    role = FakeRole(20, "Staff", 10)
    guild.roles.append(role)
    role.edit = AsyncMock(return_value=role)
    guild.edit_role_positions = AsyncMock(side_effect=asyncio.TimeoutError())
    recorded = []
    install_runtime_database(
        monkeypatch,
        record_backup_operation_step=lambda *args, **kwargs: recorded.append(
            (args, kwargs)
        ) or len(recorded),
    )
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    payload = {
        "roles": [{
            "id": 5,
            "discord_id": 20,
            "name": "Staff",
            "color": 0,
            "permissions": 0,
            "position": 10,
            "hoist": 0,
            "mentionable": 0,
        }],
        "channels": [],
        "overwrites": [],
    }

    with patch("core.backup_runtime.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(engine.execute_restore(
            guild,
            123,
            payload,
            scope="roles",
            decision={},
            operation_id=77,
            progress_start=3,
            progress_total=6,
        ))

    assert result["status"] == "PARTIAL"
    assert "role_position_update_timeout" in result["warnings"]
    timeout_steps = [
        args for args, _kwargs in recorded
        if args[3] == "ROLE_POSITIONS_TIMEOUT"
    ]
    assert len(timeout_steps) == 1
    assert not any(
        args[3] == "ROLE_POSITIONS_APPLIED"
        for args, _kwargs in recorded
    )


def test_announcement_channel_is_downgraded_to_text_without_community_feature():
    guild = FakeGuild()
    guild.features = []
    created = SimpleNamespace(id=700, name="hall-de-anuncios", category_id=None)
    guild.create_text_channel = AsyncMock(return_value=created)
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    payload = {
        "roles": [],
        "channels": [
            {
                "id": 41,
                "discord_id": 401,
                "parent_id": None,
                "name": "hall-de-anuncios",
                "type": 5,
                "position": 0,
                "topic": None,
                "nsfw": 0,
            }
        ],
        "overwrites": [],
    }

    result = asyncio.run(engine.execute_restore(
        guild,
        123,
        payload,
        scope="channels",
        decision={},
    ))

    assert result["status"] == "PARTIAL"
    assert result["channels"]["created"] == 1
    assert "channel_type_downgraded:hall-de-anuncios:5->0" in result["warnings"]
    guild.create_text_channel.assert_awaited_once()
    assert "news" not in guild.create_text_channel.await_args.kwargs


def test_restore_preflight_warns_when_announcement_channels_will_be_downgraded(monkeypatch):
    payload = {
        "backup": {
            "id": 123,
            "original_name": "Before",
            "backup_type": "normal",
            "created_at": datetime(2026, 10, 8),
        },
        "roles": [],
        "channels": [
            {
                "id": 41,
                "discord_id": 401,
                "parent_id": None,
                "name": "hall-de-anuncios",
                "type": 5,
                "position": 0,
                "topic": None,
                "nsfw": 0,
            }
        ],
        "overwrites": [],
    }
    install_runtime_database(
        monkeypatch,
        get_discord_backup_payload=lambda *_a, **_k: payload,
    )
    guild = FakeGuild()
    guild.features = []
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))

    preflight = engine.build_restore_preflight(guild, 123, "channels")

    warning = next(
        item
        for item in preflight["warnings"]
        if item["code"] == "ANNOUNCEMENT_CHANNELS_DOWNGRADED"
    )
    assert warning["count"] == 1
    assert warning["channels"][0]["name"] == "hall-de-anuncios"


def test_channel_http_failure_is_partial_and_next_channel_still_restores():
    guild = FakeGuild()
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    response = SimpleNamespace(status=400, reason="Bad Request")
    discord_error = discord.HTTPException(
        response,
        {"code": 50035, "message": "Invalid Form Body"},
    )
    restored = SimpleNamespace(id=502, name="segundo", category_id=None)
    engine._create_channel_by_type = AsyncMock(
        side_effect=[discord_error, restored]
    )
    payload = {
        "roles": [],
        "channels": [
            {
                "id": 31,
                "discord_id": 301,
                "parent_id": None,
                "name": "primeiro",
                "type": 0,
                "position": 0,
                "topic": None,
                "nsfw": 0,
            },
            {
                "id": 32,
                "discord_id": 302,
                "parent_id": None,
                "name": "segundo",
                "type": 0,
                "position": 1,
                "topic": None,
                "nsfw": 0,
            },
        ],
        "overwrites": [],
    }

    result = asyncio.run(engine.execute_restore(
        guild,
        123,
        payload,
        scope="channels",
        decision={},
    ))

    assert result["status"] == "PARTIAL"
    assert result["channels"]["failed"] == 1
    assert result["channels"]["created"] == 1
    assert "channel_failed:primeiro:50035" in result["warnings"]
    assert engine._create_channel_by_type.await_count == 2


def test_restore_error_detail_exposes_sanitized_discord_http_metadata():
    response = SimpleNamespace(status=400, reason="Bad Request")
    error = discord.HTTPException(
        response,
        {"code": 50035, "message": "Invalid Form Body"},
    )

    detail = BackupRestoreEngine._restore_error_detail(error)

    assert detail["exceptionClass"] == "HTTPException"
    assert detail["status"] == 400
    assert detail["discordCode"] == 50035
    assert "Invalid Form Body" in detail["message"]


def test_channels_only_restore_ignores_role_ambiguity_and_hierarchy(monkeypatch):
    payload = {
        "backup": {
            "id": 123,
            "original_name": "Before",
            "backup_type": "normal",
            "created_at": datetime(2026, 10, 7),
        },
        "roles": [{
            "id": 5,
            "discord_id": 999,
            "name": "Moderador",
            "color": 0,
            "permissions": 0,
            "position": 5,
            "hoist": 0,
            "mentionable": 0,
        }],
        "channels": [],
        "overwrites": [],
    }
    install_runtime_database(
        monkeypatch,
        get_discord_backup_payload=lambda backup_id, guild_id: payload,
    )
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    guild = FakeGuild(blocking=True, duplicate=True)

    preflight = engine.build_restore_preflight(guild, 123, "channels")

    assert preflight["duplicateRoles"] == []
    assert "ROLE_RESOLUTION_REQUIRED" not in {
        item["code"] for item in preflight["blockers"]
    }
    assert "BOT_ROLE_HIERARCHY" not in {
        item["code"] for item in preflight["blockers"]
    }
    assert preflight["ready"] is True

    engine._ambiguous_backup_roles = Mock(
        side_effect=AssertionError("channels-only restore must not inspect role ambiguity")
    )
    engine._merge_duplicate_roles = AsyncMock()

    result = asyncio.run(engine.execute_restore(
        guild,
        123,
        payload,
        scope="channels",
        decision={"duplicateStrategy": "merge", "roleResolutions": {}},
    ))

    assert result["status"] == "SUCCEEDED"
    engine._ambiguous_backup_roles.assert_not_called()
    engine._merge_duplicate_roles.assert_not_awaited()


def test_permissions_only_restore_rejects_destructive_duplicate_merge():
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    guild = FakeGuild(duplicate=True)
    engine._ambiguous_backup_roles = Mock(return_value=[{
        "backupRoleId": 5,
        "backupDiscordRoleId": "999",
        "name": "Moderador",
        "candidates": [
            {"id": "20", "name": "Moderador", "position": 10},
            {"id": "21", "name": "Moderador", "position": 9},
        ],
    }])
    payload = {
        "roles": [{
            "id": 5,
            "discord_id": 999,
            "name": "Moderador",
            "color": 0,
            "permissions": 0,
            "position": 5,
            "hoist": 0,
            "mentionable": 0,
        }],
        "channels": [],
        "overwrites": [],
    }

    with pytest.raises(
        BackupDecisionRequired,
        match="duplicate_merge_requires_roles_scope",
    ):
        asyncio.run(engine.execute_restore(
            guild,
            123,
            payload,
            scope="permissions",
            decision={"duplicateStrategy": "merge", "roleResolutions": {}},
        ))


def test_duplicate_merge_failure_marks_role_restore_partial():
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    guild = FakeGuild()
    resolved_role = FakeRole(20, "Moderador", 10, managed=True)
    guild.roles.append(resolved_role)
    engine._ambiguous_backup_roles = Mock(return_value=[{
        "backupRoleId": 5,
        "backupDiscordRoleId": "999",
        "name": "Moderador",
        "candidates": [
            {"id": "20", "name": "Moderador", "position": 10},
            {"id": "21", "name": "Moderador", "position": 9},
        ],
    }])
    engine._merge_duplicate_roles = AsyncMock(
        return_value=["merge_delete_failed:Moderador:21"]
    )
    engine._resolve_role = AsyncMock(return_value=(resolved_role, "reused"))
    payload = {
        "roles": [{
            "id": 5,
            "discord_id": 999,
            "name": "Moderador",
            "color": 0,
            "permissions": 0,
            "position": 5,
            "hoist": 0,
            "mentionable": 0,
        }],
        "channels": [],
        "overwrites": [],
    }

    result = asyncio.run(engine.execute_restore(
        guild,
        123,
        payload,
        scope="roles",
        decision={"duplicateStrategy": "merge", "roleResolutions": {}},
    ))

    assert result["status"] == "PARTIAL"
    assert result["roles"]["failed"] == 1
    assert "merge_delete_failed:Moderador:21" in result["warnings"]


def test_restore_result_is_partial_when_requested_items_are_unresolved(monkeypatch):
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    guild = FakeGuild()
    engine._resolve_role = AsyncMock(return_value=(None, "skipped"))
    payload = {
        "roles": [{
            "id": 5,
            "discord_id": 999,
            "name": "Missing",
            "color": 0,
            "permissions": 0,
            "position": 5,
            "hoist": 0,
            "mentionable": 0,
        }],
        "channels": [],
        "overwrites": [],
    }

    result = asyncio.run(engine.execute_restore(
        guild,
        123,
        payload,
        scope="roles",
        decision={"duplicateStrategy": "explicit", "roleResolutions": {}},
    ))

    assert result["status"] == "PARTIAL"
    assert result["roles"]["skipped"] == 1


def test_duplicate_snapshot_dispatch_loses_atomic_claim_without_touching_lock(monkeypatch):
    acquire = Mock(return_value=True)
    reads = iter([
        {
            "id": 77,
            "guild_id": 10,
            "backup_type": "normal",
            "requested_name": "Before",
            "status": "PENDING",
            "backup_id": None,
        },
        {
            "id": 77,
            "guild_id": 10,
            "backup_type": "normal",
            "requested_name": "Before",
            "status": "RUNNING",
            "backup_id": None,
        },
    ])
    install_runtime_database(
        monkeypatch,
        acquire_backup_guild_lock=acquire,
        complete_backup_snapshot_operation=lambda *_a, **_k: True,
        create_discord_backup_snapshot=lambda **_k: 123,
        get_backup_snapshot_operation=lambda *_a, **_k: next(reads),
        mark_backup_snapshot_operation_running=lambda *_a, **_k: False,
        release_backup_guild_lock=lambda *_a, **_k: True,
    )
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))

    result = asyncio.run(engine.run_snapshot_operation(
        FakeGuild(),
        operation_id=77,
    ))

    assert result["status"] == "RUNNING"
    acquire.assert_not_called()


def test_duplicate_restore_dispatch_loses_atomic_claim_without_touching_lock(monkeypatch):
    acquire = Mock(return_value=True)
    install_runtime_database(
        monkeypatch,
        acquire_backup_guild_lock=acquire,
        complete_backup_restore_operation=lambda *_a, **_k: True,
        fail_pending_backup_restore_operation=lambda *_a, **_k: True,
        get_backup_restore_operation=lambda *_a, **_k: {
            "id": 88,
            "guild_id": 10,
            "backup_id": 123,
            "scope": "roles",
            "status": "PENDING",
            "decision_json": "{}",
        },
        get_discord_backup_payload=lambda *_a, **_k: {
            "roles": [],
            "channels": [],
            "overwrites": [],
        },
        mark_backup_restore_operation_running=lambda *_a, **_k: False,
        release_backup_guild_lock=lambda *_a, **_k: True,
        refresh_backup_guild_lock=lambda *_a, **_k: True,
    )
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    engine.execute_restore = AsyncMock()

    asyncio.run(engine.run_restore_operation(
        FakeGuild(),
        operation_id=88,
        backup_id=123,
        scope="roles",
        decision={},
    ))

    acquire.assert_not_called()
    engine.execute_restore.assert_not_awaited()


def test_restore_operation_uses_persisted_decision_not_http_payload(monkeypatch):
    completed = []
    module = install_runtime_database(
        monkeypatch,
        get_backup_restore_operation=lambda operation_id, guild_id: {
            "id": operation_id,
            "guild_id": guild_id,
            "backup_id": 123,
            "scope": "roles",
            "status": "PENDING",
            "decision_json": json.dumps({
                "duplicateStrategy": "merge",
                "roleResolutions": {},
            }),
        },
        acquire_backup_guild_lock=lambda *_a, **_k: True,
        fail_pending_backup_restore_operation=lambda *_a, **_k: True,
        mark_backup_restore_operation_running=lambda *_a, **_k: True,
        get_discord_backup_payload=lambda *_a, **_k: {"roles": [], "channels": [], "overwrites": []},
        complete_backup_restore_operation=lambda *args, **kwargs: completed.append((args, kwargs)) or True,
        release_backup_guild_lock=lambda *_a, **_k: True,
        refresh_backup_guild_lock=lambda *_a, **_k: True,
    )
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    engine.build_restore_preflight = lambda *_a, **_k: {"blockers": []}
    engine.execute_restore = AsyncMock(return_value={
        "status": "SUCCEEDED",
        "roles": engine._bucket(),
        "channels": engine._bucket(),
        "permissions": engine._bucket(),
        "warnings": [],
    })

    asyncio.run(engine.run_restore_operation(
        FakeGuild(),
        operation_id=88,
        backup_id=123,
        scope="roles",
        decision={"duplicateStrategy": "explicit", "roleResolutions": {"5": "20"}},
    ))

    engine.execute_restore.assert_awaited_once()
    assert engine.execute_restore.await_args.kwargs["decision"]["duplicateStrategy"] == "merge"
    assert completed[-1][1]["status"] == "SUCCEEDED"


@pytest.mark.parametrize(
    "aborted,prior_progress,expected_progress",
    [(True, 6, 6), (False, 187, 188)],
)
def test_partial_restore_terminal_step_preserves_real_progress(
    monkeypatch, aborted, prior_progress, expected_progress,
):
    steps = []
    completed = []
    states = iter(["PENDING", "RUNNING"])
    install_runtime_database(
        monkeypatch,
        get_backup_restore_operation=lambda operation_id, guild_id: {
            "id": operation_id,
            "guild_id": guild_id,
            "backup_id": 123,
            "scope": "roles",
            "status": next(states, "RUNNING"),
            "decision_json": "{}",
            "progress_current": prior_progress,
            "progress_total": 188,
        },
        record_backup_operation_step=lambda *args, **kwargs: (
            steps.append((args, kwargs)) or len(steps)
        ),
        acquire_backup_guild_lock=lambda *_a, **_k: True,
        fail_pending_backup_restore_operation=lambda *_a, **_k: True,
        mark_backup_restore_operation_running=lambda *_a, **_k: True,
        get_discord_backup_payload=lambda *_a, **_k: {
            # 4 fixed steps + 183 roles + role positioning = 188 steps.
            "roles": [{"id": i} for i in range(183)],
            "channels": [],
            "overwrites": [],
        },
        complete_backup_restore_operation=lambda *args, **kwargs: (
            completed.append((args, kwargs)) or True
        ),
        release_backup_guild_lock=lambda *_a, **_k: True,
        refresh_backup_guild_lock=lambda *_a, **_k: True,
    )
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    engine.build_restore_preflight = lambda *_a, **_k: {"blockers": []}
    engine.execute_restore = AsyncMock(return_value={
        "status": "PARTIAL",
        "aborted": aborted,
        "roles": engine._bucket(),
        "channels": engine._bucket(),
        "permissions": engine._bucket(),
        "warnings": ["discord_role_creation_cooldown"],
    })

    asyncio.run(engine.run_restore_operation(
        FakeGuild(),
        operation_id=88,
        backup_id=123,
        scope="roles",
        decision={},
    ))

    terminal = [args for args, _ in steps if args[3] == "COMPLETED"][-1]
    assert terminal[4] == "FAILED"
    assert terminal[6:8] == (expected_progress, 188)
    assert completed[-1][1]["status"] == "PARTIAL"


def test_cancelled_restore_is_finalized_partial_and_releases_lock(monkeypatch):
    completed = []
    released = []
    install_runtime_database(
        monkeypatch,
        get_backup_restore_operation=lambda operation_id, guild_id: {
            "id": operation_id,
            "guild_id": guild_id,
            "backup_id": 123,
            "scope": "roles",
            "status": "PENDING",
            "decision_json": json.dumps({
                "duplicateStrategy": "explicit",
                "roleResolutions": {},
            }),
        },
        acquire_backup_guild_lock=lambda *_a, **_k: True,
        fail_pending_backup_restore_operation=lambda *_a, **_k: True,
        mark_backup_restore_operation_running=lambda *_a, **_k: True,
        get_discord_backup_payload=lambda *_a, **_k: {
            "roles": [],
            "channels": [],
            "overwrites": [],
        },
        complete_backup_restore_operation=lambda *args, **kwargs: (
            completed.append((args, kwargs)) or True
        ),
        release_backup_guild_lock=lambda *args, **_k: released.append(args) or True,
        refresh_backup_guild_lock=lambda *_a, **_k: True,
    )
    engine = BackupRestoreEngine(SimpleNamespace(user=SimpleNamespace(id=999)))
    engine.build_restore_preflight = lambda *_a, **_k: {"blockers": []}
    engine.execute_restore = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(engine.run_restore_operation(
            FakeGuild(),
            operation_id=88,
            backup_id=123,
            scope="roles",
            decision={},
        ))

    assert completed[-1][1]["status"] == "PARTIAL"
    assert (
        completed[-1][1]["error_code"]
        == "restore_interrupted_by_runtime_shutdown"
    )
    assert released == [(10, "restore:88")]
