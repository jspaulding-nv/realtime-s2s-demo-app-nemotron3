import asyncio
import json
import threading
import time
from dataclasses import replace

import pytest

from config import StagedPipelineConfig
from direct_nmt_client import DirectNMTRecoveryError
from staged_models import (
    ASRStreamEvent,
    ASRStreamEventKind,
    ASRTranscript,
    AsrFinal,
    EmissionReason,
    StagedOutputEvent,
    StagedOutputEventKind,
    SynthesizedAudioFrame,
    SynthesizedSegment,
    SynthesizedStreamCompletion,
    TTSResponseChunkMetric,
    TextSegment,
    TranslatedSegment,
)
from staged_pipeline import (
    StagedPipelineError,
    StagedPipelineSession,
    StagedPipelineState,
)
from target_text_validation import TargetTextValidationError


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
    def __init__(self, delay_s=0, fail_sequence=None, retry_count=0):
        self.connected = True
        self.delay_s = delay_s
        self.fail_sequence = fail_sequence
        self.retry_count = retry_count
        self.sequences = []
        self.translations = []
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
        translation = TranslatedSegment(
            segment=segment,
            text=f"ES: {segment.text}",
            language=target_language,
            started_monotonic_ms=completed - self.delay_s * 1_000,
            completed_monotonic_ms=completed,
            retry_count=self.retry_count,
        )
        self.translations.append(translation)
        return translation


class ChildIdentityNMT(FakeNMTClient):
    def translate_segment(self, segment, target_language):
        translation = super().translate_segment(segment, target_language)
        return replace(
            translation,
            subsequence_id=0,
            subsequence_count=2,
        )


class FakeTTSClient:
    def __init__(
        self,
        delay_s=0,
        fail_sequence=None,
        retry_count=0,
        *,
        fail_order_key=None,
        retry_order_key=None,
    ):
        self.connected = True
        self.delay_s = delay_s
        self.fail_sequence = fail_sequence
        self.fail_order_key = fail_order_key
        self.retry_count = retry_count
        self.retry_order_key = retry_order_key
        self.sequences = []
        self.order_keys = []
        self.translations = []
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
        self.order_keys.append(translation.order_key)
        self.translations.append(translation)
        self.starts[sequence_id] = time.monotonic()
        if self.delay_s:
            time.sleep(self.delay_s)
        if (
            sequence_id == self.fail_sequence
            or translation.order_key == self.fail_order_key
        ):
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
            retry_count=(
                self.retry_count
                if self.retry_order_key is None
                or translation.order_key == self.retry_order_key
                else 0
            ),
        )


class ResponseMetricTTS(FakeTTSClient):
    def synthesize(self, translation):
        synthesized = super().synthesize(translation)
        first_bytes = len(synthesized.audio) // 2
        second_bytes = len(synthesized.audio) - first_bytes
        return replace(
            synthesized,
            response_chunks=(
                TTSResponseChunkMetric(
                    response_index=0,
                    audio_bytes=first_bytes,
                    cumulative_audio_bytes=first_bytes,
                    received_monotonic_ms=(
                        synthesized.first_audio_monotonic_ms
                    ),
                    retry_count=synthesized.retry_count,
                ),
                TTSResponseChunkMetric(
                    response_index=1,
                    audio_bytes=second_bytes,
                    cumulative_audio_bytes=len(synthesized.audio),
                    received_monotonic_ms=(
                        synthesized.completed_monotonic_ms
                    ),
                    retry_count=synthesized.retry_count,
                ),
            ),
        )


class SyntheticTTSRetryError(RuntimeError):
    retry_count = 1

    def __init__(self, translation):
        self.segment = translation.segment
        super().__init__(
            f"synthetic exhausted TTS retry for sequence {translation.sequence_id}"
        )


class ExhaustedRetryTTS(FakeTTSClient):
    def synthesize(self, translation):
        raise SyntheticTTSRetryError(translation)


class ExhaustedChildRetryTTS(FakeTTSClient):
    def __init__(self, failing_order_key):
        super().__init__()
        self.failing_order_key = failing_order_key

    def synthesize(self, translation):
        if translation.order_key == self.failing_order_key:
            self.order_keys.append(translation.order_key)
            self.translations.append(translation)
            raise SyntheticTTSRetryError(translation)
        return super().synthesize(translation)


class BlockingRetryTTS(FakeTTSClient):
    def __init__(self):
        super().__init__()
        self.active_retry_count = 1
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def synthesize(self, translation):
        del translation
        self.started.set()
        self.cancelled.wait(timeout=2)
        raise RuntimeError("synthetic timed-out retry cancelled")

    def disconnect(self):
        super().disconnect()
        self.cancelled.set()


class BlockingChildRetryTTS(FakeTTSClient):
    def __init__(self, blocking_order_key):
        super().__init__()
        self.blocking_order_key = blocking_order_key
        self.active_retry_count = 1
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def synthesize(self, translation):
        if translation.order_key != self.blocking_order_key:
            return super().synthesize(translation)
        self.order_keys.append(translation.order_key)
        self.translations.append(translation)
        self.started.set()
        self.cancelled.wait(timeout=2)
        raise RuntimeError("synthetic timed-out child retry cancelled")

    def disconnect(self):
        super().disconnect()
        self.cancelled.set()


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


