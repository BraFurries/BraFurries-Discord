import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from functools import wraps
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

with patch("mysql.connector.pooling.MySQLConnectionPool"):
    from core import database, routine_functions, temp_role_locks


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


class TempRoleCursor:
    def __init__(self, grants=None, *, community=True, user=True, fail_persist=False):
        self.grants = list(grants or [])
        self.community = community
        self.user = user
        self.fail_persist = fail_persist
        self.executed = []
        self.result = None

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SELECT id FROM community_discord"):
            self.result = {"id": 11} if self.community else None
        elif normalized.startswith("SELECT id FROM user_discord"):
            self.result = {"id": 22} if self.user else None
        elif "FROM user_temp_roles" in normalized and "FOR UPDATE" in normalized:
            if self.fail_persist:
                raise RuntimeError("persistence failed")
            self.result = sorted(
                self.grants,
                key=lambda grant: (-grant["expiring_date"].timestamp(), grant["id"]),
            )
        elif normalized.startswith("INSERT INTO user_temp_roles"):
            if self.fail_persist:
                raise RuntimeError("persistence failed")
            self.grants.append(
                {
                    "id": max((grant["id"] for grant in self.grants), default=0) + 1,
                    "expiring_date": params[3],
                    "reason": params[4],
                }
            )
        elif normalized.startswith("UPDATE user_temp_roles"):
            grant = next(grant for grant in self.grants if grant["id"] == params[2])
            grant.update(expiring_date=params[0], reason=params[1])
        elif normalized.startswith("DELETE FROM user_temp_roles"):
            self.grants = [grant for grant in self.grants if grant["id"] != params[0]]

    def fetchone(self):
        return self.result

    def fetchall(self):
        return list(self.result or [])


def connection_for(cursor):
    @contextmanager
    def connection():
        yield cursor

    return connection


def member_for(role, *, has_role=False):
    guild = SimpleNamespace(get_role=lambda role_id: role if role_id == role.id else None)
    return SimpleNamespace(
        id=7,
        guild=guild,
        roles=[role] if has_role else [],
        add_roles=AsyncMock(),
        remove_roles=AsyncMock(),
    )


@async_test
async def test_assign_temp_role_adds_and_persists_new_grant(monkeypatch):
    role = SimpleNamespace(id=50)
    member = member_for(role)
    cursor = TempRoleCursor()
    expires = datetime(2030, 1, 1)
    monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))
    monkeypatch.setattr(database, "includeUser", lambda _member, _guild_id: 33)

    assert await database.assignTempRole(1, member, role.id, expires, "new")
    member.add_roles.assert_awaited_once_with(role)
    member.remove_roles.assert_not_awaited()
    assert cursor.grants == [{"id": 1, "expiring_date": expires, "reason": "new"}]


@async_test
async def test_assign_temp_role_extends_existing_grant_without_readding_role(monkeypatch):
    role = SimpleNamespace(id=50)
    member = member_for(role, has_role=True)
    old_expiry = datetime(2030, 1, 1)
    new_expiry = datetime(2030, 1, 2)
    cursor = TempRoleCursor([{"id": 4, "expiring_date": old_expiry, "reason": "old"}])
    monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))
    monkeypatch.setattr(database, "includeUser", lambda _member, _guild_id: 33)

    assert await database.assignTempRole(1, member, role.id, new_expiry, "new")
    member.add_roles.assert_not_awaited()
    assert cursor.grants == [{"id": 4, "expiring_date": new_expiry, "reason": "new"}]


@async_test
async def test_assign_temp_role_never_shortens_existing_grant_or_rewrites_reason(monkeypatch):
    role = SimpleNamespace(id=50)
    member = member_for(role, has_role=True)
    later = datetime(2030, 1, 2)
    cursor = TempRoleCursor([{"id": 4, "expiring_date": later, "reason": "keep"}])
    monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))
    monkeypatch.setattr(database, "includeUser", lambda _member, _guild_id: 33)

    assert await database.assignTempRole(1, member, role.id, datetime(2030, 1, 1), "ignore")
    assert cursor.grants == [{"id": 4, "expiring_date": later, "reason": "keep"}]
    assert not any(query.startswith("UPDATE user_temp_roles") for query, _ in cursor.executed)


