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
    def __init__(self, *, start_error=None):
        self.outputs = asyncio.Queue()
        self.start_error = start_error
        self.started = False
        self.closed = False
        self.close_calls = 0
        self.audio_chunks = []
        self.finish_calls = 0
        self.dequeued_audio_sequence_ids = []

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
            self.dequeued_audio_sequence_ids.append(output.segment.sequence_id)
        return output

    async def aclose(self):
        self.close_calls += 1
        self.closed = True

    def summary(self, include_events=False):
        return {
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