class RecoveryFailingNMT(FakeNMTClient):
    def translate_segment(self, segment, target_language):
        del target_language
        initial = TargetTextValidationError(
            sequence_id=segment.sequence_id,
            language="es-US",
            reason="unsupported_characters",
            unsupported_code_points=("U+3002",),
            unsupported_scripts=("UnsupportedPunctuation",),
        )
        retry = TargetTextValidationError(
            sequence_id=segment.sequence_id,
            language="es-US",
            reason="unsupported_characters",
            unsupported_code_points=("U+0400",),
            unsupported_scripts=("Cyrillic",),
        )
        raise DirectNMTRecoveryError(
            segment=segment,
            initial_error=initial,
            retry_error=retry,
        ) from retry


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
async def test_summary_exports_privacy_safe_final_attribution_diagnostics():
    private_text = "Do not export this transcript."
    timed_event = ASRStreamEvent(
        kind=ASRStreamEventKind.FINAL,
        final=AsrFinal(
            final_id=0,
            text=private_text,
            received_monotonic_ms=now_ms(),
            source_start_ms=100,
            source_end_ms=900,
            audio_processed_s=1.25,
            word_count=5,
            first_word_start_ms=100,
            last_word_end_ms=900,
            timing_basis="word_offsets",
        ),
    )
    session = StagedPipelineSession(
        asr_client=FakeASRClient([timed_event, COMPLETE]),
        nmt_client=FakeNMTClient(),
        tts_client=FakeTTSClient(),
        config=config(),
        session_id="asr-attribution-diagnostic",
    )

    await session.start()
    session.finish_input()
    await drain(session)
    summary = session.summary(include_events=True)

    diagnostic = summary["asr_final_attribution"]
    assert diagnostic["nonempty_final_count"] == 1
    assert diagnostic["final_missing_word_offsets_count"] == 0
    assert diagnostic["all_nonempty_finals_have_word_offsets"] is True
    assert diagnostic["finals"][0]["final_id"] == 0
    assert diagnostic["finals"][0]["text_chars"] == len(private_text)
    assert private_text not in json.dumps(diagnostic)
    assert '"text"' not in json.dumps(diagnostic)
    await session.aclose()


@pytest.mark.asyncio
async def test_default_off_preserves_one_call_and_parent_translation_identity():
    nmt = FakeNMTClient()
    tts = FakeTTSClient()
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "A compact parent."), COMPLETE]),
        nmt_client=nmt,
        tts_client=tts,
        config=config(tts_subsegment_max_chars=0),
        session_id="subsegment-default-off",
    )

    await session.start()
    session.finish_input()
    outputs = await drain(session)

    assert [output.kind for output in outputs] == [
        StagedOutputEventKind.AUDIO,
        StagedOutputEventKind.COMPLETE,
    ]
    assert nmt.sequences == [0]
    assert tts.sequences == [0]
    assert tts.order_keys == [(0, 0, 1)]
    assert tts.translations[0] is nmt.translations[0]
    summary = session.summary(include_events=True)
    assert summary["telemetry_schema_version"] == 1
    assert summary["tts_subsegmentation_enabled"] is False
    assert "tts_response_chunk_telemetry_enabled" not in summary
    assert "tts_response_chunk_telemetry" not in summary
    assert "planned_subsegment_keys" not in summary
    assert all(
        event["subsequence_id"] is None
        and event["subsequence_count"] is None
        for event in summary["events"]
    )
    await session.aclose()


@pytest.mark.asyncio
async def test_opt_in_retains_privacy_safe_tts_response_chunk_sidecar():
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "A compact parent."), COMPLETE]),
        nmt_client=FakeNMTClient(),
        tts_client=ResponseMetricTTS(),
        config=config(tts_response_chunk_telemetry_enabled=True),
        session_id="response-chunk-diagnostic",
    )

    await session.start()
    session.finish_input()
    outputs = await drain(session)

    assert [output.kind for output in outputs] == [
        StagedOutputEventKind.AUDIO,
        StagedOutputEventKind.COMPLETE,
    ]
    summary = session.summary(include_events=True)
    assert summary["telemetry_schema_version"] == 1
    assert summary["tts_response_chunk_telemetry_enabled"] is True
    diagnostic = summary["tts_response_chunk_telemetry"]
    assert diagnostic["schema_version"] == 1
    assert diagnostic["segments_observed"] == 1
    assert diagnostic["response_chunk_count"] == 2
    assert [
        (
            chunk["parent_sequence_id"],
            chunk["subsequence_id"],
            chunk["subsequence_count"],
            chunk["response_index"],
            chunk["response_count"],
            chunk["audio_bytes"],
            chunk["cumulative_audio_bytes"],
        )
        for chunk in diagnostic["chunks"]
    ] == [
        (0, 0, 1, 0, 2, 160, 160),
        (0, 0, 1, 1, 2, 160, 320),
    ]
    assert all(
        chunk["audio_duration_ms"] == pytest.approx(5)
        for chunk in diagnostic["chunks"]
    )
    assert "audio" not in diagnostic["chunks"][0]
    assert "text" not in str(diagnostic)
    await session.aclose()


@pytest.mark.asyncio
async def test_opt_in_fails_closed_when_tts_omits_response_metrics():
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "A compact parent."), COMPLETE]),
        nmt_client=FakeNMTClient(),
        tts_client=FakeTTSClient(),
        config=config(tts_response_chunk_telemetry_enabled=True),
        session_id="response-chunk-diagnostic-missing",
    )

    await session.start()
    session.finish_input()
    terminal = await session.next_output(timeout_s=2)

    assert terminal.kind is StagedOutputEventKind.ERROR
    assert terminal.stage == "tts"
    assert "response-chunk telemetry" in terminal.error
    assert session.summary()["completed_sequence_ids"] == []
    await session.aclose()


@pytest.mark.parametrize("max_chars", [0, 20])
@pytest.mark.asyncio
async def test_nmt_cannot_inject_a_child_identity(max_chars):
    tts = FakeTTSClient()
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "Parent."), COMPLETE]),
        nmt_client=ChildIdentityNMT(),
        tts_client=tts,
        config=config(
            tts_subsegment_max_chars=max_chars,
            tts_subsegment_min_chars=1,
        ),
        session_id=f"nmt-child-identity-{max_chars}",
    )

    await session.start()
    session.finish_input()
    terminal = await session.next_output(timeout_s=2)

    assert terminal.kind is StagedOutputEventKind.ERROR
    assert terminal.stage == "nmt"
    assert tts.order_keys == []
    error = next(
        event
        for event in session.telemetry
        if event.stage == "nmt" and event.event == "error"
    )
    assert error.sequence_id == 0
    assert error.subsequence_id is None
    assert error.subsequence_count is None
    assert session.summary()["completed_sequence_ids"] == []
    await session.aclose()


