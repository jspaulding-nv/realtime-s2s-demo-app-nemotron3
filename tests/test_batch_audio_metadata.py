import asyncio
import json

import pytest

from batch_latency_test import (
    AUDIO_METADATA_PROTOCOL_VERSION,
    run_test,
    validate_audio_metadata_observation,
    validate_capture_result,
)


def audio_frame(*, audio_bytes=3200):
    return {
        "type": "audio_frame",
        "protocolVersion": 1,
        "streamGeneration": 1,
        "parentSequenceId": 0,
        "audioFrameId": 0,
        "audioBytes": audio_bytes,
        "sampleRateHz": 16000,
        "channels": 1,
        "bytesPerSample": 2,
        "sourceStartMs": None,
        "sourceEndMs": 10.0,
    }


def parent_complete(*, audio_bytes=3200):
    return {
        "type": "audio_parent_complete",
        "protocolVersion": 1,
        "streamGeneration": 1,
        "parentSequenceId": 0,
        "audioFrameCount": 1,
        "audioBytes": audio_bytes,
        "sourceStartMs": None,
        "sourceEndMs": 10.0,
    }


class FakePcm:
    def __len__(self):
        return 4800

    def tobytes(self):
        return b"\0" * 9600


class FakeResponse:
    def raise_for_status(self):
        return None


class FakeWebSocket:
    def __init__(self, *, wrong_binary_size=False, legacy=False):
        self.responses = [
            json.dumps({"type": "status", "status": "connected"}),
            json.dumps({"type": "status", "status": "listening"}),
        ]
        if legacy:
            self.responses.append(b"\0" * 3200)
        else:
            self.responses.extend(
                [
                    json.dumps(audio_frame()),
                    b"\0" * (1600 if wrong_binary_size else 3200),
                    json.dumps(parent_complete()),
                ]
            )
        self.end_input = asyncio.Event()
        self.control_sends = []
        self.terminal_sent = False

    async def recv(self):
        if self.responses:
            return self.responses.pop(0)
        await self.end_input.wait()
        if not self.terminal_sent:
            self.terminal_sent = True
            return json.dumps({"type": "status", "status": "completed"})
        await asyncio.Future()

    async def send(self, payload):
        if isinstance(payload, bytes):
            return
        message = json.loads(payload)
        self.control_sends.append(message)
        if message["type"] == "end_input":
            self.end_input.set()


class FakeConnection:
    def __init__(self, websocket):
        self.websocket = websocket

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, *_args):
        return None


def install_run_fakes(monkeypatch, websocket, *, advertise_metadata=True):
    backend_config = {
        "pipelineMode": "staged",
        "sampleRate": 16000,
        "channels": 1,
        "modelConfig": {"nmt": {"targetLanguage": "es-US"}},
        "stagedConfig": {
            "telemetrySchemaVersion": 3,
            "ttsIncrementalPublishEnabled": True,
        },
    }
    if advertise_metadata:
        backend_config["audioMetadataProtocolVersions"] = [1]
    monkeypatch.setattr(
        "batch_latency_test.decode_audio",
        lambda _path: FakePcm(),
    )
    monkeypatch.setattr(
        "batch_latency_test.fetch_backend_config",
        lambda url: (
            backend_config,
            "staged",
            "api_config",
            f"{url}/api/config",
        ),
    )
    monkeypatch.setattr(
        "batch_latency_test.websockets.connect",
        lambda *_args, **_kwargs: FakeConnection(websocket),
    )
    monkeypatch.setattr("batch_latency_test.CHUNK_DURATION", 0.001)
    monkeypatch.setattr("batch_latency_test.TERMINAL_SETTLE_SECONDS", 0)
    monkeypatch.setattr(
        "batch_latency_test.requests.post",
        lambda *_args, **_kwargs: FakeResponse(),
    )

    async def fake_export(*_args, **_kwargs):
        return {
            "events": [],
            "stagedPipeline": {
                "websocket_send_events": [
                    {
                        "parent_sequence_id": 0,
                        "audio_frame_id": 0,
                        "audio_bytes": 3200,
                    }
                ],
                "websocket_completed_parent_summaries": [
                    {
                        "parent_sequence_id": 0,
                        "audio_frame_count": 1,
                        "audio_bytes": 3200,
                    }
                ],
            },
        }

    monkeypatch.setattr(
        "batch_latency_test.fetch_backend_export",
        fake_export,
    )
    monkeypatch.setattr(
        "batch_latency_test.validate_staged_pipeline_integrity",
        lambda *_args, **_kwargs: [],
    )


