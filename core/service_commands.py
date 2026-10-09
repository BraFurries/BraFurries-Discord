"""Utilities to register and execute community store service commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Optional

from discord import Interaction, Member
from discord.errors import HTTPException, NotFound
from core.runtime_config import get_optional_discord_snowflake


class ServiceCommandError(RuntimeError):
    """Raised when a service command cannot be executed."""


ServiceCommandHandler = Callable[
    [
        Interaction,
        dict[str, Any],
        Optional[dict[str, Any]],
        datetime,
        Optional[datetime],
    ],
    Awaitable[None],
]

_SERVICE_COMMANDS: dict[int, ServiceCommandHandler] = {}


def register_service_command(store_item_id: int, handler: ServiceCommandHandler) -> None:
    """Register ``handler`` to be executed when ``store_item_id`` is used."""

    _SERVICE_COMMANDS[int(store_item_id)] = handler


def unregister_service_command(store_item_id: int) -> None:
    """Remove a previously registered service command handler."""

    _SERVICE_COMMANDS.pop(int(store_item_id), None)


def get_service_command(store_item_id: int) -> Optional[ServiceCommandHandler]:
    """Return the handler registered for ``store_item_id`` if available."""

    return _SERVICE_COMMANDS.get(int(store_item_id))


def has_service_command(store_item_id: int) -> bool:
    """Check whether a handler is registered for ``store_item_id``."""

    return get_service_command(store_item_id) is not None


async def execute_service_command(
    store_item_id: int,
    *,
    interaction: Interaction,
    item: dict[str, Any],
    inventory_entry: Optional[dict[str, Any]] = None,
    used_at: datetime,
    valid_until: Optional[datetime],
) -> None:
    """Execute the service command named ``name``.

    Parameters
    ----------
    store_item_id:
        Identificador do item na loja utilizado para buscar o manipulador.
    interaction:
        Interaction that triggered the execution.
    item:
        Store item metadata associated with the service.
    inventory_entry:
        The inventory record that represents the purchased service.
    used_at:
        Data e hora em que o serviço foi ativado.
    valid_until:
        Data limite de validade calculada para o serviço, quando aplicável.
    """

    handler = get_service_command(store_item_id)
    if handler is None:
        raise ServiceCommandError(
            f"Nenhum manipulador registrado para o serviço com ID {store_item_id}."
        )

    await handler(interaction, item, inventory_entry, used_at, valid_until)


VIP_ROLE_ID = get_optional_discord_snowflake("CODDY_LEGACY_VIP_ROLE_ID") or 0


async def _grant_brafurries_vip(
    interaction: Interaction,
    item: dict[str, Any],
    inventory_entry: Optional[dict[str, Any]],
    used_at: datetime,
    valid_until: Optional[datetime],
) -> None:
    """Grant the BraFurries VIP temporary role for the configured duration."""

    del inventory_entry  # currently unused but kept for future auditing needs

    guild = interaction.guild
    if guild is None:
        raise ServiceCommandError(
            "Não foi possível identificar o servidor para ativar o serviço."
        )

    if isinstance(interaction.user, Member):
        member: Optional[Member] = interaction.user
    else:
        try:
            member = await guild.fetch_member(interaction.user.id)
        except (HTTPException, NotFound):
            member = None
    if member is None:
        raise ServiceCommandError(
            "Não foi possível encontrar o membro no servidor para aplicar o serviço."
        )

    role = guild.get_role(VIP_ROLE_ID)
    if role is None:
        raise ServiceCommandError(
            "O cargo configurado para o VIP temporário não foi encontrado no servidor."
        )

    expiration = valid_until
    if expiration is None:
        duration_value = item.get("duration")
        try:
            duration_seconds = (
                int(duration_value) if duration_value is not None else None
            )
        except (TypeError, ValueError):
            duration_seconds = None

        if duration_seconds and duration_seconds > 0:
            expiration = used_at + timedelta(seconds=duration_seconds)

    if expiration is None:
        raise ServiceCommandError(
            "Não foi possível determinar a data de expiração do VIP temporário."
        )

    from core.database import assignTempRole

    success = await assignTempRole(
        guild.id,
        member,
        VIP_ROLE_ID,
        expiration,
        reason="Serviço VIP temporário da loja da comunidade",
    )
    if not success:
        raise ServiceCommandError(
            "Não foi possível aplicar o cargo VIP temporário. Tente novamente mais tarde."
        )


register_service_command(1, _grant_brafurries_vip)