@pytest.mark.asyncio
async def test_multiple_parents_emit_all_children_in_composite_fifo_order():
    tts = FakeTTSClient()
    session = StagedPipelineSession(
        asr_client=FakeASRClient(
            [
                final(0, "First parent has enough translated words."),
                final(1, "Second parent also has several translated words."),
                COMPLETE,
            ]
        ),
        nmt_client=FakeNMTClient(),
        tts_client=tts,
        config=config(
            tts_subsegment_max_chars=20,
            tts_subsegment_min_chars=1,
        ),
        session_id="multi-parent-subsegments",
    )

    await session.start()
    session.finish_input()
    outputs = await drain(session)

    audio = [output.segment for output in outputs[:-1]]
    expected_keys = [
        (0, 0, 4),
        (0, 1, 4),
        (0, 2, 4),
        (0, 3, 4),
        (1, 0, 4),
        (1, 1, 4),
        (1, 2, 4),
        (1, 3, 4),
    ]
    assert [segment.order_key for segment in audio] == expected_keys
    assert tts.order_keys == expected_keys
    assert [segment.translation.text for segment in audio] == [
        "ES:",
        "First parent has",
        "enough translated",
        "words.",
        "ES:",
        "Second parent also",
        "has several",
        "translated words.",
    ]
    assert outputs[-1].kind is StagedOutputEventKind.COMPLETE

    summary = session.summary(include_events=True)
    expected_payloads = [
        {
            "parent_sequence_id": parent,
            "subsequence_id": child,
            "subsequence_count": count,
        }
        for parent, child, count in expected_keys
    ]
    assert summary["telemetry_schema_version"] == 2
    assert summary["tts_subsegmentation_enabled"] is True
    assert summary["tts_subsegments_planned"] == 8
    assert summary["tts_subsegments_produced"] == 8
    assert summary["planned_subsegment_keys"] == expected_payloads
    assert summary["synthesized_subsegment_keys"] == expected_payloads
    assert summary["completed_subsegment_keys"] == expected_payloads
    assert summary["incomplete_subsegment_keys"] == []
    assert summary["completed_sequence_ids"] == [0, 1]
    assert summary["incomplete_sequence_ids"] == []
    split_events = [
        event
        for event in summary["events"]
        if event["stage"] == "target_splitter"
        and event["event"] == "emitted"
    ]
    assert [
        (
            event["parent_sequence_id"],
            event["subsequence_id"],
            event["subsequence_count"],
        )
        for event in split_events
    ] == expected_keys
    assert all(event["text_chars"] <= 20 for event in split_events)
    assert {
        event["parent_text_chars"]
        for event in split_events
        if event["sequence_id"] == 0
    } == {len("ES: First parent has enough translated words.")}
    assert {
        event["parent_text_chars"]
        for event in split_events
        if event["sequence_id"] == 1
    } == {len("ES: Second parent also has several translated words.")}
    assert all("text" not in event for event in summary["events"])
    await session.aclose()


@pytest.mark.asyncio
async def test_parent_completes_only_when_its_final_child_is_dequeued():
    session = StagedPipelineSession(
        asr_client=FakeASRClient(
            [
                final(
                    0,
                    "This parent has several words and a final clause.",
                ),
                COMPLETE,
            ]
        ),
        nmt_client=FakeNMTClient(),
        tts_client=FakeTTSClient(),
        config=config(
            tts_subsegment_max_chars=20,
            tts_subsegment_min_chars=1,
        ),
        session_id="parent-completes-on-final-child",
    )

    await session.start()
    session.finish_input()

    for subsequence_id in range(4):
        output = await session.next_output(timeout_s=2)
        assert output.kind is StagedOutputEventKind.AUDIO
        assert output.segment.order_key == (0, subsequence_id, 4)
        summary = session.summary()
        assert len(summary["completed_subsegment_keys"]) == subsequence_id + 1
        if subsequence_id < 3:
            assert summary["completed_sequence_ids"] == []
            assert summary["incomplete_sequence_ids"] == [0]
        else:
            assert summary["completed_sequence_ids"] == [0]
            assert summary["incomplete_sequence_ids"] == []

    terminal = await session.next_output(timeout_s=2)
    assert terminal.kind is StagedOutputEventKind.COMPLETE
    await session.aclose()


@pytest.mark.asyncio
async def test_nmt_recovery_retry_is_observable_without_changing_order_or_tts_count():
    nmt = FakeNMTClient(retry_count=1)
    tts = FakeTTSClient()
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "Peace."), COMPLETE]),
        nmt_client=nmt,
        tts_client=tts,
        config=config(),
        session_id="nmt-recovery",
    )

    await session.start()
    session.finish_input()
    outputs = await drain(session)

    assert [output.kind for output in outputs] == [
        StagedOutputEventKind.AUDIO,
        StagedOutputEventKind.COMPLETE,
    ]
    assert nmt.sequences == [0]
    assert tts.sequences == [0]
    nmt_completed = [
        event
        for event in session.telemetry
        if event.stage == "nmt" and event.event == "completed"
    ]
    assert len(nmt_completed) == 1
    assert nmt_completed[0].sequence_id == 0
    assert nmt_completed[0].retry_count == 1
    summary = session.summary(include_events=True)
    assert summary["nmt_retry_count"] == 1
    assert sum(
        event["retry_count"]
        for event in summary["events"]
        if event["stage"] == "nmt" and event["event"] == "completed"
    ) == 1
    assert all("text" not in event for event in summary["events"])
    await session.aclose()


@pytest.mark.asyncio
async def test_atomic_tts_retry_is_observable_without_duplicate_audio():
    tts = FakeTTSClient(retry_count=1)
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "Peace."), COMPLETE]),
        nmt_client=FakeNMTClient(),
        tts_client=tts,
        config=config(),
        session_id="tts-retry",
    )

    await session.start()
    session.finish_input()
    outputs = await drain(session)

    assert [output.kind for output in outputs] == [
        StagedOutputEventKind.AUDIO,
        StagedOutputEventKind.COMPLETE,
    ]
    assert tts.sequences == [0]
    tts_completed = [
        event
        for event in session.telemetry
        if event.stage == "tts" and event.event == "completed"
    ]
    assert len(tts_completed) == 1
    assert tts_completed[0].sequence_id == 0
    assert tts_completed[0].retry_count == 1
    summary = session.summary(include_events=True)
    assert summary["tts_retry_count"] == 1
    assert summary["audio_segments_produced"] == 1
    assert summary["completed_sequence_ids"] == [0]
    await session.aclose()


