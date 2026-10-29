"""Pure moderation-input validations; no Discord or database side effects."""

# Discord embed field.value accepts up to 1024 UTF-16 code units; the
# persistence column deliberately has a larger capacity (VARCHAR(2048)).
BAN_REASON_MAX_LENGTH = 1024


def discord_embed_text_length(value: str) -> int:
    """Count Discord-compatible string units, including astral emoji pairs."""
    return len(value.encode("utf-16-le")) // 2


def valid_ban_reason(reason: str | None) -> bool:
    """Ensure the complete reason fits the ban and identity-propagation logs."""
    return bool(reason and reason.strip()) and (
        discord_embed_text_length(reason) <= BAN_REASON_MAX_LENGTH
    )
