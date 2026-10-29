from urllib.parse import quote

import pytest

from core.moderation_validation import (
    BAN_REASON_MAX_LENGTH,
    BAN_REASON_DETAILS_MAX_LENGTH,
    audit_reason_encoded_length,
    normalize_ban_reason_details,
    valid_ban_reason,
    valid_legacy_ban_reason,
)


def test_public_reason_audit_header_limit_counts_utf8_percent_escapes():
    assert BAN_REASON_MAX_LENGTH == 512
    assert valid_ban_reason("x" * 512)
    assert not valid_ban_reason("x" * 513)
    # Spaces, accents and emoji occupy more than one encoded character.
    assert audit_reason_encoded_length(" ") == 3
    assert audit_reason_encoded_length("á") == len(quote("á", safe="")) == 6
    assert audit_reason_encoded_length("🦊") == 12
    assert valid_ban_reason("🦊" * 42)
    assert not valid_ban_reason("🦊" * 43)
    assert valid_ban_reason("a")
    for invalid in (None, "", "   ", "a" * 2048):
        assert not valid_ban_reason(invalid)


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
    assert 512 < audit_reason_encoded_length(reason)
    assert len(reason) < 2048
    assert valid_legacy_ban_reason(reason)
    assert not valid_ban_reason(reason)
    assert not valid_legacy_ban_reason("x" * 2049)
    assert not valid_legacy_ban_reason("")