def test_run_test_negotiates_and_captures_observation_metadata(monkeypatch):
    websocket = FakeWebSocket()
    install_run_fakes(monkeypatch, websocket)

    result = asyncio.run(
        run_test(
            "synthetic.wav",
            "http://backend",
            audio_metadata_protocol_version=(
                AUDIO_METADATA_PROTOCOL_VERSION
            ),
        )
    )

    assert websocket.control_sends[0] == {
        "type": "start_stream",
        "targetLanguage": "es-US",
        "audioMetadataProtocolVersion": 1,
    }
    assert result.translation_completed is True
    assert result.audio_metadata_stream_generation == 1
    assert result.audio_metadata_paired_frames == 1
    assert result.audio_metadata_completed_parents == 1
    assert result.source_end_to_receipt_availability == (
        "available_audio_processed_end_offset_not_semantic_boundary"
    )
    assert len(result.source_end_to_receipt_samples_ms) == 1
    assert result.source_end_to_receipt_p50_ms == (
        result.source_end_to_receipt_samples_ms[0]
    )
    pcm_event = next(
        event
        for event in result.websocket_receive_events
        if event["frame_type"] == "pcm"
    )
    assert pcm_event["parentSequenceId"] == 0
    assert pcm_event["audioFrameId"] == 0
    assert pcm_event["sourceEndMs"] == 10.0
    assert validate_audio_metadata_observation(result) == []
    assert validate_capture_result(result) == []


def test_run_test_legacy_capture_does_not_record_v1_clock_anchor(monkeypatch):
    websocket = FakeWebSocket(legacy=True)
    install_run_fakes(monkeypatch, websocket)

    result = asyncio.run(
        run_test(
            "synthetic.wav",
            "http://backend",
        )
    )

    assert websocket.control_sends[0] == {
        "type": "start_stream",
        "targetLanguage": "es-US",
    }
    assert result.input_sample_zero_timestamp_ms is None
    assert result.audio_metadata_stream_generation is None
    assert result.audio_metadata_paired_frames == 0
    assert result.audio_metadata_completed_parents == 0
    assert result.source_end_to_receipt_availability == (
        "protocol_not_negotiated"
    )
    assert validate_audio_metadata_observation(result) == []


def test_run_test_fails_closed_on_header_binary_size_mismatch(monkeypatch):
    websocket = FakeWebSocket(wrong_binary_size=True)
    install_run_fakes(monkeypatch, websocket)

    result = asyncio.run(
        run_test(
            "synthetic.wav",
            "http://backend",
            audio_metadata_protocol_version=1,
        )
    )

    assert result.translation_completed is False
    assert result.server_error == (
        "audio metadata protocol violation: binary PCM byte count "
        "does not match audio_frame"
    )
    assert result.audio_metadata_paired_frames == 0


def test_run_test_refuses_unadvertised_protocol(monkeypatch):
    websocket = FakeWebSocket()
    install_run_fakes(
        monkeypatch,
        websocket,
        advertise_metadata=False,
    )

    with pytest.raises(
        RuntimeError,
        match="does not advertise audio metadata protocol version 1",
    ):
        asyncio.run(
            run_test(
                "synthetic.wav",
                "http://backend",
                audio_metadata_protocol_version=1,
            )
        )
