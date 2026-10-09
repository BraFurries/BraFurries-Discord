import asyncio
import io
import json
import logging
import sys
from types import ModuleType, SimpleNamespace

import pytest
from unittest.mock import AsyncMock
from aiohttp.test_utils import make_mocked_request

from core.recent_logs import RecentLogBufferHandler
from core.runtime_metrics import RuntimeMetrics
from message_services.bot_status_api import (
    BotStatusApi,
    StatusApiAccessLogger,
    _build_auth_middleware,
    access_logger,
)


class FakeBot:
    def __init__(self, *, ready: bool):
        self._ready = ready
        self.ready_calls = 0

    def is_ready(self):
        self.ready_calls += 1
        return self._ready


async def _ok_handler(request):
    return request


class JsonRequest:
    def __init__(self, guild_id: int, payload: dict):
        self.match_info = {'guild_id': str(guild_id)}
        self._payload = payload

    async def json(self):
        return self._payload


class GuildAdminJsonRequest:
    def __init__(self, guild_id: int, discord_user_id: int, payload: dict | None = None):
        self.match_info = {
            'guild_id': str(guild_id),
            'discord_user_id': str(discord_user_id),
        }
        self._payload = payload or {}

    async def json(self):
        return self._payload


class CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class CapturingStreamHandler(logging.StreamHandler):
    def __init__(self):
        super().__init__(sys.stderr)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _emit_status_access(path: str, status: int) -> None:
    StatusApiAccessLogger(access_logger, '').log(
        SimpleNamespace(method='GET', path=path),
        SimpleNamespace(status=status, body_length=42),
        0.125,
    )


@pytest.mark.parametrize(('path', 'status', 'expected_level'), [
    ('/live', 200, logging.DEBUG),
    ('/ready', 200, logging.DEBUG),
    ('/status', 200, logging.DEBUG),
    ('/health', 200, logging.DEBUG),
    ('/logs', 200, logging.DEBUG),
    ('/ready', 503, logging.DEBUG),
    ('/health', 503, logging.WARNING),
    ('/status', 401, logging.WARNING),
    ('/status', 500, logging.ERROR),
])
def test_status_api_access_logs_have_outcome_appropriate_levels(path, status, expected_level):
    capture = CapturingHandler()
    original_handlers = list(access_logger.handlers)
    original_level = access_logger.level
    original_propagate = access_logger.propagate
    try:
        access_logger.handlers.clear()
        access_logger.setLevel(logging.DEBUG)
        access_logger.propagate = False
        access_logger.addHandler(capture)

        StatusApiAccessLogger(access_logger, '').log(
            SimpleNamespace(method='GET', path=path, headers={'Authorization': 'Bearer secret-token'}),
            SimpleNamespace(status=status, body_length=42),
            0.125,
        )

        assert len(capture.records) == 1
        record = capture.records[0]
        assert record.levelno == expected_level
        assert record.getMessage() == f'GET {path} {status} completed in 0.125s (42 bytes)'
        assert 'secret-token' not in record.getMessage()
        assert 'Authorization' not in record.getMessage()
    finally:
        access_logger.handlers[:] = original_handlers
        access_logger.setLevel(original_level)
        access_logger.propagate = original_propagate


def test_identity_ban_propagation_endpoint_extends_only_missing_effects(monkeypatch):
    guild = SimpleNamespace(id=555)
    bot = FakeBot(ready=True)
    bot.get_guild = lambda guild_id: guild if guild_id == 555 else None
    api = BotStatusApi(
        bot,
        host='127.0.0.1',
        port=8080,
        token='internal-token',
    )

    database_module = ModuleType('core.database')
    database_module.banBelongsToGuild = lambda ban_id, guild_id: (
        ban_id == 77 and guild_id == 555
    )
    database_module.getSatisfiedBanEffectDiscordIds = lambda ban_id: {111}
    persisted = []
    database_module.recordBanDiscordEffects = lambda ban_id, effects: (
        persisted.append((ban_id, effects)) or True
    )

    identity_module = ModuleType('core.identity_bans')
    effect = SimpleNamespace(
        identity_user_id=20,
        discord_user_id=222,
        is_origin=False,
        outcome='APPLIED',
        error_code=None,
    )
    propagate = AsyncMock(return_value=[effect])
    identity_module.propagate_new_confirmed_identity_ban = propagate
    identity_module.compensate_unrecorded_propagated_bans = AsyncMock(return_value=[])
    identity_module.summarize_ban_effects = lambda effects: {'APPLIED': len(effects)}

    events_module = ModuleType('core.discord_events')
    log_propagation = AsyncMock()
    events_module.logIdentityBanPropagation = log_propagation

    monkeypatch.setitem(sys.modules, 'core.database', database_module)
    monkeypatch.setitem(sys.modules, 'core.identity_bans', identity_module)
    monkeypatch.setitem(sys.modules, 'core.discord_events', events_module)

    response = asyncio.run(api._handle_identity_ban_propagation(JsonRequest(555, {
        'banId': 77,
        'reason': 'ban ativo',
        'identities': [
            {'userId': 10, 'discordUserId': '111'},
            {'userId': 20, 'discordUserId': '222'},
        ],
    })))

    assert response.status == 200
    assert json.loads(response.text) == {
        'banId': 77,
        'processed': 1,
        'effects': {'APPLIED': 1},
    }
    propagate.assert_awaited_once_with(
        guild,
        [(20, 222)],
        reason='ban ativo',
    )
    assert persisted == [(77, [effect])]
    log_propagation.assert_awaited_once()


