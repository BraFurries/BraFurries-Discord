from __future__ import annotations

import asyncio
import weakref
from contextlib import asynccontextmanager


class _GuildMembershipGate:
    """Allow concurrent member events but make full reconciliation exclusive.

    Waiting reconciliations get writer preference so a busy guild cannot starve
    recovery forever.
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active_events = 0
        self._reconciling = False
        self._waiting_reconciliations = 0

    @asynccontextmanager
    async def event(self):
        async with self._condition:
            while self._reconciling or self._waiting_reconciliations > 0:
                await self._condition.wait()
            self._active_events += 1

        try:
            yield
        finally:
            async with self._condition:
                self._active_events -= 1
                if self._active_events == 0:
                    self._condition.notify_all()

    @asynccontextmanager
    async def reconciliation(self):
        async with self._condition:
            self._waiting_reconciliations += 1
            try:
                while self._reconciling or self._active_events > 0:
                    await self._condition.wait()
                self._reconciling = True
            finally:
                self._waiting_reconciliations -= 1

        try:
            yield
        finally:
            async with self._condition:
                self._reconciling = False
                self._condition.notify_all()


_guild_gates: dict[int, _GuildMembershipGate] = {}
_member_locks: weakref.WeakValueDictionary[
    tuple[int, int], asyncio.Lock
] = weakref.WeakValueDictionary()


def _guild_gate(guild_id: int) -> _GuildMembershipGate:
    gate = _guild_gates.get(guild_id)
    if gate is None:
        gate = _GuildMembershipGate()
        _guild_gates[guild_id] = gate
    return gate


def _member_lock(guild_id: int, member_id: int) -> asyncio.Lock:
    key = (guild_id, member_id)
    lock = _member_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _member_locks[key] = lock
    return lock


@asynccontextmanager
async def membership_event_guard(guild_id: int, member_id: int):
    """Serialize one member's events while allowing other members in parallel."""
    async with _member_lock(guild_id, member_id):
        async with _guild_gate(guild_id).event():
            yield


@asynccontextmanager
async def membership_reconciliation_guard(guild_id: int):
    """Acquire exclusive membership access for a complete guild snapshot."""
    async with _guild_gate(guild_id).reconciliation():
        yield
