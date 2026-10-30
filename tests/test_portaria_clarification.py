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
    cursor = Cursor([{"status": "pending", "guild_id": 222, "user_id": 777, "flow_type": "portaria"}, {"id": 42}])
    @contextmanager
    def connection():
        yield cursor
    monkeypatch.setattr(database, "pooled_connection", connection)
    with pytest.raises(ValueError, match="Encerre o ticket"):
        database.record_form_decision(19, "approved", 888)
    assert "FOR UPDATE" in cursor.calls[0][0]
    assert "portaria_clarification_tickets" in cursor.calls[1][0]
    assert cursor.calls[1][1] == (222, 777)
    assert all("INSERT INTO form_decisions" not in sql for sql, _ in cursor.calls)


def test_normal_form_decision_works_without_active_clarification(monkeypatch):
    from core import database
    cursor = Cursor([{"status": "pending", "guild_id": 222, "user_id": 777, "flow_type": "portaria"}, None])
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
    assert params == (222, 19, 222)


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


def test_non_portaria_form_decision_is_unaffected_by_ticket_feature(monkeypatch):
    from core import database
    cursor = Cursor([{"status": "pending", "guild_id": 222,
                      "user_id": 777, "flow_type": "normal"}])
    @contextmanager
    def connection():
        yield cursor
    monkeypatch.setattr(database, "pooled_connection", connection)
    database.record_form_decision(19, "approved", 888)
    assert all("portaria_clarification_tickets" not in sql for sql, _ in cursor.calls)
    assert any("INSERT INTO form_decisions" in sql for sql, _ in cursor.calls)


