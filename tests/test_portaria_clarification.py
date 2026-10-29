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
        {"id": 42, "status": "OPEN", "discord_channel_id": 102,
         "applicant_discord_user_id": 777},
    ])
    patch_db(monkeypatch, cursor)
    ticket = store.reserve_ticket(19, 222, 333,
                                  "Preciso esclarecer uma divergência")
    assert ticket["id"] == 42
    assert ticket["status"] == "OPEN"
    assert sum("FOR UPDATE" in sql for sql, _ in cursor.calls) == 3
    assert all("INSERT INTO portaria_clarification_tickets" not in sql
               for sql, _ in cursor.calls)


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


