import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import discord

import core.routine_functions as routines


class FakeRole:
    def __init__(self, role_id, name, position, *, color=0x123456, created_at=None):
        self.id = role_id
        self.name = name
        self.position = position
        self.color = discord.Color(color)
        self.display_icon = None
        self.members = []
        self.managed = False
        self.deleted = False
        self.created_at = created_at or datetime.now(timezone.utc) - timedelta(hours=1)

    def __gt__(self, other):
        return self.position > other.position

    async def edit(self, *, name=None, reason=None):
        if name is not None:
            self.name = name
        return self

    async def delete(self, *, reason=None):
        self.deleted = True


class FakeMember:
    def __init__(self, user_id, name, roles=None):
        self.id = user_id
        self.name = name
        self.roles = list(roles or [])

    async def add_roles(self, role, *, reason=None):
        if role not in self.roles:
            self.roles.append(role)
        if self not in role.members:
            role.members.append(self)

    async def remove_roles(self, role, *, reason=None):
        if role in self.roles:
            self.roles.remove(role)
        if self in role.members:
            role.members.remove(self)


class FakeGuild:
    def __init__(self, roles, members):
        self.id = 123
        self.roles = list(roles)
        self._members = {member.id: member for member in members}
        self.created_roles = []
        self.position_updates = []
        bot_top = FakeRole(999, "Coddy", 100)
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_roles=True),
            top_role=bot_top,
        )

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)

    def get_member(self, user_id):
        return self._members.get(user_id)

    async def fetch_roles(self):
        return [role for role in self.roles if not role.deleted]

    async def create_role(self, *, name, mentionable, reason):
        role = FakeRole(500 + len(self.created_roles), name, 1, color=0)
        self.roles.append(role)
        self.created_roles.append(role)
        return role

    async def edit_role_positions(self, *, positions):
        self.position_updates.append(positions)
        for role, position in positions.items():
            role.position = position


def test_position_only_role_update_is_not_vip_reconcile_signal():
    before = FakeRole(20, "VIP Nick", 10)
    after = FakeRole(20, "VIP Nick", 11)

    assert routines.shouldReconcileVipRoleUpdate(before, after) is False

    renamed = FakeRole(20, "VIP Nicholas", 11)
    assert routines.shouldReconcileVipRoleUpdate(after, renamed) is True


def test_vip_role_position_updates_are_serialized_per_guild():
    start = FakeRole(100, "VIP START", 50)
    first = FakeRole(20, "VIP Nick", 10)
    second = FakeRole(21, "VIP Maya", 11)
    guild = FakeGuild([start, first, second], [])

    active = 0
    max_active = 0

    async def edit_role_positions(*, positions):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        for role, position in positions.items():
            role.position = position
        active -= 1

    guild.edit_role_positions = edit_role_positions

    async def exercise():
        await asyncio.gather(
            routines.rearrangeRoleInsideInterval(guild, first.id, start, None),
            routines.rearrangeRoleInsideInterval(guild, second.id, start, None),
        )

    asyncio.run(exercise())

    assert max_active == 1


def test_color_policy_can_explicitly_allow_staff_colors(monkeypatch):
    guild = FakeGuild([], [])
    monkeypatch.setattr(routines, "getVipAllowStaffColorsConfig", lambda guild_id: True)
    blocked_lookup = Mock(side_effect=AssertionError("staff colors should not be consulted"))
    monkeypatch.setattr(routines, "getGuildStaffColors", blocked_lookup)

    assert asyncio.run(routines.colorIsAvailable("#abcdef", guild)) is True
    blocked_lookup.assert_not_called()



