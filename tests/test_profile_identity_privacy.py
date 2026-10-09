from datetime import datetime
import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


with patch("mysql.connector.pooling.MySQLConnectionPool"):
    from cogs.moderation import (
        ModerationCog,
        confirmed_warning_count_for_limit,
        profile_stats_account_ids,
        profile_warning_lines,
        should_include_identity_aggregate,
    )
    from schemas.models.user import Warning


def test_identity_aggregates_require_staff_and_private_response():
    assert should_include_identity_aggregate(True, True) is True
    assert should_include_identity_aggregate(True, False) is False
    assert should_include_identity_aggregate(False, True) is False
    assert should_include_identity_aggregate(False, False) is False


def test_profile_public_parameter_is_named_privado_and_keeps_true_default():
    parameters = inspect.signature(ModerationCog.profile.callback).parameters

    assert parameters["privado"].default is True
    assert "efemero" not in parameters


def test_profile_privado_keeps_current_ephemeral_behavior():
    cog = SimpleNamespace(_send_member_profile=AsyncMock())
    interaction = SimpleNamespace(user=SimpleNamespace(id=111))

    asyncio.run(ModerationCog.profile.callback(cog, interaction))
    cog._send_member_profile.assert_awaited_once_with(
        interaction,
        interaction.user,
        ephemeral=True,
    )


def test_profile_privado_false_keeps_public_behavior():
    cog = SimpleNamespace(_send_member_profile=AsyncMock())
    interaction = SimpleNamespace(user=SimpleNamespace(id=111))

    asyncio.run(ModerationCog.profile.callback(cog, interaction, privado=False))
    cog._send_member_profile.assert_awaited_once_with(
        interaction,
        interaction.user,
        ephemeral=False,
    )


def test_linked_accounts_never_enter_statistics_account_ids():
    assert profile_stats_account_ids(111, [112]) == {111, 112}
    linked_accounts = [999, 1000]
    assert profile_stats_account_ids(111, [112]).isdisjoint(linked_accounts)


def test_own_warnings_keep_details_and_external_warnings_are_aggregate_only():
    own = [Warning(datetime(2026, 9, 1), "motivo próprio")]

    lines = profile_warning_lines(own, 4)

    assert lines[0] == "**01/09/2026** - motivo próprio"
    assert lines[1] == "+ 4 advertências associadas a outras contas vinculadas"


def test_external_warning_summary_does_not_leak_identity_or_moderation_details():
    lines = profile_warning_lines([], 3)
    rendered = "\n".join(lines)

    assert rendered == "+ 3 advertências associadas a outras contas vinculadas"
    for forbidden in ("123456789", "username", "display", "user_a", "user_b", "autor", "motivo"):
        assert forbidden not in rendered.lower()


def test_confirmed_warning_count_for_limit_uses_cluster_summary():
    with patch(
        "cogs.moderation.get_identity_summary",
        new=AsyncMock(
            return_value=SimpleNamespace(
                warning_count=5,
            )
        ),
    ) as get_identity_summary:
        count = asyncio.run(
            confirmed_warning_count_for_limit(111, 7)
        )

    assert count == 5
    get_identity_summary.assert_awaited_once_with(111, 7)