@pytest.mark.asyncio
async def test_child_retry_is_attributed_without_duplicate_composite_output():
    retry_key = (0, 1, 4)
    tts = FakeTTSClient(retry_count=1, retry_order_key=retry_key)
    session = StagedPipelineSession(
        asr_client=FakeASRClient(
            [
                final(
                    0,
                    "This parent has several words and a final clause.",
                ),
                COMPLETE,
            ]
        ),
        nmt_client=FakeNMTClient(),
        tts_client=tts,
        config=config(
            tts_subsegment_max_chars=20,
            tts_subsegment_min_chars=1,
        ),
        session_id="child-retry",
    )

    await session.start()
    session.finish_input()
    outputs = await drain(session)

    expected_keys = [(0, index, 4) for index in range(4)]
    assert [output.segment.order_key for output in outputs[:-1]] == expected_keys
    assert tts.order_keys == expected_keys
    completed = [
        event
        for event in session.telemetry
        if event.stage == "tts" and event.event == "completed"
    ]
    assert [event.retry_count for event in completed] == [0, 1, 0, 0]
    retried = completed[1]
    assert (
        retried.parent_sequence_id,
        retried.subsequence_id,
        retried.subsequence_count,
    ) == retry_key
    summary = session.summary()
    assert summary["tts_retry_count"] == 1
    assert summary["audio_segments_produced"] == 4
    assert summary["completed_sequence_ids"] == [0]
    assert summary["incomplete_subsegment_keys"] == []
    await session.aclose()


@pytest.mark.asyncio
async def test_exhausted_tts_retry_records_sequence_and_retry_without_audio():
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "Peace."), COMPLETE]),
        nmt_client=FakeNMTClient(),
        tts_client=ExhaustedRetryTTS(),
        config=config(),
        session_id="tts-retry-failed",
    )

    await session.start()
    session.finish_input()
    terminal = await session.next_output(timeout_s=2)

    assert terminal.kind is StagedOutputEventKind.ERROR
    assert terminal.stage == "tts"
    retry_error = next(
        event
        for event in session.telemetry
        if event.stage == "tts" and event.event == "error"
    )
    assert retry_error.sequence_id == 0
    assert retry_error.retry_count == 1
    summary = session.summary()
    assert summary["tts_retry_count"] == 0
    assert summary["audio_segments_produced"] == 0
    assert summary["completed_sequence_ids"] == []
    assert summary["incomplete_sequence_ids"] == [0]
    await session.aclose()


@pytest.mark.asyncio
async def test_child_failure_keeps_composite_attribution_and_parent_plan():
    failing_key = (0, 1, 4)
    tts = ExhaustedChildRetryTTS(failing_key)
    session = StagedPipelineSession(
        asr_client=FakeASRClient(
            [
                final(
                    0,
                    "This parent has several words and a final clause.",
                ),
                COMPLETE,
            ]
        ),
        nmt_client=FakeNMTClient(),
        tts_client=tts,
        config=config(
            tts_subsegment_max_chars=20,
            tts_subsegment_min_chars=1,
        ),
        session_id="child-retry-failed",
    )

    await session.start()
    session.finish_input()
    outputs = await drain(session)

    assert [output.kind for output in outputs] == [
        StagedOutputEventKind.AUDIO,
        StagedOutputEventKind.ERROR,
    ]
    assert outputs[0].segment.order_key == (0, 0, 4)
    error = next(
        event
        for event in session.telemetry
        if event.stage == "tts" and event.event == "error"
    )
    assert (
        error.parent_sequence_id,
        error.subsequence_id,
        error.subsequence_count,
    ) == failing_key
    assert error.retry_count == 1
    assert error.error_code == "SyntheticTTSRetryError"

    summary = session.summary()
    all_keys = [
        {
            "parent_sequence_id": 0,
            "subsequence_id": index,
            "subsequence_count": 4,
        }
        for index in range(4)
    ]
    assert summary["planned_subsegment_keys"] == all_keys
    assert summary["synthesized_subsegment_keys"] == all_keys[:1]
    assert summary["completed_subsegment_keys"] == all_keys[:1]
    assert summary["incomplete_subsegment_keys"] == all_keys[1:]
    assert summary["completed_sequence_ids"] == []
    assert summary["incomplete_sequence_ids"] == [0]
    await session.aclose()


@pytest.mark.asyncio
async def test_tts_timeout_preserves_active_sequence_and_retry_attribution():
    tts = BlockingRetryTTS()
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "Peace."), COMPLETE]),
        nmt_client=FakeNMTClient(),
        tts_client=tts,
        config=config(tts_rpc_timeout_s=0.03),
        session_id="tts-retry-timeout",
    )

    await session.start()
    session.finish_input()
    terminal = await session.next_output(timeout_s=1)

    assert terminal.kind is StagedOutputEventKind.ERROR
    assert terminal.stage == "tts"
    assert tts.started.is_set()
    assert tts.cancelled.is_set()
    timeout_error = next(
        event
        for event in session.telemetry
        if event.stage == "tts" and event.event == "error"
    )
    assert timeout_error.sequence_id == 0
    assert timeout_error.retry_count == 1
    assert timeout_error.error_code == "StagedModelTimeoutError"
    assert session.summary()["incomplete_sequence_ids"] == [0]
    await session.aclose()


