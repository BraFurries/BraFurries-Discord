import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from message_services.bot_status_api import BotStatusApi, build_member_state


class FakeRole:
    def __init__(self, role_id, name, position, *, default=False, managed=False, color="#000000"):
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = managed
        self.color = color
        self._default = default

    def is_default(self):
        return self._default

    def __lt__(self, other):
        if self.position != other.position:
            return self.position < other.position
        # Mirrors discord.py's role hierarchy tie-breaker: for equal positions,
        # the lower snowflake ID ranks higher.
        return self.id > other.id


def test_member_state_is_minimal_and_orders_roles_by_hierarchy():
    member = SimpleNamespace(
        id=42,
        name="fox",
        display_name="Fox",
        nick="Raposa",
        joined_at=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
        guild=SimpleNamespace(id=99),
        roles=[
            FakeRole(99, "@everyone", 0, default=True),
            FakeRole(2, "Membro", 2),
            FakeRole(4, "Membro novo", 2),
            FakeRole(3, "Staff", 10, managed=True, color="#8a3594"),
        ],
    )

    payload = build_member_state(member)

    assert payload["guildId"] == "99"
    assert payload["discordUserId"] == "42"
    assert payload["nickname"] == "Raposa"
    assert [role["name"] for role in payload["roles"]] == ["Staff", "Membro", "Membro novo"]
    assert "permissions" not in payload
    assert all(role["name"] != "@everyone" for role in payload["roles"])


def test_member_state_returns_503_while_discord_runtime_is_not_ready():
    bot = SimpleNamespace(is_ready=lambda: False)
    api = BotStatusApi(bot, "127.0.0.1", 0, token="internal-token", initialized_getter=lambda: True)
    request = SimpleNamespace(match_info={"guild_id": "99", "discord_user_id": "42"})

    response = asyncio.run(api._handle_member_state(request))

    assert response.status == 503


def test_member_state_fails_closed_without_internal_token():
    api = BotStatusApi(SimpleNamespace(), "127.0.0.1", 0, token=None)
    response = asyncio.run(api._handle_member_state(SimpleNamespace(match_info={})))

    assert response.status == 503
