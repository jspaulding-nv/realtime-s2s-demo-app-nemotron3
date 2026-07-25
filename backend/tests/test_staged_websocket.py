"""WebSocket contract tests for the feature-flagged staged pipeline path."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.websockets import WebSocketState

from config import staged_pipeline_config
from staged_models import StagedOutputEventKind
from websocket_handler import SessionManager, SessionStatus, TranslationSession


class FakeStagedPipeline:
    def __init__(self, *, start_error=None, subsegmentation_enabled=False):
        self.outputs = asyncio.Queue()
        self.start_error = start_error
        self.config = SimpleNamespace(
            tts_subsegment_max_chars=(
                40 if subsegmentation_enabled else 0
            )
        )
        self.started = False
        self.closed = False
        self.close_calls = 0
        self.audio_chunks = []
        self.finish_calls = 0
        self.dequeued_audio_sequence_ids = []
        self.dequeued_audio_subsegment_keys = []

    async def start(self):
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    def add_audio(self, audio):
        self.audio_chunks.append(audio)

    def finish_input(self):
        self.finish_calls += 1

    async def next_output(self):
        output = await self.outputs.get()
        if output.kind is StagedOutputEventKind.AUDIO:
            subsequence_id = getattr(output.segment, "subsequence_id", 0)
            subsequence_count = getattr(output.segment, "subsequence_count", 1)
            self.dequeued_audio_subsegment_keys.append(
                (
                    output.segment.sequence_id,
                    subsequence_id,
                    subsequence_count,
                )
            )
            if subsequence_id == subsequence_count - 1:
                self.dequeued_audio_sequence_ids.append(
                    output.segment.sequence_id
                )
        return output

    async def aclose(self):
        self.close_calls += 1
        self.closed = True

    def summary(self, include_events=False):
        result = {
            "telemetry_schema_version": (
                2 if self.config.tts_subsegment_max_chars > 0 else 1
            ),
            "state": "closed" if self.closed else "running",
            "outcome": "complete",
            "cleanup_errors": [],
            "completed_sequence_ids": list(self.dequeued_audio_sequence_ids),
            "incomplete_sequence_ids": [],
            "events": (
                [{"stage": "fake", "event": "captured"}]
                if include_events
                else None
            ),
        }
        if self.config.tts_subsegment_max_chars > 0:
            result["completed_subsegment_keys"] = [
                {
                    "parent_sequence_id": parent_sequence_id,
                    "subsequence_id": subsequence_id,
                    "subsequence_count": subsequence_count,
                }
                for (
                    parent_sequence_id,
                    subsequence_id,
                    subsequence_count,
                ) in self.dequeued_audio_subsegment_keys
            ]
        return result


class WorkerFailureInputRacePipeline(FakeStagedPipeline):
    """Expose FAILED just before the structured relay error becomes readable."""

    def __init__(self, *, fail_operation, queued_audio=None):
        super().__init__()
        self.fail_operation = fail_operation
        self.queued_audio = queued_audio
        self.failure = None
        self.state = SimpleNamespace(value="running")

    def _raise_during_terminal_enqueue(self):
        self.failure = ("nmt", "translation failed")
        self.state = SimpleNamespace(value="failed")
        if self.queued_audio is not None:
            self.outputs.put_nowait(
                SimpleNamespace(
                    kind=StagedOutputEventKind.AUDIO,
                    segment=SimpleNamespace(
                        audio=self.queued_audio,
                        sequence_id=0,
                    ),
                )
            )
        error = SimpleNamespace(
            kind=StagedOutputEventKind.ERROR,
            stage="nmt",
            error="translation failed",
        )
        # Model the real pipeline's narrow interleaving: state/failure are
        # visible now, while its structured terminal is queued on the next
        # event-loop turn.
        asyncio.get_running_loop().call_soon(self.outputs.put_nowait, error)
        raise RuntimeError("input observed failed worker")

    def add_audio(self, audio):
        if self.fail_operation == "add_audio":
            self._raise_during_terminal_enqueue()
        super().add_audio(audio)

    def finish_input(self):
        if self.fail_operation == "finish_input":
            self._raise_during_terminal_enqueue()
        super().finish_input()


class YieldingCleanupPipeline(FakeStagedPipeline):
    """Expose cancellation while WebSocket shutdown awaits model cleanup."""

    def __init__(self):
        super().__init__()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_finished = asyncio.Event()
        self.close_cancelled = False

    async def aclose(self):
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        self.closed = True
        self.close_finished.set()


@pytest.fixture
def mock_websocket():
    websocket = AsyncMock()
    websocket.client_state = WebSocketState.CONNECTED
    websocket.application_state = WebSocketState.CONNECTED
    return websocket


async def _wait_until(predicate, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0)


def composite_audio(
    parent_sequence_id,
    subsequence_id,
    subsequence_count,
    audio,
):
    return SimpleNamespace(
        kind=StagedOutputEventKind.AUDIO,
        segment=SimpleNamespace(
            audio=audio,
            sequence_id=parent_sequence_id,
            subsequence_id=subsequence_id,
            subsequence_count=subsequence_count,
        ),
    )


def incremental_audio_frame(
    parent_sequence_id,
    audio_frame_id,
    audio,
    *,
    sample_rate_hz=16_000,
    channels=1,
    bytes_per_sample=2,
    source_start_ms=0.0,
    source_end_ms=1_000.0,
):
    return SimpleNamespace(
        kind=StagedOutputEventKind.AUDIO_FRAME,
        frame=SimpleNamespace(
            audio=audio,
            parent_sequence_id=parent_sequence_id,
            audio_frame_id=audio_frame_id,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
            bytes_per_sample=bytes_per_sample,
            translation=SimpleNamespace(
                segment=SimpleNamespace(
                    source_start_ms=source_start_ms,
                    source_end_ms=source_end_ms,
                )
            ),
        ),
    )


def incremental_parent_complete(
    parent_sequence_id,
    audio_frame_count,
    audio_bytes,
    *,
    retry_count=0,
    atomic_fallback_applied=False,
    sample_rate_hz=16_000,
    channels=1,
    bytes_per_sample=2,
    source_start_ms=0.0,
    source_end_ms=1_000.0,
):
    return SimpleNamespace(
        kind=StagedOutputEventKind.PARENT_COMPLETE,
        completion=SimpleNamespace(
            parent_sequence_id=parent_sequence_id,
            audio_frame_count=audio_frame_count,
            audio_bytes=audio_bytes,
            retry_count=retry_count,
            atomic_fallback_applied=atomic_fallback_applied,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
            bytes_per_sample=bytes_per_sample,
            translation=SimpleNamespace(
                segment=SimpleNamespace(
                    source_start_ms=source_start_ms,
                    source_end_ms=source_end_ms,
                )
            ),
        ),
    )


class FakeIncrementalStagedPipeline(FakeStagedPipeline):
    """Schema-v3 output fake with independent frame/parent accounting."""

    def __init__(self):
        super().__init__()
        self.config.tts_incremental_publish_enabled = True
        self.config.tts_incremental_atomic_fallback_max_chars = 4
        self.published_audio_frame_keys = []
        self.published_audio_frame_bytes = []
        self.dequeued_audio_frame_keys = []
        self.dequeued_audio_frame_bytes = []
        self.produced_parent_summaries = []
        self.completed_parent_summaries = []

    async def next_output(self):
        output = await self.outputs.get()
        if output.kind is StagedOutputEventKind.AUDIO_FRAME:
            frame = output.frame
            key = (frame.parent_sequence_id, frame.audio_frame_id)
            self.published_audio_frame_keys.append(key)
            self.published_audio_frame_bytes.append(len(frame.audio))
            self.dequeued_audio_frame_keys.append(key)
            self.dequeued_audio_frame_bytes.append(len(frame.audio))
        elif output.kind is StagedOutputEventKind.PARENT_COMPLETE:
            completion = output.completion
            summary = {
                "parent_sequence_id": completion.parent_sequence_id,
                "audio_frame_count": completion.audio_frame_count,
                "audio_bytes": completion.audio_bytes,
                "retry_count": completion.retry_count,
                "atomic_fallback_applied": (
                    completion.atomic_fallback_applied
                ),
            }
            self.produced_parent_summaries.append(summary)
            self.completed_parent_summaries.append(dict(summary))
            self.dequeued_audio_sequence_ids.append(
                completion.parent_sequence_id
            )
        return output

    def summary(self, include_events=False):
        def frame_keys(keys):
            return [
                {
                    "parent_sequence_id": parent_sequence_id,
                    "audio_frame_id": audio_frame_id,
                }
                for parent_sequence_id, audio_frame_id in keys
            ]

        fallback_parent_ids = [
            item["parent_sequence_id"]
            for item in self.produced_parent_summaries
            if item["atomic_fallback_applied"]
        ]
        return {
            "telemetry_schema_version": 3,
            "tts_incremental_publish_enabled": True,
            "tts_incremental_atomic_fallback_max_chars": 4,
            "tts_incremental_atomic_fallback_parent_count": len(
                fallback_parent_ids
            ),
            "tts_incremental_atomic_fallback_parent_sequence_ids": list(
                fallback_parent_ids
            ),
            "state": "closed" if self.closed else "running",
            "outcome": "complete",
            "cleanup_errors": [],
            "completed_sequence_ids": list(
                self.dequeued_audio_sequence_ids
            ),
            "incomplete_sequence_ids": [],
            "published_audio_frame_keys": frame_keys(
                self.published_audio_frame_keys
            ),
            "published_audio_frame_bytes": list(
                self.published_audio_frame_bytes
            ),
            "dequeued_audio_frame_keys": frame_keys(
                self.dequeued_audio_frame_keys
            ),
            "dequeued_audio_frame_bytes": list(
                self.dequeued_audio_frame_bytes
            ),
            "produced_parent_summaries": [
                dict(item) for item in self.produced_parent_summaries
            ],
            "completed_parent_summaries": [
                dict(item) for item in self.completed_parent_summaries
            ],
            "events": (
                [{"stage": "fake", "event": "captured"}]
                if include_events
                else None
            ),
        }


@pytest.mark.asyncio
async def test_schema_v3_frames_are_sent_fifo_and_parent_complete_has_no_wire_bytes(
    mock_websocket,
):
    pipeline = FakeIncrementalStagedPipeline()
    outbound = []

    async def record_audio(payload):
        outbound.append(("audio", payload))

    async def record_json(payload):
        outbound.append(("json", payload))

    mock_websocket.send_bytes.side_effect = record_audio
    mock_websocket.send_json.side_effect = record_json
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.finish_input()
    await pipeline.outputs.put(incremental_audio_frame(0, 0, b"aa"))
    await pipeline.outputs.put(incremental_audio_frame(0, 1, b"bbbb"))
    await pipeline.outputs.put(incremental_parent_complete(0, 2, 6))
    await pipeline.outputs.put(incremental_audio_frame(1, 0, b"cc"))
    await pipeline.outputs.put(
        incremental_parent_complete(
            1,
            1,
            2,
            atomic_fallback_applied=True,
        )
    )
    await pipeline.outputs.put(
        SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
    )
    await _wait_until(lambda: session.status is SessionStatus.COMPLETED)

    assert [
        payload for kind, payload in outbound if kind == "audio"
    ] == [b"aa", b"bbbb", b"cc"]
    assert session._staged_audio_sequence_ids_sent == [0, 1]
    assert session._staged_parent_completions_sent == [
        {
            "parent_sequence_id": 0,
            "audio_frame_count": 2,
            "audio_bytes": 6,
            "retry_count": 0,
            "atomic_fallback_applied": False,
        },
        {
            "parent_sequence_id": 1,
            "audio_frame_count": 1,
            "audio_bytes": 2,
            "retry_count": 0,
            "atomic_fallback_applied": True,
        },
    ]
    snapshot = session.staged_telemetry_snapshot()
    assert snapshot["tts_incremental_atomic_fallback_parent_count"] == 1
    assert (
        snapshot["tts_incremental_atomic_fallback_parent_sequence_ids"]
        == [1]
    )
    assert snapshot["websocket_completed_parent_summaries"] == (
        snapshot["produced_parent_summaries"]
    )
    completed = {
        "type": "status",
        "status": "completed",
        "message": "Riva translated-audio stream complete",
    }
    assert outbound.index(("audio", b"cc")) < outbound.index(
        ("json", completed)
    )
    assert not any(
        payload.get("type") in {"audio_frame", "audio_parent_complete"}
        for kind, payload in outbound
        if kind == "json"
    )


@pytest.mark.asyncio
async def test_schema_v3_metadata_v1_precedes_each_frame_and_completes_parent(
    mock_websocket,
):
    pipeline = FakeIncrementalStagedPipeline()
    outbound = []

    async def record_audio(payload):
        outbound.append(("audio", payload))

    async def record_json(payload):
        outbound.append(("json", payload))

    mock_websocket.send_bytes.side_effect = record_audio
    mock_websocket.send_json.side_effect = record_json
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream(
        "es-US",
        audio_metadata_protocol_version=1,
    )
    generation = session._staged_generation
    await session.finish_input()
    await pipeline.outputs.put(
        incremental_audio_frame(
            0,
            0,
            b"aa",
            sample_rate_hz=22_050,
            source_start_ms=1_250.0,
            source_end_ms=2_500.0,
        )
    )
    await pipeline.outputs.put(
        incremental_audio_frame(
            0,
            1,
            b"bbbb",
            sample_rate_hz=22_050,
            source_start_ms=1_250.0,
            source_end_ms=2_500.0,
        )
    )
    await pipeline.outputs.put(
        incremental_parent_complete(
            0,
            2,
            6,
            sample_rate_hz=22_050,
            source_start_ms=1_250.0,
            source_end_ms=2_500.0,
        )
    )
    await pipeline.outputs.put(
        incremental_audio_frame(
            1,
            0,
            b"cc",
            sample_rate_hz=22_050,
            source_start_ms=2_500.0,
            source_end_ms=2_900.0,
        )
    )
    await pipeline.outputs.put(
        incremental_parent_complete(
            1,
            1,
            2,
            atomic_fallback_applied=True,
            sample_rate_hz=22_050,
            source_start_ms=2_500.0,
            source_end_ms=2_900.0,
        )
    )
    await pipeline.outputs.put(
        SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
    )
    await _wait_until(
        lambda: (
            session.status is SessionStatus.COMPLETED
            and session._audio_metadata_protocol_version is None
        )
    )

    relevant = [
        item
        for item in outbound
        if item[0] == "audio"
        or item[1].get("type")
        in {"audio_frame", "audio_parent_complete"}
    ]
    assert relevant == [
        (
            "json",
            {
                "type": "audio_frame",
                "protocolVersion": 1,
                "streamGeneration": generation,
                "parentSequenceId": 0,
                "audioFrameId": 0,
                "audioBytes": 2,
                "sampleRateHz": 22_050,
                "channels": 1,
                "bytesPerSample": 2,
                "sourceStartMs": 1_250.0,
                "sourceEndMs": 2_500.0,
            },
        ),
        ("audio", b"aa"),
        (
            "json",
            {
                "type": "audio_frame",
                "protocolVersion": 1,
                "streamGeneration": generation,
                "parentSequenceId": 0,
                "audioFrameId": 1,
                "audioBytes": 4,
                "sampleRateHz": 22_050,
                "channels": 1,
                "bytesPerSample": 2,
                "sourceStartMs": 1_250.0,
                "sourceEndMs": 2_500.0,
            },
        ),
        ("audio", b"bbbb"),
        (
            "json",
            {
                "type": "audio_parent_complete",
                "protocolVersion": 1,
                "streamGeneration": generation,
                "parentSequenceId": 0,
                "audioFrameCount": 2,
                "audioBytes": 6,
                "sourceStartMs": 1_250.0,
                "sourceEndMs": 2_500.0,
            },
        ),
        (
            "json",
            {
                "type": "audio_frame",
                "protocolVersion": 1,
                "streamGeneration": generation,
                "parentSequenceId": 1,
                "audioFrameId": 0,
                "audioBytes": 2,
                "sampleRateHz": 22_050,
                "channels": 1,
                "bytesPerSample": 2,
                "sourceStartMs": 2_500.0,
                "sourceEndMs": 2_900.0,
            },
        ),
        ("audio", b"cc"),
        (
            "json",
            {
                "type": "audio_parent_complete",
                "protocolVersion": 1,
                "streamGeneration": generation,
                "parentSequenceId": 1,
                "audioFrameCount": 1,
                "audioBytes": 2,
                "sourceStartMs": 2_500.0,
                "sourceEndMs": 2_900.0,
            },
        ),
    ]
    assert session._staged_parent_completions_sent[-1][
        "atomic_fallback_applied"
    ] is True


@pytest.mark.asyncio
async def test_metadata_v1_rejects_pcm_format_change_between_parents(
    mock_websocket,
):
    pipeline = FakeIncrementalStagedPipeline()
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream(
        "es-US",
        audio_metadata_protocol_version=1,
    )
    generation = session._staged_generation
    assert await session._send_staged_audio(
        generation,
        b"aa",
        0,
        audio_frame_id=0,
        include_frame=True,
        sample_rate_hz=16_000,
        channels=1,
        bytes_per_sample=2,
        source_start_ms=0.0,
        source_end_ms=1_000.0,
    ) is True
    assert await session._complete_staged_parent(
        generation,
        incremental_parent_complete(0, 1, 2).completion,
    ) is True

    assert await session._send_staged_audio(
        generation,
        b"bb",
        1,
        audio_frame_id=0,
        include_frame=True,
        sample_rate_hz=22_050,
        channels=1,
        bytes_per_sample=2,
        source_start_ms=1_000.0,
        source_end_ms=2_000.0,
    ) is False
    assert [
        call.args[0]
        for call in mock_websocket.send_bytes.await_args_list
    ] == [b"aa"]
    assert [
        call.args[0]["parentSequenceId"]
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "audio_frame"
    ] == [0]

    await session.stop_stream()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested_version",
    [None, True, False, 0, 2, 1.0, "1", {}, []],
)
async def test_audio_metadata_version_is_rejected_fail_closed(
    mock_websocket,
    requested_version,
):
    factory = MagicMock(return_value=FakeIncrementalStagedPipeline())
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=factory,
    )

    await session.start_stream(
        "es-US",
        audio_metadata_protocol_version=requested_version,
    )

    factory.assert_not_called()
    assert session.status is SessionStatus.ERROR
    mock_websocket.send_bytes.assert_not_awaited()
    errors = [
        call.args[0]
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "error"
    ]
    assert errors == [
        {
            "type": "error",
            "message": (
                "audioMetadataProtocolVersion must be the integer 1"
            ),
        }
    ]


@pytest.mark.asyncio
async def test_audio_metadata_v1_rejects_non_incremental_staged_pipeline(
    mock_websocket,
):
    pipeline = FakeStagedPipeline()
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream(
        "es-US",
        audio_metadata_protocol_version=1,
    )

    assert not pipeline.started
    assert pipeline.closed
    assert session.status is SessionStatus.ERROR
    assert session._audio_metadata_protocol_version is None
    mock_websocket.send_bytes.assert_not_awaited()
    errors = [
        call.args[0]
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "error"
    ]
    assert errors == [
        {
            "type": "error",
            "message": (
                "Failed to start stream: audio metadata protocol version 1 "
                "requires the staged schema-3 incremental TTS pipeline"
            ),
        }
    ]


@pytest.mark.asyncio
async def test_audio_metadata_negotiation_is_not_sticky_across_streams(
    mock_websocket,
):
    first = FakeIncrementalStagedPipeline()
    second = FakeIncrementalStagedPipeline()
    pipelines = iter((first, second))
    outbound = []

    async def record_json(payload):
        outbound.append(("json", payload))

    async def record_audio(payload):
        outbound.append(("audio", payload))

    mock_websocket.send_json.side_effect = record_json
    mock_websocket.send_bytes.side_effect = record_audio
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: next(pipelines),
    )

    await session.start_stream(
        "es-US",
        audio_metadata_protocol_version=1,
    )
    first_generation = session._staged_generation
    await first.outputs.put(incremental_audio_frame(0, 0, b"aa"))
    await first.outputs.put(incremental_parent_complete(0, 1, 2))
    await _wait_until(
        lambda: any(
            kind == "json"
            and payload.get("type") == "audio_parent_complete"
            for kind, payload in outbound
        )
    )

    await session.start_stream("es-US")
    await second.outputs.put(incremental_audio_frame(0, 0, b"bb"))
    await second.outputs.put(incremental_parent_complete(0, 1, 2))
    await _wait_until(
        lambda: [payload for kind, payload in outbound if kind == "audio"]
        == [b"aa", b"bb"]
    )

    assert [
        payload
        for kind, payload in outbound
        if kind == "json" and payload.get("type") == "audio_frame"
    ] == [
        {
            "type": "audio_frame",
            "protocolVersion": 1,
            "streamGeneration": first_generation,
            "parentSequenceId": 0,
            "audioFrameId": 0,
            "audioBytes": 2,
            "sampleRateHz": 16_000,
            "channels": 1,
            "bytesPerSample": 2,
            "sourceStartMs": 0.0,
            "sourceEndMs": 1_000.0,
        }
    ]
    await session.stop_stream()
    assert session._audio_metadata_protocol_version is None


@pytest.mark.asyncio
async def test_schema_v3_summary_reconciles_frames_bytes_and_parents(
    mock_websocket,
):
    pipeline = FakeIncrementalStagedPipeline()
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.finish_input()
    await pipeline.outputs.put(incremental_audio_frame(0, 0, b"aa"))
    await pipeline.outputs.put(incremental_audio_frame(0, 1, b"bbbb"))
    await pipeline.outputs.put(incremental_parent_complete(0, 2, 6))
    await pipeline.outputs.put(
        SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
    )
    await _wait_until(lambda: session.status is SessionStatus.COMPLETED)
    snapshot = session.staged_telemetry_snapshot()

    expected_keys = [
        {"parent_sequence_id": 0, "audio_frame_id": 0},
        {"parent_sequence_id": 0, "audio_frame_id": 1},
    ]
    expected_parent = [
        {
            "parent_sequence_id": 0,
            "audio_frame_count": 2,
            "audio_bytes": 6,
            "retry_count": 0,
            "atomic_fallback_applied": False,
        }
    ]
    assert snapshot["published_audio_frame_keys"] == expected_keys
    assert snapshot["dequeued_audio_frame_keys"] == expected_keys
    assert snapshot["websocket_sent_audio_frame_keys"] == expected_keys
    assert snapshot["published_audio_frame_bytes"] == [2, 4]
    assert snapshot["dequeued_audio_frame_bytes"] == [2, 4]
    assert snapshot["websocket_sent_audio_frame_bytes"] == [2, 4]
    assert snapshot["produced_parent_summaries"] == expected_parent
    assert snapshot["completed_parent_summaries"] == expected_parent
    assert snapshot["websocket_completed_parent_summaries"] == expected_parent
    assert snapshot["tts_incremental_atomic_fallback_parent_count"] == 0
    assert (
        snapshot["tts_incremental_atomic_fallback_parent_sequence_ids"]
        == []
    )
    assert snapshot["websocket_sent_sequence_ids"] == [0]
    assert [
        (
            event["parent_sequence_id"],
            event["audio_frame_id"],
            event["audio_bytes"],
        )
        for event in snapshot["websocket_send_events"]
    ] == [(0, 0, 2), (0, 1, 4)]


@pytest.mark.asyncio
async def test_schema_v3_summary_mismatch_replaces_completion_with_error(
    mock_websocket,
):
    class MismatchedIncrementalPipeline(FakeIncrementalStagedPipeline):
        def summary(self, include_events=False):
            result = super().summary(include_events=include_events)
            result["published_audio_frame_bytes"] = [8]
            return result

    pipeline = MismatchedIncrementalPipeline()
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.finish_input()
    await pipeline.outputs.put(incremental_audio_frame(0, 0, b"aa"))
    await pipeline.outputs.put(incremental_parent_complete(0, 1, 2))
    await pipeline.outputs.put(
        SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
    )
    await _wait_until(lambda: session.status is SessionStatus.ERROR)

    assert mock_websocket.send_json.await_args_list[-1].args[0] == {
        "type": "error",
        "message": (
            "Staged cleanup failed: schema-3 audio-frame byte counts "
            "did not reconcile across pipeline and WebSocket"
        ),
    }
    assert not any(
        call.args[0].get("status") == "completed"
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "status"
    )


@pytest.mark.asyncio
async def test_schema_v3_fallback_summary_mismatch_replaces_completion_with_error(
    mock_websocket,
):
    class MismatchedFallbackPipeline(FakeIncrementalStagedPipeline):
        def summary(self, include_events=False):
            result = super().summary(include_events=include_events)
            result[
                "tts_incremental_atomic_fallback_parent_count"
            ] = 1
            result[
                "tts_incremental_atomic_fallback_parent_sequence_ids"
            ] = [0]
            return result

    pipeline = MismatchedFallbackPipeline()
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.finish_input()
    await pipeline.outputs.put(incremental_audio_frame(0, 0, b"aa"))
    await pipeline.outputs.put(incremental_parent_complete(0, 1, 2))
    await pipeline.outputs.put(
        SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
    )
    await _wait_until(lambda: session.status is SessionStatus.ERROR)

    assert mock_websocket.send_json.await_args_list[-1].args[0] == {
        "type": "error",
        "message": (
            "Staged cleanup failed: schema-3 atomic fallback summary did "
            "not reconcile across pipeline and WebSocket"
        ),
    }


@pytest.mark.asyncio
async def test_schema_v3_send_failure_preserves_prefix_and_emits_one_error(
    mock_websocket,
):
    pipeline = FakeIncrementalStagedPipeline()
    mock_websocket.send_bytes.side_effect = [
        None,
        RuntimeError("socket write failed"),
    ]
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.finish_input()
    await pipeline.outputs.put(incremental_audio_frame(0, 0, b"aa"))
    await pipeline.outputs.put(incremental_audio_frame(0, 1, b"bbbb"))
    await pipeline.outputs.put(incremental_parent_complete(0, 2, 6))
    await pipeline.outputs.put(
        SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
    )
    await _wait_until(lambda: session.status is SessionStatus.ERROR)
    await _wait_until(lambda: pipeline.closed)

    assert [call.args[0] for call in mock_websocket.send_bytes.await_args_list] == [
        b"aa",
        b"bbbb",
    ]
    errors = [
        call.args[0]
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "error"
    ]
    assert errors == [
        {
            "type": "error",
            "message": "translated audio could not be sent to the client",
        }
    ]
    assert session._staged_audio_frame_keys_sent == [(0, 0)]
    assert session._staged_audio_frame_bytes_sent == [2]
    assert session._staged_parent_completions_sent == []
    assert session._staged_audio_sequence_ids_sent == []
    assert session._staged_pending_frame_parent == 0
    assert session._staged_pending_frame_count == 1
    assert session._staged_pending_frame_bytes == 2
    assert not any(
        call.args[0].get("status") == "completed"
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "status"
    )


@pytest.mark.asyncio
async def test_metadata_v1_binary_send_failure_leaves_header_then_terminal_error(
    mock_websocket,
):
    pipeline = FakeIncrementalStagedPipeline()
    outbound = []

    async def record_json(payload):
        outbound.append(("json", payload))

    async def fail_audio(payload):
        outbound.append(("audio_attempt", payload))
        raise RuntimeError("socket write failed")

    mock_websocket.send_json.side_effect = record_json
    mock_websocket.send_bytes.side_effect = fail_audio
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream(
        "es-US",
        audio_metadata_protocol_version=1,
    )
    generation = session._staged_generation
    await session.finish_input()
    await pipeline.outputs.put(
        incremental_audio_frame(
            0,
            0,
            b"aa",
            source_start_ms=500.0,
            source_end_ms=750.0,
        )
    )
    await pipeline.outputs.put(
        incremental_parent_complete(
            0,
            1,
            2,
            source_start_ms=500.0,
            source_end_ms=750.0,
        )
    )
    await _wait_until(lambda: session.status is SessionStatus.ERROR)
    await _wait_until(lambda: pipeline.closed)

    header = {
        "type": "audio_frame",
        "protocolVersion": 1,
        "streamGeneration": generation,
        "parentSequenceId": 0,
        "audioFrameId": 0,
        "audioBytes": 2,
        "sampleRateHz": 16_000,
        "channels": 1,
        "bytesPerSample": 2,
        "sourceStartMs": 500.0,
        "sourceEndMs": 750.0,
    }
    error = {
        "type": "error",
        "message": "translated audio could not be sent to the client",
    }
    assert outbound.index(("json", header)) < outbound.index(
        ("audio_attempt", b"aa")
    )
    assert outbound.index(("audio_attempt", b"aa")) < outbound.index(
        ("json", error)
    )
    assert not any(
        payload.get("type") == "audio_parent_complete"
        for kind, payload in outbound
        if kind == "json"
    )
    assert session._staged_audio_frame_keys_sent == []
    assert session._staged_parent_completions_sent == []


@pytest.mark.asyncio
async def test_metadata_v1_header_send_failure_never_sends_unpaired_binary(
    mock_websocket,
):
    pipeline = FakeIncrementalStagedPipeline()

    async def fail_header(payload):
        if payload.get("type") == "audio_frame":
            raise RuntimeError("socket JSON write failed")

    mock_websocket.send_json.side_effect = fail_header
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream(
        "es-US",
        audio_metadata_protocol_version=1,
    )
    await pipeline.outputs.put(incremental_audio_frame(0, 0, b"aa"))
    await _wait_until(lambda: session.status is SessionStatus.ERROR)

    mock_websocket.send_bytes.assert_not_awaited()
    errors = [
        call.args[0]
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "error"
    ]
    assert errors == [
        {
            "type": "error",
            "message": "translated audio could not be sent to the client",
        }
    ]
    assert session._staged_audio_frame_keys_sent == []
    assert session._staged_parent_completions_sent == []


@pytest.mark.asyncio
async def test_metadata_v1_parent_completion_precedes_structured_failure(
    mock_websocket,
):
    pipeline = FakeIncrementalStagedPipeline()
    outbound = []

    async def record_json(payload):
        outbound.append(("json", payload))

    async def record_audio(payload):
        outbound.append(("audio", payload))

    mock_websocket.send_json.side_effect = record_json
    mock_websocket.send_bytes.side_effect = record_audio
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream(
        "es-US",
        audio_metadata_protocol_version=1,
    )
    await pipeline.outputs.put(incremental_audio_frame(0, 0, b"aa"))
    await pipeline.outputs.put(incremental_parent_complete(0, 1, 2))
    await pipeline.outputs.put(
        SimpleNamespace(
            kind=StagedOutputEventKind.ERROR,
            stage="nmt",
            error="translation failed",
        )
    )
    await _wait_until(lambda: session.status is SessionStatus.ERROR)

    parent_complete = next(
        payload
        for kind, payload in outbound
        if kind == "json"
        and payload.get("type") == "audio_parent_complete"
    )
    error = {
        "type": "error",
        "message": "Staged nmt failed: translation failed",
    }
    assert outbound.index(("audio", b"aa")) < outbound.index(
        ("json", parent_complete)
    )
    assert outbound.index(("json", parent_complete)) < outbound.index(
        ("json", error)
    )
    assert not any(
        payload.get("status") == "completed"
        for kind, payload in outbound
        if kind == "json" and payload.get("type") == "status"
    )


@pytest.mark.asyncio
async def test_metadata_v1_preserves_end_only_source_range_without_word_times(
    mock_websocket,
):
    pipeline = FakeIncrementalStagedPipeline()
    outbound_json = []

    async def record_json(payload):
        outbound_json.append(payload)

    mock_websocket.send_json.side_effect = record_json
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream(
        "es-US",
        audio_metadata_protocol_version=1,
    )
    await session.finish_input()
    await pipeline.outputs.put(
        incremental_audio_frame(
            0,
            0,
            b"aa",
            source_start_ms=None,
            source_end_ms=750.0,
        )
    )
    await pipeline.outputs.put(
        incremental_parent_complete(
            0,
            1,
            2,
            source_start_ms=None,
            source_end_ms=750.0,
        )
    )
    await pipeline.outputs.put(
        SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
    )
    await _wait_until(lambda: session.status is SessionStatus.COMPLETED)

    frame_header = next(
        payload
        for payload in outbound_json
        if payload.get("type") == "audio_frame"
    )
    parent_complete = next(
        payload
        for payload in outbound_json
        if payload.get("type") == "audio_parent_complete"
    )
    assert frame_header["sourceStartMs"] is None
    assert frame_header["sourceEndMs"] == 750.0
    assert parent_complete["sourceStartMs"] is None
    assert parent_complete["sourceEndMs"] == 750.0
    mock_websocket.send_bytes.assert_awaited_once_with(b"aa")


@pytest.mark.asyncio
async def test_schema_v3_new_generation_resets_partial_frame_evidence(
    mock_websocket,
):
    first = FakeIncrementalStagedPipeline()
    second = FakeIncrementalStagedPipeline()
    pipelines = iter((first, second))
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: next(pipelines),
    )

    await session.start_stream("es-US")
    await first.outputs.put(incremental_audio_frame(0, 0, b"aa"))
    await _wait_until(
        lambda: session._staged_audio_frame_keys_sent == [(0, 0)]
    )
    assert session._staged_pending_frame_parent == 0

    await session.start_stream("es-US")

    assert first.closed
    assert second.started
    assert session.status is SessionStatus.LISTENING
    assert session._staged_audio_sequence_ids_sent == []
    assert session._staged_audio_frame_keys_sent == []
    assert session._staged_audio_frame_bytes_sent == []
    assert session._staged_parent_completions_sent == []
    assert session._staged_pending_frame_parent is None
    assert session._staged_pending_frame_count == 0
    assert session._staged_pending_frame_bytes == 0
    active_snapshot = session.staged_telemetry_snapshot()
    assert active_snapshot["websocket_sent_sequence_ids"] == []
    assert active_snapshot["websocket_sent_audio_frame_keys"] == []
    assert active_snapshot["websocket_sent_audio_frame_bytes"] == []
    assert active_snapshot["websocket_completed_parent_summaries"] == []
    assert active_snapshot["websocket_send_events"] == []
    await session.stop_stream()


@pytest.mark.asyncio
async def test_staged_stream_preserves_audio_and_terminal_websocket_contract(
    mock_websocket,
):
    pipeline = FakeStagedPipeline()
    outbound = []

    async def record_json(payload):
        outbound.append(("json", payload))

    async def record_audio(payload):
        outbound.append(("audio", payload))

    mock_websocket.send_json.side_effect = record_json
    mock_websocket.send_bytes.side_effect = record_audio
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    with (
        patch("websocket_handler.calculate_rms", return_value=0.25),
        patch("websocket_handler.timing_logger") as timing_logger,
    ):
        await session.start_stream("es-US")
        await session.process_audio(b"source pcm")
        await session.finish_input()
        await pipeline.outputs.put(
            SimpleNamespace(
                kind=StagedOutputEventKind.AUDIO,
                segment=SimpleNamespace(audio=b"translated pcm", sequence_id=0),
            )
        )
        await pipeline.outputs.put(
            SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
        )
        await _wait_until(lambda: session.status is SessionStatus.COMPLETED)
        await _wait_until(lambda: pipeline.closed)

    assert pipeline.started
    assert pipeline.audio_chunks == [b"source pcm"]
    assert pipeline.finish_calls == 1
    assert session._staged_audio_sequence_ids_sent == [0]
    timing_logger.log_audio_received.assert_called_once_with(len(b"source pcm"))
    timing_logger.log_audio_from_riva.assert_called_once_with(
        len(b"translated pcm")
    )
    timing_logger.log_audio_sent_to_client.assert_called_once_with(
        len(b"translated pcm")
    )

    audio_index = outbound.index(("audio", b"translated pcm"))
    completed = (
        "json",
        {
            "type": "status",
            "status": "completed",
            "message": "Riva translated-audio stream complete",
        },
    )
    assert audio_index < outbound.index(completed)


@pytest.mark.asyncio
async def test_staged_pipeline_error_maps_to_existing_error_message_contract(
    mock_websocket,
):
    pipeline = FakeStagedPipeline()
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await pipeline.outputs.put(
        SimpleNamespace(
            kind=StagedOutputEventKind.ERROR,
            stage="nmt",
            error="translation failed",
        )
    )
    await _wait_until(lambda: session.status is SessionStatus.ERROR)
    await _wait_until(lambda: pipeline.closed)

    assert mock_websocket.send_json.await_args_list[-1].args[0] == {
        "type": "error",
        "message": "Staged nmt failed: translation failed",
    }
    assert not any(
        call.args[0].get("status") == "completed"
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "status"
    )


@pytest.mark.asyncio
async def test_multiple_audio_segments_are_fifo_and_delay_completion(
    mock_websocket,
):
    pipeline = FakeStagedPipeline()
    first_send_started = asyncio.Event()
    release_first_send = asyncio.Event()
    sent_audio = []

    async def delayed_send(audio):
        sent_audio.append(audio)
        if len(sent_audio) == 1:
            first_send_started.set()
            await release_first_send.wait()

    mock_websocket.send_bytes.side_effect = delayed_send
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.finish_input()
    for sequence_id, audio in enumerate((b"first", b"second")):
        await pipeline.outputs.put(
            SimpleNamespace(
                kind=StagedOutputEventKind.AUDIO,
                segment=SimpleNamespace(audio=audio, sequence_id=sequence_id),
            )
        )
    await pipeline.outputs.put(
        SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
    )

    await asyncio.wait_for(first_send_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert sent_audio == [b"first"]
    assert session.status is SessionStatus.PROCESSING

    release_first_send.set()
    await _wait_until(lambda: session.status is SessionStatus.COMPLETED)

    assert sent_audio == [b"first", b"second"]
    assert session._staged_audio_sequence_ids_sent == [0, 1]
    telemetry = session.staged_telemetry_snapshot()
    assert telemetry["websocket_sent_sequence_ids"] == [0, 1]
    assert [
        event["sequence_id"] for event in telemetry["websocket_send_events"]
    ] == [0, 1]
    assert telemetry["events"] == [{"stage": "fake", "event": "captured"}]


@pytest.mark.asyncio
async def test_subsegments_send_fifo_and_count_parent_only_on_final_child(
    mock_websocket,
):
    pipeline = FakeStagedPipeline(subsegmentation_enabled=True)
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.finish_input()
    for subsequence_id, audio in enumerate((b"first", b"second", b"third")):
        await pipeline.outputs.put(
            composite_audio(7, subsequence_id, 3, audio)
        )
    await pipeline.outputs.put(
        SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
    )
    await _wait_until(lambda: session.status is SessionStatus.COMPLETED)
    await _wait_until(lambda: pipeline.closed)

    expected_keys = [(7, 0, 3), (7, 1, 3), (7, 2, 3)]
    expected_payloads = [
        {
            "parent_sequence_id": parent_sequence_id,
            "subsequence_id": subsequence_id,
            "subsequence_count": subsequence_count,
        }
        for (
            parent_sequence_id,
            subsequence_id,
            subsequence_count,
        ) in expected_keys
    ]
    assert [
        call.args[0] for call in mock_websocket.send_bytes.await_args_list
    ] == [b"first", b"second", b"third"]
    assert pipeline.dequeued_audio_subsegment_keys == expected_keys
    assert pipeline.dequeued_audio_sequence_ids == [7]
    assert session._staged_audio_subsegment_keys_sent == expected_keys
    assert session._staged_audio_sequence_ids_sent == [7]

    snapshot = session.staged_telemetry_snapshot()
    assert snapshot["telemetry_schema_version"] == 2
    assert snapshot["completed_sequence_ids"] == [7]
    assert snapshot["completed_subsegment_keys"] == expected_payloads
    assert snapshot["websocket_sent_sequence_ids"] == [7]
    assert snapshot["websocket_sent_subsegment_keys"] == expected_payloads
    assert [
        (
            event["parent_sequence_id"],
            event["subsequence_id"],
            event["subsequence_count"],
        )
        for event in snapshot["websocket_send_events"]
    ] == expected_keys


@pytest.mark.asyncio
async def test_new_staged_generation_resets_subsegment_send_evidence(
    mock_websocket,
):
    first = FakeStagedPipeline(subsegmentation_enabled=True)
    second = FakeStagedPipeline(subsegmentation_enabled=True)
    pipelines = iter((first, second))
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: next(pipelines),
    )

    await session.start_stream("es-US")
    await session.finish_input()
    await first.outputs.put(composite_audio(0, 0, 2, b"first"))
    await first.outputs.put(composite_audio(0, 1, 2, b"second"))
    await first.outputs.put(
        SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
    )
    await _wait_until(lambda: session.status is SessionStatus.COMPLETED)
    assert session.staged_telemetry_snapshot()[
        "websocket_sent_subsegment_keys"
    ] == [
        {
            "parent_sequence_id": 0,
            "subsequence_id": 0,
            "subsequence_count": 2,
        },
        {
            "parent_sequence_id": 0,
            "subsequence_id": 1,
            "subsequence_count": 2,
        },
    ]

    await session.start_stream("es-US")

    assert session.status is SessionStatus.LISTENING
    assert session._staged_audio_sequence_ids_sent == []
    assert session._staged_audio_subsegment_keys_sent == []
    active_snapshot = session.staged_telemetry_snapshot()
    assert active_snapshot["completed_sequence_ids"] == []
    assert active_snapshot["completed_subsegment_keys"] == []
    assert active_snapshot["websocket_sent_sequence_ids"] == []
    assert active_snapshot["websocket_sent_subsegment_keys"] == []
    assert active_snapshot["websocket_send_events"] == []
    await session.stop_stream()


@pytest.mark.asyncio
async def test_subsegment_summary_mismatch_replaces_completion_with_error(
    mock_websocket,
):
    class MismatchedSubsegmentPipeline(FakeStagedPipeline):
        def summary(self, include_events=False):
            result = super().summary(include_events=include_events)
            result["completed_subsegment_keys"] = [
                {
                    "parent_sequence_id": 0,
                    "subsequence_id": 1,
                    "subsequence_count": 2,
                }
            ]
            return result

    pipeline = MismatchedSubsegmentPipeline(
        subsegmentation_enabled=True
    )
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.finish_input()
    await pipeline.outputs.put(composite_audio(0, 0, 1, b"only child"))
    await pipeline.outputs.put(
        SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
    )
    await _wait_until(lambda: session.status is SessionStatus.ERROR)

    assert mock_websocket.send_json.await_args_list[-1].args[0] == {
        "type": "error",
        "message": (
            "Staged cleanup failed: WebSocket-sent subsegment keys "
            "did not match dequeued pipeline output"
        ),
    }
    assert not any(
        call.args[0].get("status") == "completed"
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "status"
    )
    snapshot = session.staged_telemetry_snapshot()
    assert snapshot["websocket_sent_subsegment_keys"] == [
        {
            "parent_sequence_id": 0,
            "subsequence_id": 0,
            "subsequence_count": 1,
        }
    ]
    assert snapshot["completed_subsegment_keys"] != snapshot[
        "websocket_sent_subsegment_keys"
    ]


@pytest.mark.asyncio
async def test_audio_send_failure_emits_one_error_and_never_completed(
    mock_websocket,
):
    pipeline = FakeStagedPipeline()
    mock_websocket.send_bytes.side_effect = RuntimeError("socket write failed")
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.finish_input()
    await pipeline.outputs.put(
        SimpleNamespace(
            kind=StagedOutputEventKind.AUDIO,
            segment=SimpleNamespace(audio=b"unsent", sequence_id=0),
        )
    )
    await pipeline.outputs.put(
        SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
    )
    await _wait_until(lambda: session.status is SessionStatus.ERROR)
    await _wait_until(lambda: pipeline.closed)

    error_messages = [
        call.args[0]
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "error"
    ]
    assert error_messages == [
        {
            "type": "error",
            "message": "translated audio could not be sent to the client",
        }
    ]
    assert session._staged_audio_sequence_ids_sent == []
    assert not any(
        call.args[0].get("status") == "completed"
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "status"
    )


@pytest.mark.asyncio
async def test_worker_failure_racing_audio_input_keeps_structured_relay_terminal(
    mock_websocket,
):
    pipeline = WorkerFailureInputRacePipeline(
        fail_operation="add_audio",
        queued_audio=b"translated before failure",
    )
    outbound = []

    async def record_json(payload):
        outbound.append(("json", payload))

    async def record_audio(payload):
        outbound.append(("audio", payload))

    mock_websocket.send_json.side_effect = record_json
    mock_websocket.send_bytes.side_effect = record_audio
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.process_audio(b"source pcm")
    # Simulate an endpoint-level fallback arriving after the input call.  The
    # generation latch must not add another terminal error.
    await session.send_runtime_error("generic websocket error")

    errors = [
        payload
        for kind, payload in outbound
        if kind == "json" and payload.get("type") == "error"
    ]
    assert errors == [
        {
            "type": "error",
            "message": "Staged nmt failed: translation failed",
        }
    ]
    assert outbound.index(("audio", b"translated before failure")) < outbound.index(
        ("json", errors[0])
    )
    assert session.status is SessionStatus.ERROR
    assert pipeline.closed
    assert pipeline.close_calls == 1
    assert session._staged_output_task is None


@pytest.mark.asyncio
async def test_worker_failure_racing_end_input_keeps_structured_relay_terminal(
    mock_websocket,
):
    pipeline = WorkerFailureInputRacePipeline(fail_operation="finish_input")
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.finish_input()

    errors = [
        call.args[0]
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "error"
    ]
    assert errors == [
        {
            "type": "error",
            "message": "Staged nmt failed: translation failed",
        }
    ]
    assert not any(
        call.args[0].get("status") in {"processing", "completed"}
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "status"
    )
    assert session.status is SessionStatus.ERROR
    assert pipeline.closed
    assert pipeline.close_calls == 1
    assert session._staged_output_task is None


@pytest.mark.asyncio
async def test_duplicate_end_input_is_idempotent(mock_websocket):
    pipeline = FakeStagedPipeline()
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.finish_input()
    await session.finish_input()

    assert pipeline.finish_calls == 1
    processing_messages = [
        call.args[0]
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("status") == "processing"
    ]
    assert len(processing_messages) == 1
    await session.aclose()


@pytest.mark.asyncio
async def test_cleanup_failure_replaces_completed_with_error(mock_websocket):
    class CleanupFailedPipeline(FakeStagedPipeline):
        def summary(self):
            return {
                "cleanup_errors": [
                    {"stage": "tts", "error": "TTS worker did not stop"}
                ]
            }

    pipeline = CleanupFailedPipeline()
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.finish_input()
    await pipeline.outputs.put(
        SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
    )
    await _wait_until(lambda: session.status is SessionStatus.ERROR)

    assert mock_websocket.send_json.await_args_list[-1].args[0] == {
        "type": "error",
        "message": "Staged cleanup failed: TTS worker did not stop",
    }
    assert not any(
        call.args[0].get("status") == "completed"
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "status"
    )


@pytest.mark.asyncio
async def test_incomplete_sequence_ids_replace_completed_with_error(mock_websocket):
    class IncompletePipeline(FakeStagedPipeline):
        def summary(self, include_events=False):
            result = super().summary(include_events=include_events)
            result["incomplete_sequence_ids"] = [7]
            return result

    pipeline = IncompletePipeline()
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.finish_input()
    await pipeline.outputs.put(
        SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
    )
    await _wait_until(lambda: session.status is SessionStatus.ERROR)

    assert mock_websocket.send_json.await_args_list[-1].args[0] == {
        "type": "error",
        "message": "Staged cleanup failed: incomplete sequence IDs: [7]",
    }


@pytest.mark.asyncio
async def test_staged_start_failure_is_cleaned_up_and_reported(mock_websocket):
    pipeline = FakeStagedPipeline(start_error=RuntimeError("ASR unavailable"))
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")

    assert pipeline.closed
    assert session.status is SessionStatus.ERROR
    assert session._staged_pipeline is None
    assert session._staged_output_task is None
    assert mock_websocket.send_json.await_args_list[-1].args[0] == {
        "type": "error",
        "message": "Failed to start stream: ASR unavailable",
    }
    assert not any(
        call.args[0].get("status") == "listening"
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "status"
    )


@pytest.mark.asyncio
async def test_stop_stream_cancels_staged_work_before_stopped_status(mock_websocket):
    pipeline = FakeStagedPipeline()
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.stop_stream()

    assert pipeline.closed
    assert session._staged_pipeline is None
    assert session._staged_output_task is None
    assert session.status is SessionStatus.STOPPED
    assert mock_websocket.send_json.await_args_list[-1].args[0] == {
        "type": "status",
        "status": "stopped",
        "message": "Stream stopped",
    }


@pytest.mark.asyncio
async def test_backpressured_audio_send_does_not_block_stop_lifecycle(
    mock_websocket,
):
    pipeline = FakeStagedPipeline()
    send_started = asyncio.Event()
    never_release = asyncio.Event()

    async def backpressured_send(_audio):
        send_started.set()
        await never_release.wait()

    mock_websocket.send_bytes.side_effect = backpressured_send
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await pipeline.outputs.put(
        SimpleNamespace(
            kind=StagedOutputEventKind.AUDIO,
            segment=SimpleNamespace(audio=b"blocked", sequence_id=0),
        )
    )
    await asyncio.wait_for(send_started.wait(), timeout=1)

    await asyncio.wait_for(session.stop_stream(), timeout=1)

    assert pipeline.closed
    assert session.status is SessionStatus.STOPPED
    assert session._staged_output_task is None
    assert session._staged_audio_sequence_ids_sent == []


@pytest.mark.asyncio
async def test_shutdown_shields_yielding_pipeline_cleanup_from_relay_cancel(
    mock_websocket,
):
    pipeline = YieldingCleanupPipeline()
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.finish_input()
    await pipeline.outputs.put(
        SimpleNamespace(kind=StagedOutputEventKind.COMPLETE)
    )
    await asyncio.wait_for(pipeline.close_started.wait(), timeout=1)

    stop = asyncio.create_task(session.stop_stream())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not stop.done()
    assert not pipeline.close_cancelled

    pipeline.release_close.set()
    await asyncio.wait_for(stop, timeout=1)

    assert pipeline.close_finished.is_set()
    assert not pipeline.close_cancelled
    assert pipeline.close_calls == 1
    assert session.status is SessionStatus.STOPPED


@pytest.mark.asyncio
async def test_staged_telemetry_remains_exportable_while_cleanup_yields(
    mock_websocket,
):
    pipeline = YieldingCleanupPipeline()
    manager = SessionManager(
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )
    session = await manager.create_session(mock_websocket)
    await session.start_stream("es-US")

    stop = asyncio.create_task(session.stop_stream())
    await asyncio.wait_for(pipeline.close_started.wait(), timeout=1)

    in_progress = manager.get_staged_telemetry()
    assert isinstance(in_progress, dict)
    assert in_progress["state"] == "running"
    assert manager.clear_staged_telemetry() is False

    pipeline.release_close.set()
    await asyncio.wait_for(stop, timeout=1)

    finalized = manager.get_staged_telemetry()
    assert finalized["state"] == "closed"
    assert manager.clear_staged_telemetry() is True
    assert manager.get_staged_telemetry() is None
    await manager.remove_session(session)


@pytest.mark.asyncio
async def test_protocol_error_claims_one_terminal_and_closes_active_pipeline(
    mock_websocket,
):
    pipeline = FakeStagedPipeline()
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: pipeline,
    )

    await session.start_stream("es-US")
    await session.send_protocol_error("Unknown message type: invalid")
    await session.send_protocol_error("duplicate invalid control")

    assert pipeline.closed
    assert session._staged_pipeline is None
    assert session._staged_output_task is None
    assert session.status is SessionStatus.ERROR
    errors = [
        call.args[0]
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "error"
    ]
    assert errors == [
        {
            "type": "error",
            "message": "Unknown message type: invalid",
        }
    ]
    mock_websocket.send_bytes.assert_not_awaited()
    assert not any(
        call.args[0].get("status") == "completed"
        for call in mock_websocket.send_json.await_args_list
        if call.args[0].get("type") == "status"
    )


@pytest.mark.asyncio
async def test_repeated_start_cancels_old_generation_without_late_terminal(
    mock_websocket,
):
    first = FakeStagedPipeline()
    second = FakeStagedPipeline()
    pipelines = iter((first, second))
    factory = MagicMock(side_effect=lambda target: next(pipelines))
    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="staged",
        staged_pipeline_factory=factory,
    )

    await session.start_stream("es-US")
    await session.finish_input()
    await session.start_stream("es-US")
    await first.outputs.put(
        SimpleNamespace(
            kind=StagedOutputEventKind.ERROR,
            stage="asr",
            error="late old failure",
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert first.closed
    assert second.started
    assert session.status is SessionStatus.LISTENING
    assert not any(
        call.args[0].get("type") == "error"
        and "late old failure" in call.args[0].get("message", "")
        for call in mock_websocket.send_json.await_args_list
    )
    await session.aclose()


@pytest.mark.asyncio
async def test_session_manager_uses_staged_flag_without_monolithic_connection(
    mock_websocket,
):
    manager = SessionManager(
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: FakeStagedPipeline(),
    )

    with patch("websocket_handler.riva_client") as monolithic_client:
        session = await manager.create_session(mock_websocket)

    assert session is not None
    assert session.uses_staged_pipeline
    monolithic_client.is_connected.assert_not_called()
    monolithic_client.connect.assert_not_called()
    await manager.remove_session(session)


@pytest.mark.asyncio
async def test_session_manager_keeps_monolithic_path_as_default(mock_websocket):
    manager = SessionManager(pipeline_mode="monolithic")

    with patch("websocket_handler.riva_client") as monolithic_client:
        monolithic_client.is_connected.return_value = False
        monolithic_client.connect.return_value = True
        session = await manager.create_session(mock_websocket)

    assert session is not None
    assert not session.uses_staged_pipeline
    monolithic_client.connect.assert_called_once_with()
    await manager.remove_session(session)


@pytest.mark.asyncio
async def test_environment_default_monolithic_never_uses_staged_factory(
    mock_websocket,
):
    factory = MagicMock()
    manager = SessionManager(staged_pipeline_factory=factory)

    with (
        patch.object(
            staged_pipeline_config,
            "pipeline_mode",
            "monolithic",
        ),
        patch("websocket_handler.riva_client") as monolithic_client,
    ):
        monolithic_client.is_connected.return_value = True
        session = await manager.create_session(mock_websocket)

    assert session is not None
    assert not session.uses_staged_pipeline
    factory.assert_not_called()
    await manager.remove_session(session)


@pytest.mark.asyncio
async def test_replaced_monolithic_stream_drops_stale_audio_and_error(
    mock_websocket,
):
    callbacks = []

    async def translate_stream(**kwargs):
        callbacks.append(kwargs)
        return MagicMock()

    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="monolithic",
    )
    with patch(
        "websocket_handler.riva_client.translate_stream",
        side_effect=translate_stream,
    ):
        await session.start_stream("es-US")
        # Queue old PCM behind the writer lock, then replace the stream before
        # that coroutine can re-check its generation.
        await session._send_lock.acquire()
        callbacks[0]["on_audio"](b"stale audio")
        await asyncio.sleep(0)
        replacement = asyncio.create_task(session.start_stream("es-US"))
        await asyncio.sleep(0)
        session._send_lock.release()
        await replacement

    mock_websocket.send_bytes.reset_mock()
    mock_websocket.send_json.reset_mock()
    callbacks[0]["on_error"]("stale error")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    mock_websocket.send_bytes.assert_not_awaited()
    assert not any(
        call.args
        and call.args[0].get("type") == "error"
        for call in mock_websocket.send_json.await_args_list
    )
    assert session.status is SessionStatus.LISTENING

    callbacks[1]["on_audio"](b"current audio")
    await _wait_until(lambda: mock_websocket.send_bytes.await_count == 1)
    mock_websocket.send_bytes.assert_awaited_once_with(b"current audio")
    await session.aclose()


@pytest.mark.asyncio
async def test_replaced_monolithic_completion_cannot_complete_new_stream(
    mock_websocket,
):
    callbacks = []

    async def translate_stream(**kwargs):
        callbacks.append(kwargs)
        return MagicMock()

    session = TranslationSession(
        websocket=mock_websocket,
        pipeline_mode="monolithic",
    )
    with patch(
        "websocket_handler.riva_client.translate_stream",
        side_effect=translate_stream,
    ):
        await session.start_stream("es-US")
        await session.start_stream("es-US")
        await session.finish_input()

    mock_websocket.send_json.reset_mock()
    callbacks[0]["on_complete"]()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert session.status is SessionStatus.PROCESSING
    assert session._awaiting_completion is True
    assert not any(
        call.args
        and call.args[0].get("status") == SessionStatus.COMPLETED.value
        for call in mock_websocket.send_json.await_args_list
    )

    callbacks[1]["on_complete"]()
    await _wait_until(lambda: session.status is SessionStatus.COMPLETED)
    assert session._awaiting_completion is False
    mock_websocket.send_json.assert_awaited_once_with(
        {
            "type": "status",
            "status": SessionStatus.COMPLETED.value,
            "message": "Riva translated-audio stream complete",
        }
    )
    await session.aclose()


@pytest.mark.asyncio
async def test_manager_replacement_awaits_old_staged_cleanup(mock_websocket):
    manager = SessionManager(
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: FakeStagedPipeline(),
    )
    first_session = await manager.create_session(mock_websocket)
    await first_session.start_stream("es-US")
    first_pipeline = first_session._staged_pipeline

    replacement_websocket = AsyncMock()
    replacement_websocket.client_state = WebSocketState.CONNECTED
    replacement_websocket.application_state = WebSocketState.CONNECTED
    second_session = await manager.create_session(replacement_websocket)

    assert first_pipeline.closed
    assert manager.get_active_session() is second_session
    await manager.remove_session(second_session)


@pytest.mark.asyncio
async def test_manager_displaced_closed_session_cannot_restart_owned_pipeline(
    mock_websocket,
):
    created = []

    def factory(_target):
        pipeline = FakeStagedPipeline()
        created.append(pipeline)
        return pipeline

    manager = SessionManager(
        pipeline_mode="staged",
        staged_pipeline_factory=factory,
    )
    first_session = await manager.create_session(mock_websocket)
    await first_session.start_stream("es-US")

    replacement_websocket = AsyncMock()
    replacement_websocket.client_state = WebSocketState.CONNECTED
    replacement_websocket.application_state = WebSocketState.CONNECTED
    second_session = await manager.create_session(replacement_websocket)

    assert first_session._closed
    await first_session.start_stream("es-US")

    assert len(created) == 1
    assert first_session._staged_pipeline is None
    assert manager.get_active_session() is second_session
    await manager.remove_session(second_session)


@pytest.mark.asyncio
async def test_manager_telemetry_clear_rejects_active_staged_stream(
    mock_websocket,
):
    manager = SessionManager(
        pipeline_mode="staged",
        staged_pipeline_factory=lambda target: FakeStagedPipeline(),
    )
    session = await manager.create_session(mock_websocket)
    await session.start_stream("es-US")

    assert not manager.clear_staged_telemetry()

    await session.stop_stream()
    assert manager.get_staged_telemetry() is not None
    assert manager.clear_staged_telemetry()
    assert manager.get_staged_telemetry() is None
    await manager.remove_session(session)


def test_session_rejects_unknown_pipeline_mode(mock_websocket):
    with pytest.raises(ValueError, match="pipeline_mode"):
        TranslationSession(websocket=mock_websocket, pipeline_mode="unknown")
