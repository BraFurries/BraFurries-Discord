import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.disboard_bump import resolve_assignable_bump_role

with patch("mysql.connector.pooling.MySQLConnectionPool"):
    from message_services import discord_service


class Role:
    def __init__(self, role_id, position, *, managed=False):
        self.id = role_id
        self.position = position
        self.managed = managed

    def __lt__(self, other):
        return (self.position, self.id) < (other.position, other.id)

    def __gt__(self, other):
        return (self.position, self.id) > (other.position, other.id)


class Guild:
    def __init__(self, *, manage_roles=True, role=None, member=None, top_position=10):
        self.id = 123
        self.default_role = Role(1, 0)
        self.role = role
        self.member = member
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_roles=manage_roles),
            top_role=Role(999, top_position),
        )

    def get_role(self, role_id):
        return self.role if self.role and self.role.id == role_id else None

    def get_member(self, _member_id):
        return self.member


def test_live_role_validation_rejects_missing_managed_default_permission_and_hierarchy():
    member = SimpleNamespace()
    assert resolve_assignable_bump_role(Guild(role=None, member=member), 10) is None
    assert resolve_assignable_bump_role(Guild(role=Role(10, 2, managed=True), member=member), 10) is None
    no_permission = Guild(manage_roles=False, role=Role(10, 2), member=member)
    assert resolve_assignable_bump_role(no_permission, 10) is None
    above = Guild(role=Role(10, 11), member=member)
    assert resolve_assignable_bump_role(above, 10) is None
    equal = Guild(role=Role(999, 10), member=member)
    assert resolve_assignable_bump_role(equal, 999) is None
    default = Guild(role=None, member=member)
    default.role = default.default_role
    assert resolve_assignable_bump_role(default, 1) is None
    valid = Guild(role=Role(10, 2), member=member)
    assert resolve_assignable_bump_role(valid, 10) is valid.role


async def inline_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def message_for(guild, *, channel_id=50, created_at=None):
    bumper = SimpleNamespace(id=7, mention="<@7>")
    return SimpleNamespace(
        id=1000 + channel_id,
        interaction=SimpleNamespace(name="bump", user=bumper),
        guild=guild,
        channel=SimpleNamespace(id=channel_id, send=AsyncMock()),
        created_at=created_at or datetime.now(timezone.utc),
        content="Bump done!",
        embeds=[],
    )


def install_handler_fakes(monkeypatch, config, *, allowed=None):
    monkeypatch.setattr(discord_service.asyncio, "to_thread", inline_to_thread)
    monkeypatch.setattr(discord_service, "getBumpConfig", Mock(return_value=config))
    monkeypatch.setattr(discord_service, "get_allowed_feature_channels", Mock(return_value=allowed or []))
    monkeypatch.setattr(discord_service, "setBumpWarningSchedule", Mock())
    monkeypatch.setattr(discord_service, "adjust_user_economy_balance", Mock())
    monkeypatch.setattr(discord_service, "assignTempRole", AsyncMock(return_value=True))
    discord_service.processed_disboard_messages.clear()
    discord_service._bump_warning_locks.clear()


def reward_config(**overrides):
    config = {
        "warnEnabled": True,
        "rewardCoinsEnabled": True,
        "rewardCoins": 25,
        "rewardEnabled": True,
        "rewardTempRoleId": 10,
        "rewardRoleMinutes": 120,
        "rewardMessage": "Obrigado pelo bump!",
    }
    config.update(overrides)
    return config


def test_valid_role_uses_configured_minutes_reason_and_reward_message(monkeypatch):
    member = SimpleNamespace(id=7)
    role = Role(10, 2)
    guild = Guild(role=role, member=member)
    message = message_for(guild)
    install_handler_fakes(monkeypatch, reward_config())

    before = discord_service.now()
    asyncio.run(discord_service.handle_disboard_bump(message))
    after = discord_service.now()

    discord_service.assignTempRole.assert_awaited_once()
    guild_id, assigned_member, role_id, expires_at, reason = discord_service.assignTempRole.await_args.args
    assert (guild_id, assigned_member, role_id, reason) == (123, member, 10, "Bump Individual Reward")
    assert before.replace(microsecond=0) <= (expires_at - discord_service.timedelta(minutes=120)).replace(microsecond=0) <= after.replace(microsecond=0)
    message.channel.send.assert_awaited_once_with("<@7>, Obrigado pelo bump!", delete_after=30)


@pytest.mark.parametrize("updates", [
    {"rewardEnabled": False}, {"rewardTempRoleId": None}, {"rewardRoleMinutes": 0},
])
def test_disabled_incomplete_role_reward_does_nothing(monkeypatch, updates):
    guild = Guild(role=Role(10, 2), member=SimpleNamespace(id=7))
    message = message_for(guild)
    install_handler_fakes(monkeypatch, reward_config(**updates))
    asyncio.run(discord_service.handle_disboard_bump(message))
    discord_service.assignTempRole.assert_not_awaited()
    message.channel.send.assert_not_awaited()


