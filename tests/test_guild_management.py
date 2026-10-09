import asyncio
import json
import pytest
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from aiohttp.test_utils import make_mocked_request

import core.guild_management as guild_management
from core.guild_management import _ensure_role, _publish_portaria_form, _validate_portaria_bypass_target, build_guild_resources, build_structure_preview, can_manage_guild
from message_services.bot_status_api import BotStatusApi, _build_auth_middleware


class FakeRole:
    def __init__(self, role_id, name, position, managed=False):
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = managed

    def __lt__(self, other):
        return (self.position, self.id) < (other.position, other.id)


class FakeGuild:
    def __init__(self, *, manage_channels=True, manage_roles=True, administrator=False, top_position=20):
        self.id = 1480343896461545606
        self.owner_id = 1
        self.default_role = FakeRole(1, "@everyone", 0)
        self.roles = [
            self.default_role,
            FakeRole(10, "Moderador", 5),
            FakeRole(20, "Acima do Coddy", 30),
        ]
        self.categories = [SimpleNamespace(id=100, name="Geral")]
        self.text_channels = [SimpleNamespace(id=200, name="geral", type="text", category_id=100)]
        self.channels = [
            SimpleNamespace(id=100, name="Geral", type="category"),
            SimpleNamespace(id=200, name="geral", type="text", category_id=100),
            SimpleNamespace(id=201, name="Voz livre", type="voice"),
            SimpleNamespace(id=202, name="forum", type="forum"),
            SimpleNamespace(id=203, name="palco", type="stage_voice"),
        ]
        self.bot_top_role = FakeRole(15, "Coddy", top_position)
        self.roles.append(self.bot_top_role)
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_channels=manage_channels, manage_roles=manage_roles, administrator=administrator),
            top_role=self.bot_top_role,
            roles=[self.default_role, self.bot_top_role],
        )
        self.members = {
            1: SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False, manage_guild=False)),
            2: SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True, manage_guild=False)),
            3: SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False, manage_guild=True)),
            4: SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False, manage_guild=False)),
        }

    def get_member(self, user_id):
        return self.members.get(user_id)

    def by_category(self):
        uncategorized = [
            channel for channel in self.channels
            if str(getattr(channel, "type", "")) != "category"
            and getattr(channel, "category_id", None) is None
        ]
        return [
            (None, uncategorized),
            (self.categories[0], list(self.text_channels)),
        ]


class FakePublishedMessage:
    def __init__(self, message_id, components=()):
        self.id = message_id
        self.components = components
        self.delete = AsyncMock()
        self.edit = AsyncMock()


class FakePublicationChannel:
    def __init__(self, channel_id, *, block_send=None, send_started=None):
        self.id = channel_id
        self.messages = {}
        self._block_send = block_send
        self._send_started = send_started
        self.send = AsyncMock(side_effect=self._send)
        self.fetch_message = AsyncMock(side_effect=self._fetch_message)

    async def _send(self, **_kwargs):
        if self._send_started is not None:
            self._send_started.set()
        if self._block_send is not None:
            await self._block_send.wait()
        message = FakePublishedMessage(900000000000000000 + len(self.messages) + 1)
        self.messages[message.id] = message
        return message

    async def _fetch_message(self, message_id):
        return self.messages[message_id]


class FakePublicationGuild:
    def __init__(self, channels):
        self.id = 1480343896461545606
        self._channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


def _fake_portaria_dependencies(monkeypatch, records, *, persist=None):
    database = types.ModuleType("core.database")
    database.get_form_flow = Mock(return_value={"id": 7, "name": "Portaria", "type": "portaria"})
    database.list_portaria_published_channels = Mock(side_effect=lambda _guild_id: list(records))
    database.create_form_published_message = persist or Mock(side_effect=lambda flow_id, message_id, channel_id, guild_id: records.append({
        "flow_id": flow_id, "message_id": message_id, "channel_id": channel_id, "guild_id": guild_id,
    }))
    form_views = types.ModuleType("core.form_views")
    form_views.FormFlowButtonView = Mock(side_effect=lambda flow_id, label="Abrir formulário": SimpleNamespace(flow_id=flow_id, label=label))
    monkeypatch.setitem(sys.modules, "core.database", database)
    monkeypatch.setitem(sys.modules, "core.form_views", form_views)
    guild_management._portaria_publication_locks.clear()
    return database, form_views


