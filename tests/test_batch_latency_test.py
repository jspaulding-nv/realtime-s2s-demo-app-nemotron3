import asyncio
import json

import pytest

from batch_latency_test import (
    TestResult as BatchTestResult,
    TimingEvent,
    compute_playback_metrics,
    fetch_backend_config,
    generate_summary,
    resolve_pipeline_mode,
    run_batch,
    run_test,
    validate_staged_pipeline_integrity,
)


def staged_config():
    return {
        "pipelineMode": "staged",
        "stagedConfig": {
            "segmentMaxChars": 240,
            "segmentMaxAgeMs": 2000,
            "asrEventQueueMaxSize": 32,
            "nmtQueueMaxSize": 4,
            "ttsQueueMaxSize": 4,
            "outputQueueMaxSize": 4,
            "nmtRpcTimeoutSeconds": 15,
            "ttsRpcTimeoutSeconds": 60,
            "ttsMaxSegmentAudioSeconds": 60,
            "closeTimeoutSeconds": 10,
        },
    }


def successful_staged_export():
    completed = [0, 1]
    events = []
    for sequence_id in completed:
        events.extend(
            [
                {
                    "stage": "segmenter",
                    "event": "emitted",
                    "sequence_id": sequence_id,
                },
                {
                    "stage": "nmt",
                    "event": "completed",
                    "sequence_id": sequence_id,
                },
                {
                    "stage": "tts",
                    "event": "completed",
                    "sequence_id": sequence_id,
                },
                {
                    "stage": "output",
                    "event": "dequeued",
                    "sequence_id": sequence_id,
                    "queue_depth": 0,
                    "queue_capacity": 4,
                },
            ]
        )
    return {
        "session_id": "session-1",
        "state": "closed",
        "outcome": "complete",
        "segments_emitted": 2,
        "audio_segments_produced": 2,
        "completed_sequence_ids": completed,
        "incomplete_sequence_ids": [],
        "failure": None,
        "cleanup_errors": [],
        "max_queue_depths": {"nmt": 2, "tts": 1, "output": 2},
        "blocked_put_counts": {"nmt": 0, "tts": 0, "output": 0},
        "events": events,
        "websocket_sent_sequence_ids": completed,
        "websocket_send_events": [
            {
                "sequence_id": sequence_id,
                "sent_monotonic_ms": 1000.0 + sequence_id,
                "audio_bytes": 3200,
            }
            for sequence_id in completed
        ],
    }


def successful_websocket_receive_events():
    return [
        {
            "order": 0,
            "timestamp_ms": 1.0,
            "frame_type": "control",
            "message_type": "status",
            "status": "connected",
            "audio_bytes": 0,
        },
        {
            "order": 1,
            "timestamp_ms": 2.0,
            "frame_type": "pcm",
            "audio_bytes": 3200,
        },
        {
            "order": 2,
            "timestamp_ms": 2.5,
            "frame_type": "pcm",
            "audio_bytes": 3200,
        },
        {
            "order": 3,
            "timestamp_ms": 3.0,
            "frame_type": "control",
            "message_type": "status",
            "status": "completed",
            "audio_bytes": 0,
        },
    ]


def test_generate_summary_records_capture_integrity(tmp_path):
    config = staged_config()
    staged_pipeline = successful_staged_export()
    result = BatchTestResult(
        audio_path="test_audio/example.mp3",
        duration_sec=60.0,
        backend_url="http://localhost:8000",
        backend_config_url="http://localhost:8000/api/config",
        backend_config=config,
        pipeline_mode="staged",
        pipeline_mode_source="api_config",
        staged_pipeline=staged_pipeline,
        staged_integrity_errors=[],
        websocket_receive_events=successful_websocket_receive_events(),
        chunks_sent=200,
        audio_responses=12,
        input_completed=True,
        connection_lost=False,
        drain_timed_out=False,
        drain_duration_sec=13.25,
        input_end_timestamp_ms=60_001.5,
        terminal_arrival_timestamp_ms=60_501.5,
        terminal_arrival_lag_sec=0.5,
        translation_completed=True,
        server_error="",
    )
    output = tmp_path / "example_summary.json"

    generate_summary(result, str(output))

    summary = json.loads(output.read_text(encoding="utf-8"))
    assert summary["backend_url"] == "http://localhost:8000"
    assert summary["backend_config_url"].endswith("/api/config")
    assert summary["backend_config"] == config
    assert summary["pipeline_mode"] == "staged"
    assert summary["pipeline_mode_source"] == "api_config"
    assert summary["staged_pipeline"] == staged_pipeline
    assert summary["staged_integrity"] == {
        "applicable": True,
        "passed": True,
        "errors": [],
    }
    assert summary["websocket_receive_events"] == successful_websocket_receive_events()
    assert summary["input_completed"] is True
    assert summary["connection_lost"] is False
    assert summary["drain_timed_out"] is False
    assert summary["drain_duration_sec"] == 13.25
    assert summary["input_end_timestamp_ms"] == 60_001.5
    assert summary["terminal_arrival_timestamp_ms"] == 60_501.5
    assert summary["terminal_arrival_lag_sec"] == 0.5
    assert summary["translation_completed"] is True
    assert summary["server_error"] == ""


