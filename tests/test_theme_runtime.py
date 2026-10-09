import asyncio
import hashlib
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.theme_runtime as theme_runtime
from core.theme_runtime import ThemeAssetBytes, ThemeRuntime


class FakePermissions:
    def __init__(
        self,
        *,
        administrator=False,
        manage_guild=True,
        manage_channels=True,
        manage_roles=True,
    ):
        self.administrator = administrator
        self.manage_guild = manage_guild
        self.manage_channels = manage_channels
        self.manage_roles = manage_roles


class FakeRole:
    def __init__(
        self,
        role_id,
        name,
        *,
        position=1,
        managed=False,
        default=False,
        fail_edit=False,
    ):
        self.id = int(role_id)
        self.name = name
        self.position = int(position)
        self.managed = managed
        self._default = default
        self.fail_edit = fail_edit

    def is_default(self):
        return self._default

    async def edit(self, *, name, reason=None):
        if self.fail_edit:
            raise RuntimeError("role_edit_failed")
        self.name = name
        return self


class FakeChannel:
    def __init__(self, channel_id, name, *, fail_edit=False, before_edit=None):
        self.id = int(channel_id)
        self.name = name
        self.fail_edit = fail_edit
        self.before_edit = before_edit

    async def edit(self, *, name, reason=None):
        if self.before_edit:
            self.before_edit()
        if self.fail_edit:
            raise RuntimeError("channel_edit_failed")
        self.name = name
        return self


class FakeCategory(FakeChannel):
    pass


class FakeAsset:
    def __init__(self, data):
        self.data = bytes(data)

    async def read(self):
        return self.data


class FakeGuild:
    def __init__(
        self,
        *,
        guild_id=10,
        channels=None,
        roles=None,
        features=None,
        top_role_position=50,
        permissions=None,
        icon=None,
        banner=None,
    ):
        self.id = int(guild_id)
        self._channels = {int(item.id): item for item in (channels or [])}
        self._roles = {int(item.id): item for item in (roles or [])}
        self.features = list(features or [])
        self.me = SimpleNamespace(
            guild_permissions=permissions or FakePermissions(),
            top_role=SimpleNamespace(position=top_role_position),
        )
        self.icon = icon
        self.banner = banner
        self.edit_calls = []

    def get_channel(self, resource_id):
        return self._channels.get(int(resource_id))

    def get_role(self, resource_id):
        return self._roles.get(int(resource_id))

    async def edit(self, *, icon=None, banner=None, reason=None):
        if "icon" in locals() and icon is not None:
            self.icon = FakeAsset(icon)
        elif "icon" in locals() and icon is None and banner is None:
            self.icon = None
        if banner is not None:
            self.banner = FakeAsset(banner)
        self.edit_calls.append({"icon": icon, "banner": banner, "reason": reason})
        return self


class FakeAssetApi:
    def __init__(self, *, desired=None, rollback=None):
        self.desired = desired or {}
        self.rollback = rollback or {}
        self.uploaded = []

    async def fetch_desired(self, guild_id, application_id, asset_type):
        return self.desired[asset_type]

    async def upload_rollback(
        self,
        guild_id,
        application_id,
        asset_type,
        data,
        content_type,
    ):
        digest = hashlib.sha256(data).hexdigest()
        self.uploaded.append(
            (guild_id, application_id, asset_type, data, content_type, digest)
        )
        return {
            "key": f"rollback/{application_id}/{asset_type.lower()}",
            "contentType": content_type,
            "sha256": digest,
            "sizeBytes": len(data),
        }

    async def fetch_rollback(self, guild_id, application_id, asset_type):
        return self.rollback[asset_type]


def definition(*, resources=None, icon=None, banner=None):
    return {
        "guildId": "10",
        "themeId": 7,
        "name": "Halloween",
        "icon": icon or {"action": "UNCHANGED"},
        "banner": banner or {"action": "UNCHANGED"},
        "resources": resources or [],
    }


def install_theme_database(monkeypatch, **attributes):
    module = ModuleType("core.database")
    defaults = {
        "initialize_discord_theme_tables": lambda: None,
        "get_theme_vip_custom_role_ids": lambda guild_id: set(),
        "mark_theme_operation_running": lambda *_a, **_k: True,
        "acquire_theme_guild_lock": lambda *_a, **_k: True,
        "refresh_theme_guild_lock": lambda *_a, **_k: True,
        "release_theme_guild_lock": lambda *_a, **_k: True,
        "record_theme_operation_step": lambda *_a, **_k: 1,
        "update_theme_snapshot_apply_status": lambda *_a, **_k: True,
        "update_theme_snapshot_restore_status": lambda *_a, **_k: True,
    }
    defaults.update(attributes)
    for name, value in defaults.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "core.database", module)
    return module