def test_portaria_publish_creates_a_missing_publication(monkeypatch):
    records = []
    database, _ = _fake_portaria_dependencies(monkeypatch, records)
    channel = FakePublicationChannel(200)

    result = asyncio.run(_publish_portaria_form(FakePublicationGuild([channel]), {"flowId": 7, "welcomeChannelId": "200"}))

    assert result == {"created": [{"key": "publication", "id": "900000000000000001"}], "reused": [], "updated": [], "warnings": [],
                      "resources": {"messageId": "900000000000000001", "welcomeChannelId": "200"}}
    channel.send.assert_awaited_once()
    assert channel.send.await_args.kwargs["content"] == "📋 **Portaria**\nClique no botão para abrir o formulário."
    assert channel.send.await_args.kwargs["view"].label == "Abrir formulário"
    database.create_form_published_message.assert_called_once_with(7, 900000000000000001, 200, 1480343896461545606)


def test_portaria_publish_reuses_a_live_publication_for_the_same_flow_and_channel(monkeypatch):
    channel = FakePublicationChannel(200)
    message = FakePublishedMessage(333)
    channel.messages[message.id] = message
    records = [{"flow_id": 7, "channel_id": 200, "message_id": 333}]
    _fake_portaria_dependencies(monkeypatch, records)

    result = asyncio.run(_publish_portaria_form(FakePublicationGuild([channel]), {"flowId": 7, "welcomeChannelId": "200"}))

    assert result["created"] == []
    assert result["reused"] == [{"key": "publication", "id": "333"}]
    channel.send.assert_not_awaited()
    message.edit.assert_not_awaited()


def test_portaria_publish_creates_with_custom_message_and_button(monkeypatch):
    records = []
    _, form_views = _fake_portaria_dependencies(monkeypatch, records)
    channel = FakePublicationChannel(200)

    result = asyncio.run(_publish_portaria_form(FakePublicationGuild([channel]), {
        "flowId": 7, "welcomeChannelId": "200", "message": "Minha ficha", "buttonText": "Fazer minha ficha",
    }))

    assert result["created"]
    assert channel.send.await_args.kwargs["content"] == "Minha ficha"
    assert channel.send.await_args.kwargs["view"].label == "Fazer minha ficha"
    form_views.FormFlowButtonView.assert_called_once_with(7, label="Fazer minha ficha")


def test_portaria_publish_allows_empty_custom_message(monkeypatch):
    _fake_portaria_dependencies(monkeypatch, [])
    channel = FakePublicationChannel(200)

    asyncio.run(_publish_portaria_form(FakePublicationGuild([channel]), {"flowId": 7, "welcomeChannelId": "200", "message": ""}))

    assert channel.send.await_args.kwargs["content"] is None


def test_portaria_publish_updates_existing_message_and_preserves_existing_button_label(monkeypatch):
    button = SimpleNamespace(custom_id="form_flow:7", label="Ficha existente")
    message = FakePublishedMessage(333, components=(SimpleNamespace(children=(button,)),))
    channel = FakePublicationChannel(200)
    channel.messages[message.id] = message
    _fake_portaria_dependencies(monkeypatch, [{"flow_id": 7, "channel_id": 200, "message_id": 333}])

    result = asyncio.run(_publish_portaria_form(FakePublicationGuild([channel]), {"flowId": 7, "welcomeChannelId": "200", "message": "Atualizada"}))

    assert result["updated"] == [{"key": "publication", "id": "333"}]
    message.edit.assert_awaited_once()
    assert message.edit.await_args.kwargs["content"] == "Atualizada"
    assert message.edit.await_args.kwargs["view"].label == "Ficha existente"
    channel.send.assert_not_awaited()


