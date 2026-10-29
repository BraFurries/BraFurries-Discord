"""Side-effect-free validation for one ban's public reason and private details."""

from urllib.parse import quote

# X-Audit-Log-Reason accepts <= 512 URL-encoded UTF-8 characters.
# No prefixes/truncation may be added downstream. Embed fields support 1024.
BAN_REASON_MAX_LENGTH = 512
BAN_REASON_DETAILS_MAX_LENGTH = 2048


def audit_reason_encoded_length(reason: str) -> int:
    """Conservatively count URL-encoded UTF-8 characters for Discord audit logs."""
    return len(quote(reason, safe=""))


def valid_ban_reason(reason: str | None) -> bool:
    """A nonblank reason must fit BOTH the audit log and normal Discord embeds."""
    return bool(reason and reason.strip()) and (
        audit_reason_encoded_length(reason) <= BAN_REASON_MAX_LENGTH
    )


def normalize_ban_reason_details(details: str | None) -> str | None:
    """Optional context is private; reject invalid size without truncating."""
    if details is None or not details.strip():
        return None
    if len(details) > BAN_REASON_DETAILS_MAX_LENGTH:
        raise ValueError("ban reason details exceed 2048 characters")
    return details
