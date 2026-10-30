"""Manual temporary role reconciliation; all effects mocked, no Discord or DB."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

with patch("mysql.connector.pooling.MySQLConnectionPool"):
    from core import temporary_role_reconciler as worker


class Role:
    id=50
    managed=False
    def is_default(self):
        return False
    def __ge__(self, other):
        return False


@asynccontextmanager
async def no_lock(*args):
    yield


def make_guild(*, owned=False, present=False):
    role=Role()
    member=SimpleNamespace(
        id=7,roles=[role] if present else [],
        add_roles=AsyncMock(),remove_roles=AsyncMock())
    guild=SimpleNamespace(
        id=1,get_role=lambda role_id: role if role_id==50 else None,
        get_member=lambda user_id:member if user_id==7 else None,
        me=SimpleNamespace(top_role=SimpleNamespace(),
                           guild_permissions=SimpleNamespace(manage_roles=True)))
    return guild,role,member


def row(status="ACTIVE", owned=False):
    return dict(id=9,community_id=4,guild_id=1,discord_user_id=7,
                role_id=50,version=1,status=status,sync_status="PENDING",
                role_added_by_bot=int(owned))


def prepare(monkeypatch,guild):
    claimed=Mock(return_value=2)
    finished=Mock(return_value=True)
    monkeypatch.setattr(worker,"temp_role_lock",no_lock)
    monkeypatch.setattr(worker,"_claim",claimed)
    monkeypatch.setattr(worker,"_finish",finished)
    bot=SimpleNamespace(get_guild=lambda gid:guild if gid==1 else None)
    return bot,claimed,finished


def test_active_grant_is_applied_and_owned(monkeypatch):
    guild,role,member=make_guild()
    bot,claim,finish=prepare(monkeypatch,guild)
    asyncio.run(worker.reconcile_grant(bot,row()))
    claim.assert_called_once()
    member.add_roles.assert_awaited_once()
    member.remove_roles.assert_not_awaited()
    assert finish.call_args.kwargs["added"] is True
    assert finish.call_args.kwargs["error"] is None


def test_cancel_preserves_legacy_or_other_active_grant(monkeypatch):
    guild,role,member=make_guild(present=True)
    bot,_,finish=prepare(monkeypatch,guild)
    sibling=Mock(return_value=True)
    monkeypatch.setattr(worker,"_other_active_grant",sibling)
    asyncio.run(worker.reconcile_grant(bot,row("CANCELLED",owned=True)))
    sibling.assert_called_once()
    member.remove_roles.assert_not_awaited()
    assert finish.call_args.kwargs["error"] is None


def test_cancel_never_removes_a_preexisting_role(monkeypatch):
    guild,role,member=make_guild(present=True)
    bot,_,finish=prepare(monkeypatch,guild)
    sibling=Mock()
    monkeypatch.setattr(worker,"_other_active_grant",sibling)
    asyncio.run(worker.reconcile_grant(bot,row("CANCELLED",owned=False)))
    sibling.assert_not_called()
    member.remove_roles.assert_not_awaited()
    assert finish.call_args.kwargs["added"] is False


def test_expiration_removes_only_owned_role_without_active_sibling(monkeypatch):
    guild,role,member=make_guild(present=True)
    bot,_,finish=prepare(monkeypatch,guild)
    monkeypatch.setattr(worker,"_other_active_grant",lambda _:False)
    asyncio.run(worker.reconcile_grant(bot,row("EXPIRED",owned=True)))
    member.remove_roles.assert_awaited_once()
    assert finish.call_args.kwargs["error"] is None


def test_stale_grant_cannot_touch_discord(monkeypatch):
    guild,role,member=make_guild()
    bot,claim,finish=prepare(monkeypatch,guild)
    claim.return_value=None
    asyncio.run(worker.reconcile_grant(bot,row()))
    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()
    finish.assert_not_called()


def test_missing_manage_roles_reports_failure_without_adding(monkeypatch):
    guild,role,member=make_guild()
    guild.me.guild_permissions.manage_roles=False
    bot,_,finish=prepare(monkeypatch,guild)
    asyncio.run(worker.reconcile_grant(bot,row()))
    member.add_roles.assert_not_awaited()
    assert finish.call_args.kwargs["error"]=="MISSING_MANAGE_ROLES"


def test_queries_are_tenant_scoped_to_community_and_guild(monkeypatch):
    class Cursor:
        def __init__(self): self.calls=[]
        def execute(self,sql,args): self.calls.append((sql,args))
        def fetchone(self): return None
    from contextlib import contextmanager
    cursor=Cursor()
    @contextmanager
    def pool():
        yield cursor
    monkeypatch.setattr(worker,"pooled_connection",pool)
    assert worker.has_active_manual_grant(13,1,7,50) is False
    sql,args=cursor.calls[0]
    assert "cd.id=%s" in sql
    assert "g.guild_id=%s" in sql
    assert args==(13,1,7,50)


def test_owned_expired_grant_transfers_removal_responsibility_to_active_successor(monkeypatch):
    from contextlib import contextmanager
    class Cursor:
        def __init__(self): self.queries=[]
        def execute(self, sql, args=()): self.queries.append((sql,args))
        def fetchone(self): return {"exists": 1}
    cursor=Cursor()
    @contextmanager
    def pool():
        yield cursor
    monkeypatch.setattr(worker,"pooled_connection",pool)
    assert worker._other_active_grant(row("EXPIRED",owned=True))
    sql,args=cursor.queries[-1]
    assert "SET role_added_by_bot=1" in sql
    assert "status='ACTIVE'" in sql
    assert args==(4,1,7,50,9)


def test_cancel_retries_when_discord_member_lookup_temporarily_fails(monkeypatch):
    guild,role,member=make_guild(present=True)
    guild.get_member=lambda _:None
    response=SimpleNamespace(status=503,reason="Service Unavailable")
    guild.fetch_member=AsyncMock(side_effect=worker.discord.HTTPException(
        response,{"message":"Unavailable","code":0}))
    bot,_,finish=prepare(monkeypatch,guild)
    asyncio.run(worker.reconcile_grant(bot,row("CANCELLED",owned=True)))
    assert finish.call_args.kwargs["error"]=="MEMBER_LOOKUP_FAILED"
    member.remove_roles.assert_not_awaited()


def test_claim_uses_distributed_processing_lease_and_strict_tenant_version(monkeypatch):
    from contextlib import contextmanager
    class Cursor:
        rowcount=1
        def __init__(self): self.calls=[]
        def execute(self, sql, params): self.calls.append((sql,params))
    cursor=Cursor()
    @contextmanager
    def pool(): yield cursor
    monkeypatch.setattr(worker,"pooled_connection",pool)
    assert worker._claim(row())==2
    sql,params=cursor.calls[0]
    assert "sync_status='PROCESSING'" in sql
    assert "last_checked_at=UTC_TIMESTAMP(6)" in sql
    assert "expiring_date>UTC_TIMESTAMP(6)" in sql
    assert "last_checked_at<UTC_TIMESTAMP(6)-INTERVAL 5 MINUTE" in sql
    assert params==(9,4,1,1,"ACTIVE","PENDING")


def test_processing_lease_must_own_current_version_to_mark_finished(monkeypatch):
    from contextlib import contextmanager
    class Cursor:
        rowcount=0
        def __init__(self):self.calls=[]
        def execute(self,sql,params):self.calls.append((sql,params))
    cursor=Cursor()
    @contextmanager
    def pool():yield cursor
    monkeypatch.setattr(worker,"pooled_connection",pool)
    assert not worker._finish(row(),2,added=True,error=None)
    sql,params=cursor.calls[0]
    assert "sync_status='PROCESSING'" in sql
    assert "version=%s" in sql
    assert params[-5:]==(9,4,1,2,"ACTIVE")


def test_confirmed_add_remains_owned_after_concurrent_cancel(monkeypatch):
    guild,role,member=make_guild()
    bot,_,finish=prepare(monkeypatch,guild)
    finish.return_value=False
    remember=Mock()
    monkeypatch.setattr(worker,"_remember_added_after_race",remember)
    asyncio.run(worker.reconcile_grant(bot,row()))
    member.add_roles.assert_awaited_once()
    remember.assert_called_once()
    assert remember.call_args.args[0]["id"]==9


def test_preexisting_role_is_never_claimed_after_concurrent_cancel(monkeypatch):
    guild,role,member=make_guild(present=True)
    bot,_,finish=prepare(monkeypatch,guild)
    finish.return_value=False
    remember=Mock()
    monkeypatch.setattr(worker,"_remember_added_after_race",remember)
    asyncio.run(worker.reconcile_grant(bot,row()))
    member.add_roles.assert_not_awaited()
    remember.assert_not_called()
