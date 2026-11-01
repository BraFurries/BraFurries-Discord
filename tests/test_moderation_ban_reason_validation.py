from urllib.parse import quote

import pytest

from core.moderation_validation import (
    BAN_REASON_MAX_LENGTH,
    BAN_REASON_DETAILS_MAX_LENGTH,
    DISCORD_AUDIT_REASON_MAX_ENCODED_LENGTH,
    audit_reason_encoded_length,
    discord_audit_reason,
    normalize_ban_reason_details,
    valid_ban_reason,
    valid_legacy_ban_reason,
)


REPORT_REASON = (
    "Solicitação de conteúdo íntimo explícito a um menor de 13 anos. "
    "A denúncia também inclui um relato público em vídeo no YouTube no qual "
    "o usuário afirma ter praticado condutas semelhantes anteriormente e "
    "declara que não voltaria a fazê-las. Diante das evidências apresentadas "
    "e da gravidade da conduta denunciada, foi aplicado banimento permanente "
    "para proteger os membros da comunidade."
)


def test_public_reason_is_validated_separately_from_url_encoded_audit_header():
    assert BAN_REASON_MAX_LENGTH == 512
    assert valid_ban_reason("x" * 512)
    assert not valid_ban_reason("x" * 513)
    assert valid_ban_reason("🦊" * 512)
    assert not valid_ban_reason("🦊" * 513)
    assert valid_ban_reason("á" * 100)
    assert valid_ban_reason("a")
    for invalid in (None, "", "   ", "a" * 2048):
        assert not valid_ban_reason(invalid)


def test_exact_report_rejected_by_old_validator_now_preserves_full_public_reason():
    assert len(REPORT_REASON) == 386
    assert len(REPORT_REASON.encode("utf-8")) == 399
    assert audit_reason_encoded_length(REPORT_REASON) == 569
    assert valid_ban_reason(REPORT_REASON)

    audit = discord_audit_reason(REPORT_REASON)
    assert audit.startswith("Solicitação de conteúdo íntimo explícito")
    assert audit.endswith("... [motivo completo no registro do Coddy]")
    assert len(quote(audit, safe="")) <= 512
    assert REPORT_REASON.endswith("para proteger os membros da comunidade.")


def test_discord_audit_header_is_exact_if_it_fits_and_marked_if_it_does_not():
    assert DISCORD_AUDIT_REASON_MAX_ENCODED_LENGTH == 512
    assert audit_reason_encoded_length(" ") == 3
    assert audit_reason_encoded_length("á") == len(quote("á", safe="")) == 6
    assert audit_reason_encoded_length("🦊") == 12
    assert discord_audit_reason("x" * 512) == "x" * 512
    assert discord_audit_reason("ação grave") == "ação grave"
    assert discord_audit_reason("x" * 511 + " ") != "x" * 511 + " "

    unicode_audit = discord_audit_reason("🦊" * 50)
    assert unicode_audit.startswith("🦊")
    assert unicode_audit.endswith("... [motivo completo no registro do Coddy]")
    assert audit_reason_encoded_length(unicode_audit) <= 512
    assert audit_reason_encoded_length(discord_audit_reason("é" * 100)) <= 512

    for invalid in ("", "  ", "a" * 513):
        with pytest.raises(ValueError):
            discord_audit_reason(invalid)


def test_optional_private_reason_details_are_not_truncated():
    assert BAN_REASON_DETAILS_MAX_LENGTH == 2048
    assert normalize_ban_reason_details(None) is None
    assert normalize_ban_reason_details("") is None
    assert normalize_ban_reason_details("   ") is None
    details = "Relato reservado com evidências. " * 50
    assert normalize_ban_reason_details(details) == details
    assert normalize_ban_reason_details("x" * 2048) == "x" * 2048
    with pytest.raises(ValueError):
        normalize_ban_reason_details("x" * 2049)


def test_report_can_be_private_while_the_public_reason_is_short():
    short = "Violação grave das regras da comunidade."
    details = (
        "A denúncia também inclui um relato público em vídeo no YouTube "
        "no qual o usuário afirma ter praticado condutas semelhantes "
        "anteriormente. A staff revisou as evidências antes de decidir."
    )
    assert valid_ban_reason(short)
    assert normalize_ban_reason_details(details) == details


def test_legacy_import_preserves_older_reason_without_relaxing_new_bans():
    reason = "Descrição histórica com acentuação e contexto. " * 20
    assert 512 < len(reason) < 2048
    assert valid_legacy_ban_reason(reason)
    assert not valid_ban_reason(reason)
    assert not valid_legacy_ban_reason("x" * 2049)
    assert not valid_legacy_ban_reason("")
