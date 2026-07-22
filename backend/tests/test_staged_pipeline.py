import asyncio
import threading
import time

import pytest

from config import StagedPipelineConfig
from staged_models import (
    ASRStreamEvent,
    ASRStreamEventKind,
    ASRTranscript,
    AsrFinal,
    EmissionReason,
    StagedOutputEvent,
    StagedOutputEventKind,
    SynthesizedSegment,
    TextSegment,
    TranslatedSegment,
)
from staged_pipeline import (
    StagedPipelineError,
    StagedPipelineSession,
    StagedPipelineState,
)


def now_ms():
    return time.monotonic_ns() / 1_000_000


def final(final_id, text):
    return ASRStreamEvent(
        kind=ASRStreamEventKind.FINAL,
        final=AsrFinal(
            final_id=final_id,
            text=text,
            received_monotonic_ms=now_ms(),
            source_start_ms=final_id * 1_000,
            source_end_ms=(final_id + 1) * 1_000,
        ),
    )


COMPLETE = ASRStreamEvent(kind=ASRStreamEventKind.COMPLETE)


class FakeASRStream:
    def __init__(self, events=()):
        self.events = asyncio.Queue()
        for event in events:
            self.events.put_nowait(event)
        self.chunks = []
        self.finished = False
        self.closed = False

    def add_chunk(self, chunk):
        self.chunks.append(chunk)

    def finish_input(self):
        self.finished = True

    async def next_event(self, timeout_s=None):
        get = self.events.get()
        return await (get if timeout_s is None else asyncio.wait_for(get, timeout_s))

    async def aclose(self, timeout_s=10):
        del timeout_s
        self.closed = True


class FakeASRClient:
    def __init__(self, events=()):
        self.stream = FakeASRStream(events)
        self.connected = True
        self.open_sizes = []

    def is_connected(self):
        return self.connected

    def connect(self):
        self.connected = True
        return True

    async def open_stream(self, event_queue_maxsize):
        self.open_sizes.append(event_queue_maxsize)
        return self.stream

    async def aclose(self, timeout_s=10):
        del timeout_s
        self.connected = False
        await self.stream.aclose()


class FakeNMTClient:
    def __init__(self, delay_s=0, fail_sequence=None):
        self.connected = True
        self.delay_s = delay_s
        self.fail_sequence = fail_sequence
        self.sequences = []
        self.starts = {}
        self.ends = {}
        self.disconnect_count = 0
        self.lock = threading.Lock()

    def is_connected(self):
        return self.connected

    def connect(self):
        self.connected = True
        return True

    def disconnect(self):
        self.disconnect_count += 1
        self.connected = False

    def translate_segment(self, segment, target_language):
        self.starts[segment.sequence_id] = time.monotonic()
        if self.delay_s:
            time.sleep(self.delay_s)
        if segment.sequence_id == self.fail_sequence:
            raise RuntimeError("synthetic NMT failure")
        with self.lock:
            self.sequences.append(segment.sequence_id)
        self.ends[segment.sequence_id] = time.monotonic()
        completed = now_ms()
        return TranslatedSegment(
            segment=segment,
            text=f"ES: {segment.text}",
            language=target_language,
            started_monotonic_ms=completed - self.delay_s * 1_000,
            completed_monotonic_ms=completed,
        )


class FakeTTSClient:
    def __init__(self, delay_s=0, fail_sequence=None):
        self.connected = True
        self.delay_s = delay_s
        self.fail_sequence = fail_sequence
        self.sequences = []
        self.starts = {}
        self.ends = {}
        self.disconnect_count = 0

    def is_connected(self):
        return self.connected

    def connect(self):
        self.connected = True
        return True

    def disconnect(self):
        self.disconnect_count += 1
        self.connected = False

    def synthesize(self, translation):
        sequence_id = translation.sequence_id
        self.starts[sequence_id] = time.monotonic()
        if self.delay_s:
            time.sleep(self.delay_s)
        if sequence_id == self.fail_sequence:
            raise RuntimeError("synthetic TTS failure")
        self.sequences.append(sequence_id)
        self.ends[sequence_id] = time.monotonic()
        completed = now_ms()
        audio = bytes([sequence_id + 1, 0]) * 160
        return SynthesizedSegment(
            translation=translation,
            audio=audio,
            sample_rate_hz=16_000,
            channels=1,
            bytes_per_sample=2,
            started_monotonic_ms=completed - self.delay_s * 1_000,
            first_audio_monotonic_ms=completed - self.delay_s * 500,
            completed_monotonic_ms=completed,
        )


