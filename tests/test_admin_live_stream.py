import asyncio
import logging
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.recent_logs import RecentLogBufferHandler
from core.runtime_metrics import RuntimeMetrics
from message_services.bot_status_api import BotStatusApi


class Bot:
    user = SimpleNamespace(name="Coddy")
    guilds = []
    tree = SimpleNamespace(get_commands=lambda: [])
    cogs = {}
    latency = 0.1

    def is_ready(self):
        return True

    def is_closed(self):
        return False


def test_buffer_wakes_async_subscribers_without_periodic_polling():
    async def scenario():
        handler = RecentLogBufferHandler(capacity=5)
        waiting = asyncio.create_task(handler.wait_for_new_logs(after=0, timeout=2))
        await asyncio.sleep(0)
        handler.emit(logging.LogRecord("sample", logging.INFO, "", 1, "hello", (), None))
        assert await asyncio.wait_for(waiting, timeout=1)
        assert handler.snapshot().latest_sequence == 1
        assert handler._watchers == set()
    asyncio.run(scenario())


def test_admin_sse_stream_requires_token_and_emits_status_and_logs():
    async def scenario():
        handler = RecentLogBufferHandler(capacity=10)
        api = BotStatusApi(
            Bot(), host="127.0.0.1", port=0, token="internal-test",
            log_handler=handler,
            metrics_provider=SimpleNamespace(
                collect=lambda: RuntimeMetrics(None, None, None)
            ),
        )
        from message_services.bot_status_api import _build_auth_middleware
        secured = web.Application(middlewares=[_build_auth_middleware("internal-test")])
        secured.router.add_get("/admin-stream", api._handle_admin_stream)
        async with TestClient(TestServer(secured)) as client:
            denied = await client.get("/admin-stream")
            assert denied.status == 401
            denied.release()
            headers = {"Authorization": "Bearer internal-test"}
            stream = await client.get("/admin-stream?level=ERROR&after=0", headers=headers)
            assert stream.status == 200
            assert (await asyncio.wait_for(stream.content.readline(), 2)).decode() == "event: status\n"
            await stream.content.readline()
            await stream.content.readline()
            handler.emit(logging.LogRecord("unit", logging.ERROR, "", 1, "failure", (), None))
            assert (await asyncio.wait_for(stream.content.readline(), 2)).decode() == "event: logs\n"
            data_line = (await stream.content.readline()).decode()
            assert '"failure"' in data_line
            assert '"sequence": 1' in data_line
            stream.close()
        assert api._active_admin_streams == 0
    asyncio.run(scenario())



def test_admin_stream_keeps_emitting_status_without_reconnect():
    async def scenario():
        api = BotStatusApi(
            Bot(), host="127.0.0.1", port=0, token="internal-test",
            log_handler=RecentLogBufferHandler(capacity=10),
            metrics_provider=SimpleNamespace(
                collect=lambda: RuntimeMetrics(None, None, None),
            ),
        )
        from message_services.bot_status_api import _build_auth_middleware
        app = web.Application(middlewares=[_build_auth_middleware("internal-test")])
        app.router.add_get("/admin-stream", api._handle_admin_stream)
        async with TestClient(TestServer(app)) as client:
            stream = await client.get(
                "/admin-stream?after=0",
                headers={"Authorization": "Bearer internal-test"},
            )
            assert stream.status == 200
            assert (await asyncio.wait_for(stream.content.readline(), 2)).decode() == "event: status\n"
            await stream.content.readline()
            await stream.content.readline()
            # The next metrics update arrives on the SAME connection, without
            # the old 55-second session rotation.
            assert (await asyncio.wait_for(stream.content.readline(), 8)).decode() == "event: status\n"
            await stream.content.readline()
            await stream.content.readline()
            assert not stream.content.at_eof()
            # Cross the former 55-second deadline: unlike a short heartbeat
            # test, this fails if the automatic stream rotation is reintroduced.
            await asyncio.sleep(56)
            assert api._active_admin_streams == 1
            stream.close()
        assert api._active_admin_streams == 0

    asyncio.run(scenario())



def test_shutdown_drains_an_open_admin_stream():
    async def scenario():
        api = BotStatusApi(
            Bot(), host="127.0.0.1", port=0, token="internal-test",
            log_handler=RecentLogBufferHandler(capacity=10),
            metrics_provider=SimpleNamespace(
                collect=lambda: RuntimeMetrics(None, None, None),
            ),
        )
        from message_services.bot_status_api import _build_auth_middleware
        app = web.Application(middlewares=[_build_auth_middleware("internal-test")])
        app.router.add_get("/admin-stream", api._handle_admin_stream)
        async with TestClient(TestServer(app)) as client:
            stream = await client.get(
                "/admin-stream", headers={"Authorization": "Bearer internal-test"},
            )
            assert stream.status == 200
            assert (await asyncio.wait_for(stream.content.readline(), 2)).decode() == "event: status\n"
            await stream.content.readline()
            await stream.content.readline()
            assert api._active_admin_streams == 1
            # Production stop() sets this before awaiting runner.cleanup().
            api._admin_stream_stopping.set()
            await asyncio.wait_for(stream.content.read(), timeout=6)
            assert stream.content.at_eof()
        assert api._active_admin_streams == 0
    asyncio.run(scenario())


def test_stop_signals_stream_shutdown_before_runner_cleanup():
    async def scenario():
        api = BotStatusApi(Bot(), host="127.0.0.1", port=0, token="internal-test")
        class Runner:
            async def cleanup(self):
                assert api._admin_stream_stopping.is_set()
        api._runner = Runner()
        await api.stop()
        assert api._admin_stream_stopping.is_set()
    asyncio.run(scenario())


def test_stream_rejects_missing_internal_configuration():
    async def scenario():
        api = BotStatusApi(Bot(), host="127.0.0.1", port=0, token=None)
        app = web.Application()
        app.router.add_get("/admin-stream", api._handle_admin_stream)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/admin-stream")
            assert response.status == 503
            assert (await response.json())["error"] == "internal_auth_not_configured"
    asyncio.run(scenario())
