"""Administrator alert attribution regression tests (no Discord or DB needed)."""

from types import SimpleNamespace

from core.command_error_alerts import format_command_error_alert


def test_alert_includes_stable_discord_ids_and_full_command_name():
    ctx = SimpleNamespace(
        channel=SimpleNamespace(id=123456789012345670, name="geral"),
        channel_id=123456789012345670,
        user=SimpleNamespace(id=123456789012345671, display_name="TDM"),
        guild=SimpleNamespace(id=123456789012345672),
        guild_id=123456789012345672,
        id=123456789012345673,
        command=SimpleNamespace(name="eventos", qualified_name="eventos"),
    )
    text = format_command_error_alert(ctx, ValueError("missing event date"))
    assert "**Canal**: geral (ID: 123456789012345670)" in text
    assert "**Usuário**: TDM (Discord ID: 123456789012345671)" in text
    assert "**Servidor**: 123456789012345672" in text
    assert "**Interação**: 123456789012345673" in text
    assert "**Comando**: eventos" in text
    assert "**Tipo do erro**: ValueError" in text
    assert "**Erro**: missing event date" in text


def test_error_notification_handles_missing_context_without_guessing_ids():
    ctx = SimpleNamespace(
        channel=None, user=None, command=None, guild_id=None, channel_id=None, id=None
    )
    text = format_command_error_alert(ctx, RuntimeError("failed"))
    assert "Discord ID: indisponível" in text
    assert "**Servidor**: indisponível" in text
    assert "**Interação**: indisponível" in text


def test_error_text_is_truncated_without_hiding_identity_fields():
    ctx = SimpleNamespace(
        channel=SimpleNamespace(id=1, name="geral"),
        user=SimpleNamespace(id=2, display_name="TDM"),
        guild_id=3, channel_id=1, id=4,
        command=SimpleNamespace(name="eventos"),
    )
    text = format_command_error_alert(ctx, RuntimeError("X" * 5000))
    assert "**Interação**: 4" in text
    assert "**Usuário**: TDM (Discord ID: 2)" in text
    assert text.endswith("…")
    assert len(text) < 2400