def test_portaria_publish_updates_existing_button(monkeypatch):
    message = FakePublishedMessage(333)
    channel = FakePublicationChannel(200)
    channel.messages[message.id] = message
    _fake_portaria_dependencies(monkeypatch, [{"flow_id": 7, "channel_id": 200, "message_id": 333}])

    result = asyncio.run(_publish_portaria_form(FakePublicationGuild([channel]), {"flowId": 7, "welcomeChannelId": "200", "buttonText": "Novo botão"}))

    assert result["updated"]
    assert message.edit.await_args.kwargs["view"].label == "Novo botão"


def test_portaria_publish_uses_default_button_label_when_existing_label_cannot_be_recovered(monkeypatch):
    message = FakePublishedMessage(333)
    channel = FakePublicationChannel(200)
    channel.messages[message.id] = message
    _fake_portaria_dependencies(monkeypatch, [{"flow_id": 7, "channel_id": 200, "message_id": 333}])

    asyncio.run(_publish_portaria_form(FakePublicationGuild([channel]), {"flowId": 7, "welcomeChannelId": "200", "message": "Atualizada"}))

    assert message.edit.await_args.kwargs["view"].label == "Abrir formulário"


@pytest.mark.parametrize("exception_name", ["Forbidden", "HTTPException"])
def test_portaria_publish_propagates_edit_failures_without_sending(monkeypatch, exception_name):
    class EditError(Exception):
        pass

    monkeypatch.setattr(guild_management.discord, exception_name, EditError)
    message = FakePublishedMessage(333)
    message.edit.side_effect = EditError()
    channel = FakePublicationChannel(200)
    channel.messages[message.id] = message
    _fake_portaria_dependencies(monkeypatch, [{"flow_id": 7, "channel_id": 200, "message_id": 333}])

    with pytest.raises(EditError):
        asyncio.run(_publish_portaria_form(FakePublicationGuild([channel]), {"flowId": 7, "welcomeChannelId": "200", "message": "Atualizada"}))
    channel.send.assert_not_awaited()


@pytest.mark.parametrize("payload", [
    {"message": "x" * 2001}, {"buttonText": "   "}, {"buttonText": "x" * 81},
    {"message": 7}, {"buttonText": 7},
])
def test_portaria_publish_rejects_invalid_customization_payload(monkeypatch, payload):
    _fake_portaria_dependencies(monkeypatch, [])
    channel = FakePublicationChannel(200)
    payload.update({"flowId": 7, "welcomeChannelId": "200"})

    with pytest.raises(ValueError, match="invalid_portaria_publication"):
        asyncio.run(_publish_portaria_form(FakePublicationGuild([channel]), payload))
    channel.send.assert_not_awaited()


def test_portaria_publish_recreates_a_stale_not_found_publication(monkeypatch):
    class NotFoundError(Exception):
        pass

    monkeypatch.setattr(guild_management.discord, "NotFound", NotFoundError)
    channel = FakePublicationChannel(200)
    channel.fetch_message.side_effect = NotFoundError()
    records = [{"flow_id": 7, "channel_id": 200, "message_id": 333}]
    _fake_portaria_dependencies(monkeypatch, records)

    result = asyncio.run(_publish_portaria_form(FakePublicationGuild([channel]), {"flowId": 7, "welcomeChannelId": "200"}))

    assert result["created"][0]["key"] == "publication"
    channel.send.assert_awaited_once()


@pytest.mark.parametrize("exception_name", ["Forbidden", "HTTPException"])
def test_portaria_publish_propagates_live_fetch_failures_without_sending(monkeypatch, exception_name):
    class FetchError(Exception):
        pass

    monkeypatch.setattr(guild_management.discord, exception_name, FetchError)
    channel = FakePublicationChannel(200)
    channel.fetch_message.side_effect = FetchError()
    records = [{"flow_id": 7, "channel_id": 200, "message_id": 333}]
    _fake_portaria_dependencies(monkeypatch, records)

    with pytest.raises(FetchError):
        asyncio.run(_publish_portaria_form(FakePublicationGuild([channel]), {"flowId": 7, "welcomeChannelId": "200"}))

    channel.send.assert_not_awaited()