def test_generate_summary_keeps_monolithic_runs_backward_compatible(tmp_path):
    result = BatchTestResult(
        audio_path="test_audio/example.mp3",
        duration_sec=60.0,
        backend_config={"sampleRate": 16000},
    )
    output = tmp_path / "monolithic_summary.json"

    generate_summary(result, str(output))

    summary = json.loads(output.read_text(encoding="utf-8"))
    assert summary["pipeline_mode"] == "monolithic"
    assert summary["pipeline_mode_source"] == "legacy_default"
    assert summary["staged_pipeline"] is None
    assert summary["staged_integrity"] == {
        "applicable": False,
        "passed": None,
        "errors": [],
    }


def test_resolve_pipeline_mode_records_api_or_legacy_provenance():
    assert resolve_pipeline_mode({"pipelineMode": "staged"}) == (
        "staged",
        "api_config",
    )
    assert resolve_pipeline_mode({"sampleRate": 16000}) == (
        "monolithic",
        "legacy_default",
    )


def test_fetch_backend_config_captures_server_snapshot_and_source(monkeypatch):
    config = staged_config()

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return config

    calls = []

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return Response()

    monkeypatch.setattr("batch_latency_test.requests.get", fake_get)

    captured, mode, source, url = fetch_backend_config(
        "http://localhost:8000/"
    )

    assert captured is config
    assert mode == "staged"
    assert source == "api_config"
    assert url == "http://localhost:8000/api/config"
    assert calls == [(url, 10)]


@pytest.mark.parametrize("mode", ["unknown", "STAGED", 1, None, True])
def test_resolve_pipeline_mode_rejects_invalid_values(mode):
    with pytest.raises(ValueError, match="pipelineMode"):
        resolve_pipeline_mode({"pipelineMode": mode})


def test_staged_integrity_accepts_complete_ordered_bounded_export():
    errors = validate_staged_pipeline_integrity(
        successful_staged_export(),
        staged_config(),
        successful_websocket_receive_events(),
        2.75,
    )

    assert errors == []


def test_staged_integrity_reports_lifecycle_order_and_queue_failures():
    export = successful_staged_export()
    export.update(
        {
            "outcome": "cleanup_failed",
            "failure": {"stage": "cleanup", "error": "disconnect failed"},
            "cleanup_errors": [
                {"stage": "tts", "error": "disconnect failed"}
            ],
            "completed_sequence_ids": [0, 2],
            "incomplete_sequence_ids": [1],
            "websocket_sent_sequence_ids": [0],
            "max_queue_depths": {"nmt": 5, "tts": 1, "output": 2},
        }
    )
    export["events"][0]["queue_depth"] = 5
    export["events"][0]["queue_capacity"] = 4

    errors = validate_staged_pipeline_integrity(
        export,
        staged_config(),
        successful_websocket_receive_events(),
        2.75,
    )

    assert any("outcome" in error for error in errors)
    assert any("failure must be null" in error for error in errors)
    assert any("cleanup_errors must be empty" in error for error in errors)
    assert any("incomplete sequence IDs remain" in error for error in errors)
    assert any("contiguous and ordered" in error for error in errors)
    assert any("websocket_sent_sequence_ids" in error for error in errors)
    assert any("queue depth 5 exceeds capacity 4" in error for error in errors)
    assert any("max_queue_depths.nmt=5" in error for error in errors)


