from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

import discord

from core.shutdown import GracefulShutdownHandler


def configure_application_logging() -> None:
    """Ensure application INFO records reach the normal process stream."""
    root_logger = logging.getLogger()
    if root_logger.getEffectiveLevel() > logging.INFO:
        root_logger.setLevel(logging.INFO)

    stream_handlers = [
        handler for handler in root_logger.handlers if _is_standard_stream_handler(handler)
    ]
    if not stream_handlers:
        root_logger.addHandler(logging.StreamHandler())
    elif not any(handler.level <= logging.INFO for handler in stream_handlers):
        stream_handlers[0].setLevel(logging.INFO)


def configure_discord_logging() -> None:
    """Configure discord.py logging like ``Client.run`` without duplication."""
    discord_logger = logging.getLogger("discord")
    if any(not isinstance(handler, logging.NullHandler) for handler in discord_logger.handlers):
        return

    discord.utils.setup_logging(level=logging.INFO, root=False)
    # setup_logging(root=False) adds a handler to ``discord`` but leaves
    # propagation enabled. Keep the library stream isolated so root handlers
    # (including the administrative buffer) do not duplicate its output.
    discord_logger.propagate = False


def _is_standard_stream_handler(handler: logging.Handler) -> bool:
    return isinstance(handler, logging.StreamHandler) and getattr(handler, 'stream', None) in {
        sys.stdout,
        sys.stderr,
    }


def register_shutdown_signals(
    loop: asyncio.AbstractEventLoop,
    shutdown_handler: GracefulShutdownHandler,
) -> list[int]:
    """Register SIGTERM and SIGINT against the loop running the bot."""
    registered_signals: list[int] = []

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, shutdown_handler.request)
        except (NotImplementedError, RuntimeError):
            # Windows proactor loops do not implement add_signal_handler.
            # Keep the OS signal callback minimal and hand work to this loop.
            signal.signal(
                signum,
                lambda received_signum, frame: loop.call_soon_threadsafe(
                    shutdown_handler.request, received_signum, frame
                ),
            )
        registered_signals.append(signum)

    return registered_signals


async def run_discord_bot(bot: Any, token: str) -> None:
    """Run Discord and wait for every part of its asynchronous close path."""
    loop = asyncio.get_running_loop()
    shutdown_handler = GracefulShutdownHandler(bot, loop)
    registered_signals = register_shutdown_signals(loop, shutdown_handler)

    try:
        await bot.start(token)
    finally:
        try:
            close_task = shutdown_handler.request()
            await _wait_for_close(close_task)
        finally:
            for signum in registered_signals:
                try:
                    loop.remove_signal_handler(signum)
                except (NotImplementedError, RuntimeError):
                    pass


async def _wait_for_close(close_task: asyncio.Task[None]) -> None:
    """Finish close even when the runner itself is cancelled during teardown."""
    cancellation: asyncio.CancelledError | None = None

    while not close_task.done():
        try:
            # A signal-triggered close can make bot.start return before the
            # HTTP client closes. Shielding keeps the close task alive while
            # this runner waits, including if the runner gets cancelled.
            await asyncio.shield(close_task)
        except asyncio.CancelledError as exc:
            cancellation = exc

    if close_task.cancelled():
        raise asyncio.CancelledError

    close_exception = close_task.exception()
    if close_exception is not None:
        raise close_exception

    if cancellation is not None:
        raise cancellation
