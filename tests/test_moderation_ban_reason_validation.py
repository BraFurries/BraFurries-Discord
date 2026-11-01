from urllib.parse import quote

import pytest

from core.moderation_validation import (
    AuditReasonTooLong,
    BAN_REASON_MAX_LENGTH,
    BAN_REASON_DETAILS_MAX_LENGTH,
    DISCORD_AUDIT_REASON_MAX_ENCODED_LENGTH,
    audit_reason_encoded_length,
    ban_audit_reason_validation_message,
    ban_reason_validation_message,
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
    assert len(quote(REPORT_REASON, safe="")) == 569  # old, overly restrictive count
    assert audit_reason_encoded_length(REPORT_REASON) == 453  # actual discord.py header
    assert valid_ban_reason(REPORT_REASON)

    audit = discord_audit_reason(REPORT_REASON)
    assert audit == REPORT_REASON  # no unnecessary abbreviation
    assert len(quote(audit, safe="/ ")) == 453
    assert REPORT_REASON.endswith("para proteger os membros da comunidade.")


def test_discord_audit_header_is_exact_if_it_fits_and_normalized_when_necessary():
    assert DISCORD_AUDIT_REASON_MAX_ENCODED_LENGTH == 512
    assert audit_reason_encoded_length(" ") == 1
    assert audit_reason_encoded_length("/") == 1
    assert audit_reason_encoded_length("á") == len(quote("á", safe="")) == 6
    assert audit_reason_encoded_length("🦊") == 12
    assert discord_audit_reason("x" * 512) == "x" * 512
    assert discord_audit_reason("ação grave") == "ação grave"
    assert discord_audit_reason("x" * 511 + " ") == "x" * 511 + " "

    # Do not drop letters or emojis: only remove Latin accents in the audit.
    assert discord_audit_reason("é" * 100) == "e" * 100
    assert discord_audit_reason("á" * 512) == "a" * 512
    assert discord_audit_reason("—" * 100) == "-" * 100
    assert discord_audit_reason("é" * 80 + "   " * 60 + "texto") == "e" * 80 + " texto"
    assert discord_audit_reason("e\u0301" * 100) == "e" * 100

    for invalid in ("", "  ", "a" * 513):
        with pytest.raises(ValueError):
            discord_audit_reason(invalid)


def test_unsimplifiable_reason_fails_closed_without_truncating_or_erasing_symbols():
    for reason in ("🦊" * 50, "你好" * 100, "é" * 80 + "🦊" * 40):
        assert valid_ban_reason(reason)
        assert audit_reason_encoded_length(reason) > 512
        with pytest.raises(AuditReasonTooLong):
            discord_audit_reason(reason)

    explanation = ban_audit_reason_validation_message()
    assert "registro de auditoria do Discord" in explanation
    assert "Resuma o texto" in explanation
    assert "details" in explanation
    assert "Nenhum banimento foi aplicado" in explanation
    assert "UTF-8" not in explanation


def test_invalid_reason_message_is_friendly_and_reports_the_actual_length():
    too_long = "a" * 513
    explanation = ban_reason_validation_message(too_long)
    assert "513 caracteres" in explanation
    assert "512" in explanation
    assert "Resuma" in explanation
    assert "details" in explanation
    assert "codificação" not in explanation
    assert "UTF-8" not in explanation

    missing = ban_reason_validation_message("  ")
    assert "informar o motivo" in missing
    assert "512 caracteres" in missing
    assert "escreveu" not in missing


def test_slash_command_rejects_unfittable_audit_before_any_ban_side_effect():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "cogs" / "moderation.py"
    ).read_text(encoding="utf-8")
    slash = source.split("@app_commands.command(name='ban'", 1)[1]
    slash = slash.split("@app_commands.command(name=f'notas_add'", 1)[0]
    assert slash.index("audit_reason = discord_audit_reason(reason)") < slash.index(
        "ban_id = registerUserBan("
    )
    assert slash.index("except AuditReasonTooLong:") < slash.index(
        "await ctx.response.send_message(content='Processando banimento...'"
    )
    assert "ban_audit_reason_validation_message()" in slash


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
