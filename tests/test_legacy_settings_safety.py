"""Isolated tests for sanitized legacy settings.

Never use production credentials. These tests only patch process-local variables.
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import patch

import settings


def test_legacy_discord_settings_fail_closed_when_unconfigured() -> None:
    with patch.dict(os.environ, {}, clear=True):
        config = importlib.reload(settings)
        assert config.BOT_NAME == "Coddy"
        assert config.DISCORD_GUILD_ID == 0
        assert config.DISCORD_ADMINS == []
        assert config.DISCORD_VIP_ROLES_ID == []
        assert config.DISCORD_TEST_CHANNEL == 0
        assert config.TELEGRAM_ADMIN == ""
        assert config.INSTAGRAM_TOKEN == ""
    importlib.reload(settings)


def test_legacy_discord_ids_load_from_synthetic_environment() -> None:
    with patch.dict(
        os.environ,
        {
            "DISCORD_GUILD_ID": "123456789012345678",
            "DISCORD_ADMINS": "123456789012345679, 123456789012345680",
            "DISCORD_VIP_ROLES_ID": "123456789012345681,123456789012345682",
            "DISCORD_TEST_CHANNEL": "123456789012345683",
            "TELEGRAM_ADMIN": "synthetic-admin",
        },
        clear=True,
    ):
        config = importlib.reload(settings)
        assert config.DISCORD_GUILD_ID == 123456789012345678
        assert config.DISCORD_ADMINS == [123456789012345679, 123456789012345680]
        assert config.DISCORD_VIP_ROLES_ID == [[123456789012345681], [123456789012345682]]
        assert config.DISCORD_TEST_CHANNEL == 123456789012345683
        assert config.TELEGRAM_ADMIN == "synthetic-admin"
    importlib.reload(settings)


def test_malformed_legacy_discord_ids_are_not_accepted() -> None:
    with patch.dict(
        os.environ,
        {
            "DISCORD_GUILD_ID": "not-a-snowflake",
            "DISCORD_ADMINS": "123, invalid",
            "DISCORD_VIP_ROLES_ID": "123,,456",
            "DISCORD_TEST_CHANNEL": "-1",
            "DISCORD_MEMBER_NOT_VERIFIED_ROLE": "9" * 30,
        },
        clear=True,
    ):
        config = importlib.reload(settings)
        assert config.DISCORD_GUILD_ID == 0
        assert config.DISCORD_ADMINS == []
        assert config.DISCORD_VIP_ROLES_ID == []
        assert config.DISCORD_TEST_CHANNEL == 0
        assert config.DISCORD_MEMBER_NOT_VERIFIED_ROLE == 0
    importlib.reload(settings)