def test_portaria_publish_deletes_sent_message_when_persistence_fails(monkeypatch):
    persist = Mock(side_effect=RuntimeError("database unavailable"))
    _fake_portaria_dependencies(monkeypatch, [], persist=persist)
    channel = FakePublicationChannel(200)

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(_publish_portaria_form(FakePublicationGuild([channel]), {"flowId": 7, "welcomeChannelId": "200"}))

    sent = next(iter(channel.messages.values()))
    sent.delete.assert_awaited_once()


def test_portaria_publish_serializes_concurrent_requests_for_the_same_key(monkeypatch):
    records = []
    _fake_portaria_dependencies(monkeypatch, records)
    release_send = asyncio.Event()
    send_started = asyncio.Event()
    channel = FakePublicationChannel(200, block_send=release_send, send_started=send_started)
    guild = FakePublicationGuild([channel])

    async def publish_twice():
        first = asyncio.create_task(_publish_portaria_form(guild, {"flowId": 7, "welcomeChannelId": "200"}))
        await send_started.wait()
        second = asyncio.create_task(_publish_portaria_form(guild, {"flowId": 7, "welcomeChannelId": "200"}))
        await asyncio.sleep(0)
        release_send.set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(publish_twice())

    assert channel.send.await_count == 1
    assert first["created"] or second["created"]
    assert first["reused"] or second["reused"]


def test_portaria_publish_uses_independent_locks_for_different_publication_keys(monkeypatch):
    _fake_portaria_dependencies(monkeypatch, [])
    release_send = asyncio.Event()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    first_channel = FakePublicationChannel(200, block_send=release_send, send_started=first_started)
    second_channel = FakePublicationChannel(201, block_send=release_send, send_started=second_started)
    guild = FakePublicationGuild([first_channel, second_channel])

    async def publish_in_parallel():
        first = asyncio.create_task(_publish_portaria_form(guild, {"flowId": 7, "welcomeChannelId": "200"}))
        second = asyncio.create_task(_publish_portaria_form(guild, {"flowId": 7, "welcomeChannelId": "201"}))
        try:
            await asyncio.wait_for(asyncio.gather(first_started.wait(), second_started.wait()), timeout=0.2)
        finally:
            release_send.set()
        return await asyncio.gather(first, second)

    asyncio.run(publish_in_parallel())

    first_channel.send.assert_awaited_once()
    second_channel.send.assert_awaited_once()


def test_authorization_accepts_owner_administrator_and_manage_guild():
    guild = FakeGuild()
    assert can_manage_guild(guild, 1)
    assert can_manage_guild(guild, 2)
    assert can_manage_guild(guild, 3)
    assert not can_manage_guild(guild, 4)
    assert not can_manage_guild(guild, 999)


def test_resources_are_scoped_to_the_requested_guild_and_keep_snowflakes_as_strings():
    resources = build_guild_resources(FakeGuild())
    assert resources["guildId"] == "1480343896461545606"
    assert resources["roles"] == [
        {"id": "20", "name": "Acima do Coddy", "position": 30, "managed": False, "editableByBot": False},
        {"id": "15", "name": "Coddy", "position": 20, "managed": False, "editableByBot": False},
        {"id": "10", "name": "Moderador", "position": 5, "managed": False, "editableByBot": True},
    ]
    assert resources["channels"] == [
        {"id": "201", "name": "Voz livre", "type": "voice", "categoryId": None},
        {"id": "202", "name": "forum", "type": "forum", "categoryId": None},
        {"id": "203", "name": "palco", "type": "stage_voice", "categoryId": None},
        {"id": "200", "name": "geral", "type": "text", "categoryId": "100"},
    ]
    assert resources["categories"] == [{"id": "100", "name": "Geral"}]


