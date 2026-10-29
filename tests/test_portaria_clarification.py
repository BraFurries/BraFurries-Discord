"""Discord-only exceptional ticket regression tests; no Discord or real DB."""
import asyncio
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from core import portaria_views, portaria_clarification_store as store


class Cursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []
        self.rowcount = 1
        self.lastrowid = 42

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


def patch_db(monkeypatch, cursor):
    @contextmanager
    def connection():
        yield cursor
    monkeypatch.setattr(store, "pooled_connection", connection)


def test_closed_feature_does_not_offer_discord_button(monkeypatch):
    monkeypatch.setattr(portaria_views, "ticket_settings",
                        lambda _id: {"clarification_tickets_enabled": 0})
    async def make():
        return portaria_views.PortariaDecisionView(7, guild_id=100)
    view = asyncio.run(make())
    assert [b.label for b in view.children] == ["Aprovar", "Reprovar"]


def test_enabled_feature_adds_clarification_button_to_discord_form(monkeypatch):
    monkeypatch.setattr(portaria_views, "ticket_settings",
                        lambda _id: {"clarification_tickets_enabled": 1})
    async def make():
        return portaria_views.PortariaDecisionView(7, guild_id=100)
    view = asyncio.run(make())
    assert [b.label for b in view.children] == [
        "Aprovar", "Reprovar", "Solicitar esclarecimentos"
    ]
    assert view.children[2].custom_id == "portaria_clarification_open:7"


def test_reserve_rejects_wrong_guild_before_creating_ticket(monkeypatch):
    cursor = Cursor()  # form JOIN returns nothing, even if an ID was guessed
    patch_db(monkeypatch, cursor)
    with pytest.raises(ValueError, match="não encontrada"):
        store.reserve_ticket(19, 222, 333, "Preciso esclarecer uma divergência")
    sql, params = cursor.calls[0]
    assert "fs.guild_id = %s" in sql
    assert "ff.type = 'portaria'" in sql
    assert "cd.active = 1" in sql
    assert params == (19, 222)
    assert all("INSERT INTO portaria_clarification_tickets" not in q
               for q, _ in cursor.calls)


def test_reserve_serializes_and_reuses_existing_ticket(monkeypatch):
    cursor = Cursor([
        {"id": 19, "user_id": 777, "community_id": 3},
        {"clarification_tickets_enabled": 1,
         "clarification_ticket_category_id": 999},
        {"id": 42, "submission_id": 19, "status": "OPEN",
         "discord_channel_id": 102, "applicant_discord_user_id": 777},
    ])
    patch_db(monkeypatch, cursor)
    ticket = store.reserve_ticket(19, 222, 333,
                                  "Preciso esclarecer uma divergência")
    assert ticket["id"] == 42
    assert ticket["status"] == "OPEN"
    assert sum("FOR UPDATE" in sql for sql, _ in cursor.calls) == 3
    assert all("INSERT INTO portaria_clarification_tickets" not in sql
               for sql, _ in cursor.calls)




def test_reserve_rejects_second_active_form_for_same_applicant(monkeypatch):
    cursor = Cursor([
        {"id": 20, "user_id": 777, "community_id": 3},
        {"clarification_tickets_enabled": 1,
         "clarification_ticket_category_id": 999},
        {"id": 42, "submission_id": 19, "status": "OPEN",
         "discord_channel_id": 102, "applicant_discord_user_id": 777},
    ])
    patch_db(monkeypatch, cursor)
    with pytest.raises(ValueError, match="outra ficha"):
        store.reserve_ticket(20, 222, 333, "Outra dúvida sobre o candidato")
    assert "applicant_discord_user_id = %s" in cursor.calls[2][0]
    assert cursor.calls[2][1] == (3, 222, 777)
    assert all("INSERT INTO portaria_clarification_tickets" not in sql
               for sql, _ in cursor.calls)


def test_closed_history_allows_a_second_ticket_same_submission(monkeypatch):
    cursor = Cursor([
        {"id": 19, "user_id": 777, "community_id": 3},
        {"clarification_tickets_enabled": 1,
         "clarification_ticket_category_id": 999},
        None,
    ])
    patch_db(monkeypatch, cursor)
    ticket = store.reserve_ticket(19, 222, 333, "Novo esclarecimento após o encerramento")
    assert ticket["status"] == "REQUESTED"
    assert ticket["id"] == 42
    assert "status <> 'CLOSED'" in cursor.calls[2][0]
    assert "INSERT INTO portaria_clarification_tickets" in cursor.calls[3][0]

def test_disabled_portaria_fails_closed_inside_locked_reservation(monkeypatch):
    cursor = Cursor([
        {"id": 19, "user_id": 777, "community_id": 3},
        {"clarification_tickets_enabled": 0,
         "clarification_ticket_category_id": None}
    ])
    patch_db(monkeypatch, cursor)
    with pytest.raises(ValueError, match="não estão habilitados"):
        store.reserve_ticket(19, 222, 333, "Precisamos confirmar informações")


