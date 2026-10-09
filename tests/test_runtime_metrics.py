from types import SimpleNamespace

import core.runtime_metrics as runtime_metrics
from core.runtime_metrics import RuntimeMetricsProvider


class FakeProcess:
    def __init__(self):
        self.rss = 3 * 1024 * 1024
        self.cpu = 1.0

    def memory_info(self):
        return SimpleNamespace(rss=self.rss)

    def cpu_times(self):
        return SimpleNamespace(user=self.cpu, system=0.0)


def test_provider_reads_process_rss_and_non_blocking_cpu(monkeypatch):
    process = FakeProcess()
    monotonic_values = iter((10.0, 12.0, 14.0))
    monkeypatch.setattr(runtime_metrics.time, 'monotonic', lambda: next(monotonic_values))
    provider = RuntimeMetricsProvider(process=process)
    monkeypatch.setattr(provider, '_discover_memory_limit_mb', lambda: 768.0)

    first = provider.collect()
    process.cpu = 1.5
    second = provider.collect()

    assert first.memory_used_mb == 3
    assert first.memory_limit_mb == 768
    assert first.cpu_usage_percent == 0
    assert second.cpu_usage_percent == 25


def test_provider_returns_null_for_individual_unavailable_metric(monkeypatch):
    provider = RuntimeMetricsProvider(process=FakeProcess())
    monkeypatch.setattr(provider, '_discover_memory_limit_mb', lambda: None)
    monkeypatch.setattr(provider._process, 'memory_info', lambda: (_ for _ in ()).throw(OSError()))

    metrics = provider.collect()

    assert metrics.memory_used_mb is None
    assert metrics.memory_limit_mb is None