def test_resources_follow_discord_ui_order_from_by_category_and_keep_empty_categories():
    guild = FakeGuild()
    first_category = SimpleNamespace(id=101, name="Primeira")
    second_category = SimpleNamespace(id=102, name="Segunda")
    empty_category = SimpleNamespace(id=103, name="Vazia")
    uncategorized = SimpleNamespace(id=210, name="topo", type="text", category_id=None)
    first_channel = SimpleNamespace(id=211, name="primeiro", type="text", category_id=101)
    second_channel = SimpleNamespace(id=212, name="segundo", type="text", category_id=102)

    # Deliberately keep the raw channel caches in a different order.
    # by_category() is authoritative for channel UI order, while categories
    # must still come from guild.categories so empty categories are retained.
    guild.categories = [second_category, empty_category, first_category]
    guild.channels = [second_channel, first_channel, uncategorized]
    guild.text_channels = [second_channel, first_channel, uncategorized]
    guild.by_category = Mock(return_value=[
        (None, [uncategorized]),
        (first_category, [first_channel]),
        (second_category, [second_channel]),
    ])

    resources = build_guild_resources(guild)

    assert [channel["id"] for channel in resources["channels"]] == ["210", "211", "212"]
    assert [category["id"] for category in resources["categories"]] == ["102", "103", "101"]
    guild.by_category.assert_called_once_with()


def test_bot_capabilities_report_missing_permissions():
    resources = build_guild_resources(FakeGuild(manage_channels=False, manage_roles=False))
    assert not resources["botCapabilities"]["canApply"]
    assert resources["botCapabilities"]["missingPermissions"] == ["MANAGE_CHANNELS", "MANAGE_ROLES"]


@pytest.mark.parametrize(("administrator", "top_position", "expected_hierarchy"), [
    (True, 40, True),
    (True, 20, False),
    (False, 40, True),
    (False, 20, False),
])
def test_protection_readiness_requires_administrator_and_role_hierarchy(administrator, top_position, expected_hierarchy):
    resources = build_guild_resources(FakeGuild(administrator=administrator, top_position=top_position))
    capabilities = resources["botCapabilities"]

    assert capabilities["hasAdministrator"] is administrator
    assert capabilities["roleHierarchyOk"] is expected_hierarchy
    assert capabilities["protectionReady"] is (administrator and expected_hierarchy)
    assert capabilities["botTopRole"] == {"id": "15", "name": "Coddy", "position": top_position}
    assert [role["name"] for role in capabilities["rolesAboveBot"]] == ([] if expected_hierarchy else ["Acima do Coddy"])


def test_protection_hierarchy_ignores_everyone_and_own_top_role_but_keeps_managed_blockers():
    guild = FakeGuild(administrator=True, top_position=20)
    guild.roles.append(FakeRole(40, "Integração", 30, managed=True))
    resources = build_guild_resources(guild)

    assert [role["name"] for role in resources["botCapabilities"]["rolesAboveBot"]] == ["Integração", "Acima do Coddy"]
    assert resources["botCapabilities"]["rolesAboveBot"][0]["managed"] is True


