"""Regression coverage for discord.py's long-window false positive."""

import asyncio

import discord
import pytest

from core import discord_ratelimit_compat as compat


def _bucket(*, remaining: int, reset_seconds: float = 1380.0):
    bucket = discord.http.Ratelimit(120.0)
    bucket.limit = 250
    bucket.remaining = remaining
    bucket.reset_after = reset_seconds
    bucket.expires = asyncio.get_running_loop().time() + reset_seconds
    return bucket


def test_upstream_232_rejects_available_tokens_but_compat_allows_request():
    async def scenario():
        bucket = _bucket(remaining=248)
        with pytest.raises(discord.RateLimited):
            await compat._ORIGINAL_ACQUIRE(bucket)
        assert bucket.remaining == 248
        assert bucket.outgoing == 0

        await compat._acquire_with_available_capacity(bucket)
        assert bucket.remaining == 247
        assert bucket.outgoing == 1
        assert bucket.reset_after == 1380.0

    asyncio.run(scenario())


def test_exhausted_bucket_still_enforces_120_second_limit():
    async def scenario():
        bucket = _bucket(remaining=0)
        with pytest.raises(discord.RateLimited) as exc_info:
            await compat._acquire_with_available_capacity(bucket)
        assert exc_info.value.retry_after > 120
        assert bucket.remaining == 0
        assert bucket.outgoing == 0

    asyncio.run(scenario())


def test_expired_bucket_replenishes_before_limit_check():
    async def scenario():
        bucket = _bucket(remaining=0)
        bucket.expires = asyncio.get_running_loop().time() - 1.0

        await compat._acquire_with_available_capacity(bucket)
        assert bucket.remaining == bucket.limit - 1
        assert bucket.outgoing == 1

    asyncio.run(scenario())


def test_patch_is_idempotent_and_version_guarded(monkeypatch):
    monkeypatch.setattr(discord.http.Ratelimit, "acquire", compat._ORIGINAL_ACQUIRE)
    monkeypatch.setattr(discord, "__version__", "2.3.2")
    assert compat.install_discord_ratelimit_compat()
    assert discord.http.Ratelimit.acquire is compat._acquire_with_available_capacity
    assert compat.install_discord_ratelimit_compat()

    monkeypatch.setattr(discord, "__version__", "2.7.0")
    monkeypatch.setattr(discord.http.Ratelimit, "acquire", compat._ORIGINAL_ACQUIRE)
    assert not compat.install_discord_ratelimit_compat()
    assert discord.http.Ratelimit.acquire is compat._ORIGINAL_ACQUIRE
