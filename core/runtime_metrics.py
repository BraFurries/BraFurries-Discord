from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psutil


_MEBIBYTE = 1024 * 1024
_UNLIMITED_CGROUP_V1_LIMIT = 1 << 60


@dataclass(frozen=True, slots=True)
class RuntimeMetrics:
    memory_used_mb: float | None
    memory_limit_mb: float | None
    cpu_usage_percent: float | None


class RuntimeMetricsProvider:
    """Collect non-blocking metrics for this Python process and its cgroup."""

    def __init__(self, process: psutil.Process | None = None) -> None:
        self._process = process or psutil.Process(os.getpid())
        self._lock = threading.Lock()
        self._previous_cpu_seconds: float | None = None
        self._previous_wall_seconds: float | None = None
        self._memory_limit_mb: float | None | object = _NotRead
        self._seed_cpu_sample()

    def _seed_cpu_sample(self) -> None:
        try:
            cpu_times = self._process.cpu_times()
            self._previous_cpu_seconds = cpu_times.user + cpu_times.system
            self._previous_wall_seconds = time.monotonic()
        except (psutil.Error, OSError):
            pass

    def collect(self) -> RuntimeMetrics:
        try:
            memory_used_mb = self._read_memory_used_mb()
        except Exception:
            memory_used_mb = None
        try:
            memory_limit_mb = self._read_memory_limit_mb()
        except Exception:
            memory_limit_mb = None
        try:
            cpu_usage_percent = self._read_cpu_usage_percent()
        except Exception:
            cpu_usage_percent = None
        return RuntimeMetrics(
            memory_used_mb=memory_used_mb,
            memory_limit_mb=memory_limit_mb,
            cpu_usage_percent=cpu_usage_percent,
        )

    def _read_memory_used_mb(self) -> float | None:
        try:
            return self._process.memory_info().rss / _MEBIBYTE
        except (psutil.Error, OSError):
            return None

    def _read_memory_limit_mb(self) -> float | None:
        with self._lock:
            if self._memory_limit_mb is _NotRead:
                self._memory_limit_mb = self._discover_memory_limit_mb()
            return self._memory_limit_mb  # type: ignore[return-value]

    def _read_cpu_usage_percent(self) -> float | None:
        try:
            cpu_times = self._process.cpu_times()
            cpu_seconds = cpu_times.user + cpu_times.system
            wall_seconds = time.monotonic()
        except (psutil.Error, OSError):
            return None

        with self._lock:
            previous_cpu = self._previous_cpu_seconds
            previous_wall = self._previous_wall_seconds
            self._previous_cpu_seconds = cpu_seconds
            self._previous_wall_seconds = wall_seconds

        if previous_cpu is None or previous_wall is None:
            return None

        elapsed = wall_seconds - previous_wall
        if elapsed <= 0:
            return None
        return max(0.0, (cpu_seconds - previous_cpu) / elapsed * 100)

    def _discover_memory_limit_mb(self) -> float | None:
        try:
            cgroup_lines = Path('/proc/self/cgroup').read_text(encoding='utf-8').splitlines()
        except OSError:
            return None

        for line in cgroup_lines:
            parts = line.split(':', 2)
            if len(parts) == 3 and parts[1] == '':
                return _read_limit_file(_cgroup_path(Path('/sys/fs/cgroup'), parts[2]) / 'memory.max')

        for line in cgroup_lines:
            parts = line.split(':', 2)
            if len(parts) == 3 and 'memory' in parts[1].split(','):
                return _read_limit_file(
                    _cgroup_path(Path('/sys/fs/cgroup/memory'), parts[2]) / 'memory.limit_in_bytes'
                )
        return None


class _NotReadType:
    pass


_NotRead = _NotReadType()


def _cgroup_path(root: Path, relative_path: str) -> Path:
    return root.joinpath(*relative_path.lstrip('/').split('/'))


def _read_limit_file(path: Path) -> float | None:
    try:
        raw_value = path.read_text(encoding='utf-8').strip()
    except OSError:
        return None

    if raw_value == 'max':
        return None
    try:
        limit_bytes = int(raw_value)
    except ValueError:
        return None
    if limit_bytes <= 0 or limit_bytes >= _UNLIMITED_CGROUP_V1_LIMIT:
        return None
    return limit_bytes / _MEBIBYTE