def test_identity_ban_propagation_endpoint_rejects_cross_tenant_ban(monkeypatch):
    guild = SimpleNamespace(id=555)
    bot = FakeBot(ready=True)
    bot.get_guild = lambda guild_id: guild
    api = BotStatusApi(
        bot,
        host='127.0.0.1',
        port=8080,
        token='internal-token',
    )

    database_module = ModuleType('core.database')
    database_module.banBelongsToGuild = lambda _ban_id, _guild_id: False
    database_module.getSatisfiedBanEffectDiscordIds = lambda _ban_id: set()
    database_module.recordBanDiscordEffects = lambda _ban_id, _effects: True
    identity_module = ModuleType('core.identity_bans')
    identity_module.propagate_new_confirmed_identity_ban = AsyncMock()
    identity_module.compensate_unrecorded_propagated_bans = AsyncMock()
    identity_module.summarize_ban_effects = lambda _effects: {}
    events_module = ModuleType('core.discord_events')
    events_module.logIdentityBanPropagation = AsyncMock()

    monkeypatch.setitem(sys.modules, 'core.database', database_module)
    monkeypatch.setitem(sys.modules, 'core.identity_bans', identity_module)
    monkeypatch.setitem(sys.modules, 'core.discord_events', events_module)

    response = asyncio.run(api._handle_identity_ban_propagation(JsonRequest(555, {
        'banId': 77,
        'reason': 'ban ativo',
        'identities': [{'userId': 20, 'discordUserId': '222'}],
    })))

    assert response.status == 404
    assert json.loads(response.text)['error'] == 'ban_not_found_for_guild'
    identity_module.propagate_new_confirmed_identity_ban.assert_not_awaited()


def test_xp_simulation_requires_real_guild_management_access(monkeypatch):
    guild = SimpleNamespace(id=555)
    bot = SimpleNamespace(
        is_ready=lambda: True,
        get_guild=lambda guild_id: guild if guild_id == 555 else None,
    )
    api = BotStatusApi(
        bot,
        host='127.0.0.1',
        port=8080,
        token='internal-token',
        initialized_getter=lambda: True,
    )
    monkeypatch.setattr('message_services.bot_status_api.can_manage_guild', lambda _guild, _user_id: False)
    simulate = AsyncMock(return_value={'guildId': '555', 'points': []})
    monkeypatch.setattr('message_services.bot_status_api.simulate_xp_runtime', simulate)

    response = asyncio.run(
        api._handle_xp_simulation(
            GuildAdminJsonRequest(555, 42, {'config': {}, 'levels': [1]})
        )
    )

    assert response.status == 403
    simulate.assert_not_awaited()


def test_xp_simulation_uses_authorized_canonical_runtime(monkeypatch):
    guild = SimpleNamespace(id=555)
    bot = SimpleNamespace(
        is_ready=lambda: True,
        get_guild=lambda guild_id: guild if guild_id == 555 else None,
    )
    api = BotStatusApi(
        bot,
        host='127.0.0.1',
        port=8080,
        token='internal-token',
        initialized_getter=lambda: True,
    )
    monkeypatch.setattr('message_services.bot_status_api.can_manage_guild', lambda _guild, _user_id: True)
    simulate = AsyncMock(
        return_value={
            'guildId': '555',
            'points': [{'level': 10, 'totalXp': 4500, 'xpFromPreviousLevel': 855}],
            'curve': {'phase1K': '45', 'phase1P': '2', 'phase1B': '0'},
        }
    )
    monkeypatch.setattr('message_services.bot_status_api.simulate_xp_runtime', simulate)
    payload = {'config': {'phase1_k': '45'}, 'levels': [10]}

    response = asyncio.run(
        api._handle_xp_simulation(GuildAdminJsonRequest(555, 42, payload))
    )

    assert response.status == 200
    assert json.loads(response.text)['points'][0]['totalXp'] == 4500
    simulate.assert_awaited_once_with(555, payload)


def test_xp_runtime_refresh_fails_closed_without_internal_token(monkeypatch):
    guild = SimpleNamespace(id=555)
    bot = SimpleNamespace(is_ready=lambda: True, get_guild=lambda _guild_id: guild)
    api = BotStatusApi(
        bot,
        host='127.0.0.1',
        port=8080,
        initialized_getter=lambda: True,
    )
    refresh = AsyncMock()
    monkeypatch.setattr('message_services.bot_status_api.refresh_xp_runtime', refresh)

    response = asyncio.run(
        api._handle_xp_runtime_refresh(GuildAdminJsonRequest(555, 42))
    )

    assert response.status == 503
    assert json.loads(response.text)['error'] == 'xp_runtime_auth_not_configured'
    refresh.assert_not_awaited()