@async_test
async def test_assign_temp_role_coalesces_duplicates_into_latest_grant(monkeypatch):
    role = SimpleNamespace(id=50)
    member = member_for(role, has_role=True)
    grants = [
        {"id": 3, "expiring_date": datetime(2030, 1, 1), "reason": "older"},
        {"id": 4, "expiring_date": datetime(2030, 1, 3), "reason": "latest"},
        {"id": 5, "expiring_date": datetime(2030, 1, 2), "reason": "middle"},
    ]
    cursor = TempRoleCursor(grants)
    monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))
    monkeypatch.setattr(database, "includeUser", lambda _member, _guild_id: 33)

    assert await database.assignTempRole(1, member, role.id, datetime(2030, 1, 4), "newest")
    assert cursor.grants == [{"id": 4, "expiring_date": datetime(2030, 1, 4), "reason": "newest"}]


@async_test
async def test_assign_temp_role_does_not_touch_discord_when_identity_is_missing(monkeypatch):
    role = SimpleNamespace(id=50)
    member = member_for(role)
    cursor = TempRoleCursor(community=False)
    monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))
    monkeypatch.setattr(database, "includeUser", Mock())

    assert not await database.assignTempRole(1, member, role.id, datetime(2030, 1, 1), "new")
    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()


@async_test
async def test_assign_temp_role_does_not_touch_discord_when_user_mapping_is_missing(monkeypatch):
    role = SimpleNamespace(id=50)
    member = member_for(role)
    cursor = TempRoleCursor(user=False)
    monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))
    monkeypatch.setattr(database, "includeUser", lambda _member, _guild_id: 33)

    assert not await database.assignTempRole(1, member, role.id, datetime(2030, 1, 1), "new")
    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()


@async_test
async def test_assign_temp_role_stops_when_discord_rejects_role(monkeypatch):
    role = SimpleNamespace(id=50)
    member = member_for(role)
    member.add_roles.side_effect = RuntimeError("forbidden")
    cursor = TempRoleCursor()
    monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))
    monkeypatch.setattr(database, "includeUser", lambda _member, _guild_id: 33)

    assert not await database.assignTempRole(1, member, role.id, datetime(2030, 1, 1), "new")
    assert not any("FROM user_temp_roles" in query for query, _ in cursor.executed)


@async_test
async def test_assign_temp_role_compensates_new_role_when_persistence_fails(monkeypatch):
    role = SimpleNamespace(id=50)
    member = member_for(role)
    cursor = TempRoleCursor(fail_persist=True)
    monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))
    monkeypatch.setattr(database, "includeUser", lambda _member, _guild_id: 33)

    assert not await database.assignTempRole(1, member, role.id, datetime(2030, 1, 1), "new")
    member.add_roles.assert_awaited_once_with(role)
    member.remove_roles.assert_awaited_once_with(role)


@async_test
async def test_assign_temp_role_does_not_remove_preexisting_role_on_persistence_failure(monkeypatch):
    role = SimpleNamespace(id=50)
    member = member_for(role, has_role=True)
    cursor = TempRoleCursor(fail_persist=True)
    monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))
    monkeypatch.setattr(database, "includeUser", lambda _member, _guild_id: 33)

    assert not await database.assignTempRole(1, member, role.id, datetime(2030, 1, 1), "new")
    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()


def cleanup_context(*, member, role, active_sibling):
    record = {
        "id": 9,
        "disc_community_id": 11,
        "disc_user_id": 22,
        "role_id": 50,
        "user_id": 7,
    }
    guild = SimpleNamespace(
        get_member=lambda user_id: member if member and user_id == 7 else None,
        get_role=lambda role_id: role if role and role_id == 50 else None,
    )
    bot = SimpleNamespace(
        config=[SimpleNamespace(guildId=1)],
        get_guild=lambda guild_id: guild if guild_id == 1 else None,
    )
    return record, bot, active_sibling


@async_test
async def test_expired_grant_with_active_sibling_keeps_discord_role_and_deletes_row(monkeypatch):
    role = SimpleNamespace(id=50, name="Role")
    member = SimpleNamespace(id=7, display_name="User", roles=[role], remove_roles=AsyncMock())
    record, bot, active_sibling = cleanup_context(member=member, role=role, active_sibling=True)
    deleted = Mock()
    monkeypatch.setattr(routine_functions, "getExpiringTempRoles", lambda _guild_id: [record])
    monkeypatch.setattr(routine_functions, "getTempRoleExpirationState", lambda _id: {"is_expired": True})
    monkeypatch.setattr(routine_functions, "hasActiveTempRoleSibling", lambda *_args: active_sibling)
    monkeypatch.setattr(routine_functions, "deleteTempRole", deleted)

    await routine_functions.removeTempRoles(bot)
    member.remove_roles.assert_not_awaited()
    deleted.assert_called_once_with(9)


