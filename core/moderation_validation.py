"""Side-effect-free validation for one ban's reason, Discord audit header and private details."""

from urllib.parse import quote

# Ban reason: up to 512 Unicode characters, stored verbatim in MariaDB and
# included in the moderation log. Discord's URL-encoded audit header has a
# distinct, 512-character transport limit; it may need a marked abbreviation.
# discord.py URL-encodes with safe='/ ', so spaces and slashes remain literal.
BAN_REASON_MAX_LENGTH = 512
BAN_REASON_DETAILS_MAX_LENGTH = 2048
DISCORD_AUDIT_REASON_MAX_ENCODED_LENGTH = 512
_AUDIT_ABBREVIATION_SUFFIX = "... [motivo completo no registro do Coddy]"


def audit_reason_encoded_length(reason: str) -> int:
    """Count the header value encoded by discord.py (spaces and / remain safe).

    Keep this identical to discord.http.HTTPClient.request's URL-quoting
    behavior; otherwise a valid ban can be rejected unnecessarily.
    """
    return len(quote(reason, safe="/ "))


def valid_ban_reason(reason: str | None) -> bool:
    """Require a nonblank public/admin reason that fits the stored input contract.

    Audit-header encoding is not an input restriction. Call discord_audit_reason
    at the Discord effect boundary, without changing the persisted reason.
    """
    return bool(reason and reason.strip()) and len(reason) <= BAN_REASON_MAX_LENGTH


def discord_audit_reason(reason: str) -> str:
    """Build a safe, explicitly abbreviated Discord audit reason if needed.

    The original reason remains unchanged in user_bans.reason and in Coddy's
    moderation log. Only the transport-specific audit string is shortened.
    """
    if not valid_ban_reason(reason):
        raise ValueError("invalid public ban reason")
    if audit_reason_encoded_length(reason) <= DISCORD_AUDIT_REASON_MAX_ENCODED_LENGTH:
        return reason

    remaining = (
        DISCORD_AUDIT_REASON_MAX_ENCODED_LENGTH
        - audit_reason_encoded_length(_AUDIT_ABBREVIATION_SUFFIX)
    )
    prefix: list[str] = []
    for char in reason:
        encoded_length = audit_reason_encoded_length(char)
        if encoded_length > remaining:
            break
        prefix.append(char)
        remaining -= encoded_length

    shortened = "".join(prefix).rstrip() + _AUDIT_ABBREVIATION_SUFFIX
    assert audit_reason_encoded_length(shortened) <= DISCORD_AUDIT_REASON_MAX_ENCODED_LENGTH
    return shortened


def valid_legacy_ban_reason(reason: str | None) -> bool:
    """Preserve imported Discord bans that predate the short-reason contract.

    Profile synchronization imports existing sanctions; it does not issue a
    new Discord ban or audit-log message. Its only capacity limit is the
    persisted VARCHAR(2048), never the new slash-command limit.
    """
    return bool(reason and reason.strip()) and len(reason) <= 2048


def normalize_ban_reason_details(details: str | None) -> str | None:
    """Optional context is private; reject invalid size without truncating."""
    if details is None or not details.strip():
        return None
    if len(details) > BAN_REASON_DETAILS_MAX_LENGTH:
        raise ValueError("ban reason details exceed 2048 characters")
    return details