def test_status_api_access_logger_does_not_modify_global_aiohttp_access_logger():
    aiohttp_access_logger = logging.getLogger('aiohttp.access')
    original_handlers = list(aiohttp_access_logger.handlers)
    original_level = aiohttp_access_logger.level
    original_propagate = aiohttp_access_logger.propagate

    StatusApiAccessLogger(access_logger, '').log(
        SimpleNamespace(method='GET', path='/live'),
        SimpleNamespace(status=200, body_length=42),
        0.125,
    )

    assert aiohttp_access_logger.handlers == original_handlers
    assert aiohttp_access_logger.level == original_level
    assert aiohttp_access_logger.propagate == original_propagate


def test_status_api_access_debug_reaches_buffer_without_reaching_normal_stream(capsys):
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    original_access_handlers = list(access_logger.handlers)
    original_access_level = access_logger.level
    original_access_propagate = access_logger.propagate
    handler = RecentLogBufferHandler()
    stream_handler = CapturingStreamHandler()
    stream_handler.setLevel(logging.INFO)
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)
    try:
        root_logger.handlers.clear()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(stream_handler)
        access_logger.handlers.clear()
        access_logger.setLevel(logging.DEBUG)
        access_logger.propagate = False

        api._attach_log_handler()
        api._attach_log_handler()
        _emit_status_access('/live', 200)
        access_logger.info('access info')
        _emit_status_access('/health', 503)
        _emit_status_access('/status', 500)

        access_items = [item for item in handler.items() if item['logger'] == access_logger.name]
        assert [item['level'] for item in access_items] == ['DEBUG', 'INFO', 'WARNING', 'ERROR']
        assert len(access_items) == 4
        assert [record for record in stream_handler.records if record.name == access_logger.name] == []
        stderr = capsys.readouterr().err
        assert stderr.count('access info') == 1
        assert stderr.count('GET /health 503 completed in 0.125s (42 bytes)') == 1
        assert stderr.count('GET /status 500 completed in 0.125s (42 bytes)') == 1
        assert 'GET /live 200 completed in 0.125s (42 bytes)' not in stderr
        assert root_logger.level == logging.INFO
        assert access_logger.propagate is False
        assert access_logger.handlers.count(handler) == 1
        assert api._owned_access_stream_handler in access_logger.handlers
        assert api._owned_access_stream_handler.level == logging.INFO

        api._detach_log_handler()
        api._detach_log_handler()
        assert handler not in access_logger.handlers
        assert stream_handler in root_logger.handlers
    finally:
        api._detach_log_handler()
        access_logger.handlers[:] = original_access_handlers
        access_logger.setLevel(original_access_level)
        access_logger.propagate = original_access_propagate
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)


def test_status_api_access_logs_follow_minimum_level_filters():
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    original_access_handlers = list(access_logger.handlers)
    handler = RecentLogBufferHandler()
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)
    try:
        root_logger.handlers.clear()
        root_logger.setLevel(logging.INFO)
        access_logger.handlers.clear()
        api._attach_log_handler()

        _emit_status_access('/live', 200)
        _emit_status_access('/ready', 503)
        access_logger.info('access info')
        _emit_status_access('/health', 503)
        _emit_status_access('/status', 500)
        access_logger.critical('access critical')

        def messages(level: str) -> list[str]:
            response = asyncio.run(api._handle_logs(make_mocked_request('GET', f'/logs?level={level}')))
            return [item['message'] for item in json.loads(response.text)['items']]

        debug_messages = messages('DEBUG')
        assert 'GET /live 200 completed in 0.125s (42 bytes)' in debug_messages
        assert 'GET /ready 503 completed in 0.125s (42 bytes)' in debug_messages
        assert 'GET /health 503 completed in 0.125s (42 bytes)' in debug_messages
        assert 'access info' in debug_messages
        assert 'GET /status 500 completed in 0.125s (42 bytes)' in debug_messages
        assert 'access critical' in debug_messages

        info_messages = messages('INFO')
        assert 'GET /live 200 completed in 0.125s (42 bytes)' not in info_messages
        assert 'GET /ready 503 completed in 0.125s (42 bytes)' not in info_messages
        assert 'access info' in info_messages
        assert 'GET /health 503 completed in 0.125s (42 bytes)' in info_messages
        assert 'GET /health 503 completed in 0.125s (42 bytes)' in messages('WARNING')
    finally:
        api._detach_log_handler()
        access_logger.handlers[:] = original_access_handlers
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)


def test_auth_middleware_allows_live_and_ready_without_token():
    middleware = _build_auth_middleware("secret")

    for path in ("/live", "/ready"):
        request = make_mocked_request("GET", path)

        assert asyncio.run(middleware(request, _ok_handler)) is request


def test_auth_middleware_protects_status_with_token():
    middleware = _build_auth_middleware("secret")
    request = make_mocked_request("GET", "/status")

    response = asyncio.run(middleware(request, _ok_handler))

    assert response.status == 401


def test_auth_middleware_protects_logs_with_token():
    middleware = _build_auth_middleware("secret")
    request = make_mocked_request("GET", "/logs")

    response = asyncio.run(middleware(request, _ok_handler))

    assert response.status == 401

    authorized = make_mocked_request("GET", "/logs", headers={"Authorization": "Bearer secret"})
    assert asyncio.run(middleware(authorized, _ok_handler)) is authorized


