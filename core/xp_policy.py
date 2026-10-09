from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from core.levels import apply_xp_multipliers


@dataclass(frozen=True)
class XpAwardResult:
    granted_xp: int
    blocked: bool = False
    reason: str | None = None


class XpPolicy:
    """Unified XP policy for text + voice sources with global and per-source caps."""

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _global_daily_cap(config: Mapping[str, Any]) -> int:
        combined = XpPolicy._as_int(config.get("globalDailyCapXp"), 0)
        if combined > 0:
            return combined
        text_cap = XpPolicy._as_int(config.get("textDailyCapXp"), 0)
        voice_cap = XpPolicy._as_int(config.get("voiceDailyCapXp"), 0)
        return max(0, text_cap + voice_cap)

    @staticmethod
    def award_text_xp(base_xp: int, config: Mapping[str, Any], today_total_xp: int, today_source_xp: int = 0) -> XpAwardResult:
        if config.get("textXpEnabled") is False:
            return XpAwardResult(0, blocked=True, reason="source_disabled")
        if base_xp <= 0:
            return XpAwardResult(0, blocked=True, reason="invalid_base")
        gained = apply_xp_multipliers(base_xp, config)
        global_cap = XpPolicy._as_int(config.get("globalDailyCapXp"), 0)
        source_cap = XpPolicy._as_int(config.get("textDailyCapXp"), 0)
        return XpPolicy._apply_caps(gained, today_total_xp, today_source_xp, global_cap, source_cap)

    @staticmethod
    def award_voice_xp(
        base_xp: int,
        config: Mapping[str, Any],
        today_total_xp: int,
        activity_factor: float = 1.0,
        today_source_xp: int = 0,
    ) -> XpAwardResult:
        if config.get("voiceXpEnabled") is False:
            return XpAwardResult(0, blocked=True, reason="source_disabled")
        if base_xp <= 0 or activity_factor <= 0:
            return XpAwardResult(0, blocked=True, reason="invalid_base")
        gained = apply_xp_multipliers(base_xp, config)
        gained = int(Decimal(gained) * Decimal(str(activity_factor)))
        global_cap = XpPolicy._as_int(config.get("globalDailyCapXp"), 0)
        source_cap = XpPolicy._as_int(config.get("voiceDailyCapXp"), 0)
        return XpPolicy._apply_caps(gained, today_total_xp, today_source_xp, global_cap, source_cap)

    @staticmethod
    def _apply_caps(gained_xp: int, today_total_xp: int, today_source_xp: int, global_cap: int, source_cap: int) -> XpAwardResult:
        if gained_xp <= 0:
            return XpAwardResult(0, blocked=True, reason="non_positive")
        remaining_candidates = [gained_xp]
        if global_cap > 0:
            remaining_candidates.append(max(0, global_cap - max(0, today_total_xp)))
        if source_cap > 0:
            remaining_candidates.append(max(0, source_cap - max(0, today_source_xp)))
        granted = min(remaining_candidates)
        if granted <= 0:
            return XpAwardResult(0, blocked=True, reason="daily_cap_reached")
        return XpAwardResult(granted)
