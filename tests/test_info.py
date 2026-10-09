import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cogs.info import InfoCog


def test_register_birthday_handles_database_error_after_defer():
    cog = InfoCog(Mock())
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        user=SimpleNamespace(id=456),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
        delete_original_response=AsyncMock(),
    )

    def database_error(*_args):
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        raise RuntimeError("MySQL secret details")

    with (
        patch("cogs.info.getUserBirthday", side_effect=database_error),
        patch("cogs.info.logging.exception") as log_exception,
    ):
        asyncio.run(
            InfoCog.registerBirthday.callback(
                cog,
                interaction,
                "03/01/1994",
                "sim",
            )
        )

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.response.send_message.assert_not_awaited()
    interaction.followup.send.assert_awaited_once_with(
        content="Algo deu errado... Avise o titio!",
        ephemeral=True,
    )
    assert "MySQL secret details" not in interaction.followup.send.await_args.kwargs["content"]
    log_exception.assert_called_once_with(
        "Erro ao registrar aniversário (guild_id=%s, user_id=%s).",
        123,
        456,
    )


def test_register_birthday_sends_success_publicly_after_ephemeral_defer():
    cog = InfoCog(Mock())
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        user=SimpleNamespace(id=456),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
        delete_original_response=AsyncMock(),
    )

    with (
        patch("cogs.info.getUserBirthday", return_value=None),
        patch("cogs.info.includeBirthday", return_value=True),
    ):
        asyncio.run(
            InfoCog.registerBirthday.callback(cog, interaction, "03/01/1994", "sim")
        )

    content = "você foi registrado com o aniversário 03/01!"
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.edit_original_response.assert_awaited_once_with(content=content)
    interaction.delete_original_response.assert_awaited_once_with()
    interaction.followup.send.assert_awaited_once_with(content=content, ephemeral=False)


def test_register_birthday_does_not_log_duplicate_as_database_error():
    cog = InfoCog(Mock())
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        user=SimpleNamespace(id=456),
        command=SimpleNamespace(name="aniversario"),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
        delete_original_response=AsyncMock(),
    )

    with (
        patch("cogs.info.getUserBirthday", return_value=None),
        patch("cogs.info.includeBirthday", side_effect=Exception("Duplicate entry")),
        patch("cogs.info.logging.exception") as log_exception,
    ):
        asyncio.run(
            InfoCog.registerBirthday.callback(cog, interaction, "03/01/1994", "sim")
        )

    log_exception.assert_not_called()
    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