class BlockingNMTClient(FakeNMTClient):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def translate_segment(self, segment, target_language):
        del segment, target_language
        self.started.set()
        self.cancelled.wait(timeout=2)
        raise RuntimeError("blocking NMT cancelled")

    def disconnect(self):
        super().disconnect()
        self.cancelled.set()


class RaisingDisconnectNMT(FakeNMTClient):
    def disconnect(self):
        self.disconnect_count += 1
        raise RuntimeError("disconnect failed")


def config(**overrides):
    values = {
        "pipeline_mode": "staged",
        "segment_max_chars": 240,
        "segment_max_age_ms": 2_000,
        "asr_event_queue_maxsize": 7,
        "nmt_queue_maxsize": 2,
        "tts_queue_maxsize": 2,
        "output_queue_maxsize": 2,
        "nmt_rpc_timeout_s": 2,
        "tts_rpc_timeout_s": 2,
        "close_timeout_s": 1,
    }
    values.update(overrides)
    return StagedPipelineConfig(**values)


async def drain(session):
    outputs = []
    while True:
        event = await session.next_output(timeout_s=3)
        outputs.append(event)
        if event.kind is not StagedOutputEventKind.AUDIO:
            return outputs


@pytest.mark.asyncio
async def test_ordered_natural_drain_flushes_residual_and_overlaps_nmt_tts():
    asr = FakeASRClient(
        [final(0, "First. Second"), final(1, "sentence!"), final(2, "Tail"), COMPLETE]
    )
    nmt = FakeNMTClient(delay_s=0.04)
    tts = FakeTTSClient(delay_s=0.06)
    session = StagedPipelineSession(
        asr_client=asr,
        nmt_client=nmt,
        tts_client=tts,
        config=config(),
        session_id="ordered",
    )

    await session.start()
    session.add_audio(b"pcm")
    session.finish_input()
    outputs = await drain(session)
    await session.wait_for_workers(timeout_s=1)

    audio = [event.segment for event in outputs[:-1]]
    assert [segment.sequence_id for segment in audio] == [0, 1, 2]
    assert [segment.translation.segment.text for segment in audio] == [
        "First.",
        "Second sentence!",
        "Tail",
    ]
    assert outputs[-1].kind is StagedOutputEventKind.COMPLETE
    assert nmt.sequences == [0, 1, 2]
    assert tts.sequences == [0, 1, 2]
    assert nmt.starts[1] < tts.ends[0]
    assert asr.open_sizes == [7]
    assert session.state is StagedPipelineState.COMPLETE
    assert session.summary()["audio_segments_produced"] == 3
    assert session.summary()["completed_sequence_ids"] == [0, 1, 2]
    assert session.summary()["incomplete_sequence_ids"] == []
    first_audio_events = [
        event
        for event in session.telemetry
        if event.stage == "tts" and event.event == "first_audio"
    ]
    assert [event.monotonic_ms for event in first_audio_events] == [
        segment.first_audio_monotonic_ms for segment in audio
    ]
    await session.aclose()


@pytest.mark.asyncio
async def test_filler_discard_preserves_pipeline_order_and_summary_evidence():
    nmt = FakeNMTClient()
    session = StagedPipelineSession(
        asr_client=FakeASRClient(
            [final(0, "It moves from. uh. from something..."), COMPLETE]
        ),
        nmt_client=nmt,
        tts_client=FakeTTSClient(),
        config=config(),
        session_id="filler-order",
    )

    await session.start()
    session.finish_input()
    outputs = await drain(session)

    audio = [event.segment for event in outputs[:-1]]
    assert [segment.sequence_id for segment in audio] == [0, 1]
    assert [segment.translation.segment.text for segment in audio] == [
        "It moves from.",
        "from something...",
    ]
    assert nmt.sequences == [0, 1]
    segmenter_events = [
        event for event in session.telemetry if event.stage == "segmenter"
    ]
    assert [(event.event, event.sequence_id) for event in segmenter_events] == [
        ("emitted", 0),
        ("filler_discarded", None),
        ("emitted", 1),
    ]
    discard = segmenter_events[1]
    assert discard.text_chars == len("uh.")
    assert discard.asr_final_id == 0
    assert discard.contributing_final_ids == (0,)
    assert "text" not in discard.to_dict()
    summary = session.summary()
    assert summary["fillers_discarded"] == 1
    assert summary["segments_emitted"] == 2
    assert summary["completed_sequence_ids"] == [0, 1]
    assert summary["incomplete_sequence_ids"] == []
    await session.aclose()