def test_auth_middleware_protects_managed_guilds_with_token():
    middleware = _build_auth_middleware("secret")
    request = make_mocked_request("GET", "/managed-guilds/123")

    response = asyncio.run(middleware(request, _ok_handler))

    assert response.status == 401


def test_managed_guilds_returns_only_owner_and_manage_guild_memberships():
    def guild(guild_id, owner_id, name, member_count, member, icon_url=None):
        return SimpleNamespace(
            id=guild_id,
            owner_id=owner_id,
            name=name,
            member_count=member_count,
            icon=SimpleNamespace(url=icon_url) if icon_url else None,
            get_member=lambda user_id: member if user_id == 42 else None,
        )

    owner = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False, manage_guild=False))
    administrator = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True, manage_guild=False))
    manager = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False, manage_guild=True))
    member = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False, manage_guild=False))
    bot = SimpleNamespace(
        guilds=[
            guild(30, 99, 'Not allowed', 30, member),
            guild(20, 99, 'Administrator', 20, administrator),
            guild(10, 42, 'Owner', 10, owner, 'https://cdn.discordapp.com/icons/10/icon.png'),
            guild(40, 99, 'Manager', None, manager),
        ],
        is_ready=lambda: True,
    )
    api = BotStatusApi(bot, host='127.0.0.1', port=8080, initialized_getter=lambda: True)
    request = make_mocked_request('GET', '/managed-guilds/42', match_info={'discord_user_id': '42'})

    response = asyncio.run(api._handle_managed_guilds(request))

    assert response.status == 200
    assert json.loads(response.text) == {
        'guilds': [
            {'guild_id': '10', 'name': 'Owner', 'member_count': 10, 'icon_url': 'https://cdn.discordapp.com/icons/10/icon.png'},
            {'guild_id': '20', 'name': 'Administrator', 'member_count': 20, 'icon_url': None},
            {'guild_id': '40', 'name': 'Manager', 'member_count': None, 'icon_url': None},
        ]
    }


def test_managed_guilds_returns_empty_when_user_has_no_eligible_guilds():
    bot = SimpleNamespace(
        guilds=[
            SimpleNamespace(
                id=10,
                owner_id=1,
                name='Member only',
                member_count=5,
                get_member=lambda user_id: SimpleNamespace(
                    guild_permissions=SimpleNamespace(administrator=False, manage_guild=False)
                ),
            )
        ],
        is_ready=lambda: True,
    )
    api = BotStatusApi(bot, host='127.0.0.1', port=8080, initialized_getter=lambda: True)
    request = make_mocked_request('GET', '/managed-guilds/42', match_info={'discord_user_id': '42'})

    response = asyncio.run(api._handle_managed_guilds(request))

    assert response.status == 200
    assert json.loads(response.text) == {'guilds': []}


def test_managed_guilds_fails_closed_when_bot_is_not_ready():
    api = BotStatusApi(FakeBot(ready=False), host='127.0.0.1', port=8080, initialized_getter=lambda: True)
    request = make_mocked_request('GET', '/managed-guilds/42', match_info={'discord_user_id': '42'})

    response = asyncio.run(api._handle_managed_guilds(request))

    assert response.status == 503


def test_guild_owner_returns_live_discord_owner():
    bot = SimpleNamespace(
        is_ready=lambda: True,
        get_guild=lambda guild_id: SimpleNamespace(id=10, owner_id=42) if guild_id == 10 else None,
    )
    api = BotStatusApi(
        bot,
        host='127.0.0.1',
        port=8080,
        token='internal-token',
        initialized_getter=lambda: True,
    )
    request = make_mocked_request('GET', '/guilds/10/owner', match_info={'guild_id': '10'})

    response = asyncio.run(api._handle_guild_owner(request))

    assert response.status == 200
    assert json.loads(response.text) == {'guildId': '10', 'ownerId': '42'}


def test_guild_owner_fails_closed_without_internal_token():
    bot = SimpleNamespace(
        is_ready=lambda: True,
        get_guild=lambda guild_id: SimpleNamespace(id=guild_id, owner_id=42),
    )
    api = BotStatusApi(bot, host='127.0.0.1', port=8080, initialized_getter=lambda: True)
    request = make_mocked_request('GET', '/guilds/10/owner', match_info={'guild_id': '10'})

    response = asyncio.run(api._handle_guild_owner(request))

    assert response.status == 503
    assert json.loads(response.text)['error'] == 'guild_owner_auth_not_configured'


def test_guild_owner_fails_closed_when_bot_not_ready():
    bot = SimpleNamespace(is_ready=lambda: False, get_guild=lambda _guild_id: None)
    api = BotStatusApi(
        bot,
        host='127.0.0.1',
        port=8080,
        token='internal-token',
        initialized_getter=lambda: True,
    )
    request = make_mocked_request('GET', '/guilds/10/owner', match_info={'guild_id': '10'})

    response = asyncio.run(api._handle_guild_owner(request))

    assert response.status == 503
    assert json.loads(response.text)['error'] == 'bot_not_ready'