@async_test
async def test_expired_final_grant_removes_discord_role_and_deletes_row(monkeypatch):
    role = SimpleNamespace(id=50, name="Role")
    member = SimpleNamespace(id=7, display_name="User", roles=[role], remove_roles=AsyncMock())
    record, bot, active_sibling = cleanup_context(member=member, role=role, active_sibling=False)
    deleted = Mock()
    monkeypatch.setattr(routine_functions, "getExpiringTempRoles", lambda _guild_id: [record])
    monkeypatch.setattr(routine_functions, "getTempRoleExpirationState", lambda _id: {"is_expired": True})
    monkeypatch.setattr(routine_functions, "hasActiveTempRoleSibling", lambda *_args: active_sibling)
    monkeypatch.setattr(routine_functions, "deleteTempRole", deleted)

    await routine_functions.removeTempRoles(bot)
    member.remove_roles.assert_awaited_once_with(role)
    deleted.assert_called_once_with(9)


@async_test
async def test_expired_grant_without_member_still_deletes_row(monkeypatch):
    role = SimpleNamespace(id=50, name="Role")
    record, bot, active_sibling = cleanup_context(member=None, role=role, active_sibling=False)
    deleted = Mock()
    monkeypatch.setattr(routine_functions, "getExpiringTempRoles", lambda _guild_id: [record])
    monkeypatch.setattr(routine_functions, "getTempRoleExpirationState", lambda _id: {"is_expired": True})
    monkeypatch.setattr(routine_functions, "hasActiveTempRoleSibling", lambda *_args: active_sibling)
    monkeypatch.setattr(routine_functions, "deleteTempRole", deleted)

    await routine_functions.removeTempRoles(bot)
    deleted.assert_called_once_with(9)


@async_test
async def test_duplicate_expired_grants_remove_role_once_and_delete_every_row(monkeypatch):
    role = SimpleNamespace(id=50, name="Role")
    member = SimpleNamespace(id=7, display_name="User", roles=[role], remove_roles=AsyncMock())
    record, bot, _active_sibling = cleanup_context(member=member, role=role, active_sibling=False)
    duplicate = {**record, "id": 10}
    deleted = Mock()
    monkeypatch.setattr(routine_functions, "getExpiringTempRoles", lambda _guild_id: [record, duplicate])
    monkeypatch.setattr(routine_functions, "getTempRoleExpirationState", lambda _id: {"is_expired": True})
    monkeypatch.setattr(routine_functions, "hasActiveTempRoleSibling", lambda *_args: False)
    monkeypatch.setattr(routine_functions, "deleteTempRole", deleted)

    await routine_functions.removeTempRoles(bot)
    member.remove_roles.assert_awaited_once_with(role)
    assert deleted.call_args_list == [call(9), call(10)]


def test_has_active_temp_role_sibling_uses_full_identity_and_exclusion(monkeypatch):
    cursor = TempRoleCursor()
    cursor.fetchone = Mock(return_value={"1": 1})
    monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))

    assert database.hasActiveTempRoleSibling(11, 22, 50, 9)
    query, params = cursor.executed[-1]
    assert "expiring_date > NOW()" in query
    assert params == (11, 22, 50, 9)


def test_temp_role_expiration_state_revalidates_the_grant_by_id(monkeypatch):
    expires = datetime(2030, 1, 1)
    cursor = TempRoleCursor()
    cursor.result = {"expiring_date": expires, "is_expired": 0}
    monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))

    assert database.getTempRoleExpirationState(9) == {
        "expiring_date": expires,
        "is_expired": False,
    }
    query, params = cursor.executed[-1]
    assert "WHERE id = %s" in query
    assert params == (9,)


@async_test
async def test_cleanup_revalidates_renewed_snapshot_without_removing_or_deleting(monkeypatch):
    role = SimpleNamespace(id=50, name="Role")
    member = SimpleNamespace(id=7, display_name="User", roles=[role], remove_roles=AsyncMock())
    record, bot, _active_sibling = cleanup_context(member=member, role=role, active_sibling=False)
    deleted = Mock()
    sibling_check = Mock()
    monkeypatch.setattr(routine_functions, "getExpiringTempRoles", lambda _guild_id: [record])
    monkeypatch.setattr(
        routine_functions,
        "getTempRoleExpirationState",
        lambda _id: {"expiring_date": datetime(2030, 1, 1), "is_expired": False},
    )
    monkeypatch.setattr(routine_functions, "hasActiveTempRoleSibling", sibling_check)
    monkeypatch.setattr(routine_functions, "deleteTempRole", deleted)

    await routine_functions.removeTempRoles(bot)
    member.remove_roles.assert_not_awaited()
    sibling_check.assert_not_called()
    deleted.assert_not_called()


