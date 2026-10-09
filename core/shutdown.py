from __future__ import annotations

import asyncio
from typing import Any


class GracefulShutdownHandler:
    """Schedules one close operation on the runtime event loop.

    The runtime owns the returned task and awaits it before leaving
    ``asyncio.run``. Keeping that ownership outside the signal callback is
    important because Client.close marks the client closed before all of its
    resources have finished closing.
    """

    def __init__(self, bot: Any, loop: asyncio.AbstractEventLoop):
        self.bot = bot
        self.loop = loop
        self.shutdown_requested = False
        self.close_task: asyncio.Task[None] | None = None

    def request(self, signum: int | None = None, frame: Any = None) -> asyncio.Task[None]:
        """Request shutdown and return the single task that performs it."""
        if self.close_task is not None:
            return self.close_task

        self.shutdown_requested = True
        self.close_task = self.loop.create_task(self.bot.close())
        return self.close_task
