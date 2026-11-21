"""Minimal attribution for administrator-only Discord command error alerts.

This does not persist incidents or send messages to end users. Discord
snowflake IDs let staff distinguish users with identical display names.
"""


def _id(value) -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else "indisponível"


def format_command_error_alert(interaction, error: object) -> str:
    """Build the admin Telegram alert without needing Discord or database access."""
    channel = getattr(interaction, "channel", None)
    user = getattr(interaction, "user", None)
    guild = getattr(interaction, "guild", None)
    command = getattr(interaction, "command", None)
    channel_name = getattr(channel, "name", None) or str(channel or "desconhecido")
    user_name = getattr(user, "display_name", None) or str(user or "desconhecido")
    command_name = getattr(command, "qualified_name", None) or getattr(command, "name", None) or "desconhecido"
    user_id = _id(getattr(user, "id", None))
    guild_id = _id(getattr(interaction, "guild_id", None) or getattr(guild, "id", None))
    channel_id = _id(getattr(interaction, "channel_id", None) or getattr(channel, "id", None))
    interaction_id = _id(getattr(interaction, "id", None))

    # Telegram sendMessage has a 4096-character limit. Keep operational IDs
    # visible even for extremely long exception messages.
    error_text = str(error)
    if len(error_text) > 1800:
        error_text = error_text[:1800] + "…"

    return (
        "Coddy apresentou um erro:\n"
        f"**Canal**: {channel_name} (ID: {channel_id})\n"
        f"**Usuário**: {user_name} (Discord ID: {user_id})\n"
        f"**Servidor**: {guild_id}\n"
        f"**Interação**: {interaction_id}\n"
        f"**Comando**: {command_name}\n"
        f"**Tipo do erro**: {type(error).__name__}\n"
        f"**Erro**: {error_text}"
    )