def test_role_failure_does_not_block_coins_and_does_not_announce(monkeypatch):
    guild = Guild(role=Role(10, 2), member=SimpleNamespace(id=7))
    message = message_for(guild)
    install_handler_fakes(monkeypatch, reward_config())
    discord_service.assignTempRole.side_effect = RuntimeError("role failure")
    asyncio.run(discord_service.handle_disboard_bump(message))
    discord_service.adjust_user_economy_balance.assert_called_once()
    message.channel.send.assert_not_awaited()


def test_valid_role_without_reward_message_does_not_send(monkeypatch):
    guild = Guild(role=Role(10, 2), member=SimpleNamespace(id=7))
    message = message_for(guild)
    install_handler_fakes(monkeypatch, reward_config(rewardMessage=None))
    asyncio.run(discord_service.handle_disboard_bump(message))
    discord_service.assignTempRole.assert_awaited_once()
    message.channel.send.assert_not_awaited()


def test_coin_failure_does_not_block_role_and_send_failure_does_not_escape(monkeypatch):
    guild = Guild(role=Role(10, 2), member=SimpleNamespace(id=7))
    message = message_for(guild)
    install_handler_fakes(monkeypatch, reward_config())
    discord_service.adjust_user_economy_balance.side_effect = RuntimeError("coin failure")
    message.channel.send.side_effect = RuntimeError("send failure")
    asyncio.run(discord_service.handle_disboard_bump(message))
    discord_service.assignTempRole.assert_awaited_once()
    message.channel.send.assert_awaited_once()


def test_processing_gate_blocks_all_reward_and_warning_effects(monkeypatch):
    guild = Guild(role=Role(10, 2), member=SimpleNamespace(id=7))
    message = message_for(guild, channel_id=50)
    install_handler_fakes(monkeypatch, reward_config(), allowed=[99])
    asyncio.run(discord_service.handle_disboard_bump(message))
    discord_service.adjust_user_economy_balance.assert_not_called()
    discord_service.assignTempRole.assert_not_awaited()
    message.channel.send.assert_not_awaited()
    discord_service.setBumpWarningSchedule.assert_not_called()


def test_processing_channel_wins_over_legacy_warning_source(monkeypatch):
    guild = Guild(role=Role(10, 2), member=SimpleNamespace(id=7))
    config = reward_config(warnDisboardChannelId=60)
    install_handler_fakes(monkeypatch, config, allowed=[50])

    accepted = message_for(guild, channel_id=50)
    asyncio.run(discord_service.handle_disboard_bump(accepted))
    discord_service.setBumpWarningSchedule.assert_called_once()
    discord_service.adjust_user_economy_balance.assert_called_once()

    install_handler_fakes(monkeypatch, config, allowed=[50])
    rejected = message_for(guild, channel_id=60)
    asyncio.run(discord_service.handle_disboard_bump(rejected))
    discord_service.setBumpWarningSchedule.assert_not_called()
    discord_service.adjust_user_economy_balance.assert_not_called()


def test_legacy_warning_source_is_realtime_fallback(monkeypatch):
    guild = Guild(role=Role(10, 2), member=SimpleNamespace(id=7))
    message = message_for(guild, channel_id=60)
    install_handler_fakes(monkeypatch, reward_config(warnDisboardChannelId=60), allowed=[])

    asyncio.run(discord_service.handle_disboard_bump(message))

    discord_service.setBumpWarningSchedule.assert_called_once()
    discord_service.adjust_user_economy_balance.assert_called_once()


def test_no_realtime_channel_preserves_acceptance_in_any_channel(monkeypatch):
    guild = Guild(role=Role(10, 2), member=SimpleNamespace(id=7))
    message = message_for(guild, channel_id=987)
    install_handler_fakes(monkeypatch, reward_config(warnDisboardChannelId=None), allowed=[])

    asyncio.run(discord_service.handle_disboard_bump(message))

    discord_service.setBumpWarningSchedule.assert_called_once()
    discord_service.adjust_user_economy_balance.assert_called_once()


def test_disabled_warning_records_last_bump_clears_next_and_keeps_rewards(monkeypatch):
    guild = Guild(role=Role(10, 2), member=SimpleNamespace(id=7))
    bump_at = datetime(2026, 9, 30, 20, 30, tzinfo=timezone.utc)
    message = message_for(guild, channel_id=50, created_at=bump_at)
    install_handler_fakes(monkeypatch, reward_config(warnEnabled=False), allowed=[50])

    asyncio.run(discord_service.handle_disboard_bump(message))

    schedule = discord_service.setBumpWarningSchedule.call_args
    assert schedule.kwargs["next_at"] is None
    assert schedule.kwargs["last_bump_at"] == datetime(2026, 9, 30, 17, 30)
    discord_service.adjust_user_economy_balance.assert_called_once()


