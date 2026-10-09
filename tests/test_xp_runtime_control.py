import asyncio
import sys
from decimal import Decimal
from types import ModuleType

import pytest

from core.xp_runtime_control import simulate_xp_runtime


def _database_module(config: dict):
    module = ModuleType("core.database")

    async def async_get_level_config(_guild_id: int):
        return dict(config)

    def validate(payload, current_config=None):
        if "phase1_p" in payload and Decimal(str(payload["phase1_p"])) < 1:
            raise ValueError("Invalid value for phase1_p")

    module.async_get_level_config = async_get_level_config
    module._validate_level_config_payload = validate
    return module


def test_simulation_uses_canonical_level_formula(monkeypatch):
    config = {
        "phase1K": Decimal("45"),
        "phase1P": Decimal("2"),
        "phase1B": Decimal("0"),
    }
    monkeypatch.setitem(sys.modules, "core.database", _database_module(config))

    result = asyncio.run(
        simulate_xp_runtime(
            123,
            {
                "config": {
                    "phase1_k": "12",
                    "phase1_p": "2",
                    "phase1_b": "3",
                },
                "levels": [1, 5, 10],
            },
        )
    )

    assert result["guildId"] == "123"
    assert result["points"] == [
        {"level": 1, "totalXp": 15, "xpFromPreviousLevel": 15},
        {"level": 5, "totalXp": 315, "xpFromPreviousLevel": 111},
        {"level": 10, "totalXp": 1230, "xpFromPreviousLevel": 231},
    ]


def test_simulation_rejects_unknown_config_fields(monkeypatch):
    config = {
        "phase1K": Decimal("45"),
        "phase1P": Decimal("2"),
        "phase1B": Decimal("0"),
    }
    monkeypatch.setitem(sys.modules, "core.database", _database_module(config))

    with pytest.raises(ValueError, match="unsupported XP config fields"):
        asyncio.run(
            simulate_xp_runtime(
                123,
                {"config": {"made_up_formula": 999}, "levels": [1]},
            )
        )


def test_simulation_rejects_invalid_levels(monkeypatch):
    config = {
        "phase1K": Decimal("45"),
        "phase1P": Decimal("2"),
        "phase1B": Decimal("0"),
    }
    monkeypatch.setitem(sys.modules, "core.database", _database_module(config))

    with pytest.raises(ValueError, match="simulation level out of range"):
        asyncio.run(simulate_xp_runtime(123, {"levels": [1001]}))