def test_sync_adds_button_to_legacy_pending_discord_card(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    class FakeChannel:
        def __init__(self, message):
            self.fetch_message = AsyncMock(return_value=message)

    submission = {"id": 7, "guild_id": 222, "channel_id": 99, "message_id": 44}
    message = SimpleNamespace(
        id=44,
        components=[SimpleNamespace(children=[
            SimpleNamespace(custom_id=f"{portaria_views.PORTARIA_APPROVE_PREFIX}7"),
            SimpleNamespace(custom_id=f"{portaria_views.PORTARIA_REJECT_PREFIX}7"),
        ])],
        edit=AsyncMock(),
    )
    channel = FakeChannel(message)
    guild = SimpleNamespace(get_channel=lambda _: channel)
    bot = SimpleNamespace(get_guild=lambda _: guild, add_view=Mock())
    monkeypatch.setattr(portaria_views, "list_pending_portaria_submissions",
                        lambda: [submission])
    monkeypatch.setattr(portaria_views.discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(portaria_views, "ticket_settings",
                        lambda _: {"clarification_tickets_enabled": 1})
    next_offset = asyncio.run(
        portaria_views.sync_pending_portaria_clarification_buttons(bot, limit=1))
    assert next_offset == 0
    message.edit.assert_awaited_once()
    view = message.edit.await_args.kwargs["view"]
    assert [item.label for item in view.children] == [
        "Aprovar", "Reprovar", "Solicitar esclarecimentos"]
    bot.add_view.assert_called_once_with(view, message_id=44)


def test_sync_removes_disabled_ticket_button_from_existing_card(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    class FakeChannel:
        def __init__(self, message):
            self.fetch_message = AsyncMock(return_value=message)

    submission = {"id": 7, "guild_id": 222, "channel_id": 99, "message_id": 44}
    message = SimpleNamespace(
        id=44,
        components=[SimpleNamespace(children=[
            SimpleNamespace(custom_id=f"{portaria_views.PORTARIA_APPROVE_PREFIX}7"),
            SimpleNamespace(custom_id=f"{portaria_views.PORTARIA_REJECT_PREFIX}7"),
            SimpleNamespace(custom_id="portaria_clarification_open:7"),
        ])],
        edit=AsyncMock(),
    )
    bot = SimpleNamespace(
        get_guild=lambda _: SimpleNamespace(get_channel=lambda _: FakeChannel(message)),
        add_view=Mock(),
    )
    monkeypatch.setattr(portaria_views, "list_pending_portaria_submissions",
                        lambda: [submission])
    monkeypatch.setattr(portaria_views.discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(portaria_views, "ticket_settings",
                        lambda _: {"clarification_tickets_enabled": 0})
    asyncio.run(portaria_views.sync_pending_portaria_clarification_buttons(bot))
    view = message.edit.await_args.kwargs["view"]
    assert [item.label for item in view.children] == ["Aprovar", "Reprovar"]


def test_ticket_config_outage_never_removes_existing_portaria_actions(monkeypatch):
    def unavailable(_):
        raise ConnectionError("db unavailable")
    monkeypatch.setattr(portaria_views, "ticket_settings", unavailable)
    async def make():
        return portaria_views.PortariaDecisionView(7, guild_id=222)
    view = asyncio.run(make())
    assert [item.label for item in view.children] == ["Aprovar", "Reprovar"]


def test_candidate_mention_is_not_sent_again_on_retry(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core import portaria_clarification as handler

    async def messages():
        yield SimpleNamespace(author=SimpleNamespace(id=77),
                              content="Este é o atendimento **#42**.")
    channel = SimpleNamespace(history=lambda limit: messages(),
                              send=AsyncMock())
    applicant = SimpleNamespace(mention="<@777>")
    asyncio.run(handler._ensure_ticket_introduction(
        channel, bot_id=77, ticket_id=42, submission_id=19,
        applicant=applicant, view=Mock(),
    ))
    channel.send.assert_not_awaited()


def test_candidate_mention_goes_to_private_ticket_when_not_yet_notified():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core import portaria_clarification as handler

    async def messages():
        if False:
            yield None
    channel = SimpleNamespace(history=lambda limit: messages(),
                              send=AsyncMock())
    applicant = SimpleNamespace(id=777, mention="<@777>")
    asyncio.run(handler._ensure_ticket_introduction(
        channel, bot_id=77, ticket_id=42, submission_id=19,
        applicant=applicant, view=Mock(),
    ))
    channel.send.assert_awaited_once()
    content = channel.send.await_args.args[0]
    assert "<@777>" in content
    assert "atendimento **#42**" in content
    assert channel.send.await_args.kwargs["allowed_mentions"].everyone is False


def test_background_recovery_only_reuses_an_existing_channel(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core import portaria_clarification as handler

    ticket = {"id": 42, "guild_id": 222, "submission_id": 19,
              "applicant_discord_user_id": 777,
              "discord_channel_id": 999, "status": "FAILED"}
    class FakeChannel:
        id = 999
        def overwrites_for(self, _role):
            return SimpleNamespace(view_channel=False)
    channel = FakeChannel()
    applicant = SimpleNamespace(id=777)
    guild = SimpleNamespace(id=222, default_role=object(),
                            get_member=lambda _id: applicant)
    bot = SimpleNamespace(get_guild=lambda _id: guild,
                          user=SimpleNamespace(id=333), add_view=Mock())
    claim = Mock(return_value=True)
    remember = Mock(return_value=True)
    mark = Mock(return_value=True)
    notify = AsyncMock()
    monkeypatch.setattr(handler, "list_recoverable_tickets", lambda **_: [ticket])
    monkeypatch.setattr(handler, "_existing_channel", AsyncMock(return_value=channel))
    monkeypatch.setattr(handler, "_ensure_ticket_introduction", notify)
    monkeypatch.setattr(handler, "claim_open", claim)
    monkeypatch.setattr(handler, "remember_channel", remember)
    monkeypatch.setattr(handler, "mark_open", mark)

    asyncio.run(handler.reconcile_incomplete_clarification_tickets(bot))
    claim.assert_called_once_with(42, 222)
    remember.assert_called_once_with(42, 222, 999)
    mark.assert_called_once_with(42, 222, 999)
    notify.assert_awaited_once()
    bot.add_view.assert_called_once()


def test_background_recovery_does_not_create_a_channel_when_missing(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core import portaria_clarification as handler

    ticket = {"id": 42, "guild_id": 222, "submission_id": 19,
              "applicant_discord_user_id": 777, "discord_channel_id": 999,
              "status": "FAILED"}
    bot = SimpleNamespace(get_guild=lambda _: SimpleNamespace(id=222))
    claim = Mock()
    monkeypatch.setattr(handler, "list_recoverable_tickets", lambda **_: [ticket])
    monkeypatch.setattr(handler, "_existing_channel", AsyncMock(return_value=None))
    monkeypatch.setattr(handler, "claim_open", claim)
    asyncio.run(handler.reconcile_incomplete_clarification_tickets(bot))
    claim.assert_not_called()


def test_public_ticket_channel_is_rejected_before_candidate_notification():
    from types import SimpleNamespace
    from core import portaria_clarification as handler

    channel = SimpleNamespace(
        overwrites_for=lambda _: SimpleNamespace(view_channel=True))
    guild = SimpleNamespace(default_role=object())
    with pytest.raises(ValueError, match="não está privado"):
        handler._assert_channel_is_private(guild, channel)


def test_pending_form_lookup_includes_discord_channel_for_button_sync(monkeypatch):
    """The synchronization worker requires channel_id from the real DB query."""
    from core import database
    rows = [{"id": 7, "guild_id": 222, "channel_id": 99, "message_id": 44}]
    cursor = Cursor()
    cursor.fetchall = lambda: rows
    @contextmanager
    def connection():
        yield cursor
    monkeypatch.setattr(database, "pooled_connection", connection)
    assert database.list_pending_portaria_submissions() == rows
    sql, _ = cursor.calls[0]
    assert "fs.channel_id" in sql
    assert "fs.guild_id" in sql
    assert "fs.message_id" in sql
    assert "ORDER BY fs.id ASC" in sql


def test_portaria_sync_explains_unrecognized_legacy_components(monkeypatch, caplog):
    """Do not overwrite a message just because an old DB row refers to it."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import logging

    class FakeChannel:
        def __init__(self, message):
            self.fetch_message = AsyncMock(return_value=message)

    form = {"id": 7, "guild_id": 222, "channel_id": 99, "message_id": 44}
    message = SimpleNamespace(
        id=44,
        components=[SimpleNamespace(children=[
            SimpleNamespace(custom_id="legacy_approval:7"),
            SimpleNamespace(custom_id="legacy_rejection:7"),
        ])],
        edit=AsyncMock(),
    )
    channel = FakeChannel(message)
    guild = SimpleNamespace(get_channel=lambda _: channel)
    bot = SimpleNamespace(get_guild=lambda _: guild, add_view=Mock())
    monkeypatch.setattr(portaria_views, "list_pending_portaria_submissions",
                        lambda: [form])
    monkeypatch.setattr(portaria_views.discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(portaria_views, "ticket_settings",
                        lambda _: {"clarification_tickets_enabled": 1})
    problems = {}
    with caplog.at_level(logging.WARNING, logger="core.portaria_views"):
        asyncio.run(portaria_views.sync_pending_portaria_clarification_buttons(
            bot, failure_cache=problems))
        asyncio.run(portaria_views.sync_pending_portaria_clarification_buttons(
            bot, failure_cache=problems))
    message.edit.assert_not_awaited()
    assert problems[(222, 7)].startswith("unrecognized_buttons:")
    assert caplog.text.count("reason=unrecognized_buttons") == 1


def test_portaria_sync_retries_404_without_repeating_same_warning(monkeypatch, caplog):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import logging

    class FakeChannel:
        def __init__(self):
            self.fetch_message = AsyncMock()

    form = {"id": 7, "guild_id": 222, "channel_id": 99, "message_id": 44}
    channel = FakeChannel()
    response = SimpleNamespace(status=404, reason="Not Found")
    channel.fetch_message.side_effect = portaria_views.discord.NotFound(
        response, {"code": 10008, "message": "Unknown Message"})
    guild = SimpleNamespace(get_channel=lambda _: channel)
    bot = SimpleNamespace(get_guild=lambda _: guild, add_view=Mock())
    monkeypatch.setattr(portaria_views, "list_pending_portaria_submissions",
                        lambda: [form])
    monkeypatch.setattr(portaria_views.discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(portaria_views, "ticket_settings",
                        lambda _: {"clarification_tickets_enabled": 1})
    problems = {}
    with caplog.at_level(logging.WARNING, logger="core.portaria_views"):
        asyncio.run(portaria_views.sync_pending_portaria_clarification_buttons(
            bot, failure_cache=problems))
        asyncio.run(portaria_views.sync_pending_portaria_clarification_buttons(
            bot, failure_cache=problems))
    assert channel.fetch_message.await_count == 2  # retry remains enabled
    assert caplog.text.count("reason=discord_http_error") == 1
    assert "status=404" in caplog.text
    assert "code=10008" in caplog.text

    # If the old message becomes reachable, the next cycle updates it and
    # clears the warning so a new failure can be reported normally.
    message = SimpleNamespace(
        id=44,
        components=[SimpleNamespace(children=[
            SimpleNamespace(custom_id=f"{portaria_views.PORTARIA_APPROVE_PREFIX}7"),
            SimpleNamespace(custom_id=f"{portaria_views.PORTARIA_REJECT_PREFIX}7"),
        ])],
        edit=AsyncMock(),
    )
    channel.fetch_message.side_effect = None
    channel.fetch_message.return_value = message
    asyncio.run(portaria_views.sync_pending_portaria_clarification_buttons(
        bot, failure_cache=problems))
    message.edit.assert_awaited_once()
    assert problems == {}


def test_closing_intent_persists_actor_reason_before_discord_edit(monkeypatch):
    cursor = Cursor()
    patch_db(monkeypatch, cursor)
    assert store.claim_close(42, 222, 333, "Conclusão registrada pela Staff")
    sql, args = cursor.calls[0]
    assert "closed_by_discord_user_id = COALESCE" in sql
    assert "close_reason = COALESCE" in sql
    assert "INTERVAL 10 MINUTE" in sql
    assert args == (333, "Conclusão registrada pela Staff", 42, 222)
    store.restore_open(42, 222)
    assert "closed_by_discord_user_id = NULL" in cursor.calls[-1][0]
    assert "close_reason = NULL" in cursor.calls[-1][0]


def test_closing_recovery_lists_only_stale_durable_intents(monkeypatch):
    cursor = Cursor()
    cursor.fetchall = lambda: []
    patch_db(monkeypatch, cursor)
    assert store.list_stale_closing_tickets(limit=999) == []
    sql, args = cursor.calls[0]
    assert "status = 'CLOSING'" in sql
    assert "closed_by_discord_user_id IS NOT NULL" in sql
    assert "close_reason IS NOT NULL" in sql
    assert "closing_attempted_at < UTC_TIMESTAMP(6) - INTERVAL 10 MINUTE" in sql
    assert args == (20,)


def test_recover_stale_closing_revokes_candidate_and_commits_original_intent(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core import portaria_clarification as handler

    class Target:
        def __init__(self, id):
            self.id = id

    class FakeChannel:
        def __init__(self, guild):
            self.id = 999
            self.guild = guild
            self.topic = handler._ticket_topic(42, 19)
            self.overwrites = {
                Target(0): SimpleNamespace(view_channel=False),
                Target(777): SimpleNamespace(view_channel=True),
                Target(333): SimpleNamespace(view_channel=True),
            }
            self.edit = AsyncMock()
        def overwrites_for(self, role):
            return SimpleNamespace(view_channel=False)

    guild = SimpleNamespace(id=222, default_role=Target(0))
    channel = FakeChannel(guild)
    guild.get_channel = lambda _: channel
    bot = SimpleNamespace(get_guild=lambda _: guild)
    ticket = {"id": 42, "guild_id": 222, "submission_id": 19,
              "applicant_discord_user_id": 777, "discord_channel_id": 999,
              "closed_by_discord_user_id": 333,
              "close_reason": "Confirmação manual após atendimento"}
    monkeypatch.setattr(handler.discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(handler, "list_stale_closing_tickets", lambda **_: [ticket])
    claim = Mock(return_value=True)
    closed = Mock(return_value=True)
    monkeypatch.setattr(handler, "claim_close", claim)
    monkeypatch.setattr(handler, "mark_closed", closed)

    asyncio.run(handler.reconcile_stale_closing_clarification_tickets(bot))

    claim.assert_called_once_with(
        42, 222, 333, "Confirmação manual após atendimento")
    channel.edit.assert_awaited_once()
    overwrites = channel.edit.await_args.kwargs["overwrites"]
    assert all(target.id != 777 for target in overwrites)
    closed.assert_called_once_with(
        42, 222, 333, "Confirmação manual após atendimento")


def test_recover_stale_closing_never_closes_unrelated_or_insecure_channel(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core import portaria_clarification as handler

    class FakeChannel:
        def __init__(self, guild):
            self.id = 999
            self.guild = guild
            self.topic = "somebody-elses-ticket"
            self.overwrites = {}
            self.edit = AsyncMock()
        def overwrites_for(self, role):
            return SimpleNamespace(view_channel=True)

    guild = SimpleNamespace(id=222, default_role=object())
    channel = FakeChannel(guild)
    guild.get_channel = lambda _: channel
    bot = SimpleNamespace(get_guild=lambda _: guild)
    ticket = {"id": 42, "guild_id": 222, "submission_id": 19,
              "applicant_discord_user_id": 777, "discord_channel_id": 999,
              "closed_by_discord_user_id": 333,
              "close_reason": "Confirmação manual após atendimento"}
    monkeypatch.setattr(handler.discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(handler, "list_stale_closing_tickets", lambda **_: [ticket])
    claim, closed = Mock(), Mock()
    monkeypatch.setattr(handler, "claim_close", claim)
    monkeypatch.setattr(handler, "mark_closed", closed)
    asyncio.run(handler.reconcile_stale_closing_clarification_tickets(bot))
    claim.assert_not_called()
    closed.assert_not_called()
    channel.edit.assert_not_awaited()
    channel.topic = handler._ticket_topic(42, 19)
    asyncio.run(handler.reconcile_stale_closing_clarification_tickets(bot))
    claim.assert_not_called()
    closed.assert_not_called()
    channel.edit.assert_not_awaited()


def test_recover_stale_closing_defers_database_completion_after_discord_failure(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core import portaria_clarification as handler

    class FakeChannel:
        def __init__(self, guild):
            self.id = 999
            self.guild = guild
            self.topic = handler._ticket_topic(42, 19)
            self.overwrites = {SimpleTarget(777): object()}
            self.edit = AsyncMock(side_effect=RuntimeError("Discord uncertain"))
        def overwrites_for(self, role):
            return SimpleNamespace(view_channel=False)

    class SimpleTarget:
        def __init__(self, id):
            self.id = id

    guild = SimpleNamespace(id=222, default_role=object())
    channel = FakeChannel(guild)
    guild.get_channel = lambda _: channel
    bot = SimpleNamespace(get_guild=lambda _: guild)
    ticket = {"id": 42, "guild_id": 222, "submission_id": 19,
              "applicant_discord_user_id": 777, "discord_channel_id": 999,
              "closed_by_discord_user_id": 333,
              "close_reason": "Confirmação manual após atendimento"}
    monkeypatch.setattr(handler.discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(handler, "list_stale_closing_tickets", lambda **_: [ticket])
    monkeypatch.setattr(handler, "claim_close", Mock(return_value=True))
    closed = Mock()
    monkeypatch.setattr(handler, "mark_closed", closed)
    asyncio.run(handler.reconcile_stale_closing_clarification_tickets(bot))
    channel.edit.assert_awaited_once()
    closed.assert_not_called()
