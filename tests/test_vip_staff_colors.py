import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import discord

from core import routine_functions as runtime


def test_reserved_staff_colors_come_only_from_effective_staff_roles(monkeypatch):
    role = SimpleNamespace(color=discord.Color(0x9900AA))
    guild = SimpleNamespace(id=123, get_role=lambda id: role if id == 44 else None)
    monkeypatch.setattr(runtime, "getVipAllowStaffColorsConfig", lambda guild_id: False)
    monkeypatch.setattr(runtime, "getStaffRoles", lambda guild_id: [44])
    # A stale legacy manual color must never affect the effective VIP check.
    monkeypatch.setattr(runtime, "getGuildStaffColors", Mock(side_effect=AssertionError(
        "manual staff colors must not be read"
    )))
    assert asyncio.run(runtime.colorIsAvailable("#9900AA", guild)) is False
    assert asyncio.run(runtime.colorIsAvailable("#00FF00", guild)) is True


def test_missing_or_uncolored_staff_roles_do_not_reserve_colors(monkeypatch):
    guild = SimpleNamespace(id=345, get_role=lambda id: SimpleNamespace(color=discord.Color(0)))
    monkeypatch.setattr(runtime, "getVipAllowStaffColorsConfig", lambda guild_id: False)
    monkeypatch.setattr(runtime, "getStaffRoles", lambda guild_id: [99, 11])
    assert asyncio.run(runtime.colorIsAvailable("#010203", guild)) is True
