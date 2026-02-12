"""Unit tests for TimingLogger."""

import asyncio
import threading

import pytest

from timing_logger import TimingLogger


@pytest.fixture
def logger():
    return TimingLogger()


def test_log_event_stores_correctly(logger: TimingLogger):
    logger.start_test()
    logger.log_audio_received(9600)
    events = logger.get_all_events()
    assert len(events) == 1
    assert events[0]["stage"] == "audio_received"
    assert events[0]["audio_bytes_len"] == 9600
    assert events[0]["chunk_index"] == 0
    assert events[0]["source_position_sec"] == pytest.approx(0.0)


def test_source_position_calculation(logger: TimingLogger):
    logger.start_test()
    for _ in range(10):
        logger.log_audio_received(9600)
    events = logger.get_all_events()
    positions = [e["source_position_sec"] for e in events]
    expected = [i * 0.3 for i in range(10)]
    for actual, exp in zip(positions, expected):
        assert actual == pytest.approx(exp)


def test_start_stop_lifecycle(logger: TimingLogger):
    # Before start, events should not be recorded
    logger.log_audio_received(9600)
    assert logger.get_all_events() == []

    # After start, events should be recorded
    logger.start_test()
    logger.log_audio_received(9600)
    assert len(logger.get_all_events()) == 1

    # After stop, events should not be recorded
    logger.stop_test()
    logger.log_audio_received(9600)
    assert len(logger.get_all_events()) == 1


@pytest.mark.asyncio
async def test_subscriber_receives_events(logger: TimingLogger):
    logger.start_test()
    q = logger.subscribe()
    logger.log_audio_received(9600)
    event = q.get_nowait()
    assert event.stage == "audio_received"


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery(logger: TimingLogger):
    logger.start_test()
    q = logger.subscribe()
    logger.unsubscribe(q)
    logger.log_audio_received(9600)
    assert q.empty()


def test_deque_bounded():
    logger = TimingLogger(maxlen=100)
    logger.start_test()
    for _ in range(150):
        logger.log_audio_received(9600)
    events = logger.get_all_events()
    assert len(events) == 100


def test_thread_safety(logger: TimingLogger):
    logger.start_test()
    errors = []

    def log_many(n):
        try:
            for _ in range(500):
                logger.log_audio_received(9600)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=log_many, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    events = logger.get_all_events()
    assert len(events) == 2000  # 4 threads * 500 events


def test_get_all_events_does_not_clear(logger: TimingLogger):
    logger.start_test()
    logger.log_audio_received(9600)
    first = logger.get_all_events()
    second = logger.get_all_events()
    assert len(first) == len(second) == 1
    assert first[0] == second[0]
