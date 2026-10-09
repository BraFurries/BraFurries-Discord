import asyncio

from core import backup_progress_events as events


def test_notification_is_disabled_without_service_token(monkeypatch):
    monkeypatch.delenv("BOT_STATUS_API_TOKEN", raising=False)

    async def check():
        events.notify_backup_progress(77, "RESTORE", 9, 12)
        await asyncio.sleep(0)
        assert events._latest == {}
        assert events._sending == set()

    asyncio.run(check())


def test_notification_posts_authenticated_persisted_step(monkeypatch):
    monkeypatch.setenv("BOT_STATUS_API_TOKEN", "test-internal-token")
    monkeypatch.setenv("BOT_API_BASE_URL", "http://127.0.0.1:18080/")
    sent = []

    class FakeResponse:
        status = 204

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeSession:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, *, json, headers):
            sent.append((url, json, headers))
            return FakeResponse()

    monkeypatch.setattr(events.aiohttp, "ClientSession", FakeSession)

    async def check():
        events.notify_backup_progress(77, "RESTORE", 9, 123)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(check())

    assert sent == [(
        "http://127.0.0.1:18080/internal/backup-progress",
        {"guildId": 77, "operationKind": "RESTORE", "operationId": 9, "stepId": 123},
        {"Authorization": "Bearer test-internal-token"},
    )]
    assert events._latest == {}
    assert events._sending == set()
