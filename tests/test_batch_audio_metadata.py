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
    assert result.input_pacing["mode"] == "chunk_end_boundary_v1"
    assert result.input_pacing["chunk_duration_ms"] == 1.0
    assert (
        result.input_pacing["source_sample_zero_clock"]
        == "client_monotonic"
    )
    assert result.input_pacing["deadline_basis"] == (
        "source_sample_zero_plus_one_based_chunk_duration"
    )
    assert result.input_pacing["source_sample_zero_timestamp_ms"] == (
        pytest.approx(result.input_sample_zero_timestamp_ms)
    )
    assert result.input_pacing["observed_chunk_count"] == 1
    assert (
        result.input_pacing["min_emission_minus_deadline_ms"]
        == pytest.approx(
            result.input_pacing["max_emission_minus_deadline_ms"]
        )
    )
    assert result.input_pacing["min_emission_minus_deadline_ms"] >= 0
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
    assert result.source_end_to_receipt_samples_ms[0] == pytest.approx(
        pcm_event["timestamp_ms"]
        - result.input_sample_zero_timestamp_ms
        - pcm_event["sourceEndMs"]
    )
    assert result.headless_playback_report["capture"][
        "canonical_replay_verified"
    ] is True
    assert result.headless_playback_report["capture"][
        "frames_scheduled"
    ] == 1
    assert result.headless_playback_report["queue_gate"][
        "all_frames_preserved_once_in_order"
    ] is True
    assert validate_audio_metadata_observation(result) == []
    assert validate_capture_result(result) == []


def test_validated_audio_frame_sink_receives_only_paired_metadata(monkeypatch):
    websocket = FakeWebSocket()
    install_run_fakes(monkeypatch, websocket)
    observed = []

    result = asyncio.run(
        run_test(
            "synthetic.wav",
            "http://backend",
            audio_metadata_protocol_version=1,
            audio_frame_sink=observed.append,
        )
    )

    assert result.translation_completed is True
    assert len(observed) == 1
    frame = observed[0]
    assert frame.audio_bytes == 3200
    assert frame.protocol_version == 1
    assert frame.stream_generation == 1
    assert frame.parent_sequence_id == 0
    assert frame.audio_frame_id == 0
    assert frame.sample_rate_hz == 16000
    assert frame.channels == 1
    assert frame.bytes_per_sample == 2
    assert frame.source_start_ms is None
    assert frame.source_end_ms == 10.0
    assert frame.arrival_seconds >= 0
    assert not hasattr(frame, "pcm")
    assert not hasattr(frame, "text")


def test_validated_audio_frame_sink_requires_protocol_v1(monkeypatch):
    websocket = FakeWebSocket(legacy=True)
    install_run_fakes(monkeypatch, websocket)

    with pytest.raises(
        ValueError,
        match="audio_frame_sink requires audio metadata protocol version 1",
    ):
        asyncio.run(
            run_test(
                "synthetic.wav",
                "http://backend",
                audio_frame_sink=lambda _frame: None,
            )
        )


def test_invalid_binary_never_reaches_validated_audio_frame_sink(monkeypatch):
    websocket = FakeWebSocket(wrong_binary_size=True)
    install_run_fakes(monkeypatch, websocket)
    observed = []

    result = asyncio.run(
        run_test(
            "synthetic.wav",
            "http://backend",
            audio_metadata_protocol_version=1,
            audio_frame_sink=observed.append,
        )
    )

    assert observed == []
    assert result.translation_completed is False
    assert result.server_error.startswith(
        "audio metadata protocol violation:"
    )


def test_validated_audio_frame_sink_failure_aborts_capture_safely(monkeypatch):
    websocket = FakeWebSocket()
    install_run_fakes(monkeypatch, websocket)

    def fail_sink(_frame):
        raise RuntimeError("private details must not be copied")

    result = asyncio.run(
        run_test(
            "synthetic.wav",
            "http://backend",
            audio_metadata_protocol_version=1,
            audio_frame_sink=fail_sink,
        )
    )

    assert result.translation_completed is False
    assert result.server_error == (
        "validated audio frame sink failed: RuntimeError"
    )
    assert "private details" not in result.server_error


def test_capture_validation_requires_reconciled_headless_report(monkeypatch):
    websocket = FakeWebSocket()
    install_run_fakes(monkeypatch, websocket)
    result = asyncio.run(
        run_test(
            "synthetic.wav",
            "http://backend",
            audio_metadata_protocol_version=1,
        )
    )

    valid_report = result.headless_playback_report
    result.headless_playback_report = None
    assert (
        "audio metadata capture requires a headless playback report"
        in validate_capture_result(result)
    )

    result.headless_playback_report = valid_report
    result.headless_playback_report["capture"]["frames_scheduled"] = 2
    assert (
        "headless playback scheduled-frame count does not match "
        "translated responses"
    ) in validate_capture_result(result)


def test_protocol_v1_validation_rejects_missing_or_wrong_input_pacing(
    monkeypatch,
):
    websocket = FakeWebSocket()
    install_run_fakes(monkeypatch, websocket)
    result = asyncio.run(
        run_test(
            "synthetic.wav",
            "http://backend",
            audio_metadata_protocol_version=1,
        )
    )
    valid_pacing = dict(result.input_pacing)

    result.input_pacing = None
    assert (
        "audio metadata capture requires input pacing provenance"
        in validate_audio_metadata_observation(result)
    )

    result.input_pacing = dict(
        valid_pacing,
        mode="chunk_start_boundary",
        source_sample_zero_clock="wall_clock",
    )
    errors = validate_audio_metadata_observation(result)
    assert (
        "audio metadata input pacing mode must be "
        "'chunk_end_boundary_v1'"
    ) in errors
    assert (
        "audio metadata input sample-zero clock must be "
        "'client_monotonic'"
    ) in errors

    result.input_pacing = dict(valid_pacing, chunk_duration_ms=0.5)
    assert (
        "audio metadata input pacing chunk duration is invalid"
        in validate_audio_metadata_observation(result)
    )

    result.input_pacing = dict(
        valid_pacing,
        observed_chunk_count=2,
    )
    assert (
        "audio metadata input pacing observed chunk count does not "
        "match chunks_sent"
    ) in validate_audio_metadata_observation(result)

    result.input_pacing = dict(
        valid_pacing,
        min_emission_minus_deadline_ms=-0.25,
    )
    assert (
        "audio metadata input pacing contains an early chunk emission"
    ) in validate_audio_metadata_observation(result)

    result.input_pacing = dict(
        valid_pacing,
        max_emission_minus_deadline_ms=(
            valid_pacing["max_emission_minus_deadline_ms"] + 1
        ),
    )
    assert (
        "audio metadata input pacing emission margins do not match "
        "the client event ledger"
    ) in validate_audio_metadata_observation(result)

    result.input_pacing = valid_pacing
    chunk_event = next(
        event
        for event in result.client_events
        if event.stage == "chunk_sent"
    )
    chunk_event.timestamp_ms -= 2.0
    errors = validate_audio_metadata_observation(result)
    assert (
        "audio metadata input pacing event ledger contains an early "
        "chunk emission"
    ) in errors
    assert (
        "audio metadata input pacing emission margins do not match "
        "the client event ledger"
    ) in errors


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
    assert result.input_pacing is None
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