def install_apply_database(
    monkeypatch,
    *,
    frozen,
    create_snapshot,
    completed,
    snapshot_statuses=None,
):
    snapshot_statuses = snapshot_statuses if snapshot_statuses is not None else []
    return install_theme_database(
        monkeypatch,
        get_theme_operation=lambda operation_id, guild_id: {
            "id": operation_id,
            "application_id": 70,
            "guild_id": guild_id,
            "operation_type": "APPLY",
            "status": "PENDING",
        },
        get_theme_application=lambda application_id, guild_id: {
            "id": application_id,
            "guild_id": guild_id,
            "status": "APPLYING",
            "frozen_definition_json": json.dumps(frozen),
        },
        create_theme_application_snapshot=create_snapshot,
        update_theme_snapshot_apply_status=(
            lambda guild_id, application_id, snapshot_id, status, **kwargs:
                snapshot_statuses.append((snapshot_id, status, kwargs))
        ),
        complete_theme_operation=(
            lambda operation_id, guild_id, **kwargs:
                completed.append(kwargs) or True
        ),
    )

def test_theme_schema_fail_fast_uses_soft_delete_aware_name_index():
    from pathlib import Path

    source = Path("core/database.py").read_text(encoding="utf-8")
    start = source.index("def initialize_discord_theme_tables")
    end = source.index("def initialize_config_levels_table", start)
    theme_schema_check = source[start:end]

    assert '("discord_themes", "uq_discord_themes_guild_active_name")' in theme_schema_check
    assert '("discord_themes", "uq_discord_themes_guild_name")' not in theme_schema_check


def test_preflight_blocks_vip_managed_hierarchy_and_unsupported_banner(monkeypatch):
    monkeypatch.setattr(theme_runtime.discord, "CategoryChannel", FakeCategory)
    vip = FakeRole(20, "VIP Nick", position=5)
    managed = FakeRole(21, "Integration", position=5, managed=True)
    high = FakeRole(22, "Owner-ish", position=80)
    guild = FakeGuild(roles=[vip, managed, high], features=[])
    install_theme_database(
        monkeypatch,
        get_theme_vip_custom_role_ids=lambda guild_id: {20},
    )

    runtime = ThemeRuntime(SimpleNamespace())
    result = runtime.build_preflight(
        guild,
        definition(
            banner={
                "action": "SET",
                "assetKey": "themes/10/7/definition/banner/banner.png",
                "contentType": "image/png",
                "sha256": "a" * 64,
            },
            resources=[
                {"resourceType": "ROLE", "resourceId": "20", "targetName": "Vampiro Nick"},
                {"resourceType": "ROLE", "resourceId": "21", "targetName": "Halloween"},
                {"resourceType": "ROLE", "resourceId": "22", "targetName": "Guardião"},
            ],
        ),
    )

    codes = {item["code"] for item in result["blockers"]}
    assert "BANNER_UNAVAILABLE" in codes
    assert "CODDY_VIP_CUSTOM_ROLE" in codes
    assert "MANAGED_ROLE" in codes
    assert "BOT_ROLE_HIERARCHY" in codes
    assert result["ready"] is False


def test_apply_persists_snapshot_before_first_discord_effect(monkeypatch):
    monkeypatch.setattr(theme_runtime.discord, "CategoryChannel", FakeCategory)
    persisted = []
    completed = []

    def assert_snapshot_exists():
        assert persisted, "Discord effect happened before durable rollback snapshot"

    channel = FakeChannel(101, "geral", before_edit=assert_snapshot_exists)
    guild = FakeGuild(channels=[channel])

    def create_snapshot(guild_id, application_id, **snapshot):
        row = {"id": 501, **snapshot}
        persisted.append(row)
        return row

    frozen = definition(
        resources=[
            {"resourceType": "CHANNEL", "resourceId": "101", "targetName": "🎃・geral"}
        ]
    )
    install_apply_database(
        monkeypatch,
        frozen=frozen,
        create_snapshot=create_snapshot,
        completed=completed,
    )

    asyncio.run(
        ThemeRuntime(SimpleNamespace()).run_apply_operation(
            guild,
            application_id=70,
            operation_id=80,
        )
    )

    assert channel.name == "🎃・geral"
    assert persisted[0]["before_value"] == "geral"
    assert persisted[0]["applied_value"] == "🎃・geral"
    assert completed[-1]["status"] == "SUCCEEDED"
    assert completed[-1]["application_status"] == "ACTIVE"
    assert completed[-1]["release_active"] is False


