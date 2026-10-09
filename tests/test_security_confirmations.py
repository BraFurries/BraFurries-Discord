import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import cogs.security as security_module

from cogs.security import OwnerConfirmationView, PendingSecurityAction, SecurityCog


def _security_cog(*, owner_id: int = 10) -> SecurityCog:
    cog = object.__new__(SecurityCog)
    cog.bot = SimpleNamespace(get_guild=Mock(return_value=SimpleNamespace(owner_id=owner_id)))
    cog.pending_actions = {}
    return cog


def _pending_action(callback: AsyncMock) -> PendingSecurityAction:
    return PendingSecurityAction(
        guild_id=1,
        description="Adicionar permissão administrativa",
        actor_id=20,
        created_at=datetime.utcnow(),
        confirm_callback=callback,
    )


def test_reject_pending_action_keeps_preventive_action() -> None:
    cog = _security_cog()
    callback = AsyncMock()
    cog.pending_actions[1] = _pending_action(callback)

    ok, message = cog.reject_pending_action(1, SimpleNamespace(id=10))

    assert ok is True
    assert "medida preventiva" in message
    assert 1 not in cog.pending_actions
    callback.assert_not_awaited()


def test_only_owner_can_reject_pending_action() -> None:
    cog = _security_cog()
    cog.pending_actions[1] = _pending_action(AsyncMock())

    ok, message = cog.reject_pending_action(1, SimpleNamespace(id=99))

    assert ok is False
    assert "Somente o dono" in message
    assert 1 in cog.pending_actions


def test_confirmation_claims_action_and_runs_callback() -> None:
    async def scenario() -> None:
        cog = _security_cog()
        callback = AsyncMock()
        cog.pending_actions[1] = _pending_action(callback)

        ok, message = await cog.confirm_pending_action(1, SimpleNamespace(id=10))

        assert ok is True
        assert "aplicada com sucesso" in message
        assert 1 not in cog.pending_actions
        callback.assert_awaited_once_with()

    asyncio.run(scenario())


def test_timeout_removes_buttons_and_marks_message_expired() -> None:
    async def scenario() -> None:
        cog = _security_cog()
        cog.pending_actions[1] = _pending_action(AsyncMock())
        view = OwnerConfirmationView(cog, 1, "Adicionar permissão administrativa")
        view.message = SimpleNamespace(edit=AsyncMock())

        await view.on_timeout()

        assert 1 not in cog.pending_actions
        view.message.edit.assert_awaited_once()
        edit_kwargs = view.message.edit.await_args.kwargs
        assert edit_kwargs["view"] is None
        assert "não aceita mais interações" in edit_kwargs["embed"].description
        assert "prazo de confirmação terminou" in edit_kwargs["embed"].description

    asyncio.run(scenario())


def test_member_update_missing_permissions_does_not_register_confirmation(caplog) -> None:
    async def scenario() -> None:
        cog = _security_cog()
        cog.bot.user = SimpleNamespace(id=999)
        cog._get_relevant_audit_entry = AsyncMock(return_value=SimpleNamespace(user=SimpleNamespace(id=20)))
        cog._register_owner_confirmation = AsyncMock()

        guild = SimpleNamespace(id=1, name="BraFurries")
        admin_role = SimpleNamespace(
            id=100,
            name="Admin",
            permissions=SimpleNamespace(administrator=True),
        )
        before = SimpleNamespace(roles=[], guild=guild)
        after = SimpleNamespace(
            id=30,
            name="member",
            display_name="Member",
            mention="<@30>",
            roles=[admin_role],
            guild=guild,
            remove_roles=AsyncMock(
                side_effect=discord.Forbidden(
                    response=SimpleNamespace(status=403, reason="Forbidden"),
                    message="Missing Permissions",
                )
            ),
        )

        await cog.on_member_update(before, after)

        after.remove_roles.assert_awaited_once_with(
            admin_role,
            reason="Aguardando confirmação do dono para atribuição de cargo administrador",
        )
        cog._register_owner_confirmation.assert_not_awaited()
        assert "Não foi possível remover preventivamente cargos administradores" in caplog.text
        assert "guild=BraFurries (1)" in caplog.text
        assert "member=Member (30)" in caplog.text

    asyncio.run(scenario())


