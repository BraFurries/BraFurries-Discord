from core.moderation_validation import BAN_REASON_MAX_LENGTH, valid_ban_reason


def test_detailed_reason_from_moderation_incident_fits_expanded_capacity():
    reason = (
        "Solicitação de conteúdo íntimo explícito a um menor de 13 anos. "
        "A denúncia também inclui um relato público em vídeo no YouTube "
        "no qual o usuário afirma ter praticado condutas semelhantes "
        "anteriormente e declara que não voltaria a fazê-las. "
        "Diante das evidências apresentadas e da gravidade da conduta denunciada, "
        "foi aplicado banimento permanente para proteger os membros da comunidade."
    )
    assert len(reason) == 386
    assert valid_ban_reason(reason)


def test_reason_validation_handles_empty_and_boundary_values():
    assert BAN_REASON_MAX_LENGTH == 2048
    assert not valid_ban_reason(None)
    assert not valid_ban_reason("")
    assert not valid_ban_reason("   ")
    assert valid_ban_reason("a")
    assert valid_ban_reason("x" * BAN_REASON_MAX_LENGTH)
    assert valid_ban_reason("🦊" * BAN_REASON_MAX_LENGTH)
    assert not valid_ban_reason("x" * (BAN_REASON_MAX_LENGTH + 1))
    assert not valid_ban_reason("🦊" * (BAN_REASON_MAX_LENGTH + 1))