def test_guild_owner_returns_not_found_for_unknown_guild():
    bot = SimpleNamespace(is_ready=lambda: True, get_guild=lambda _guild_id: None)
    api = BotStatusApi(
        bot,
        host='127.0.0.1',
        port=8080,
        token='internal-token',
        initialized_getter=lambda: True,
    )
    request = make_mocked_request('GET', '/guilds/10/owner', match_info={'guild_id': '10'})

    response = asyncio.run(api._handle_guild_owner(request))

    assert response.status == 404
    assert json.loads(response.text)['error'] == 'guild_not_found'


def test_live_returns_ok_without_checking_discord_ready():
    request = make_mocked_request("GET", "/live")
    bot = FakeBot(ready=False)
    api = BotStatusApi(bot, host="127.0.0.1", port=8080)

    response = asyncio.run(api._handle_live(request))

    assert response.status == 200
    assert response.text == '{"ok": true, "state": "live"}'
    assert bot.ready_calls == 0


def test_ready_returns_unavailable_when_not_initialized_even_if_discord_ready():
    request = make_mocked_request("GET", "/ready")
    api = BotStatusApi(
        FakeBot(ready=True),
        host="127.0.0.1",
        port=8080,
        initialized_getter=lambda: False,
    )

    response = asyncio.run(api._handle_ready(request))

    assert response.status == 503


def test_ready_returns_unavailable_when_initialized_but_discord_not_ready():
    request = make_mocked_request("GET", "/ready")
    api = BotStatusApi(
        FakeBot(ready=False),
        host="127.0.0.1",
        port=8080,
        initialized_getter=lambda: True,
    )

    response = asyncio.run(api._handle_ready(request))

    assert response.status == 503


def test_ready_returns_ok_when_initialized_and_discord_ready():
    request = make_mocked_request("GET", "/ready")
    api = BotStatusApi(
        FakeBot(ready=True),
        host="127.0.0.1",
        port=8080,
        initialized_getter=lambda: True,
    )

    response = asyncio.run(api._handle_ready(request))

    assert response.status == 200


def test_logs_uses_default_and_maximum_limit_and_filters_levels():
    handler = RecentLogBufferHandler()
    logger = logging.getLogger('tests.status-api.logs')
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        logger.info('first')
        logger.warning('second')
        api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)

        response = asyncio.run(api._handle_logs(make_mocked_request('GET', '/logs')))
        payload = json.loads(response.text)
        assert payload['limit'] == 200
        assert payload['totalBuffered'] == 2
        assert [item['message'] for item in payload['items']] == ['first', 'second']

        response = asyncio.run(api._handle_logs(make_mocked_request('GET', '/logs?limit=500&level=WARNING')))
        payload = json.loads(response.text)
        assert payload['limit'] == 500
        assert [item['message'] for item in payload['items']] == ['second']

        response = asyncio.run(api._handle_logs(make_mocked_request('GET', '/logs?level=WARNING:exact')))
        payload = json.loads(response.text)
        assert [item['message'] for item in payload['items']] == ['second']
    finally:
        logger.removeHandler(handler)


def test_logs_rejects_limit_above_maximum():
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080)

    response = asyncio.run(api._handle_logs(make_mocked_request('GET', '/logs?limit=1001')))

    assert response.status == 400


@pytest.mark.parametrize('limit', [0, 1001])
def test_logs_rejects_limits_outside_supported_range(limit):
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080)
    response = asyncio.run(api._handle_logs(make_mocked_request('GET', f'/logs?limit={limit}')))
    assert response.status == 400


def test_default_admin_buffer_retains_ten_thousand_records():
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080)
    record = logging.LogRecord('capacity', logging.INFO, __file__, 0, 'log', (), None)
    for _ in range(10005):
        api._log_handler.handle(record)
    assert api._log_handler.total_buffered == 10000
    assert api._log_handler.oldest_sequence == 6


def test_full_history_and_incremental_queries_filter_before_applying_limit():
    handler = RecentLogBufferHandler()
    for sequence in range(1, 31):
        severity = logging.INFO if sequence % 3 == 0 else logging.DEBUG
        handler.handle(logging.LogRecord('filter-first', severity, __file__, 0, str(sequence), (), None))
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)

    def fetch(query):
        response = asyncio.run(api._handle_logs(make_mocked_request('GET', f'/logs?{query}')))
        assert response.status == 200
        return json.loads(response.text)

    assert [item['sequence'] for item in fetch('level=INFO&limit=3')['items']] == [24, 27, 30]
    assert [item['sequence'] for item in fetch('level=INFO&before=24&limit=3')['items']] == [15, 18, 21]
    assert [item['sequence'] for item in fetch('level=INFO&after=21&limit=3')['items']] == [24, 27, 30]
    assert [item['sequence'] for item in fetch('level=DEBUG&limit=1000')['items']] == list(range(1, 31))


def test_before_and_after_are_mutually_exclusive():
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080)
    response = asyncio.run(api._handle_logs(make_mocked_request('GET', '/logs?before=2&after=1')))
    assert response.status == 400
    assert json.loads(response.text)['error'] == 'before and after cannot be used together'


