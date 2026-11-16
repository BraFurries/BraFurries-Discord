from types import SimpleNamespace

import pytest

from core.bulk_role_assignment import (
    BulkRolePreviewError,
    build_bulk_role_preview,
)


class FakeRole:
    def __init__(self, role_id, name, position, *, managed=False, default=False):
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = managed
        self.default = default

    def __gt__(self, other):
        return self.position > other.position

    def is_default(self):
        return self.default


def guild_fixture(*, actor_admin=True, bot_can_manage=True, chunked=True,
                  target_managed=False, target_position=5):
    target = FakeRole(55, "Membro", target_position, managed=target_managed)
    actor = SimpleNamespace(
        id=101,
        bot=False,
        roles=[],
        guild_permissions=SimpleNamespace(administrator=actor_admin),
        top_role=FakeRole(100, "Administrador", 15),
    )
    existing = SimpleNamespace(id=102, bot=False, roles=[target])
    eligible = SimpleNamespace(id=103, bot=False, roles=[])
    excluded_bot = SimpleNamespace(id=104, bot=True, roles=[])
    bot = SimpleNamespace(
        top_role=FakeRole(200, "Coddy", 20),
        guild_permissions=SimpleNamespace(manage_roles=bot_can_manage),
    )
    members = [actor, existing, eligible, excluded_bot]
    guild = SimpleNamespace(
        id=777,
        owner_id=999,
        me=bot,
        chunked=chunked,
        members=members,
        get_member=lambda uid: next((x for x in members if x.id == uid), None),
        get_role=lambda rid: target if rid == target.id else None,
    )
    return guild, target


def test_preview_excludes_bots_and_counts_only_missing_human_roles():
    guild, _ = guild_fixture()
    preview = build_bulk_role_preview(guild, 101, 55)
    assert preview["guildId"] == "777"
    assert preview["roleId"] == "55"
    assert preview["roleName"] == "Membro"
    assert preview["humanMembers"] == 3
    assert preview["alreadyAssigned"] == 1
    assert preview["eligible"] == 2
    assert preview["excludedBots"] == 1
    assert len(preview["fingerprint"]) == 64


def test_preview_fingerprint_tracks_live_member_assignment_state():
    guild, role = guild_fixture()
    before = build_bulk_role_preview(guild, 101, 55)
    guild.members[2].roles.append(role)
    after = build_bulk_role_preview(guild, 101, 55)
    assert after["eligible"] == 1
    assert before["fingerprint"] != after["fingerprint"]


def test_preview_fails_closed_for_partial_member_cache():
    guild, _ = guild_fixture(chunked=False)
    with pytest.raises(BulkRolePreviewError, match="member_cache_incomplete"):
        build_bulk_role_preview(guild, 101, 55)


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"actor_admin": False}, "bulk_assignment_requires_administrator"),
        ({"bot_can_manage": False}, "bot_cannot_manage_roles"),
        ({"target_managed": True}, "role_not_assignable"),
        ({"target_position": 21}, "role_above_bot"),
        ({"target_position": 17}, "role_above_actor"),
    ],
)
def test_preview_rejects_unauthorized_or_unassignable_roles(kwargs, error):
    guild, _ = guild_fixture(**kwargs)
    with pytest.raises((PermissionError, BulkRolePreviewError), match=error):
        build_bulk_role_preview(guild, 101, 55)


def test_preview_rejects_missing_guild_role():
    guild, _ = guild_fixture()
    with pytest.raises(BulkRolePreviewError, match="role_not_found"):
        build_bulk_role_preview(guild, 101, 999)
