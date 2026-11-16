import asyncio
from unittest.mock import Mock

import cogs.records as record_module


def test_blacklist_cache_reloads_after_admin_edits_without_restart(monkeypatch):
    async def scenario():
        current = ["Jogo antigo"]
        db = Mock(side_effect=lambda guild_id: list(current))
        monkeypatch.setattr(record_module, "getBlacklistedGames", db)
        clock = [100.0]
        monkeypatch.setattr(record_module.time, "monotonic", lambda: clock[0])
        cog = record_module.RecordsCog.__new__(record_module.RecordsCog)
        cog.blacklisted_games_by_guild = {}
        cog._blacklist_refreshed_at = {}
        cog._blacklist_ttl_seconds = 10.0
        cog._blacklist_loads = {}

        assert await cog._get_blacklisted_games(123) == {"jogo antigo"}
        current.append("Jogo novo")
        assert await cog._get_blacklisted_games(123) == {"jogo antigo"}
        clock[0] = 111.0
        assert await cog._get_blacklisted_games(123) == {"jogo antigo", "jogo novo"}
        assert db.call_count == 2
    asyncio.run(scenario())


def test_blacklist_cache_is_isolated_per_guild(monkeypatch):
    async def scenario():
        db = Mock(side_effect=lambda guild_id: [f"Jogo {guild_id}"])
        monkeypatch.setattr(record_module, "getBlacklistedGames", db)
        cog = record_module.RecordsCog.__new__(record_module.RecordsCog)
        cog.blacklisted_games_by_guild = {}
        cog._blacklist_refreshed_at = {}
        cog._blacklist_ttl_seconds = 10.0
        cog._blacklist_loads = {}

        assert await cog._get_blacklisted_games(1) == {"jogo 1"}
        assert await cog._get_blacklisted_games(2) == {"jogo 2"}
        assert db.call_count == 2
    asyncio.run(scenario())
