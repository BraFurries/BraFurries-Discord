import asyncio
from dataclasses import dataclass
import os

import aiohttp


DEFAULT_API_BASE_URL = "http://127.0.0.1:18080"


class IdentityApiError(RuntimeError):
    """Raised when the API identity summary cannot be obtained safely."""


@dataclass(frozen=True)
class ConfirmedIdentity:
    user_id: int
    discord_user_ids: tuple[int, ...]


@dataclass(frozen=True)
class IdentitySummary:
    requested_user_id: int
    confirmed_identities: tuple[ConfirmedIdentity, ...]
    other_account_count: int
    warning_count: int


def _parse_identity_summary(payload: object, community_id: int) -> IdentitySummary:
    if not isinstance(payload, dict):
        raise IdentityApiError("Resposta de identidade inválida")

    moderation = payload.get("moderation")
    if not isinstance(moderation, dict) or moderation.get("communityId") != community_id:
        raise IdentityApiError("Resumo de moderação ausente ou fora da comunidade solicitada")

    other_account_count = payload.get("otherAccountCount")
    warning_count = moderation.get("warningCount")
    requested_user_id = payload.get("requestedUserId")
    identities_payload = payload.get("confirmedIdentities")
    if (
        not isinstance(requested_user_id, int)
        or isinstance(requested_user_id, bool)
        or requested_user_id <= 0
        or not isinstance(identities_payload, list)
        or not isinstance(other_account_count, int)
        or isinstance(other_account_count, bool)
        or other_account_count < 0
        or not isinstance(warning_count, int)
        or isinstance(warning_count, bool)
        or warning_count < 0
    ):
        raise IdentityApiError("Contagens de identidade inválidas")

    identities: list[ConfirmedIdentity] = []
    seen_discord_ids: set[int] = set()
    for item in identities_payload:
        if not isinstance(item, dict):
            raise IdentityApiError("Identidade confirmada inválida")
        user_id = item.get("userId")
        raw_discord_ids = item.get("discordUserIds")
        if (
            not isinstance(user_id, int)
            or isinstance(user_id, bool)
            or user_id <= 0
            or not isinstance(raw_discord_ids, list)
        ):
            raise IdentityApiError("Identidade confirmada inválida")
        discord_ids: list[int] = []
        for raw_id in raw_discord_ids:
            if not isinstance(raw_id, str) or not raw_id.isdecimal():
                raise IdentityApiError("Discord ID confirmado inválido")
            discord_id = int(raw_id)
            if discord_id <= 0 or discord_id in seen_discord_ids:
                raise IdentityApiError("Discord ID confirmado duplicado ou inválido")
            seen_discord_ids.add(discord_id)
            discord_ids.append(discord_id)
        identities.append(ConfirmedIdentity(user_id, tuple(discord_ids)))

    return IdentitySummary(
        requested_user_id=requested_user_id,
        confirmed_identities=tuple(identities),
        other_account_count=other_account_count,
        warning_count=warning_count,
    )


async def get_identity_summary(
    discord_user_id: int,
    community_id: int,
    *,
    session_factory=aiohttp.ClientSession,
) -> IdentitySummary:
    base_url = os.getenv("BOT_API_BASE_URL", DEFAULT_API_BASE_URL).strip().rstrip("/")
    token = os.getenv("BOT_STATUS_API_TOKEN", "").strip()
    if not base_url or not token:
        raise IdentityApiError("Integração de identidade com a API não configurada")

    url = f"{base_url}/internal/identity/discord-users/{int(discord_user_id)}"
    timeout = aiohttp.ClientTimeout(total=5)
    try:
        async with session_factory(timeout=timeout) as session:
            async with session.get(
                url,
                params={"communityId": int(community_id)},
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if response.status != 200:
                    raise IdentityApiError(
                        f"API de identidade respondeu com status {response.status}"
                    )
                payload = await response.json()
    except IdentityApiError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as error:
        raise IdentityApiError("Falha ao consultar a API de identidade") from error

    return _parse_identity_summary(payload, community_id)
