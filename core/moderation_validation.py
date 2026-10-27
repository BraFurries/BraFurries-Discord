"""Pure moderation-input validations; no Discord or database side effects."""

BAN_REASON_MAX_LENGTH = 2048


def valid_ban_reason(reason: str | None) -> bool:
    """Match user_bans.reason VARCHAR(2048) without silently truncating evidence."""
    return bool(reason and reason.strip()) and len(reason) <= BAN_REASON_MAX_LENGTH
