"""Side-effect-free validation for ban reasons, Discord audit headers and details."""

import re
import unicodedata
from urllib.parse import quote

# The administrative reason and optional private details are stored verbatim.
# Discord's audit header has a separate URL-encoded UTF-8 size limit.
BAN_REASON_MAX_LENGTH = 512
BAN_REASON_DETAILS_MAX_LENGTH = 2048
DISCORD_AUDIT_REASON_MAX_ENCODED_LENGTH = 512

# Only visual typography is replaced. Emoji, other scripts, numbers and
# substantive words must never be dropped to make an audit reason fit.
_AUDIT_TYPOGRAPHY = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "‑": "-", "…": "...",
})


class AuditReasonTooLong(ValueError):
    """The Discord audit header cannot carry the complete reason, even simplified."""


def audit_reason_encoded_length(reason: str) -> int:
    """Count the header value exactly as encoded by discord.py.

    Discord.py uses quote(reason, safe="/ "), keeping spaces and / literal.
    """
    return len(quote(reason, safe="/ "))


def valid_ban_reason(reason: str | None) -> bool:
    """Validate administrative input independently of Discord header encoding."""
    return bool(reason and reason.strip()) and len(reason) <= BAN_REASON_MAX_LENGTH


def ban_reason_validation_message(reason: str | None) -> str:
    """Explain invalid input in plain language, without transport details."""
    if not reason or not reason.strip():
        return (
            "Você precisa informar o motivo do banimento. "
            "Escreva uma justificativa com até 512 caracteres no campo 'reason'."
        )
    return (
        f"O motivo ficou muito longo: você escreveu {len(reason)} caracteres, "
        f"mas o limite é de {BAN_REASON_MAX_LENGTH}. "
        "Resuma o texto no campo 'reason' e, se precisar, coloque informações "
        "adicionais no campo 'details' (visível apenas à equipe)."
    )


def ban_audit_reason_validation_message() -> str:
    """Give Staff an actionable explanation without exposing HTTP encodings."""
    return (
        "O motivo ainda é grande demais para o registro de auditoria do Discord, "
        "mesmo após simplificar acentos e formatação. "
        "Resuma o texto no campo 'reason' e coloque os detalhes adicionais no campo "
        "'details' (visível apenas à equipe). Nenhum banimento foi aplicado."
    )


def _simplify_latin_accents(reason: str) -> str:
    """Remove only diacritics on Latin letters; keep other scripts and emoji."""
    result: list[str] = []
    for char in reason:
        decomposed = unicodedata.normalize("NFD", char)
        if (
            len(decomposed) > 1
            and unicodedata.name(decomposed[0], "").startswith("LATIN ")
            and all(unicodedata.combining(mark) for mark in decomposed[1:])
        ):
            result.append(decomposed[0])
        else:
            result.append(char)
    return "".join(result)


def discord_audit_reason(reason: str) -> str:
    """Keep the full reason if possible, otherwise simplify without truncating.

    Only the Discord audit reason changes; persist and display the original
    administrative reason. Refuse a new ban if it still cannot fit after
    canonical Unicode normalization, accent simplification and typography.
    """
    if not valid_ban_reason(reason):
        raise ValueError("invalid public ban reason")
    if audit_reason_encoded_length(reason) <= DISCORD_AUDIT_REASON_MAX_ENCODED_LENGTH:
        return reason

    normalized = unicodedata.normalize("NFC", reason)
    if audit_reason_encoded_length(normalized) <= DISCORD_AUDIT_REASON_MAX_ENCODED_LENGTH:
        return normalized

    normalized = _simplify_latin_accents(normalized)
    normalized = re.sub(r"\s+", " ", normalized.translate(_AUDIT_TYPOGRAPHY)).strip()
    if audit_reason_encoded_length(normalized) <= DISCORD_AUDIT_REASON_MAX_ENCODED_LENGTH:
        return normalized

    raise AuditReasonTooLong("reason exceeds Discord audit header capacity after normalization")


def valid_legacy_ban_reason(reason: str | None) -> bool:
    """Preserve imported Discord bans even if they exceed the slash-command cap."""
    return bool(reason and reason.strip()) and len(reason) <= 2048


def normalize_ban_reason_details(details: str | None) -> str | None:
    """Optional context is private; reject invalid size without truncating."""
    if details is None or not details.strip():
        return None
    if len(details) > BAN_REASON_DETAILS_MAX_LENGTH:
        raise ValueError("ban reason details exceed 2048 characters")
    return details
