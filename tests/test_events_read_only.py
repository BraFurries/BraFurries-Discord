from pathlib import Path


EVENTS_COG_SOURCE = Path(__file__).resolve().parents[1] / "cogs" / "events.py"


def test_events_cog_keeps_public_read_only_commands():
    source = EVENTS_COG_SOURCE.read_text(encoding="utf-8")

    assert "@app_commands.command(name='eventos'" in source
    assert "@app_commands.command(name=f'evento'" in source


def test_events_cog_does_not_register_admin_mutation_group():
    source = EVENTS_COG_SOURCE.read_text(encoding="utf-8")

    assert "app_commands.Group(name=\"admin-event\"" not in source
    assert "@event_admin.command" not in source
    assert "includeEvent(" not in source
    assert "approveEventById(" not in source
    assert "rescheduleEventDate(" not in source
    assert "scheduleNextEventDate(" not in source