@async_test
async def test_assign_and_cleanup_derive_the_same_lifecycle_lock_key(monkeypatch):
    keys = []

    @asynccontextmanager
    async def recording_lock(guild_id, user_id, role_id):
        keys.append((int(guild_id), int(user_id), int(role_id)))
        yield

    role = SimpleNamespace(id=50, name="Role")
    member = member_for(role, has_role=True)
    member.display_name = "User"
    cursor = TempRoleCursor()
    record, bot, _active_sibling = cleanup_context(member=member, role=role, active_sibling=False)
    monkeypatch.setattr(database, "temp_role_lock", recording_lock)
    monkeypatch.setattr(routine_functions, "temp_role_lock", recording_lock)
    monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))
    monkeypatch.setattr(database, "includeUser", lambda _member, _guild_id: 33)
    monkeypatch.setattr(routine_functions, "getExpiringTempRoles", lambda _guild_id: [record])
    monkeypatch.setattr(routine_functions, "getTempRoleExpirationState", lambda _id: None)

    assert await database.assignTempRole(1, member, role.id, datetime(2030, 1, 1), "new")
    await routine_functions.removeTempRoles(bot)
    assert keys == [(1, 7, 50), (1, 7, 50)]


@async_test
async def test_distinct_temp_role_keys_use_distinct_locks_and_are_released():
    async with temp_role_locks.temp_role_lock(1, 7, 50):
        async with temp_role_locks.temp_role_lock(1, 8, 50):
            first = temp_role_locks._temp_role_locks[(1, 7, 50)][0]
            second = temp_role_locks._temp_role_locks[(1, 8, 50)][0]
            assert first is not second
    assert temp_role_locks._temp_role_locks == {}


@async_test
async def test_concurrent_renewal_finishes_before_cleanup_revalidation(monkeypatch):
    expired = datetime(2029, 1, 1)
    renewed = datetime(2030, 1, 1)
    role = SimpleNamespace(id=50, name="Role")
    assign_holds_lock = asyncio.Event()
    allow_assignment = asyncio.Event()
    cleanup_waiting = asyncio.Event()

    async def add_role(added_role):
        assert added_role is role
        assign_holds_lock.set()
        await allow_assignment.wait()
        member.roles.append(role)

    member = SimpleNamespace(
        id=7,
        display_name="User",
        guild=SimpleNamespace(get_role=lambda role_id: role if role_id == 50 else None),
        roles=[],
        add_roles=add_role,
        remove_roles=AsyncMock(),
    )
    cursor = TempRoleCursor([{"id": 9, "expiring_date": expired, "reason": "old"}])
    record, bot, _active_sibling = cleanup_context(member=member, role=role, active_sibling=False)
    deleted = Mock()

    @asynccontextmanager
    async def observed_cleanup_lock(guild_id, user_id, role_id):
        cleanup_waiting.set()
        async with temp_role_locks.temp_role_lock(guild_id, user_id, role_id):
            yield

    monkeypatch.setattr(database, "pooled_connection", connection_for(cursor))
    monkeypatch.setattr(database, "includeUser", lambda _member, _guild_id: 33)
    monkeypatch.setattr(routine_functions, "temp_role_lock", observed_cleanup_lock)
    monkeypatch.setattr(routine_functions, "getExpiringTempRoles", lambda _guild_id: [record])
    monkeypatch.setattr(
        routine_functions,
        "getTempRoleExpirationState",
        lambda _id: {
            "expiring_date": cursor.grants[0]["expiring_date"],
            "is_expired": cursor.grants[0]["expiring_date"] <= expired,
        },
    )
    monkeypatch.setattr(routine_functions, "deleteTempRole", deleted)

    assignment = asyncio.create_task(
        database.assignTempRole(1, member, role.id, renewed, "renewed")
    )
    await assign_holds_lock.wait()
    cleanup = asyncio.create_task(routine_functions.removeTempRoles(bot))
    await cleanup_waiting.wait()
    assert not cleanup.done()

    allow_assignment.set()
    assert await assignment
    await cleanup

    assert cursor.grants == [{"id": 9, "expiring_date": renewed, "reason": "renewed"}]
    member.remove_roles.assert_not_awaited()
    deleted.assert_not_called()
