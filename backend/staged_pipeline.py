"""Bounded, ordered ASR -> NMT -> TTS orchestration.

This module is the application boundary for the direct NIM adapters.  The
pipeline is available to the browser WebSocket route only when the explicit
``S2S_PIPELINE_MODE=staged`` feature flag is selected; the monolithic route
remains the default.  One worker per model preserves source order while still
allowing NMT for segment ``n + 1`` to overlap TTS for segment ``n``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple

from config import SUPPORTED_LANGUAGES, StagedPipelineConfig, staged_pipeline_config
from punctuation_segmenter import FillerDiscard, PunctuationSegmenter
from staged_models import (
    ASRStreamEventKind,
    PipelineEvent,
    StagedOutputEvent,
    StagedOutputEventKind,
    SynthesizedSegment,
    TextSegment,
    TranslatedSegment,
)
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


class StagedPipelineSession:
    """One bounded, FIFO speech-translation session.

    The ASR adapter is asynchronous.  Blocking NMT and TTS calls run on two
    separate single-worker executors, which provides stage overlap without a
    reorder buffer.  A model result is published only after the full stage
    succeeds; in particular, partial TTS chunks never reach the output queue.
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
        self._max_queue_depths: Dict[str, int] = {"nmt": 0, "tts": 0, "output": 0}
        self._blocked_put_counts: Dict[str, int] = {
            "nmt": 0,
            "tts": 0,
            "output": 0,
        }
        self._audio_segments_produced = 0
        self._fillers_discarded = 0
        self._emitted_sequence_ids: list[int] = []
        self._planned_subsegment_keys: list[Tuple[int, int, int]] = []
        self._synthesized_subsegment_keys: list[Tuple[int, int, int]] = []
        self._consumed_subsegment_keys: list[Tuple[int, int, int]] = []
        self._consumed_sequence_ids: list[int] = []
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
        self._output_queue.task_done()
        output = item.payload
        if not isinstance(output, StagedOutputEvent):
            raise StagedPipelineError("output queue contained an invalid item")
        if output.kind is StagedOutputEventKind.AUDIO:
            self._queue_slots["output"].release()
            self._advance_output_cursor(output.segment)
            self._consumed_subsegment_keys.append(output.segment.order_key)
            if output.segment.is_final_subsequence:
                self._consumed_sequence_ids.append(output.segment.sequence_id)
        segment = output.segment
        self._record(
            stage="output",
            event="dequeued",
            segment=segment,
            queue_depth=self._output_queue.qsize(),
            queue_capacity=(
                self.config.output_queue_maxsize
                if output.kind is StagedOutputEventKind.AUDIO
                else self._output_queue.maxsize
            ),
            queue_residence_ms=max(0.0, self._clock_ms() - item.enqueued_monotonic_ms),
            audio_bytes=len(segment.audio) if segment is not None else 0,
            audio_duration_ms=(segment.audio_duration_ms if segment is not None else 0.0),
        )
        if output.kind is not StagedOutputEventKind.AUDIO:
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
            "telemetry_schema_version": (
                2 if self.config.tts_subsegment_max_chars > 0 else 1
            ),
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
                    abort=self.nmt_client.disconnect,
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
                    self.tts_client.disconnect()

                try:
                    synthesized = await self._call_blocking(
                        self._tts_executor,
                        self.tts_client.synthesize,
                        translation,
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
                if not isinstance(synthesized, SynthesizedSegment):
                    contract_error = StagedPipelineError(
                        "TTS returned an invalid segment type"
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
                self._advance_tts_cursor(translation)
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
                    audio_bytes=len(synthesized.audio),
                    audio_duration_ms=synthesized.audio_duration_ms,
                    processing_duration_ms=synthesized.processing_duration_ms,
                    retry_count=synthesized.retry_count,
                )
                self._synthesized_subsegment_keys.append(
                    synthesized.order_key
                )
                await self._enqueue(
                    self._output_queue,
                    StagedOutputEvent(
                        kind=StagedOutputEventKind.AUDIO, segment=synthesized
                    ),
                    "output",
                    "enqueued",
                )
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
        synthesized: SynthesizedSegment,
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
            and payload.kind is not StagedOutputEventKind.AUDIO
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
        audio = payload.segment if isinstance(payload, StagedOutputEvent) else None
        self._record(
            stage=queue_name,
            event=event,
            segment=payload,
            queue_depth=depth,
            queue_capacity=(
                self.config.output_queue_maxsize
                if queue_name == "output" and not is_reserved_terminal
                else queue.maxsize
            ),
            blocked_put_ms=blocked_ms,
            text_chars=_text_chars(payload),
            audio_bytes=len(audio.audio) if audio is not None else 0,
            audio_duration_ms=audio.audio_duration_ms if audio is not None else 0.0,
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
    ) -> None:
        source = _source_segment(segment)
        subsequence = (
            _subsequence_identity(segment)
            if self.config.tts_subsegment_max_chars > 0
            else None
        )
        record = PipelineEvent(
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
            retry_count=retry_count,
            error_code=error_code,
        )
        if self._retain_telemetry:
            self._telemetry.append(record)
        if self._event_sink is not None:
            self._event_sink(record)


def _is_connected(client: Any) -> bool:
    check = getattr(client, "is_connected", None)
    return bool(check()) if callable(check) else False


def _source_segment(value: Any) -> Optional[TextSegment]:
    if isinstance(value, TextSegment):
        return value
    if isinstance(value, TranslatedSegment):
        return value.segment
    if isinstance(value, SynthesizedSegment):
        return value.translation.segment
    if isinstance(value, StagedOutputEvent) and value.segment is not None:
        return value.segment.translation.segment
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
    if isinstance(value, StagedOutputEvent) and value.segment is not None:
        return len(value.segment.translation.text)
    return 0
