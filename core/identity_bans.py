from __future__ import annotations

from dataclasses import dataclass

import discord

from core.identity_api import IdentitySummary


@dataclass(frozen=True)
class BanEffect:
    identity_user_id: int
    discord_user_id: int
    is_origin: bool
    outcome: str
    error_code: str | None = None


def _error_effect(
    identity_user_id: int,
    discord_user_id: int,
    is_origin: bool,
    error: Exception,
) -> BanEffect:
    if isinstance(error, discord.NotFound):
        outcome = "NOT_FOUND"
    elif isinstance(error, discord.Forbidden):
        outcome = "FORBIDDEN"
    else:
        outcome = "FAILED"
    return BanEffect(
        identity_user_id,
        discord_user_id,
        is_origin,
        outcome,
        type(error).__name__[:64],
    )


def _ordered_candidates(
    summary: IdentitySummary,
    origin_discord_id: int,
) -> list[tuple[int, int, bool]]:
    origin_identity = next(
        (
            identity
            for identity in summary.confirmed_identities
            if identity.user_id == summary.requested_user_id
        ),
        None,
    )
    if origin_identity is None or origin_discord_id not in origin_identity.discord_user_ids:
        raise ValueError(
            "A API não vinculou a identidade Discord de origem ao User solicitado"
        )

    candidates = [
        (identity.user_id, discord_id, discord_id == origin_discord_id)
        for identity in summary.confirmed_identities
        for discord_id in identity.discord_user_ids
    ]
    return sorted(candidates, key=lambda item: (not item[2], item[0], item[1]))


def validate_confirmed_ban_origin(
    summary: IdentitySummary,
    origin_discord_id: int,
) -> None:
    """Fail closed before persistence or Discord mutation if the origin is inconsistent."""
    _ordered_candidates(summary, origin_discord_id)


async def propagate_confirmed_ban(
    guild: discord.Guild,
    origin_discord_id: int,
    summary: IdentitySummary,
    *,
    reason: str,
    delete_message_seconds: int = 0,
) -> list[BanEffect]:
    """Apply one Community ban to every known Discord account in the CONFIRMED cluster."""
    effects: list[BanEffect] = []
    for identity_user_id, discord_user_id, is_origin in _ordered_candidates(
        summary, origin_discord_id
    ):
        target = discord.Object(id=discord_user_id)
        try:
            await guild.fetch_ban(target)
        except discord.NotFound:
            pass
        except Exception as error:
            effects.append(
                _error_effect(identity_user_id, discord_user_id, is_origin, error)
            )
            if is_origin:
                return effects
            continue
        else:
            effects.append(
                BanEffect(
                    identity_user_id,
                    discord_user_id,
                    is_origin,
                    "ALREADY_BANNED",
                )
            )
            continue

        audit_reason = reason
        if not is_origin:
            audit_reason = (
                f"[Propagado por identidade confirmada; origem Discord "
                f"{origin_discord_id}] {reason}"
            )
        try:
            await guild.ban(
                target,
                reason=audit_reason[:512],
                delete_message_seconds=delete_message_seconds if is_origin else 0,
            )
        except Exception as error:
            effects.append(
                _error_effect(identity_user_id, discord_user_id, is_origin, error)
            )
            if is_origin:
                return effects
        else:
            effects.append(
                BanEffect(identity_user_id, discord_user_id, is_origin, "APPLIED")
            )
    return effects


async def propagate_new_confirmed_identity_ban(
    guild: discord.Guild,
    candidates: list[tuple[int, int]],
    *,
    reason: str,
) -> list[BanEffect]:
    """Extend an existing Community ban to newly confirmed Discord identities."""
    effects: list[BanEffect] = []
    seen: set[int] = set()
    for identity_user_id, discord_user_id in sorted(candidates):
        if discord_user_id in seen:
            continue
        seen.add(discord_user_id)
        target = discord.Object(id=discord_user_id)
        try:
            await guild.fetch_ban(target)
        except discord.NotFound:
            pass
        except Exception as error:
            effects.append(
                _error_effect(identity_user_id, discord_user_id, False, error)
            )
            continue
        else:
            effects.append(
                BanEffect(
                    identity_user_id,
                    discord_user_id,
                    False,
                    "ALREADY_BANNED",
                )
            )
            continue

        try:
            await guild.ban(
                target,
                reason=(
                    "[Propagação automática após confirmação de identidade] "
                    + reason
                )[:512],
                delete_message_seconds=0,
            )
        except Exception as error:
            effects.append(
                _error_effect(identity_user_id, discord_user_id, False, error)
            )
        else:
            effects.append(
                BanEffect(identity_user_id, discord_user_id, False, "APPLIED")
            )
    return effects


async def compensate_unrecorded_propagated_bans(
    guild: discord.Guild,
    effects: list[BanEffect],
) -> list[int]:
    """Undo untracked propagated bans, preserving the explicit origin action."""
    failed: list[int] = []
    for effect in effects:
        if effect.is_origin or effect.outcome != "APPLIED":
            continue
        try:
            await guild.unban(
                discord.Object(id=effect.discord_user_id),
                reason=(
                    "Reversão automática: falha ao persistir o efeito de "
                    "banimento propagado"
                ),
            )
        except discord.NotFound:
            continue
        except Exception:
            failed.append(effect.discord_user_id)
    return failed


def summarize_ban_effects(effects: list[BanEffect]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for effect in effects:
        summary[effect.outcome] = summary.get(effect.outcome, 0) + 1
    return summary
