import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import make_mocked_request

from message_services.bot_status_api import BotStatusApi, _build_auth_middleware


def lookup_request(value: str):
    return make_mocked_request(
        'GET', f'/users/{value}',
        match_info={'discord_user_id': value},
    )


def make_api(*, ready=True, initialized=True, token='internal-token'):
    bot = SimpleNamespace(
        is_ready=lambda: ready,
        fetch_user=AsyncMock(),
    )
    api = BotStatusApi(
        bot,
        host='127.0.0.1',
        port=8080,
        token=token,
        initialized_getter=lambda: initialized,
    )
    return api, bot


def test_lookup_returns_guild_independent_identity_contract():
    api, bot = make_api()
    bot.fetch_user.return_value = SimpleNamespace(
        id=123456789012345678,
        name='outside.user',
        global_name='Outside User',
        display_name='Outside User',
        display_avatar=SimpleNamespace(url='https://cdn.example/avatar.png'),
        bot=False,
    )

    result = asyncio.run(api._handle_discord_user_lookup(lookup_request('123456789012345678')))

    assert result.status == 200
    assert json.loads(result.text) == {
        'discordUserId': '123456789012345678',
        'username': 'outside.user',
        'displayName': 'Outside User',
        'avatarUrl': 'https://cdn.example/avatar.png',
        'bot': False,
    }
    bot.fetch_user.assert_awaited_once_with(123456789012345678)


def test_lookup_uses_discord_display_name_and_default_avatar():
    api, bot = make_api()
    bot.fetch_user.return_value = SimpleNamespace(
        id=123456789012345678,
        name='another.user',
        global_name=None,
        display_name='another.user',
        display_avatar=SimpleNamespace(url='https://cdn.example/default.png'),
        bot=True,
    )

    result = asyncio.run(api._handle_discord_user_lookup(lookup_request('123456789012345678')))

    assert result.status == 200
    assert json.loads(result.text)['displayName'] == 'another.user'
    assert json.loads(result.text)['avatarUrl'] == 'https://cdn.example/default.png'
    assert json.loads(result.text)['bot'] is True


@pytest.mark.parametrize('value', [
    '0', '-1', 'not-an-id', '123abc', '9223372036854775808', '123456789012345678901',
])
def test_lookup_rejects_invalid_discord_ids_without_calling_discord(value):
    api, bot = make_api()

    result = asyncio.run(api._handle_discord_user_lookup(lookup_request(value)))

    assert result.status == 400
    assert json.loads(result.text)['error'] == 'invalid_discord_user_id'
    bot.fetch_user.assert_not_awaited()


@pytest.mark.parametrize('ready,initialized,token', [
    (False, True, 'internal-token'),
    (True, False, 'internal-token'),
    (True, True, None),
])
def test_lookup_fails_closed_when_not_ready_or_without_internal_token(ready, initialized, token):
    api, bot = make_api(ready=ready, initialized=initialized, token=token)

    result = asyncio.run(api._handle_discord_user_lookup(lookup_request('123456789012345678')))

    assert result.status == 503
    bot.fetch_user.assert_not_awaited()


def test_lookup_returns_404_only_when_discord_confirms_missing_user():
    api, bot = make_api()
    bot.fetch_user.side_effect = discord.NotFound(
        SimpleNamespace(status=404, reason='Not Found'),
        'Unknown User',
    )

    result = asyncio.run(api._handle_discord_user_lookup(lookup_request('123456789012345678')))

    assert result.status == 404
    assert json.loads(result.text)['error'] == 'discord_user_not_found'


@pytest.mark.parametrize('exception_cls,status', [
    (discord.Forbidden, 403),
    (discord.HTTPException, 503),
])
def test_lookup_returns_503_on_discord_api_failure(exception_cls, status):
    api, bot = make_api()
    bot.fetch_user.side_effect = exception_cls(
        SimpleNamespace(status=status, reason='Discord API error'),
        'Discord unavailable',
    )

    result = asyncio.run(api._handle_discord_user_lookup(lookup_request('123456789012345678')))

    assert result.status == 503
    assert json.loads(result.text)['error'] == 'discord_user_lookup_unavailable'


def test_lookup_requires_correct_internal_bearer_token():
    middleware = _build_auth_middleware('internal-token')

    async def handler(request):
        return request

    denied = asyncio.run(middleware(lookup_request('123456789012345678'), handler))
    assert denied.status == 401
    authorized = make_mocked_request(
        'GET', '/users/123456789012345678',
        headers={'Authorization': 'Bearer internal-token'},
    )
    assert asyncio.run(middleware(authorized, handler)) is authorized


def test_live_http_lookup_route_is_authenticated():
    api, bot = make_api()
    api.port = 0
    bot.fetch_user.return_value = SimpleNamespace(
        id=123456789012345678,
        name='outside.user',
        global_name=None,
        display_name='outside.user',
        display_avatar=SimpleNamespace(url='https://cdn.example/avatar.png'),
        bot=False,
    )

    async def run():
        await api.start()
        try:
            port = api._site._server.sockets[0].getsockname()[1]
            url = f'http://127.0.0.1:{port}/users/123456789012345678'
            async with ClientSession() as session:
                async with session.get(url) as denied:
                    assert denied.status == 401
                bot.fetch_user.assert_not_awaited()
                async with session.get(url, headers={'Authorization': 'Bearer internal-token'}) as allowed:
                    assert allowed.status == 200
                    assert (await allowed.json())['username'] == 'outside.user'
                bot.fetch_user.assert_awaited_once_with(123456789012345678)
        finally:
            await api.stop()

    asyncio.run(run())
