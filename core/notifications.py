from os import getenv
from typing import Any, Dict, Optional
import logging

import aiohttp

logger = logging.getLogger(__name__)


def _format_command_params(command_params: Optional[Dict[str, Any]]) -> str:
    if not command_params:
        return "Nenhum"

    formatted_params = []
    for key, value in command_params.items():
        formatted_params.append(f"- {key}: {value!r}")
    return "\n".join(formatted_params)


async def notify_owner_and_user(
    ctx: Any,
    error: Exception,
    user_message: str,
    *,
    use_followup: bool,
    command_params: Optional[Dict[str, Any]] = None,
    ephemeral: bool = False,
):
    channel_name = getattr(ctx.channel, "name", str(ctx.channel))
    user_name = getattr(ctx.user, "display_name", str(ctx.user))
    command_name = getattr(getattr(ctx, "command", None), "name", "desconhecido")

    lines = [
        "Coddy apresentou um erro:",
        f"Canal: {channel_name}",
        f"Usuário: {user_name}",
        f"Comando: {command_name}",
        "Parâmetros:",
        _format_command_params(command_params),
        f"Erro: {error}",
    ]
    text = "\n".join(lines)

    try:
        telegram_token = getenv("TELEGRAM_TOKEN")
        telegram_admin = getenv("TELEGRAM_ADMIN")
        if telegram_token and telegram_admin:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.post(
                    f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                    json={"chat_id": telegram_admin, "text": text},
                ) as response:
                    response.raise_for_status()
        else:
            # Command parameters and exception text may contain sensitive user
            # input, so do not mirror the Telegram payload to administrative logs.
            logger.warning(
                'Erro capturado, mas TELEGRAM_TOKEN/TELEGRAM_ADMIN não estão configurados'
            )
    except Exception as notify_error:
        # ClientResponseError may include the request URL containing the token.
        logger.error(
            'Erro ao notificar o responsável no Telegram (tipo=%s)',
            type(notify_error).__name__,
        )

    sender = ctx.followup.send if use_followup else ctx.response.send_message
    return await sender(content=user_message, ephemeral=ephemeral)