def test_portaria_preview_includes_isolation_and_hierarchy_warning_without_mutations():
    guild = FakeGuild()
    before = (list(guild.roles), list(guild.categories), list(guild.text_channels))
    preview = build_structure_preview(guild, {
        "type": "portaria",
        "visitorRoleName": "Visitante",
        "categoryName": "Portaria",
        "channelNames": ["boas-vindas", "fichas", "aprovados", "reprovados"],
        "staffRoleIds": [10, 20],
        "isolateVisitors": True,
    })
    assert preview["creates"]["roles"] == [{"name": "Visitante"}]
    assert len(preview["creates"]["channels"]) == 4
    assert preview["affectedExistingResources"] == [
        {"id": "100", "name": "Geral", "type": "category"},
        {"id": "200", "name": "geral", "type": "text"},
        {"id": "201", "name": "Voz livre", "type": "voice"},
        {"id": "202", "name": "forum", "type": "forum"},
        {"id": "203", "name": "palco", "type": "stage_voice"},
    ]
    assert len({item["id"] for item in preview["affectedExistingResources"]}) == 5
    for name in ("boas-vindas", "fichas", "aprovados", "reprovados"):
        assert any(
            change["target"]["name"] == name
            and change["subject"] == {"kind": "everyone"}
            and change["effect"] == "deny"
            for change in preview["permissionChanges"]
        )
    assert any(change["target"]["name"] == "boas-vindas" and change["subject"] == {"kind": "planned-role", "name": "Visitante"} and change["effect"] == "allow" for change in preview["permissionChanges"])
    for name in ("fichas", "aprovados", "reprovados"):
        assert any(change["target"]["name"] == name and change["subject"] == {"kind": "planned-role", "name": "Visitante"} and change["effect"] == "deny" for change in preview["permissionChanges"])
        assert any(change["target"]["name"] == name and change["subject"] == {"kind": "role", "id": "10"} and change["effect"] == "allow" for change in preview["permissionChanges"])
    assert preview["warnings"] == ["O cargo Acima do Coddy está acima do cargo do Coddy ou não pode ser editado."]
    assert before == (guild.roles, guild.categories, guild.text_channels)


def test_roles_with_equal_positions_follow_discord_role_hierarchy():
    guild = FakeGuild()
    guild.roles.extend([FakeRole(30, "Empate baixo", 8), FakeRole(40, "Empate alto", 8)])
    resources = build_guild_resources(guild)
    assert [role["id"] for role in resources["roles"]] == ["20", "15", "40", "30", "10"]


def test_private_area_uses_the_same_preview_model_without_mutations():
    guild = FakeGuild()
    preview = build_structure_preview(guild, {
        "type": "private-area",
        "categoryName": "Staff",
        "channelNames": ["staff", "avisos-staff", "logs"],
        "allowedRoleIds": [10],
    })
    assert preview["type"] == "private-area"
    assert preview["creates"]["categories"] == [{"name": "Staff"}]
    assert preview["permissionChanges"][0]["subject"] == {"kind": "everyone"}
    assert not any(hasattr(guild, name) for name in ("create_role", "create_category", "create_text_channel"))


def test_structure_preview_rejects_non_object_json_with_400():
    api = BotStatusApi(SimpleNamespace(), host="127.0.0.1", port=8080)
    api._authorized_guild = lambda request: (FakeGuild(), None)
    for payload in ([], None, "text", 42):
        request = SimpleNamespace(json=AsyncMock(return_value=payload))
        response = asyncio.run(api._handle_structure_preview(request))
        assert response.status == 400
        assert json.loads(response.body) == {"error": "invalid_preview_request"}


def test_operation_is_fail_closed_without_internal_token():
    api = BotStatusApi(SimpleNamespace(), host="127.0.0.1", port=8080, token=None)
    authorized_guild = Mock()
    api._authorized_guild = authorized_guild
    request = SimpleNamespace(json=AsyncMock(return_value={"operation": "private-area"}))

    with patch("message_services.bot_status_api.apply_guild_operation", new_callable=AsyncMock) as apply:
        response = asyncio.run(api._handle_guild_operation(request))

    assert response.status == 503
    assert "operation_auth_not_configured" in response.text
    authorized_guild.assert_not_called()
    apply.assert_not_awaited()