def test_before_returns_last_matching_older_records_in_chronological_order():
    handler = RecentLogBufferHandler()
    for sequence in (100, 101, 105, 108, 120):
        handler.handle(logging.LogRecord('history', logging.INFO, __file__, 0, str(sequence), (), None))
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)
    response = asyncio.run(api._handle_logs(make_mocked_request('GET', '/logs?before=4&limit=2')))
    payload = json.loads(response.text)
    assert [item['sequence'] for item in payload['items']] == [2, 3]
    assert payload['nextSequence'] == 5


def test_expired_before_returns_no_invented_history_and_signals_reload():
    handler = RecentLogBufferHandler(capacity=2)
    record = logging.LogRecord('expired-history', logging.INFO, __file__, 0, 'log', (), None)
    for _ in range(3):
        handler.handle(record)
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)

    payload = json.loads(asyncio.run(api._handle_logs(
        make_mocked_request('GET', '/logs?before=1')
    )).text)
    assert payload['cursorExpired'] is True
    assert payload['items'] == []


@pytest.mark.parametrize(('after', 'expired', 'sequences'), [
    (0, True, [2, 3]),
    (1, False, [2, 3]),
    (3, False, []),
    (4, True, []),
])
def test_logs_reports_incremental_cursor_expiry(after, expired, sequences):
    handler = RecentLogBufferHandler(capacity=2)
    logger = logging.getLogger('tests.status-api.cursor')
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        logger.info('one')
        logger.info('two')
        logger.info('three')
        api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)
        response = asyncio.run(api._handle_logs(make_mocked_request('GET', f'/logs?after={after}')))
        payload = json.loads(response.text)
        assert response.status == 200
        assert payload['cursorExpired'] is expired
        assert payload['oldestSequence'] == 2
        assert payload['latestSequence'] == 3
        assert [item['sequence'] for item in payload['items']] == sequences
    finally:
        logger.removeHandler(handler)


def test_logs_expires_previous_process_cursor_after_restart():
    previous_handler = RecentLogBufferHandler()
    record = logging.LogRecord('tests.status-api.restart', logging.INFO, __file__, 0, 'log', (), None)
    for _ in range(5832):
        previous_handler.handle(record)
    after = previous_handler.latest_sequence

    handler = RecentLogBufferHandler()
    for _ in range(3):
        handler.handle(record)
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)

    response = asyncio.run(api._handle_logs(make_mocked_request('GET', f'/logs?after={after}')))
    payload = json.loads(response.text)
    assert response.status == 200
    assert payload['oldestSequence'] == 1
    assert payload['latestSequence'] == 3
    assert payload['cursorExpired'] is True
    assert payload['items'] == []

    response = asyncio.run(api._handle_logs(make_mocked_request('GET', '/logs')))
    payload = json.loads(response.text)
    assert payload['cursorExpired'] is False
    assert [item['sequence'] for item in payload['items']] == [1, 2, 3]


@pytest.mark.parametrize(('query', 'expired'), [
    ('', False),
    ('?after=0', False),
    ('?after=1', True),
    ('?after=5832', True),
])
def test_logs_empty_buffer_cursor_expiry(query, expired):
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080)

    response = asyncio.run(api._handle_logs(make_mocked_request('GET', f'/logs{query}')))

    assert response.status == 200
    assert json.loads(response.text) == {
        'items': [],
        'totalBuffered': 0,
        'limit': 200,
        'oldestSequence': None,
        'latestSequence': 0,
        'nextSequence': 0,
        'cursorExpired': expired,
    }


def test_logs_incremental_pages_do_not_skip_matching_records():
    handler = RecentLogBufferHandler()
    for sequence in range(1, 601):
        handler.handle(logging.LogRecord('pagination', logging.INFO, __file__, 0, str(sequence), (), None))
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)

    def fetch(query):
        return json.loads(asyncio.run(api._handle_logs(make_mocked_request('GET', f'/logs?{query}'))).text)

    first = fetch('after=0&limit=500')
    assert [item['sequence'] for item in first['items']] == list(range(1, 501))
    assert first['nextSequence'] == 500
    assert first['latestSequence'] == 600
    assert first['cursorExpired'] is False
    second = fetch(f"after={first['nextSequence']}&limit=500")
    assert [item['sequence'] for item in first['items'] + second['items']] == list(range(1, 601))
    assert second['nextSequence'] == second['latestSequence'] == 600
    full = fetch('limit=100')
    assert [item['sequence'] for item in full['items']] == list(range(501, 601))
    assert full['nextSequence'] == full['latestSequence'] == 600


