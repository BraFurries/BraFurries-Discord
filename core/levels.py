from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
from typing import Mapping, Any


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def xp_to_reach_level(level: int, config: Mapping[str, Any]) -> int:
    """Return total XP required to reach ``level`` with the configured curve."""

    if level <= 0:
        return 0

    phase1_k = _decimal(config.get("phase1K", 12))
    phase1_p = _decimal(config.get("phase1P", 2))
    phase1_b = _decimal(config.get("phase1B", 0))
    decimal_level = Decimal(level)
    required = phase1_k * (decimal_level ** phase1_p) + (phase1_b * decimal_level)
    return int(required.to_integral_value(rounding=ROUND_FLOOR))


def level_from_total_xp(total_xp: int, config: Mapping[str, Any], max_iterations: int = 10_000) -> int:
    """Return level inferred from ``total_xp`` for the configured progression."""

    if total_xp <= 0:
        return 0

    low = 0
    high = 1
    iterations = 0
    while xp_to_reach_level(high, config) <= total_xp and iterations < max_iterations:
        high *= 2
        iterations += 1

    # If we cannot bracket an upper bound within the iteration budget,
    # return a conservative finite fallback instead of an artificial level.
    if iterations >= max_iterations and xp_to_reach_level(high, config) <= total_xp:
        return total_xp

    while low < high:
        mid = (low + high + 1) // 2
        if xp_to_reach_level(mid, config) <= total_xp:
            low = mid
        else:
            high = mid - 1
    return low


def apply_xp_multipliers(base_xp: int, config: Mapping[str, Any], combo_count: int | None = None) -> int:
    """Apply server multiplier and optional daily combo multiplier to base XP."""

    amount = Decimal(max(base_xp, 0))

    multiplier = config.get("multiplier")
    if multiplier is not None:
        amount *= _decimal(multiplier)

    daily_combo = config.get("dailyCombo")
    combo_multiplier = config.get("comboMultiplier")
    if (
        combo_count is not None
        and daily_combo is not None
        and combo_multiplier is not None
        and combo_count >= int(daily_combo)
    ):
        amount *= _decimal(combo_multiplier)

    return int(amount.to_integral_value(rounding=ROUND_FLOOR))
