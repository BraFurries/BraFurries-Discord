import asyncio

import pytest

from core.identity_api import IdentityApiError, get_identity_summary


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response, request_log):
        self.response = response
        self.request_log = request_log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, url, **kwargs):
        self.request_log.append((url, kwargs))
        return self.response


def session_factory(payload, request_log):
    return lambda **_kwargs: FakeSession(FakeResponse(payload), request_log)


def test_user_without_links_has_zero_other_accounts(monkeypatch):
    monkeypatch.setenv("BOT_API_BASE_URL", "http://api.internal/")
    monkeypatch.setenv("BOT_STATUS_API_TOKEN", "shared-token")
    requests = []
    payload = {
        "requestedUserId": 1,
        "confirmedIdentities": [{"userId": 1, "discordUserIds": ["111"]}],
        "otherAccountCount": 0,
        "moderation": {"communityId": 50, "warningCount": 2},
    }

    summary = asyncio.run(
        get_identity_summary(
            111,
            50,
            session_factory=session_factory(payload, requests),
        )
    )

    assert summary.other_account_count == 0
    assert summary.warning_count == 2
    assert summary.requested_user_id == 1
    assert summary.confirmed_identities[0].discord_user_ids == (111,)


def test_abc_cluster_uses_api_count_and_ignores_suspected_payload(monkeypatch):
    monkeypatch.setenv("BOT_API_BASE_URL", "http://api.internal")
    monkeypatch.setenv("BOT_STATUS_API_TOKEN", "shared-token")
    requests = []
    payload = {
        "requestedUserId": 1,
        "confirmedIdentities": [
            {"userId": 1, "discordUserIds": ["111"]},
            {"userId": 2, "discordUserIds": ["222"]},
            {"userId": 3, "discordUserIds": ["333"]},
        ],
        "suspectedIdentities": [
            {"userId": 4, "username": "não pode vazar", "discordUserIds": ["444"]}
        ],
        "otherAccountCount": 2,
        "moderation": {"communityId": 50, "warningCount": 6},
    }

    summary = asyncio.run(
        get_identity_summary(
            111,
            50,
            session_factory=session_factory(payload, requests),
        )
    )

    assert summary.other_account_count == 2
    assert summary.warning_count == 6
    url, kwargs = requests[0]
    assert url == "http://api.internal/internal/identity/discord-users/111"
    assert kwargs["params"] == {"communityId": 50}
    assert kwargs["headers"] == {"Authorization": "Bearer shared-token"}


def test_community_mismatch_is_rejected(monkeypatch):
    monkeypatch.setenv("BOT_API_BASE_URL", "http://api.internal")
    monkeypatch.setenv("BOT_STATUS_API_TOKEN", "shared-token")
    payload = {
        "otherAccountCount": 1,
        "moderation": {"communityId": 51, "warningCount": 99},
    }

    with pytest.raises(IdentityApiError, match="fora da comunidade"):
        asyncio.run(
            get_identity_summary(
                111,
                50,
                session_factory=session_factory(payload, []),
            )
        )


def test_missing_token_fails_closed(monkeypatch):
    monkeypatch.setenv("BOT_API_BASE_URL", "http://api.internal")
    monkeypatch.delenv("BOT_STATUS_API_TOKEN", raising=False)

    with pytest.raises(IdentityApiError, match="não configurada"):
        asyncio.run(get_identity_summary(111, 50))