@pytest.mark.parametrize(('level', 'expected'), [
    ('WARNING', [2, 4, 6]),
    ('WARNING:exact', [2, 6]),
])
def test_logs_filtered_pages_advance_safely_to_snapshot_head(level, expected):
    handler = RecentLogBufferHandler()
    for severity in (logging.INFO, logging.WARNING, logging.DEBUG, logging.ERROR,
                     logging.INFO, logging.WARNING, logging.DEBUG):
        handler.handle(logging.LogRecord('filtered', severity, __file__, 0, 'log', (), None))
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)
    after = 0
    received = []
    for sequence in expected:
        payload = json.loads(asyncio.run(api._handle_logs(
            make_mocked_request('GET', f'/logs?after={after}&limit=1&level={level}')
        )).text)
        assert [item['sequence'] for item in payload['items']] == [sequence]
        received.extend(item['sequence'] for item in payload['items'])
        assert payload['latestSequence'] == 7
        assert payload['nextSequence'] == (7 if sequence == expected[-1] else sequence)
        after = payload['nextSequence']
    assert received == expected

    handler.handle(logging.LogRecord('filtered', logging.INFO, __file__, 0, 'unmatched', (), None))
    payload = json.loads(asyncio.run(api._handle_logs(
        make_mocked_request('GET', f'/logs?after={after}&level={level}')
    )).text)
    assert payload['items'] == []
    assert payload['nextSequence'] == payload['latestSequence'] == 8


def test_logs_uses_one_snapshot_even_when_an_emit_evicts_records_after_capture():
    class EmittingHandler(RecentLogBufferHandler):
        snapshots = 0

        def snapshot(self):
            snapshot = super().snapshot()
            self.snapshots += 1
            self.handle(logging.LogRecord('snapshot', logging.INFO, __file__, 0, 'new', (), None))
            return snapshot

    handler = EmittingHandler(capacity=2)
    for _ in range(3):
        handler.handle(logging.LogRecord('snapshot', logging.INFO, __file__, 0, 'old', (), None))
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)
    payload = json.loads(asyncio.run(api._handle_logs(make_mocked_request('GET', '/logs?after=1'))).text)
    assert handler.snapshots == 1
    assert handler.latest_sequence == 4
    assert payload['oldestSequence'] == 2
    assert payload['latestSequence'] == payload['nextSequence'] == 3
    assert payload['totalBuffered'] == 2
    assert payload['cursorExpired'] is False
    assert [item['sequence'] for item in payload['items']] == [2, 3]


def test_logs_empty_snapshot_can_poll_the_first_record():
    handler = RecentLogBufferHandler()
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)
    empty = json.loads(asyncio.run(api._handle_logs(make_mocked_request('GET', '/logs'))).text)
    handler.handle(logging.LogRecord('first', logging.INFO, __file__, 0, 'first', (), None))
    payload = json.loads(asyncio.run(api._handle_logs(
        make_mocked_request('GET', f"/logs?after={empty['nextSequence']}")
    )).text)
    assert payload['cursorExpired'] is False
    assert payload['nextSequence'] == 1
    assert [item['sequence'] for item in payload['items']] == [1]


def test_status_adds_runtime_metrics_and_keeps_existing_contract():
    class MetricsProvider:
        def collect(self):
            return RuntimeMetrics(12.345, 768, 7.891)

    bot = SimpleNamespace(
        chatBot={'name': 'Coddy'},
        user=None,
        guilds=[],
        tree=SimpleNamespace(get_commands=lambda: []),
        cogs={},
        latency=0.012,
        is_ready=lambda: True,
        is_closed=lambda: False,
    )
    api = BotStatusApi(bot, host='127.0.0.1', port=8080, metrics_provider=MetricsProvider())

    response = asyncio.run(api._handle_status(make_mocked_request('GET', '/status')))
    payload = json.loads(response.text)

    assert {'state', 'ready', 'guilds', 'users', 'process_id'} <= payload.keys()
    assert payload['memory_used_mb'] == 12.35
    assert payload['memory_limit_mb'] == 768
    assert payload['cpu_usage_percent'] == 7.89


def test_status_returns_null_metrics_when_provider_fails():
    class MetricsProvider:
        def collect(self):
            raise OSError('unavailable')

    bot = SimpleNamespace(
        chatBot={'name': 'Coddy'}, user=None, guilds=[],
        tree=SimpleNamespace(get_commands=lambda: []), cogs={}, latency=None,
        is_ready=lambda: False, is_closed=lambda: False,
    )
    api = BotStatusApi(bot, host='127.0.0.1', port=8080, metrics_provider=MetricsProvider())

    payload = json.loads(asyncio.run(api._handle_status(make_mocked_request('GET', '/status'))).text)

    assert payload['memory_used_mb'] is None
    assert payload['memory_limit_mb'] is None
    assert payload['cpu_usage_percent'] is None


def test_module_info_log_reaches_stream_and_buffer_once():
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    stream = io.StringIO()
    external_stream_handler = logging.StreamHandler(stream)
    app_logger = logging.getLogger('tests.status-api.mirrored-logging')
    original_app_handlers = list(app_logger.handlers)
    original_app_level = app_logger.level
    original_app_propagate = app_logger.propagate
    handler = RecentLogBufferHandler()
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)
    try:
        root_logger.handlers.clear()
        root_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(external_stream_handler)
        app_logger.handlers.clear()
        app_logger.setLevel(logging.DEBUG)
        app_logger.propagate = True

        api._attach_log_handler()
        app_logger.info('continues to standard output')

        assert 'continues to standard output' in stream.getvalue()
        messages = [item['message'] for item in handler.items()]
        assert messages.count('continues to standard output') == 1
        assert messages.count('Admin log buffer initialized') == 1
    finally:
        api._detach_log_handler()
        app_logger.handlers[:] = original_app_handlers
        app_logger.setLevel(original_app_level)
        app_logger.propagate = original_app_propagate
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)


