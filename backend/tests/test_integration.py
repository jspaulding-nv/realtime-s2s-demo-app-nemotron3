"""Integration tests for the timing pipeline."""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from main import app
from timing_logger import timing_logger


@pytest.fixture(autouse=True)
def reset_logger():
    timing_logger.stop_test()
    timing_logger._events.clear()
    timing_logger._chunk_counter = 0
    yield
    timing_logger.stop_test()
    timing_logger._events.clear()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_backend_timing_pipeline(client: AsyncClient):
    """IT-1: Start test, generate events, export and verify."""
    resp = await client.post("/api/test/start")
    assert resp.status_code == 200

    for i in range(10):
        timing_logger.log_audio_received(9600)

    resp = await client.get("/api/test/export")
    data = resp.json()
    events = data["events"]

    received = [e for e in events if e["stage"] == "audio_received"]
    assert len(received) == 10

    for i, event in enumerate(received):
        assert event["source_position_sec"] == pytest.approx(i * 0.3)
        assert event["chunk_index"] == i

    timestamps = [e["timestamp"] for e in events]
    for i in range(1, len(timestamps)):
        assert timestamps[i] >= timestamps[i - 1]

    resp = await client.post("/api/test/stop")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_multiple_metric_subscribers():
    """IT-3: Multiple subscribers receive the same events."""
    timing_logger.start_test()
    q1 = timing_logger.subscribe()
    q2 = timing_logger.subscribe()

    timing_logger.log_audio_received(9600)

    e1 = q1.get_nowait()
    e2 = q2.get_nowait()
    assert e1.stage == e2.stage == "audio_received"

    timing_logger.unsubscribe(q1)
    timing_logger.unsubscribe(q2)


@pytest.mark.asyncio
async def test_audio_to_riva_events(client: AsyncClient):
    """Verify audio_to_riva events are logged with correct chunk indices."""
    await client.post("/api/test/start")

    for i in range(5):
        idx = timing_logger.log_audio_received(9600)
        timing_logger.log_audio_to_riva(idx, 9600)

    resp = await client.get("/api/test/export")
    events = resp.json()["events"]

    to_riva = [e for e in events if e["stage"] == "audio_to_riva"]
    assert len(to_riva) == 5
    for i, event in enumerate(to_riva):
        assert event["chunk_index"] == i


@pytest.mark.asyncio
async def test_from_riva_events():
    """Verify audio_from_riva events are logged correctly."""
    timing_logger.start_test()
    timing_logger.log_audio_from_riva(4800)
    timing_logger.log_audio_sent_to_client(4800)

    events = timing_logger.get_all_events()
    stages = [e["stage"] for e in events]
    assert "audio_from_riva" in stages
    assert "audio_sent_to_client" in stages
