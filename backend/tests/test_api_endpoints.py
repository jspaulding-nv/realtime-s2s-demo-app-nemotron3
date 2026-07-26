"""Unit tests for test control and metrics API endpoints."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from config import riva_config, staged_pipeline_config
from main import app, handle_control_message, lifespan
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
    with patch(
        "main.session_manager.clear_staged_telemetry",
        return_value=True,
    ) as clear_telemetry:
        resp = await client.post("/api/test/start")
    assert resp.status_code == 200
    assert resp.json() == {"status": "started"}
    assert timing_logger.is_test_active
    clear_telemetry.assert_called_once_with()


@pytest.mark.asyncio
async def test_start_test_rejects_active_staged_stream(client: AsyncClient):
    with patch(
        "main.session_manager.clear_staged_telemetry",
        return_value=False,
    ):
        response = await client.post("/api/test/start")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Cannot start a new test while a staged stream is active"
    }
    assert not timing_logger.is_test_active


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


@pytest.mark.asyncio
async def test_export_includes_retained_staged_pipeline_telemetry(
    client: AsyncClient,
):
    telemetry = {
        "outcome": "complete",
        "events": [{"stage": "nmt", "event": "completed"}],
        "websocket_sent_sequence_ids": [0],
        "websocket_send_events": [
            {"sequence_id": 0, "sent_monotonic_ms": 123.0, "audio_bytes": 3200}
        ],
    }
    with patch(
        "main.session_manager.get_staged_telemetry",
        return_value=telemetry,
    ):
        response = await client.get("/api/test/export")

    assert response.status_code == 200
    assert response.json()["stagedPipeline"] == telemetry


@pytest.mark.asyncio
async def test_config_and_root_expose_active_pipeline_mode(client: AsyncClient):
    with patch("main.staged_pipeline_config.pipeline_mode", "staged"):
        config_response = await client.get("/api/config")
        root_response = await client.get("/")

    assert config_response.json()["pipelineMode"] == "staged"
    assert root_response.json()["pipeline_mode"] == "staged"
    assert config_response.json()["modelConfig"] == {
        "asr": {
            "endpoint": riva_config.asr_uri,
            "image": riva_config.asr_image,
            "imageDigest": riva_config.asr_image_digest or None,
            "profile": riva_config.asr_profile or None,
            "eouMs": riva_config.endpointing_history_ms,
            "wordTimeOffsets": riva_config.asr_word_time_offsets,
            "sourceLanguage": riva_config.source_language,
        },
        "nmt": {
            "endpoint": riva_config.uri,
            "image": riva_config.nmt_image,
            "imageDigest": riva_config.nmt_image_digest or None,
            "profile": riva_config.nmt_profile or None,
            "model": riva_config.model,
            "sourceLanguage": riva_config.source_language,
            "targetLanguage": riva_config.target_language,
        },
        "tts": {
            "endpoint": riva_config.tts_uri,
            "image": riva_config.tts_image,
            "imageDigest": riva_config.tts_image_digest or None,
            "profile": riva_config.tts_profile or None,
            "targetLanguage": riva_config.target_language,
            "voice": "Magpie-Multilingual.ES-US.Isabela",
        },
    }
    assert config_response.json()["stagedConfig"] == {
        "telemetrySchemaVersion": (
            2
            if staged_pipeline_config.tts_subsegment_max_chars > 0
            else 1
        ),
        "segmentMaxChars": staged_pipeline_config.segment_max_chars,
        "segmentMaxAgeMs": staged_pipeline_config.segment_max_age_ms,
        "asrEventQueueMaxSize": staged_pipeline_config.asr_event_queue_maxsize,
        "nmtQueueMaxSize": staged_pipeline_config.nmt_queue_maxsize,
        "ttsQueueMaxSize": staged_pipeline_config.tts_queue_maxsize,
        "outputQueueMaxSize": staged_pipeline_config.output_queue_maxsize,
        "nmtRpcTimeoutSeconds": staged_pipeline_config.nmt_rpc_timeout_s,
        "ttsRpcTimeoutSeconds": staged_pipeline_config.tts_rpc_timeout_s,
        "ttsMaxSegmentAudioSeconds": (
            staged_pipeline_config.tts_max_segment_audio_s
        ),
        "ttsMaxRetries": staged_pipeline_config.tts_max_retries,
        "ttsResponseChunkTelemetryEnabled": (
            staged_pipeline_config.tts_response_chunk_telemetry_enabled
        ),
        "ttsSubsegmentMaxChars": (
            staged_pipeline_config.tts_subsegment_max_chars
        ),
        "ttsSubsegmentMinChars": (
            staged_pipeline_config.tts_subsegment_min_chars
        ),
        "closeTimeoutSeconds": staged_pipeline_config.close_timeout_s,
    }


@pytest.mark.asyncio
async def test_config_uses_process_start_repository_provenance(
    client: AsyncClient,
):
    process_snapshot = {"commit": "a" * 40, "dirty": False}
    with (
        patch("main.PROCESS_REPOSITORY_PROVENANCE", process_snapshot),
        patch(
            "main.get_repository_provenance",
            return_value={"commit": "b" * 40, "dirty": True},
        ) as rediscover,
    ):
        response = await client.get("/api/config")

    assert response.json()["repositoryProvenance"] == process_snapshot
    rediscover.assert_not_called()


@pytest.mark.asyncio
async def test_staged_lifespan_does_not_open_monolithic_connection():
    with (
        patch("main.staged_pipeline_config.pipeline_mode", "staged"),
        patch("main.riva_client") as monolithic_client,
    ):
        monolithic_client.is_connected.return_value = False
        async with lifespan(app):
            pass

    monolithic_client.connect.assert_not_called()
    monolithic_client.disconnect.assert_not_called()


@pytest.mark.asyncio
async def test_config_reports_schema_v2_only_for_enabled_tts_subsegmentation(
    client: AsyncClient,
):
    with (
        patch("main.staged_pipeline_config.tts_subsegment_max_chars", 40),
        patch("main.staged_pipeline_config.tts_subsegment_min_chars", 12),
    ):
        response = await client.get("/api/config")

    staged = response.json()["stagedConfig"]
    assert staged["telemetrySchemaVersion"] == 2
    assert staged["ttsSubsegmentMaxChars"] == 40
    assert staged["ttsSubsegmentMinChars"] == 12


@pytest.mark.asyncio
async def test_config_advertises_audio_metadata_v1_for_schema_v3_staged(
    client: AsyncClient,
):
    with (
        patch("main.staged_pipeline_config.pipeline_mode", "staged"),
        patch(
            "main.staged_pipeline_config.tts_incremental_publish_enabled",
            True,
        ),
    ):
        response = await client.get("/api/config")

    assert response.status_code == 200
    assert response.json()["audioMetadataProtocolVersions"] == [1]
    assert response.json()["stagedConfig"]["telemetrySchemaVersion"] == 3
    assert response.json()["stagedConfig"]["ttsIncrementalFrameMs"] == 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pipeline_mode", "incremental_publish_enabled"),
    [
        ("staged", False),
        ("monolithic", False),
        ("monolithic", True),
    ],
)
async def test_config_does_not_advertise_audio_metadata_outside_schema_v3_staged(
    client: AsyncClient,
    pipeline_mode,
    incremental_publish_enabled,
):
    with (
        patch(
            "main.staged_pipeline_config.pipeline_mode",
            pipeline_mode,
        ),
        patch(
            "main.staged_pipeline_config.tts_incremental_publish_enabled",
            incremental_publish_enabled,
        ),
    ):
        response = await client.get("/api/config")

    assert response.status_code == 200
    assert response.json()["audioMetadataProtocolVersions"] == []


@pytest.mark.asyncio
async def test_monolithic_lifespan_keeps_eager_connection():
    with (
        patch("main.staged_pipeline_config.pipeline_mode", "monolithic"),
        patch("main.riva_client") as monolithic_client,
    ):
        monolithic_client.connect.return_value = True
        monolithic_client.is_connected.return_value = True
        async with lifespan(app):
            pass

    monolithic_client.connect.assert_called_once_with()
    monolithic_client.disconnect.assert_called_once_with()


@pytest.mark.asyncio
async def test_ping_control_uses_session_serialized_pong_sender():
    session = MagicMock()
    session.send_pong = AsyncMock()

    await handle_control_message(session, {"type": "ping"})

    session.send_pong.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_start_control_forwards_explicit_audio_metadata_version():
    session = MagicMock()
    session.start_stream = AsyncMock()

    await handle_control_message(
        session,
        {
            "type": "start_stream",
            "targetLanguage": "es-US",
            "audioMetadataProtocolVersion": 1,
        },
    )

    session.start_stream.assert_awaited_once_with(
        "es-US",
        audio_metadata_protocol_version=1,
    )


@pytest.mark.asyncio
async def test_start_control_preserves_legacy_call_when_metadata_is_absent():
    session = MagicMock()
    session.start_stream = AsyncMock()

    await handle_control_message(
        session,
        {
            "type": "start_stream",
            "targetLanguage": "es-US",
        },
    )

    session.start_stream.assert_awaited_once_with("es-US")


@pytest.mark.asyncio
async def test_unknown_control_uses_generation_aware_protocol_failure():
    session = MagicMock()
    session.send_protocol_error = AsyncMock()

    await handle_control_message(session, {"type": "unexpected"})

    session.send_protocol_error.assert_awaited_once_with(
        "Unknown message type: unexpected"
    )
