import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from core.recent_logs import RecentLogBufferHandler


def test_snapshot_captures_items_and_boundaries_under_one_lock():
    handler = RecentLogBufferHandler(capacity=2)
    record = logging.LogRecord('snapshot', logging.INFO, __file__, 0, 'log', (), None)
    handler.handle(record)
    handler.handle(record)
    attempted = Event()

    def emit_next():
        attempted.set()
        handler.handle(record)

    with ThreadPoolExecutor(max_workers=1) as executor:
        handler.acquire()
        try:
            pending = executor.submit(emit_next)
            assert attempted.wait(timeout=5)
            snapshot = handler.snapshot()
            assert not pending.done()
        finally:
            handler.release()
        pending.result(timeout=5)

    assert [item['sequence'] for item in snapshot.items] == [1, 2]
    assert snapshot.total_buffered == 2
    assert snapshot.oldest_sequence == 1
    assert snapshot.latest_sequence == 2
    current = handler.snapshot()
    assert [item['sequence'] for item in current.items] == [2, 3]
    assert current.oldest_sequence == 2
    assert current.latest_sequence == 3
    snapshot.items[-1]['message'] = 'changed copy'
    assert handler.snapshot().items[0]['message'] == 'log'


def test_ring_buffer_discards_oldest_entries():
    handler = RecentLogBufferHandler(capacity=2)
    logger = logging.getLogger('tests.recent-logs.ring-buffer')
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        logger.info('one')
        logger.warning('two')
        logger.error('three')
    finally:
        logger.removeHandler(handler)

    assert handler.total_buffered == 2
    assert [item['message'] for item in handler.items()] == ['two', 'three']


def test_large_ring_buffer_keeps_only_the_latest_ten_thousand_entries():
    handler = RecentLogBufferHandler(capacity=10000)
    record = logging.LogRecord('capacity', logging.INFO, __file__, 0, 'log', (), None)
    for _ in range(10005):
        handler.handle(record)

    snapshot = handler.snapshot()
    assert snapshot.total_buffered == 10000
    assert snapshot.oldest_sequence == 6
    assert snapshot.latest_sequence == 10005


def test_ring_buffer_supports_minimum_and_exact_level_filters():
    handler = RecentLogBufferHandler()
    logger = logging.getLogger('tests.recent-logs.filters')
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        logger.info('info')
        logger.warning('warning')
        logger.error('error')
    finally:
        logger.removeHandler(handler)

    assert [item['level'] for item in handler.items(minimum_level=logging.WARNING)] == ['WARNING', 'ERROR']
    assert [item['level'] for item in handler.items(exact_level=logging.WARNING)] == ['WARNING']


def test_sequence_is_monotonic_and_after_keeps_filtered_cursor_progress():
    handler = RecentLogBufferHandler()
    logger = logging.getLogger('tests.recent-logs.cursor')
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        logger.info('one')
        logger.debug('two')
        logger.warning('three')
    finally:
        logger.removeHandler(handler)

    assert [item['sequence'] for item in handler.items()] == [1, 2, 3]
    assert [item['message'] for item in handler.items(after=1, minimum_level=logging.WARNING)] == ['three']
    assert handler.items(after=3) == []


def test_before_returns_only_older_filtered_records_in_chronological_order():
    handler = RecentLogBufferHandler()
    for severity in (logging.INFO, logging.DEBUG, logging.WARNING, logging.INFO, logging.ERROR):
        handler.handle(logging.LogRecord('history', severity, __file__, 0, 'log', (), None))

    assert [item['sequence'] for item in handler.items(before=5, minimum_level=logging.INFO)] == [1, 3, 4]


def test_new_handler_starts_sequence_again_at_one():
    handler = RecentLogBufferHandler()
    logger = logging.getLogger('tests.recent-logs.restart')
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        logger.info('fresh process')
    finally:
        logger.removeHandler(handler)
    assert handler.latest_sequence == 1


def test_sequence_exposes_overflow_boundary():
    handler = RecentLogBufferHandler(capacity=2)
    logger = logging.getLogger('tests.recent-logs.overflow-sequence')
    logger.addHandler(handler)
    try:
        for message in ('one', 'two', 'three'):
            logger.warning(message)
    finally:
        logger.removeHandler(handler)
    assert handler.oldest_sequence == 2
    assert handler.latest_sequence == 3