def test_staged_integrity_requires_export_object_and_configured_queue_bounds():
    assert validate_staged_pipeline_integrity(
        None,
        staged_config(),
        successful_websocket_receive_events(),
        2.75,
    ) == [
        "/api/test/export.stagedPipeline must be a JSON object"
    ]

    export = successful_staged_export()
    config = staged_config()
    del config["stagedConfig"]["outputQueueMaxSize"]

    errors = validate_staged_pipeline_integrity(
        export,
        config,
        successful_websocket_receive_events(),
        2.75,
    )

    assert errors == [
        "/api/config.stagedConfig.outputQueueMaxSize must be a positive integer"
    ]


def test_staged_integrity_requires_exactly_one_completed_terminal():
    events = successful_websocket_receive_events()
    events.append(
        {
            "order": 4,
            "timestamp_ms": 4.0,
            "frame_type": "control",
            "message_type": "status",
            "status": "completed",
            "audio_bytes": 0,
        }
    )

    errors = validate_staged_pipeline_integrity(
        successful_staged_export(),
        staged_config(),
        events,
        2.75,
    )

    assert "exactly one completed WebSocket terminal is required (got 2)" in errors


def test_staged_integrity_rejects_pcm_after_completed_terminal():
    events = successful_websocket_receive_events()
    events.append(
        {
            "order": 4,
            "timestamp_ms": 4.0,
            "frame_type": "pcm",
            "audio_bytes": 3200,
        }
    )

    errors = validate_staged_pipeline_integrity(
        successful_staged_export(),
        staged_config(),
        events,
        2.75,
    )

    assert "PCM was received after the completed WebSocket terminal" in errors


def test_staged_integrity_rejects_send_receive_count_and_byte_mismatch():
    missing_frame = successful_websocket_receive_events()
    del missing_frame[2]
    for order, event in enumerate(missing_frame):
        event["order"] = order

    errors = validate_staged_pipeline_integrity(
        successful_staged_export(),
        staged_config(),
        missing_frame,
        2.75,
    )
    assert any("receive count" in error for error in errors)

    wrong_bytes = successful_websocket_receive_events()
    wrong_bytes[2]["audio_bytes"] = 1600
    errors = validate_staged_pipeline_integrity(
        successful_staged_export(),
        staged_config(),
        wrong_bytes,
        2.75,
    )
    assert any("received bytes" in error for error in errors)


def test_staged_integrity_rejects_completed_before_input_end():
    errors = validate_staged_pipeline_integrity(
        successful_staged_export(),
        staged_config(),
        successful_websocket_receive_events(),
        3.5,
    )
    assert "completed WebSocket terminal arrived before end_input" in errors


def test_playback_tail_uses_end_input_not_final_chunk_start():
    result = BatchTestResult(
        audio_path="test_audio/example.wav",
        duration_sec=0.3,
        input_end_timestamp_ms=300.0,
        client_events=[
            TimingEvent("client", "chunk_sent", 0.0, 0, 0.0, 9600),
            TimingEvent("client", "input_ended", 300.0, 1, 0.3, 0),
            TimingEvent("client", "audio_received", 600.0, 0, 0.1, 3200),
        ],
    )

    compute_playback_metrics(result)

    assert result.first_audio_latency_sec == pytest.approx(0.6)
    assert result.playback_tail_sec == pytest.approx(0.4)


def test_playback_tail_legacy_fallback_adds_exact_chunk_duration():
    result = BatchTestResult(
        audio_path="test_audio/example.wav",
        duration_sec=0.1,
        client_events=[
            TimingEvent("client", "chunk_sent", 0.0, 0, 0.0, 3200),
            TimingEvent("client", "audio_received", 600.0, 0, 0.1, 3200),
        ],
    )

    compute_playback_metrics(result)

    assert result.playback_tail_sec == pytest.approx(0.6)


def test_run_batch_fails_when_requested_file_is_missing(tmp_path):
    ok = asyncio.run(
        run_batch(
            [str(tmp_path / "missing.wav")],
            "http://localhost:8000",
            str(tmp_path / "results"),
        )
    )

    assert ok is False