@pytest.mark.asyncio
async def test_filler_only_session_completes_without_calling_nmt_or_tts():
    nmt = FakeNMTClient()
    tts = FakeTTSClient()
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, '“Hmm.”'), COMPLETE]),
        nmt_client=nmt,
        tts_client=tts,
        config=config(),
        session_id="filler-only",
    )

    await session.start()
    session.finish_input()
    outputs = await drain(session)

    assert [event.kind for event in outputs] == [StagedOutputEventKind.COMPLETE]
    assert nmt.sequences == []
    assert tts.sequences == []
    summary = session.summary()
    assert summary["fillers_discarded"] == 1
    assert summary["segments_emitted"] == 0
    assert summary["audio_segments_produced"] == 0
    assert summary["completed_sequence_ids"] == []
    assert summary["incomplete_sequence_ids"] == []
    await session.aclose()


@pytest.mark.asyncio
async def test_bounded_output_backpressure_preserves_every_segment():
    events = [final(index, f"Sentence {index}.") for index in range(8)] + [COMPLETE]
    session = StagedPipelineSession(
        asr_client=FakeASRClient(events),
        nmt_client=FakeNMTClient(),
        tts_client=FakeTTSClient(),
        config=config(
            nmt_queue_maxsize=1,
            tts_queue_maxsize=1,
            output_queue_maxsize=1,
        ),
        session_id="bounded",
    )

    await session.start()
    session.finish_input()
    await asyncio.sleep(0.1)
    assert session._output_queue.qsize() == 1

    outputs = await drain(session)
    assert [event.segment.sequence_id for event in outputs[:-1]] == list(range(8))
    summary = session.summary()
    assert summary["max_queue_depths"] == {"nmt": 1, "tts": 1, "output": 1}
    assert summary["blocked_put_counts"]["output"] >= 1
    assert all(
        event.queue_depth is None
        or event.queue_capacity is None
        or event.queue_depth <= event.queue_capacity
        for event in session.telemetry
    )
    await session.aclose()


@pytest.mark.parametrize("failing_stage", ["asr", "nmt", "tts"])
@pytest.mark.asyncio
async def test_first_stage_failure_emits_one_error_and_no_completion(failing_stage):
    if failing_stage == "asr":
        events = [
            ASRStreamEvent(
                kind=ASRStreamEventKind.ERROR, error="synthetic ASR failure"
            )
        ]
    else:
        events = [final(0, "Failure case."), COMPLETE]
    session = StagedPipelineSession(
        asr_client=FakeASRClient(events),
        nmt_client=FakeNMTClient(fail_sequence=0 if failing_stage == "nmt" else None),
        tts_client=FakeTTSClient(fail_sequence=0 if failing_stage == "tts" else None),
        config=config(),
        session_id=f"failure-{failing_stage}",
    )

    await session.start()
    session.finish_input()
    output = await session.next_output(timeout_s=2)

    assert output.kind is StagedOutputEventKind.ERROR
    assert output.stage == failing_stage
    assert session.state is StagedPipelineState.FAILED
    assert session.failure[0] == failing_stage
    assert [event.event for event in session.telemetry].count("error") == 2
    with pytest.raises(StagedPipelineError, match="already consumed"):
        await session.next_output()
    await session.aclose()