@pytest.mark.parametrize(("handler_name", "fake_request"), [
    ("_handle_auto_join_add", SimpleNamespace(match_info={"role_id": "10"})),
    ("_handle_auto_join_delete", SimpleNamespace(match_info={"role_id": "10"})),
    ("_handle_auto_join_enabled", SimpleNamespace(json=AsyncMock(return_value={"enabled": False}))),
])
def test_auto_join_mutations_fail_closed_without_internal_token(handler_name, fake_request):
    api = BotStatusApi(SimpleNamespace(), host="127.0.0.1", port=8080, token=None)
    authorized_guild = Mock()
    api._authorized_guild = authorized_guild

    with patch("message_services.bot_status_api.add_auto_join_role") as add, \
         patch("message_services.bot_status_api.remove_auto_join_role") as remove, \
         patch("message_services.bot_status_api.set_auto_join_enabled") as enabled, \
         patch("core.auto_join_roles._write_auto_join_config") as persist:
        response = asyncio.run(getattr(api, handler_name)(fake_request))

    assert response.status == 503
    assert "operation_auth_not_configured" in response.text
    authorized_guild.assert_not_called()
    add.assert_not_called(); remove.assert_not_called(); enabled.assert_not_called(); persist.assert_not_called()


@pytest.mark.parametrize("path", [
    "/guilds/1/auto-join-roles/10/2",
    "/guilds/1/auto-join-roles/2",
])
def test_auto_join_mutation_middleware_rejects_invalid_token_without_calling_handler(path):
    middleware = _build_auth_middleware("internal-token")
    handler = AsyncMock()
    request = SimpleNamespace(path=path, headers={"Authorization": "Bearer wrong-token"})
    response = asyncio.run(middleware(request, handler))
    assert response.status == 401
    handler.assert_not_awaited()


def test_auto_join_mutation_middleware_allows_valid_token():
    middleware = _build_auth_middleware("internal-token")
    handler = AsyncMock(return_value=SimpleNamespace(status=200))
    request = SimpleNamespace(path="/guilds/1/auto-join-roles/10/2", headers={"Authorization": "Bearer internal-token"})
    response = asyncio.run(middleware(request, handler))
    assert response.status == 200
    handler.assert_awaited_once_with(request)


def test_auto_join_enabled_route_precedes_the_generic_add_route():
    api = BotStatusApi(SimpleNamespace(), host="127.0.0.1", port=8080, token="internal-token")
    captured = {}

    class Runner:
        def __init__(self, app, **_kwargs): captured["app"] = app
        async def setup(self): pass
    class Site:
        def __init__(self, *_args, **_kwargs): pass
        async def start(self): pass

    with patch("message_services.bot_status_api.web.AppRunner", Runner), patch("message_services.bot_status_api.web.TCPSite", Site):
        asyncio.run(api.start())

    app = captured["app"]
    enabled = asyncio.run(app.router.resolve(make_mocked_request("POST", "/guilds/123/auto-join-roles/456/enabled", app=app)))
    add = asyncio.run(app.router.resolve(make_mocked_request("POST", "/guilds/123/auto-join-roles/789/456", app=app)))
    assert enabled.handler == api._handle_auto_join_enabled
    assert dict(enabled) == {"guild_id": "123", "discord_user_id": "456"}
    assert add.handler == api._handle_auto_join_add
    assert dict(add) == {"guild_id": "123", "role_id": "789", "discord_user_id": "456"}


@pytest.mark.parametrize(("handler_name", "fake_request", "mutation_name"), [
    ("_handle_auto_join_add", SimpleNamespace(match_info={"role_id": "10"}), "add_auto_join_role"),
    ("_handle_auto_join_delete", SimpleNamespace(match_info={"role_id": "10"}), "remove_auto_join_role"),
    ("_handle_auto_join_enabled", SimpleNamespace(json=AsyncMock(return_value={"enabled": True})), "set_auto_join_enabled"),
])
def test_auto_join_mutations_allow_configured_authenticated_requests(handler_name, fake_request, mutation_name):
    api = BotStatusApi(SimpleNamespace(), host="127.0.0.1", port=8080, token="internal-token")
    guild = FakeGuild()
    api._authorized_guild = Mock(return_value=(guild, None))
    with patch(f"message_services.bot_status_api.{mutation_name}", return_value={"enabled": True, "roleIds": [10]}) as mutation:
        response = asyncio.run(getattr(api, handler_name)(fake_request))
    assert response.status == 200
    api._authorized_guild.assert_called_once_with(fake_request)
    mutation.assert_called_once()


