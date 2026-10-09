import asyncio
import io
import logging
import sys

import pytest

from core.discord_runtime import (
    configure_application_logging,
    configure_discord_logging,
    run_discord_bot,
)


def test_configure_application_logging_enables_info_from_default_root():
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    try:
        root_logger.handlers.clear()
        root_logger.setLevel(logging.WARNING)

        configure_application_logging()

        assert root_logger.level == logging.INFO
    finally:
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)


def test_configure_application_logging_preserves_debug_level():
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    try:
        root_logger.handlers.clear()
        root_logger.setLevel(logging.DEBUG)

        configure_application_logging()

        assert root_logger.level == logging.DEBUG
    finally:
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)


def test_configure_application_logging_is_idempotent_and_preserves_handlers():
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    external_handler = logging.NullHandler()
    try:
        root_logger.handlers[:] = [external_handler]
        root_logger.setLevel(logging.WARNING)

        configure_application_logging()
        configure_application_logging()

        assert external_handler in root_logger.handlers
        assert sum(
            isinstance(handler, logging.StreamHandler)
            and getattr(handler, 'stream', None) in {sys.stdout, sys.stderr}
            for handler in root_logger.handlers
        ) == 1
    finally:
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)


def test_application_info_reaches_stream_without_status_api(monkeypatch):
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    application_logger = logging.getLogger(__name__)
    original_application_handlers = list(application_logger.handlers)
    original_application_level = application_logger.level
    original_application_propagate = application_logger.propagate
    stream = io.StringIO()
    try:
        root_logger.handlers.clear()
        root_logger.setLevel(logging.WARNING)
        application_logger.handlers.clear()
        application_logger.setLevel(logging.NOTSET)
        application_logger.propagate = True
        monkeypatch.setattr(sys, 'stderr', stream)

        configure_application_logging()
        application_logger.info('application logging is active')

        assert stream.getvalue().count('application logging is active') == 1
    finally:
        application_logger.handlers[:] = original_application_handlers
        application_logger.setLevel(original_application_level)
        application_logger.propagate = original_application_propagate
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)


def test_configure_application_logging_reuses_warning_stream_for_info(monkeypatch):
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    application_logger = logging.getLogger('tests.application-logging.warning-stream')
    original_application_handlers = list(application_logger.handlers)
    original_application_level = application_logger.level
    original_application_propagate = application_logger.propagate
    stream = io.StringIO()
    monkeypatch.setattr(sys, 'stderr', stream)
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.WARNING)
    try:
        root_logger.handlers[:] = [stream_handler]
        root_logger.setLevel(logging.WARNING)
        application_logger.handlers.clear()
        application_logger.setLevel(logging.NOTSET)
        application_logger.propagate = True

        configure_application_logging()
        configure_application_logging()
        application_logger.info('info once')
        application_logger.warning('warning once')
        application_logger.error('error once')

        assert root_logger.handlers == [stream_handler]
        assert stream_handler.level == logging.INFO
        assert stream.getvalue().count('info once') == 1
        assert stream.getvalue().count('warning once') == 1
        assert stream.getvalue().count('error once') == 1
    finally:
        application_logger.handlers[:] = original_application_handlers
        application_logger.setLevel(original_application_level)
        application_logger.propagate = original_application_propagate
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)


def test_configure_application_logging_keeps_notset_stream(monkeypatch):
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    stream = io.StringIO()
    monkeypatch.setattr(sys, 'stderr', stream)
    warning_handler = logging.StreamHandler()
    warning_handler.setLevel(logging.WARNING)
    stream_handler = logging.StreamHandler()
    assert stream_handler.level == logging.NOTSET
    try:
        root_logger.handlers[:] = [warning_handler, stream_handler]
        root_logger.setLevel(logging.WARNING)

        configure_application_logging()
        configure_application_logging()

        assert root_logger.handlers == [warning_handler, stream_handler]
        assert warning_handler.level == logging.WARNING
        assert stream_handler.level == logging.NOTSET
    finally:
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)


def test_configure_discord_logging_matches_client_run(monkeypatch):
    setup_calls = []
    discord_logger = logging.getLogger("discord")
    monkeypatch.setattr(discord_logger, "handlers", [])
    monkeypatch.setattr(discord_logger, "propagate", True)
    monkeypatch.setattr(
        "core.discord_runtime.discord.utils.setup_logging",
        lambda **kwargs: setup_calls.append(kwargs),
    )

    configure_discord_logging()

    assert setup_calls == [{"level": logging.INFO, "root": False}]
    assert discord_logger.propagate is False


