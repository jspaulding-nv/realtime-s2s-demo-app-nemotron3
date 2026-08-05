"""Bounded, ordered ASR -> NMT -> TTS orchestration.

This module is the application boundary for the direct NIM adapters.  The
pipeline is available to the browser WebSocket route only when the explicit
``S2S_PIPELINE_MODE=staged`` feature flag is selected; the monolithic route
remains the default.  One worker per model preserves source order while still
allowing NMT for segment ``n + 1`` to overlap TTS for segment ``n``.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple

from asr_attribution import (
    final_attribution_record,
    summarize_final_attribution,
)
from config import (
    SUPPORTED_LANGUAGES,
    StagedPipelineConfig,
    audio_config,
    staged_pipeline_config,
)
from punctuation_segmenter import FillerDiscard, PunctuationSegmenter
from staged_models import (
    ASRStreamEventKind,
    PipelineEvent,
    StagedOutputEvent,
    StagedOutputEventKind,
    SynthesizedAudioFrame,
    SynthesizedSegment,
    SynthesizedStreamCompletion,
    TextSegment,
    TranslatedSegment,
)
from target_text_validation import validate_target_text
from target_text_splitter import split_target_text


class StagedPipelineState(str, Enum):
    NEW = "new"
    RUNNING = "running"
    DRAINING = "draining"
    COMPLETE = "complete"
    FAILED = "failed"
    CLOSED = "closed"


class StagedPipelineError(RuntimeError):
    """Raised for an invalid lifecycle or failed model stage."""


class StagedModelTimeoutError(StagedPipelineError):
    """Privacy-safe timeout with segment and retry-attempt attribution."""

    def __init__(
        self,
        *,
        stage: str,
        segment: Optional[TextSegment] = None,
        translation: Optional[TranslatedSegment] = None,
        timeout_s: float,
        retry_count: int = 0,
    ) -> None:
        if segment is not None and translation is not None:
            raise ValueError("provide segment or translation, not both")
        if translation is None and not isinstance(segment, TextSegment):
            raise ValueError("a segment or translation is required")
        if translation is not None and not isinstance(
            translation, TranslatedSegment
        ):
            raise ValueError("translation must be a TranslatedSegment")
        self.translation = translation
        self.segment = translation.segment if translation is not None else segment
        self.sequence_id = self.segment.sequence_id
        self.parent_sequence_id = self.sequence_id
        self.subsequence_id = (
            translation.subsequence_id if translation is not None else None
        )
        self.subsequence_count = (
            translation.subsequence_count if translation is not None else None
        )
        self.retry_count = (
            retry_count
            if isinstance(retry_count, int)
            and not isinstance(retry_count, bool)
            and retry_count in {0, 1}
            else 0
        )
        identity = f"sequence {self.sequence_id}"
        if translation is not None:
            identity += (
                f" subsequence {translation.subsequence_id + 1}/"
                f"{translation.subsequence_count}"
            )
        super().__init__(
            f"{stage.upper()} model RPC exceeded {timeout_s:.3f} seconds "
            f"for {identity}"
        )


class StagedAttributedModelError(StagedPipelineError):
    """Retain composite identity for a model exception without metadata."""

    def __init__(
        self,
        exc: Exception,
        translation: TranslatedSegment,
    ) -> None:
        self.translation = translation
        self.segment = translation.segment
        self.sequence_id = translation.sequence_id
        self.parent_sequence_id = translation.sequence_id
        self.subsequence_id = translation.subsequence_id
        self.subsequence_count = translation.subsequence_count
        retry_count = getattr(exc, "retry_count", 0)
        self.retry_count = (
            retry_count
            if isinstance(retry_count, int)
            and not isinstance(retry_count, bool)
            and retry_count in {0, 1}
            else 0
        )
        self.original_error_code = type(exc).__name__
        super().__init__(str(exc) or self.original_error_code)


class StagedTargetSplitError(StagedPipelineError):
    """Privacy-safe target-split failure with parent attribution."""

    failure_stage = "target_splitter"
    retry_count = 0

    def __init__(
        self,
        exc: Exception,
        translation: TranslatedSegment,
    ) -> None:
        self.translation = translation
        self.segment = translation.segment
        self.sequence_id = translation.sequence_id
        self.parent_sequence_id = translation.sequence_id
        self.subsequence_id = None
        self.subsequence_count = None
        self.original_error_code = type(exc).__name__
        super().__init__(
            "target-text splitting failed for sequence "
            f"{translation.sequence_id}: {self.original_error_code}"
        )


@dataclass(frozen=True)
class _QueuedItem:
    payload: Any
    enqueued_monotonic_ms: float


_DRAIN = object()


class _FramePublisherAborted(StagedPipelineError):
    """Raised in the TTS worker when a frame bridge is explicitly aborted."""


class _ThreadsafeFramePublisher:
    """Acknowledge one bounded async enqueue from the blocking TTS thread."""

    def __init__(
        self,
        session: "StagedPipelineSession",
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._session = session
        self._loop = loop
        self._thread_aborted = threading.Event()
        self._async_aborted = asyncio.Event()
        self._lock = threading.Lock()
        self._pending: Optional[Future] = None

    def publish(self, frame: SynthesizedAudioFrame) -> None:
        """Return only after ``frame`` is committed to the output queue."""
        if self._thread_aborted.is_set():
            raise _FramePublisherAborted("incremental frame publisher aborted")
        publish_requested_ms = (
            self._session._clock_ms()
            if self._session.config.tts_publisher_handoff_telemetry_enabled
            else None
        )
        pending = asyncio.run_coroutine_threadsafe(
            self._session._enqueue_incremental_frame(
                frame,
                abort_event=self._async_aborted,
                publish_requested_monotonic_ms=publish_requested_ms,
            ),
            self._loop,
        )
        with self._lock:
            self._pending = pending
        try:
            committed = pending.result()
        except Exception as exc:
            raise _FramePublisherAborted(
                "incremental frame publisher failed"
            ) from exc
        finally:
            with self._lock:
                if self._pending is pending:
                    self._pending = None
        if not committed:
            raise _FramePublisherAborted("incremental frame publisher aborted")

    def abort(self) -> None:
        """Wake a worker blocked on output capacity without an ambiguous commit."""
        self._thread_aborted.set()
        self._async_aborted.set()


class StagedPipelineSession:
    """One bounded, FIFO speech-translation session.

    The ASR adapter is asynchronous.  Blocking NMT and TTS calls run on two
    separate single-worker executors, which provides stage overlap without a
    reorder buffer. Schemas 1 and 2 publish TTS atomically. The default-off
    schema-3 experiment instead commits bounded PCM frames while one TTS RPC
    remains active.
    """

    def __init__(
        self,
        *,
        asr_client: Any,
        nmt_client: Any,
        tts_client: Any,
        target_language: str = "es-US",
        config: Optional[StagedPipelineConfig] = None,
        session_id: Optional[str] = None,
        clock_ms: Optional[Callable[[], float]] = None,
        owns_clients: bool = False,
        retain_telemetry: bool = True,
        event_sink: Optional[Callable[[PipelineEvent], None]] = None,
    ) -> None:
        if target_language not in SUPPORTED_LANGUAGES:
            raise ValueError(f"unsupported target language: {target_language}")
        self.asr_client = asr_client
        self.nmt_client = nmt_client
        self.tts_client = tts_client
        self.target_language = target_language
        self.config = config or staged_pipeline_config
        frame_bytes_numerator = (
            audio_config.sample_rate
            * audio_config.channels
            * audio_config.bytes_per_sample
            * self.config.tts_incremental_frame_ms
        )
        if frame_bytes_numerator % 1_000:
            raise ValueError(
                "incremental TTS frame duration must produce whole PCM bytes"
            )
        self._incremental_frame_bytes = frame_bytes_numerator // 1_000
        self.session_id = session_id or uuid.uuid4().hex
        if not self.session_id.strip():
            raise ValueError("session_id is required")
        self._clock_ms = clock_ms or (lambda: time.monotonic_ns() / 1_000_000)
        self._owns_clients = owns_clients
        self._retain_telemetry = retain_telemetry
        self._event_sink = event_sink

        self._nmt_queue: asyncio.Queue[_QueuedItem] = asyncio.Queue(
            maxsize=self.config.nmt_queue_maxsize
        )
        self._tts_queue: asyncio.Queue[_QueuedItem] = asyncio.Queue(
            maxsize=self.config.tts_queue_maxsize
        )
        self._output_queue: asyncio.Queue[_QueuedItem] = asyncio.Queue(
            # One extra slot is reserved for COMPLETE/ERROR so terminal state
            # cannot deadlock behind a full audio queue.
            maxsize=self.config.output_queue_maxsize + 1
        )
        self._queue_slots = {
            "nmt": asyncio.Semaphore(self.config.nmt_queue_maxsize),
            "tts": asyncio.Semaphore(self.config.tts_queue_maxsize),
            "output": asyncio.Semaphore(self.config.output_queue_maxsize),
        }
        self._nmt_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="staged-nmt"
        )
        self._tts_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="staged-tts"
        )
        self._blocking_futures: set[Future] = set()
        self._active_tts_publisher: Optional[_ThreadsafeFramePublisher] = None
        self._tasks: Tuple[asyncio.Task, ...] = ()
        self._asr_stream = None
        self._failure_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._terminal_queued = asyncio.Event()
        self._closed_event = asyncio.Event()
        self._terminal_consumed = False
        self._failure: Optional[Tuple[str, str]] = None
        self._cleanup_errors: list[Dict[str, str]] = []
        self._outcome: Optional[str] = None
        self._state = StagedPipelineState.NEW
        self._closed = False
        self._telemetry: list[PipelineEvent] = []
        self._asr_final_attribution_records: list[Dict[str, Any]] = []
        self._max_queue_depths: Dict[str, int] = {"nmt": 0, "tts": 0, "output": 0}
        self._blocked_put_counts: Dict[str, int] = {
            "nmt": 0,
            "tts": 0,
            "output": 0,
        }
        self._audio_segments_produced = 0
        self._audio_frames_produced = 0
        self._fillers_discarded = 0
        self._emitted_sequence_ids: list[int] = []
        self._planned_subsegment_keys: list[Tuple[int, int, int]] = []
        self._synthesized_subsegment_keys: list[Tuple[int, int, int]] = []
        self._consumed_subsegment_keys: list[Tuple[int, int, int]] = []
        self._consumed_sequence_ids: list[int] = []
        self._published_audio_frame_keys: list[Tuple[int, int]] = []
        self._published_audio_frame_bytes: list[int] = []
        self._dequeued_audio_frame_keys: list[Tuple[int, int]] = []
        self._dequeued_audio_frame_bytes: list[int] = []
        self._produced_parent_summaries: list[Dict[str, Any]] = []
        self._completed_parent_summaries: list[Dict[str, Any]] = []
        self._parent_translation_char_counts: Dict[int, int] = {}
        self._tts_response_chunk_metrics: list[Dict[str, Any]] = []
        self._tts_response_segments_observed = 0
        self._expected_nmt_sequence = 0
        self._expected_tts_sequence = 0
        self._expected_tts_subsequence = 0
        self._expected_tts_subsequence_count: Optional[int] = None
        self._expected_output_sequence = 0
        self._expected_output_subsequence = 0
        self._expected_output_subsequence_count: Optional[int] = None
        self._published_frame_count = 0
        self._published_frame_bytes = 0
        self._published_frame_retry_count: Optional[int] = None
        self._expected_output_frame_id = 0
        self._expected_output_frame_bytes = 0
        self._expected_output_frame_retry_count: Optional[int] = None
        self._last_asr_observation_ms: Optional[float] = None

    @property
    def state(self) -> StagedPipelineState:
        return self._state

    @property
    def telemetry(self) -> Tuple[PipelineEvent, ...]:
        return tuple(self._telemetry)

    @property
    def failure(self) -> Optional[Tuple[str, str]]:
        return self._failure

    async def start(self) -> None:
        """Connect owned clients, open direct ASR, and start all workers."""
        if self._state is not StagedPipelineState.NEW:
            raise StagedPipelineError("staged session can only be started once")
        connected = []
        try:
            for name, client in (
                ("asr", self.asr_client),
                ("nmt", self.nmt_client),
                ("tts", self.tts_client),
            ):
                if self._owns_clients and not _is_connected(client):
                    if not client.connect():
                        raise StagedPipelineError(f"failed to connect direct {name}")
                    connected.append(client)
                if not _is_connected(client):
                    raise StagedPipelineError(f"direct {name} client is not connected")

            self._asr_stream = await self.asr_client.open_stream(
                event_queue_maxsize=self.config.asr_event_queue_maxsize
            )
            self._state = StagedPipelineState.RUNNING
            self._record(stage="pipeline", event="started")
            self._tasks = (
                asyncio.create_task(
                    self._stage_runner("asr", self._consume_asr),
                    name=f"staged-asr-{self.session_id}",
                ),
                asyncio.create_task(
                    self._stage_runner("nmt", self._run_nmt),
                    name=f"staged-nmt-{self.session_id}",
                ),
                asyncio.create_task(
                    self._stage_runner("tts", self._run_tts),
                    name=f"staged-tts-{self.session_id}",
                ),
            )
        except Exception:
            for client in reversed(connected):
                client.disconnect()
            raise

    def add_audio(self, chunk: bytes) -> None:
        if self._state is not StagedPipelineState.RUNNING:
            raise StagedPipelineError("audio is accepted only while the session is running")
        if not isinstance(chunk, bytes) or not chunk:
            raise ValueError("audio chunk must be non-empty bytes")
        self._asr_stream.add_chunk(chunk)

    def finish_input(self) -> None:
        """Close ASR input once; downstream stages continue their natural drain."""
        if self._state is StagedPipelineState.DRAINING:
            return
        if self._state is not StagedPipelineState.RUNNING:
            raise StagedPipelineError("input can only finish from the running state")
        self._asr_stream.finish_input()
        self._state = StagedPipelineState.DRAINING
        self._record(stage="pipeline", event="input_finished")

    async def next_output(
        self, timeout_s: Optional[float] = None
    ) -> StagedOutputEvent:
        """Return ordered audio or the single COMPLETE/ERROR terminal event."""
        if self._state is StagedPipelineState.NEW:
            raise StagedPipelineError("staged session has not started")
        if self._terminal_consumed:
            raise StagedPipelineError("terminal output event was already consumed")
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        get = self._wait_for_output_or_close()
        item = await (get if timeout_s is None else asyncio.wait_for(get, timeout_s))
        output = item.payload
        handoff_dequeued_ms = (
            self._clock_ms()
            if (
                self.config.tts_publisher_handoff_telemetry_enabled
                and isinstance(output, StagedOutputEvent)
                and output.kind is StagedOutputEventKind.AUDIO_FRAME
            )
            else None
        )
        self._output_queue.task_done()
        if not isinstance(output, StagedOutputEvent):
            raise StagedPipelineError("output queue contained an invalid item")
        if (
            output.kind is StagedOutputEventKind.COMPLETE
            and self.config.tts_incremental_publish_enabled
            and (
                self._expected_output_frame_id != 0
                or self._expected_output_frame_bytes != 0
            )
        ):
            raise StagedPipelineError(
                "pipeline completed with an unfinished incremental parent"
            )
        if output.kind is StagedOutputEventKind.AUDIO:
            self._queue_slots["output"].release()
            self._advance_output_cursor(output.segment)
            self._consumed_subsegment_keys.append(output.segment.order_key)
            if output.segment.is_final_subsequence:
                self._consumed_sequence_ids.append(output.segment.sequence_id)
        elif output.kind is StagedOutputEventKind.AUDIO_FRAME:
            self._queue_slots["output"].release()
            frame = output.frame
            self._validate_output_frame(frame)
            self._dequeued_audio_frame_keys.append(frame.frame_key)
            self._dequeued_audio_frame_bytes.append(len(frame.audio))
            self._expected_output_frame_id += 1
            self._expected_output_frame_bytes += len(frame.audio)
        elif output.kind is StagedOutputEventKind.PARENT_COMPLETE:
            self._queue_slots["output"].release()
            completion = output.completion
            self._validate_output_parent_completion(completion)
            self._completed_parent_summaries.append(
                _parent_completion_payload(completion)
            )
            self._consumed_sequence_ids.append(completion.sequence_id)
            self._expected_output_sequence += 1
            self._expected_output_frame_id = 0
            self._expected_output_frame_bytes = 0
            self._expected_output_frame_retry_count = None
        payload = output.segment or output.frame or output.completion
        event_name = (
            "dequeued"
            if output.kind
            in {
                StagedOutputEventKind.AUDIO,
                StagedOutputEventKind.COMPLETE,
                StagedOutputEventKind.ERROR,
            }
            else (
                "frame_dequeued"
                if output.kind is StagedOutputEventKind.AUDIO_FRAME
                else "parent_complete_dequeued"
            )
        )
        residence_clock_ms = (
            handoff_dequeued_ms
            if handoff_dequeued_ms is not None
            else self._clock_ms()
        )
        self._record(
            stage="output",
            event=event_name,
            segment=payload,
            queue_depth=self._output_queue.qsize(),
            queue_capacity=(
                self.config.output_queue_maxsize
                if output.kind
                in {
                    StagedOutputEventKind.AUDIO,
                    StagedOutputEventKind.AUDIO_FRAME,
                    StagedOutputEventKind.PARENT_COMPLETE,
                }
                else self._output_queue.maxsize
            ),
            queue_residence_ms=max(
                0.0,
                residence_clock_ms - item.enqueued_monotonic_ms,
            ),
            audio_bytes=(
                len(payload.audio)
                if isinstance(payload, (SynthesizedSegment, SynthesizedAudioFrame))
                else (
                    payload.audio_bytes
                    if isinstance(payload, SynthesizedStreamCompletion)
                    else 0
                )
            ),
            audio_duration_ms=(
                payload.audio_duration_ms if payload is not None else 0.0
            ),
            retry_count=(
                payload.retry_count
                if isinstance(
                    payload,
                    (
                        SynthesizedAudioFrame,
                        SynthesizedStreamCompletion,
                    ),
                )
                else 0
            ),
            monotonic_ms=handoff_dequeued_ms,
        )
        if output.kind in {
            StagedOutputEventKind.COMPLETE,
            StagedOutputEventKind.ERROR,
        }:
            self._terminal_consumed = True
        return output

    async def wait_for_terminal(self, timeout_s: Optional[float] = None) -> None:
        waiter = self._wait_for_terminal_or_close()
        await (waiter if timeout_s is None else asyncio.wait_for(waiter, timeout_s))

    async def wait_for_workers(self, timeout_s: Optional[float] = None) -> None:
        if not self._tasks:
            return
        waiter = asyncio.gather(*self._tasks, return_exceptions=True)
        await (
            waiter
            if timeout_s is None
            else asyncio.wait_for(asyncio.shield(waiter), timeout_s)
        )

    def summary(self, *, include_events: bool = False) -> Dict[str, Any]:
        consumed_subsegment_keys = set(self._consumed_subsegment_keys)
        result: Dict[str, Any] = {
            "telemetry_schema_version": self.config.telemetry_schema_version,
            "tts_subsegmentation_enabled": (
                self.config.tts_subsegment_max_chars > 0
            ),
            "session_id": self.session_id,
            "state": self._state.value,
            "outcome": self._outcome,
            "target_language": self.target_language,
            "audio_segments_produced": self._audio_segments_produced,
            "fillers_discarded": self._fillers_discarded,
            "segments_emitted": len(self._emitted_sequence_ids),
            "asr_final_attribution": summarize_final_attribution(
                self._asr_final_attribution_records
            ),
            "nmt_retry_count": sum(
                event.retry_count
                for event in self._telemetry
                if event.stage == "nmt" and event.event == "completed"
            ),
            "tts_retry_count": sum(
                event.retry_count
                for event in self._telemetry
                if event.stage == "tts" and event.event == "completed"
            ),
            "completed_sequence_ids": list(self._consumed_sequence_ids),
            "incomplete_sequence_ids": sorted(
                set(self._emitted_sequence_ids) - set(self._consumed_sequence_ids)
            ),
            "failure": (
                {"stage": self._failure[0], "error": self._failure[1]}
                if self._failure is not None
                else (
                    {
                        "stage": "cleanup",
                        "error": self._cleanup_errors[0]["error"],
                    }
                    if self._cleanup_errors
                    else None
                )
            ),
            "cleanup_errors": list(self._cleanup_errors),
            "max_queue_depths": dict(self._max_queue_depths),
            "blocked_put_counts": dict(self._blocked_put_counts),
            "telemetry_event_count": len(self._telemetry),
        }
        if self.config.tts_response_chunk_telemetry_enabled:
            result["tts_response_chunk_telemetry_enabled"] = True
            result["tts_response_chunk_telemetry"] = {
                "schema_version": 1,
                "segments_observed": self._tts_response_segments_observed,
                "response_chunk_count": len(
                    self._tts_response_chunk_metrics
                ),
                "chunks": [
                    dict(metric)
                    for metric in self._tts_response_chunk_metrics
                ],
            }
        if self.config.tts_publisher_handoff_telemetry_enabled:
            result["tts_publisher_handoff_telemetry_enabled"] = True
        if self.config.tts_incremental_publish_enabled:
            atomic_fallback_parent_ids = [
                item["parent_sequence_id"]
                for item in self._produced_parent_summaries
                if item["atomic_fallback_applied"]
            ]
            result.update(
                {
                    "tts_incremental_publish_enabled": True,
                    "tts_incremental_frame_ms": (
                        self.config.tts_incremental_frame_ms
                    ),
                    "tts_incremental_frame_bytes": (
                        self._incremental_frame_bytes
                    ),
                    "tts_incremental_atomic_fallback_max_chars": (
                        self.config.tts_incremental_atomic_fallback_max_chars
                    ),
                    "tts_incremental_atomic_fallback_parent_count": len(
                        atomic_fallback_parent_ids
                    ),
                    "tts_incremental_atomic_fallback_parent_sequence_ids": (
                        list(atomic_fallback_parent_ids)
                    ),
                    "audio_frames_produced": self._audio_frames_produced,
                    "published_audio_frame_keys": _frame_key_payloads(
                        self._published_audio_frame_keys
                    ),
                    "published_audio_frame_bytes": list(
                        self._published_audio_frame_bytes
                    ),
                    "dequeued_audio_frame_keys": _frame_key_payloads(
                        self._dequeued_audio_frame_keys
                    ),
                    "dequeued_audio_frame_bytes": list(
                        self._dequeued_audio_frame_bytes
                    ),
                    "produced_parent_summaries": [
                        dict(item) for item in self._produced_parent_summaries
                    ],
                    "completed_parent_summaries": [
                        dict(item) for item in self._completed_parent_summaries
                    ],
                }
            )
        if self.config.tts_subsegment_max_chars > 0:
            result.update(
                {
                    "tts_subsegment_max_chars": (
                        self.config.tts_subsegment_max_chars
                    ),
                    "tts_subsegment_min_chars": (
                        self.config.tts_subsegment_min_chars
                    ),
                    "tts_subsegments_planned": len(
                        self._planned_subsegment_keys
                    ),
                    "tts_subsegments_produced": (
                        len(self._synthesized_subsegment_keys)
                    ),
                    "planned_subsegment_keys": _subsequence_key_payloads(
                        self._planned_subsegment_keys
                    ),
                    "synthesized_subsegment_keys": (
                        _subsequence_key_payloads(
                            self._synthesized_subsegment_keys
                        )
                    ),
                    "completed_subsegment_keys": (
                        _subsequence_key_payloads(
                            self._consumed_subsegment_keys
                        )
                    ),
                    "incomplete_subsegment_keys": (
                        _subsequence_key_payloads(
                            [
                                key
                                for key in self._planned_subsegment_keys
                                if key not in consumed_subsegment_keys
                            ]
                        )
                    ),
                }
            )
        if include_events:
            result["events"] = [event.to_dict() for event in self._telemetry]
        return result

    async def aclose(self) -> None:
        """Cancel unfinished work and release per-session threads/resources."""
        async with self._close_lock:
            if self._closed:
                return
            self._closed_event.set()
            all_tasks_done = all(task.done() for task in self._tasks)
            try:
                for task in self._tasks:
                    if not task.done():
                        task.cancel()

                must_abort_model_calls = not all_tasks_done or any(
                    not future.done() for future in self._blocking_futures
                )
                if self._owns_clients or must_abort_model_calls:
                    # Cancelled executor functions require call/channel abort.
                    # Supplied clients must be reconnected before reuse after
                    # an unfinished session is closed.
                    self._disconnect_model_clients()

                if self._asr_stream is not None:
                    try:
                        await self._asr_stream.aclose(
                            timeout_s=self.config.close_timeout_s
                        )
                    except Exception as exc:
                        self._record_cleanup_error("asr", exc)

                if self._tasks:
                    await asyncio.gather(*self._tasks, return_exceptions=True)
                blocking_stopped = await self._wait_for_blocking_futures(
                    self.config.close_timeout_s
                )
                if not blocking_stopped:
                    self._record_cleanup_error(
                        "pipeline", "BlockingWorkerTimeout"
                    )
                if self._owns_clients:
                    try:
                        await self.asr_client.aclose(
                            timeout_s=self.config.close_timeout_s
                        )
                    except Exception as exc:
                        self._record_cleanup_error("asr", exc)
            finally:
                futures_done = all(
                    future.done() for future in self._blocking_futures
                )
                for stage, executor in (
                    ("nmt", self._nmt_executor),
                    ("tts", self._tts_executor),
                ):
                    try:
                        executor.shutdown(
                            wait=futures_done, cancel_futures=True
                        )
                    except Exception as exc:
                        self._record_cleanup_error(stage, exc)
                if self._cleanup_errors and self._outcome != "failed":
                    self._outcome = "cleanup_failed"
                if self._outcome is None:
                    self._outcome = "cancelled"
                self._state = StagedPipelineState.CLOSED
                self._closed = True
                self._record(stage="pipeline", event="closed")

    async def _stage_runner(
        self, stage: str, operation: Callable[[], Any]
    ) -> None:
        try:
            await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail(
                getattr(exc, "failure_stage", stage),
                exc,
            )

    async def _consume_asr(self) -> None:
        segmenter_outcomes = []
        segmenter = PunctuationSegmenter(
            max_chars=self.config.segment_max_chars,
            max_age_ms=self.config.segment_max_age_ms,
            outcome_sink=segmenter_outcomes.append,
            punctuation_min_chars=(
                self.config.segment_punctuation_min_chars
            ),
        )
        poll_s = min(0.1, self.config.segment_max_age_ms / 1_000)
        while True:
            try:
                event = await self._asr_stream.next_event(timeout_s=poll_s)
            except asyncio.TimeoutError:
                segmenter.emit_due(self._normalized_asr_observation_ms())
                await self._handle_segmenter_outcomes(segmenter_outcomes)
                continue

            if event.kind is ASRStreamEventKind.INTERIM:
                transcript = event.transcript
                self._record(
                    stage="asr",
                    event="interim",
                    text_chars=len(transcript.text),
                    source_start_ms=transcript.source_start_ms,
                    source_end_ms=transcript.source_end_ms,
                )
                # Interims can arrive continuously, so check buffered-final age
                # after each one instead of relying only on a quiet timeout.
                segmenter.emit_due(
                    self._normalized_asr_observation_ms(
                        transcript.received_monotonic_ms
                    )
                )
                await self._handle_segmenter_outcomes(segmenter_outcomes)
            elif event.kind is ASRStreamEventKind.FINAL:
                final = event.final
                self._asr_final_attribution_records.append(
                    final_attribution_record(final)
                )
                self._record(
                    stage="asr",
                    event="final",
                    asr_final_id=final.final_id,
                    text_chars=len(final.text),
                    source_start_ms=final.source_start_ms,
                    source_end_ms=final.source_end_ms,
                )
                segmenter.push_final(
                    final,
                    observed_monotonic_ms=self._normalized_asr_observation_ms(
                        final.received_monotonic_ms
                    ),
                )
                await self._handle_segmenter_outcomes(segmenter_outcomes)
            elif event.kind is ASRStreamEventKind.COMPLETE:
                segmenter.flush(self._normalized_asr_observation_ms())
                await self._handle_segmenter_outcomes(segmenter_outcomes)
                await self._enqueue(self._nmt_queue, _DRAIN, "nmt", "drain_enqueued")
                self._record(stage="asr", event="complete")
                return
            else:
                raise StagedPipelineError(event.error or "direct ASR failed")

    def _normalized_asr_observation_ms(
        self, captured_monotonic_ms: Optional[float] = None
    ) -> float:
        """Return a nondecreasing consumer time without rewriting ASR timing.

        Direct-ASR events are timestamped in their blocking producer thread.
        Once they cross the bounded event queue, the asyncio consumer clock can
        already be slightly ahead of the next event's capture time.  Segment
        age is evaluated on this normalized consumer timeline; source word
        offsets and the event's original capture time remain unchanged.
        """
        observed_ms = self._clock_ms()
        if captured_monotonic_ms is not None:
            observed_ms = max(observed_ms, captured_monotonic_ms)
        if self._last_asr_observation_ms is not None:
            observed_ms = max(observed_ms, self._last_asr_observation_ms)
        self._last_asr_observation_ms = observed_ms
        return observed_ms

    async def _handle_segmenter_outcomes(self, outcomes: list[Any]) -> None:
        pending = tuple(outcomes)
        outcomes.clear()
        for outcome in pending:
            if isinstance(outcome, TextSegment):
                await self._emit_text_segment(outcome)
                continue
            if not isinstance(outcome, FillerDiscard):
                raise StagedPipelineError("segmenter returned an invalid outcome")
            self._fillers_discarded += 1
            self._record(
                stage="segmenter",
                event="filler_discarded",
                asr_final_id=outcome.contributing_final_ids[-1],
                contributing_final_ids=outcome.contributing_final_ids,
                source_start_ms=outcome.source_start_ms,
                source_end_ms=outcome.source_end_ms,
                text_chars=outcome.text_chars,
                monotonic_ms=outcome.discarded_monotonic_ms,
            )

    async def _emit_text_segment(self, segment: TextSegment) -> None:
        self._emitted_sequence_ids.append(segment.sequence_id)
        self._record(
            stage="segmenter",
            event="emitted",
            segment=segment,
            text_chars=len(segment.text),
        )
        await self._enqueue(self._nmt_queue, segment, "nmt", "enqueued")

    async def _run_nmt(self) -> None:
        while True:
            item = await self._nmt_queue.get()
            self._queue_slots["nmt"].release()
            try:
                if item.payload is _DRAIN:
                    await self._enqueue(
                        self._tts_queue, _DRAIN, "tts", "drain_enqueued"
                    )
                    self._record(stage="nmt", event="complete")
                    return
                segment = item.payload
                if segment.sequence_id != self._expected_nmt_sequence:
                    raise StagedPipelineError(
                        "NMT input sequence mismatch: "
                        f"expected {self._expected_nmt_sequence}, "
                        f"received {segment.sequence_id}"
                    )
                residence_ms = max(
                    0.0, self._clock_ms() - item.enqueued_monotonic_ms
                )
                self._record(
                    stage="nmt",
                    event="started",
                    segment=segment,
                    queue_depth=self._nmt_queue.qsize(),
                    queue_capacity=self._nmt_queue.maxsize,
                    queue_residence_ms=residence_ms,
                    text_chars=len(segment.text),
                )
                translation = await self._call_blocking(
                    self._nmt_executor,
                    self.nmt_client.translate_segment,
                    segment,
                    self.target_language,
                    timeout_s=self.config.nmt_rpc_timeout_s,
                    abort=lambda: _disconnect_if_connected(
                        self.nmt_client
                    ),
                )
                if not isinstance(translation, TranslatedSegment):
                    raise StagedPipelineError("NMT returned an invalid segment type")
                if translation.sequence_id != segment.sequence_id:
                    raise StagedPipelineError("NMT changed the segment sequence ID")
                if (
                    translation.subsequence_id != 0
                    or translation.subsequence_count != 1
                ):
                    contract_error = StagedPipelineError(
                        "NMT returned a non-parent composite identity"
                    )
                    contract_error.segment = segment
                    contract_error.sequence_id = segment.sequence_id
                    raise contract_error
                self._expected_nmt_sequence += 1
                self._record(
                    stage="nmt",
                    event="completed",
                    # NMT remains a parent-level stage even when its target
                    # text is subsequently divided into TTS children.
                    segment=translation.segment,
                    monotonic_ms=translation.completed_monotonic_ms,
                    text_chars=len(translation.text),
                    processing_duration_ms=translation.processing_duration_ms,
                    retry_count=translation.retry_count,
                )
                self._parent_translation_char_counts[
                    translation.sequence_id
                ] = len(translation.text)
                tts_translations = self._tts_translations(translation)
                if self.config.tts_subsegment_max_chars > 0:
                    self._planned_subsegment_keys.extend(
                        child.order_key for child in tts_translations
                    )
                for tts_translation in tts_translations:
                    if self.config.tts_subsegment_max_chars > 0:
                        self._record(
                            stage="target_splitter",
                            event="emitted",
                            segment=tts_translation,
                            text_chars=len(tts_translation.text),
                            parent_text_chars=len(translation.text),
                        )
                    await self._enqueue(
                        self._tts_queue,
                        tts_translation,
                        "tts",
                        "enqueued",
                    )
            finally:
                self._nmt_queue.task_done()

    async def _run_tts(self) -> None:
        while True:
            item = await self._tts_queue.get()
            self._queue_slots["tts"].release()
            try:
                if item.payload is _DRAIN:
                    if (
                        self._expected_tts_sequence
                        != self._expected_nmt_sequence
                        or self._expected_tts_subsequence != 0
                        or self._published_frame_count != 0
                        or self._published_frame_bytes != 0
                    ):
                        raise StagedPipelineError(
                            "TTS drain arrived before every translated "
                            "subsequence completed"
                        )
                    self._record(stage="tts", event="complete")
                    await self._emit_terminal(
                        StagedOutputEvent(kind=StagedOutputEventKind.COMPLETE)
                    )
                    return
                translation = item.payload
                try:
                    self._validate_tts_cursor(translation)
                except Exception as exc:
                    if isinstance(translation, TranslatedSegment):
                        attributed = _retain_translation_identity(
                            exc,
                            translation,
                        )
                        if attributed is not exc:
                            raise attributed from exc
                    raise
                residence_ms = max(
                    0.0, self._clock_ms() - item.enqueued_monotonic_ms
                )
                self._record(
                    stage="tts",
                    event="started",
                    segment=translation,
                    queue_depth=self._tts_queue.qsize(),
                    queue_capacity=self._tts_queue.maxsize,
                    queue_residence_ms=residence_ms,
                    text_chars=len(translation.text),
                    parent_text_chars=self._parent_translation_chars(
                        translation
                    ),
                )
                timeout_retry_count = 0
                publisher = None
                if self.config.tts_incremental_publish_enabled:
                    publisher = _ThreadsafeFramePublisher(
                        self,
                        asyncio.get_running_loop(),
                    )
                    self._active_tts_publisher = publisher

                def abort_tts() -> None:
                    nonlocal timeout_retry_count
                    value = getattr(self.tts_client, "active_retry_count", 0)
                    timeout_retry_count = (
                        value
                        if isinstance(value, int)
                        and not isinstance(value, bool)
                        and value in {0, 1}
                        else 0
                    )
                    if publisher is not None:
                        publisher.abort()
                    _disconnect_if_connected(self.tts_client)

                try:
                    if publisher is None:
                        synthesized = await self._call_blocking(
                            self._tts_executor,
                            self.tts_client.synthesize,
                            translation,
                            timeout_s=self.config.tts_rpc_timeout_s,
                            abort=abort_tts,
                        )
                    else:
                        synthesized = await self._call_blocking(
                            self._tts_executor,
                            self.tts_client.synthesize_incremental,
                            translation,
                            publisher.publish,
                            timeout_s=self.config.tts_rpc_timeout_s,
                            abort=abort_tts,
                        )
                except asyncio.TimeoutError as exc:
                    raise StagedModelTimeoutError(
                        stage="tts",
                        translation=translation,
                        timeout_s=self.config.tts_rpc_timeout_s,
                        retry_count=timeout_retry_count,
                    ) from exc
                except Exception as exc:
                    attributed = _retain_translation_identity(
                        exc,
                        translation,
                    )
                    if attributed is exc:
                        raise
                    raise attributed from exc
                finally:
                    if self._active_tts_publisher is publisher:
                        self._active_tts_publisher = None
                expected_type = (
                    SynthesizedStreamCompletion
                    if self.config.tts_incremental_publish_enabled
                    else SynthesizedSegment
                )
                if not isinstance(synthesized, expected_type):
                    contract_error = StagedPipelineError(
                        "TTS returned an invalid output type"
                    )
                    _retain_translation_identity(
                        contract_error,
                        translation,
                    )
                    raise contract_error
                if synthesized.order_key != translation.order_key:
                    contract_error = StagedPipelineError(
                        "TTS changed the segment composite identity"
                    )
                    _retain_translation_identity(
                        contract_error,
                        translation,
                    )
                    raise contract_error
                if isinstance(synthesized, SynthesizedStreamCompletion):
                    normalized_target = validate_target_text(
                        translation.text,
                        language=translation.language,
                        sequence_id=translation.sequence_id,
                    )
                    fallback_threshold = (
                        self.config.tts_incremental_atomic_fallback_max_chars
                    )
                    expected_atomic_fallback = (
                        fallback_threshold > 0
                        and len(normalized_target) <= fallback_threshold
                    )
                    if (
                        synthesized.atomic_fallback_applied
                        is not expected_atomic_fallback
                    ):
                        contract_error = StagedPipelineError(
                            "incremental TTS atomic fallback attribution did "
                            "not match the configured target-length policy"
                        )
                        _retain_translation_identity(
                            contract_error,
                            translation,
                        )
                        raise contract_error
                    if (
                        synthesized.audio_frame_count
                        != self._published_frame_count
                        or synthesized.audio_bytes
                        != self._published_frame_bytes
                        or self._published_frame_retry_count is None
                        or synthesized.retry_count
                        != self._published_frame_retry_count
                    ):
                        contract_error = StagedPipelineError(
                            "incremental TTS completion did not reconcile "
                            "committed frame totals"
                        )
                        _retain_translation_identity(
                            contract_error,
                            translation,
                        )
                        raise contract_error
                    current_frame_sizes = self._published_audio_frame_bytes[
                        -self._published_frame_count :
                    ]
                    if (
                        any(
                            size != self._incremental_frame_bytes
                            for size in current_frame_sizes[:-1]
                        )
                        or current_frame_sizes[-1]
                        > self._incremental_frame_bytes
                    ):
                        contract_error = StagedPipelineError(
                            "incremental TTS frame sizes did not reconcile "
                            "with the configured framing policy"
                        )
                        _retain_translation_identity(
                            contract_error,
                            translation,
                        )
                        raise contract_error
                if self.config.tts_response_chunk_telemetry_enabled:
                    if not synthesized.response_chunks:
                        contract_error = StagedPipelineError(
                            "TTS response-chunk telemetry was enabled but "
                            "the adapter returned no response metrics"
                        )
                        _retain_translation_identity(
                            contract_error,
                            translation,
                        )
                        raise contract_error
                    self._capture_tts_response_chunks(synthesized)
                self._record(
                    stage="tts",
                    event="first_audio",
                    segment=synthesized,
                    monotonic_ms=synthesized.first_audio_monotonic_ms,
                    processing_duration_ms=synthesized.first_audio_latency_ms,
                )
                self._record(
                    stage="tts",
                    event="completed",
                    segment=synthesized,
                    monotonic_ms=synthesized.completed_monotonic_ms,
                    audio_bytes=(
                        len(synthesized.audio)
                        if isinstance(synthesized, SynthesizedSegment)
                        else synthesized.audio_bytes
                    ),
                    audio_duration_ms=synthesized.audio_duration_ms,
                    processing_duration_ms=synthesized.processing_duration_ms,
                    retry_count=synthesized.retry_count,
                )
                self._synthesized_subsegment_keys.append(
                    synthesized.order_key
                )
                if isinstance(synthesized, SynthesizedStreamCompletion):
                    await self._enqueue(
                        self._output_queue,
                        StagedOutputEvent(
                            kind=StagedOutputEventKind.PARENT_COMPLETE,
                            completion=synthesized,
                        ),
                        "output",
                        "parent_complete_enqueued",
                    )
                    self._produced_parent_summaries.append(
                        _parent_completion_payload(synthesized)
                    )
                    self._published_frame_count = 0
                    self._published_frame_bytes = 0
                    self._published_frame_retry_count = None
                else:
                    await self._enqueue(
                        self._output_queue,
                        StagedOutputEvent(
                            kind=StagedOutputEventKind.AUDIO,
                            segment=synthesized,
                        ),
                        "output",
                        "enqueued",
                    )
                self._advance_tts_cursor(translation)
                self._audio_segments_produced += 1
            finally:
                self._tts_queue.task_done()

    def _tts_translations(
        self,
        translation: TranslatedSegment,
    ) -> Tuple[TranslatedSegment, ...]:
        max_chars = self.config.tts_subsegment_max_chars
        if max_chars == 0:
            return (translation,)
        try:
            chunks = split_target_text(
                translation.text,
                max_chars=max_chars,
                min_chars=self.config.tts_subsegment_min_chars,
            )
            count = len(chunks)
            if count <= 0:
                raise StagedPipelineError(
                    "target-text splitter returned no TTS subsequences"
                )
            return tuple(
                replace(
                    translation,
                    text=chunk.text,
                    subsequence_id=index,
                    subsequence_count=count,
                )
                for index, chunk in enumerate(chunks)
            )
        except Exception as exc:
            raise StagedTargetSplitError(exc, translation) from exc

    def _parent_translation_chars(
        self,
        translation: TranslatedSegment,
    ) -> Optional[int]:
        if self.config.tts_subsegment_max_chars == 0:
            return None
        return self._parent_translation_char_counts.get(
            translation.sequence_id
        )

    def _capture_tts_response_chunks(
        self,
        synthesized: Any,
    ) -> None:
        """Retain timing/size evidence without retaining PCM or text."""
        bytes_per_second = (
            synthesized.sample_rate_hz
            * synthesized.channels
            * synthesized.bytes_per_sample
        )
        response_count = len(synthesized.response_chunks)
        previous_received_ms = synthesized.started_monotonic_ms
        for chunk in synthesized.response_chunks:
            self._tts_response_chunk_metrics.append(
                {
                    "parent_sequence_id": synthesized.parent_sequence_id,
                    "subsequence_id": synthesized.subsequence_id,
                    "subsequence_count": synthesized.subsequence_count,
                    "response_index": chunk.response_index,
                    "response_count": response_count,
                    "audio_bytes": chunk.audio_bytes,
                    "cumulative_audio_bytes": (
                        chunk.cumulative_audio_bytes
                    ),
                    "audio_duration_ms": (
                        chunk.audio_bytes / bytes_per_second * 1_000
                    ),
                    "cumulative_audio_duration_ms": (
                        chunk.cumulative_audio_bytes
                        / bytes_per_second
                        * 1_000
                    ),
                    "received_monotonic_ms": (
                        chunk.received_monotonic_ms
                    ),
                    "since_request_start_ms": (
                        chunk.received_monotonic_ms
                        - synthesized.started_monotonic_ms
                    ),
                    "since_previous_response_ms": (
                        chunk.received_monotonic_ms
                        - previous_received_ms
                    ),
                    "retry_count": chunk.retry_count,
                }
            )
            previous_received_ms = chunk.received_monotonic_ms
        self._tts_response_segments_observed += 1

    async def _enqueue_incremental_frame(
        self,
        frame: SynthesizedAudioFrame,
        *,
        abort_event: asyncio.Event,
        publish_requested_monotonic_ms: Optional[float] = None,
    ) -> bool:
        """Commit one frame or return ``False`` before consuming capacity."""
        handoff_telemetry_enabled = (
            self.config.tts_publisher_handoff_telemetry_enabled
        )
        event_loop_callback_started_ms = (
            self._clock_ms() if handoff_telemetry_enabled else None
        )
        if handoff_telemetry_enabled and publish_requested_monotonic_ms is None:
            raise StagedPipelineError(
                "publisher handoff telemetry requires a request timestamp"
            )
        if (
            not handoff_telemetry_enabled
            and publish_requested_monotonic_ms is not None
        ):
            raise StagedPipelineError(
                "publisher handoff timestamp received while telemetry is disabled"
            )
        if not self.config.tts_incremental_publish_enabled:
            raise StagedPipelineError(
                "incremental frame received while schema 3 is disabled"
            )
        if not isinstance(frame, SynthesizedAudioFrame):
            raise StagedPipelineError(
                "incremental publisher received an invalid frame type"
            )
        self._validate_published_frame(frame)
        if abort_event.is_set():
            return False

        slots = self._queue_slots["output"]
        requested_ms = (
            event_loop_callback_started_ms
            if handoff_telemetry_enabled
            else self._clock_ms()
        )
        was_full = slots.locked()
        acquire_task = asyncio.create_task(slots.acquire())
        abort_task = asyncio.create_task(abort_event.wait())
        try:
            done, _ = await asyncio.wait(
                {acquire_task, abort_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            acquire_task.cancel()
            abort_task.cancel()
            await asyncio.gather(
                acquire_task,
                abort_task,
                return_exceptions=True,
            )
            raise

        # Queue insertion, not semaphore acquisition, is the commit boundary.
        # If abort and capacity become ready in the same event-loop turn, abort
        # must win so a reserved ERROR can never overtake an uncommitted frame.
        if abort_event.is_set() or abort_task in done:
            if not acquire_task.done():
                acquire_task.cancel()
            acquire_result = (
                await asyncio.gather(
                    acquire_task,
                    return_exceptions=True,
                )
            )[0]
            if acquire_result is True:
                slots.release()
            if not abort_task.done():
                abort_task.cancel()
                await asyncio.gather(
                    abort_task,
                    return_exceptions=True,
                )
            return False

        # From here through put_nowait no await is allowed: callback success is
        # the commit linearization point.
        if acquire_task not in done or acquire_task.cancelled():
            acquire_task.cancel()
            await asyncio.gather(acquire_task, return_exceptions=True)
            return False
        abort_task.cancel()
        capacity_acquired_ms = self._clock_ms()
        blocked_ms = (
            max(0.0, capacity_acquired_ms - requested_ms)
            if was_full
            else 0.0
        )
        output = StagedOutputEvent(
            kind=StagedOutputEventKind.AUDIO_FRAME,
            frame=frame,
        )
        depth = self._output_queue.qsize() + 1
        try:
            self._record(
                stage="tts",
                event="frame_received",
                segment=frame,
                monotonic_ms=frame.received_monotonic_ms,
                audio_bytes=len(frame.audio),
                audio_duration_ms=frame.audio_duration_ms,
                retry_count=frame.retry_count,
            )
            enqueue_record_candidate = self._build_record(
                stage="output",
                event="frame_enqueued",
                segment=frame,
                queue_depth=depth,
                queue_capacity=self.config.output_queue_maxsize,
                blocked_put_ms=blocked_ms,
                audio_bytes=len(frame.audio),
                audio_duration_ms=frame.audio_duration_ms,
                retry_count=frame.retry_count,
                monotonic_ms=capacity_acquired_ms,
                publish_requested_monotonic_ms=(
                    publish_requested_monotonic_ms
                ),
                event_loop_callback_started_monotonic_ms=(
                    event_loop_callback_started_ms
                ),
                output_capacity_acquired_monotonic_ms=(
                    capacity_acquired_ms
                    if handoff_telemetry_enabled
                    else None
                ),
            )
            # All validation and potentially fallible telemetry bookkeeping is
            # complete before this timestamp. Queue insertion is the commit
            # boundary and is the immediately following operation.
            enqueued_ms = self._clock_ms()
            enqueue_record = replace(
                enqueue_record_candidate,
                monotonic_ms=enqueued_ms,
            )
            self._output_queue.put_nowait(_QueuedItem(output, enqueued_ms))
        except Exception:
            slots.release()
            raise

        # The frame is committed. A best-effort external telemetry sink must
        # never turn that successful commit into a worker-visible failure that
        # could cause the same PCM frame to be retried.
        self._emit_record(enqueue_record, sink_errors_fatal=False)
        if was_full:
            self._blocked_put_counts["output"] += 1
        self._max_queue_depths["output"] = max(
            self._max_queue_depths["output"],
            depth,
        )
        self._published_audio_frame_keys.append(frame.frame_key)
        self._published_audio_frame_bytes.append(len(frame.audio))
        self._published_frame_count += 1
        self._published_frame_bytes += len(frame.audio)
        if self._published_frame_retry_count is None:
            self._published_frame_retry_count = frame.retry_count
        self._audio_frames_produced += 1
        return True

    def _validate_published_frame(self, frame: SynthesizedAudioFrame) -> None:
        if frame.parent_sequence_id != self._expected_tts_sequence:
            raise StagedPipelineError(
                "incremental TTS frame parent mismatch: expected "
                f"{self._expected_tts_sequence}, received "
                f"{frame.parent_sequence_id}"
            )
        if frame.audio_frame_id != self._published_frame_count:
            raise StagedPipelineError(
                "incremental TTS frame identity mismatch: expected "
                f"{self._published_frame_count}, received "
                f"{frame.audio_frame_id}"
            )
        if (
            frame.sample_rate_hz != audio_config.sample_rate
            or frame.channels != audio_config.channels
            or frame.bytes_per_sample != audio_config.bytes_per_sample
        ):
            raise StagedPipelineError(
                "incremental TTS frame audio format changed"
            )
        if len(frame.audio) > self._incremental_frame_bytes:
            raise StagedPipelineError(
                "incremental TTS frame exceeded configured frame bytes"
            )
        if (
            self._published_frame_count > 0
            and self._published_audio_frame_bytes[-1]
            < self._incremental_frame_bytes
        ):
            raise StagedPipelineError(
                "incremental TTS published audio after a short final frame"
            )
        if (
            self._published_frame_retry_count is not None
            and frame.retry_count != self._published_frame_retry_count
        ):
            raise StagedPipelineError(
                "incremental TTS retry count changed within a parent"
            )

    def _validate_output_frame(self, frame: SynthesizedAudioFrame) -> None:
        if not isinstance(frame, SynthesizedAudioFrame):
            raise StagedPipelineError(
                "output queue contained an invalid incremental frame"
            )
        if frame.parent_sequence_id != self._expected_output_sequence:
            raise StagedPipelineError(
                "output frame parent mismatch: expected "
                f"{self._expected_output_sequence}, received "
                f"{frame.parent_sequence_id}"
            )
        if frame.audio_frame_id != self._expected_output_frame_id:
            raise StagedPipelineError(
                "output frame identity mismatch: expected "
                f"{self._expected_output_frame_id}, received "
                f"{frame.audio_frame_id}"
            )
        if (
            frame.sample_rate_hz != audio_config.sample_rate
            or frame.channels != audio_config.channels
            or frame.bytes_per_sample != audio_config.bytes_per_sample
        ):
            raise StagedPipelineError("output frame audio format changed")
        if len(frame.audio) > self._incremental_frame_bytes:
            raise StagedPipelineError(
                "output frame exceeded configured frame bytes"
            )
        if (
            self._expected_output_frame_id > 0
            and self._dequeued_audio_frame_bytes[-1]
            < self._incremental_frame_bytes
        ):
            raise StagedPipelineError(
                "output audio followed a short final frame"
            )
        if (
            self._expected_output_frame_retry_count is not None
            and frame.retry_count != self._expected_output_frame_retry_count
        ):
            raise StagedPipelineError(
                "output frame retry count changed within a parent"
            )
        if self._expected_output_frame_retry_count is None:
            self._expected_output_frame_retry_count = frame.retry_count

    def _validate_output_parent_completion(
        self,
        completion: SynthesizedStreamCompletion,
    ) -> None:
        if not isinstance(completion, SynthesizedStreamCompletion):
            raise StagedPipelineError(
                "output queue contained an invalid parent completion"
            )
        if completion.parent_sequence_id != self._expected_output_sequence:
            raise StagedPipelineError(
                "output parent completion sequence mismatch"
            )
        if completion.audio_frame_count != self._expected_output_frame_id:
            raise StagedPipelineError(
                "output parent completion frame count mismatch"
            )
        if completion.audio_bytes != self._expected_output_frame_bytes:
            raise StagedPipelineError(
                "output parent completion byte count mismatch"
            )
        if (
            completion.sample_rate_hz != audio_config.sample_rate
            or completion.channels != audio_config.channels
            or completion.bytes_per_sample != audio_config.bytes_per_sample
        ):
            raise StagedPipelineError(
                "output parent completion audio format changed"
            )
        if (
            self._expected_output_frame_retry_count is None
            or completion.retry_count
            != self._expected_output_frame_retry_count
        ):
            raise StagedPipelineError(
                "output parent completion retry count mismatch"
            )

    def _validate_tts_cursor(self, translation: TranslatedSegment) -> None:
        if not isinstance(translation, TranslatedSegment):
            raise StagedPipelineError(
                "TTS queue contained an invalid translated segment"
            )
        self._validate_subsequence_cursor(
            stage="TTS",
            sequence_id=translation.sequence_id,
            subsequence_id=translation.subsequence_id,
            subsequence_count=translation.subsequence_count,
            expected_sequence=self._expected_tts_sequence,
            expected_subsequence=self._expected_tts_subsequence,
            expected_count=self._expected_tts_subsequence_count,
        )
        if self._expected_tts_subsequence == 0:
            self._expected_tts_subsequence_count = (
                translation.subsequence_count
            )

    def _advance_tts_cursor(self, translation: TranslatedSegment) -> None:
        (
            self._expected_tts_sequence,
            self._expected_tts_subsequence,
            self._expected_tts_subsequence_count,
        ) = _advanced_subsequence_cursor(translation)

    def _advance_output_cursor(self, segment: SynthesizedSegment) -> None:
        self._validate_subsequence_cursor(
            stage="output",
            sequence_id=segment.sequence_id,
            subsequence_id=segment.subsequence_id,
            subsequence_count=segment.subsequence_count,
            expected_sequence=self._expected_output_sequence,
            expected_subsequence=self._expected_output_subsequence,
            expected_count=self._expected_output_subsequence_count,
        )
        if self._expected_output_subsequence == 0:
            self._expected_output_subsequence_count = (
                segment.subsequence_count
            )
        (
            self._expected_output_sequence,
            self._expected_output_subsequence,
            self._expected_output_subsequence_count,
        ) = _advanced_subsequence_cursor(segment)

    @staticmethod
    def _validate_subsequence_cursor(
        *,
        stage: str,
        sequence_id: int,
        subsequence_id: int,
        subsequence_count: int,
        expected_sequence: int,
        expected_subsequence: int,
        expected_count: Optional[int],
    ) -> None:
        if (
            sequence_id != expected_sequence
            or subsequence_id != expected_subsequence
        ):
            raise StagedPipelineError(
                f"{stage} input composite identity mismatch: expected "
                f"({expected_sequence}, {expected_subsequence}), received "
                f"({sequence_id}, {subsequence_id})"
            )
        if (
            expected_subsequence > 0
            and expected_count is not None
            and subsequence_count != expected_count
        ):
            raise StagedPipelineError(
                f"{stage} subsequence_count changed within parent "
                f"{sequence_id}: expected {expected_count}, received "
                f"{subsequence_count}"
            )

    async def _call_blocking(
        self,
        executor: ThreadPoolExecutor,
        function: Callable[..., Any],
        *args: Any,
        timeout_s: float,
        abort: Callable[[], None],
    ) -> Any:
        future = executor.submit(function, *args)
        self._blocking_futures.add(future)
        deadline = asyncio.get_running_loop().time() + timeout_s
        try:
            while not future.done():
                if asyncio.get_running_loop().time() >= deadline:
                    abort()
                    future.cancel()
                    raise asyncio.TimeoutError(
                        f"model RPC exceeded {timeout_s:.3f} seconds"
                    )
                await asyncio.sleep(0.01)
            return future.result()
        except asyncio.CancelledError:
            abort()
            future.cancel()
            raise
        finally:
            if future.done():
                self._blocking_futures.discard(future)

    async def _enqueue(
        self,
        queue: asyncio.Queue,
        payload: Any,
        queue_name: str,
        event: str,
    ) -> None:
        requested_ms = self._clock_ms()
        slots = self._queue_slots[queue_name]
        is_reserved_terminal = (
            queue_name == "output"
            and isinstance(payload, StagedOutputEvent)
            and payload.kind
            in {
                StagedOutputEventKind.COMPLETE,
                StagedOutputEventKind.ERROR,
            }
        )
        was_full = slots.locked() if not is_reserved_terminal else False
        if not is_reserved_terminal:
            await slots.acquire()
        accepted_ms = self._clock_ms()
        blocked_ms = max(0.0, accepted_ms - requested_ms) if was_full else 0.0
        queue.put_nowait(_QueuedItem(payload, accepted_ms))
        if was_full:
            self._blocked_put_counts[queue_name] += 1
        depth = queue.qsize()
        if not is_reserved_terminal:
            self._max_queue_depths[queue_name] = max(
                self._max_queue_depths[queue_name], depth
            )
        output_payload = (
            payload.segment or payload.frame or payload.completion
            if isinstance(payload, StagedOutputEvent)
            else None
        )
        audio = (
            output_payload
            if isinstance(
                output_payload,
                (SynthesizedSegment, SynthesizedAudioFrame),
            )
            else None
        )
        self._record(
            stage=queue_name,
            event=event,
            segment=output_payload if output_payload is not None else payload,
            queue_depth=depth,
            queue_capacity=(
                self.config.output_queue_maxsize
                if queue_name == "output" and not is_reserved_terminal
                else queue.maxsize
            ),
            blocked_put_ms=blocked_ms,
            text_chars=_text_chars(payload),
            audio_bytes=(
                len(audio.audio)
                if audio is not None
                else (
                    output_payload.audio_bytes
                    if isinstance(
                        output_payload,
                        SynthesizedStreamCompletion,
                    )
                    else 0
                )
            ),
            audio_duration_ms=(
                output_payload.audio_duration_ms
                if output_payload is not None
                else 0.0
            ),
            retry_count=(
                output_payload.retry_count
                if isinstance(
                    output_payload,
                    (
                        SynthesizedAudioFrame,
                        SynthesizedStreamCompletion,
                    ),
                )
                else 0
            ),
        )

    async def _emit_terminal(self, output: StagedOutputEvent) -> None:
        async with self._failure_lock:
            if self._terminal_queued.is_set():
                return
            await self._enqueue(
                self._output_queue, output, "output", output.kind.value
            )
            if output.kind is StagedOutputEventKind.COMPLETE:
                self._outcome = "complete"
                self._state = StagedPipelineState.COMPLETE
                self._record(stage="pipeline", event="complete")
            self._terminal_queued.set()

    async def _fail(self, stage: str, exc: Exception) -> None:
        async with self._failure_lock:
            if self._failure is not None or self._terminal_queued.is_set():
                return
            message = str(exc) or type(exc).__name__
            self._failure = (stage, message)
            self._outcome = "failed"
            self._state = StagedPipelineState.FAILED
            failure_translation = getattr(exc, "translation", None)
            failure_segment = (
                failure_translation
                if isinstance(failure_translation, TranslatedSegment)
                and getattr(exc, "subsequence_id", None) is not None
                else None
            )
            if failure_segment is None:
                failure_segment = getattr(exc, "segment", None)
            if not isinstance(
                failure_segment,
                (TextSegment, TranslatedSegment),
            ):
                failure_segment = None
            retry_count = getattr(exc, "retry_count", 0)
            if (
                not isinstance(retry_count, int)
                or isinstance(retry_count, bool)
                or retry_count < 0
            ):
                retry_count = 0
            self._record(
                stage=stage,
                event="error",
                segment=failure_segment,
                retry_count=retry_count,
                error_code=getattr(
                    exc,
                    "original_error_code",
                    type(exc).__name__,
                ),
            )
            current = asyncio.current_task()
            for task in self._tasks:
                if task is not current and not task.done():
                    task.cancel()
            # Sibling executor calls continue running after asyncio task
            # cancellation unless their retained RPC/channel is aborted.
            self._disconnect_model_clients()
            await self._enqueue(
                self._output_queue,
                StagedOutputEvent(
                    kind=StagedOutputEventKind.ERROR,
                    stage=stage,
                    error=message,
                ),
                "output",
                "error",
            )
            self._terminal_queued.set()

    async def _wait_for_output_or_close(self) -> _QueuedItem:
        # Polling keeps queue removal in this task. A timeout/cancellation can
        # therefore never race a child ``queue.get()`` that consumes and loses
        # an item before the output semaphore is released.
        while True:
            if self._closed_event.is_set():
                raise StagedPipelineError(
                    "staged session closed while awaiting output"
                )
            try:
                return self._output_queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.01)

    async def _wait_for_terminal_or_close(self) -> None:
        while not self._terminal_queued.is_set():
            if self._closed_event.is_set():
                raise StagedPipelineError(
                    "staged session closed before a terminal event"
                )
            await asyncio.sleep(0.01)

    def _disconnect_model_clients(self) -> None:
        publisher = self._active_tts_publisher
        if publisher is not None:
            publisher.abort()
        for stage, client in (
            ("nmt", self.nmt_client),
            ("tts", self.tts_client),
        ):
            if not _is_connected(client):
                continue
            try:
                client.disconnect()
            except Exception as exc:
                self._record_cleanup_error(stage, exc)

    def _record_cleanup_error(self, stage: str, error: object) -> None:
        error_code = error if isinstance(error, str) else type(error).__name__
        message = str(error)
        self._cleanup_errors.append(
            {"stage": stage, "code": str(error_code), "error": message}
        )
        self._record(
            stage=stage,
            event="close_error",
            error_code=str(error_code),
        )

    async def _wait_for_blocking_futures(self, timeout_s: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            completed = {future for future in self._blocking_futures if future.done()}
            self._blocking_futures.difference_update(completed)
            if not self._blocking_futures:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.01)

    def _build_record(
        self,
        *,
        stage: str,
        event: str,
        segment: Any = None,
        asr_final_id: Optional[int] = None,
        contributing_final_ids: Tuple[int, ...] = (),
        source_start_ms: Optional[float] = None,
        source_end_ms: Optional[float] = None,
        queue_depth: Optional[int] = None,
        queue_capacity: Optional[int] = None,
        queue_residence_ms: float = 0.0,
        processing_duration_ms: float = 0.0,
        blocked_put_ms: float = 0.0,
        text_chars: int = 0,
        parent_text_chars: Optional[int] = None,
        audio_bytes: int = 0,
        audio_duration_ms: float = 0.0,
        retry_count: int = 0,
        error_code: str = "",
        monotonic_ms: Optional[float] = None,
        publish_requested_monotonic_ms: Optional[float] = None,
        event_loop_callback_started_monotonic_ms: Optional[float] = None,
        output_capacity_acquired_monotonic_ms: Optional[float] = None,
    ) -> PipelineEvent:
        source = _source_segment(segment)
        subsequence = (
            _subsequence_identity(segment)
            if self.config.tts_subsegment_max_chars > 0
            else None
        )
        audio_frame_id = (
            segment.audio_frame_id
            if isinstance(segment, SynthesizedAudioFrame)
            else None
        )
        audio_frame_count = (
            segment.audio_frame_count
            if isinstance(segment, SynthesizedStreamCompletion)
            else None
        )
        atomic_fallback_applied = (
            segment.atomic_fallback_applied
            if isinstance(segment, SynthesizedStreamCompletion)
            else None
        )
        return PipelineEvent(
            session_id=self.session_id,
            stage=stage,
            event=event,
            monotonic_ms=(self._clock_ms() if monotonic_ms is None else monotonic_ms),
            sequence_id=source.sequence_id if source is not None else None,
            subsequence_id=(
                subsequence[1] if subsequence is not None else None
            ),
            subsequence_count=(
                subsequence[2] if subsequence is not None else None
            ),
            asr_final_id=asr_final_id,
            contributing_final_ids=(
                source.contributing_final_ids
                if source is not None
                else contributing_final_ids
            ),
            emission_reason=source.reason if source is not None else None,
            source_start_ms=(
                source.source_start_ms if source is not None else source_start_ms
            ),
            source_end_ms=(
                source.source_end_ms if source is not None else source_end_ms
            ),
            queue_depth=queue_depth,
            queue_capacity=queue_capacity,
            queue_residence_ms=queue_residence_ms,
            processing_duration_ms=processing_duration_ms,
            blocked_put_ms=blocked_put_ms,
            text_chars=text_chars,
            parent_text_chars=parent_text_chars,
            audio_bytes=audio_bytes,
            audio_duration_ms=audio_duration_ms,
            audio_frame_id=audio_frame_id,
            audio_frame_count=audio_frame_count,
            atomic_fallback_applied=atomic_fallback_applied,
            retry_count=retry_count,
            error_code=error_code,
            publish_requested_monotonic_ms=(
                publish_requested_monotonic_ms
            ),
            event_loop_callback_started_monotonic_ms=(
                event_loop_callback_started_monotonic_ms
            ),
            output_capacity_acquired_monotonic_ms=(
                output_capacity_acquired_monotonic_ms
            ),
        )

    def _emit_record(
        self,
        record: PipelineEvent,
        *,
        sink_errors_fatal: bool = True,
    ) -> None:
        if self._retain_telemetry:
            self._telemetry.append(record)
        if self._event_sink is not None:
            try:
                self._event_sink(record)
            except Exception as exc:
                if sink_errors_fatal:
                    raise
                self._cleanup_errors.append(
                    {
                        "stage": "telemetry",
                        "code": type(exc).__name__,
                        "error": (
                            "event sink failed after committed output frame"
                        ),
                    }
                )

    def _record(
        self,
        *,
        stage: str,
        event: str,
        segment: Any = None,
        asr_final_id: Optional[int] = None,
        contributing_final_ids: Tuple[int, ...] = (),
        source_start_ms: Optional[float] = None,
        source_end_ms: Optional[float] = None,
        queue_depth: Optional[int] = None,
        queue_capacity: Optional[int] = None,
        queue_residence_ms: float = 0.0,
        processing_duration_ms: float = 0.0,
        blocked_put_ms: float = 0.0,
        text_chars: int = 0,
        parent_text_chars: Optional[int] = None,
        audio_bytes: int = 0,
        audio_duration_ms: float = 0.0,
        retry_count: int = 0,
        error_code: str = "",
        monotonic_ms: Optional[float] = None,
        publish_requested_monotonic_ms: Optional[float] = None,
        event_loop_callback_started_monotonic_ms: Optional[float] = None,
        output_capacity_acquired_monotonic_ms: Optional[float] = None,
    ) -> None:
        record = self._build_record(
            stage=stage,
            event=event,
            segment=segment,
            asr_final_id=asr_final_id,
            contributing_final_ids=contributing_final_ids,
            source_start_ms=source_start_ms,
            source_end_ms=source_end_ms,
            queue_depth=queue_depth,
            queue_capacity=queue_capacity,
            queue_residence_ms=queue_residence_ms,
            processing_duration_ms=processing_duration_ms,
            blocked_put_ms=blocked_put_ms,
            text_chars=text_chars,
            parent_text_chars=parent_text_chars,
            audio_bytes=audio_bytes,
            audio_duration_ms=audio_duration_ms,
            retry_count=retry_count,
            error_code=error_code,
            monotonic_ms=monotonic_ms,
            publish_requested_monotonic_ms=(
                publish_requested_monotonic_ms
            ),
            event_loop_callback_started_monotonic_ms=(
                event_loop_callback_started_monotonic_ms
            ),
            output_capacity_acquired_monotonic_ms=(
                output_capacity_acquired_monotonic_ms
            ),
        )
        self._emit_record(record)


def _is_connected(client: Any) -> bool:
    check = getattr(client, "is_connected", None)
    return bool(check()) if callable(check) else False


def _disconnect_if_connected(client: Any) -> None:
    """Disconnect once when concurrent cancellation paths converge."""
    if _is_connected(client):
        client.disconnect()


def _source_segment(value: Any) -> Optional[TextSegment]:
    if isinstance(value, TextSegment):
        return value
    if isinstance(value, TranslatedSegment):
        return value.segment
    if isinstance(value, SynthesizedSegment):
        return value.translation.segment
    if isinstance(value, (SynthesizedAudioFrame, SynthesizedStreamCompletion)):
        return value.translation.segment
    if isinstance(value, StagedOutputEvent):
        payload = value.segment or value.frame or value.completion
        if payload is not None:
            return payload.translation.segment
    return None


def _subsequence_identity(
    value: Any,
) -> Optional[Tuple[int, int, int]]:
    if isinstance(value, TranslatedSegment):
        return value.order_key
    if isinstance(value, SynthesizedSegment):
        return value.order_key
    if isinstance(value, StagedOutputEvent) and value.segment is not None:
        return value.segment.order_key
    return None


def _advanced_subsequence_cursor(
    value: Any,
) -> Tuple[int, int, Optional[int]]:
    if value.subsequence_id + 1 < value.subsequence_count:
        return (
            value.sequence_id,
            value.subsequence_id + 1,
            value.subsequence_count,
        )
    return (value.sequence_id + 1, 0, None)


def _subsequence_key_payloads(
    keys: list[Tuple[int, int, int]],
) -> list[Dict[str, int]]:
    return [
        {
            "parent_sequence_id": parent_sequence_id,
            "subsequence_id": subsequence_id,
            "subsequence_count": subsequence_count,
        }
        for parent_sequence_id, subsequence_id, subsequence_count in keys
    ]


def _frame_key_payloads(
    keys: list[Tuple[int, int]],
) -> list[Dict[str, int]]:
    return [
        {
            "parent_sequence_id": parent_sequence_id,
            "audio_frame_id": audio_frame_id,
        }
        for parent_sequence_id, audio_frame_id in keys
    ]


def _parent_completion_payload(
    completion: SynthesizedStreamCompletion,
) -> Dict[str, Any]:
    return {
        "parent_sequence_id": completion.parent_sequence_id,
        "audio_frame_count": completion.audio_frame_count,
        "audio_bytes": completion.audio_bytes,
        "retry_count": completion.retry_count,
        "atomic_fallback_applied": completion.atomic_fallback_applied,
    }


def _retain_translation_identity(
    exc: Exception,
    translation: TranslatedSegment,
) -> Exception:
    attributes = {
        "translation": translation,
        "segment": translation.segment,
        "sequence_id": translation.sequence_id,
        "parent_sequence_id": translation.sequence_id,
        "subsequence_id": translation.subsequence_id,
        "subsequence_count": translation.subsequence_count,
    }
    try:
        for name, value in attributes.items():
            setattr(exc, name, value)
    except Exception:
        return StagedAttributedModelError(exc, translation)
    return exc


def _text_chars(value: Any) -> int:
    if isinstance(value, TextSegment):
        return len(value.text)
    if isinstance(value, TranslatedSegment):
        return len(value.text)
    if isinstance(value, SynthesizedSegment):
        return len(value.translation.text)
    if isinstance(value, (SynthesizedAudioFrame, SynthesizedStreamCompletion)):
        return len(value.translation.text)
    if isinstance(value, StagedOutputEvent):
        payload = value.segment or value.frame or value.completion
        if payload is not None:
            return len(payload.translation.text)
    return 0
