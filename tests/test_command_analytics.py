from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from core import command_analytics as metrics


def test_successful_slash_command_is_one_immutable_event_without_user_data(monkeypatch):
    monkeypatch.setenv("CODDY_COMMAND_METRICS_ENABLED", "true")
    write = AsyncMock(return_value=1)
    monkeypatch.setattr(metrics, "_write", write)
    assert asyncio.run(metrics.record_completion(
        event_id=123456789012345678,
        guild_id=234567890123456789,
        command_name="admin status", kind="SLASH",
    )) is True
    query, params = write.await_args.args
    assert "INSERT INTO stats_coddy_command_executions" in query
    assert "ON DUPLICATE KEY UPDATE" in query
    assert params[:4] == (123456789012345678, 234567890123456789, "admin status", "SLASH")
    assert len(params) == 5


def test_feature_off_does_not_require_db_migration(monkeypatch):
    monkeypatch.delenv("CODDY_COMMAND_METRICS_ENABLED", raising=False)
    write = AsyncMock()
    monkeypatch.setattr(metrics, "_write", write)
    assert asyncio.run(metrics.record_completion(
        event_id=1, guild_id=None, command_name="ping", kind="PREFIX",
    )) is False
    write.assert_not_called()


def test_bad_metadata_is_rejected_without_db_access(monkeypatch):
    monkeypatch.setenv("CODDY_COMMAND_METRICS_ENABLED", "true")
    write = AsyncMock()
    monkeypatch.setattr(metrics, "_write", write)
    assert asyncio.run(metrics.record_completion(
        event_id=0, guild_id=None, command_name="ping", kind="PREFIX",
    )) is False
    assert asyncio.run(metrics.record_completion(
        event_id=1, guild_id=None, command_name="", kind="SLASH",
    )) is False
    write.assert_not_called()


def test_duplicate_event_not_counted_again(monkeypatch):
    monkeypatch.setenv("CODDY_COMMAND_METRICS_ENABLED", "true")
    write = AsyncMock(return_value=0)
    monkeypatch.setattr(metrics, "_write", write)
    assert asyncio.run(metrics.record_completion(
        event_id=1, guild_id=None, command_name="ping", kind="PREFIX",
    )) is False


def test_database_error_does_not_break_successfully_completed_command(monkeypatch):
    monkeypatch.setenv("CODDY_COMMAND_METRICS_ENABLED", "true")
    write = AsyncMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(metrics, "_write", write)
    assert asyncio.run(metrics.record_completion(
        event_id=1, guild_id=None, command_name="ping", kind="PREFIX",
    )) is False