@pytest.mark.asyncio
async def test_child_timeout_preserves_composite_and_retry_attribution():
    blocking_key = (0, 1, 4)
    tts = BlockingChildRetryTTS(blocking_key)
    session = StagedPipelineSession(
        asr_client=FakeASRClient(
            [
                final(
                    0,
                    "This parent has several words and a final clause.",
                ),
                COMPLETE,
            ]
        ),
        nmt_client=FakeNMTClient(),
        tts_client=tts,
        config=config(
            tts_rpc_timeout_s=0.03,
            tts_subsegment_max_chars=20,
            tts_subsegment_min_chars=1,
        ),
        session_id="child-retry-timeout",
    )

    await session.start()
    session.finish_input()
    outputs = await drain(session)

    assert [output.kind for output in outputs] == [
        StagedOutputEventKind.AUDIO,
        StagedOutputEventKind.ERROR,
    ]
    assert tts.started.is_set()
    assert tts.cancelled.is_set()
    error = next(
        event
        for event in session.telemetry
        if event.stage == "tts" and event.event == "error"
    )
    assert (
        error.parent_sequence_id,
        error.subsequence_id,
        error.subsequence_count,
    ) == blocking_key
    assert error.retry_count == 1
    assert error.error_code == "StagedModelTimeoutError"
    summary = session.summary()
    assert summary["tts_subsegments_planned"] == 4
    assert summary["tts_subsegments_produced"] == 1
    assert summary["completed_sequence_ids"] == []
    assert summary["incomplete_sequence_ids"] == [0]
    await session.aclose()


@pytest.mark.asyncio
async def test_exhausted_nmt_recovery_records_sequence_and_retry_before_tts():
    tts = FakeTTSClient()
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "Peace."), COMPLETE]),
        nmt_client=RecoveryFailingNMT(),
        tts_client=tts,
        config=config(),
        session_id="nmt-recovery-failed",
    )

    await session.start()
    session.finish_input()
    terminal = await session.next_output(timeout_s=2)

    assert terminal.kind is StagedOutputEventKind.ERROR
    assert terminal.stage == "nmt"
    assert tts.sequences == []
    recovery_error = next(
        event
        for event in session.telemetry
        if event.stage == "nmt" and event.event == "error"
    )
    assert recovery_error.sequence_id == 0
    assert recovery_error.retry_count == 1
    assert recovery_error.error_code == "DirectNMTRecoveryError"
    assert recovery_error.contributing_final_ids == (0,)
    assert "text" not in recovery_error.to_dict()
    summary = session.summary(include_events=True)
    assert summary["nmt_retry_count"] == 0
    assert summary["incomplete_sequence_ids"] == [0]
    assert summary["failure"]["stage"] == "nmt"
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
    with pytest.raises(ValueError, match="tts_max_retries"):
        config(tts_max_retries=2)


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
        {"tts_response_chunk_telemetry_enabled": 1},
        {"tts_subsegment_max_chars": -1},
        {"tts_subsegment_max_chars": True},
        {"tts_subsegment_max_chars": 1.5},
        {"tts_subsegment_min_chars": 0},
        {"tts_subsegment_min_chars": True},
        {"close_timeout_s": True},
    ],
)
def test_staged_config_rejects_invalid_runtime_types(overrides):
    with pytest.raises(ValueError):
        config(**overrides)


def test_staged_config_allows_minimum_packing_preference_above_cap():
    resolved = config(
        tts_subsegment_max_chars=5,
        tts_subsegment_min_chars=12,
    )

    assert resolved.tts_subsegment_max_chars == 5
    assert resolved.tts_subsegment_min_chars == 12