@pytest.mark.asyncio
async def test_unpunctuated_final_emits_on_age_without_another_asr_event():
    asr = FakeASRClient([final(0, "Aged residual")])
    session = StagedPipelineSession(
        asr_client=asr,
        nmt_client=FakeNMTClient(),
        tts_client=FakeTTSClient(),
        config=config(segment_max_age_ms=20),
        session_id="age",
    )

    await session.start()
    session.finish_input()
    for _ in range(100):
        if any(event.event == "completed" and event.stage == "nmt" for event in session.telemetry):
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail("aged segment never reached NMT")

    await asr.stream.events.put(COMPLETE)
    outputs = await drain(session)
    assert outputs[0].segment.translation.segment.text == "Aged residual"
    assert outputs[0].segment.translation.segment.reason.value == "age"
    assert outputs[-1].kind is StagedOutputEventKind.COMPLETE
    await session.aclose()


@pytest.mark.asyncio
async def test_asr_consumer_normalizes_queued_final_capture_time():
    captured_ms = 180_110_061.943155
    observed_ms = 180_110_062.075487
    events = [
        ASRStreamEvent(
            kind=ASRStreamEventKind.INTERIM,
            transcript=ASRTranscript(
                text="long interim hypothesis",
                is_final=False,
                received_monotonic_ms=180_109_747.916788,
                source_end_ms=68_960.0601196289,
            ),
        ),
        ASRStreamEvent(
            kind=ASRStreamEventKind.FINAL,
            final=AsrFinal(
                final_id=17,
                text="A queued final.",
                received_monotonic_ms=captured_ms,
                source_start_ms=60_800,
                source_end_ms=68_080,
            ),
        ),
        COMPLETE,
    ]
    session = StagedPipelineSession(
        asr_client=FakeASRClient(events),
        nmt_client=FakeNMTClient(),
        tts_client=FakeTTSClient(),
        config=config(),
        session_id="queued-final-clock-normalization",
        clock_ms=lambda: observed_ms,
    )

    await session.start()
    session.finish_input()
    outputs = await drain(session)

    assert outputs[-1].kind is StagedOutputEventKind.COMPLETE
    source = outputs[0].segment.translation.segment
    assert source.buffered_since_monotonic_ms == captured_ms
    assert source.emitted_monotonic_ms == observed_ms
    assert source.source_start_ms == 60_800
    assert source.source_end_ms == 68_080
    assert session.summary()["failure"] is None
    await session.aclose()


@pytest.mark.asyncio
async def test_lifecycle_guards_and_idempotent_close():
    asr = FakeASRClient([COMPLETE])
    session = StagedPipelineSession(
        asr_client=asr,
        nmt_client=FakeNMTClient(),
        tts_client=FakeTTSClient(),
        config=config(),
        session_id="lifecycle",
    )

    with pytest.raises(StagedPipelineError, match="has not started"):
        await session.next_output()
    await session.start()
    with pytest.raises(StagedPipelineError, match="started once"):
        await session.start()
    with pytest.raises(ValueError, match="non-empty"):
        session.add_audio(b"")
    session.finish_input()
    session.finish_input()
    assert (await session.next_output(timeout_s=1)).kind is StagedOutputEventKind.COMPLETE
    await session.aclose()
    await session.aclose()
    assert session.state is StagedPipelineState.CLOSED
    assert asr.stream.closed is True


@pytest.mark.asyncio
async def test_owned_clients_connect_and_disconnect_exactly_once():
    asr = FakeASRClient([COMPLETE])
    nmt = FakeNMTClient()
    tts = FakeTTSClient()
    asr.connected = nmt.connected = tts.connected = False
    session = StagedPipelineSession(
        asr_client=asr,
        nmt_client=nmt,
        tts_client=tts,
        config=config(),
        session_id="owned",
        owns_clients=True,
    )

    await session.start()
    session.finish_input()
    assert (await session.next_output(timeout_s=1)).kind is StagedOutputEventKind.COMPLETE
    await session.aclose()

    assert asr.connected is False
    assert nmt.disconnect_count == 1
    assert tts.disconnect_count == 1