def test_first_vip_customization_holds_lock_until_role_is_customized(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    member = FakeMember(1, "Nick", [access])
    guild = FakeGuild([access], [member])
    persisted = {}

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "VIP")
    monkeypatch.setattr(routines, "getVIPConfigurations", lambda guild: {
        "VIPRoles": [access],
        "VIPRoleDivisionStartID": None,
        "VIPRoleDivisionEndID": None,
    })
    monkeypatch.setattr(
        routines,
        "getCustomRoleEntry",
        lambda guild_id, user_id: persisted.get((guild_id, user_id)),
    )

    def save_custom_role(guild_id, discord_user, **values):
        key = (guild_id, discord_user.id)
        entry = persisted.setdefault(
            key,
            {"owner_discord_user_id": discord_user.id, "role_id": None},
        )
        if values.get("roleId") is not None:
            entry["role_id"] = values["roleId"]
        return True

    def clear_custom_role(guild_id, user_id):
        entry = persisted.get((guild_id, user_id))
        if entry:
            entry["role_id"] = None
        return True

    monkeypatch.setattr(routines, "saveCustomRole", save_custom_role)
    monkeypatch.setattr(routines, "clearCustomRoleRoleId", clear_custom_role)

    async def exercise():
        ctx = SimpleNamespace(guild=guild, user=member)
        async with routines.vipCustomRoleMutation(ctx) as custom_role:
            assert custom_role.color == discord.Color.default()
            reconcile = asyncio.create_task(
                routines.reconcileVipMember(guild, member.id)
            )
            await asyncio.sleep(0)
            assert reconcile.done() is False
            assert custom_role.deleted is False

            custom_role.color = discord.Color(0x99FBEC)

        result = await reconcile
        return custom_role, result

    custom_role, result = asyncio.run(exercise())

    assert custom_role.deleted is False
    assert custom_role in member.roles
    assert persisted[(guild.id, member.id)]["role_id"] == custom_role.id
    assert result["deleted"] == 0



def test_add_vip_role_replaces_persisted_role_that_only_exists_in_gateway_cache(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    stale = FakeRole(20, "VIP Nick", 10, color=0x654321)
    stale.deleted = True
    member = FakeMember(1, "Nick", [access])
    guild = FakeGuild([access, stale], [member])
    saved = Mock(return_value=True)
    cleared = Mock(return_value=True)

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "VIP")
    monkeypatch.setattr(
        routines,
        "getCustomRoleEntry",
        lambda guild_id, user_id: {
            "role_id": stale.id,
            "owner_discord_user_id": member.id,
            "color": "#654321",
            "color2": None,
            "icon_id": None,
        },
    )
    monkeypatch.setattr(routines, "getVIPConfigurations", lambda guild: {
        "VIPRoles": [access],
        "VIPRoleDivisionStartID": None,
        "VIPRoleDivisionEndID": None,
    })
    monkeypatch.setattr(routines, "saveCustomRole", saved)
    monkeypatch.setattr(routines, "clearCustomRoleRoleId", cleared)

    resolved = asyncio.run(routines.addVipRole(SimpleNamespace(guild=guild, user=member)))

    assert resolved is not stale
    assert resolved in guild.created_roles
    assert resolved in member.roles
    assert resolved.deleted is False
    cleared.assert_called_once_with(guild.id, member.id)
    assert any(call.kwargs.get("roleId") == resolved.id for call in saved.call_args_list)



def test_add_vip_role_recreates_once_when_role_disappears_after_remote_validation(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    existing = FakeRole(20, "VIP Nick", 10, color=0x654321)
    member = FakeMember(1, "Nick", [access])
    guild = FakeGuild([access, existing], [member])
    saved = Mock(return_value=True)
    cleared = Mock(return_value=True)
    original_add_roles = member.add_roles
    attempted_existing = False

    async def add_roles(role, *, reason=None):
        nonlocal attempted_existing
        if role.id == existing.id and not attempted_existing:
            attempted_existing = True
            existing.deleted = True
            raise discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"),
                {"code": 10011, "message": "Unknown Role"},
            )
        await original_add_roles(role, reason=reason)

    member.add_roles = add_roles

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "VIP")
    monkeypatch.setattr(
        routines,
        "getCustomRoleEntry",
        lambda guild_id, user_id: {
            "role_id": existing.id,
            "owner_discord_user_id": member.id,
            "color": "#654321",
            "color2": None,
            "icon_id": None,
        },
    )
    monkeypatch.setattr(routines, "getVIPConfigurations", lambda guild: {
        "VIPRoles": [access],
        "VIPRoleDivisionStartID": None,
        "VIPRoleDivisionEndID": None,
    })
    monkeypatch.setattr(routines, "saveCustomRole", saved)
    monkeypatch.setattr(routines, "clearCustomRoleRoleId", cleared)

    resolved = asyncio.run(routines.addVipRole(SimpleNamespace(guild=guild, user=member)))

    assert attempted_existing is True
    assert resolved is not existing
    assert resolved in guild.created_roles
    assert resolved in member.roles
    cleared.assert_called_once_with(guild.id, member.id)
    assert any(call.kwargs.get("roleId") == resolved.id for call in saved.call_args_list)