def test_unauthorized_unban_is_restored_before_identity_state_changes(monkeypatch) -> None:
    async def scenario() -> None:
        cog = _security_cog()
        cog.bot.user = SimpleNamespace(id=999)
        cog._get_relevant_audit_entry = AsyncMock(
            return_value=SimpleNamespace(
                user=SimpleNamespace(id=20),
                reason="manual",
            )
        )
        cog._is_sensitive_action_authorized = AsyncMock(return_value=False)
        cog._handle_unauthorized_sensitive_action = AsyncMock()
        begin_identity_unban = Mock()
        monkeypatch.setattr(
            security_module,
            "begin_identity_unban",
            begin_identity_unban,
        )

        guild = SimpleNamespace(
            id=1,
            ban=AsyncMock(),
        )
        user = SimpleNamespace(id=111, mention="<@111>")

        await cog.on_member_unban(guild, user)

        guild.ban.assert_awaited_once()
        assert guild.ban.await_args.args[0].id == 111
        begin_identity_unban.assert_not_called()
        cog._handle_unauthorized_sensitive_action.assert_awaited_once()

    asyncio.run(scenario())


def test_unban_waits_for_delayed_audit_entry_before_authorization(monkeypatch) -> None:
    async def scenario() -> None:
        cog = _security_cog()
        cog.bot.user = SimpleNamespace(id=999)
        entry = SimpleNamespace(
            user=SimpleNamespace(id=20),
            reason="staff unban",
        )
        cog._get_relevant_audit_entry = AsyncMock(
            side_effect=[None, None, entry]
        )
        cog._is_sensitive_action_authorized = AsyncMock(return_value=True)
        sleep = AsyncMock()
        monkeypatch.setattr(security_module.asyncio, "sleep", sleep)
        begin_identity_unban = Mock(return_value=[])
        monkeypatch.setattr(
            security_module,
            "begin_identity_unban",
            begin_identity_unban,
        )

        guild = SimpleNamespace(id=1)
        user = SimpleNamespace(id=111, mention="<@111>")

        await cog.on_member_unban(guild, user)

        assert cog._get_relevant_audit_entry.await_count == 3
        assert sleep.await_count == 2
        begin_identity_unban.assert_called_once_with(
            1,
            111,
            20,
            "staff unban",
        )

    asyncio.run(scenario())


def test_authorized_unban_propagates_to_sibling_owned_effects(monkeypatch) -> None:
    async def scenario() -> None:
        cog = _security_cog()
        cog.bot.user = SimpleNamespace(id=999)
        cog._get_relevant_audit_entry = AsyncMock(
            return_value=SimpleNamespace(
                user=SimpleNamespace(id=20),
                reason="revogado pela staff",
            )
        )
        cog._is_sensitive_action_authorized = AsyncMock(return_value=True)
        begin_identity_unban = Mock(
            return_value=[
                {
                    "discord_user_id": 222,
                    "effect_ids": [9],
                }
            ]
        )
        mark_effect = Mock(return_value=True)
        monkeypatch.setattr(
            security_module,
            "begin_identity_unban",
            begin_identity_unban,
        )
        monkeypatch.setattr(
            security_module,
            "markBanDiscordEffectReverted",
            mark_effect,
        )

        guild = SimpleNamespace(
            id=1,
            unban=AsyncMock(),
        )
        user = SimpleNamespace(id=111, mention="<@111>")

        await cog.on_member_unban(guild, user)

        begin_identity_unban.assert_called_once_with(
            1,
            111,
            20,
            "revogado pela staff",
        )
        guild.unban.assert_awaited_once()
        assert guild.unban.await_args.args[0].id == 222
        mark_effect.assert_called_once_with(9, "REVERTED", None)

    asyncio.run(scenario())