def test_apply_stops_before_discord_effect_when_durable_lock_is_lost(monkeypatch):
    monkeypatch.setattr(theme_runtime.discord, "CategoryChannel", FakeCategory)
    channel = FakeChannel(101, "geral")
    guild = FakeGuild(channels=[channel])
    completed = []

    def create_snapshot(guild_id, application_id, **snapshot):
        return {"id": 501, **snapshot}

    frozen = definition(
        resources=[
            {"resourceType": "CHANNEL", "resourceId": "101", "targetName": "assombrado"}
        ]
    )
    module = install_apply_database(
        monkeypatch,
        frozen=frozen,
        create_snapshot=create_snapshot,
        completed=completed,
    )
    module.refresh_theme_guild_lock = lambda *_a, **_k: False

    asyncio.run(
        ThemeRuntime(SimpleNamespace()).run_apply_operation(
            guild,
            application_id=70,
            operation_id=80,
        )
    )

    assert channel.name == "geral"
    assert completed[-1]["status"] == "FAILED"
    assert completed[-1]["application_status"] == "FAILED"
    assert completed[-1]["release_active"] is True


def test_partial_apply_keeps_application_recoverable(monkeypatch):
    monkeypatch.setattr(theme_runtime.discord, "CategoryChannel", FakeCategory)
    first = FakeChannel(101, "geral")
    second = FakeChannel(102, "artes", fail_edit=True)
    guild = FakeGuild(channels=[first, second])
    completed = []
    snapshots = []
    next_id = iter((501, 502))

    def create_snapshot(guild_id, application_id, **snapshot):
        row = {"id": next(next_id), **snapshot}
        snapshots.append(row)
        return row

    frozen = definition(
        resources=[
            {"resourceType": "CHANNEL", "resourceId": "101", "targetName": "🎃・geral"},
            {"resourceType": "CHANNEL", "resourceId": "102", "targetName": "🕸️・artes"},
        ]
    )
    install_apply_database(
        monkeypatch,
        frozen=frozen,
        create_snapshot=create_snapshot,
        completed=completed,
    )

    asyncio.run(
        ThemeRuntime(SimpleNamespace()).run_apply_operation(
            guild,
            application_id=70,
            operation_id=80,
        )
    )

    assert first.name == "🎃・geral"
    assert second.name == "artes"
    assert len(snapshots) == 2
    assert completed[-1]["status"] == "PARTIAL"
    assert completed[-1]["application_status"] == "APPLY_PARTIAL"
    assert completed[-1]["release_active"] is False


def test_restore_detects_name_drift_without_overwriting(monkeypatch):
    channel = FakeChannel(101, "mudado-manualmente")
    guild = FakeGuild(channels=[channel])
    updates = []
    install_theme_database(
        monkeypatch,
        update_theme_snapshot_restore_status=(
            lambda guild_id, application_id, snapshot_id, status, **kwargs:
                updates.append((status, kwargs))
        ),
    )
    runtime = ThemeRuntime(SimpleNamespace())

    outcome = asyncio.run(
        runtime._restore_snapshot(
            guild,
            70,
            {
                "id": 501,
                "resource_type": "CHANNEL",
                "resource_discord_id": 101,
                "before_value": "geral",
                "applied_value": "🎃・geral",
            },
            force=False,
        )
    )

    assert outcome == "DRIFTED"
    assert channel.name == "mudado-manualmente"
    assert updates[-1][0] == "DRIFTED"


def test_force_restore_overwrites_drift_with_exact_before_value(monkeypatch):
    channel = FakeChannel(101, "mudado-manualmente")
    guild = FakeGuild(channels=[channel])
    updates = []
    install_theme_database(
        monkeypatch,
        update_theme_snapshot_restore_status=(
            lambda guild_id, application_id, snapshot_id, status, **kwargs:
                updates.append((status, kwargs))
        ),
    )

    outcome = asyncio.run(
        ThemeRuntime(SimpleNamespace())._restore_snapshot(
            guild,
            70,
            {
                "id": 501,
                "resource_type": "CHANNEL",
                "resource_discord_id": 101,
                "before_value": "geral",
                "applied_value": "🎃・geral",
            },
            force=True,
        )
    )

    assert outcome == "RESTORED"
    assert channel.name == "geral"
    assert updates[-1][0] == "RESTORED"