def test_add_vip_role_resolves_persisted_role_id_before_name(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    custom = FakeRole(20, "VIP Nick", 10)
    member = FakeMember(1, "Nick", [access])
    guild = FakeGuild([access, custom], [member])
    saved = Mock(return_value=True)

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "Apoiador")
    monkeypatch.setattr(
        routines,
        "getCustomRoleEntry",
        lambda guild_id, user_id: {
            "role_id": 20,
            "owner_discord_user_id": member.id,
        },
    )
    monkeypatch.setattr(routines, "getVIPConfigurations", lambda guild: {
        "VIPRoles": [access],
        "VIPRoleDivisionStartID": None,
        "VIPRoleDivisionEndID": None,
    })
    monkeypatch.setattr(routines, "saveCustomRole", saved)

    ctx = SimpleNamespace(guild=guild, user=member)
    resolved = asyncio.run(routines.addVipRole(ctx))

    assert resolved is custom
    assert guild.created_roles == []
    assert custom.name == "Apoiador Nick"
    assert custom in member.roles
    saved.assert_called_with(guild.id, member, roleId=custom.id)




def test_add_vip_role_recovers_legacy_name_and_persists_role_id(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    legacy = FakeRole(21, "VIP Nick", 10)
    member = FakeMember(1, "Nick", [access])
    guild = FakeGuild([access, legacy], [member])
    saved = Mock(return_value=True)

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "Apoiador")
    monkeypatch.setattr(routines, "getCustomRoleEntry", lambda guild_id, user_id: {"role_id": None})
    monkeypatch.setattr(routines, "getVIPConfigurations", lambda guild: {
        "VIPRoles": [access],
        "VIPRoleDivisionStartID": None,
        "VIPRoleDivisionEndID": None,
    })
    monkeypatch.setattr(routines, "saveCustomRole", saved)

    resolved = asyncio.run(routines.addVipRole(SimpleNamespace(guild=guild, user=member)))

    assert resolved is legacy
    assert guild.created_roles == []
    assert legacy.name == "Apoiador Nick"
    assert any(call.kwargs.get("roleId") == legacy.id for call in saved.call_args_list)


def test_add_vip_role_recovers_previous_non_default_prefix_from_unique_member_owned_role(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    legacy = FakeRole(21, "Apoiador Nick", 10)
    member = FakeMember(1, "Nick", [access, legacy])
    legacy.members = [member]
    guild = FakeGuild([access, legacy], [member])
    saved = Mock(return_value=True)

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "Patrono")
    monkeypatch.setattr(routines, "getCustomRoleEntry", lambda guild_id, user_id: {"role_id": None})
    monkeypatch.setattr(routines, "getVIPConfigurations", lambda guild: {
        "VIPRoles": [access],
        "VIPRoleDivisionStartID": None,
        "VIPRoleDivisionEndID": None,
    })
    monkeypatch.setattr(routines, "saveCustomRole", saved)

    resolved = asyncio.run(routines.addVipRole(SimpleNamespace(guild=guild, user=member)))

    assert resolved is legacy
    assert guild.created_roles == []
    assert legacy.name == "Patrono Nick"
    assert any(call.kwargs.get("roleId") == legacy.id for call in saved.call_args_list)


def test_reconcile_is_non_destructive_when_no_vip_grant_role_resolves(monkeypatch):
    custom = FakeRole(20, "VIP Nick", 10)
    member = FakeMember(1, "Nick", [custom])
    custom.members = [member]
    guild = FakeGuild([custom], [member])
    clear = Mock(return_value=True)
    save = Mock(return_value=True)

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "VIP")
    monkeypatch.setattr(routines, "getVIPConfigurations", lambda guild: {
        "VIPRoles": [],
        "VIPRoleDivisionStartID": None,
        "VIPRoleDivisionEndID": None,
    })
    monkeypatch.setattr(routines, "getAllCustomRoles", Mock(side_effect=AssertionError("must fail safe before loading custom roles")))
    monkeypatch.setattr(routines, "clearCustomRoleRoleId", clear)
    monkeypatch.setattr(routines, "saveCustomRole", save)

    result = asyncio.run(routines.reconcileVipCustomRoles(guild))

    assert result == {
        "processed": 0,
        "updated": 0,
        "deleted": 0,
        "recovered": 0,
        "warnings": ["vip_roles_unavailable"],
    }
    assert not custom.deleted
    assert custom in member.roles
    clear.assert_not_called()
    save.assert_not_called()