def test_valid_bump_restarts_warning_timer_from_bump_moment(monkeypatch):
    guild = Guild(role=Role(10, 2), member=SimpleNamespace(id=7))
    bump_at = datetime(2026, 9, 30, 20, 30, tzinfo=timezone.utc)
    message = message_for(guild, channel_id=50, created_at=bump_at)
    install_handler_fakes(monkeypatch, reward_config(warnNextAt=datetime(2026, 9, 30, 18, 56)), allowed=[50])

    asyncio.run(discord_service.handle_disboard_bump(message))

    schedule = discord_service.setBumpWarningSchedule.call_args
    assert schedule.kwargs["last_bump_at"] == datetime(2026, 9, 30, 17, 30)
    assert schedule.kwargs["next_at"] == datetime(2026, 9, 30, 19, 30)


def test_schedule_failure_does_not_block_individual_rewards(monkeypatch):
    guild = Guild(role=Role(10, 2), member=SimpleNamespace(id=7))
    message = message_for(guild, channel_id=50)
    install_handler_fakes(monkeypatch, reward_config(), allowed=[50])
    discord_service.setBumpWarningSchedule.side_effect = RuntimeError("schedule failure")

    asyncio.run(discord_service.handle_disboard_bump(message))

    discord_service.adjust_user_economy_balance.assert_called_once()
    discord_service.assignTempRole.assert_awaited_once()


class HistoryChannel:
    def __init__(self, channel_id, messages):
        self.id = channel_id
        self._messages = messages

    async def history(self, *, limit):
        for message in self._messages[:limit]:
            yield message


def install_warning_scheduler_fakes(monkeypatch, *, current_time, next_at, persisted_next_at=None):
    disboard_message = SimpleNamespace(
        author=SimpleNamespace(id=discord_service.DISBOARD_BOT_ID),
        jump_url="https://discord.test/jump",
    )
    source = HistoryChannel(50, [disboard_message])
    target = SimpleNamespace(id=70, send=AsyncMock())
    guild = SimpleNamespace(id=123)
    guild.get_channel = Mock(side_effect=lambda channel_id: {50: source, 70: target}.get(channel_id))
    monkeypatch.setattr(discord_service, "now", Mock(return_value=current_time))
    monkeypatch.setattr(discord_service.bot, "get_guild", Mock(return_value=guild))
    monkeypatch.setattr(discord_service, "get_allowed_feature_channels", Mock(return_value=[50]))
    monkeypatch.setattr(discord_service, "listBumpWarningConfigs", Mock(return_value=[{
        "guildId": 123,
        "disboardChannelId": 60,
        "targetChannelId": 70,
        "messages": ["Hora do bump!"],
        "nextAt": next_at,
    }]))
    monkeypatch.setattr(discord_service, "getBumpConfig", Mock(return_value={
        "warnEnabled": True,
        "warnNextAt": persisted_next_at if persisted_next_at is not None else next_at,
    }))
    monkeypatch.setattr(discord_service, "setBumpWarningSchedule", Mock())
    discord_service.processed_disboard_messages.clear()
    discord_service._bump_warning_locks.clear()
    return source, target


def test_warning_scheduler_recurs_two_hours_after_success(monkeypatch):
    due_at = datetime(2026, 9, 30, 18, 56)
    _, target = install_warning_scheduler_fakes(monkeypatch, current_time=due_at, next_at=due_at)

    asyncio.run(discord_service.bumpWarning.coro())

    target.send.assert_awaited_once_with("Hora do bump! https://discord.test/jump")
    discord_service.setBumpWarningSchedule.assert_called_once_with(
        123, next_at=due_at + timedelta(hours=2)
    )


def test_warning_scheduler_ignores_stale_snapshot_after_new_bump(monkeypatch):
    due_at = datetime(2026, 9, 30, 18, 56)
    new_next_at = datetime(2026, 9, 30, 20, 57)
    _, target = install_warning_scheduler_fakes(
        monkeypatch,
        current_time=datetime(2026, 9, 30, 18, 57),
        next_at=due_at,
        persisted_next_at=new_next_at,
    )

    asyncio.run(discord_service.bumpWarning.coro())

    target.send.assert_not_awaited()
    discord_service.setBumpWarningSchedule.assert_not_called()


def test_warning_scheduler_waits_for_timer_restarted_by_new_bump(monkeypatch):
    restarted_next_at = datetime(2026, 9, 30, 21, 30)
    _, target = install_warning_scheduler_fakes(
        monkeypatch,
        current_time=datetime(2026, 9, 30, 20, 56),
        next_at=restarted_next_at,
    )

    asyncio.run(discord_service.bumpWarning.coro())

    target.send.assert_not_awaited()
    discord_service.setBumpWarningSchedule.assert_not_called()


