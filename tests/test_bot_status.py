from datetime import datetime, timezone

from core.bot_status import BotStatusSnapshot, build_status_payload


def test_build_status_payload_marks_online_bot_and_rounds_metrics():
    snapshot = BotStatusSnapshot(
        bot_name='Coddy',
        ready=True,
        connected=True,
        guild_count=4,
        user_count=120,
        commands_loaded=18,
        cogs_loaded=12,
        latency_ms=123.4567,
        uptime_seconds=3600.1234,
        started_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
        last_ready_at=datetime(2026, 7, 1, 12, 5, 0, tzinfo=timezone.utc),
        python_version='3.12.4',
        discord_py_version='2.3.2',
        process_id=1234,
    )

    payload = build_status_payload(snapshot)

    assert payload['ok'] is True
    assert payload['state'] == 'online'
    assert payload['latency_ms'] == 123.46
    assert payload['uptime_seconds'] == 3600.12
    assert payload['guilds'] == 4
    assert payload['users'] == 120
    assert payload['started_at'] == '2026-07-01T12:00:00+00:00'
    assert payload['memory_used_mb'] is None
    assert payload['memory_limit_mb'] is None
    assert payload['cpu_usage_percent'] is None


def test_build_status_payload_marks_offline_bot_without_latency():
    snapshot = BotStatusSnapshot(
        bot_name='Coddy',
        ready=False,
        connected=False,
        guild_count=0,
        user_count=0,
        commands_loaded=0,
        cogs_loaded=0,
        latency_ms=None,
        uptime_seconds=1.2,
        started_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
        last_ready_at=None,
        python_version='3.12.4',
        discord_py_version='2.3.2',
        process_id=1234,
    )

    payload = build_status_payload(snapshot)

    assert payload['ok'] is False
    assert payload['state'] == 'offline'
    assert payload['latency_ms'] is None
    assert payload['last_ready_at'] is None