def test_configure_discord_logging_ignores_only_null_handler(monkeypatch):
    discord_logger = logging.getLogger("discord")
    monkeypatch.setattr(discord_logger, "handlers", [logging.NullHandler()])
    monkeypatch.setattr(discord_logger, "propagate", True)
    setup_calls = []
    monkeypatch.setattr(
        "core.discord_runtime.discord.utils.setup_logging",
        lambda **kwargs: setup_calls.append(kwargs),
    )

    configure_discord_logging()

    assert setup_calls == [{"level": logging.INFO, "root": False}]


def test_configure_discord_logging_is_idempotent(monkeypatch):
    discord_logger = logging.getLogger("discord")
    monkeypatch.setattr(discord_logger, "handlers", [logging.NullHandler()])
    monkeypatch.setattr(discord_logger, "propagate", True)
    setup_calls = []

    def setup_logging(**kwargs):
        setup_calls.append(kwargs)
        discord_logger.addHandler(logging.StreamHandler())

    monkeypatch.setattr("core.discord_runtime.discord.utils.setup_logging", setup_logging)

    configure_discord_logging()
    configure_discord_logging()

    assert setup_calls == [{"level": logging.INFO, "root": False}]
    assert sum(not isinstance(handler, logging.NullHandler) for handler in discord_logger.handlers) == 1


def test_configure_discord_logging_keeps_real_existing_handler(monkeypatch):
    discord_logger = logging.getLogger("discord")
    null_handler = logging.NullHandler()
    existing_handler = logging.StreamHandler()
    monkeypatch.setattr(discord_logger, "handlers", [null_handler, existing_handler])
    monkeypatch.setattr(
        "core.discord_runtime.discord.utils.setup_logging",
        lambda **kwargs: pytest.fail("setup_logging should not add a duplicate handler"),
    )

    configure_discord_logging()

    assert discord_logger.handlers == [null_handler, existing_handler]


class FakeBot:
    def __init__(self):
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.close_finished = asyncio.Event()
        self.start_returned = asyncio.Event()
        self.close_calls = 0
        self.shutdown_callbacks_finished = False

    async def start(self, token):
        await self.close_started.wait()
        self.start_returned.set()

    async def close(self):
        self.close_calls += 1
        self.close_started.set()
        await self.allow_close.wait()
        self.shutdown_callbacks_finished = True
        self.close_finished.set()


def test_runner_waits_for_close_and_shutdown_callbacks(monkeypatch):
    async def scenario():
        bot = FakeBot()
        handlers = []

        def register_signals(loop, handler):
            handlers.append(handler)
            return []

        monkeypatch.setattr(
            "core.discord_runtime.register_shutdown_signals",
            register_signals,
        )

        runner = asyncio.create_task(run_discord_bot(bot, "token"))
        await asyncio.sleep(0)
        handlers[0].request()
        handlers[0].request()
        await bot.close_started.wait()
        await bot.start_returned.wait()
        assert not runner.done()
        assert not bot.shutdown_callbacks_finished

        bot.allow_close.set()
        await runner
        assert bot.close_calls == 1
        assert bot.shutdown_callbacks_finished

    asyncio.run(scenario())


def test_runner_propagates_close_errors(monkeypatch):
    class FailingBot:
        async def start(self, token):
            return None

        async def close(self):
            raise RuntimeError("shutdown callback failed")

    monkeypatch.setattr(
        "core.discord_runtime.register_shutdown_signals",
        lambda loop, handler: [],
    )

    with pytest.raises(RuntimeError, match="shutdown callback failed"):
        asyncio.run(run_discord_bot(FailingBot(), "token"))


def test_runner_propagates_start_errors_after_closing(monkeypatch):
    class FailingBot:
        def __init__(self):
            self.closed = False

        async def start(self, token):
            raise ValueError("discord connection failed")

        async def close(self):
            self.closed = True

    bot = FailingBot()
    monkeypatch.setattr(
        "core.discord_runtime.register_shutdown_signals",
        lambda loop, handler: [],
    )

    with pytest.raises(ValueError, match="discord connection failed"):
        asyncio.run(run_discord_bot(bot, "token"))
    assert bot.closed


def test_runner_finishes_close_when_cancelled(monkeypatch):
    async def scenario():
        bot = FakeBot()
        handlers = []
        monkeypatch.setattr(
            "core.discord_runtime.register_shutdown_signals",
            lambda loop, handler: (handlers.append(handler) or []),
        )

        runner = asyncio.create_task(run_discord_bot(bot, "token"))
        await asyncio.sleep(0)
        handlers[0].request()
        await bot.close_started.wait()
        runner.cancel()
        await asyncio.sleep(0)
        assert not bot.close_finished.is_set()

        bot.allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert bot.close_finished.is_set()

    asyncio.run(scenario())