@pytest.mark.asyncio
async def test_manual_close_cancels_workers_blocked_by_output_backpressure():
    events = [final(index, f"Sentence {index}.") for index in range(5)] + [COMPLETE]
    session = StagedPipelineSession(
        asr_client=FakeASRClient(events),
        nmt_client=FakeNMTClient(),
        tts_client=FakeTTSClient(),
        config=config(output_queue_maxsize=1),
        session_id="cancel",
    )

    await session.start()
    session.finish_input()
    await asyncio.sleep(0.1)
    await asyncio.wait_for(session.aclose(), timeout=1)

    assert session.state is StagedPipelineState.CLOSED
    assert all(task.done() for task in session._tasks)


def test_staged_config_rejects_invalid_mode_and_nonpositive_limits():
    with pytest.raises(ValueError, match="S2S_PIPELINE_MODE"):
        config(pipeline_mode="unknown")
    with pytest.raises(ValueError, match="nmt_queue_maxsize"):
        config(nmt_queue_maxsize=0)
    with pytest.raises(ValueError, match="tts_rpc_timeout_s"):
        config(tts_rpc_timeout_s=0)


@pytest.mark.asyncio
async def test_worker_wait_timeout_is_observational_and_does_not_cancel_pipeline():
    session = StagedPipelineSession(
        asr_client=FakeASRClient(),
        nmt_client=FakeNMTClient(),
        tts_client=FakeTTSClient(),
        config=config(),
        session_id="wait-timeout",
    )
    await session.start()

    with pytest.raises(asyncio.TimeoutError):
        await session.wait_for_workers(timeout_s=0.01)

    assert session.state is StagedPipelineState.RUNNING
    assert all(not task.done() for task in session._tasks)
    await session.aclose()


@pytest.mark.asyncio
async def test_close_wakes_consumer_waiting_for_output():
    session = StagedPipelineSession(
        asr_client=FakeASRClient(),
        nmt_client=FakeNMTClient(),
        tts_client=FakeTTSClient(),
        config=config(),
        session_id="blocked-consumer",
    )
    await session.start()
    waiter = asyncio.create_task(session.next_output())
    await asyncio.sleep(0)

    await session.aclose()

    with pytest.raises(StagedPipelineError, match="closed"):
        await asyncio.wait_for(waiter, timeout=0.2)


@pytest.mark.asyncio
async def test_immediate_close_has_no_unawaited_worker_coroutines(recwarn):
    session = StagedPipelineSession(
        asr_client=FakeASRClient(),
        nmt_client=FakeNMTClient(),
        tts_client=FakeTTSClient(),
        config=config(),
        session_id="immediate-close",
    )
    await session.start()
    await session.aclose()
    await asyncio.sleep(0)

    assert not [
        warning
        for warning in recwarn
        if "was never awaited" in str(warning.message)
    ]


@pytest.mark.asyncio
async def test_close_aborts_running_blocking_rpc_for_supplied_exclusive_client():
    nmt = BlockingNMTClient()
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "Blocked."), COMPLETE]),
        nmt_client=nmt,
        tts_client=FakeTTSClient(),
        config=config(),
        session_id="blocking-rpc",
    )
    await session.start()
    session.finish_input()
    for _ in range(100):
        if nmt.started.is_set():
            break
        await asyncio.sleep(0.01)
    assert nmt.started.is_set()

    await asyncio.wait_for(session.aclose(), timeout=1)

    assert nmt.disconnect_count == 1
    assert nmt.cancelled.is_set()
    assert not session._blocking_futures


@pytest.mark.asyncio
async def test_close_continues_when_one_client_disconnect_raises():
    nmt = RaisingDisconnectNMT()
    session = StagedPipelineSession(
        asr_client=FakeASRClient(),
        nmt_client=nmt,
        tts_client=FakeTTSClient(),
        config=config(),
        session_id="close-error",
    )
    await session.start()

    await session.aclose()

    assert session.state is StagedPipelineState.CLOSED
    assert all(task.done() for task in session._tasks)
    assert any(
        event.stage == "nmt" and event.event == "close_error"
        for event in session.telemetry
    )
    assert session.summary()["outcome"] == "cleanup_failed"
    assert session.summary()["failure"]["stage"] == "cleanup"
    assert session.summary()["cleanup_errors"][0]["stage"] == "nmt"


