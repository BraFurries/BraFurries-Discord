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


def test_scan_uses_existing_import_parser_and_persists_complete_snapshot(monkeypatch):
    class FakeChannel:
        id = 300

        async def history(self, **kwargs):
            assert kwargs == {"limit": None, "oldest_first": True}
            for id in range(3):
                yield SimpleNamespace(id=id)

    async def scenario():
        parser = Mock()
        parser._normalize_role_name.side_effect = str.casefold
        parser._is_message_from_selected_bot.side_effect = lambda message, *_: message.id != 1

        def process(bot_name, message, roles_by_id, roles_by_name, entries):
            assert bot_name == "Dyno"
            entries["id:555"] = SimpleNamespace(
                display_name="Cargo legado",
                role_id_from_log=555,
                server_role=None,
                member_ids={101, 202},
                is_name_ambiguous=False,
            )
            return None

        parser._process_recovery_message.side_effect = process
        monkeypatch.setattr(module.discord, "TextChannel", FakeChannel)
        runtime = RoleToolRuntime(Mock(get_cog=lambda name: parser if name == "Import" else None))
        runtime._complete = AsyncMock()
        g = SimpleNamespace(
            id=88, roles=[], get_channel=lambda channel_id: FakeChannel() if channel_id == 300 else None
        )
        await runtime._run_scan(g, job(
            operation_type="RECOVERY_SCAN", source_bot="Dyno", channel_id=300,
        ))
        args = runtime._complete.await_args
        assert args.args[1] == "READY"
        assert args.kwargs["scanned"] == 3
        result = args.kwargs["result"]
        assert result["botMessagesFound"] == 2
        assert result["roleEntries"][0]["memberIds"] == [101, 202]
        assert result["roleEntries"][0]["foundRoleId"] is None
        assert parser._process_recovery_message.call_count == 2
    asyncio.run(scenario())


def test_recovery_only_restores_members_in_selected_persisted_role(monkeypatch):
    async def scenario():
        runtime = RoleToolRuntime(Mock())
        source = {
            "roleEntries": [
                {"key": "id:123", "memberIds": [3, 5]},
                {"key": "id:456", "memberIds": [99]},
            ]
        }
        role = SimpleNamespace(id=555)
        runtime._read_source = AsyncMock(return_value=source)
        runtime._assign_members = AsyncMock()
        monkeypatch.setattr(module, "_require_bulk_role_access", lambda *args: role)
        await runtime._run_recovery(SimpleNamespace(id=88), job(
            operation_type="RECOVERY_APPLY", role_key="id:123",
            source_operation_id=7, expected_count=2,
        ))
        assert runtime._assign_members.await_args.args[3] == [3, 5]
        assert runtime._assign_members.await_args.kwargs["allow_missing"] is True

        with pytest.raises(RoleToolRuntimeError, match="ROLE_NOT_FOUND_IN_SCAN"):
            await runtime._run_recovery(SimpleNamespace(id=88), job(
                operation_type="RECOVERY_APPLY", role_key="id:789",
                source_operation_id=7, expected_count=2,
            ))
        assert runtime._assign_members.await_count == 1
    asyncio.run(scenario())


def test_worker_stops_if_operation_is_no_longer_running(monkeypatch):
    from contextlib import asynccontextmanager

    class FakeCursor:
        rowcount = 0

        async def execute(self, sql, args):
            assert "status='RUNNING'" in sql

    @asynccontextmanager
    async def fake_connection():
        yield FakeCursor()

    async def scenario():
        monkeypatch.setattr(module, "role_tool_connection", fake_connection)
        with pytest.raises(RoleToolRuntimeError, match="JOB_NOT_RUNNING"):
            await RoleToolRuntime._progress(
                job(), current=25, total=100,
                added=20, skipped=5, failed=0,
            )
    asyncio.run(scenario())


def test_parallel_role_workers_stop_before_operation_can_be_finalized(monkeypatch):
    async def scenario():
        runtime = RoleToolRuntime(Mock())
        role = SimpleNamespace(id=555)
        monkeypatch.setattr(module, "_require_bulk_role_access", lambda *args: role)
        second_started = asyncio.Event()
        second_cancelled = asyncio.Event()
        leaked = []

        async def first_assignment(*args, **kwargs):
            await second_started.wait()
            raise RuntimeError("unexpected_discord_runtime_failure")

        async def second_assignment(*args, **kwargs):
            second_started.set()
            try:
                await asyncio.sleep(0.5)
                leaked.append("role_was_assigned_after_failure")
            except asyncio.CancelledError:
                second_cancelled.set()
                raise

        first = SimpleNamespace(id=1, bot=False, roles=[], add_roles=first_assignment)
        second = SimpleNamespace(id=2, bot=False, roles=[], add_roles=second_assignment)
        guild = SimpleNamespace(
            get_member=lambda uid: {1: first, 2: second}.get(uid)
        )
        runtime._progress = AsyncMock()
        runtime._complete = AsyncMock()
        with pytest.raises(ExceptionGroup):
            await runtime._assign_members(
                guild, job(), role, [1, 2], allow_missing=False
            )
        assert second_started.is_set()
        assert second_cancelled.is_set()
        assert leaked == []
        runtime._complete.assert_not_awaited()

    asyncio.run(scenario())


def test_interrupted_mutating_job_never_reports_no_effects(monkeypatch):
    async def scenario():
        runtime = RoleToolRuntime(Mock())
        runtime._complete = AsyncMock()
        class Cursor:
            execute = AsyncMock()
            fetchone = AsyncMock(return_value={
                "added_count": 0, "progress_current": 0,
                "progress_total": 3, "skipped_count": 0,
                "failed_count": 0, "scanned_count": 0,
            })
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def connection():
            yield Cursor()

        monkeypatch.setattr(module, "role_tool_connection", connection)
        await runtime._mark_interrupted(job(), "WORKER_INTERRUPTED")
        assert runtime._complete.await_args.args[1] == "PARTIAL"

        runtime._complete.reset_mock()
        await runtime._mark_interrupted(
            job(operation_type="RECOVERY_SCAN"), "WORKER_INTERRUPTED"
        )
        assert runtime._complete.await_args.args[1] == "FAILED"

    asyncio.run(scenario())