def test_restore_missing_resource_never_recreates_by_name(monkeypatch):
    guild = FakeGuild()
    updates = []
    install_theme_database(
        monkeypatch,
        update_theme_snapshot_restore_status=(
            lambda guild_id, application_id, snapshot_id, status, **kwargs:
                updates.append((status, kwargs))
        ),
    )

    outcome = asyncio.run(
        ThemeRuntime(SimpleNamespace())._restore_snapshot(
            guild,
            70,
            {
                "id": 501,
                "resource_type": "CHANNEL",
                "resource_discord_id": 999,
                "before_value": "geral",
                "applied_value": "🎃・geral",
            },
            force=False,
        )
    )

    assert outcome == "MISSING"
    assert guild.get_channel(999) is None
    assert updates[-1][0] == "MISSING"


def test_apply_rejects_desired_asset_when_hash_differs_from_frozen_definition():
    desired = b"desired-but-not-frozen"
    desired_hash = hashlib.sha256(desired).hexdigest()
    asset_api = FakeAssetApi(
        desired={
            "ICON": ThemeAssetBytes(
                data=desired,
                content_type="image/png",
                sha256=desired_hash,
            )
        }
    )
    guild = FakeGuild()
    frozen = definition(
        icon={
            "action": "SET",
            "assetKey": "themes/10/7/definition/icon/v1.png",
            "contentType": "image/png",
            "sha256": "a" * 64,
        }
    )

    with pytest.raises(theme_runtime.ThemeRuntimeError, match="theme_desired_asset_hash_mismatch"):
        asyncio.run(
            ThemeRuntime(SimpleNamespace(), asset_api=asset_api)._capture_apply_plans(
                guild,
                70,
                frozen,
            )
        )


def test_restore_rejects_rollback_bytes_when_hash_differs_from_snapshot(monkeypatch):
    applied = b"applied-icon"
    wrong_previous = b"wrong-previous-icon"
    applied_hash = hashlib.sha256(applied).hexdigest()
    wrong_previous_hash = hashlib.sha256(wrong_previous).hexdigest()
    expected_previous_hash = hashlib.sha256(b"expected-previous-icon").hexdigest()
    asset_api = FakeAssetApi(
        rollback={
            "ICON": ThemeAssetBytes(
                data=wrong_previous,
                content_type="image/png",
                sha256=wrong_previous_hash,
            )
        }
    )
    guild = FakeGuild(icon=FakeAsset(applied))
    install_theme_database(monkeypatch)

    with pytest.raises(theme_runtime.ThemeRuntimeError, match="theme_rollback_asset_hash_mismatch"):
        asyncio.run(
            ThemeRuntime(SimpleNamespace(), asset_api=asset_api)._restore_snapshot(
                guild,
                70,
                {
                    "id": 501,
                    "resource_type": "GUILD_ICON",
                    "resource_discord_id": 0,
                    "before_value": "PRESENT",
                    "applied_value": "PRESENT",
                    "before_asset_sha256": expected_previous_hash,
                    "applied_asset_sha256": applied_hash,
                },
                force=False,
            )
        )

    assert guild.edit_calls == []


def test_cancelled_apply_remains_recoverable_instead_of_being_terminalized(monkeypatch):
    monkeypatch.setattr(theme_runtime.discord, "CategoryChannel", FakeCategory)
    channel = FakeChannel(101, "geral")
    guild = FakeGuild(channels=[channel])
    completed = []

    def create_snapshot(guild_id, application_id, **snapshot):
        return {"id": 501, **snapshot}

    frozen = definition(
        resources=[
            {"resourceType": "CHANNEL", "resourceId": "101", "targetName": "assombrado"}
        ]
    )
    install_apply_database(
        monkeypatch,
        frozen=frozen,
        create_snapshot=create_snapshot,
        completed=completed,
    )
    runtime = ThemeRuntime(SimpleNamespace())

    async def cancel_during_effect(*_args, **_kwargs):
        raise asyncio.CancelledError()

    runtime._apply_plan = cancel_during_effect

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runtime.run_apply_operation(
                guild,
                application_id=70,
                operation_id=80,
            )
        )

    assert completed == []


