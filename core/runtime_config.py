from __future__ import annotations

import os


FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


def env_flag_enabled(name: str, *, default: bool = True) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default

    return raw_value.strip().lower() not in FALSE_VALUES


def get_optional_discord_snowflake(name: str) -> int | None:
    """Load a legacy Discord ID from the environment without a hardcoded fallback.

    Unset or invalid input is disabled rather than selecting a different guild,
    member, channel or role.
    """
    raw = os.getenv(name, "").strip()
    if not raw or not raw.isascii() or not raw.isdecimal() or len(raw) > 20:
        return None
    value = int(raw)
    return value if value > 0 else None


def get_database_name() -> str:
    value = os.getenv("BOT_DATABASE_NAME")
    return value.strip() if isinstance(value, str) and value.strip() else "coddy"


def get_ai_sponsored_fallback_guilds() -> frozenset[int]:
    """Return the explicitly authorized guild IDs, or none for invalid config.

    This parser is deliberately fail-closed: one malformed item makes the
    sponsored fallback unavailable instead of accidentally widening its scope.
    """

    raw_value = os.getenv("AI_SPONSORED_FALLBACK_GUILDS", "")
    items = [item.strip() for item in raw_value.split(",")]
    if not raw_value.strip() or not all(items):
        return frozenset()

    if any(not item.isdecimal() or int(item) <= 0 for item in items):
        return frozenset()
    return frozenset(int(item) for item in items)


def is_ai_sponsored_fallback_enabled_for_guild(guild_id: object) -> bool:
    """Whether a guild is opted into the project-sponsored AI fallback."""

    raw_flag = os.getenv("AI_SPONSORED_FALLBACK_ENABLED", "")
    if raw_flag.strip().lower() not in TRUE_VALUES:
        return False
    if not isinstance(guild_id, int) or isinstance(guild_id, bool):
        return False
    return guild_id in get_ai_sponsored_fallback_guilds()
