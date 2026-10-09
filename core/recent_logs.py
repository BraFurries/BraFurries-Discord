from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class LogBufferSnapshot:
    items: list[dict[str, Any]]
    total_buffered: int
    oldest_sequence: int | None
    latest_sequence: int

    def filtered_items(
        self,
        *,
        minimum_level: int | None = None,
        exact_level: int | None = None,
        after: int | None = None,
        before: int | None = None,
    ) -> list[dict[str, Any]]:
        items = self.items
        if after is not None:
            items = [item for item in items if item['sequence'] > after]
        if before is not None:
            items = [item for item in items if item['sequence'] < before]
        if exact_level is not None:
            return [item for item in items if logging.getLevelName(item['level']) == exact_level]
        if minimum_level is not None:
            return [item for item in items if logging.getLevelName(item['level']) >= minimum_level]
        return items


class RecentLogBufferHandler(logging.Handler):
    """A bounded, in-memory view of records already sent to normal handlers."""

    def __init__(self, capacity: int = 1000) -> None:
        super().__init__()
        if capacity < 1:
            raise ValueError('capacity must be positive')
        self._items: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._next_sequence = 1
        self._watchers: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            message = '<unable to format log message>'

        self.acquire()
        try:
            item = {
                'sequence': self._next_sequence,
                'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                'level': record.levelname,
                'logger': record.name,
                'message': message,
            }
            self._next_sequence += 1
            self._items.append(item)
            watchers = tuple(self._watchers)
        finally:
            self.release()
        for loop, signal in watchers:
            try:
                loop.call_soon_threadsafe(signal.set)
            except RuntimeError:
                # Event loop closed between snapshotting and notification.
                pass

    async def wait_for_new_logs(self, after: int, timeout: float = 15.0) -> bool:
        """Sleep until a new record appears; register before checking latest."""
        loop = asyncio.get_running_loop()
        signal = asyncio.Event()
        registration = (loop, signal)
        self.acquire()
        try:
            self._watchers.add(registration)
            already_available = self._next_sequence - 1 > after
        finally:
            self.release()
        try:
            if already_available:
                return True
            try:
                await asyncio.wait_for(signal.wait(), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                return False
        finally:
            self.acquire()
            try:
                self._watchers.discard(registration)
            finally:
                self.release()

    def items(
        self,
        *,
        minimum_level: int | None = None,
        exact_level: int | None = None,
        after: int | None = None,
        before: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.snapshot().filtered_items(
            minimum_level=minimum_level, exact_level=exact_level, after=after, before=before,
        )

    def snapshot(self) -> LogBufferSnapshot:
        """Copy records and their boundaries together; consumers work outside the lock."""
        self.acquire()
        try:
            items = [item.copy() for item in self._items]
            return LogBufferSnapshot(
                items=items,
                total_buffered=len(items),
                oldest_sequence=items[0]['sequence'] if items else None,
                latest_sequence=self._next_sequence - 1,
            )
        finally:
            self.release()

    @property
    def total_buffered(self) -> int:
        self.acquire()
        try:
            return len(self._items)
        finally:
            self.release()

    @property
    def oldest_sequence(self) -> int | None:
        self.acquire()
        try:
            return self._items[0]['sequence'] if self._items else None
        finally:
            self.release()

    @property
    def latest_sequence(self) -> int | None:
        self.acquire()
        try:
            return self._items[-1]['sequence'] if self._items else self._next_sequence - 1
        finally:
            self.release()
