"""Regression coverage for approvals of Portaria forms without birthday questions.

All Discord and database operations are mocked; tests never contact Discord.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core import portaria_views


class FakeRole:
    def __init__(self, role_id: int, position: int = 1):
        self.id = role_id
        self.position = position

    def __ge__(self, other):
        return self.position >= other.position


class FakeMember:
    def __init__(self, member_id: int, roles: list[FakeRole]):
        self.id = member_id
        self.roles = roles
        self.remove_roles = AsyncMock()


def build_approval(monkeypatch, *, role_present=True, configured=True, can_manage=True,
                   submission_guild_id=100):
    visitor_role = FakeRole(501)
    staff_role = FakeRole(502)
    member = FakeMember(42, [visitor_role] if role_present else [])
    staff = FakeMember(99, [staff_role])
    bot = SimpleNamespace(
        guild_permissions=SimpleNamespace(manage_roles=can_manage),
        top_role=FakeRole(999, position=10),
    )
    guild = SimpleNamespace(
        id=100,
        me=bot,
        get_member=lambda member_id: member if member_id == 42 else None,
        get_role=lambda role_id: visitor_role if configured and role_id == 501 else None,
    )
    interaction = SimpleNamespace(
        guild=guild,
        user=staff,
        client=SimpleNamespace(user=SimpleNamespace(id=777)),
        response=SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        message=object(),
    )
    submission = {
        "id": 7, "guild_id": submission_guild_id, "user_id": 42,
        "status": "pending", "flow_id": 31,
    }
    decision_record = Mock()
    register = Mock()
    rollback = Mock(return_value=True)
    routing = AsyncMock(return_value=True)

    # The staff-user type guard still runs, but without constructing Discord models.
    monkeypatch.setattr(portaria_views.discord, "Member", FakeMember)
    monkeypatch.setattr(portaria_views, "getStaffRoles", lambda _guild: [staff_role])
    monkeypatch.setattr(portaria_views, "get_form_submission", lambda _id: submission)
    monkeypatch.setattr(
        portaria_views, "get_portaria_base_config",
        lambda _guild_id: {"visitante_role_id": 501},
    )
    monkeypatch.setattr(portaria_views, "record_form_decision", decision_record)
    monkeypatch.setattr(portaria_views, "includeUser", register)
    monkeypatch.setattr(portaria_views, "rollback_form_decision", rollback)
    monkeypatch.setattr(
        portaria_views.PortariaDecisionView, "_validate_routing_requirements",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        portaria_views.PortariaDecisionView, "_extract_birthday_from_message",
        lambda *args: None,
    )
    monkeypatch.setattr(
        portaria_views.PortariaDecisionView, "_route_submission_message", routing,
    )
    return SimpleNamespace(
        member=member, staff=staff, interaction=interaction,
        decision_record=decision_record, register=register,
        rollback=rollback, routing=routing,
    )


def decide(context):
    async def run():
        view = portaria_views.PortariaDecisionView(submission_id=7)
        await view._handle_decision(context.interaction, "approved")

    asyncio.run(run())


def test_no_birthday_approval_removes_configured_visitor_only_in_current_guild(monkeypatch):
    context = build_approval(monkeypatch)

    decide(context)

    context.decision_record.assert_called_once_with(7, "approved", 99)
    assert context.register.call_count == 1
    assert context.register.call_args.args[:2] == (context.member, 100)
    assert isinstance(context.register.call_args.args[2], datetime)
    context.member.remove_roles.assert_awaited_once_with(
        context.member.roles[0],
        reason="Aprovação da ficha da Portaria sem data de nascimento",
    )
    context.routing.assert_awaited_once()
    assert context.rollback.call_count == 0
    assert "sucesso" in context.interaction.followup.send.await_args.args[0]


def test_no_birthday_approval_is_idempotent_when_visitor_already_removed(monkeypatch):
    context = build_approval(monkeypatch, role_present=False)

    decide(context)

    context.member.remove_roles.assert_not_awaited()
    context.decision_record.assert_called_once()
    context.routing.assert_awaited_once()


def test_missing_visitor_role_blocks_approval_before_database_change(monkeypatch):
    context = build_approval(monkeypatch, configured=False)

    decide(context)

    context.decision_record.assert_not_called()
    context.register.assert_not_called()
    context.member.remove_roles.assert_not_awaited()
    assert "Visitante" in context.interaction.followup.send.await_args.kwargs["content"]


def test_missing_discord_role_permission_blocks_approval(monkeypatch):
    context = build_approval(monkeypatch, can_manage=False)

    decide(context)

    context.decision_record.assert_not_called()
    context.register.assert_not_called()
    assert "permissão" in context.interaction.followup.send.await_args.kwargs["content"]


def test_role_removal_failure_rolls_back_decision_and_never_routes_approved_form(monkeypatch):
    context = build_approval(monkeypatch)
    context.member.remove_roles.side_effect = PermissionError("missing role permissions")

    decide(context)

    context.decision_record.assert_called_once()
    context.register.assert_called_once()
    context.rollback.assert_called_once_with(7, "approved", 99)
    context.routing.assert_not_awaited()
    assert "revertida para pendente" in context.interaction.followup.send.await_args.kwargs["content"]


def test_approval_rejects_submission_from_other_guild_before_any_side_effect(monkeypatch):
    context = build_approval(monkeypatch, submission_guild_id=200)

    decide(context)

    context.interaction.response.defer.assert_not_awaited()
    context.interaction.response.send_message.assert_awaited_once()
    assert "não pertence ao servidor" in context.interaction.response.send_message.await_args.kwargs["content"]
    context.decision_record.assert_not_called()
    context.register.assert_not_called()
    context.member.remove_roles.assert_not_awaited()