@pytest.mark.asyncio
async def test_error_terminal_uses_reserved_slot_when_audio_queue_is_full():
    session = StagedPipelineSession(
        asr_client=FakeASRClient(
            [final(0, "First."), final(1, "Second."), COMPLETE]
        ),
        nmt_client=FakeNMTClient(),
        tts_client=FakeTTSClient(fail_sequence=1),
        config=config(output_queue_maxsize=1),
        session_id="reserved-terminal",
    )
    await session.start()
    session.finish_input()

    await session.wait_for_terminal(timeout_s=1)
    first = await session.next_output(timeout_s=1)
    terminal = await session.next_output(timeout_s=1)

    assert first.kind is StagedOutputEventKind.AUDIO
    assert terminal.kind is StagedOutputEventKind.ERROR
    assert terminal.stage == "tts"
    await session.aclose()


@pytest.mark.asyncio
async def test_queue_residence_excludes_time_waiting_for_output_capacity():
    session = StagedPipelineSession(
        asr_client=FakeASRClient(
            [final(0, "First."), final(1, "Second."), COMPLETE]
        ),
        nmt_client=FakeNMTClient(),
        tts_client=FakeTTSClient(),
        config=config(output_queue_maxsize=1),
        session_id="queue-timing",
    )
    await session.start()
    session.finish_input()
    await asyncio.sleep(0.06)
    await session.next_output(timeout_s=1)
    await asyncio.sleep(0.06)
    second = await session.next_output(timeout_s=1)
    assert second.kind is StagedOutputEventKind.AUDIO

    enqueue = next(
        event
        for event in session.telemetry
        if event.stage == "output"
        and event.event == "enqueued"
        and event.sequence_id == 1
    )
    dequeue = next(
        event
        for event in session.telemetry
        if event.stage == "output"
        and event.event == "dequeued"
        and event.sequence_id == 1
    )
    assert enqueue.blocked_put_ms >= 30
    assert 30 <= dequeue.queue_residence_ms < 90
    await session.next_output(timeout_s=1)
    await session.aclose()


@pytest.mark.asyncio
async def test_cancelled_output_waiter_cannot_consume_and_lose_queue_item():
    session = StagedPipelineSession(
        asr_client=FakeASRClient(),
        nmt_client=FakeNMTClient(),
        tts_client=FakeTTSClient(),
        config=config(output_queue_maxsize=1),
        session_id="cancelled-output-waiter",
    )
    await session.start()
    source = TextSegment(
        sequence_id=0,
        text="Source.",
        reason=EmissionReason.PUNCTUATION,
        emitted_monotonic_ms=now_ms(),
        buffered_since_monotonic_ms=now_ms() - 1,
        source_start_ms=0,
        source_end_ms=100,
        contributing_final_ids=(0,),
    )
    translation = TranslatedSegment(
        segment=source,
        text="Fuente.",
        language="es-US",
        started_monotonic_ms=now_ms(),
        completed_monotonic_ms=now_ms(),
    )
    synthesized = SynthesizedSegment(
        translation=translation,
        audio=b"\x00\x00",
        sample_rate_hz=16_000,
        channels=1,
        bytes_per_sample=2,
        started_monotonic_ms=now_ms(),
        first_audio_monotonic_ms=now_ms(),
        completed_monotonic_ms=now_ms(),
    )
    waiter = asyncio.create_task(session.next_output())
    await asyncio.sleep(0)
    await session._enqueue(
        session._output_queue,
        StagedOutputEvent(
            kind=StagedOutputEventKind.AUDIO,
            segment=synthesized,
        ),
        "output",
        "enqueued",
    )
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert session._output_queue.qsize() == 1
    assert session._queue_slots["output"]._value == 0
    recovered = await session.next_output(timeout_s=1)
    assert recovered.segment is synthesized
    assert session._queue_slots["output"]._value == 1
    await session.aclose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"nmt_queue_maxsize": True},
        {"nmt_queue_maxsize": 1.5},
        {"nmt_rpc_timeout_s": float("nan")},
        {"tts_rpc_timeout_s": float("inf")},
        {"close_timeout_s": True},
    ],
)
def test_staged_config_rejects_invalid_runtime_types(overrides):
    with pytest.raises(ValueError):
        config(**overrides)