def test_targeted_reconcile_preserves_persisted_customization_when_role_cache_is_stale(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    custom = FakeRole(20, "VIP Nick", 10, color=0)
    member = FakeMember(1, "Nick", [access, custom])
    custom.members = [member]
    guild = FakeGuild([access, custom], [member])
    saved = Mock(return_value=True)

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "VIP")
    monkeypatch.setattr(routines, "getVIPConfigurations", lambda guild: {
        "VIPRoles": [access],
        "VIPRoleDivisionStartID": None,
        "VIPRoleDivisionEndID": None,
    })
    monkeypatch.setattr(
        routines,
        "getCustomRoleEntry",
        lambda guild_id, user_id: {
            "role_id": custom.id,
            "owner_discord_user_id": member.id,
            "color": "#99fbec",
            "color2": "#8819d7",
            "icon_id": 1068197782701224117,
        },
    )
    monkeypatch.setattr(routines, "saveCustomRole", saved)

    result = asyncio.run(routines.reconcileVipMember(guild, member.id))

    assert result["deleted"] == 0
    assert custom.deleted is False
    assert custom in member.roles
    assert not any(call.kwargs.get("color") == "#000000" for call in saved.call_args_list)
    assert any(call.kwargs == {"roleId": custom.id} for call in saved.call_args_list)


def test_targeted_reconcile_preserves_fresh_default_role_without_persisted_customization(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    custom = FakeRole(
        20,
        "VIP Nick",
        10,
        color=0,
        created_at=datetime.now(timezone.utc),
    )
    member = FakeMember(1, "Nick", [access, custom])
    custom.members = [member]
    guild = FakeGuild([access, custom], [member])

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "VIP")
    monkeypatch.setattr(routines, "getVIPConfigurations", lambda guild: {
        "VIPRoles": [access],
        "VIPRoleDivisionStartID": None,
        "VIPRoleDivisionEndID": None,
    })
    monkeypatch.setattr(
        routines,
        "getCustomRoleEntry",
        lambda guild_id, user_id: {
            "role_id": custom.id,
            "owner_discord_user_id": member.id,
            "color": None,
            "color2": None,
            "icon_id": None,
        },
    )
    monkeypatch.setattr(routines, "saveCustomRole", Mock(return_value=True))

    result = asyncio.run(routines.reconcileVipMember(guild, member.id))

    assert result["deleted"] == 0
    assert custom.deleted is False
    assert custom in member.roles


