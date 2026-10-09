from __future__ import annotations

from collections import deque
from time import monotonic


class MembershipRecoveryQueue:
    """FIFO recovery queue with per-guild generations.

    A new recovery request increments the guild generation. A successful
    reconciliation may clear only the generation that existed when it began.
    Failed attempts can be requeued at the tail without starving later guilds.
    """

    def __init__(self, *, clock=monotonic) -> None:
        self._generation: dict[int, int] = {}
        self._queue: deque[int] = deque()
        self._queued: set[int] = set()
        self._not_before: dict[int, float] = {}
        self._clock = clock

    def mark_required(self, guild_id: int, *, delay_seconds: float = 0) -> int:
        generation = self._generation.get(guild_id, 0) + 1
        self._generation[guild_id] = generation
        if delay_seconds > 0:
            retry_at = self._clock() + delay_seconds
            self._not_before[guild_id] = max(
                self._not_before.get(guild_id, retry_at),
                retry_at,
            )
        if guild_id not in self._queued:
            self._queue.append(guild_id)
            self._queued.add(guild_id)
        return generation

    def is_required(self, guild_id: int) -> bool:
        return guild_id in self._generation

    def is_deferred(self, guild_id: int) -> bool:
        retry_at = self._not_before.get(guild_id)
        return (
            guild_id in self._generation
            and retry_at is not None
            and retry_at > self._clock()
        )

    def generation(self, guild_id: int) -> int | None:
        return self._generation.get(guild_id)

    def clear_if_unchanged(
        self,
        guild_id: int,
        generation_at_start: int | None,
    ) -> bool:
        current = self._generation.get(guild_id)
        if (
            current is not None
            and generation_at_start is not None
            and current == generation_at_start
        ):
            self._generation.pop(guild_id, None)
            self._not_before.pop(guild_id, None)
            return True
        return False

    def discard(self, guild_id: int) -> None:
        self._generation.pop(guild_id, None)
        self._not_before.pop(guild_id, None)

    def pop_next(self) -> int | None:
        now = self._clock()
        queued_at_start = len(self._queue)
        for _ in range(queued_at_start):
            guild_id = self._queue.popleft()
            self._queued.discard(guild_id)
            if guild_id not in self._generation:
                continue
            if self._not_before.get(guild_id, 0) > now:
                self._queue.append(guild_id)
                self._queued.add(guild_id)
                continue
            self._not_before.pop(guild_id, None)
            return guild_id
        return None
