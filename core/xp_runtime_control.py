from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from core.levels import xp_to_reach_level
from core.xp_reconciliation import reconcile_guild_levels_serialized


XP_CONFIG_FIELD_MAP: dict[str, str] = {
    "clear_on_exit": "clearOnExit",
    "levelup_warning": "levelupWarning",
    "levelup_warning_channel": "levelupWarningChannel",
    "level_up_message": "levelUpMessage",
    "multiplier": "multiplier",
    "phase1_k": "phase1K",
    "phase1_p": "phase1P",
    "phase1_b": "phase1B",
    "daily_combo": "dailyCombo",
    "combo_multiplier": "comboMultiplier",
    "xp_base_per_min": "xpBasePerMin",
    "voice_social_bonus_pct": "voiceSocialBonusPct",
    "voice_social_bonus_min_humans": "voiceSocialBonusMinHumans",
    "voice_diminishing_window1_minutes": "voiceDiminishingWindow1Minutes",
    "voice_diminishing_window2_minutes": "voiceDiminishingWindow2Minutes",
    "voice_diminishing_factor2": "voiceDiminishingFactor2",
    "voice_diminishing_factor3": "voiceDiminishingFactor3",
    "voice_daily_cap_xp": "voiceDailyCapXp",
    "text_daily_cap_xp": "textDailyCapXp",
    "global_daily_cap_xp": "globalDailyCapXp",
    "text_xp_enabled": "textXpEnabled",
    "voice_xp_enabled": "voiceXpEnabled",
    "text_xp_base_min": "textXpBaseMin",
    "text_xp_base_max": "textXpBaseMax",
    "text_xp_cooldown_min_seconds": "textXpCooldownMinSeconds",
    "text_xp_cooldown_max_seconds": "textXpCooldownMaxSeconds",
}

DEFAULT_SIMULATION_LEVELS = (1, 5, 10, 25, 50, 75, 100)
MAX_SIMULATION_LEVEL = 1000
MAX_SIMULATION_POINTS = 250

logger = logging.getLogger(__name__)

_reconcile_tasks: dict[int, asyncio.Task] = {}


def _normalize_levels(raw_levels: Any) -> list[int]:
    if raw_levels is None:
        return list(DEFAULT_SIMULATION_LEVELS)
    if not isinstance(raw_levels, list) or not raw_levels:
        raise ValueError("levels must be a non-empty array")
    if len(raw_levels) > MAX_SIMULATION_POINTS:
        raise ValueError("too many simulation levels")

    levels: set[int] = set()
    for raw in raw_levels:
        try:
            level = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("simulation levels must be integers") from exc
        if level < 1 or level > MAX_SIMULATION_LEVEL:
            raise ValueError("simulation level out of range")
        levels.add(level)
    return sorted(levels)


def _merge_candidate(
    current_config: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    unknown = sorted(set(candidate) - set(XP_CONFIG_FIELD_MAP))
    if unknown:
        raise ValueError(f"unsupported XP config fields: {', '.join(unknown)}")

    merged = dict(current_config)
    for snake_name, value in candidate.items():
        merged[XP_CONFIG_FIELD_MAP[snake_name]] = value
    return merged


async def simulate_xp_runtime(guild_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    from core.database import (
        _validate_level_config_payload,
        async_get_level_config,
    )

    candidate = payload.get("config") or {}
    if not isinstance(candidate, Mapping):
        raise ValueError("config must be an object")

    current = await async_get_level_config(int(guild_id))
    _validate_level_config_payload(candidate, current_config=current)
    effective = _merge_candidate(current, candidate)
    levels = _normalize_levels(payload.get("levels"))

    points = []
    for level in levels:
        total = int(xp_to_reach_level(level, effective))
        previous_total = int(xp_to_reach_level(level - 1, effective))
        points.append(
            {
                "level": level,
                "totalXp": total,
                "xpFromPreviousLevel": max(0, total - previous_total),
            }
        )

    return {
        "guildId": str(guild_id),
        "points": points,
        "curve": {
            "phase1K": str(effective["phase1K"]),
            "phase1P": str(effective["phase1P"]),
            "phase1B": str(effective["phase1B"]),
        },
    }


def _drop_finished_reconcile_task(guild_id: int, task: asyncio.Task) -> None:
    current = _reconcile_tasks.get(guild_id)
    if current is task:
        _reconcile_tasks.pop(guild_id, None)
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is not None:
        logger.error(
            "Background XP level reconciliation failed for guild %s",
            guild_id,
            exc_info=(type(error), error, error.__traceback__),
        )


async def refresh_xp_runtime(guild_id: int) -> dict[str, Any]:
    from core.database import clearLevelConfigCache, async_get_level_config

    guild_id = int(guild_id)
    clearLevelConfigCache(guild_id)
    config = await async_get_level_config(guild_id, use_cache=False)
    pending = bool(config.get("levelReconcileRequired"))

    if pending:
        task = _reconcile_tasks.get(guild_id)
        if task is None or task.done():
            task = asyncio.create_task(reconcile_guild_levels_serialized(guild_id))
            _reconcile_tasks[guild_id] = task
            task.add_done_callback(
                lambda completed, gid=guild_id: _drop_finished_reconcile_task(gid, completed)
            )

    return {
        "guildId": str(guild_id),
        "cacheInvalidated": True,
        "levelReconciliationPending": pending,
    }