class IncrementalFakeTTS(FakeTTSClient):
    """Publish deterministic schema-v3 frames from the blocking TTS worker."""

    def __init__(
        self,
        frame_sizes=(3_200, 800),
        *,
        fail_after_frames=None,
        completion_frame_delta=0,
        completion_byte_delta=0,
        atomic_fallback_applied=False,
    ):
        super().__init__()
        self.frame_sizes = tuple(frame_sizes)
        self.fail_after_frames = fail_after_frames
        self.completion_frame_delta = completion_frame_delta
        self.completion_byte_delta = completion_byte_delta
        self.atomic_fallback_applied = atomic_fallback_applied
        self.publish_attempted = [
            threading.Event() for _ in self.frame_sizes
        ]
        self.publish_committed = [
            threading.Event() for _ in self.frame_sizes
        ]
        self.incremental_calls = 0

    def synthesize_incremental(self, translation, publish_frame):
        self.incremental_calls += 1
        self.order_keys.append(translation.order_key)
        self.translations.append(translation)
        self.starts[translation.sequence_id] = time.monotonic()
        started_ms = now_ms()
        first_audio_ms = None
        committed_bytes = 0
        atomic_completed_ms = (
            now_ms() if self.atomic_fallback_applied else None
        )

        for audio_frame_id, audio_bytes in enumerate(self.frame_sizes):
            received_ms = (
                atomic_completed_ms
                if atomic_completed_ms is not None
                else now_ms()
            )
            if first_audio_ms is None:
                first_audio_ms = received_ms
            audio = bytes(
                (translation.sequence_id + 1, audio_frame_id + 1)
            ) * (audio_bytes // 2)
            frame = SynthesizedAudioFrame(
                translation=translation,
                audio_frame_id=audio_frame_id,
                audio=audio,
                sample_rate_hz=16_000,
                channels=1,
                bytes_per_sample=2,
                received_monotonic_ms=received_ms,
            )
            self.publish_attempted[audio_frame_id].set()
            publish_frame(frame)
            self.publish_committed[audio_frame_id].set()
            committed_bytes += len(audio)
            if self.fail_after_frames == audio_frame_id + 1:
                raise RuntimeError(
                    "synthetic incremental failure after committed audio"
                )

        completed_ms = (
            atomic_completed_ms
            if atomic_completed_ms is not None
            else now_ms()
        )
        self.sequences.append(translation.sequence_id)
        self.ends[translation.sequence_id] = time.monotonic()
        return SynthesizedStreamCompletion(
            translation=translation,
            audio_frame_count=(
                len(self.frame_sizes) + self.completion_frame_delta
            ),
            audio_bytes=committed_bytes + self.completion_byte_delta,
            sample_rate_hz=16_000,
            channels=1,
            bytes_per_sample=2,
            started_monotonic_ms=started_ms,
            first_audio_monotonic_ms=first_audio_ms,
            completed_monotonic_ms=completed_ms,
            atomic_fallback_applied=self.atomic_fallback_applied,
        )


class FixedTranslationNMT(FakeNMTClient):
    """Return one deterministic target while preserving source attribution."""

    def __init__(self, target_text):
        super().__init__()
        self.target_text = target_text

    def translate_segment(self, segment, target_language):
        return replace(
            super().translate_segment(segment, target_language),
            text=self.target_text,
        )


async def wait_for_thread_event(event, timeout_s=1):
    deadline = time.monotonic() + timeout_s
    while not event.is_set():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for worker-thread event")
        await asyncio.sleep(0.01)


async def drain_incremental(session):
    outputs = []
    while True:
        output = await session.next_output(timeout_s=3)
        outputs.append(output)
        if output.kind in {
            StagedOutputEventKind.COMPLETE,
            StagedOutputEventKind.ERROR,
        }:
            return outputs


def streaming_translation(sequence_id=0):
    captured_ms = now_ms()
    source = TextSegment(
        sequence_id=sequence_id,
        text="A compact source.",
        reason=EmissionReason.PUNCTUATION,
        emitted_monotonic_ms=captured_ms,
        buffered_since_monotonic_ms=captured_ms - 1,
        source_start_ms=0,
        source_end_ms=1_000,
        contributing_final_ids=(sequence_id,),
    )
    return TranslatedSegment(
        segment=source,
        text="Una fuente compacta.",
        language="es-US",
        started_monotonic_ms=captured_ms,
        completed_monotonic_ms=captured_ms,
    )


def streaming_frame(translation, audio_frame_id=0, audio_bytes=1_600):
    return SynthesizedAudioFrame(
        translation=translation,
        audio_frame_id=audio_frame_id,
        audio=b"\x01\x00" * (audio_bytes // 2),
        sample_rate_hz=16_000,
        channels=1,
        bytes_per_sample=2,
        received_monotonic_ms=now_ms(),
    )


def streaming_completion(
    translation,
    *,
    audio_frame_count=1,
    audio_bytes=1_600,
    atomic_fallback_applied=False,
):
    captured_ms = now_ms()
    return SynthesizedStreamCompletion(
        translation=translation,
        audio_frame_count=audio_frame_count,
        audio_bytes=audio_bytes,
        sample_rate_hz=16_000,
        channels=1,
        bytes_per_sample=2,
        started_monotonic_ms=captured_ms,
        first_audio_monotonic_ms=captured_ms,
        completed_monotonic_ms=captured_ms,
        atomic_fallback_applied=atomic_fallback_applied,
    )


@pytest.mark.asyncio
async def test_incremental_clean_order_is_frames_parent_marker_then_complete():
    tts = IncrementalFakeTTS()
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "One parent."), COMPLETE]),
        nmt_client=FakeNMTClient(),
        tts_client=tts,
        config=config(
            output_queue_maxsize=4,
            tts_incremental_publish_enabled=True,
            tts_incremental_frame_ms=100,
        ),
        session_id="incremental-clean-order",
    )

    await session.start()
    session.finish_input()
    outputs = await drain_incremental(session)

    assert [output.kind for output in outputs] == [
        StagedOutputEventKind.AUDIO_FRAME,
        StagedOutputEventKind.AUDIO_FRAME,
        StagedOutputEventKind.PARENT_COMPLETE,
        StagedOutputEventKind.COMPLETE,
    ]
    assert [output.frame.frame_key for output in outputs[:2]] == [
        (0, 0),
        (0, 1),
    ]
    assert outputs[2].completion.parent_sequence_id == 0
    assert outputs[2].completion.audio_frame_count == 2
    assert outputs[2].completion.audio_bytes == 4_000
    assert tts.incremental_calls == 1

    summary = session.summary(include_events=True)
    expected_keys = [
        {"parent_sequence_id": 0, "audio_frame_id": 0},
        {"parent_sequence_id": 0, "audio_frame_id": 1},
    ]
    assert summary["telemetry_schema_version"] == 3
    assert summary["tts_incremental_publish_enabled"] is True
    assert summary["tts_incremental_atomic_fallback_max_chars"] == 4
    assert summary["tts_incremental_atomic_fallback_parent_count"] == 0
    assert (
        summary["tts_incremental_atomic_fallback_parent_sequence_ids"]
        == []
    )
    assert summary["audio_frames_produced"] == 2
    assert summary["published_audio_frame_keys"] == expected_keys
    assert summary["dequeued_audio_frame_keys"] == expected_keys
    assert summary["published_audio_frame_bytes"] == [3_200, 800]
    assert summary["dequeued_audio_frame_bytes"] == [3_200, 800]
    assert summary["completed_sequence_ids"] == [0]
    assert summary["incomplete_sequence_ids"] == []
    assert summary["produced_parent_summaries"] == [
        {
            "parent_sequence_id": 0,
            "audio_frame_count": 2,
            "audio_bytes": 4_000,
            "retry_count": 0,
            "atomic_fallback_applied": False,
        }
    ]
    assert (
        summary["completed_parent_summaries"]
        == summary["produced_parent_summaries"]
    )
    await session.aclose()


