import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import core.role_tool_runtime as module
from core.role_tool_runtime import RoleToolRuntime, RoleToolRuntimeError


def job(**overrides):
    data = {
        "id": 10, "guild_id": 88, "community_id": 5,
        "actor_user_id": 7, "actor_discord_user_id": 99,
        "operation_type": "BULK_ADD", "role_id": 555,
        "channel_id": None, "source_bot": None,
        "source_operation_id": None, "role_key": None,
        "preview_fingerprint": "a" * 64,
        "expected_count": 1, "progress_current": 0,
        "status": "PENDING",
    }
    data.update(overrides)
    return data


def guild():
    actor = SimpleNamespace(
        id=99, guild_permissions=SimpleNamespace(administrator=True)
    )
    return SimpleNamespace(
        id=88, owner_id=777, get_member=lambda uid: actor if uid == 99 else None,
        me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=True)),
    )


def test_dispatch_accepts_only_persisted_actor_and_never_starts_two_workers():
    async def scenario():
        bot = Mock()
        runtime = RoleToolRuntime(bot)
        current = job()
        runtime._read_job = AsyncMock(return_value=current)
        runtime._claim = AsyncMock(return_value=True)
        release = asyncio.Event()

        async def fake_worker(g, requested):
            await release.wait()

        runtime._run = fake_worker
        accepted = await runtime.dispatch(guild(), 99, 10)
        assert accepted["status"] == "RUNNING"
        with pytest.raises(RoleToolRuntimeError, match="another_role_operation_running"):
            await runtime.dispatch(guild(), 99, 10)
        runtime._claim.assert_awaited_once()

        with pytest.raises(RoleToolRuntimeError, match="role_operation_not_found"):
            await runtime.dispatch(guild(), 100, 10)
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    asyncio.run(scenario())


def test_bulk_fails_if_membership_changed_since_preview(monkeypatch):
    async def scenario():
        runtime = RoleToolRuntime(Mock())
        role = SimpleNamespace(id=555)
        monkeypatch.setattr(module, "_require_bulk_role_access", lambda *args: role)
        monkeypatch.setattr(module, "build_bulk_role_preview", lambda *args: {
            "fingerprint": "b" * 64, "eligible": 1
        })
        g = SimpleNamespace(chunked=True)
        with pytest.raises(RoleToolRuntimeError, match="PREVIEW_CHANGED"):
            await runtime._run_bulk(g, job())
    asyncio.run(scenario())


def test_worker_skips_already_assigned_and_bot_members_without_discord_network(monkeypatch):
    async def scenario():
        runtime = RoleToolRuntime(Mock())
        role = SimpleNamespace(id=555)
        monkeypatch.setattr(module, "_require_bulk_role_access", lambda *args: role)
        missing = SimpleNamespace(id=1, bot=False, roles=[])
        missing.add_roles = AsyncMock()
        already = SimpleNamespace(id=2, bot=False, roles=[role])
        bot = SimpleNamespace(id=3, bot=True, roles=[])
        guild_members = {1: missing, 2: already, 3: bot}
        g = SimpleNamespace(get_member=lambda uid: guild_members.get(uid))
        runtime._progress = AsyncMock()
        runtime._complete = AsyncMock()
        await runtime._assign_members(
            g, job(), role, [1, 2, 3], allow_missing=False
        )
        missing.add_roles.assert_awaited_once()
        assert runtime._complete.await_args.args[1] == "SUCCEEDED"
        assert runtime._complete.await_args.kwargs["added"] == 1
        assert runtime._complete.await_args.kwargs["skipped"] == 2
        assert runtime._complete.await_args.kwargs["failed"] == 0
    asyncio.run(scenario())


def test_failed_discord_role_assignment_is_counted_without_retrying_successes(monkeypatch):
    async def scenario():
        runtime = RoleToolRuntime(Mock())
        role = SimpleNamespace(id=555)
        monkeypatch.setattr(module, "_require_bulk_role_access", lambda *args: role)
        member = SimpleNamespace(id=1, bot=False, roles=[])
        member.add_roles = AsyncMock(side_effect=module.discord.Forbidden(
            Mock(status=403, reason="Forbidden"), "missing_permissions"
        ))
        g = SimpleNamespace(get_member=lambda uid: member)
        runtime._progress = AsyncMock()
        runtime._complete = AsyncMock()
        await runtime._assign_members(g, job(), role, [1], allow_missing=False)
        assert runtime._complete.await_args.args[1] == "PARTIAL"
        assert runtime._complete.await_args.kwargs["failed"] == 1
    asyncio.run(scenario())
