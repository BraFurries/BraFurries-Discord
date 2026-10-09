from core.runtime_config import (
    env_flag_enabled,
    get_database_name,
    get_optional_discord_snowflake,
)


def test_env_flag_enabled_defaults_to_enabled(monkeypatch):
    monkeypatch.delenv("BOT_DAILY_RESTART_ENABLED", raising=False)

    assert env_flag_enabled("BOT_DAILY_RESTART_ENABLED") is True


def test_env_flag_enabled_empty_value_uses_default_enabled(monkeypatch):
    monkeypatch.setenv("BOT_DAILY_RESTART_ENABLED", "  ")

    assert env_flag_enabled("BOT_DAILY_RESTART_ENABLED") is True


def test_env_flag_enabled_explicit_true_values(monkeypatch):
    for value in ("true", "TRUE", "1", "yes"):
        monkeypatch.setenv("BOT_DAILY_RESTART_ENABLED", value)

        assert env_flag_enabled("BOT_DAILY_RESTART_ENABLED") is True


def test_env_flag_enabled_explicit_false_values(monkeypatch):
    for value in ("false", "FALSE", "0", "no"):
        monkeypatch.setenv("BOT_DAILY_RESTART_ENABLED", value)

        assert env_flag_enabled("BOT_DAILY_RESTART_ENABLED") is False


def test_env_flag_enabled_invalid_value_stays_enabled(monkeypatch):
    monkeypatch.setenv("BOT_DAILY_RESTART_ENABLED", "definitely")

    assert env_flag_enabled("BOT_DAILY_RESTART_ENABLED") is True


def test_runtime_paths_and_database_name_keep_legacy_defaults(monkeypatch):
    monkeypatch.delenv("BOT_DATABASE_NAME", raising=False)

    assert get_database_name() == "coddy"


def test_runtime_paths_and_database_name_treat_blank_values_as_defaults(monkeypatch):
    monkeypatch.setenv("BOT_DATABASE_NAME", "  ")

    assert get_database_name() == "coddy"


def test_runtime_paths_and_database_name_can_be_configured(monkeypatch):
    monkeypatch.setenv("BOT_DATABASE_NAME", "brafurries_dev")

    assert get_database_name() == "brafurries_dev"


def test_optional_legacy_snowflake_absent_or_invalid_fails_closed(monkeypatch):
    monkeypatch.delenv("CODDY_CREATOR_DISCORD_ID", raising=False)
    assert get_optional_discord_snowflake("CODDY_CREATOR_DISCORD_ID") is None

    for invalid in ("", "  ", "0", "-1", "1e18", "not-an-id", "123456789012345678901"):
        monkeypatch.setenv("CODDY_CREATOR_DISCORD_ID", invalid)
        assert get_optional_discord_snowflake("CODDY_CREATOR_DISCORD_ID") is None


def test_optional_legacy_snowflake_accepts_synthetic_positive_id(monkeypatch):
    monkeypatch.setenv("CODDY_CREATOR_DISCORD_ID", " 123456789012345678 ")
    assert get_optional_discord_snowflake("CODDY_CREATOR_DISCORD_ID") == 123456789012345678