def test_claim_open_has_single_operator_lease_with_restart_fallback(monkeypatch):
    cursor = Cursor()
    patch_db(monkeypatch, cursor)
    assert store.claim_open(42, 222)
    sql, params = cursor.calls[0]
    assert "opening_attempted_at" in sql
    assert "INTERVAL 10 MINUTE" in sql
    assert params == (42, 222)
    cursor.rowcount = 0
    assert not store.claim_open(42, 222)




def test_decision_database_lock_prevents_ticket_race(monkeypatch):
    """A queued approval cannot bypass an opened ticket after acquiring the form lock."""
    from core import database
    cursor = Cursor([{"status": "pending"}, {"id": 42}])
    @contextmanager
    def connection():
        yield cursor
    monkeypatch.setattr(database, "pooled_connection", connection)
    with pytest.raises(ValueError, match="Encerre o ticket"):
        database.record_form_decision(19, "approved", 888)
    assert "FOR UPDATE" in cursor.calls[0][0]
    assert "portaria_clarification_tickets" in cursor.calls[1][0]
    assert all("INSERT INTO form_decisions" not in sql for sql, _ in cursor.calls)


def test_normal_form_decision_works_without_active_clarification(monkeypatch):
    from core import database
    cursor = Cursor([{"status": "pending"}, None])
    @contextmanager
    def connection():
        yield cursor
    monkeypatch.setattr(database, "pooled_connection", connection)
    database.record_form_decision(19, "approved", 888)
    assert any("INSERT INTO form_decisions" in sql for sql, _ in cursor.calls)
    assert any("UPDATE form_submissions" in sql for sql, _ in cursor.calls)


def test_created_channel_is_recorded_before_discord_message_is_posted(monkeypatch):
    cursor = Cursor()
    patch_db(monkeypatch, cursor)
    assert store.remember_channel(42, 222, 999)
    sql, params = cursor.calls[0]
    assert "discord_channel_id = %s" in sql
    assert "status = 'OPENING'" in sql
    assert params == (999, 42, 222, 999)


def test_unreconciled_failed_ticket_with_channel_blocks_decision(monkeypatch):
    cursor = Cursor([{"id": 42}])
    patch_db(monkeypatch, cursor)
    assert store.has_active_ticket(19, 222)
    sql, params = cursor.calls[0]
    assert "status = 'FAILED' AND discord_channel_id IS NOT NULL" in sql
    assert params == (19, 222)


def test_failed_ticket_without_channel_can_be_retried(monkeypatch):
    cursor = Cursor()
    patch_db(monkeypatch, cursor)
    assert store.claim_open(42, 222)
    sql, params = cursor.calls[0]
    assert "status IN ('REQUESTED','FAILED')" in sql
    assert params == (42, 222)


def test_missing_saved_channel_is_reset_with_compare_and_swap(monkeypatch):
    cursor = Cursor()
    patch_db(monkeypatch, cursor)
    assert store.reset_missing_open_channel(42, 222, 999)
    sql, params = cursor.calls[0]
    assert "status = 'OPEN'" in sql
    assert "discord_channel_id = %s" in sql
    assert "discord_channel_id = NULL" in sql
    assert params == (42, 222, 999)


def test_channel_recovery_never_trusts_another_guild_or_topic(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core import portaria_clarification as handler

    class FakeTextChannel:
        def __init__(self, topic, guild_id):
            self.topic = topic
            self.guild = SimpleNamespace(id=guild_id)

    monkeypatch.setattr(handler.discord, "TextChannel", FakeTextChannel)
    ticket_topic = handler._ticket_topic(42, 19)
    wrong_channel = FakeTextChannel("unrelated ticket", 222)
    guild = SimpleNamespace(
        id=222,
        get_channel=lambda _: wrong_channel,
        fetch_channel=AsyncMock(),
        text_channels=[FakeTextChannel(ticket_topic, 222)],
        fetch_channels=AsyncMock(return_value=[]),
    )

    with pytest.raises(ValueError, match="não corresponde"):
        asyncio.run(handler._existing_channel(guild, 42, 19, 999))
    guild.fetch_channel.assert_not_awaited()
    guild.fetch_channels.assert_not_awaited()


def test_channel_recovery_uses_discord_list_if_saved_channel_was_deleted(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core import portaria_clarification as handler

    class FakeTextChannel:
        def __init__(self, topic, guild_id):
            self.topic = topic
            self.guild = SimpleNamespace(id=guild_id)

    monkeypatch.setattr(handler.discord, "TextChannel", FakeTextChannel)
    matched = FakeTextChannel(handler._ticket_topic(42, 19), 222)
    guild = SimpleNamespace(
        id=222,
        get_channel=lambda _: None,
        fetch_channel=AsyncMock(side_effect=handler.discord.NotFound(
            Mock(status=404, reason="Not Found"), "gone")),
        text_channels=[],
        fetch_channels=AsyncMock(return_value=[matched]),
    )
    assert asyncio.run(handler._existing_channel(guild, 42, 19, 999)) is matched
    guild.fetch_channels.assert_awaited_once()
