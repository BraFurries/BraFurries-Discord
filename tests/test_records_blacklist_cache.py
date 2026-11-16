import asyncio
from datetime import datetime
from unittest.mock import Mock

import cogs.records as record_module


def make_cog():
    cog = record_module.RecordsCog.__new__(record_module.RecordsCog)
    cog.blacklisted_games_global = None
    cog._blacklist_refreshed_at = 0.0
    cog._blacklist_ttl_seconds = 10.0
    cog._blacklist_load = None
    cog.game_sessions = {}
    return cog


def test_blacklist_cache_reloads_after_admin_edits_without_restart(monkeypatch):
    async def scenario():
        current = ["Jogo antigo"]
        db = Mock(side_effect=lambda guild_id: list(current))
        monkeypatch.setattr(record_module, "getBlacklistedGames", db)
        clock = [100.0]
        monkeypatch.setattr(record_module.time, "monotonic", lambda: clock[0])
        cog = make_cog()
        cog.game_sessions[(123, 456)] = (datetime(2026, 1, 1), "JoGo NoVo")

        assert await cog._get_blacklisted_games(123) == {"jogo antigo"}
        current.append("Jogo novo")
        assert await cog._get_blacklisted_games(123) == {"jogo antigo"}
        clock[0] = 111.0
        assert await cog._get_blacklisted_games(123) == {"jogo antigo", "jogo novo"}
        assert (123, 456) not in cog.game_sessions
        assert db.call_count == 2

        # Removing a game does not revive the discarded session.
        current.remove("Jogo novo")
        clock[0] = 122.0
        assert await cog._get_blacklisted_games(123) == {"jogo antigo"}
        assert (123, 456) not in cog.game_sessions
    asyncio.run(scenario())


def test_blacklist_policy_and_query_are_global_across_guilds(monkeypatch):
    async def scenario():
        db = Mock(return_value=["Jogo global"])
        monkeypatch.setattr(record_module, "getBlacklistedGames", db)
        cog = make_cog()
        results = await asyncio.gather(
            cog._get_blacklisted_games(1),
            cog._get_blacklisted_games(2),
            cog._get_blacklisted_games(3)
        )
        assert results == [{"jogo global"}] * 3
        assert db.call_count == 1
        assert await cog._get_blacklisted_games(4) == {"jogo global"}
        assert db.call_count == 1
    asyncio.run(scenario())


def test_failure_does_not_replace_last_valid_policy(monkeypatch):
    async def scenario():
        clock = [100.0]
        monkeypatch.setattr(record_module.time, "monotonic", lambda: clock[0])
        db = Mock(side_effect=[["blocked"], RuntimeError("db unavailable")])
        monkeypatch.setattr(record_module, "getBlacklistedGames", db)
        cog = make_cog()
        assert await cog._get_blacklisted_games(1) == {"blocked"}
        clock[0] = 111.0
        try:
            await cog._get_blacklisted_games(2)
            assert False, "expected refresh failure"
        except RuntimeError:
            pass
        assert cog.blacklisted_games_global == {"blocked"}
        assert cog._blacklist_load is None
    asyncio.run(scenario())
