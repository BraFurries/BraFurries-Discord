import asyncio
import signal

from core.discord_runtime import register_shutdown_signals
from core.shutdown import GracefulShutdownHandler


class FakeBot:
    def __init__(self):
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1


def test_multiple_shutdown_requests_schedule_close_once():
    async def scenario():
        bot = FakeBot()
        handler = GracefulShutdownHandler(bot, asyncio.get_running_loop())

        first_task = handler.request()
        second_task = handler.request()

        assert first_task is second_task
        await first_task
        assert bot.close_calls == 1

    asyncio.run(scenario())


def test_sigterm_starts_only_one_shutdown():
    class SignalRegistrationLoop:
        def __init__(self):
            self.callbacks = {}

        def add_signal_handler(self, signum, callback):
            self.callbacks[signum] = callback

    async def scenario():
        bot = FakeBot()
        handler = GracefulShutdownHandler(bot, asyncio.get_running_loop())
        registration_loop = SignalRegistrationLoop()

        register_shutdown_signals(registration_loop, handler)
        registration_loop.callbacks[signal.SIGTERM]()
        registration_loop.callbacks[signal.SIGTERM]()

        await handler.close_task
        assert bot.close_calls == 1

    asyncio.run(scenario())
