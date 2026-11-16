import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.channel_permission_copy import (
    ChannelPermissionConflict,
    apply_channel_permission_copy,
    build_channel_permission_copy_preview,
    validate_channel_permission_copy,
)


class Overwrite:
    def __init__(self, allow, deny):
        self.allow, self.deny = allow, deny

    def pair(self):
        return SimpleNamespace(value=self.allow), SimpleNamespace(value=self.deny)


class Channel:
    def __init__(self, guild, channel_id, acl, *, kind="text"):
        self.guild, self.id, self.name, self.type = guild, channel_id, f"canal-{channel_id}", kind
        self.overwrites = dict(acl)
        self.edit = AsyncMock(side_effect=self._edit)

    async def _edit(self, **kwargs):
        self.overwrites = dict(kwargs["overwrites"])

    def permissions_for(self, member):
        return SimpleNamespace(manage_channels=member.guild_permissions.manage_channels)


class Guild:
    def __init__(self):
        self.id, self.owner_id = 900, 1
        perms = SimpleNamespace(manage_guild=True, manage_channels=True, administrator=False)
        self.members = {1: SimpleNamespace(guild_permissions=perms)}
        self.me = SimpleNamespace(guild_permissions=perms)
        principal = SimpleNamespace(id=23, name="staff")
        # Namespace hashes by identity and can act as overwrite map keys.
        self.source = Channel(self, 100, {principal: Overwrite(1024, 0)})
        self.target = Channel(self, 200, {})
        self.channels = {100: self.source, 200: self.target}

    def get_member(self, actor):
        return self.members.get(actor)

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)


def payload(**updates):
    request = {"sourceChannelId": "100", "targetChannelId": "200"}
    request.update(updates)
    return request


def test_preview_is_read_only_and_counts_both_acl_sets():
    guild = Guild()
    source, target = validate_channel_permission_copy(guild, 1, payload())
    result = build_channel_permission_copy_preview(source, target)
    assert result["changed"] is True
    assert result["appliedCount"] == 1
    assert result["replacedCount"] == 0
    assert len(result["fingerprint"]) == 64
    target.edit.assert_not_called()


def test_authorization_and_guild_isolation_fail_closed():
    guild = Guild()
    with pytest.raises(PermissionError):
        validate_channel_permission_copy(guild, 2, payload())
    with pytest.raises(ValueError):
        validate_channel_permission_copy(guild, 1, payload(targetChannelId="300"))
    with pytest.raises(ValueError):
        validate_channel_permission_copy(guild, 1, payload(targetChannelId="100"))
    foreign = Guild()
    foreign.id = 901
    guild.channels[300] = Channel(foreign, 300, {})
    with pytest.raises(ValueError):
        validate_channel_permission_copy(guild, 1, payload(targetChannelId="300"))
    guild.me.guild_permissions.manage_channels = False
    with pytest.raises(PermissionError):
        validate_channel_permission_copy(guild, 1, payload())


def test_apply_atomically_replaces_destination_and_rejects_stale_preview():
    guild = Guild()
    before = build_channel_permission_copy_preview(guild.source, guild.target)
    result = asyncio.run(apply_channel_permission_copy(guild, 1, payload(fingerprint=before["fingerprint"])))
    assert result["changed"] is True
    guild.target.edit.assert_awaited_once()
    assert "overwrites" in guild.target.edit.await_args.kwargs
    assert guild.target.overwrites == guild.source.overwrites
    with pytest.raises(ChannelPermissionConflict):
        asyncio.run(apply_channel_permission_copy(guild, 1, payload(fingerprint=before["fingerprint"])))
    assert guild.target.edit.await_count == 1


def test_noop_copy_preserves_destination_and_needs_no_edit():
    guild = Guild()
    guild.target.overwrites = dict(guild.source.overwrites)
    before = build_channel_permission_copy_preview(guild.source, guild.target)
    result = asyncio.run(apply_channel_permission_copy(guild, 1, payload(fingerprint=before["fingerprint"])))
    assert result["changed"] is False
    guild.target.edit.assert_not_called()


def test_concurrent_copies_to_same_target_fail_stale_second_snapshot():
    async def run():
        guild = Guild()
        before = build_channel_permission_copy_preview(guild.source, guild.target)
        return await asyncio.gather(
            apply_channel_permission_copy(guild, 1, payload(fingerprint=before["fingerprint"])),
            apply_channel_permission_copy(guild, 1, payload(fingerprint=before["fingerprint"])),
            return_exceptions=True,
        ), guild
    results, guild = asyncio.run(run())
    assert sum(isinstance(x, ChannelPermissionConflict) for x in results) == 1
    assert guild.target.edit.await_count == 1
