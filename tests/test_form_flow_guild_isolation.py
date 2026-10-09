import contextlib

import pytest

from core import database
from cogs.forms import FormsCog
from core.portaria_views import PortariaDecisionView


class Cursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.lastrowid = 99
        self.calls = []

    def execute(self, query, params=()):
        self.calls.append((" ".join(query.split()), params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def cursor_context(monkeypatch, cursor):
    @contextlib.contextmanager
    def connection():
        yield cursor
    monkeypatch.setattr(database, "pooled_connection", connection)


def test_create_flow_persists_guild_ownership(monkeypatch):
    cursor = Cursor()
    cursor_context(monkeypatch, cursor)
    assert database.create_form_flow(10, "Portaria", "portaria", 100) == 99
    assert cursor.calls[0][1] == (10, "Portaria", "portaria", 100)


def test_list_and_id_lookup_are_scoped_to_guild(monkeypatch):
    cursor = Cursor([{"id": 1, "server_guild_id": 10, "name": "Portaria"}])
    cursor_context(monkeypatch, cursor)
    assert database.list_form_flows(10)[0]["server_guild_id"] == 10
    assert database.get_form_flow(10, 1)["id"] == 1
    assert all(10 in call[1] for call in cursor.calls)


def test_same_name_is_scoped_and_duplicates_are_ambiguous(monkeypatch):
    cursor = Cursor([{"id": 2, "server_guild_id": 20, "name": "Portaria"}])
    cursor_context(monkeypatch, cursor)
    assert database.get_form_flow_by_name(20, "Portaria")["server_guild_id"] == 20
    cursor.rows = [{"id": 2}, {"id": 3}]
    with pytest.raises(ValueError, match="ambíguo"):
        database.get_form_flow_by_name(20, "Portaria")


def test_cross_guild_update_uses_ownership_predicate(monkeypatch):
    cursor = Cursor()
    cursor_context(monkeypatch, cursor)
    database.update_form_flow(10, 1, name="Novo")
    query, params = cursor.calls[0]
    assert "server_guild_id = %s" in query
    assert params == ("Novo", 1, 10)


def test_forms_cog_resolves_ids_and_names_with_the_current_guild(monkeypatch):
    calls = []
    monkeypatch.setattr("cogs.forms.get_form_flow", lambda guild_id, flow_id: calls.append((guild_id, flow_id)) or {"id": flow_id})
    monkeypatch.setattr("cogs.forms.get_form_flow_by_name", lambda guild_id, name: calls.append((guild_id, name)) or {"name": name})
    assert FormsCog._resolve_flow(10, "22")["id"] == 22
    assert FormsCog._resolve_flow(20, "Portaria")["name"] == "Portaria"
    assert calls == [(10, 22), (20, "Portaria")]


def test_portaria_destination_and_feedback_use_submission_guild(monkeypatch):
    calls = []
    monkeypatch.setattr("core.portaria_views.get_form_flow", lambda guild_id, flow_id: calls.append((guild_id, flow_id)) or {"approved_target_channel_id": None, "rejection_feedback_enabled": True})
    guild = type("Guild", (), {"id": 10, "get_channel": lambda *_: None})()
    submission = {"guild_id": 10, "flow_id": 22}
    assert PortariaDecisionView._resolve_destination_channel(guild, submission, "approved") is None
    assert PortariaDecisionView._is_rejection_feedback_enabled(submission) is True
    assert calls == [(10, 22), (10, 22)]