def test_run_batch_fails_when_run_test_raises(monkeypatch, tmp_path):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")

    async def fail_run_test(_audio_path, _backend_url):
        raise RuntimeError("capture exploded")

    monkeypatch.setattr("batch_latency_test.run_test", fail_run_test)

    ok = asyncio.run(
        run_batch(
            [str(audio)],
            "http://localhost:8000",
            str(tmp_path / "results"),
        )
    )

    assert ok is False


def test_backend_error_stops_streaming_early_and_batch_writes_failed_capture(
    monkeypatch,
    tmp_path,
):
    total_chunks = 20
    error_after_chunks = 3

    class FakePcm:
        def __len__(self):
            return total_chunks * 4800

        def tobytes(self):
            return b"\0" * (total_chunks * 9600)

    class FakeResponse:
        def __init__(self, payload=None):
            self.payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeWebSocket:
        def __init__(self):
            self.recv_count = 0
            self.binary_sends = 0
            self.control_sends = []
            self.error_ready = asyncio.Event()

        async def recv(self):
            self.recv_count += 1
            if self.recv_count == 1:
                return json.dumps({"type": "status", "status": "connected"})
            if self.recv_count == 2:
                return json.dumps({"type": "status", "status": "listening"})
            if self.recv_count == 3:
                await self.error_ready.wait()
                return json.dumps(
                    {"type": "error", "message": "synthetic backend failure"}
                )
            await asyncio.Future()

        async def send(self, payload):
            if isinstance(payload, bytes):
                self.binary_sends += 1
                if self.binary_sends == error_after_chunks:
                    self.error_ready.set()
                return
            self.control_sends.append(json.loads(payload))

    class FakeConnection:
        def __init__(self, websocket):
            self.websocket = websocket
            self.exited = False

        async def __aenter__(self):
            return self.websocket

        async def __aexit__(self, *_args):
            self.exited = True

    fake_ws = FakeWebSocket()
    fake_connection = FakeConnection(fake_ws)
    post_calls = []
    get_calls = []

    monkeypatch.setattr("batch_latency_test.decode_audio", lambda _path: FakePcm())
    monkeypatch.setattr(
        "batch_latency_test.fetch_backend_config",
        lambda url: (
            {"pipelineMode": "monolithic"},
            "monolithic",
            "api_config",
            f"{url}/api/config",
        ),
    )
    monkeypatch.setattr(
        "batch_latency_test.websockets.connect",
        lambda *_args, **_kwargs: fake_connection,
    )
    monkeypatch.setattr("batch_latency_test.CHUNK_DURATION", 0.01)
    monkeypatch.setattr(
        "batch_latency_test.requests.post",
        lambda url, timeout: post_calls.append((url, timeout)) or FakeResponse(),
    )
    monkeypatch.setattr(
        "batch_latency_test.requests.get",
        lambda url, timeout: get_calls.append((url, timeout))
        or FakeResponse({"events": []}),
    )

    result = asyncio.run(run_test("synthetic.wav", "http://backend"))

    assert result.chunks_sent == error_after_chunks
    assert result.chunks_sent < total_chunks
    assert result.input_completed is False
    assert result.server_error == "synthetic backend failure"
    assert result.translation_completed is False
    assert result.drain_timed_out is False
    assert fake_connection.exited is True
    assert [message["type"] for message in fake_ws.control_sends] == [
        "start_stream",
        "stop_stream",
    ]
    assert [event["order"] for event in result.websocket_receive_events] == [
        0,
        1,
        2,
    ]
    terminal_event = result.websocket_receive_events[-1]
    assert terminal_event["frame_type"] == "control"
    assert terminal_event["message_type"] == "error"
    assert terminal_event["message"] == "synthetic backend failure"
    assert not any(
        event.get("status") == "completed"
        for event in result.websocket_receive_events
    )
    assert post_calls == [
        ("http://backend/api/test/start", 10),
        ("http://backend/api/test/stop", 10),
    ]
    assert get_calls == [("http://backend/api/test/export", 30)]

    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")

    async def return_failed_capture(_audio_path, _backend_url):
        return result

    generated = []
    monkeypatch.setattr("batch_latency_test.run_test", return_failed_capture)
    monkeypatch.setattr(
        "batch_latency_test.generate_plot",
        lambda _result, path: generated.append(path),
    )
    monkeypatch.setattr(
        "batch_latency_test.generate_csv",
        lambda _result, path: generated.append(path),
    )
    monkeypatch.setattr(
        "batch_latency_test.generate_summary",
        lambda _result, path: generated.append(path),
    )

    ok = asyncio.run(
        run_batch(
            [str(audio)],
            "http://backend",
            str(tmp_path / "results"),
        )
    )

    assert ok is False
    assert len(generated) == 3