@pytest.mark.asyncio
async def test_incremental_short_target_records_atomic_fallback_at_every_barrier():
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "One parent."), COMPLETE]),
        nmt_client=FixedTranslationNMT("Sí"),
        tts_client=IncrementalFakeTTS(
            frame_sizes=(3_200, 800),
            atomic_fallback_applied=True,
        ),
        config=config(
            output_queue_maxsize=4,
            tts_incremental_publish_enabled=True,
            tts_incremental_frame_ms=100,
            tts_incremental_atomic_fallback_max_chars=4,
        ),
        session_id="incremental-atomic-fallback",
    )

    await session.start()
    session.finish_input()
    outputs = await drain_incremental(session)

    assert [output.kind for output in outputs] == [
        StagedOutputEventKind.AUDIO_FRAME,
        StagedOutputEventKind.AUDIO_FRAME,
        StagedOutputEventKind.PARENT_COMPLETE,
        StagedOutputEventKind.COMPLETE,
    ]
    assert [len(output.frame.audio) for output in outputs[:2]] == [3_200, 800]
    assert outputs[2].completion.atomic_fallback_applied is True

    summary = session.summary(include_events=True)
    assert summary["tts_incremental_atomic_fallback_max_chars"] == 4
    assert summary["tts_incremental_atomic_fallback_parent_count"] == 1
    assert (
        summary["tts_incremental_atomic_fallback_parent_sequence_ids"]
        == [0]
    )
    expected_parent = {
        "parent_sequence_id": 0,
        "audio_frame_count": 2,
        "audio_bytes": 4_000,
        "retry_count": 0,
        "atomic_fallback_applied": True,
    }
    assert summary["produced_parent_summaries"] == [expected_parent]
    assert summary["completed_parent_summaries"] == [expected_parent]
    completion_events = [
        event
        for event in summary["events"]
        if (
            event["stage"] == "tts"
            and event["event"] in {"first_audio", "completed"}
        )
        or (
            event["stage"] == "output"
            and event["event"]
            in {"parent_complete_enqueued", "parent_complete_dequeued"}
        )
    ]
    assert completion_events
    assert all(
        event["atomic_fallback_applied"] is True
        for event in completion_events
    )
    assert all(
        "atomic_fallback_applied" not in event
        for event in summary["events"]
        if event["event"]
        in {"frame_received", "frame_enqueued", "frame_dequeued"}
    )
    tts_completed_ms = next(
        event["monotonic_ms"]
        for event in summary["events"]
        if (event["stage"], event["event"]) == ("tts", "completed")
    )
    assert all(
        event["monotonic_ms"] >= tts_completed_ms
        for event in summary["events"]
        if (event["stage"], event["event"]) == ("tts", "frame_received")
    )
    await session.aclose()


@pytest.mark.asyncio
async def test_incremental_rejects_fallback_attribution_outside_length_policy():
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "One parent."), COMPLETE]),
        nmt_client=FixedTranslationNMT("Una traducción larga."),
        tts_client=IncrementalFakeTTS(
            frame_sizes=(1_600,),
            atomic_fallback_applied=True,
        ),
        config=config(
            output_queue_maxsize=2,
            tts_incremental_publish_enabled=True,
            tts_incremental_atomic_fallback_max_chars=4,
        ),
        session_id="incremental-invalid-atomic-fallback",
    )

    await session.start()
    session.finish_input()
    outputs = await drain_incremental(session)

    assert [output.kind for output in outputs] == [
        StagedOutputEventKind.AUDIO_FRAME,
        StagedOutputEventKind.ERROR,
    ]
    assert "fallback attribution" in outputs[-1].error
    summary = session.summary()
    assert summary["tts_incremental_atomic_fallback_parent_count"] == 0
    assert (
        summary["tts_incremental_atomic_fallback_parent_sequence_ids"]
        == []
    )
    assert summary["produced_parent_summaries"] == []
    await session.aclose()


@pytest.mark.asyncio
async def test_incremental_parent_completes_only_when_marker_is_dequeued():
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "One parent."), COMPLETE]),
        nmt_client=FakeNMTClient(),
        tts_client=IncrementalFakeTTS(frame_sizes=(1_600,)),
        config=config(
            output_queue_maxsize=2,
            tts_incremental_publish_enabled=True,
        ),
        session_id="incremental-marker-completion",
    )

    await session.start()
    session.finish_input()
    frame = await session.next_output(timeout_s=2)

    assert frame.kind is StagedOutputEventKind.AUDIO_FRAME
    assert session.summary()["completed_sequence_ids"] == []
    assert session.summary()["incomplete_sequence_ids"] == [0]

    marker = await session.next_output(timeout_s=2)

    assert marker.kind is StagedOutputEventKind.PARENT_COMPLETE
    assert session.summary()["completed_sequence_ids"] == [0]
    assert session.summary()["incomplete_sequence_ids"] == []
    assert (
        await session.next_output(timeout_s=2)
    ).kind is StagedOutputEventKind.COMPLETE
    await session.aclose()


@pytest.mark.asyncio
async def test_incremental_output_capacity_one_backpressures_worker():
    tts = IncrementalFakeTTS(frame_sizes=(3_200, 3_200))
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "One parent."), COMPLETE]),
        nmt_client=FakeNMTClient(),
        tts_client=tts,
        config=config(
            output_queue_maxsize=1,
            tts_incremental_publish_enabled=True,
            tts_incremental_frame_ms=100,
        ),
        session_id="incremental-backpressure",
    )

    await session.start()
    session.finish_input()
    await wait_for_thread_event(tts.publish_attempted[1])

    assert tts.publish_committed[0].is_set()
    assert not tts.publish_committed[1].is_set()
    assert session._output_queue.qsize() == 1
    assert session.summary()["audio_frames_produced"] == 1
    assert session.summary()["max_queue_depths"]["output"] == 1

    first = await session.next_output(timeout_s=2)
    assert first.frame.frame_key == (0, 0)
    await wait_for_thread_event(tts.publish_committed[1])
    assert session.summary()["audio_frames_produced"] == 2

    second = await session.next_output(timeout_s=2)
    marker = await session.next_output(timeout_s=2)
    terminal = await session.next_output(timeout_s=2)

    assert second.frame.frame_key == (0, 1)
    assert marker.kind is StagedOutputEventKind.PARENT_COMPLETE
    assert terminal.kind is StagedOutputEventKind.COMPLETE
    assert session.summary()["max_queue_depths"]["output"] == 1
    assert session.summary()["blocked_put_counts"]["output"] >= 1
    await session.aclose()