def test_warning_send_and_concurrent_bump_timer_update_are_serialized(monkeypatch):
    async def scenario():
        due_at = datetime(2026, 9, 30, 18, 56)
        bump_at = datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc)
        _, target = install_warning_scheduler_fakes(monkeypatch, current_time=due_at, next_at=due_at)
        monkeypatch.setattr(discord_service.asyncio, "to_thread", inline_to_thread)
        monkeypatch.setattr(discord_service, "adjust_user_economy_balance", Mock())
        monkeypatch.setattr(discord_service, "assignTempRole", AsyncMock(return_value=True))
        entered_send = asyncio.Event()
        release_send = asyncio.Event()

        async def blocked_send(*_args, **_kwargs):
            entered_send.set()
            await release_send.wait()

        target.send.side_effect = blocked_send
        guild = Guild(role=Role(10, 2), member=SimpleNamespace(id=7))
        guild.get_channel = discord_service.bot.get_guild.return_value.get_channel
        message = message_for(guild, channel_id=50, created_at=bump_at)

        scheduler_task = asyncio.create_task(discord_service.bumpWarning.coro())
        await entered_send.wait()
        bump_task = asyncio.create_task(discord_service.handle_disboard_bump(message))
        await asyncio.sleep(0)

        discord_service.setBumpWarningSchedule.assert_not_called()

        release_send.set()
        await asyncio.gather(scheduler_task, bump_task)

        assert discord_service.setBumpWarningSchedule.call_count == 2
        scheduler_call, bump_call = discord_service.setBumpWarningSchedule.call_args_list
        assert scheduler_call.args == (123,)
        assert scheduler_call.kwargs["next_at"] == due_at + timedelta(hours=2)
        assert bump_call.args == (123,)
        assert bump_call.kwargs["last_bump_at"] == datetime(2026, 9, 30, 19, 30)
        assert bump_call.kwargs["next_at"] == datetime(2026, 9, 30, 21, 30)

    asyncio.run(scenario())


def test_warning_scheduler_does_not_send_stale_warning_when_bump_holds_lock_first(monkeypatch):
    async def scenario():
        due_at = datetime(2026, 9, 30, 18, 56)
        bump_at = datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc)
        bump_next_at = datetime(2026, 9, 30, 21, 30)
        _, target = install_warning_scheduler_fakes(monkeypatch, current_time=due_at, next_at=due_at)
        stored_next_at = due_at
        entered_bump_schedule = asyncio.Event()
        release_bump_schedule = asyncio.Event()

        def config_for_timer():
            return reward_config(
                warnNextAt=stored_next_at,
                rewardCoinsEnabled=False,
                rewardEnabled=False,
                rewardMessage=None,
            )

        def get_bump_config(_guild_id):
            config = config_for_timer()
            config["warnNextAt"] = stored_next_at
            return config

        async def controlled_to_thread(function, *args, **kwargs):
            nonlocal stored_next_at
            if function is discord_service.setBumpWarningSchedule:
                entered_bump_schedule.set()
                await release_bump_schedule.wait()
                stored_next_at = kwargs["next_at"]
            return function(*args, **kwargs)

        def set_schedule(_guild_id, *, next_at, last_bump_at=None):
            nonlocal stored_next_at
            stored_next_at = next_at

        monkeypatch.setattr(discord_service.asyncio, "to_thread", controlled_to_thread)
        monkeypatch.setattr(discord_service, "getBumpConfig", Mock(side_effect=get_bump_config))
        monkeypatch.setattr(discord_service, "setBumpWarningSchedule", Mock(side_effect=set_schedule))
        monkeypatch.setattr(discord_service, "adjust_user_economy_balance", Mock())
        monkeypatch.setattr(discord_service, "assignTempRole", AsyncMock(return_value=True))
        guild = Guild(role=Role(10, 2), member=SimpleNamespace(id=7))
        guild.get_channel = discord_service.bot.get_guild.return_value.get_channel
        message = message_for(guild, channel_id=50, created_at=bump_at)

        bump_task = asyncio.create_task(discord_service.handle_disboard_bump(message))
        await entered_bump_schedule.wait()
        scheduler_task = asyncio.create_task(discord_service.bumpWarning.coro())
        await asyncio.sleep(0)

        target.send.assert_not_awaited()

        release_bump_schedule.set()
        await asyncio.gather(bump_task, scheduler_task)

        assert stored_next_at == bump_next_at
        discord_service.setBumpWarningSchedule.assert_called_once()
        target.send.assert_not_awaited()

    asyncio.run(scenario())
