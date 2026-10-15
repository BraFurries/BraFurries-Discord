"""Compatibility fix for a discord.py 2.3.2 rate-limit false positive.

In Ratelimit.acquire(), discord.py checks the time until reset against
max_ratelimit_timeout before checking `remaining`. For large buckets
(e.g. 250 create-role requests in a long window), the second request can
then block all subsequent requests despite remaining > 0.

This mirrors the installed 2.3.2 implementation with just one change:
only reject a long wait when the bucket has no remaining tokens.
The bot's 120-second protection is otherwise unchanged.
"""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)
_ORIGINAL_ACQUIRE = discord.http.Ratelimit.acquire
_SUPPORTED_VERSION = "2.3.2"


async def _acquire_with_available_capacity(self: discord.http.Ratelimit) -> None:
    self._last_request = self._loop.time()
    if self.is_expired():
        self.reset()

    # The upstream 2.3.2 check has no `remaining <= 0` guard. The reset
    # time is NOT a cooldown while the bucket still has request capacity.
    if (
        self.remaining <= 0
        and self._max_ratelimit_timeout is not None
        and self.expires is not None
    ):
        current_reset_after = self.expires - self._loop.time()
        if current_reset_after > self._max_ratelimit_timeout:
            raise discord.RateLimited(current_reset_after)

    while self.remaining <= 0:
        future = self._loop.create_future()
        self._pending_requests.append(future)
        try:
            await future
        except BaseException:
            future.cancel()
            if self.remaining > 0 and not future.cancelled():
                self._wake_next()
            raise

    self.remaining -= 1
    self.outgoing += 1


def install_discord_ratelimit_compat() -> bool:
    """Install the minimal fix once, and only for the audited library version."""
    if discord.__version__ != _SUPPORTED_VERSION:
        logger.warning(
            "Skipping discord.py rate-limit compatibility fix: version=%s expected=%s",
            discord.__version__,
            _SUPPORTED_VERSION,
        )
        return False

    current = discord.http.Ratelimit.acquire
    if current is _acquire_with_available_capacity:
        return True
    if current is not _ORIGINAL_ACQUIRE:
        logger.warning("Skipping discord.py rate-limit compatibility fix: acquire already patched")
        return False

    discord.http.Ratelimit.acquire = _acquire_with_available_capacity
    logger.info("Installed discord.py 2.3.2 available-capacity rate-limit compatibility fix")
    return True