@pytest.mark.asyncio
async def test_incremental_abort_wins_simultaneous_output_capacity_race():
    session = StagedPipelineSession(
        asr_client=FakeASRClient(),
        nmt_client=FakeNMTClient(),
        tts_client=IncrementalFakeTTS(frame_sizes=(1_600,)),
        config=config(
            output_queue_maxsize=1,
            tts_incremental_publish_enabled=True,
        ),
        session_id="incremental-abort-capacity-race",
    )
    session._state = StagedPipelineState.RUNNING
    translation = streaming_translation()
    frame = streaming_frame(translation)
    abort_event = asyncio.Event()
    output_slots = session._queue_slots["output"]

    await output_slots.acquire()
    publish = asyncio.create_task(
        session._enqueue_incremental_frame(
            frame,
            abort_event=abort_event,
        )
    )
    await asyncio.sleep(0)

    # Make capacity and abort ready in the same event-loop turn. Queue insertion
    # is the commit boundary, so this uncommitted frame must lose the race.
    output_slots.release()
    abort_event.set()

    assert await publish is False
    assert session._output_queue.empty()
    assert output_slots._value == 1
    assert session.summary()["audio_frames_produced"] == 0
    assert session.summary()["published_audio_frame_keys"] == []

    await session._enqueue(
        session._output_queue,
        StagedOutputEvent(
            kind=StagedOutputEventKind.ERROR,
            stage="nmt",
            error="synthetic sibling failure",
        ),
        "output",
        "error",
    )
    terminal = await session.next_output(timeout_s=1)
    assert terminal.kind is StagedOutputEventKind.ERROR
    assert session._output_queue.empty()
    await session.aclose()


@pytest.mark.asyncio
async def test_incremental_partial_prefix_precedes_single_error_without_marker():
    tts = IncrementalFakeTTS(
        frame_sizes=(3_200, 3_200),
        fail_after_frames=1,
    )
    session = StagedPipelineSession(
        asr_client=FakeASRClient([final(0, "One parent."), COMPLETE]),
        nmt_client=FakeNMTClient(),
        tts_client=tts,
        config=config(
            output_queue_maxsize=1,
            tts_incremental_publish_enabled=True,
        ),
        session_id="incremental-partial-prefix",
    )

    await session.start()
    session.finish_input()
    outputs = await drain_incremental(session)

    assert [output.kind for output in outputs] == [
        StagedOutputEventKind.AUDIO_FRAME,
        StagedOutputEventKind.ERROR,
    ]
    assert outputs[0].frame.frame_key == (0, 0)
    assert outputs[1].stage == "tts"
    assert "synthetic incremental failure" in outputs[1].error
    summary = session.summary()
    assert summary["outcome"] == "failed"
    assert summary["audio_frames_produced"] == 1
    assert summary["published_audio_frame_keys"] == [
        {"parent_sequence_id": 0, "audio_frame_id": 0}
    ]
    assert summary["dequeued_audio_frame_keys"] == [
        {"parent_sequence_id": 0, "audio_frame_id": 0}
    ]
    assert summary["produced_parent_summaries"] == []
    assert summary["completed_parent_summaries"] == []
    assert summary["completed_sequence_ids"] == []
    assert summary["incomplete_sequence_ids"] == [0]
    assert tts.incremental_calls == 1
    await session.aclose()


@pytest.mark.asyncio
async def test_incremental_output_rejects_parent_marker_total_mismatch():
    session = StagedPipelineSession(
        asr_client=FakeASRClient(),
        nmt_client=FakeNMTClient(),
        tts_client=IncrementalFakeTTS(frame_sizes=(1_600,)),
        config=config(
            output_queue_maxsize=2,
            tts_incremental_publish_enabled=True,
        ),
        session_id="incremental-marker-mismatch",
    )
    session._state = StagedPipelineState.RUNNING
    translation = streaming_translation()
    frame = streaming_frame(translation)
    mismatched = streaming_completion(
        translation,
        audio_frame_count=2,
        audio_bytes=len(frame.audio),
    )

    await session._enqueue(
        session._output_queue,
        StagedOutputEvent(
            kind=StagedOutputEventKind.AUDIO_FRAME,
            frame=frame,
        ),
        "output",
        "frame_enqueued",
    )
    await session._enqueue(
        session._output_queue,
        StagedOutputEvent(
            kind=StagedOutputEventKind.PARENT_COMPLETE,
            completion=mismatched,
        ),
        "output",
        "parent_complete_enqueued",
    )

    assert (
        await session.next_output(timeout_s=1)
    ).kind is StagedOutputEventKind.AUDIO_FRAME
    with pytest.raises(
        StagedPipelineError,
        match="parent completion frame count mismatch",
    ):
        await session.next_output(timeout_s=1)
    assert session.summary()["completed_sequence_ids"] == []
    await session.aclose()


def test_incremental_config_selects_schema_three_and_rejects_subsegments():
    assert config().telemetry_schema_version == 1
    assert config(tts_subsegment_max_chars=20).telemetry_schema_version == 2
    assert config(
        tts_incremental_publish_enabled=True
    ).telemetry_schema_version == 3

    with pytest.raises(ValueError, match="mutually exclusive"):
        config(
            tts_incremental_publish_enabled=True,
            tts_subsegment_max_chars=20,
        )


@pytest.mark.asyncio
async def test_legacy_subsegments_remain_schema_two_and_atomic():
    session = StagedPipelineSession(
        asr_client=FakeASRClient(
            [final(0, "Alpha beta gamma delta epsilon."), COMPLETE]
        ),
        nmt_client=FakeNMTClient(),
        tts_client=FakeTTSClient(),
        config=config(
            tts_incremental_publish_enabled=False,
            tts_subsegment_max_chars=10,
            tts_subsegment_min_chars=1,
        ),
        session_id="legacy-subsegments-after-schema-three",
    )

    await session.start()
    session.finish_input()
    outputs = await drain(session)
    audio_outputs = outputs[:-1]

    assert len(audio_outputs) > 1
    assert all(
        output.kind is StagedOutputEventKind.AUDIO
        for output in audio_outputs
    )
    assert outputs[-1].kind is StagedOutputEventKind.COMPLETE
    summary = session.summary(include_events=True)
    assert summary["telemetry_schema_version"] == 2
    assert summary["tts_subsegmentation_enabled"] is True
    assert "tts_incremental_publish_enabled" not in summary
    assert "published_audio_frame_keys" not in summary
    assert all(
        "audio_frame_id" not in event
        and "audio_frame_count" not in event
        for event in summary["events"]
    )
    await session.aclose()