def test_targeted_reconcile_cleans_stale_default_role_without_customization(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    custom = FakeRole(
        20,
        "VIP Nick",
        10,
        color=0,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    member = FakeMember(1, "Nick", [access, custom])
    custom.members = [member]
    guild = FakeGuild([access, custom], [member])
    cleared = Mock(return_value=True)

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "VIP")
    monkeypatch.setattr(routines, "getVIPConfigurations", lambda guild: {
        "VIPRoles": [access],
        "VIPRoleDivisionStartID": None,
        "VIPRoleDivisionEndID": None,
    })
    monkeypatch.setattr(
        routines,
        "getCustomRoleEntry",
        lambda guild_id, user_id: {
            "role_id": custom.id,
            "owner_discord_user_id": member.id,
            "color": None,
            "color2": None,
            "icon_id": None,
        },
    )
    monkeypatch.setattr(routines, "clearCustomRoleRoleId", cleared)

    result = asyncio.run(routines.reconcileVipMember(guild, member.id))

    assert result["deleted"] == 1
    assert custom.deleted is True
    cleared.assert_called_once_with(guild.id, member.id)


def test_targeted_reconcile_reassigns_existing_role_without_full_scan(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    custom = FakeRole(20, "VIP Nick", 10, color=0x654321)
    member = FakeMember(1, "Nick", [access])
    guild = FakeGuild([access, custom], [member])
    saved = Mock(return_value=True)

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "VIP")
    monkeypatch.setattr(routines, "getVIPConfigurations", lambda guild: {
        "VIPRoles": [access],
        "VIPRoleDivisionStartID": None,
        "VIPRoleDivisionEndID": None,
    })
    monkeypatch.setattr(
        routines,
        "getCustomRoleEntry",
        lambda guild_id, user_id: {
            "role_id": 20,
            "owner_discord_user_id": member.id,
        },
    )
    monkeypatch.setattr(
        routines,
        "getAllCustomRoles",
        Mock(side_effect=AssertionError("targeted reconcile must not full-scan custom roles")),
    )
    monkeypatch.setattr(routines, "saveCustomRole", saved)

    result = asyncio.run(routines.reconcileVipMember(guild, member.id))

    assert custom in member.roles
    assert member in custom.members
    assert result["updated"] == 1
    assert guild.created_roles == []




def test_targeted_reconcile_does_not_create_role_for_vip_without_customization(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    member = FakeMember(1, "Nick", [access])
    guild = FakeGuild([access], [member])

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "VIP")
    monkeypatch.setattr(routines, "getVIPConfigurations", lambda guild: {
        "VIPRoles": [access],
        "VIPRoleDivisionStartID": None,
        "VIPRoleDivisionEndID": None,
    })
    monkeypatch.setattr(routines, "getCustomRoleEntry", lambda guild_id, user_id: None)
    monkeypatch.setattr(
        routines,
        "getAllCustomRoles",
        Mock(side_effect=AssertionError("targeted reconcile must not full-scan custom roles")),
    )
    monkeypatch.setattr(routines, "saveCustomRole", Mock(return_value=True))

    result = asyncio.run(routines.reconcileVipMember(guild, member.id))

    assert result["processed"] == 1
    assert result["recovered"] == 0
    assert result["deleted"] == 0
    assert guild.created_roles == []


def test_remove_departed_member_custom_role_is_targeted(monkeypatch):
    custom = FakeRole(20, "VIP Nick", 10, color=0x654321)
    guild = FakeGuild([custom], [])
    cleared = Mock(return_value=True)

    monkeypatch.setattr(routines, "getCustomRoleEntry", lambda guild_id, user_id: {"role_id": 20})
    monkeypatch.setattr(routines, "clearCustomRoleRoleId", cleared)

    removed = asyncio.run(
        routines.removeVipCustomRoleForMember(guild, 1, reason="Membro saiu do servidor")
    )

    assert removed is True
    assert custom.deleted is True
    cleared.assert_called_once_with(guild.id, 1)


def test_remove_departed_alt_does_not_delete_linked_accounts_vip_role(monkeypatch):
    custom = FakeRole(20, "VIP Nick", 10, color=0x654321)
    guild = FakeGuild([custom], [])
    cleared = Mock(side_effect=AssertionError("linked alt must not clear owner's role"))

    monkeypatch.setattr(
        routines,
        "getCustomRoleEntry",
        lambda guild_id, user_id: {
            "role_id": 20,
            "owner_discord_user_id": 2,
        },
    )
    monkeypatch.setattr(routines, "clearCustomRoleRoleId", cleared)

    removed = asyncio.run(
        routines.removeVipCustomRoleForMember(
            guild,
            1,
            reason="Alt saiu do servidor",
        )
    )

    assert removed is False
    assert custom.deleted is False


def test_custom_role_owner_lookup_is_delegated_to_targeted_async_db_query(monkeypatch):
    calls = []

    async def lookup(guild_id, role_ids):
        calls.append((guild_id, role_ids))
        return {1}

    monkeypatch.setattr(routines, "async_getCustomRoleOwnerDiscordIdsByRoleIds", lookup)

    assert asyncio.run(routines.getVipCustomRoleOwnerIds(123, {20, 99})) == {1}
    assert calls == [(123, {20, 99})]


def test_session_recovery_repairs_only_persisted_owner_who_lost_vip(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    custom = FakeRole(20, "VIP Nick", 10, color=0x654321)
    member = FakeMember(1, "Nick", [custom])
    custom.members = [member]
    guild = FakeGuild([access, custom], [member])
    cleared = []

    async def clear_role_id(guild_id, user_id):
        cleared.append((guild_id, user_id))
        return True

    monkeypatch.setattr(
        routines,
        "getAllCustomRoles",
        Mock(side_effect=AssertionError("session recovery must not full-scan custom roles")),
    )
    monkeypatch.setattr(
        routines,
        "getVIPConfigurations",
        Mock(side_effect=AssertionError("session recovery config must be preloaded asynchronously")),
    )
    monkeypatch.setattr(
        routines,
        "getVipCustomRolePrefix",
        Mock(side_effect=AssertionError("session recovery prefix must be preloaded asynchronously")),
    )
    monkeypatch.setattr(routines, "async_clearCustomRoleRoleId", clear_role_id)
    monkeypatch.setattr(
        routines,
        "saveCustomRole",
        Mock(side_effect=AssertionError("session recovery must not rewrite unchanged snapshots")),
    )

    entry = SimpleNamespace(
        userId=1,
        roleId=20,
        color="#654321",
        color2=None,
        iconId=None,
    )
    async def configs(guild_ids):
        assert guild_ids == {guild.id}
        return {
            guild.id: {
                "roleIds": [10],
                "customRolePrefix": "VIP",
                "startRoleId": None,
                "endRoleId": None,
            }
        }

    monkeypatch.setattr(routines, "async_getVipRecoveryConfigsForGuildIds", configs)

    result = asyncio.run(
        routines.recoverVipCustomRolesAfterSessionReset(guild, [entry])
    )

    assert result["processed"] == 1
    assert result["deleted"] == 1
    assert custom.deleted is True
    assert custom not in member.roles
    assert cleared == [(guild.id, member.id)]


def test_session_recovery_fails_safe_when_vip_grant_roles_are_unavailable(monkeypatch):
    custom = FakeRole(20, "VIP Nick", 10, color=0x654321)
    member = FakeMember(1, "Nick", [custom])
    custom.members = [member]
    guild = FakeGuild([custom], [member])
    cleared = Mock(side_effect=AssertionError("fail-safe recovery must not clear links"))

    monkeypatch.setattr(routines, "async_clearCustomRoleRoleId", cleared)

    entry = SimpleNamespace(
        userId=1,
        roleId=20,
        color="#654321",
        color2=None,
        iconId=None,
    )
    async def configs(guild_ids):
        assert guild_ids == {guild.id}
        return {
            guild.id: {
                "roleIds": [999],
                "customRolePrefix": "VIP",
                "startRoleId": None,
                "endRoleId": None,
            }
        }

    monkeypatch.setattr(routines, "async_getVipRecoveryConfigsForGuildIds", configs)

    result = asyncio.run(
        routines.recoverVipCustomRolesAfterSessionReset(guild, [entry])
    )

    assert result["processed"] == 0
    assert result["warnings"] == ["vip_roles_unavailable"]
    assert custom.deleted is False
    assert custom in member.roles




def _vip_recovery_config(guild):
    async def load(guild_ids):
        assert guild_ids == {guild.id}
        return {
            guild.id: {
                "roleIds": [10],
                "customRolePrefix": "VIP",
                "startRoleId": None,
                "endRoleId": None,
            }
        }
    return load


def test_session_recovery_claims_owner_across_linked_alts(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    custom = FakeRole(20, "VIP Nick", 10, color=0x654321)
    owner = FakeMember(1, "Nick", [access, custom])
    alt = FakeMember(2, "Alt", [])
    custom.members = [owner]
    guild = FakeGuild([access, custom], [owner, alt])
    claims = []

    async def claim(row_id, discord_user_id):
        claims.append((row_id, discord_user_id))
        return True

    monkeypatch.setattr(
        routines,
        "async_getVipRecoveryConfigsForGuildIds",
        _vip_recovery_config(guild),
    )
    monkeypatch.setattr(routines, "async_claimCustomRoleOwner", claim)

    entry = SimpleNamespace(
        userId=None,
        ownerDiscordUserId=None,
        linkedDiscordUserIds=[owner.id, alt.id],
        rowId=77,
        roleId=custom.id,
    )

    result = asyncio.run(routines.recoverVipCustomRolesAfterSessionReset(guild, [entry]))

    assert claims == [(77, owner.id)]
    assert entry.ownerDiscordUserId == owner.id
    assert result["processed"] == 1
    assert result["deleted"] == 0
    assert custom.deleted is False


def test_session_recovery_skips_ambiguous_linked_alt_owner(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    custom = FakeRole(20, "VIP Shared", 10, color=0x654321)
    first = FakeMember(1, "Nick", [access, custom])
    second = FakeMember(2, "Alt", [access, custom])
    custom.members = [first, second]
    guild = FakeGuild([access, custom], [first, second])

    monkeypatch.setattr(
        routines,
        "async_getVipRecoveryConfigsForGuildIds",
        _vip_recovery_config(guild),
    )
    monkeypatch.setattr(
        routines,
        "async_claimCustomRoleOwner",
        Mock(side_effect=AssertionError("ambiguous owner must not be claimed")),
    )

    entry = SimpleNamespace(
        userId=None,
        ownerDiscordUserId=None,
        linkedDiscordUserIds=[first.id, second.id],
        rowId=77,
        roleId=custom.id,
    )

    result = asyncio.run(routines.recoverVipCustomRolesAfterSessionReset(guild, [entry]))

    assert result["processed"] == 0
    assert result["deleted"] == 0
    assert result["warnings"] == [f"legacy_owner_ambiguous:{custom.id}"]
    assert custom.deleted is False


def test_session_recovery_continues_after_delete_forbidden(monkeypatch):
    access = FakeRole(10, "VIP Access", 30)
    blocked = FakeRole(20, "VIP Blocked", 20, color=0x654321)
    healthy = FakeRole(21, "VIP Healthy", 19, color=0x123456)
    first = FakeMember(1, "Nick", [blocked])
    second = FakeMember(2, "Alt", [healthy])
    blocked.members = [first]
    healthy.members = [second]
    guild = FakeGuild([access, blocked, healthy], [first, second])
    cleared = []

    async def fail_delete(*, reason=None):
        raise discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            "cannot manage role",
        )

    async def clear_role_id(guild_id, discord_user_id):
        cleared.append((guild_id, discord_user_id))
        return True

    blocked.delete = fail_delete
    monkeypatch.setattr(
        routines,
        "async_getVipRecoveryConfigsForGuildIds",
        _vip_recovery_config(guild),
    )
    monkeypatch.setattr(routines, "async_clearCustomRoleRoleId", clear_role_id)

    entries = [
        SimpleNamespace(userId=1, ownerDiscordUserId=1, rowId=71, roleId=blocked.id),
        SimpleNamespace(userId=2, ownerDiscordUserId=2, rowId=72, roleId=healthy.id),
    ]

    result = asyncio.run(routines.recoverVipCustomRolesAfterSessionReset(guild, entries))

    assert result["processed"] == 2
    assert result["deleted"] == 1
    assert f"role_delete_failed:{first.id}" in result["warnings"]
    assert blocked.deleted is False
    assert healthy.deleted is True
    assert (guild.id, second.id) in cleared

def test_add_vip_role_rejects_linked_alt_when_legacy_role_proves_another_owner(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    custom = FakeRole(20, "VIP Nick", 10, color=0x654321)
    owner = FakeMember(1, "Nick", [access, custom])
    alt = FakeMember(2, "Alt", [access])
    custom.members = [owner]
    guild = FakeGuild([access, custom], [owner, alt])
    claims = []

    async def claim(row_id, discord_user_id):
        claims.append((row_id, discord_user_id))
        return True

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "VIP")
    monkeypatch.setattr(
        routines,
        "getCustomRoleEntry",
        lambda guild_id, user_id: {
            "id": 77,
            "role_id": custom.id,
            "owner_discord_user_id": None,
            "linked_discord_user_ids": [owner.id, alt.id],
        },
    )
    monkeypatch.setattr(routines, "async_claimCustomRoleOwner", claim)

    ctx = SimpleNamespace(guild=guild, user=alt)

    try:
        asyncio.run(routines.addVipRole(ctx))
        assert False, "linked alt must not take over a proven legacy owner role"
    except RuntimeError as error:
        assert str(error) == f"vip_custom_role_owned_by_linked_account:{owner.id}"

    assert claims == [(77, owner.id)]
    assert custom in owner.roles
    assert custom not in alt.roles


def test_targeted_reconcile_skips_ambiguous_legacy_owner(monkeypatch):
    access = FakeRole(10, "VIP Access", 30)
    custom = FakeRole(20, "VIP Shared", 20, color=0x654321)
    first = FakeMember(1, "Nick", [access, custom])
    second = FakeMember(2, "Alt", [access, custom])
    custom.members = [first, second]
    guild = FakeGuild([access, custom], [first, second])

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "VIP")
    monkeypatch.setattr(
        routines,
        "getVIPConfigurations",
        lambda guild: {
            "VIPRoles": [access],
            "VIPRoleDivisionStartID": None,
            "VIPRoleDivisionEndID": None,
        },
    )
    monkeypatch.setattr(
        routines,
        "getCustomRoleEntry",
        lambda guild_id, user_id: {
            "id": 77,
            "role_id": custom.id,
            "owner_discord_user_id": None,
            "linked_discord_user_ids": [first.id, second.id],
        },
    )
    monkeypatch.setattr(
        routines,
        "async_claimCustomRoleOwner",
        Mock(side_effect=AssertionError("ambiguous owner must not be claimed")),
    )

    result = asyncio.run(routines.reconcileVipMember(guild, first.id))

    assert result["warnings"] == [f"legacy_owner_ambiguous:{first.id}"]
    assert custom in first.roles
    assert custom in second.roles
    assert custom.deleted is False



def test_reconcile_uses_role_id_renames_existing_role_and_preserves_visuals(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    custom = FakeRole(20, "VIP Nick", 10, color=0x654321)
    member = FakeMember(1, "Nick", [access, custom])
    custom.members = [member]
    guild = FakeGuild([access, custom], [member])
    saved = Mock(return_value=True)

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "Apoiador")
    monkeypatch.setattr(routines, "getVIPConfigurations", lambda guild: {
        "VIPRoles": [access],
        "VIPRoleDivisionStartID": None,
        "VIPRoleDivisionEndID": None,
    })
    monkeypatch.setattr(routines, "getAllCustomRoles", lambda guild_id: [
        SimpleNamespace(userId=1, roleId=20, color="#654321", color2="#abcdef", iconId=123)
    ])
    monkeypatch.setattr(routines, "getCustomRoleEntry", lambda guild_id, user_id: {"role_id": 20})
    monkeypatch.setattr(routines, "saveCustomRole", saved)

    result = asyncio.run(routines.reconcileVipCustomRoles(guild))

    assert custom.name == "Apoiador Nick"
    assert not custom.deleted
    assert guild.created_roles == []
    assert result["updated"] == 1
    assert any(call.kwargs.get("roleId") == custom.id for call in saved.call_args_list)



def test_full_reconcile_does_not_rediscover_ambiguous_persisted_role(monkeypatch):
    access = FakeRole(10, "VIP Access", 30)
    custom = FakeRole(20, "VIP Stranger", 10, color=0x654321)
    stranger = FakeMember(3, "Stranger", [access, custom])
    custom.members = [stranger]
    guild = FakeGuild([access, custom], [stranger])
    save = Mock(side_effect=AssertionError("persisted ambiguous role must not be rediscovered"))

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "VIP")
    monkeypatch.setattr(
        routines,
        "getVIPConfigurations",
        lambda guild: {
            "VIPRoles": [access],
            "VIPRoleDivisionStartID": None,
            "VIPRoleDivisionEndID": None,
        },
    )
    monkeypatch.setattr(
        routines,
        "getAllCustomRoles",
        lambda guild_id: [
            SimpleNamespace(
                userId=None,
                ownerDiscordUserId=None,
                linkedDiscordUserIds=[1, 2],
                rowId=77,
                roleId=custom.id,
            )
        ],
    )
    monkeypatch.setattr(routines, "getCustomRoleEntry", lambda guild_id, user_id: None)
    monkeypatch.setattr(routines, "saveCustomRole", save)

    result = asyncio.run(routines.reconcileVipCustomRoles(guild))

    assert result["warnings"] == [f"legacy_owner_ambiguous:{custom.id}"]
    assert custom.deleted is False
    save.assert_not_called()

def test_reconcile_does_not_create_missing_custom_role(monkeypatch):
    access = FakeRole(10, "VIP Access", 20)
    member = FakeMember(1, "Nick", [access])
    guild = FakeGuild([access], [member])
    cleared = Mock(return_value=True)

    monkeypatch.setattr(routines, "getVipCustomRolePrefix", lambda guild: "Apoiador")
    monkeypatch.setattr(routines, "getVIPConfigurations", lambda guild: {
        "VIPRoles": [access],
        "VIPRoleDivisionStartID": None,
        "VIPRoleDivisionEndID": None,
    })
    monkeypatch.setattr(routines, "getAllCustomRoles", lambda guild_id: [
        SimpleNamespace(userId=1, roleId=999, color="#654321", color2=None, iconId=None)
    ])
    monkeypatch.setattr(routines, "getCustomRoleEntry", lambda guild_id, user_id: {"role_id": None})
    monkeypatch.setattr(routines, "clearCustomRoleRoleId", cleared)
    monkeypatch.setattr(routines, "saveCustomRole", Mock(return_value=True))

    result = asyncio.run(routines.reconcileVipCustomRoles(guild))

    assert guild.created_roles == []
    assert result["warnings"] == ["role_missing:1"]
    cleared.assert_called_once_with(guild.id, member.id)
