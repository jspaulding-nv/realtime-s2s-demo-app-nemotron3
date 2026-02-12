"""Unit tests for test control and metrics API endpoints."""

from unittest.mock import patch

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
async def test_start_test_endpoint(client: AsyncClient):
    resp = await client.post("/api/test/start")
    assert resp.status_code == 200
    assert resp.json() == {"status": "started"}
    assert timing_logger.is_test_active


@pytest.mark.asyncio
async def test_stop_test_endpoint(client: AsyncClient):
    await client.post("/api/test/start")
    resp = await client.post("/api/test/stop")
    assert resp.status_code == 200
    assert resp.json() == {"status": "stopped"}
    assert not timing_logger.is_test_active


@pytest.mark.asyncio
async def test_export_returns_events(client: AsyncClient):
    await client.post("/api/test/start")
    timing_logger.log_audio_received(9600)
    resp = await client.get("/api/test/export")
    assert resp.status_code == 200
    data = resp.json()
    assert "events" in data
    assert len(data["events"]) >= 1


@pytest.mark.asyncio
async def test_export_empty_when_no_test(client: AsyncClient):
    resp = await client.get("/api/test/export")
    assert resp.status_code == 200
    assert resp.json()["events"] == []
