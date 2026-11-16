"""Role migration tests use only fake guilds; never connect to Discord."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.bulk_role_assignment import BulkRolePreviewError
from core.role_migration import build_role_migration_preview
from core.role_tool_runtime import RoleToolRuntime


class FakeRole:
    def __init__(self, id, name, position, managed=False):
        self.id, self.name, self.position, self.managed = id, name, position, managed

    def is_default(self):
        return False

    def __gt__(self, other):
        return self.position > other.position


def fixture():
    source = FakeRole(55, "Antigo", 4)
    target = FakeRole(66, "Novo", 5)
    top = FakeRole(100, "Admin", 15)
    actor = SimpleNamespace(
        id=101, bot=False, roles=[], top_role=top,
        guild_permissions=SimpleNamespace(administrator=True),
    )
    events = []

    async def add_new(role, reason):
        events.append("add")
        member.roles.append(role)

    async def remove_old(role, reason):
        events.append("remove")
        member.roles.remove(role)

    member = SimpleNamespace(
        id=102, bot=False, roles=[source],
        add_roles=AsyncMock(side_effect=add_new),
        remove_roles=AsyncMock(side_effect=remove_old),
    )
    existing = SimpleNamespace(
        id=103, bot=False, roles=[source, target],
        add_roles=AsyncMock(), remove_roles=AsyncMock(),
    )
    bot_member = SimpleNamespace(id=104, bot=True, roles=[source])
    members = [actor, member, existing, bot_member]
    guild = SimpleNamespace(
        id=777, owner_id=999, members=members, chunked=True,
        me=SimpleNamespace(top_role=FakeRole(200, "Coddy", 20),
                           guild_permissions=SimpleNamespace(manage_roles=True)),
        get_member=lambda uid: next((m for m in members if m.id == uid), None),
        get_role=lambda rid: {55: source, 66: target}.get(rid),
    )
    return guild, source, target, member, existing, events


def job(preview):
    return {
        "id": 900, "guild_id": 777, "community_id": 5,
        "operation_type": "ROLE_MIGRATE", "source_role_id": 55,
        "role_id": 66, "actor_discord_user_id": 101,
        "preview_fingerprint": preview["fingerprint"],
        "expected_count": preview["sourceMembers"],
    }


def test_preview_counts_source_members_only_and_excludes_bots():
    guild, source, target, member, existing, _ = fixture()
    p = build_role_migration_preview(guild, 101, source.id, target.id)
    assert p["sourceMembers"] == 2
    assert p["needsAddition"] == 1
    assert p["alreadyHasDestination"] == 1
    assert p["excludedBots"] == 1
    assert len(p["fingerprint"]) == 64
    assert p["sourceRoleName"] == "Antigo"


def test_preview_rejects_identical_roles_managed_roles_and_partial_cache():
    guild, source, target, *_ = fixture()
    with pytest.raises(BulkRolePreviewError, match="identical_source_and_destination"):
        build_role_migration_preview(guild, 101, source.id, source.id)
    source.managed = True
    with pytest.raises(BulkRolePreviewError, match="role_not_assignable"):
        build_role_migration_preview(guild, 101, source.id, target.id)
    source.managed = False
    guild.chunked = False
    with pytest.raises(BulkRolePreviewError, match="member_cache_incomplete"):
        build_role_migration_preview(guild, 101, source.id, target.id)


def test_migration_previews_fail_closed_when_membership_changes():
    guild, source, target, member, *_ = fixture()
    before = build_role_migration_preview(guild, 101, 55, 66)
    member.roles.append(target)
    assert before["fingerprint"] != build_role_migration_preview(guild, 101, 55, 66)["fingerprint"]


def test_migration_assigns_destination_before_removing_source():
    async def scenario():
        guild, source, target, member, existing, events = fixture()
        preview = build_role_migration_preview(guild, 101, 55, 66)
        runtime = RoleToolRuntime(Mock())
        runtime._progress = AsyncMock()
        runtime._complete = AsyncMock()
        await runtime._run_migration(guild, job(preview))
        assert events == ["add", "remove"]
        assert source not in member.roles and target in member.roles
        existing.add_roles.assert_not_awaited()
        existing.remove_roles.assert_awaited_once()
        assert runtime._complete.await_args.args[1] == "SUCCEEDED"
        assert runtime._complete.await_args.kwargs["added"] == 1
        assert runtime._complete.await_args.kwargs["removed"] == 2
    asyncio.run(scenario())


def test_failed_destination_addition_keeps_original_role():
    async def scenario():
        guild, source, target, member, existing, events = fixture()
        member.add_roles.side_effect = RuntimeError("test_failed_add")
        # Discord HTTP failure is the expected per-member recoverable outcome.
        import discord
        response = Mock(status=403, reason="Forbidden")
        member.add_roles.side_effect = discord.Forbidden(response, "blocked")
        preview = build_role_migration_preview(guild, 101, 55, 66)
        runtime = RoleToolRuntime(Mock())
        runtime._progress = AsyncMock()
        runtime._complete = AsyncMock()
        await runtime._run_migration(guild, job(preview))
        assert source in member.roles and target not in member.roles
        member.remove_roles.assert_not_awaited()
        assert runtime._complete.await_args.args[1] == "PARTIAL"
        assert runtime._complete.await_args.kwargs["failed"] == 1
        assert runtime._complete.await_args.kwargs["removed"] == 1
    asyncio.run(scenario())
