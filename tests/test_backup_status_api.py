import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

from message_services.bot_status_api import BotStatusApi


class Request:
    def __init__(self, guild_id, discord_user_id, *, backup_id=None, payload=None):
        self.match_info = {
            "guild_id": str(guild_id),
            "discord_user_id": str(discord_user_id),
        }
        if backup_id is not None:
            self.match_info["backup_id"] = str(backup_id)
        self._payload = payload or {}

    async def json(self):
        return self._payload


def make_bot(guild):
    return SimpleNamespace(
        is_ready=lambda: True,
        get_guild=lambda guild_id: guild if guild_id == guild.id else None,
        user=SimpleNamespace(id=999),
    )


def test_backup_snapshot_requires_live_guild_management_access(monkeypatch):
    guild = SimpleNamespace(id=10, owner_id=1)
    api = BotStatusApi(
        make_bot(guild),
        host="127.0.0.1",
        port=8080,
        token="internal",
        initialized_getter=lambda: True,
    )
    monkeypatch.setattr(
        "message_services.bot_status_api.can_manage_guild",
        lambda _guild, _user: False,
    )
    api._backup_runtime.create_snapshot = AsyncMock()

    response = asyncio.run(api._handle_backup_snapshot(
        Request(10, 2, payload={"name": "Before", "idempotencyKey": "key-1"})
    ))

    assert response.status == 403
    api._backup_runtime.create_snapshot.assert_not_awaited()


def test_backup_snapshot_dispatches_persisted_operation_for_authorized_guild(monkeypatch):
    guild = SimpleNamespace(id=10, owner_id=1)
    api = BotStatusApi(
        make_bot(guild),
        host="127.0.0.1",
        port=8080,
        token="internal",
        initialized_getter=lambda: True,
    )
    monkeypatch.setattr(
        "message_services.bot_status_api.can_manage_guild",
        lambda _guild, _user: True,
    )
    database_module = ModuleType("core.database")
    database_module.get_backup_snapshot_operation = lambda operation_id, guild_id: {
        "id": operation_id,
        "guild_id": guild_id,
        "backup_type": "normal",
        "actor_discord_user_id": 2,
        "status": "PENDING",
    }
    monkeypatch.setitem(sys.modules, "core.database", database_module)
    api._backup_runtime.run_snapshot_operation = AsyncMock()

    async def invoke():
        response = await api._handle_backup_snapshot(
            Request(10, 2, payload={"operationId": 77})
        )
        await asyncio.sleep(0)
        return response

    response = asyncio.run(invoke())

    assert response.status == 202
    payload = json.loads(response.text)
    assert payload["operationId"] == 77
    assert payload["accepted"] is True
    api._backup_runtime.run_snapshot_operation.assert_awaited_once_with(
        guild,
        operation_id=77,
    )


def test_restore_preview_is_owner_only_even_for_server_admin():
    guild = SimpleNamespace(id=10, owner_id=1)
    api = BotStatusApi(
        make_bot(guild),
        host="127.0.0.1",
        port=8080,
        token="internal",
        initialized_getter=lambda: True,
    )
    api._backup_runtime.build_restore_preflight = Mock()

    response = asyncio.run(api._handle_backup_restore_preview(
        Request(10, 2, backup_id=77, payload={"scope": "full"})
    ))

    assert response.status == 403
    assert json.loads(response.text)["error"] == "guild_owner_required"
    api._backup_runtime.build_restore_preflight.assert_not_called()


def test_restore_preview_never_reads_cross_guild_backup():
    guild = SimpleNamespace(id=10, owner_id=1)
    api = BotStatusApi(
        make_bot(guild),
        host="127.0.0.1",
        port=8080,
        token="internal",
        initialized_getter=lambda: True,
    )
    api._backup_runtime.build_restore_preflight = Mock(
        side_effect=LookupError("backup_not_found_for_guild")
    )

    response = asyncio.run(api._handle_backup_restore_preview(
        Request(10, 1, backup_id=999, payload={"scope": "full"})
    ))

    assert response.status == 404
    assert json.loads(response.text)["error"] == "backup_not_found_for_guild"


def test_restore_dispatch_rejects_operation_from_another_guild(monkeypatch):
    guild = SimpleNamespace(id=10, owner_id=1)
    api = BotStatusApi(
        make_bot(guild),
        host="127.0.0.1",
        port=8080,
        token="internal",
        initialized_getter=lambda: True,
    )
    database_module = ModuleType("core.database")
    database_module.get_backup_restore_operation = lambda _operation_id, _guild_id: None
    monkeypatch.setitem(sys.modules, "core.database", database_module)
    api._backup_runtime.run_restore_operation = AsyncMock()

    response = asyncio.run(api._handle_backup_restore_dispatch(
        Request(
            10,
            1,
            backup_id=77,
            payload={
                "operationId": 88,
                "scope": "full",
                "decision": {},
            },
        )
    ))

    assert response.status == 404
    api._backup_runtime.run_restore_operation.assert_not_awaited()


def test_restore_dispatch_is_async_and_owner_scoped(monkeypatch):
    guild = SimpleNamespace(id=10, owner_id=1)
    api = BotStatusApi(
        make_bot(guild),
        host="127.0.0.1",
        port=8080,
        token="internal",
        initialized_getter=lambda: True,
    )
    database_module = ModuleType("core.database")
    database_module.get_backup_restore_operation = lambda operation_id, guild_id: {
        "id": operation_id,
        "guild_id": guild_id,
        "backup_id": 77,
        "scope": "full",
        "actor_discord_user_id": 1,
        "status": "PENDING",
    }
    monkeypatch.setitem(sys.modules, "core.database", database_module)
    api._backup_runtime.run_restore_operation = AsyncMock()

    async def scenario():
        response = await api._handle_backup_restore_dispatch(
            Request(
                10,
                1,
                backup_id=77,
                payload={
                    "operationId": 88,
                    "scope": "full",
                    "decision": {"duplicateStrategy": "explicit", "roleResolutions": {}},
                },
            )
        )
        await asyncio.sleep(0)
        return response

    response = asyncio.run(scenario())

    assert response.status == 202
    assert json.loads(response.text)["operationId"] == 88
    api._backup_runtime.run_restore_operation.assert_awaited_once()

def test_stop_cancels_and_drains_backup_background_tasks():
    guild = SimpleNamespace(id=10, owner_id=1)
    api = BotStatusApi(
        make_bot(guild),
        host="127.0.0.1",
        port=8080,
        token="internal",
        initialized_getter=lambda: True,
    )

    async def scenario():
        finalized = asyncio.Event()

        async def background_restore():
            try:
                await asyncio.Event().wait()
            finally:
                finalized.set()

        task = asyncio.create_task(background_restore())
        api._background_tasks.add(task)
        task.add_done_callback(api._background_tasks.discard)
        await asyncio.sleep(0)

        await api.stop()

        assert finalized.is_set()
        assert task.cancelled()
        assert not api._background_tasks

    asyncio.run(scenario())