def test_completed_before_end_input_aborts_sender_and_is_not_success(monkeypatch):
    total_chunks = 20

    class FakePcm:
        def __len__(self):
            return total_chunks * 4800

        def tobytes(self):
            return b"\0" * (total_chunks * 9600)

    class FakeResponse:
        def __init__(self, payload=None):
            self.payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeWebSocket:
        def __init__(self):
            self.recv_count = 0
            self.binary_sends = 0
            self.control_sends = []
            self.completed_ready = asyncio.Event()

        async def recv(self):
            self.recv_count += 1
            if self.recv_count == 1:
                return json.dumps({"type": "status", "status": "connected"})
            if self.recv_count == 2:
                return json.dumps({"type": "status", "status": "listening"})
            if self.recv_count == 3:
                await self.completed_ready.wait()
                return json.dumps({"type": "status", "status": "completed"})
            await asyncio.Future()

        async def send(self, payload):
            if isinstance(payload, bytes):
                self.binary_sends += 1
                if self.binary_sends == 3:
                    self.completed_ready.set()
            else:
                self.control_sends.append(json.loads(payload))

    class FakeConnection:
        def __init__(self, websocket):
            self.websocket = websocket

        async def __aenter__(self):
            return self.websocket

        async def __aexit__(self, *_args):
            return None

    fake_ws = FakeWebSocket()
    monkeypatch.setattr("batch_latency_test.decode_audio", lambda _path: FakePcm())
    monkeypatch.setattr(
        "batch_latency_test.fetch_backend_config",
        lambda url: (
            {"pipelineMode": "monolithic"},
            "monolithic",
            "api_config",
            f"{url}/api/config",
        ),
    )
    monkeypatch.setattr(
        "batch_latency_test.websockets.connect",
        lambda *_args, **_kwargs: FakeConnection(fake_ws),
    )
    monkeypatch.setattr("batch_latency_test.CHUNK_DURATION", 0.01)
    monkeypatch.setattr(
        "batch_latency_test.requests.post",
        lambda *_args, **_kwargs: FakeResponse(),
    )
    monkeypatch.setattr(
        "batch_latency_test.requests.get",
        lambda *_args, **_kwargs: FakeResponse({"events": []}),
    )

    result = asyncio.run(run_test("synthetic.wav", "http://backend"))

    assert result.chunks_sent == 3
    assert result.input_completed is False
    assert result.translation_completed is False
    assert result.server_error == "backend completed before client end_input"
    assert result.terminal_arrival_timestamp_ms > 0
    assert [message["type"] for message in fake_ws.control_sends] == [
        "start_stream",
        "stop_stream",
    ]


def test_run_batch_writes_but_rejects_partial_capture(monkeypatch, tmp_path):
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"audio")
    result = BatchTestResult(
        audio_path=str(audio),
        duration_sec=60.0,
        chunks_sent=200,
        audio_responses=1,
        total_received_bytes=3200,
        input_completed=False,
        translation_completed=True,
    )

    async def partial_run_test(_audio_path, _backend_url):
        return result

    generated = []
    monkeypatch.setattr("batch_latency_test.run_test", partial_run_test)
    monkeypatch.setattr(
        "batch_latency_test.generate_plot",
        lambda _result, path: generated.append(path),
    )
    monkeypatch.setattr(
        "batch_latency_test.generate_csv",
        lambda _result, path: generated.append(path),
    )
    monkeypatch.setattr(
        "batch_latency_test.generate_summary",
        lambda _result, path: generated.append(path),
    )

    ok = asyncio.run(
        run_batch(
            [str(audio)],
            "http://localhost:8000",
            str(tmp_path / "results"),
        )
    )

    assert ok is False
    assert len(generated) == 3