def test_icon_snapshot_uses_managed_assets_and_round_trips(monkeypatch):
    previous = b"\x89PNG\r\n\x1a\nprevious-icon"
    desired = b"\x89PNG\r\n\x1a\ndesired-icon"
    desired_hash = hashlib.sha256(desired).hexdigest()
    previous_hash = hashlib.sha256(previous).hexdigest()
    asset_api = FakeAssetApi(
        desired={
            "ICON": ThemeAssetBytes(
                data=desired,
                content_type="image/png",
                sha256=desired_hash,
            )
        },
        rollback={
            "ICON": ThemeAssetBytes(
                data=previous,
                content_type="image/png",
                sha256=previous_hash,
            )
        },
    )
    guild = FakeGuild(icon=FakeAsset(previous))
    runtime = ThemeRuntime(SimpleNamespace(), asset_api=asset_api)

    frozen = definition(
        icon={
            "action": "SET",
            "assetKey": "themes/10/7/definition/icon/v1.png",
            "contentType": "image/png",
            "sha256": desired_hash,
        }
    )
    plans = asyncio.run(runtime._capture_apply_plans(guild, 70, frozen))

    assert len(plans) == 1
    snapshot = plans[0]["snapshot"]
    assert snapshot["before_asset_sha256"] == previous_hash
    assert snapshot["applied_asset_sha256"] == desired_hash
    assert asset_api.uploaded[0][3] == previous

    assert asyncio.run(runtime._apply_plan(guild, 70, plans[0])) == "APPLIED"
    assert asyncio.run(guild.icon.read()) == desired

    install_theme_database(
        monkeypatch,
        update_theme_snapshot_restore_status=lambda *_a, **_k: True,
    )
    persisted = {
        "id": 501,
        **snapshot,
        "apply_status": "APPLIED",
    }
    assert asyncio.run(
        runtime._restore_snapshot(guild, 70, persisted, force=False)
    ) == "RESTORED"
    assert asyncio.run(guild.icon.read()) == previous


def test_recovery_resumes_pending_apply_instead_of_marking_it_failed(monkeypatch):
    guild = FakeGuild()
    bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 10 else None)
    runtime = ThemeRuntime(bot)
    runtime.run_apply_operation = AsyncMock(return_value=None)

    install_theme_database(
        monkeypatch,
        list_recoverable_theme_operations=lambda: [{
            "id": 80,
            "application_id": 70,
            "guild_id": 10,
            "operation_type": "APPLY",
            "status": "PENDING",
        }],
        list_theme_application_snapshots=lambda *_a, **_k: [],
        complete_theme_operation=lambda *_a, **_k: True,
    )

    recovered = asyncio.run(runtime.recover_stale_operations())

    assert recovered == 1
    runtime.run_apply_operation.assert_awaited_once_with(
        guild,
        application_id=70,
        operation_id=80,
    )


def test_recovery_reconciles_stale_running_apply_without_replaying_effects(monkeypatch):
    monkeypatch.setattr(theme_runtime.discord, "CategoryChannel", FakeCategory)
    channel = FakeChannel(
        101,
        "🎃・geral",
        before_edit=lambda: pytest.fail("recovery must not replay Discord edits"),
    )
    guild = FakeGuild(channels=[channel])
    bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 10 else None)
    runtime = ThemeRuntime(bot)
    runtime.run_apply_operation = AsyncMock(return_value=None)
    completed = []
    snapshot_updates = []

    install_theme_database(
        monkeypatch,
        list_recoverable_theme_operations=lambda: [{
            "id": 80,
            "application_id": 70,
            "guild_id": 10,
            "operation_type": "APPLY",
            "status": "RUNNING",
        }],
        list_theme_application_snapshots=lambda *_a, **_k: [{
            "id": 501,
            "resource_type": "CHANNEL",
            "resource_discord_id": 101,
            "before_value": "geral",
            "applied_value": "🎃・geral",
            "apply_status": "PENDING",
        }],
        update_theme_snapshot_apply_status=(
            lambda guild_id, application_id, snapshot_id, status, **kwargs:
                snapshot_updates.append((snapshot_id, status, kwargs))
        ),
        complete_theme_operation=(
            lambda operation_id, guild_id, **kwargs:
                completed.append(kwargs) or True
        ),
    )

    recovered = asyncio.run(runtime.recover_stale_operations())

    assert recovered == 1
    runtime.run_apply_operation.assert_not_awaited()
    assert channel.name == "🎃・geral"
    assert snapshot_updates[-1][1] == "APPLIED"
    assert completed[-1]["status"] == "SUCCEEDED"
    assert completed[-1]["application_status"] == "ACTIVE"
    assert completed[-1]["release_active"] is False