def test_attach_does_not_configure_root_stream_or_level():
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    handler = RecentLogBufferHandler()
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)
    try:
        root_logger.handlers.clear()
        root_logger.setLevel(logging.WARNING)

        api._attach_log_handler()

        assert root_logger.handlers == [handler]
        assert root_logger.level == logging.WARNING
    finally:
        api._detach_log_handler()
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)


def test_detach_preserves_external_handlers_and_removes_only_api_handlers():
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_access_handlers = list(access_logger.handlers)
    external_handler = logging.NullHandler()
    external_access_handler = logging.NullHandler()
    handler = RecentLogBufferHandler()
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)
    try:
        root_logger.handlers.clear()
        original_level = root_logger.level
        root_logger.setLevel(logging.WARNING)
        root_logger.addHandler(external_handler)
        access_logger.handlers.clear()
        access_logger.addHandler(external_access_handler)

        api._attach_log_handler()
        api._detach_log_handler()

        assert root_logger.handlers == [external_handler]
        assert access_logger.handlers == [external_access_handler]
        assert handler not in root_logger.handlers
        assert handler not in access_logger.handlers
        assert root_logger.level == logging.WARNING
    finally:
        api._detach_log_handler()
        access_logger.handlers[:] = original_access_handlers
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)


def test_discord_logger_keeps_its_normal_handler_without_buffer_duplication():
    root_logger = logging.getLogger()
    original_root_level = root_logger.level
    discord_logger = logging.getLogger('discord')
    original_handlers = list(discord_logger.handlers)
    original_level = discord_logger.level
    original_propagate = discord_logger.propagate
    stream = io.StringIO()
    external_stream_handler = logging.StreamHandler(stream)
    handler = RecentLogBufferHandler()
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)
    try:
        root_logger.setLevel(logging.INFO)
        discord_logger.handlers.clear()
        discord_logger.addHandler(external_stream_handler)
        discord_logger.setLevel(logging.DEBUG)
        discord_logger.propagate = False

        api._attach_log_handler()
        discord_logger.info('discord output remains normal')

        assert 'discord output remains normal' in stream.getvalue()
        messages = [item['message'] for item in handler.items()]
        assert messages.count('discord output remains normal') == 1
        assert messages.count('Admin log buffer initialized') == 1

        api._detach_log_handler()
        assert discord_logger.handlers == [external_stream_handler]
        assert handler not in discord_logger.handlers
    finally:
        api._detach_log_handler()
        root_logger.setLevel(original_root_level)
        discord_logger.handlers[:] = original_handlers
        discord_logger.setLevel(original_level)
        discord_logger.propagate = original_propagate


def test_start_stop_are_idempotent_without_duplicate_log_handlers():
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    handler = RecentLogBufferHandler()
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=0, log_handler=handler)

    async def exercise_lifecycle():
        await api.start()
        await api.start()
        assert root_logger.handlers.count(handler) == 1
        assert [item['message'] for item in handler.items()].count(
            'Admin log buffer initialized'
        ) == 1

        await api.stop()
        await api.stop()

    try:
        root_logger.handlers.clear()
        original_level = root_logger.level
        root_logger.setLevel(logging.INFO)

        asyncio.run(exercise_lifecycle())
        assert handler not in root_logger.handlers
    finally:
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)


def test_attach_initializes_admin_log_buffer():
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    handler = RecentLogBufferHandler()
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)
    try:
        root_logger.handlers.clear()
        root_logger.setLevel(logging.INFO)

        api._attach_log_handler()

        items = handler.items()
        assert any(
            item['level'] == 'INFO'
            and item['logger'] == 'message_services.bot_status_api'
            and item['message'] == 'Admin log buffer initialized'
            for item in items
        )
    finally:
        api._detach_log_handler()
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)


def test_attached_buffer_receives_discord_info_record():
    root_logger = logging.getLogger()
    discord_logger = logging.getLogger('discord')
    original_root_handlers = list(root_logger.handlers)
    original_discord_handlers = list(discord_logger.handlers)
    original_discord_level = discord_logger.level
    original_discord_propagate = discord_logger.propagate
    handler = RecentLogBufferHandler()
    api = BotStatusApi(FakeBot(ready=True), host='127.0.0.1', port=8080, log_handler=handler)
    try:
        root_logger.handlers.clear()
        discord_logger.handlers.clear()
        discord_logger.addHandler(logging.NullHandler())
        discord_logger.setLevel(logging.INFO)
        discord_logger.propagate = False

        api._attach_log_handler()
        discord_logger.info('discord gateway ready')

        messages = [item['message'] for item in handler.items()]
        assert messages.count('discord gateway ready') == 1
    finally:
        api._detach_log_handler()
        root_logger.handlers[:] = original_root_handlers
        discord_logger.handlers[:] = original_discord_handlers
        discord_logger.setLevel(original_discord_level)
        discord_logger.propagate = original_discord_propagate