def test_visitor_role_never_reuses_everyone_or_managed_role():
    default = FakeRole(1, "@everyone", 0)
    managed = FakeRole(2, "Visitante", 1, managed=True)
    guild = SimpleNamespace(default_role=default, roles=[default, managed], create_role=AsyncMock())

    with pytest.raises(ValueError, match="invalid_visitor_role_name"):
        asyncio.run(_ensure_role(guild, "@everyone"))

    asyncio.run(_ensure_role(guild, "Visitante"))
    guild.create_role.assert_awaited_once()


def test_portaria_account_bypass_validation_is_scoped_to_live_guild_member():
    member = SimpleNamespace(id=555, display_name="Member")
    guild = SimpleNamespace(
        get_member=lambda user_id: member if user_id == 555 else None,
        fetch_member=AsyncMock(),
    )

    result = asyncio.run(_validate_portaria_bypass_target(
        guild,
        {"bypassType": "account", "value": "555"},
    ))

    assert result == {
        "bypassType": "account",
        "value": "555",
        "displayName": "Member",
    }
    guild.fetch_member.assert_not_awaited()


def test_portaria_account_bypass_rejects_user_not_in_current_guild(monkeypatch):
    class NotFound(Exception):
        pass

    monkeypatch.setattr(guild_management.discord, "NotFound", NotFound)
    monkeypatch.setattr(guild_management.discord, "Forbidden", NotFound)
    monkeypatch.setattr(guild_management.discord, "HTTPException", NotFound)
    guild = SimpleNamespace(
        get_member=lambda _user_id: None,
        fetch_member=AsyncMock(side_effect=NotFound()),
    )

    with pytest.raises(ValueError, match="portaria_bypass_account_not_in_guild"):
        asyncio.run(_validate_portaria_bypass_target(
            guild,
            {"bypassType": "account", "value": "999"},
        ))


def test_portaria_account_bypass_preserves_transient_discord_failure(monkeypatch):
    class TransientError(Exception):
        pass

    monkeypatch.setattr(guild_management.discord, "HTTPException", TransientError)
    guild = SimpleNamespace(
        get_member=lambda _user_id: None,
        fetch_member=AsyncMock(side_effect=TransientError("temporary")),
    )

    with pytest.raises(TransientError, match="temporary"):
        asyncio.run(_validate_portaria_bypass_target(
            guild,
            {"bypassType": "account", "value": "999"},
        ))


def test_portaria_invite_bypass_preserves_transient_invite_listing_failure(monkeypatch):
    class TransientError(Exception):
        pass

    monkeypatch.setattr(guild_management.discord, "HTTPException", TransientError)
    monkeypatch.setattr(
        "core.database.normalize_discord_invite_code",
        lambda value: str(value).strip() if value else None,
    )
    guild = SimpleNamespace(
        invites=AsyncMock(side_effect=TransientError("temporary")),
    )

    with pytest.raises(TransientError, match="temporary"):
        asyncio.run(_validate_portaria_bypass_target(
            guild,
            {"bypassType": "invite", "value": "CaseSensitive"},
        ))


def test_portaria_invite_bypass_accepts_only_invite_owned_by_current_guild(monkeypatch):
    monkeypatch.setattr(
        "core.database.normalize_discord_invite_code",
        lambda value: str(value).strip().lower() if value else None,
    )
    guild = SimpleNamespace(
        me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True)),
        invites=AsyncMock(return_value=[
            SimpleNamespace(code="guild-code"),
            SimpleNamespace(code="other-code"),
        ]),
    )

    result = asyncio.run(_validate_portaria_bypass_target(
        guild,
        {"bypassType": "invite", "value": "GUILD-CODE"},
    ))
    assert result["value"] == "guild-code"

    with pytest.raises(ValueError, match="portaria_bypass_invite_not_in_guild"):
        asyncio.run(_validate_portaria_bypass_target(
            guild,
            {"bypassType": "invite", "value": "foreign-code"},
        ))
